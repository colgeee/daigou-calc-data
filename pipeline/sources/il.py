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
from pipeline.sources import REGISTRY, JurisdictionKey, geoid_index

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
        carries the jurisdiction's own food/drug rate, so a Chicago grocery quote off a
        polygon is the 1% Illinois publishes rather than the general rate."""
        table = {(join_key(r.name), join_key(r.county)): r for r in parse(_fetch_text())}
        counties, places = geoid_index(census, "17")
        out: dict[JurisdictionKey, ZipRate] = {}
        for county_key, (county_geoid, county_label) in counties.items():
            row = table.get((join_key(f"{county_key} COUNTY"), county_key))
            if row is None:
                continue
            gm, dm, _covered = row.rates(on)
            out[county_geoid] = ZipRate("", "IL", STATE_RATE, _local(row, gm), dm,
                                        f"{county_label}, IL")
        for (place_key, county_key), (place_geoid, county_geoid, name) in places.items():
            row = table.get((place_key, county_key))
            if row is None:
                continue          # unincorporated, or a jurisdiction IDOR taxes by address
            gm, dm, _covered = row.rates(on)
            out[(place_geoid, county_geoid)] = ZipRate("", "IL", STATE_RATE, _local(row, gm),
                                                       dm, f"{name}, IL")
        return out

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        # Keyed on (name, county), as Texas is: 140 municipality names are filed more than
        # once because the municipality straddles a county line and is taxed differently
        # on each side -- Chicago at 10.5% in Cook and 8.5% in its DuPage sliver, Aurora
        # in four counties. A name-only key would hand such a ZIP whichever row was read
        # last. `join_key` folds the spellings the two sources disagree on (Census
        # `ST. CLAIR` vs the file's `SAINT CLAIR COUNTY`, `LA SALLE` vs `LASALLE`).
        by_name_county = {(join_key(r.name), join_key(r.county)): r for r in parse(_fetch_text())}
        matched = fell_back = no_row = uncovered = 0
        for zip5 in census.zips_in_state("17"):
            if zip5 not in census.centroids:
                continue
            county = census.county_name(zip5) or ""
            county_key = join_key(county)
            place = census.place_name(zip5)
            row = by_name_county.get((join_key(place), county_key)) if place else None
            if row is not None:
                matched += 1
                # The Census's own casing is the label (C1): `DeKalb`, `O'Fallon`.
                label = f"{census.place_display(zip5) or display_name(place)}, IL"
            else:
                # Unincorporated territory, a municipality IDOR taxes by address, or a
                # Census place under a name IDOR does not file: the ZIP pays its county's
                # rate. A county filing no readable row leaves nothing to publish.
                row = by_name_county.get((join_key(f"{county} COUNTY"), county_key))
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
            if not covered:
                uncovered += 1
            # Illinois taxes qualifying food, drugs and medical appliances at a separate
            # reduced rate, so `food_drug_rate` is always published, never collapsed to
            # None the way an SST state's equal-to-general rate is.
            yield ZipRate(zip5, "IL", STATE_RATE, _local(row, gm), dm, label)
        note = ""
        if uncovered:
            zips = "ZIP" if uncovered == 1 else "ZIPs"
            note = (
                f" ({uncovered} {zips} took the current period's rate because neither "
                f"period the record carries covers {on})"
            )
        print(
            f"[il] {matched} ZIPs matched a municipality, {fell_back} fell back to their "
            f"county's rate, {no_row} dropped with no municipality or county row{note}"
        )


REGISTRY.append(IlAdapter())
