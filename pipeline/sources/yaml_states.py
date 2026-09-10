"""States whose rates are a hand-maintained YAML table: flat, regional, or zero (spec §3.8)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from pipeline.census import Census, display_name
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

RULES_DIR = Path(__file__).resolve().parent.parent / "rules" / "states"
FIPS = {
    "DE": "10", "MT": "30", "NH": "33", "OR": "41", "PA": "42", "MA": "25", "CT": "09",
    "MD": "24", "ME": "23", "MS": "28", "ID": "16", "HI": "15", "DC": "11", "VA": "51",
    # State-rate-only states: no local-rate source, so their YAML carries the state rate
    # alone and categories.yaml flags them `localCoverage: false` (decision #26). With no
    # row at all the app cannot place the ZIP and says "couldn't get a location"; with one
    # it shows the state rate and asks the user for the local rate.
    "CO": "08", "LA": "22", "AL": "01", "AK": "02", "SC": "45", "MO": "29", "AZ": "04",
    "NM": "35",
    # Guam: one territory-wide taxing authority, zero-rated at the register (spec §2.6).
    "GU": "66",
}


@dataclass
class StateTable:
    state: str
    state_rate: Decimal
    county_rates: dict[str, Decimal] = field(default_factory=dict)
    place_rates: dict[str, Decimal] = field(default_factory=dict)
    food_drug_rate: Decimal | None = None
    # `pass_on: true` means the tax is levied on the seller and what a receipt adds is the
    # grossed-up maximum pass-on rate (Hawaii's GET; spec §2.6, decision #67). `label`
    # overrides the per-ZIP label outright, for a territory that is one jurisdiction. `bounds`
    # is the WGS84 rectangle Task 6's `pipeline/bounds/` draws instead of a Census polygon.
    pass_on: bool = False
    label: str | None = None
    bounds: tuple[float, float, float, float] | None = None   # lat_min, lat_max, lon_min, lon_max


def load_state(path: Path) -> StateTable:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    fd = doc.get("food_drug_rate")
    county_rates = doc.get("county_rates") or {}
    place_rates = doc.get("place_rates") or {}
    bounds = doc.get("bounds")
    return StateTable(
        state=doc["state"].upper(),
        state_rate=Decimal(str(doc["state_rate"])),
        county_rates={str(k).upper(): Decimal(str(v)) for k, v in county_rates.items()},
        place_rates={str(k).upper(): Decimal(str(v)) for k, v in place_rates.items()},
        food_drug_rate=None if fd is None else Decimal(str(fd)),
        pass_on=bool(doc.get("pass_on", False)),
        label=(str(doc["label"]) if doc.get("label") else None),
        bounds=None if bounds is None else (
            float(bounds["lat_min"]), float(bounds["lat_max"]),
            float(bounds["lon_min"]), float(bounds["lon_max"]),
        ),
    )


_Q6 = Decimal("0.000001")


def _round6(d: Decimal) -> Decimal:
    return d.quantize(_Q6, rounding=ROUND_HALF_UP)


def pass_on_split(state_rate: Decimal, local_rate: Decimal) -> tuple[Decimal, Decimal]:
    """The maximum pass-on split for a tax levied on the seller (spec §2.6, decision #67).

    Hawaii's GET is not a sales tax: it is charged to the business, which may pass it on
    at a grossed-up rate -- 4.5% of the *price* is 4.712% of the pre-tax price, because
    the tax is charged on the tax too. The gross-up is done **once, on the combined
    rate**, and the state share is then the same denominator applied to the state rate
    alone, so the two published parts always sum to the official figure rather than to a
    number 0.000001 either side of it: 0.041885 + 0.005235 = 0.047120."""
    combined = state_rate + local_rate
    if combined >= 1:
        raise ValueError(f"pass-on rate: combined rate {combined} is not below 1")
    g = _round6(combined / (1 - combined))
    s = _round6(state_rate / (1 - combined))
    return s, g - s


def published_rates(table: StateTable, local: Decimal) -> tuple[Decimal, Decimal]:
    """The (state, local) pair a row publishes: the table's own rates, or the pass-on split
    when the state's YAML sets `pass_on: true`. `rows_for` and the bounds build's
    `jurisdictions` both go through this, so a polygon and a ZIP row for the same county
    mint the same profile id."""
    return pass_on_split(table.state_rate, local) if table.pass_on else (table.state_rate, local)


def rows_for(table: StateTable, census: Census) -> Iterable[ZipRate]:
    for zip5 in census.zips_in_state(FIPS[table.state]):
        if zip5 not in census.centroids:
            continue
        place = census.place_name(zip5)
        county_geoid, _county_name = census.county[zip5]
        local = Decimal("0")
        if place and place in table.place_rates:
            local = table.place_rates[place]
        elif county_geoid in table.county_rates:
            local = table.county_rates[county_geoid]
        elif census.county_name(zip5) in table.county_rates:
            local = table.county_rates[census.county_name(zip5)]
        # The Census's own casing is the label (C1) -- `O'Fallon`, `McKeesport`, not what
        # `.title()` makes of an uppercased name. A ZIP with no place reads as its county
        # the way the Census names it, entity word and all (`Accomack County`, `Acadia
        # Parish`), bar an independent city (`Williamsburg`). `display_name` re-cases only
        # the last-ditch fallback, for a ZIP the files name no place or county for.
        name = census.place_display(zip5) or census.county_label(zip5)
        if not name:
            name = display_name(place or census.county_name(zip5) or table.state)
        state_rate, local_rate = published_rates(table, local)
        # A state whose whole territory is one jurisdiction overrides the label outright,
        # so every row (and its polygon) reads `Guam` rather than `Merizo, GU`.
        label = table.label or f"{name}, {table.state}"
        yield ZipRate(zip5, table.state, state_rate, local_rate, table.food_drug_rate, label)


class YamlStatesAdapter:
    name = "yaml"
    states = tuple(sorted(FIPS))

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        for path in sorted(RULES_DIR.glob("*.yaml")):
            yield from rows_for(load_state(path), census)


REGISTRY.append(YamlStatesAdapter())
