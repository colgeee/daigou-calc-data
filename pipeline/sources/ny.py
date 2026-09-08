"""New York: Publication 718 combined rates by county and city (spec §3.6)."""
from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from pypdf import PdfReader

from pipeline.census import Census, display_name, join_key
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

URL = "https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf"
STATE_RATE = Decimal("0.04")
# Publication 718 files no row for the five boroughs; each appears only as a
# `– see New York City` cross-reference. A ZIP in any of them pays the single New York
# City rate, whatever its Census place says, so the county is what decides.
NYC_COUNTIES = {"BRONX", "KINGS", "NEW YORK", "QUEENS", "RICHMOND"}
NYC_ROW = "NEW YORK CITY"
NYC_LABEL = "New York City, NY"
# New York has 62 counties and the publication carries a row for every one, the five
# boroughs folded into the single New York City row: 57 county rows plus that one. A PDF
# whose text layout shifted, or an error page served in its place, must fail the build
# loudly rather than quietly publish the 4% state rate across the state; a module
# constant so tests can lower it instead of shipping the whole publication as a fixture.
# Setting it to 0 disables the gate, the New York City check below included.
MIN_COUNTY_ROWS = 55
_FRAC = {"½": "0.5", "¼": "0.25", "¾": "0.75", "⅛": "0.125", "⅜": "0.375", "⅝": "0.625",
         "⅞": "0.875"}
# `<indent> [*]<name> <whole>[<fraction>] <4-digit reporting code>`. The asterisk flags
# the 3/8% Metropolitan Commuter Transportation District share, which is part of the
# published total and so of the local rate. `\s+` rather than a literal space because the
# `*Suffolk` row extracts TAB-separated while every other row extracts space-separated.
# Requiring the reporting code is what drops the surrounding prose, the borough
# cross-references (no rate, no code) and the MCTD footnote.
_ROW = re.compile(
    r"^\s*\*?\s*(?P<name>.+?)\s+(?P<whole>\d+)(?P<frac>[½¼¾⅛⅜⅝⅞])?\s+(?P<code>\d{4})\s*$"
)


@dataclass
class NyTable:
    """Uppercase jurisdiction name -> the **combined** state-plus-local rate published
    for it. `city_county` records which county each `(city)` row was indented under; a
    city is taxed at its own rate only inside that county (see `NyAdapter.rows`)."""

    counties: dict[str, Decimal] = field(default_factory=dict)
    cities: dict[str, Decimal] = field(default_factory=dict)
    city_county: dict[str, str] = field(default_factory=dict)


def _fetch_lines() -> list[str]:
    reader = PdfReader(io.BytesIO(get_cached(URL, ttl_days=30)))
    return "\n".join(p.extract_text() for p in reader.pages).splitlines()


def parse_lines(lines: Iterable[str]) -> NyTable:
    """Read the one-page rate table. Counties are listed alphabetically; a county that
    contains a separately-taxed city is filed as `<County> – except`, immediately
    followed by that county's indented `<City> (city)` rows, so the county last read is
    the county a city row belongs to. Rates are whole percents with an optional vulgar
    fraction (`8⅞`), and `New York State only 4` is the state share rather than a
    jurisdiction.

    Raises ``ValueError`` if implausibly few county rows parse, or if the New York City
    row is missing -- either is a sign the PDF's text layout shifted (F3)."""
    t = NyTable()
    county = ""
    for line in lines:
        m = _ROW.match(line)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group("name")).replace("–", "-").strip()
        if name.upper().startswith("NEW YORK STATE"):
            continue
        pct = Decimal(m.group("whole")) + Decimal(_FRAC.get(m.group("frac") or "", "0"))
        rate = pct / Decimal(100)
        if name.lower().endswith("(city)"):
            city = name[: -len("(city)")].strip().upper()
            t.cities[city] = rate
            t.city_county[city] = county
        else:
            county = re.sub(r"\s*-\s*except$", "", name, flags=re.I).strip().upper()
            t.counties[county] = rate
    if len(t.counties) < MIN_COUNTY_ROWS:
        raise ValueError(
            f"Publication 718: only {len(t.counties)} county rows parsed, expected at "
            f"least {MIN_COUNTY_ROWS} of New York's 62 counties -- the PDF may be an "
            f"error page or its text layout may have shifted"
        )
    if MIN_COUNTY_ROWS and NYC_ROW not in t.counties:
        raise ValueError(
            "Publication 718: no New York City row parsed -- every ZIP in the five "
            "boroughs depends on it, so the build must fail rather than drop them"
        )
    return t


class NyAdapter:
    name = "ny"
    states = ("NY",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        t = parse_lines(_fetch_lines())
        # `join_key` on both sides of every name join, as Texas and Illinois do, so a
        # spelling the two sources disagree on (`ST. LAWRENCE` vs `SAINT LAWRENCE`)
        # never silently drops a county's worth of ZIPs.
        counties = {join_key(n): r for n, r in t.counties.items()}
        cities = {join_key(n): (r, join_key(t.city_county.get(n, "")))
                  for n, r in t.cities.items()}
        counts = {"nyc": 0, "city": 0, "county": 0}
        dropped: dict[str, int] = {}
        for zip5 in census.zips_in_state("36"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            place = census.place_name(zip5)
            city = cities.get(join_key(place)) if place else None
            if county in NYC_COUNTIES:
                kind, total, label = "nyc", t.counties.get(NYC_ROW), NYC_LABEL
            elif city is not None and city[1] == join_key(county):
                # A `(city)` row is taxed only inside the county it is filed under, so
                # the ZIP's own county has to agree. `Oneida` is both a city taxed at 8%
                # (in Madison County) and a county taxed at 8.75%; a name-only match
                # would misprice one of them.
                kind, total, label = "city", city[0], f"{display_name(place)}, NY"
            else:
                # Unincorporated territory, a town or village Pub 718 does not tax
                # separately, or a city row whose county did not match: the ZIP pays its
                # county's combined rate.
                kind, total = "county", counties.get(join_key(county))
                label = f"{display_name(county)} County, NY"
            if total is None:
                dropped[county] = dropped.get(county, 0) + 1
                continue
            counts[kind] += 1
            # New York taxes food and drugs at the same combined rate as everything else
            # it taxes (most grocery food is exempt outright, not reduced-rated), so
            # there is no separate food/drug rate to publish.
            yield ZipRate(zip5, "NY", STATE_RATE, total - STATE_RATE, None, label)
        missing = f": {', '.join(sorted(dropped))}" if dropped else ""
        print(
            f"[ny] {counts['city']} ZIPs matched a Pub 718 city row, {counts['nyc']} took "
            f"the New York City rate, {counts['county']} took their county's rate, "
            f"{sum(dropped.values())} dropped across {len(dropped)} counties with no "
            f"row{missing}"
        )


REGISTRY.append(NyAdapter())
