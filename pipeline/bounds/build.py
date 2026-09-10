"""Merge every source into `out/v1/bounds.json.gz`, behind the same gates the rates build
has (spec §2.3, decisions #62, #63).

The shape of `pipeline/build_rates.py`, deliberately: collect, assemble, validate, and only
then write -- atomically, both a compact gzip for the app and a pretty file for the diff.
Nothing here touches a coordinate: mapshaper simplifies each source and merges them into one
topology, and `topo.polygons_from_topojson` reads that topology back as the shipped
document with mapshaper's own `transform` and `arcs` intact."""
from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import date
from pathlib import Path

from pipeline import VERSION, build_date, published_at, quarter_start
from pipeline.bounds import Collected, ca, gu, mapshaper, tiger, wa
from pipeline.bounds.topo import polygons_from_topojson
from pipeline.bounds.validate import validate_bounds
from pipeline.build_rates import _gzip_bytes, _write_atomic
from pipeline.census import Census

SOURCES: dict[str, Callable[[Path, Census, date], Collected]] = {
    "cdtfa": ca.collect, "wa-dor": wa.collect, "tiger": tiger.collect, "gu": gu.collect,
}
# Per-source floors, checked on the merged file. CA 540, WA 403, NY 62 counties + its Pub
# 718 cities, NV 17, HI 5, GU 1 (spec §2.1). Illinois is the one floor that is not a token:
# the built layer is 1 392 pieces, not the ~292 the spec estimated before it existed, and the
# place-onto-county overlay plus the IDOR name join is the most fragile step in the build --
# a join that quietly stopped matching would leave the metro's municipalities un-overlaid and
# publish Chicago at Cook County's rate, which is exactly the silent wrong answer the floors
# exist to make loud. 1 000 is a shade under three quarters of the live count: room for a
# genuine TIGER revision, none for a collapsed join. 200 would have let six sevenths of the
# layer disappear unnoticed.
MIN_BY_SOURCE = {"cdtfa": 500, "wa-dor": 380, "tiger-il": 1000, "tiger-ny": 60,
                 "tiger-nv": 15, "tiger-hi": 5, "gu": 1}
# The council's gate. The measured total is ~700 KB, so this is headroom, not a target.
MAX_GZ_BYTES = 1_500_000
SIMPLIFY_PCT = 10


def _census() -> Census:
    """Its own indirection, the way `build_rates._census` is, so a test can hand the
    builder a three-ZCTA `Census` without fetching 80 MB of relationship files."""
    return Census.fetch()


def assemble(collected: list[Collected], topo_doc: dict, *, effective: str,
             published: str) -> dict:
    """The shipped document: merged profiles, mapshaper's topology, and the `sources` block.

    A profile id that maps to two different bodies is fatal rather than last-write-wins: the
    id embeds the state, the rates and the label, so two bodies under one id would mean
    `model.profile_id` stopped being a function of the profile and a polygon could be priced
    by whichever source happened to be collected second.

    Each source's `sources` entry carries the count of features it handed the merge
    (`polygons`), the count that came back out of it (`count`), and, for the TIGER layers,
    how many place pieces took their county's rate (`fell_back`)."""
    profiles: dict[str, dict] = {}
    for c in collected:
        for pid, entry in c.profiles.items():
            seen = profiles.get(pid)
            if seen is not None and seen != entry:
                raise ValueError(
                    f"profile id {pid!r} names two different profiles -- {seen} and {entry}"
                )
            profiles[pid] = entry

    transform, arcs, polygons = polygons_from_topojson(topo_doc)
    counts = Counter(p["source"] for p in polygons)
    sources: dict[str, dict] = {}
    for c in collected:
        for name, note in c.notes.items():
            # The one step in the build nothing else measures: `-simplify` and
            # `-merge-layers` are handed N features and their output is read back by
            # `polygons_from_topojson`, which skips any geometry that is not a Polygon or a
            # MultiPolygon. A feature lost in there would be invisible to every gate but the
            # coarse floors, so the count that went in has to equal the count that came out.
            got = counts.get(name, 0)
            if note.get("polygons") != got:
                raise ValueError(
                    f"{name}: {note.get('polygons')} features went into the merge but "
                    f"{got} polygons came out of it -- mapshaper or the topology reader "
                    f"dropped geometry"
                )
            sources[name] = {**note, "count": got}
    for name in sorted(sources):
        print(f"[bounds] {name}: {sources[name]['count']} polygons in the merged topology")
    return {"schemaVersion": VERSION, "effectiveDate": effective, "publishedAt": published,
            "transform": transform, "arcs": arcs, "profiles": profiles,
            "polygons": polygons, "sources": sources}


def build(work: Path, census: Census, on: date, sources: list[str]) -> dict:
    """Collect every requested source into `work`, simplify each on its own, merge them into
    one topology and assemble the document.

    Each source is simplified separately because `-simplify` ranks vertices across the whole
    dataset it is given (spec §2.3): one merged run would trade California's detail against
    TIGER's coastlines in a way nobody has measured."""
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        raise ValueError(
            f"unknown source(s) {', '.join(unknown)} -- known sources are "
            f"{', '.join(sorted(SOURCES))}"
        )
    collected = [SOURCES[name](work, census, on) for name in sources]
    simplified: list[Path] = []
    for name, c in zip(sources, collected, strict=True):
        dst = work / f"{name}.simplified.geojson"
        mapshaper.simplify(c.path, dst, SIMPLIFY_PCT)
        simplified.append(dst)
    merged = work / "merged.topojson"
    mapshaper.merge_to_topojson(simplified, merged)
    topo_doc = json.loads(merged.read_text(encoding="utf-8"))
    return assemble(collected, topo_doc, effective=quarter_start(on).isoformat(),
                    published=published_at())


def main(out_dir: str, sources: list[str]) -> int:
    out = Path(out_dir) / "v1"
    rates_path = out / "rates.json"
    if not rates_path.is_file():
        print(f"[bounds] REFUSING TO WRITE — {rates_path} is missing. The bounds file is "
              f"validated against the rates file it ships with, so build that first: "
              f"python -m pipeline rates")
        return 1
    # utf-8 explicitly: the file carries names like `Cañon City`, and Windows would open it
    # as cp950 and raise.
    rates_doc = json.loads(rates_path.read_text(encoding="utf-8"))
    on = build_date()
    try:
        with tempfile.TemporaryDirectory(prefix="daigou-bounds-") as tmp:
            doc = build(Path(tmp), _census(), on, sources)
    except Exception as e:
        # Every exception, not only the parsers' own ValueErrors: a shifted CDTFA or DOR
        # column raises KeyError, a truncated download zipfile.BadZipFile, the network
        # requests' own errors, and a traceback is exactly the wrong answer at the moment a
        # maintainer needs to be told the file was not written and why.
        print(f"[bounds] REFUSING TO WRITE — {type(e).__name__}: {e}")
        return 1
    # A `--sources cdtfa` dev build must not fail for the sources it did not ask for, so the
    # floors are narrowed to the layers this run actually produced. `tiger` fans out into
    # four layer names, hence the prefix match rather than an equality one.
    floors = {k: v for k, v in MIN_BY_SOURCE.items()
              if any(k == s or k.startswith(f"{s}-") for s in sources)}
    errors = validate_bounds(doc, rates_doc=rates_doc, min_by_source=floors,
                             max_gz_bytes=MAX_GZ_BYTES)
    print(f"[bounds] validation: {errors if not errors else f'{len(errors)} error(s)'}")
    if errors:
        print("[bounds] REFUSING TO WRITE — validation failed:")
        for e in errors[:50]:
            print("   ", e)
        return 1
    compact = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    pretty = json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")
    gz = _gzip_bytes(compact)
    out.mkdir(parents=True, exist_ok=True)
    _write_atomic(out / "bounds.json.gz", gz)
    _write_atomic(out / "bounds.json", pretty)
    print(f"[bounds] wrote {out / 'bounds.json.gz'} — {len(doc['polygons'])} polygons, "
          f"{len(doc['profiles'])} profiles, {len(gz)} bytes gzipped "
          f"({len(gz) / MAX_GZ_BYTES:.0%} of the gate)")
    return 0
