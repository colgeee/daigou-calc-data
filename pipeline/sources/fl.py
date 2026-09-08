"""Florida: 6 % state + county discretionary surtax from DR-15DSS (spec §3.7)."""
from __future__ import annotations

import io
import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from pypdf import PdfReader

from pipeline.census import Census, display_name, join_key
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY

URL = "https://floridarevenue.com/Forms_library/current/dr15dss.pdf"
STATE_RATE = Decimal("0.06")
# Florida has 67 counties and the DR-15DSS lists every one, those with no surtax included
# as a `None` row. A PDF whose text layout shifted, or an error page served in its place,
# must fail the build loudly rather than quietly publish the bare 6 % state rate across
# the state; a module constant so tests can lower it instead of shipping the whole form as
# a fixture. Setting it to 0 disables the gate.
MIN_COUNTY_ROWS = 60
# `<County> <total surtax>` at the very start of the line. Only the first rate on the line
# is read: it is the **Total Surtax Rate** column, and everything after it -- the
# parenthesised component surtaxes, their effective dates and their expiration dates --
# describes how that total is made up, not what to charge. A county whose total is built
# from more than one surtax continues on unindented `(.5%) Jan 1, 2015    Dec 31, 2035`
# lines, which the leading `[A-Z]` rejects. Anchoring at `^` with no leading `\s*` is also
# what drops page 2's indented bullet prose and both pages' indented column headings.
_ROW = re.compile(r"^(?P<county>[A-Z][A-Za-z.'\- ]+?)\s+(?P<rate>None|\.?\d+(?:\.\d+)?%)")
# Page 2 is a narrative of this year's and future years' rate changes, headed per county
# by `Hillsborough  1.5% Total Surtax Rate` -- shaped exactly like a table row. Each such
# heading restates a county already listed on page 1, so admitting them adds nothing, and
# two of them (`St Johns`, `St Lucie`) drop the period page 1 writes, which would both
# inflate the row count the sanity gate reads and file a second, differently-spelled key
# for a county already present.
_NARRATIVE = "Total Surtax Rate"
# The form titles itself `Discretionary Sales Surtax Information for Calendar Year 2025
# DR-15DSS`, and that year is the only thing in it that dates the rates. The URL is the
# `/current/` one, so a stale form is served as if it were current -- Florida was still
# serving the CY2025 form there on 2026-09-08, and the surtaxes it lists were still the
# ones in force -- but a form more than one year behind the build date means Florida has
# stopped updating it (or the fetch found some other document), and the build must fail
# rather than publish rates nobody is charging any more.
_CALENDAR_YEAR = re.compile(r"Calendar\s+Year\s+(\d{4})")


def _fetch_lines() -> list[str]:
    reader = PdfReader(io.BytesIO(get_cached(URL, ttl_days=30)))
    return [ln for p in reader.pages for ln in (p.extract_text() or "").splitlines()]


def parse_lines(lines: Sequence[str]) -> dict[str, Decimal]:
    """Read the DR-15DSS rate table into uppercase county name -> surtax fraction. A
    county with no surtax is filed as `Citrus None` and maps to zero, not to a missing
    key: Florida still taxes it, at the 6 % state rate alone.

    Takes a `Sequence` rather than an `Iterable`: `FlAdapter.rows` passes the same
    `lines` here and to `check_calendar_year` in turn, so it has to survive being read
    twice, which a single-use iterable (a generator, say) would not (F3).

    Raises ``ValueError`` if implausibly few county rows parse -- a sign the PDF's text
    layout shifted or the fetch returned something other than the form (F3)."""
    out: dict[str, Decimal] = {}
    for line in lines:
        if _NARRATIVE in line:
            continue
        m = _ROW.match(line)
        if not m:
            continue
        rate = m.group("rate")
        out[m.group("county").strip().upper()] = (
            Decimal("0") if rate == "None" else Decimal(rate.rstrip("%")) / Decimal(100)
        )
    if len(out) < MIN_COUNTY_ROWS:
        raise ValueError(
            f"DR-15DSS: only {len(out)} county rows parsed, expected at least "
            f"{MIN_COUNTY_ROWS} of Florida's 67 counties -- the PDF may be an error page "
            f"or its text layout may have shifted"
        )
    return out


def check_calendar_year(lines: Sequence[str], on: date) -> int:
    """Return the calendar year the form is published for, having checked it is not stale.

    Raises ``ValueError`` if the year is more than one behind `on`'s, or if the title
    line carries no year at all. One year of slack is deliberate: `/current/` served the
    CY2025 form throughout 2026 while its surtaxes were still the ones in force, so a
    build on 2026-09-08 must accept CY2025 and reject CY2024."""
    for line in lines:
        m = _CALENDAR_YEAR.search(line)
        if m:
            year = int(m.group(1))
            if year < on.year - 1:
                raise ValueError(
                    f"DR-15DSS: the form at {URL} is for calendar year {year}, more than "
                    f"a year behind the {on} build -- Florida has stopped updating it "
                    f"and its surtax rates can no longer be trusted"
                )
            return year
    raise ValueError(
        "DR-15DSS: no `Calendar Year <year>` line found -- the fetch returned something "
        "other than the form, or its title changed and the staleness check has gone blind"
    )


class FlAdapter:
    name = "fl"
    states = ("FL",)

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        lines = _fetch_lines()
        check_calendar_year(lines, on)
        table = parse_lines(lines)
        # `join_key` on both sides of the name join, as Texas and Illinois do, so a
        # spelling the two sources disagree on (`St. Johns` vs `Saint Johns`) never
        # silently drops a county's worth of ZIPs.
        rates = {join_key(n): r for n, r in table.items()}
        kept = 0
        dropped: dict[str, int] = {}
        for zip5 in census.zips_in_state("12"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            surtax = rates.get(join_key(county))
            if surtax is None:
                dropped[county] = dropped.get(county, 0) + 1
                continue
            kept += 1
            # The surtax is imposed by the county and by nothing below it -- Florida has
            # no city or district sales tax -- so the ZIP's Census place is irrelevant to
            # both the rate and the label. Every ZIP reads as its county.
            #
            # `food_drug_rate` is None: grocery food and prescription drugs are exempt
            # outright in Florida, not reduced-rated, so there is no second rate to
            # publish.
            #
            # The Census's own casing is the label (C1): `DeSoto County, FL`, not the
            # `Desoto` a re-cased uppercase name gives. `display_name` is the fallback.
            name = census.county_display(zip5) or display_name(county)
            yield ZipRate(zip5, "FL", STATE_RATE, surtax, None, f"{name} County, FL")
        missing = f": {', '.join(sorted(dropped))}" if dropped else ""
        print(
            f"[fl] {kept} ZIPs priced at their county's surtax, {sum(dropped.values())} "
            f"dropped across {len(dropped)} counties with no DR-15DSS row{missing}"
        )


REGISTRY.append(FlAdapter())
