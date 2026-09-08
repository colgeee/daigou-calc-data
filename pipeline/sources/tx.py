"""Texas: Comptroller quarterly rate file joined to ZIPs through Census place/county (spec §3.4)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from pipeline.census import Census, display_name, join_key
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

URL = "https://comptroller.texas.gov/data/edi/sales-tax/taxrates.txt"
STATE_RATE = Decimal("0.0625")
# Tax Code §321.101(f): city, county and special-purpose district tax together may not
# exceed 2%, so no Texas ZIP is charged more than 8.25%. The file lists each overlay at
# its adopted rate, and a few compose past the ceiling; collection stops at the ceiling.
LOCAL_CAP = Decimal("0.02")
# The live file runs ~3 700 jurisdiction rows across 252 counties (see task-7-report.md).
# An error page or a re-ordered file must fail the build loudly rather than quietly
# publish the state rate everywhere; this is a module constant so tests can monkeypatch
# it down instead of shipping a multi-thousand-row fixture.
MIN_DATA_ROWS = 3000


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


def _clean(s: str) -> str:
    """Collapse internal whitespace and uppercase a jurisdiction name, matching
    ``census.normalize_place``'s own whitespace rule (M4). ``census.join_key`` strips
    whitespace entirely for join purposes and so subsumes this for matching, but
    ``parse`` still normalizes here to keep ``TxRow.city``/``.county`` readable on
    their own."""
    return re.sub(r"\s+", " ", s.strip()).upper()


def _capped(total: Decimal) -> Decimal:
    """Enforce ``LOCAL_CAP`` in exactly one place, for both a matched city row's own
    composed local and a fallback county's highest composed local."""
    return total if total <= LOCAL_CAP else LOCAL_CAP


def parse(text: str) -> list[TxRow]:
    """One row per jurisdiction: name, code and rate for the city, then the county, then
    two special-purpose district slots (MTA, crime control, ESD, library district...).
    The file opens with a header/notice line and a run of filing-due-date rows, each of
    which carries a period number where a jurisdiction name belongs.

    Raises ``ValueError`` (F3) if the parse yields implausibly few rows or names no
    county at all -- either is a sign the file is an error page or its columns shifted,
    and the build must fail rather than silently publish the state rate everywhere."""
    out = []
    for line in text.splitlines():
        f = line.rstrip("\r").split("\t")
        if len(f) < 12 or f[0].strip().isdigit():
            continue
        out.append(TxRow(_clean(f[0]), _clean(f[3]), _dec(f[2]), _dec(f[5]),
                         _dec(f[8]) + _dec(f[11])))
    if len(out) < MIN_DATA_ROWS:
        raise ValueError(
            f"Texas rate file: only {len(out)} data rows parsed, expected at least "
            f"{MIN_DATA_ROWS} -- the file may be an error page or its columns reordered"
        )
    if not any(r.county for r in out):
        raise ValueError("Texas rate file: no row names a county -- the file may be reordered")
    return out


def _county_max_local(table: Iterable[TxRow]) -> dict[str, Decimal]:
    """Each county's highest composed city+county+SPD local, read as the maximum raw
    (uncapped -- the caller applies ``_capped``) total over every row filed anywhere in
    that county (D23). This is deliberately not just the county column: where a city
    straddles a county line the Comptroller files a combined ``City/County Co`` row
    whose county column is zeroed, because the city's own overlays already fill the 2%
    cap (e.g. ``Austin/Hays … Hays n/a 0`` and ``Corpus Christi/Kleberg Co … Kleberg n/a
    0``); reading only that column, or whichever row happens to sort first, would
    understate -- or zero out -- the rate paid by unincorporated territory in Hays and
    Kleberg counties.

    Every county named anywhere in the file gets a key, a county whose rows compose to
    zero included, so a missing key means only ever "the Comptroller files no row"."""
    out: dict[str, Decimal] = {}
    for r in table:
        total = r.city_rate + r.county_rate + r.spd_rate
        cur = out.get(r.county)
        if cur is None or total > cur:
            out[r.county] = total
    return out


class TxAdapter:
    name = "tx"
    states = ("TX",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        table = parse(_fetch_text())
        by_city_county = {(join_key(r.city), r.county): r for r in table}
        county_local = _county_max_local(table)
        matched = fell_back = no_county_row = capped = 0
        for zip5 in census.zips_in_state("48"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            place = census.place_name(zip5)
            row = by_city_county.get((join_key(place), county)) if place else None
            if row is not None:
                matched += 1
                raw = row.city_rate + row.county_rate + row.spd_rate
                # The Census's own casing is the label (C1): `DeSoto`, `McKinney`. The
                # `display_name` re-casing is only the fallback.
                label = f"{census.place_display(zip5) or display_name(place)}, TX"
            else:
                # Unincorporated territory, or a Census place the Comptroller does not tax
                # under that name: the ZIP pays its county's highest filed local rate.
                # Five counties levy no sales tax and so appear in no row at all; theirs
                # pay the state rate only.
                if county in county_local:
                    fell_back += 1
                else:
                    no_county_row += 1
                raw = county_local.get(county, Decimal("0"))
                label = f"{census.county_display(zip5) or display_name(county)} County, TX"
            local = _capped(raw)
            if local != raw:
                capped += 1
            yield ZipRate(zip5, "TX", STATE_RATE, local, None, label)
        print(
            f"[tx] {matched} ZIPs matched a city, {fell_back} fell back to their "
            f"county's highest filed local rate, {no_county_row} sit in a county with "
            f"no row filed ({capped} capped at 2%)"
        )


REGISTRY.append(TxAdapter())
