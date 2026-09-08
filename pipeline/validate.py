"""Publish gates. Mirrors RatesDataset.validate in the app; [] means safe to publish."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_RATE = Decimal("0.15")
ZIP_RE = re.compile(r"^\d{5}$")
RATE_KEYS = {"stateReplaced": "rate", "combined": "rate", "surcharge": "extra"}


def _rate(v: object, where: str, errors: list[str]) -> None:
    if not isinstance(v, str):
        errors.append(f"{where}: rate must be a string, got {type(v).__name__}")
        return
    try:
        d = Decimal(v)
    except InvalidOperation:
        errors.append(f"{where}: rate {v!r} is not a decimal")
        return
    if not (0 <= d <= MAX_RATE):
        errors.append(f"{where}: rate {v} out of range")


def _rule(r: dict, where: str, errors: list[str]) -> None:
    t = r.get("t")
    if t in RATE_KEYS:
        _rate(r.get(RATE_KEYS[t]), where, errors)
    elif t == "threshold":
        if not isinstance(r.get("limit"), str):
            errors.append(f"{where}: threshold limit must be a string")
        _rule(r.get("above", {}), where, errors)
    elif t not in ("exempt", "general", "localOnly", "unknown"):
        errors.append(f"{where}: unknown rule type {t!r}")


# Default mirrors build_rates.MIN_ZIPS (decision #21): the covered states' ZCTAs total
# ~29 400 against a 33 791 nationwide count, so the earlier 38 000 floor was unreachable.
def validate(doc: dict, *, min_zips: int = 25000) -> list[str]:
    errors: list[str] = []
    if doc.get("schemaVersion") != "1":
        errors.append(f"schemaVersion: expected 1, got {doc.get('schemaVersion')!r}")
    profiles = doc.get("profiles", {})
    states = doc.get("states", {})
    zips = doc.get("zips", [])
    if len(zips) < min_zips:
        errors.append(f"zips: expected at least {min_zips}, got {len(zips)}")
    for pid, p in profiles.items():
        for k in ("stateRate", "localRate"):
            _rate(p.get(k), f"profile {pid} {k}", errors)
        if p.get("foodDrugRate") is not None:
            _rate(p["foodDrugRate"], f"profile {pid} foodDrugRate", errors)
        if isinstance(p.get("stateRate"), str) and isinstance(p.get("localRate"), str):
            try:
                if Decimal(p["stateRate"]) + Decimal(p["localRate"]) > MAX_RATE:
                    errors.append(f"profile {pid}: general rate out of range")
            except InvalidOperation:
                pass
        if p.get("state") not in states:
            errors.append(f"profile {pid}: no state rules for {p.get('state')}")
    for code, s in states.items():
        for cat, r in s.get("rules", {}).items():
            _rule(r, f"state {code}: rule {cat}", errors)
    seen: set[str] = set()
    for row in zips:
        z = row[0] if row else None
        if not isinstance(z, str) or not ZIP_RE.match(z):
            errors.append(f"zip {z!r}: malformed")
            continue
        if z in seen:
            errors.append(f"zip {z}: duplicate")
        seen.add(z)
        if row[3] not in profiles:
            errors.append(f"zip {z}: unknown profile {row[3]}")
    return errors
