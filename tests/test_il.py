from datetime import date
from decimal import Decimal as D

import pytest

from pipeline.census import Census
from pipeline.sources import il


def rec(loc, name, county, flag, eff, gm_hi, dm_hi, gm_lo, dm_lo, rflag="N"):
    return (loc.ljust(10) + name.ljust(25) + county.ljust(25) + flag + eff + gm_hi + dm_hi
            + gm_lo + dm_lo + rflag).ljust(200)


FIXTURE = "\n".join([
    rec("016-1234-1", "CHICAGO", "COOK", "N", "20260801", "10250", "01250", "10250", "01250"),
    rec("016-0000-0", "COOK COUNTY", "COOK", "N", "20260801", "08000", "01250", "07000", "01250"),
    rec("099-5555-5", "OVERRIDDEN TOWN", "DUPAGE", "Y", "20260801", "00000", "00000", "00000",
        "00000"),
    rec("022-2222-2", "SPRINGFIELD", "SANGAMON", "N", "20260801", "09750", "01750", "09750",
        "01750"),
])

# Verbatim records from the live IDOR file (2026-09-08 fetch), kept whole so a shift in
# the fixed-width columns fails here rather than silently repricing Illinois. The file is
# 211 characters wide: the header fields the brief documents, then THREE 21-character rate
# groups (high GM, high food/drug, low GM, low food/drug, "rate varies" flag) for the
# current period, then a prior period's date range and its own three groups. Only the
# first group of the current period is a jurisdiction's own general-merchandise and
# food/drug rate; the adapter reads its low pair, at [79:84] and [84:89].
LIVE_CHICAGO_COOK = (
    '016-0001-1CHICAGO                  COOK                     N202608011050002500105000250'
    '0N06250010000625001000N08750075000000000000Y201601012026073110250022501025002250N0625001'
    '0000625001000N08500072500000000000Y'
)
LIVE_CHICAGO_DUPAGE = (
    '022-0068-6CHICAGO                  DUPAGE                   N202608010850002000085000200'
    '0N06250010000625001000N08500072500000000000Y200804012026073108250017500825001750N0625001'
    '0000625001000N08250070000000000000Y'
)
LIVE_COOK_COUNTY = (
    '016-5000-1COOK COUNTY              COOK                     N202608010925002500092500250'
    '0N06250010000625001000N08750075000000000000Y201601012026073109000022500900002250N0625001'
    '0000625001000N08500072500000000000Y'
)
LIVE_SPRINGFIELD = (
    '084-0001-6SPRINGFIELD              SANGAMON                 Y202607010000000000000000000'
    '0 00000000000000000000 06250062500000000000N201907012026063000000000000000000000 0000000'
    '0000000000000 06250062500000000000N'
)
LIVE_SANGAMON_COUNTY = (
    '084-5000-8SANGAMON COUNTY          SANGAMON                 N202607010775001000077500100'
    '0N06250010000625001000N06250062500000000000N201907012026063007250010000725001000N0625001'
    '0000625001000N06250062500000000000N'
)
LIVE_SAINT_CLAIR_COUNTY = (
    '082-5000-9SAINT CLAIR COUNTY       ST. CLAIR                N200901010735001750066000100'
    '0Y06250010000625001000Y06500062500025002000Y200107012008123107100017500635001000Y0625001'
    '0000625001000Y06500062500025002000Y'
)
LIVE_WASHINGTON_TAZEWELL = (
    '090-0015-1WASHINGTON               TAZEWELL                 N202207010900001000090000100'
    '0N06250010000625001000N06250062500000000000N201807012022063008500010000850001000N0625001'
    '0000625001000N06250062500000000000N'
)
LIVE_STATE_OF_WASHINGTON = (
    '244-0099-8WASHINGTON                                        N190001010000000000000000000'
    '0N00000000000000000000N00000000000000000000Y190001019999123100000000000000000000N0000000'
    '0000000000000N00000000000000000000Y'
)


@pytest.fixture(autouse=True)
def _low_min_rows(monkeypatch):
    """The parse guards default to whole-file scale; every test here builds a handful of
    records inline, so lower them for all of them. The tests that exercise the guards
    themselves raise them back up within their own bodies."""
    monkeypatch.setattr(il, "MIN_DATA_ROWS", 1)
    monkeypatch.setattr(il, "MIN_COUNTY_ROWS", 0)


def test_parse_fixed_width_and_override_flag():
    rows = il.parse(FIXTURE)
    assert [r.name for r in rows] == ["CHICAGO", "COOK COUNTY", "SPRINGFIELD"]
    chi = rows[0]
    assert (chi.county, chi.gm_low, chi.dm_low) == ("COOK", D("0.1025"), D("0.0125"))
    assert rows[1].gm_low == D("0.07")  # low rate, not the 0.08 high


def test_parse_reads_the_live_columns():
    """Regression on real records: the brief's slices must keep landing on the current
    period's low general-merchandise and food/drug rates, not on the high pair, the two
    trailing rate groups, or the prior period."""
    rows = il.parse("\n".join([LIVE_CHICAGO_COOK, LIVE_COOK_COUNTY, LIVE_SANGAMON_COUNTY]))
    assert [(r.location_id, r.name, r.county, r.gm_low, r.dm_low) for r in rows] == [
        ("016-0001-1", "CHICAGO", "COOK", D("0.105"), D("0.025")),
        ("016-5000-1", "COOK COUNTY", "COOK", D("0.0925"), D("0.025")),
        ("084-5000-8", "SANGAMON COUNTY", "SANGAMON", D("0.0775"), D("0.01")),
    ]


def test_parse_skips_live_address_override_rows():
    """Springfield is address-overridden in the live file: IDOR zeroes its summary rate
    group and publishes only the high/low range further along the record, so the row
    carries no usable rate and must drop out."""
    assert il.parse("\n".join([LIVE_SPRINGFIELD, LIVE_SANGAMON_COUNTY])) == il.parse(
        LIVE_SANGAMON_COUNTY)


def test_parse_skips_the_out_of_state_use_tax_rows():
    """The file ends with ~50 all-zero rows naming other states (for use-tax lookups).
    They name no county, and several collide with real Illinois municipalities -- Kansas,
    Oregon, Virginia, Washington and Wyoming are all towns here. Left in, the state row
    would price Washington (Tazewell Co) at 0%, i.e. a negative local rate."""
    rows = il.parse("\n".join([LIVE_STATE_OF_WASHINGTON, LIVE_WASHINGTON_TAZEWELL]))
    assert [(r.name, r.county, r.gm_low) for r in rows] == [
        ("WASHINGTON", "TAZEWELL", D("0.09"))]


def test_parse_raises_when_too_few_rows(monkeypatch):
    """An error page, or a file whose records silently dropped out, must fail the build
    rather than quietly publish 6.25% across Illinois."""
    monkeypatch.setattr(il, "MIN_DATA_ROWS", 1200)
    with pytest.raises(ValueError, match="only 3 data rows"):
        il.parse(FIXTURE)


def test_parse_raises_when_too_few_rows_name_a_county(monkeypatch):
    """A shifted file could still yield MIN_DATA_ROWS "rows" while no record reads as the
    `<COUNTY> COUNTY` row the fallback depends on; that must fail too."""
    monkeypatch.setattr(il, "MIN_COUNTY_ROWS", 1)
    with pytest.raises(ValueError, match="only 0 county rows"):
        il.parse(rec("016-1234-1", "CHICAGO", "COOK", "N", "20260801", "10250", "01250",
                     "10250", "01250"))


def test_rows_match_place_then_county(monkeypatch):
    monkeypatch.setattr(il, "_fetch_text", lambda: FIXTURE)
    c = Census(
        centroids={"60601": (41.88, -87.62), "60004": (42.1, -87.97), "62701": (39.8, -89.65)},
        county={"60601": ("17031", "Cook County"), "60004": ("17031", "Cook County"),
                "62701": ("17167", "Sangamon County")},
        place={"60601": ("1714000", "Chicago city"), "62701": ("1772000", "Springfield city")},
    )
    rows = {r.zip: r for r in il.IlAdapter().rows(c, date(2026, 9, 8))}
    assert (rows["60601"].local_rate == D("0.04")
            and rows["60601"].food_drug_rate == D("0.0125")
            and rows["60601"].label == "Chicago, IL")
    assert rows["60004"].local_rate == D("0.0075") and rows["60004"].label == "Cook County, IL"
    assert rows["62701"].local_rate == D("0.035")
    assert all(r.state == "IL" and r.state_rate == D("0.0625") for r in rows.values())


def test_place_join_disambiguates_by_the_zips_own_county(monkeypatch):
    """Chicago is filed twice, at 10.5% in Cook and 8.5% in its DuPage sliver (O'Hare),
    and 140 municipality names are filed more than once because they straddle a county
    line. Keying the join on the name alone would hand every such ZIP whichever row
    happened to be read last, so the key carries the ZIP's own Census county too."""
    monkeypatch.setattr(il, "_fetch_text", lambda: "\n".join(
        [LIVE_CHICAGO_COOK, LIVE_CHICAGO_DUPAGE]))
    c = Census(
        centroids={"60601": (41.88, -87.62), "60666": (41.98, -87.9)},
        county={"60601": ("17031", "Cook County"), "60666": ("17043", "DuPage County")},
        place={"60601": ("1714000", "Chicago city"), "60666": ("1714000", "Chicago city")},
    )
    rows = {r.zip: r for r in il.IlAdapter().rows(c, date(2026, 9, 8))}
    assert rows["60601"].general_rate == D("0.105") and rows["60601"].food_drug_rate == D("0.025")
    assert rows["60666"].general_rate == D("0.085") and rows["60666"].food_drug_rate == D("0.02")


def test_county_fallback_joins_saint_clair_through_the_shared_join_key(monkeypatch):
    """The Census county name normalizes to `ST. CLAIR`, but IDOR files the county row as
    `SAINT CLAIR COUNTY`. `census.join_key` folds both to the same key; without it, every
    unincorporated St. Clair County ZIP would drop out of the dataset."""
    monkeypatch.setattr(il, "_fetch_text", lambda: LIVE_SAINT_CLAIR_COUNTY)
    c = Census(
        centroids={"62208": (38.6, -90.0)},
        county={"62208": ("17163", "St. Clair County")},
        place={},
    )
    rows = list(il.IlAdapter().rows(c, date(2026, 9, 8)))
    assert [(r.zip, r.local_rate, r.food_drug_rate, r.label) for r in rows] == [
        ("62208", D("0.0035"), D("0.01"), "St. Clair County, IL")]


def test_rows_skip_a_zip_whose_county_files_no_row(monkeypatch):
    """No place row and no `<COUNTY> COUNTY` row means the adapter has no rate to publish
    for the ZIP; it is dropped and counted rather than guessed at."""
    monkeypatch.setattr(il, "_fetch_text", lambda: FIXTURE)
    c = Census(
        centroids={"60601": (41.88, -87.62), "61801": (40.1, -88.2)},
        county={"60601": ("17031", "Cook County"), "61801": ("17019", "Champaign County")},
        place={"60601": ("1714000", "Chicago city")},
    )
    assert [r.zip for r in il.IlAdapter().rows(c, date(2026, 9, 8))] == ["60601"]


def test_rows_skip_zips_outside_illinois_or_without_a_centroid(monkeypatch):
    monkeypatch.setattr(il, "_fetch_text", lambda: FIXTURE)
    c = Census(
        centroids={"60601": (41.88, -87.62)},
        county={"60601": ("17031", "Cook County"), "60004": ("17031", "Cook County"),
                "46201": ("18097", "Marion County")},
        place={},
    )
    assert [r.zip for r in il.IlAdapter().rows(c, date(2026, 9, 8))] == ["60601"]


def test_rows_raise_when_a_local_rate_would_be_negative(monkeypatch):
    """Every Illinois rate includes the 6.25% state share, so a general-merchandise rate
    under it means the columns moved; fail loudly and name the row."""
    monkeypatch.setattr(il, "_fetch_text", lambda: rec(
        "016-1234-1", "COOK COUNTY", "COOK", "N", "20260801", "05000", "01000", "05000", "01000"))
    c = Census(
        centroids={"60004": (42.1, -87.97)},
        county={"60004": ("17031", "Cook County")},
        place={},
    )
    with pytest.raises(ValueError, match="COOK COUNTY"):
        list(il.IlAdapter().rows(c, date(2026, 9, 8)))


def test_food_drug_rate_is_always_published(monkeypatch):
    """Illinois taxes qualifying food and drugs at a separate reduced rate, so the field
    is always set -- including where it happens to equal nothing else in the row."""
    monkeypatch.setattr(il, "_fetch_text", lambda: LIVE_WASHINGTON_TAZEWELL)
    c = Census(
        centroids={"61571": (40.7, -89.4)},
        county={"61571": ("17179", "Tazewell County")},
        place={"61571": ("1779241", "Washington city")},
    )
    rows = list(il.IlAdapter().rows(c, date(2026, 9, 8)))
    assert [(r.local_rate, r.food_drug_rate, r.label) for r in rows] == [
        (D("0.0275"), D("0.01"), "Washington, IL")]


def test_adapter_is_registered():
    assert any(a.name == "il" and a.states == ("IL",) for a in il.REGISTRY)
