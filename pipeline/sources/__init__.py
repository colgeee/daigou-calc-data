"""Adapter protocol. Each source module registers one Adapter instance in REGISTRY."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Protocol

from pipeline.census import Census, county_short, join_key, normalize_place
from pipeline.model import ZipRate


class Adapter(Protocol):
    name: str
    states: tuple[str, ...]

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]: ...


REGISTRY: list[Adapter] = []


# A jurisdiction the bounds build can draw: a county, keyed by its 5-digit TIGER GEOID, or a
# place inside one county, keyed by `(place GEOID7, county GEOID5)`. A municipality that
# straddles a county line is taxed differently on each side -- Chicago is 10.5% in Cook and
# 8.5% in its DuPage sliver -- so the county is part of a place's key on both sides of the
# join (spec §2.3).
JurisdictionKey = str | tuple[str, str]


class BoundsAdapter(Protocol):
    """An adapter that can also answer its rate table keyed by geography rather than by ZIP.
    `bounds_states` is the subset of `states` whose polygons the bounds build draws -- the
    SST adapter covers 24 states but only Nevada has a county-exact rate."""

    bounds_states: tuple[str, ...]

    def jurisdictions(self, census: Census, on: date) -> dict[JurisdictionKey, ZipRate]: ...


def geoid_index(census: Census, fips: str) -> tuple[
        dict[str, tuple[str, str]], dict[tuple[str, str], tuple[str, str, str]]]:
    """Reverse the Census relationship files for one state:
    `(county join key -> (county GEOID5, county label),
      (place join key, county join key) -> (place GEOID7, county GEOID5, place name))`.

    The relationship files are keyed by ZCTA, so this is the only offline way to put a TIGER
    GEOID on a jurisdiction a state names rather than codes (Illinois and New York both key
    on names). `Census.place` keeps one row per ZCTA -- the place with the largest land
    overlap -- so a place that never wins a ZCTA carries no GEOID here, gets no rate row, is
    not overlaid, and stays part of its county's unincorporated remainder. That is exactly
    what the ZIP path answers for it today, and it is the safe direction (decision #71)."""
    counties: dict[str, tuple[str, str]] = {}
    places: dict[tuple[str, str], tuple[str, str, str]] = {}
    for zcta, (county_geoid, county_namelsad) in census.county.items():
        if not county_geoid.startswith(fips):
            continue
        county_key = join_key(county_short(county_namelsad))
        counties.setdefault(county_key,
                            (county_geoid, census.county_label(zcta) or county_namelsad))
        p = census.place.get(zcta)
        if p and p[0]:
            places.setdefault((join_key(normalize_place(p[1])), county_key),
                              (p[0], county_geoid, census.place_display(zcta) or p[1]))
    return counties, places


def wanted_places(places: dict[tuple[str, str], tuple[str, str, str]]
                  ) -> dict[str, tuple[str, str]]:
    """Collapse `geoid_index`'s places to `place GEOID7 -> (join key, label name)`.

    The county in a `geoid_index` key is the county of the ZCTA that named the place, not
    the set of counties the municipality reaches, so it must not be read as the latter: a
    place that straddles a line is filed by the state in every county it reaches, and only
    the state's own table knows which those are. Dropping the county here lets a caller
    cross each place with every county of the state and let the rate table decide."""
    out: dict[str, tuple[str, str]] = {}
    for (place_key, _county_key), (place_geoid, _county_geoid, name) in places.items():
        out.setdefault(place_geoid, (place_key, name))
    return out
