"""Build out/v1/rates.json.gz from every registered adapter."""
from __future__ import annotations

import gzip
import io
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

from pipeline import build_date, next_quarter_start, published_at
from pipeline.categories import load as load_categories
from pipeline.census import STATE_FIPS, Census
from pipeline.model import ZipRate, assemble
from pipeline.validate import validate

# The 2024 gazetteer carries 33 791 ZCTAs and the covered states hold ~29 400 of them, so a
# full build has to clear 25 000 rows to be publishable (decision #21).
MIN_ZIPS = 25000
# Per-state floor: an adapter that silently loses most of its state still clears MIN_ZIPS,
# so every state an adapter claims must emit at least this share of its Census ZCTAs.
MIN_STATE_COVERAGE = 0.6


def _census() -> Census:
    return Census.fetch()


def _adapters():
    from pipeline.sources import (  # noqa: F401  (import registers)
        REGISTRY,
        ca,
        fl,
        il,
        ny,
        sst,
        tx,
        yaml_states,
    )
    return list(REGISTRY)


def build(census: Census, on: date, states: list[str] | None = None, adapters=None) -> dict:
    want = set(states or [])
    best: dict[str, ZipRate] = {}
    dupes = 0
    claimed: set[str] = set()
    for adapter in adapters if adapters is not None else _adapters():
        if want and not (set(adapter.states) & want):
            continue
        claimed |= {s for s in adapter.states if not want or s in want}
        emitted = 0
        for r in adapter.rows(census, on):
            if want and r.state not in want:
                continue
            emitted += 1
            if r.zip in best:
                dupes += 1
                if r.general_rate <= best[r.zip].general_rate:
                    continue
            best[r.zip] = r
        print(f"[build] adapter {adapter.name}: {emitted} rows")
    print(f"[build] {dupes} duplicate ZIP rows resolved to the higher rate")
    rows = sorted(best.values(), key=lambda r: r.zip)
    counts = Counter(r.state for r in rows)
    print(f"[build] {'state':<7}{'emitted':>9}{'census':>9}{'cover':>9}")
    short: list[str] = []
    for st in sorted(claimed):
        fips = STATE_FIPS.get(st)
        if fips is None:
            raise ValueError(f"adapter claims unknown state code {st!r}")
        got = counts.get(st, 0)
        total = len(census.zips_in_state(fips))
        pct = got / total if total else 0.0
        print(f"[build] {st:<7}{got:>9}{total:>9}{pct:>8.1%}")
        if pct < MIN_STATE_COVERAGE:
            short.append(f"{st} {got}/{total} ({pct:.1%})")
    if short:
        raise ValueError(
            f"state coverage below {MIN_STATE_COVERAGE:.0%}: " + ", ".join(short)
        )
    print(f"[build] total: {len(rows)} ZIPs across {len(counts)} state codes")
    return assemble(rows, census.centroids, load_categories(),
                    effective=next_quarter_start(on).isoformat(), published=published_at())


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a same-directory `.tmp` file plus `os.replace`, so an
    interrupted or failing write can never leave a truncated file at `path` (F2). Writing
    in binary mode also sidesteps text-mode newline translation, so `rates.json` keeps
    its `\\n` line endings verbatim on Windows (F3). Any failure removes the `.tmp` file
    rather than leaving it behind."""
    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _gzip_bytes(data: bytes) -> bytes:
    """Gzip `data` with a zeroed mtime and no embedded filename, so the same document
    always produces byte-identical output (F1). `gzip.open`/`gzip.compress` stamp the
    wall-clock build time into the header instead, which would make every build's bytes
    differ even when the document content is unchanged."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    return buf.getvalue()


def main(out_dir: str, states: list[str]) -> int:
    try:
        doc = build(_census(), build_date(), states or None)
    except ValueError as e:
        print(f"[build] REFUSING TO WRITE — {e}")
        return 1
    errors = validate(doc, min_zips=MIN_ZIPS if not states else 1)
    print(f"[build] validation: {errors if not errors else f'{len(errors)} error(s)'}")
    if errors:
        print("[build] REFUSING TO WRITE — validation failed:")
        for e in errors[:50]:
            print("   ", e)
        return 1
    out = Path(out_dir) / "v1"
    out.mkdir(parents=True, exist_ok=True)
    compact = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    pretty = json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")
    _write_atomic(out / "rates.json.gz", _gzip_bytes(compact))
    _write_atomic(out / "rates.json", pretty)
    print(f"[build] wrote {out / 'rates.json.gz'} — "
          f"{len(doc['zips'])} ZIPs, {len(doc['profiles'])} profiles")
    return 0
