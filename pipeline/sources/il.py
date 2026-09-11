"""Illinois: IDOR fixed-width county/municipality rate file (spec §3.5).

The 18 records whose "rate varies" flag is set -- the Metro-East jurisdictions in
St. Clair and Madison counties, where a business district or a home-rule sliver taxes
some addresses above the rest -- are published at their low rate, the one every other
address in the jurisdiction pays.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pipeline.census import Census, display_name, join_key
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY, JurisdictionKey, geoid_index, wanted_places

URL = (
    "https://tax.illinois.gov/content/dam/soi/en/web/tax/research/taxrates/documents/"
    "salestaxrates/ordmache-current.txt"
)
STATE_RATE = Decimal("0.0625")
# The live file runs ~1 340 usable Illinois jurisdiction records (102 counties and their
# municipalities). An error page, or a file whose records silently stopped parsing, must
# fail the build loudly rather than quietly publish 6.25% across the state; a module
# constant so tests can monkeypatch it down instead of shipping a 1 600-row fixture.
MIN_DATA_ROWS = 1200
# Illinois has exactly 102 counties and the live file carries a `<COUNTY> COUNTY` row for
# every one of them. Those rows are what unincorporated territory falls back to, so a
# shift that leaves the record count intact but breaks the county rows must fail too.
MIN_COUNTY_ROWS = 90

# Fixed-width record layout, 211 characters per line. Header: location id [0:10], name
# [10:35], county [35:60], address-override flag [60], the current period's start date
# [61:69]. Then THREE 21-character rate groups for that period -- high general
# merchandise, high food/drug, low general merchandise, low food/drug, "rate varies"
# flag, each rate an implied five-digit fraction of 100 000 (``07750`` = 7.750%) --
# followed by the PRIOR period's start and end dates [132:140] and [140:148] and its own
# three groups. Only the first group of a period holds a jurisdiction's own retail rates,
# so the adapter reads that group's low pair, from whichever period covers the build date.
_OVERRIDE_FLAG = 60
_BEGIN = slice(61, 69)
_GM_LOW = slice(79, 84)
_DM_LOW = slice(84, 89)
_PRIOR_BEGIN = slice(132, 140)
_PRIOR_END = slice(140, 148)
_PRIOR_GM_LOW = slice(158, 163)
_PRIOR_DM_LOW = slice(163, 168)

GROCERY_URL = (
    "https://tax.illinois.gov/content/dam/soi/en/web/tax/research/taxrates/documents/"
    "salestaxrates/grocerymache-current.txt"
)
# The live grocery file runs 1 596 records: 102 counties, ~1 440 municipalities and 52 all-zero
# rows for OTHER states. Same floors, and the same reason, as the ordinance file's.
MIN_GROCERY_ROWS = 1200
MIN_GROCERY_COUNTY_ROWS = 90
# 888 of those 1 596 records carry a non-zero grocery rate, and the rest are genuinely at 0%
# because their jurisdiction adopted no local grocery tax: 708 of the 1 596 records, 656 of the
# 1 544 the parse keeps, the difference being that all 52 dropped out-of-state rows are zero.
# Columns that shift into the record's padding parse as blanks, and blanks read as 0% -- an
# all-zero table is a plausible-looking answer, not an obviously broken one, so a floor on the
# non-zero rows is what tells the two apart.
MIN_NONZERO_GROCERY_ROWS = 300
# The tax is 1% municipal or county (65 ILCS 5/8-11-24, 55 ILCS 5/5-1006.9) plus NITA or MED,
# and the published maximum is 2.5%. Anything above 5% is a general-merchandise column read as
# a grocery one.
MAX_GROCERY_RATE = Decimal("0.05")

# Fixed-width record layout, 106 characters per line, per IDOR file guide IDR-1028 (N-12/25).
# Header: location id [0:10], name [10:35], county [35:60], the current period's start date
# [60:68]. Then ONE 11-character rate group for that period -- grocery high, grocery low,
# over-ride flag -- followed by the prior period's start and end dates and its own group.
#
# Two differences from the ordinance file. There is no address-override flag: the ordinance
# file zeroes the rate group for the ~200 municipalities IDOR taxes by address, but the grocery
# file files a usable rate for every location. And the over-ride flag here is the ordinance
# file's *Receipts* over-ride, not its address one -- it marks the 52 Metro-East jurisdictions
# where the MED district makes the high and low rates differ, and the low is what every address
# outside the district pays, which is the pair this module already reads from the ordinance file.
_G_BEGIN = slice(60, 68)
_G_LOW = slice(73, 78)
_G_PRIOR_BEGIN = slice(79, 87)
_G_PRIOR_END = slice(87, 95)
_G_PRIOR_LOW = slice(100, 105)


@dataclass(frozen=True)
class IlRow:
    """A jurisdiction's two published rate periods: the current one, open-ended from
    `begin`, and the prior one, `prior_begin` to `prior_end` inclusive."""

    location_id: str
    name: str
    county: str
    begin: date | None
    gm_low: Decimal
    dm_low: Decimal
    prior_begin: date | None = None
    prior_end: date | None = None
    prior_gm_low: Decimal = Decimal(0)
    prior_dm_low: Decimal = Decimal(0)

    def rates(self, on: date) -> tuple[Decimal, Decimal, bool]:
        """The (general merchandise, food/drug) pair in force on `on`, and whether a
        period actually covered it. The current period runs from `begin` with no end, so
        it answers for every build date at or after it; otherwise the prior period does,
        if `on` falls inside it. A date before both -- a back-dated build of a
        jurisdiction incorporated since -- has no published rate at all, so the current
        one stands in and the caller counts it. A record with no readable start date is
        read as current, which is what the synthetic fixtures rely on."""
        if self.begin is None or on >= self.begin:
            return self.gm_low, self.dm_low, True
        if self.prior_begin and self.prior_end and self.prior_begin <= on <= self.prior_end:
            return self.prior_gm_low, self.prior_dm_low, True
        return self.gm_low, self.dm_low, False


@dataclass(frozen=True)
class GroceryRow:
    """A jurisdiction's published grocery rate over its two periods: the current one,
    open-ended from `begin`, and the prior one, `prior_begin` to `prior_end` inclusive."""

    location_id: str
    name: str
    county: str
    begin: date | None
    low: Decimal
    prior_begin: date | None = None
    prior_end: date | None = None
    prior_low: Decimal = Decimal(0)

    def rate(self, on: date) -> tuple[Decimal, bool]:
        """The grocery rate in force on `on`, and whether a period actually covered it --
        the ladder `IlRow.rates` walks, for the same reasons."""
        if self.begin is None or on >= self.begin:
            return self.low, True
        if self.prior_begin and self.prior_end and self.prior_begin <= on <= self.prior_end:
            return self.prior_low, True
        return self.low, False


def _fetch_text() -> str:
    return get_cached(URL, ttl_days=30).decode("utf-8", "replace")


def _v99999(s: str) -> Decimal:
    return Decimal(s.strip() or "0") / Decimal(100000)


def _date8(s: str) -> date | None:
    """``YYYYMMDD``, or None where the field is blank or not a date."""
    s = s.strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def parse(text: str) -> list[IlRow]:
    """One record per taxing jurisdiction, plus a county record named ``<COUNTY> COUNTY``
    for each of the 102 counties, which is what unincorporated territory pays.

    Two kinds of record are dropped. An address-override row (flag ``Y`` at column 60,
    ~200 of them) taxes different addresses inside one municipality at different rates --
    Springfield and Alton among them -- so IDOR zeroes its summary rate group and
    publishes only a high/low range further along the record; there is no single rate to
    read, and the ZIP falls back to its county. Rows naming no county are the ~50 all-zero
    entries for OTHER states, carried for use-tax lookups; five of them (Kansas, Oregon,
    Virginia, Washington, Wyoming) share a name with a real Illinois municipality and
    would otherwise price it at 0%, i.e. a negative local rate.

    Raises ``ValueError`` if the parse yields implausibly few rows, or too few county
    rows -- either is a sign the file is an error page or its columns shifted."""
    out = []
    for line in text.splitlines():
        if len(line) < 90 or line[_OVERRIDE_FLAG] == "Y":
            continue
        county = line[35:60].strip().upper()
        if not county:
            continue
        out.append(IlRow(
            line[0:10].strip(), line[10:35].strip().upper(), county,
            _date8(line[_BEGIN]), _v99999(line[_GM_LOW]), _v99999(line[_DM_LOW]),
            _date8(line[_PRIOR_BEGIN]), _date8(line[_PRIOR_END]),
            _v99999(line[_PRIOR_GM_LOW]), _v99999(line[_PRIOR_DM_LOW]),
        ))
    if len(out) < MIN_DATA_ROWS:
        raise ValueError(
            f"Illinois rate file: only {len(out)} data rows parsed, expected at least "
            f"{MIN_DATA_ROWS} -- the file may be an error page or its columns shifted"
        )
    counties = sum(1 for r in out if r.name.endswith(" COUNTY"))
    if counties < MIN_COUNTY_ROWS:
        raise ValueError(
            f"Illinois rate file: only {counties} county rows parsed, expected at least "
            f"{MIN_COUNTY_ROWS} of the state's 102 -- the file may be reordered"
        )
    return out


def _fetch_grocery_text() -> str:
    return get_cached(GROCERY_URL, ttl_days=30).decode("utf-8", "replace")


def parse_grocery(text: str) -> list[GroceryRow]:
    """One record per jurisdiction that may levy the local grocery tax, plus a
    ``<COUNTY> COUNTY`` record for each of the 102 counties, which is what unincorporated
    territory pays.

    Rows naming no county are dropped for the reason the ordinance file's are: they are the 52
    all-zero entries for OTHER states, carried for use-tax lookups, and five of them share a
    name with a real Illinois municipality.

    Raises ``ValueError`` if the parse yields implausibly few rows, too few county rows, too
    few non-zero rates, or a rate above ``MAX_GROCERY_RATE`` -- each is a sign the file is an
    error page or that its columns shifted."""
    out = []
    for line in text.splitlines():
        if len(line) < 105:
            continue
        county = line[35:60].strip().upper()
        if not county:
            continue
        low = _v99999(line[_G_LOW])
        if low > MAX_GROCERY_RATE:
            raise ValueError(
                f"Illinois grocery file: {line[10:35].strip()} ({county}, "
                f"{line[0:10].strip()}) has a low grocery rate of {low}, above the "
                f"{MAX_GROCERY_RATE} ceiling -- the file's columns may have shifted"
            )
        out.append(GroceryRow(
            line[0:10].strip(), line[10:35].strip().upper(), county,
            _date8(line[_G_BEGIN]), low,
            _date8(line[_G_PRIOR_BEGIN]), _date8(line[_G_PRIOR_END]),
            _v99999(line[_G_PRIOR_LOW]),
        ))
    if len(out) < MIN_GROCERY_ROWS:
        raise ValueError(
            f"Illinois grocery file: only {len(out)} data rows parsed, expected at least "
            f"{MIN_GROCERY_ROWS} -- the file may be an error page or its columns shifted"
        )
    counties = sum(1 for r in out if r.name.endswith(" COUNTY"))
    if counties < MIN_GROCERY_COUNTY_ROWS:
        raise ValueError(
            f"Illinois grocery file: only {counties} county rows parsed, expected at least "
            f"{MIN_GROCERY_COUNTY_ROWS} of the state's 102 -- the file may be reordered"
        )
    nonzero = sum(1 for r in out if r.low > 0)
    if nonzero < MIN_NONZERO_GROCERY_ROWS:
        raise ValueError(
            f"Illinois grocery file: only {nonzero} rows carry a non-zero grocery rate, "
            f"expected at least {MIN_NONZERO_GROCERY_ROWS} -- the columns may have shifted "
            f"into the record's padding, which reads as 0%"
        )
    return out


def _local(row: IlRow, gm: Decimal) -> Decimal:
    """Every Illinois rate quoted in the file includes the 6.25% state share, so the
    jurisdiction's own local rate is what is left over. A negative remainder means the
    columns moved and the build must fail naming the row, not publish a negative rate."""
    local = gm - STATE_RATE
    if local < 0:
        raise ValueError(
            f"Illinois rate file: {row.name} ({row.county}, {row.location_id}) has a low "
            f"general-merchandise rate of {gm} below the {STATE_RATE} state rate"
        )
    return local


class IlAdapter:
    name = "il"
    states = ("IL",)
    bounds_states = ("IL",)

    def jurisdictions(self, census: Census, on: date) -> dict[JurisdictionKey, ZipRate]:
        """The same table `rows` prices ZIPs from, keyed by geography instead. Every record
        carries the jurisdiction's own food/drug rate, so a polygon quotes Illinois' reduced
        medicine rate rather than the general one, and the grocery rate rides along on the
        same key, so a Chicago grocery quote off a polygon is the 1.5% IDOR publishes for
        groceries rather than that 2.5% medicine rate.

        Every place is crossed with **every** county of the state, not only the counties
        `geoid_index` happened to observe for it. The index is reversed out of the ZCTA
        relationship files, so it names a place in a county only where some ZCTA in that
        county picked that place as its dominant one -- which is not where the municipality
        reaches, it is where a ZIP's centre of mass fell. Keying off those pairs alone left
        a straddler priced on one side only: Chicago in Cook but not in its DuPage sliver,
        Bartlett in one of Cook/DuPage/Kane, and 40 rows IDOR actually files unreachable.
        A pairing IDOR files no row for stays absent, and a pairing whose place does not
        physically reach that county is inert -- the overlay emits no piece to look it up
        with, and the county's remainder answers instead (spec §2.3)."""
        table = {(join_key(r.name), join_key(r.county)): r for r in parse(_fetch_text())}
        grocery = {(join_key(r.name), join_key(r.county)): r
                   for r in parse_grocery(_fetch_grocery_text())}
        counties, places = geoid_index(census, "17")
        out: dict[JurisdictionKey, ZipRate] = {}
        for county_key, (county_geoid, county_label) in counties.items():
            key = (join_key(f"{county_key} COUNTY"), county_key)
            row = table.get(key)
            if row is None:
                continue
            gm, dm, _covered = row.rates(on)
            g = grocery.get(key)
            out[county_geoid] = ZipRate(
                "", "IL", STATE_RATE, _local(row, gm), dm, f"{county_label}, IL",
                None if g is None else g.rate(on)[0])
        for place_geoid, (place_key, name) in wanted_places(places).items():
            for county_key, (county_geoid, _label) in counties.items():
                key = (place_key, county_key)
                row = table.get(key)
                if row is None:
                    continue      # unincorporated, taxed by address, or not in this county
                gm, dm, _covered = row.rates(on)
                g = grocery.get(key)
                out[(place_geoid, county_geoid)] = ZipRate(
                    "", "IL", STATE_RATE, _local(row, gm), dm, f"{name}, IL",
                    None if g is None else g.rate(on)[0])
        return out

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        # Keyed on (name, county), as Texas is: 140 municipality names are filed more than
        # once because the municipality straddles a county line and is taxed differently
        # on each side -- Chicago at 10.5% in Cook and 8.5% in its DuPage sliver, Aurora
        # in four counties. A name-only key would hand such a ZIP whichever row was read
        # last. `join_key` folds the spellings the two sources disagree on (Census
        # `ST. CLAIR` vs the file's `SAINT CLAIR COUNTY`, `LA SALLE` vs `LASALLE`).
        by_name_county = {(join_key(r.name), join_key(r.county)): r for r in parse(_fetch_text())}
        # The grocery tax is a separate levy on a separate file (spec §3.5, decision #90): the
        # ordinance file's Drug & Medical column stopped being the grocery rate when P.A.
        # 103-0781 repealed the 1% state grocery tax on 2026-01-01. Keyed identically, so the
        # rate is read for whichever jurisdiction the general rate resolved to.
        grocery = {(join_key(r.name), join_key(r.county)): r
                   for r in parse_grocery(_fetch_grocery_text())}
        matched = fell_back = no_row = uncovered = no_grocery = 0
        for zip5 in census.zips_in_state("17"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            county_key = join_key(county)
            place = census.place_name(zip5)
            place_key = (join_key(place), county_key) if place else None
            row = by_name_county.get(place_key) if place_key else None
            if row is not None:
                matched += 1
                key = place_key
                # The Census's own casing is the label (C1): `DeKalb`, `O'Fallon`.
                label = f"{census.place_display(zip5) or display_name(place)}, IL"
            else:
                # Unincorporated territory, a municipality IDOR taxes by address, or a
                # Census place under a name IDOR does not file: the ZIP pays its county's
                # rate. A county filing no readable row leaves nothing to publish.
                key = (join_key(f"{county} COUNTY"), county_key)
                row = by_name_county.get(key)
                if row is None:
                    no_row += 1
                    continue
                fell_back += 1
                # The county label is the Census's own name, casing and entity word
                # included (C1/F5); `display_name` supplies both only as the fallback.
                name = census.county_label(zip5) or f"{display_name(county)} County"
                label = f"{name}, IL"
            # A record carries the current period and the one before it; `on` decides
            # which applies, so a build dated back before a rate change publishes the
            # rate that was actually in force then.
            gm, dm, covered = row.rates(on)
            # `key` and not the place: the ZIP is priced as one jurisdiction, so its grocery
            # rate is the one belonging to whichever jurisdiction its general rate came from.
            # Reading the municipality's own grocery row for a ZIP that fell back to its
            # county would move 60 of the state's ~1 400 ZIPs one point, and would make the
            # GPS overlay -- which draws no polygon for a municipality the ordinance file
            # zeroes -- answer differently from the ZIP for the same address (decision #91).
            g_row = grocery.get(key)
            grocery_rate = None
            g_covered = True
            if g_row is None:
                no_grocery += 1
            else:
                grocery_rate, g_covered = g_row.rate(on)
            # One counter across both files, so the note's count never exceeds the ZIPs it
            # claims to be about: a ZIP whose two records both miss `on` is one ZIP that took
            # a current-period rate, not two.
            if not (covered and g_covered):
                uncovered += 1
            # Illinois taxes qualifying drugs and medical appliances at a separate reduced
            # rate, so `food_drug_rate` is always published, never collapsed to None the way
            # an SST state's equal-to-general rate is. Since 2026-01-01 that is the medicine
            # rate only; groceries read `grocery_rate`.
            yield ZipRate(zip5, "IL", STATE_RATE, _local(row, gm), dm, label, grocery_rate)
        note = ""
        if uncovered:
            zips = "ZIP" if uncovered == 1 else "ZIPs"
            note = (
                f" ({uncovered} {zips} took the current period's rate because neither "
                f"period the record carries covers {on})"
            )
        # Appended only when it happened, exactly as the note is: `test_il.py` pins this
        # line's tail on a clean build, and "0 with no grocery row" is noise either way.
        missing = f", {no_grocery} with no grocery row" if no_grocery else ""
        print(
            f"[il] {matched} ZIPs matched a municipality, {fell_back} fell back to their "
            f"county's rate, {no_row} dropped with no municipality or county row"
            f"{missing}{note}"
        )


REGISTRY.append(IlAdapter())
