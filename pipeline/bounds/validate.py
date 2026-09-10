"""Publish gates for `bounds.json.gz`; `[]` means safe to write (spec §2.3 step 3).

The shape of `pipeline/validate.py`, deliberately: a list of strings, every rule its own
message naming what it saw, and `build.main` printing `REFUSING TO WRITE` and exiting 1 so
`publish.sh` never runs."""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation

from pipeline.bounds.topo import decode_ring
from pipeline.build_rates import _gzip_bytes
from pipeline.validate import MAX_RATE

KEYS = frozenset({"schemaVersion", "effectiveDate", "publishedAt", "transform", "arcs",
                  "profiles", "polygons", "sources"})
# Illinois publishes a reduced food/drug rate in every jurisdiction, so a null one there is
# a lost column rather than a state without the concept.
FOOD_DRUG_REQUIRED = ("IL",)


def _rate(v: object, where: str, errors: list[str]) -> Decimal | None:
    if not isinstance(v, str):
        errors.append(f"{where}: rate must be a string, got {type(v).__name__}")
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        errors.append(f"{where}: rate {v!r} is not a decimal")
        return None
    if not (0 <= d <= MAX_RATE):
        errors.append(f"{where}: rate {v} out of range")
    return d


def validate_bounds(doc: dict, *, rates_doc: dict, min_by_source: dict[str, int],
                    max_gz_bytes: int = 1_500_000) -> list[str]:
    errors: list[str] = []
    for key in sorted(KEYS - set(doc)):
        errors.append(f"missing key {key}")
    if doc.get("schemaVersion") != "1":
        errors.append(f"schemaVersion: expected 1, got {doc.get('schemaVersion')!r}")
    if doc.get("effectiveDate") != rates_doc.get("effectiveDate"):
        errors.append(
            f"effectiveDate: {doc.get('effectiveDate')!r} does not equal the rates file's "
            f"{rates_doc.get('effectiveDate')!r} -- both files are stamped by one run"
        )

    profiles = doc.get("profiles", {})
    states = rates_doc.get("states", {})
    rates_profiles = rates_doc.get("profiles", {})
    for pid, p in profiles.items():
        # An id shared with the rates file must name the same profile in both. The id is
        # minted from the body, so a shared id with a different body means one of the two
        # sides re-derived a field the id does not distinguish -- in practice the label's
        # casing -- and the README's "both sides of a jurisdiction share one id" would be
        # true of the string and false of the thing it names.
        other = rates_profiles.get(pid)
        if other is not None and other != p:
            diffs = ", ".join(
                f"{k} {p.get(k)!r} vs {other.get(k)!r}"
                for k in sorted(set(p) | set(other)) if p.get(k) != other.get(k)
            )
            errors.append(f"profile {pid}: differs from the rates file ({diffs})")
        s = _rate(p.get("stateRate"), f"profile {pid} stateRate", errors)
        loc = _rate(p.get("localRate"), f"profile {pid} localRate", errors)
        if s is not None and loc is not None and s + loc > MAX_RATE:
            errors.append(f"profile {pid}: general rate out of range")
        if p.get("foodDrugRate") is not None:
            _rate(p["foodDrugRate"], f"profile {pid} foodDrugRate", errors)
        elif p.get("state") in FOOD_DRUG_REQUIRED:
            errors.append(f"profile {pid}: {p['state']} profiles must carry a foodDrugRate")
        if p.get("state") not in states:
            errors.append(f"profile {pid}: no state rules for {p.get('state')} in the rates file")

    arcs = doc.get("arcs", [])
    counts: Counter[str] = Counter()
    for poly in doc.get("polygons", []):
        counts[poly.get("source", "")] += 1
        if poly.get("profile") not in profiles:
            errors.append(f"polygon {poly.get('id')}: unknown profile {poly.get('profile')}")
        rings = poly.get("rings") or []
        if not rings:
            # A polygon with no rings encloses nothing: the app can never resolve a fix to
            # it, and it is as unusable as an open ring, which the loop below already
            # refuses.
            errors.append(f"polygon {poly.get('id')}: no rings")
        for ring in rings:
            # Per ring, so a dangling index in one ring cannot mask an open ring after it.
            ok = True
            for idx in ring:
                i = idx if idx >= 0 else -1 - idx
                if not (0 <= i < len(arcs)):
                    errors.append(f"polygon {poly.get('id')}: arc index {idx} out of range")
                    ok = False
            if not ok:
                continue
            pts = decode_ring(doc, ring)
            if len(pts) < 4 or pts[0] != pts[-1]:
                errors.append(f"polygon {poly.get('id')}: ring {ring} does not close")

    for source, floor in sorted(min_by_source.items()):
        got = counts.get(source, 0)
        if got == 0:
            errors.append(f"source {source}: no polygons -- the source produced nothing")
        elif got < floor:
            errors.append(f"source {source}: {got} polygons, expected at least {floor}")
    for source, got in sorted(counts.items()):
        declared = (doc.get("sources", {}).get(source) or {}).get("count")
        if declared != got:
            errors.append(f"sources.{source}.count is {declared!r} but {got} polygons carry it")

    gz = len(_gzip_bytes(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()))
    if gz > max_gz_bytes:
        errors.append(f"size: {gz} bytes gzipped, over the {max_gz_bytes}-byte gate")
    return errors
