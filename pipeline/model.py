"""Frozen-schema data model shared by every adapter and the builder."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ZipRate:
    zip: str
    state: str
    state_rate: Decimal
    local_rate: Decimal
    food_drug_rate: Decimal | None
    label: str

    @property
    def general_rate(self) -> Decimal:
        return self.state_rate + self.local_rate


def rate_str(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return "0" if s in ("0", "-0", "0.0") else s


def rule(t: str, **kw: object) -> dict:
    out: dict = {"t": t}
    for k, v in kw.items():
        out[k] = rate_str(v) if isinstance(v, Decimal) else v
    return out


def profile_id(r: ZipRate) -> str:
    base = f"{r.state}-{rate_str(r.state_rate)}-{rate_str(r.local_rate)}-{r.label.upper()}"
    return base if r.food_drug_rate is None else f"{base}-FD{rate_str(r.food_drug_rate)}"


def _e6(x: float) -> int:
    return int(round(x * 1_000_000))


def assemble(zip_rates: list[ZipRate], centroids: dict[str, tuple[float, float]],
             states: dict, *, effective: str, published: str) -> dict:
    profiles: dict[str, dict] = {}
    zips: list[list] = []
    for r in zip_rates:
        c = centroids.get(r.zip)
        if c is None:
            continue
        pid = profile_id(r)
        profiles.setdefault(pid, {
            "state": r.state, "label": r.label,
            "stateRate": rate_str(r.state_rate), "localRate": rate_str(r.local_rate),
            "foodDrugRate": None if r.food_drug_rate is None else rate_str(r.food_drug_rate)})
        zips.append([r.zip, _e6(c[0]), _e6(c[1]), pid])
    zips.sort(key=lambda z: z[0])
    return {"schemaVersion": "1", "effectiveDate": effective, "publishedAt": published,
            "profiles": profiles, "states": states, "zips": zips}
