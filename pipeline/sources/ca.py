"""California: CDTFA jurisdiction rates joined to ZIPs through Census place/county (spec §3.3)."""
from __future__ import annotations

import csv
import io
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


def _fetch_text() -> str:
    return get_cached(URL, ttl_days=7).decode("utf-8-sig")


def _start(value: str) -> date:
    """START_DATE is `M/D/YYYY H:MM:SS AM`; only the day is meaningful."""
    day = value.strip().split(" ")[0]
    return datetime.strptime(day, "%m/%d/%Y").date() if day else date.min


def parse(text: str) -> dict[tuple[str, str], tuple[Decimal, str]]:
    """Key every jurisdiction `(COUNTY, CITY)`, uppercase, with the county-wide
    unincorporated row keyed `(COUNTY, "")`. CDTFA writes that row's `City_name` as a bare
    `UNINCORPORATED` and carries the county only in `JURIS_NAME`; other extracts spell it
    `UNINCORPORATED AREA-<COUNTY>` in either column, so both shapes are matched. Where a
    jurisdiction repeats, the row with the latest `START_DATE` wins."""
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
        key = (county, "") if city.startswith(UNINC) or juris.startswith(UNINC) else (county, city)
        start = _start(row["START_DATE"])
        if key in began and start < began[key]:
            continue
        began[key] = start
        out[key] = (rate, row["City_Name_Proper"].strip())
    return out


class CaAdapter:
    name = "ca"
    states = ("CA",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        table = parse(_fetch_text())
        matched = fell_back = dropped = 0
        for zip5 in census.zips_in_state("06"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            place = census.place_name(zip5)
            hit = table.get((county, place)) if place else None
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
