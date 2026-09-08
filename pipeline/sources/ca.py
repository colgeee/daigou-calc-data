"""California: CDTFA jurisdiction rates joined to ZIPs through Census place/county (spec §3.3)."""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal

from pipeline.census import Census, display_name
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

URL = "https://gis.data.ca.gov/api/download/v1/items/01883a79765a4afba132ba54da408d8b/csv?layers=1"
STATE_RATE = Decimal("0.0725")
UNINC = "UNINCORPORATED"

# Census's normalised place name diverges from CDTFA's `City_name` key for a handful of
# cities. Keyed by the normalised (suffix-stripped, uppercase) Census name.
ALIASES: dict[str, str] = {
    "SAN BUENAVENTURA (VENTURA)": "VENTURA",
    "ANGELS": "ANGELS CAMP",
}

_PAREN = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")


def _city_candidates(place: str) -> Iterable[str]:
    """CDTFA city keys to try for a normalised Census place name, in priority order: the
    alias (if any), the exact name, then — if the name carries a parenthetical, as Census
    sometimes does for a city known by two names (``SAN BUENAVENTURA (VENTURA)``) — the
    parenthetical alone and the name with it stripped."""
    alias = ALIASES.get(place)
    if alias is not None:
        yield alias
    yield place
    m = _PAREN.match(place)
    if m:
        before, inside = m.group(1).strip(), m.group(2).strip()
        if inside:
            yield inside
        if before:
            yield before


def _fetch_text() -> str:
    return get_cached(URL, ttl_days=7).decode("utf-8-sig")


def _start(value: str) -> date:
    """START_DATE is `M/D/YYYY H:MM:SS AM`; only the day is meaningful."""
    day = value.strip().split(" ")[0]
    return datetime.strptime(day, "%m/%d/%Y").date() if day else date.min


def parse(
    text: str, on: date | None = None
) -> dict[tuple[str, str], tuple[Decimal, str]]:
    """Key every jurisdiction `(COUNTY, CITY)`, uppercase, with the county-wide
    unincorporated row keyed `(COUNTY, "")`. CDTFA writes that row's `City_name` as a bare
    `UNINCORPORATED` and carries the county only in `JURIS_NAME`; other extracts spell it
    `UNINCORPORATED AREA-<COUNTY>` in either column, so both shapes are matched. Rows whose
    `START_DATE` is after `on` (today, when `on` is None) are ignored — CDTFA pre-publishes
    a future quarter's rate ahead of its effective date. Where a jurisdiction repeats among
    the remaining rows, the one with the latest `START_DATE` wins."""
    cutoff = on if on is not None else date.today()
    out: dict[tuple[str, str], tuple[Decimal, str]] = {}
    began: dict[tuple[str, str], date] = {}
    for row in csv.DictReader(io.StringIO(text)):
        county = row["County_name"].strip().upper()
        city = row["City_name"].strip().upper()
        juris = row["JURIS_NAME"].strip().upper()
        rate = Decimal(row["RATE"].strip())
        if rate < STATE_RATE:
            raise ValueError(
                f"{juris} ({county}): RATE {rate} is below the state rate {STATE_RATE}"
            )
        start = _start(row["START_DATE"])
        if start > cutoff:
            continue
        key = (county, "") if city.startswith(UNINC) or juris.startswith(UNINC) else (county, city)
        if key in began and start < began[key]:
            continue
        began[key] = start
        out[key] = (rate, row["City_Name_Proper"].strip())
    return out


class CaAdapter:
    name = "ca"
    states = ("CA",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        table = parse(_fetch_text(), on)
        matched = fell_back = dropped = 0
        for zip5 in census.zips_in_state("06"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            place = census.place_name(zip5)
            hit = None
            if place:
                for candidate in _city_candidates(place):
                    hit = table.get((county, candidate))
                    if hit is not None:
                        break
            label = None
            if hit is None:
                # A CDP or an unmatched name is unincorporated territory of its county.
                hit = table.get((county, ""))
                label = f"{display_name(county)} County, CA"
            if hit is None:
                # San Francisco is a consolidated city-county with no unincorporated row.
                dropped += 1
                continue
            if label is None:
                matched += 1
            else:
                fell_back += 1
            total, proper = hit
            yield ZipRate(
                zip5, "CA", STATE_RATE, total - STATE_RATE, None,
                label or f"{display_name(proper)}, CA",
            )
        print(
            f"[ca] {matched} ZIPs matched a city, {fell_back} fell back to the county's "
            f"unincorporated rate, {dropped} dropped with no unincorporated row"
        )


REGISTRY.append(CaAdapter())
