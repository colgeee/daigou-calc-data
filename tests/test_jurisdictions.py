"""The rate tables answered by TIGER GEOID, so a polygon and a ZIP row share a profile."""
from datetime import date
from decimal import Decimal as D

import pytest

from pipeline.census import Census
from pipeline.model import profile_id
from pipeline.sources import geoid_index, il, ny, sst, yaml_states

ON = date(2026, 9, 9)


def rec(loc, name, county, flag, eff, gm_hi, dm_hi, gm_lo, dm_lo, rflag="N"):
    """A copy of `tests/test_il.py`'s helper: one 200-character IDOR record."""
    return (loc.ljust(10) + name.ljust(25) + county.ljust(25) + flag + eff + gm_hi + dm_hi
            + gm_lo + dm_lo + rflag).ljust(200)


IL_FIXTURE = "\n".join([
    rec("016-0001-1", "CHICAGO", "COOK", "N", "20260801", "10250", "01000", "10250", "01000"),
    rec("016-5000-1", "COOK COUNTY", "COOK", "N", "20260801", "10000", "01000", "10000", "01000"),
    rec("022-0100-1", "ADDISON", "DUPAGE", "N", "20260801", "08000", "01000", "08000", "01000"),
])

# Chicago is filed in both Cook and DuPage, at each county's own combined rate; Addison is
# filed in DuPage only. No ZCTA in the census fixture below names Chicago as its dominant
# place inside DuPage -- which is the whole point: the relationship files say where a ZIP's
# centre of mass fell, not which counties a municipality reaches.
IL_STRADDLE_FIXTURE = "\n".join([
    rec("016-0001-1", "CHICAGO", "COOK", "N", "20260801", "10250", "01000", "10250", "01000"),
    rec("016-0002-1", "CHICAGO", "DUPAGE", "N", "20260801", "08500", "01000", "08500", "01000"),
    rec("016-5000-1", "COOK COUNTY", "COOK", "N", "20260801", "10000", "01000", "10000", "01000"),
    rec("022-5000-1", "DUPAGE COUNTY", "DUPAGE", "N", "20260801",
        "07250", "01000", "07250", "01000"),
    rec("022-0100-1", "ADDISON", "DUPAGE", "N", "20260801", "08000", "01000", "08000", "01000"),
])


def g_rec(loc, name, county, eff, hi, lo):
    """One 106-character record of IDOR's separate grocery file (guide IDR-1028): id, name,
    county, the current period's start date and that period's single high/low rate group,
    then a long-closed prior period at 0%. Both over-ride flags are hard-coded `N`: the
    Metro-East records that set the flag and part high from low are `tests/test_il.py`'s
    business, and nothing here needs more than a rate to ride along on a profile id."""
    return (loc.ljust(10) + name.ljust(25) + county.ljust(25) + eff + hi + lo + "N"
            + "19900101" + "20251231" + "00000" + "00000" + "N")


# The grocery table for both fixtures above. Chicago's two sides carry different grocery rates
# for the reason they carry different general ones -- IDOR files the straddler once per county
# -- so a profile id proves the polygon read the grocery row for its own county, not whichever
# was read last.
IL_GROCERY = "\n".join([
    g_rec("016-0001-1", "CHICAGO", "COOK", "20260801", "01500", "01500"),
    g_rec("016-5000-1", "COOK COUNTY", "COOK", "20260801", "01500", "01500"),
    g_rec("016-0002-1", "CHICAGO", "DUPAGE", "20260801", "01000", "01000"),
    g_rec("022-5000-1", "DUPAGE COUNTY", "DUPAGE", "20260801", "01000", "01000"),
    g_rec("022-0100-1", "ADDISON", "DUPAGE", "20260801", "01000", "01000"),
])


@pytest.fixture
def _il_grocery(monkeypatch):
    """`IlAdapter` reads two IDOR files, so an Illinois test that patches only `_fetch_text`
    pulls the live grocery file over the network and prices a synthetic rate table against
    real rates. Requested by name rather than autouse: only the Illinois tests below need it,
    and the grocery floors come down with it because these fixtures are five records, not
    1 200."""
    monkeypatch.setattr(il, "_fetch_grocery_text", lambda: IL_GROCERY)
    monkeypatch.setattr(il, "MIN_GROCERY_ROWS", 1)
    monkeypatch.setattr(il, "MIN_GROCERY_COUNTY_ROWS", 0)
    monkeypatch.setattr(il, "MIN_NONZERO_GROCERY_ROWS", 0)


NY_FIXTURE_LINES = [
    "New York City 8\u215e 8081",
    "Westchester - except 8\u215c 6011",
    "  Yonkers (city) 8\u215e 6511",
    "  White Plains (city) 8\u215c 6501",
]

# The city of Oneida is filed under two county blocks, at a different rate under each.
NY_STRADDLE_LINES = [
    "New York City 8\u215e 8081",
    "Madison - except 8\u00bc 6301",
    "  Oneida (city) 8\u00be 6302",
    "Oneida - except 8\u00be 6401",
    "  Oneida (city) 8\u215c 6402",
]

NV_RATES_INDEX = (
    b"<html><body><pre>\n"
    b'<A HREF="/ratesandboundry/Rates/NVR2026Q3JUL01.csv">NVR2026Q3JUL01.csv</A>\n'
    b"</pre></body></html>\n"
)
NV_BOUNDARY_INDEX = (
    b"<html><body><pre>\n"
    b'<A HREF="/ratesandboundry/Boundary/NVB2026Q3JUL01.csv">NVB2026Q3JUL01.csv</A>\n'
    b"</pre></body></html>\n"
)
NV_RATE_CSV = (
    b"32,45,32,0.00000,0,0.00000,0,19830301,99991231\n"
    b"32,00,003,0.08375,0,0.08375,0,20200401,99991231\n"
)


def _z_row(zip5: str) -> str:
    f = [""] * 90
    f[0] = "Z"
    f[1] = "20200401"
    f[2] = "99991231"
    f[17] = zip5
    f[19] = zip5
    f[24] = "003"
    return ",".join(f)


NV_BOUNDARY_CSV = (_z_row("89101") + "\n" + _z_row("89019") + "\n").encode()

NV_RATE_URL = "http://52.15.48.162/ratesandboundry/Rates/NVR2026Q3JUL01.csv"
NV_BOUNDARY_URL = "http://52.15.48.162/ratesandboundry/Boundary/NVB2026Q3JUL01.csv"


def fake_nv_get_cached(url, *, ttl_days: float = 1.0, min_bytes: int = 512) -> bytes:
    if url == f"{sst.MIRROR}/Rates/":
        return NV_RATES_INDEX
    if url == f"{sst.MIRROR}/Boundary/":
        return NV_BOUNDARY_INDEX
    if url == NV_RATE_URL:
        return NV_RATE_CSV
    if url == NV_BOUNDARY_URL:
        return NV_BOUNDARY_CSV
    raise AssertionError(f"unexpected url: {url}")


def il_census() -> Census:
    return Census(
        centroids={"60601": (41.88, -87.62), "60101": (41.93, -87.98)},
        county={"60601": ("17031", "Cook County"), "60101": ("17043", "DuPage County")},
        place={"60601": ("1714000", "Chicago city"), "60101": ("1700685", "Addison village")},
    )


def test_geoid_index_reverses_the_relationship_files_for_one_state():
    counties, places = geoid_index(il_census(), "17")
    assert counties["COOK"] == ("17031", "Cook County")
    assert counties["DUPAGE"] == ("17043", "DuPage County")
    assert places[("CHICAGO", "COOK")] == ("1714000", "17031", "Chicago")
    assert places[("ADDISON", "DUPAGE")] == ("1700685", "17043", "Addison")


def test_illinois_jurisdictions_match_the_zip_rows_profile_id(monkeypatch, _il_grocery):
    """The whole point of `jurisdictions`: a Chicago polygon and a Chicago ZIP row mint the
    same profile id, so the bounds file and the rates file agree on the label and all three
    rates, food/drug and grocery included (spec §2.3)."""
    monkeypatch.setattr(il, "_fetch_text", lambda: IL_FIXTURE)
    monkeypatch.setattr(il, "MIN_DATA_ROWS", 1)
    monkeypatch.setattr(il, "MIN_COUNTY_ROWS", 1)
    c = il_census()
    zip_rows = {r.zip: r for r in il.IlAdapter().rows(c, ON)}
    juris = il.IlAdapter().jurisdictions(c, ON)
    chicago = juris[("1714000", "17031")]
    assert profile_id(chicago) == profile_id(zip_rows["60601"])
    assert profile_id(chicago) == "IL-0.0625-0.04-CHICAGO, IL-FD0.01-GR0.015"
    assert chicago.food_drug_rate == D("0.01")
    # The two are separate taxes on the same polygon: 1% on medicine, 1.5% on groceries.
    assert chicago.grocery_rate == D("0.015")
    cook = juris["17031"]
    assert profile_id(cook) == "IL-0.0625-0.0375-COOK COUNTY, IL-FD0.01-GR0.015"
    assert il.IlAdapter().bounds_states == ("IL",)


def test_a_straddling_illinois_municipality_is_priced_in_every_county_idor_files_it_in(
        monkeypatch, _il_grocery):
    """`geoid_index` learns a (place, county) pair only where a ZCTA in that county named
    that place as its dominant one, which is where ZIP centres of mass fell, not where the
    municipality reaches. Keying off those pairs alone priced Chicago in Cook and left its
    DuPage sliver on DuPage's county rate -- the spec's own example of what the overlay is
    for. Every place is now crossed with every county and IDOR's table decides."""
    monkeypatch.setattr(il, "_fetch_text", lambda: IL_STRADDLE_FIXTURE)
    monkeypatch.setattr(il, "MIN_DATA_ROWS", 1)
    monkeypatch.setattr(il, "MIN_COUNTY_ROWS", 1)
    j = il.IlAdapter().jurisdictions(il_census(), ON)
    # Chicago on both sides, each at its own county's combined rate, not Cook's twice -- and
    # at its own county's grocery rate, which the two ids differ on as well.
    assert profile_id(j[("1714000", "17031")]) == "IL-0.0625-0.04-CHICAGO, IL-FD0.01-GR0.015"
    assert profile_id(j[("1714000", "17043")]) == "IL-0.0625-0.0225-CHICAGO, IL-FD0.01-GR0.01"
    assert j[("1714000", "17043")].label == "Chicago, IL"


def test_an_illinois_place_idor_files_in_one_county_only_gets_exactly_one_record(
        monkeypatch, _il_grocery):
    """The cross is inert where the state files no row: Addison is a DuPage municipality and
    IDOR files it there and nowhere else, so it gets one record and Cook's remainder is
    untouched. A pairing with no row emits no rated piece and the county answers."""
    monkeypatch.setattr(il, "_fetch_text", lambda: IL_STRADDLE_FIXTURE)
    monkeypatch.setattr(il, "MIN_DATA_ROWS", 1)
    monkeypatch.setattr(il, "MIN_COUNTY_ROWS", 1)
    j = il.IlAdapter().jurisdictions(il_census(), ON)
    assert [k for k in j if isinstance(k, tuple) and k[0] == "1700685"] \
        == [("1700685", "17043")]
    assert profile_id(j[("1700685", "17043")]) == "IL-0.0625-0.0175-ADDISON, IL-FD0.01-GR0.01"


def test_a_straddling_new_york_city_is_priced_in_every_county_pub_718_files_it_in(monkeypatch):
    """The same shape in New York: the city of Oneida is filed under two county blocks at a
    different rate under each, and only one of them has a ZCTA that names it."""
    monkeypatch.setattr(ny, "_fetch_lines", lambda: NY_STRADDLE_LINES)
    monkeypatch.setattr(ny, "MIN_COUNTY_ROWS", 0)
    c = Census(
        centroids={"13421": (43.09, -75.65), "13402": (42.93, -75.59)},
        county={"13421": ("36065", "Oneida County"), "13402": ("36053", "Madison County")},
        place={"13421": ("3654881", "Oneida city")},
    )
    j = ny.NyAdapter().jurisdictions(c, ON)
    assert profile_id(j[("3654881", "36065")]) == "NY-0.04-0.04375-ONEIDA, NY"
    assert profile_id(j[("3654881", "36053")]) == "NY-0.04-0.0475-ONEIDA, NY"


def test_new_york_cities_are_keyed_inside_their_own_county_and_the_boroughs_share_a_profile(
        monkeypatch):
    monkeypatch.setattr(ny, "_fetch_lines", lambda: NY_FIXTURE_LINES)
    monkeypatch.setattr(ny, "MIN_COUNTY_ROWS", 0)
    c = Census(
        centroids={z: (40.7, -74.0) for z in
                   ("10001", "11201", "10451", "11101", "10301", "10701", "10601")},
        county={"10001": ("36061", "New York County"), "11201": ("36047", "Kings County"),
                "10451": ("36005", "Bronx County"), "11101": ("36081", "Queens County"),
                "10301": ("36085", "Richmond County"),
                "10701": ("36119", "Westchester County"),
                "10601": ("36119", "Westchester County")},
        place={"10701": ("3684000", "Yonkers city"),
               "10601": ("3681677", "White Plains city")},
    )
    j = ny.NyAdapter().jurisdictions(c, ON)
    boroughs = {j[g] for g in ("36061", "36047", "36005", "36081", "36085")}
    assert len({profile_id(r) for r in boroughs}) == 1
    assert next(iter(boroughs)).label == "New York City, NY"
    assert profile_id(j["36119"]) == "NY-0.04-0.04375-WESTCHESTER COUNTY, NY"
    assert profile_id(j[("3684000", "36119")]) == "NY-0.04-0.04875-YONKERS, NY"


def test_nevada_counties_split_at_the_state_rate_floor(monkeypatch):
    """Nevada files its state-level (45) row at 0 and carries the whole 6.85% statewide
    minimum in county rows; `STATE_RATE_FLOOR` re-splits it, so a Clark County polygon
    publishes 0.0685 + 0.01525 exactly as the ZIP row does. A county polygon is labelled as
    a county, so the id it shares is a ZIP's **county-fallback** id -- 89101's own ZIP row
    reads `Las Vegas, NV` because the Census names it a place, and that is the right
    difference: the polygon really is the county."""
    monkeypatch.setattr(sst, "get_cached", fake_nv_get_cached)
    c = Census(centroids={"89101": (36.17, -115.14), "89019": (35.8, -115.3)},
               county={"89101": ("32003", "Clark County"), "89019": ("32003", "Clark County")},
               place={"89101": ("3240000", "Las Vegas city")})
    zip_rows = {r.zip: r for r in sst.SstAdapter().rows(c, ON)}
    clark = sst.SstAdapter().jurisdictions(c, ON)["32003"]
    assert (clark.state_rate, clark.local_rate) == (D("0.0685"), D("0.01525"))
    assert clark.food_drug_rate is None
    assert profile_id(clark) == profile_id(zip_rows["89019"])
    assert profile_id(clark) == "NV-0.0685-0.01525-CLARK COUNTY, NV"
    assert sst.SstAdapter().bounds_states == ("NV",)


def test_a_nevada_county_with_no_rate_row_fails_naming_that_cause(monkeypatch):
    """A bounds state's county polygon covers the whole county, so there is no ZIP path
    underneath to answer for an unpriced one. Without its own check the state share alone
    composes below `STATE_RATE_FLOOR` and the *state-rate-floor* error fires, which names
    the wrong cause entirely -- the floor is right, the row is missing."""
    def fake(url, *, ttl_days: float = 1.0, min_bytes: int = 512) -> bytes:
        if url == NV_RATE_URL:
            return b"32,45,32,0.00000,0,0.00000,0,19830301,99991231\n"
        return fake_nv_get_cached(url, ttl_days=ttl_days, min_bytes=min_bytes)

    monkeypatch.setattr(sst, "get_cached", fake)
    c = Census(centroids={"89101": (36.17, -115.14)},
               county={"89101": ("32003", "Clark County")})
    with pytest.raises(ValueError, match=r"32003 \(Clark County\) has no current county"):
        sst.SstAdapter().jurisdictions(c, ON)


def test_hawaii_counties_carry_the_pass_on_split_and_share_the_county_zip_row_s_profile():
    c = Census(
        centroids={"96813": (21.31, -157.85), "96742": (21.19, -156.98)},
        county={"96813": ("15003", "Honolulu County"), "96742": ("15005", "Kalawao County")},
        place={"96813": ("1571550", "Honolulu CDP")},
    )
    a = yaml_states.YamlStatesAdapter()
    zip_rows = {r.zip: r for r in a.rows(c, ON) if r.state == "HI"}
    j = a.jurisdictions(c, ON)
    assert (j["15003"].state_rate, j["15003"].local_rate) == (D("0.041885"), D("0.005235"))
    assert profile_id(j["15003"]) == "HI-0.041885-0.005235-HONOLULU COUNTY, HI"
    # 96742 names no place, so its ZIP row already reads as its county: same id.
    assert profile_id(j["15005"]) == profile_id(zip_rows["96742"])
    assert (j["15005"].state_rate, j["15005"].local_rate) == (D("0.041667"), D("0"))
    assert a.bounds_states == ("HI",)
    assert set(j) == {"15003", "15005"}          # HI only; the other 22 YAML states are ZIP-only
