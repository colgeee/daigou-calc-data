"""States whose rates are a hand-maintained YAML table: flat, regional, or zero (spec §3.8)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from pipeline.census import Census, display_name
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

RULES_DIR = Path(__file__).resolve().parent.parent / "rules" / "states"
FIPS = {
    "DE": "10", "MT": "30", "NH": "33", "OR": "41", "PA": "42", "MA": "25", "CT": "09",
    "MD": "24", "ME": "23", "MS": "28", "ID": "16", "HI": "15", "DC": "11", "VA": "51",
}


@dataclass
class StateTable:
    state: str
    state_rate: Decimal
    county_rates: dict[str, Decimal] = field(default_factory=dict)
    place_rates: dict[str, Decimal] = field(default_factory=dict)
    food_drug_rate: Decimal | None = None


def load_state(path: Path) -> StateTable:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    fd = doc.get("food_drug_rate")
    county_rates = doc.get("county_rates") or {}
    place_rates = doc.get("place_rates") or {}
    return StateTable(
        state=doc["state"].upper(),
        state_rate=Decimal(str(doc["state_rate"])),
        county_rates={str(k).upper(): Decimal(str(v)) for k, v in county_rates.items()},
        place_rates={str(k).upper(): Decimal(str(v)) for k, v in place_rates.items()},
        food_drug_rate=None if fd is None else Decimal(str(fd)),
    )


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
        # `.title()` makes of an uppercased name. `display_name` re-cases only the
        # fallback, for a ZIP the relationship files name no place or county for.
        name = census.place_display(zip5) or census.county_display(zip5)
        if name is None:
            name = display_name(place or census.county_name(zip5) or table.state)
        label = f"{name}, {table.state}"
        yield ZipRate(zip5, table.state, table.state_rate, local, table.food_drug_rate, label)


class YamlStatesAdapter:
    name = "yaml"
    states = tuple(sorted(FIPS))

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        for path in sorted(RULES_DIR.glob("*.yaml")):
            yield from rows_for(load_state(path), census)


REGISTRY.append(YamlStatesAdapter())
