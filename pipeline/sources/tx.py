"""Texas: Comptroller quarterly rate file joined to ZIPs through Census place/county (spec §3.4)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from pipeline.census import Census, display_name
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

URL = "https://comptroller.texas.gov/data/edi/sales-tax/taxrates.txt"
STATE_RATE = Decimal("0.0625")
# Tax Code §321.101(f): city, county and special-purpose district tax together may not
# exceed 2%, so no Texas ZIP is charged more than 8.25%. The file lists each overlay at
# its adopted rate, and a few compose past the ceiling; collection stops at the ceiling.
LOCAL_CAP = Decimal("0.02")


@dataclass(frozen=True)
class TxRow:
    city: str
    county: str
    city_rate: Decimal
    county_rate: Decimal
    spd_rate: Decimal


def _fetch_text() -> str:
    return get_cached(URL, ttl_days=7).decode("utf-8", "replace")


def _dec(s: str) -> Decimal:
    """Rates are plain decimal strings; the file's blank and ``n/a`` cells mean no tax."""
    try:
        return Decimal(s.strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def parse(text: str) -> list[TxRow]:
    """One row per jurisdiction: name, code and rate for the city, then the county, then
    two special-purpose district slots (MTA, crime control, ESD, library district...).
    The file opens with a header/notice line and a run of filing-due-date rows, each of
    which carries a period number where a jurisdiction name belongs."""
    out = []
    for line in text.splitlines():
        f = line.rstrip("\r").split("\t")
        if len(f) < 12 or f[0].strip().isdigit():
            continue
        out.append(TxRow(f[0].strip().upper(), f[3].strip().upper(), _dec(f[2]), _dec(f[5]),
                         _dec(f[8]) + _dec(f[11])))
    return out


def _county_rates(table: Iterable[TxRow]) -> dict[str, Decimal]:
    """Each county's own rate, read as the highest any of its rows reports. Where a city
    straddles a county line the Comptroller files a combined ``City/County Co`` row whose
    county column is zeroed, because the city's own overlays already fill the 2% cap;
    taking whichever row came first would read Hays and Kleberg counties as untaxed.

    Every county named anywhere in the file gets a key, a county that levies nothing
    included, so that a missing key means only ever "the Comptroller files no row"."""
    out: dict[str, Decimal] = {}
    for r in table:
        cur = out.get(r.county)
        if cur is None or r.county_rate > cur:
            out[r.county] = r.county_rate
    return out


class TxAdapter:
    name = "tx"
    states = ("TX",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        table = parse(_fetch_text())
        by_city_county = {(r.city, r.county): r for r in table}
        county_rate = _county_rates(table)
        matched = fell_back = no_county_row = capped = 0
        for zip5 in census.zips_in_state("48"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            place = census.place_name(zip5)
            row = by_city_county.get((place, county)) if place else None
            if row is not None:
                matched += 1
                local = row.city_rate + row.county_rate + row.spd_rate
                if local > LOCAL_CAP:
                    capped += 1
                    local = LOCAL_CAP
                label = f"{display_name(place)}, TX"
            else:
                # Unincorporated territory, or a Census place the Comptroller does not tax
                # under that name: the ZIP pays its county's rate. Five counties levy no
                # sales tax and so appear in no row at all; theirs pay the state rate only.
                if county in county_rate:
                    fell_back += 1
                else:
                    no_county_row += 1
                local = county_rate.get(county, Decimal("0"))
                label = f"{display_name(county)} County, TX"
            yield ZipRate(zip5, "TX", STATE_RATE, local, None, label)
        print(
            f"[tx] {matched} ZIPs matched a city ({capped} capped at 2%), {fell_back} fell "
            f"back to their county's rate, {no_county_row} sit in a county with no row"
        )


REGISTRY.append(TxAdapter())
