"""Build out/v1/rates.json.gz from every registered adapter."""
from __future__ import annotations

import gzip
import io
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path

from pipeline import build_date, published_at, quarter_start
from pipeline.categories import load as load_categories
from pipeline.census import STATE_FIPS, Census
from pipeline.model import ZipRate, assemble
from pipeline.validate import validate

# The 2024 gazetteer carries 33 791 ZCTAs and a full build now prices ~33 600 of them --
# every state, since decision #26 publishes the eight without a local-rate source at their
# state rate -- so a build has to clear 25 000 rows to be publishable (decision #21). The
# per-state gate below is the tighter check: this floor only catches a wholesale collapse.
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


def check_partition(adapters: list) -> None:
    """Every state + DC must be claimed by exactly one adapter (F4).

    The per-state coverage gate below only measures the states an adapter actually claims,
    so an adapter dropped from the registry -- or one whose `states` tuple loses an entry in
    a refactor -- takes its states out of the build without tripping anything: the remaining
    ~33 000 ZIPs still clear `MIN_ZIPS`, and the app would simply stop being able to place a
    ZIP anywhere in the missing state. A state claimed twice is the other half of the same
    invariant: two adapters then race for the same ZIPs, resolved only by whichever happens
    to compose the higher rate.

    Raises ``ValueError`` naming the difference in either direction."""
    claimed: dict[str, list[str]] = {}
    for a in adapters:
        for st in a.states:
            claimed.setdefault(st, []).append(a.name)
    missing = sorted(set(STATE_FIPS) - set(claimed))
    unknown = sorted(set(claimed) - set(STATE_FIPS))
    twice = sorted(f"{st} ({'+'.join(n)})" for st, n in claimed.items() if len(n) > 1)
    problems = []
    if missing:
        problems.append(f"no adapter claims {', '.join(missing)}")
    if unknown:
        problems.append(f"claimed but not a state code: {', '.join(unknown)}")
    if twice:
        problems.append(f"claimed by more than one adapter: {', '.join(twice)}")
    if problems:
        raise ValueError(
            f"the {len(adapters)} adapters must partition all {len(STATE_FIPS)} states "
            f"+ DC -- " + "; ".join(problems)
        )


def build(census: Census, on: date, states: list[str] | None = None, adapters=None) -> dict:
    want = set(states or [])
    resolved = list(adapters) if adapters is not None else _adapters()
    if states is None:
        # A `--states` build is deliberately partial, so the partition is an invariant of
        # the full quarterly build alone.
        check_partition(resolved)
    best: dict[str, ZipRate] = {}
    dupes = 0
    claimed: set[str] = set()
    for adapter in resolved:
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
    categories = load_categories()
    # Decision #26: a state with no local-rate source is still published, at the state rate
    # with local 0, and the app asks the user for the local rate there. Print the count so a
    # local-rate adapter silently lost (or newly landed) shows up in the build log.
    flagged = sorted(s for s in counts if not categories.get(s, {}).get("localCoverage", True))
    print(f"[build] state-rate-only: {len(flagged)} of {len(counts)} states with rows "
          f"are localCoverage false ({', '.join(flagged) if flagged else 'none'})")
    return assemble(rows, census.centroids, categories,
                    effective=quarter_start(on).isoformat(), published=published_at())


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
