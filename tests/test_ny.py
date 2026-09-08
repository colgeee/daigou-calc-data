from datetime import date
from decimal import Decimal as D

import pytest

from pipeline.census import Census
from pipeline.sources import ny

LINES = [
    "Publication 718", "Effective March 1, 2025",
    " New York State only 4 0021",
    " Albany 8 0181",
    " Allegany 8½ 0221",
    " Cattaraugus – except 8 0481",
    "  Olean (city) 8 0441",
    "   Salamanca (city) 8 0431",
    " New York City 8⅞ 8081",
    " *Westchester – except 8⅜ 5581",
    "  *Yonkers (city) 8⅞ 6511",
    " Yates 8 5721",
]

# Verbatim lines from `pypdf`'s extraction of the live Publication 718 (2/25) PDF
# (2026-09-08 fetch), kept exactly as extracted so a change in NY's typesetting or in
# pypdf's text layout fails here rather than silently repricing the state. They cover
# every shape the one-page table uses: a plain county row, an en-dash `– except` county
# followed by its indented `(city)` rows, the MCTD asterisk, each vulgar fraction, rows
# padded with a trailing space, the borough cross-references that carry no rate, and the
# `*Suffolk` row, which extracts TAB-separated where every other row is space-separated.
LIVE_LINES = [
    " New York State only 4 0021",
    " Albany 8 0181",
    " Allegany 8½ 0221",
    " *Bronx – see New York City",
    " Cattaraugus – except 8 0481",
    "  Olean (city) 8 0441",
    "   Salamanca (city) 8 0431",
    " *Dutchess 8⅛ 1311 ",
    " Erie 8¾ 1451",
    " Herkimer 8¼ 2121",
    " Madison – except 8 2511",
    "  Oneida (city) 8 2541",
    " *Nassau 8⅝ 2811",
    " *New York (Manhattan) –  see New York City",
    " *New York City 8⅞ 8081",
    " Oneida – except 8¾ 3010",
    "  Rome (city) 8¾ 3015",
    "  Utica (city) 8¾ 3018",
    " Ontario 7½ 3211",
    " *Richmond (Staten Island)  – see New York City",
    " St. Lawrence – except 8 4091",
    "  Ogdensburg (city) 8 4012",
    "\t *Suffolk\t 8¾\t 4711",
    " *Westchester – except 8⅜ 5581",
    "  *Yonkers (city) 8⅞ 6511",
    "*Rates in these jurisdictions include 3/8% imposed for the benefit of the "
    "Metropolitan Commuter Transportation District.",
]


@pytest.fixture(autouse=True)
def _low_min_county_rows(monkeypatch):
    """The parse sanity gate defaults to whole-publication scale; every test here builds
    a handful of rows inline, so lower it for all of them. The tests that exercise the
    gate itself raise it back up within their own bodies."""
    monkeypatch.setattr(ny, "MIN_COUNTY_ROWS", 0)


def test_parse_lines_handles_fractions_except_and_cities():
    t = ny.parse_lines(LINES)
    assert t.counties["ALBANY"] == D("0.08")
    assert t.counties["ALLEGANY"] == D("0.085")
    assert t.counties["CATTARAUGUS"] == D("0.08")
    assert t.counties["WESTCHESTER"] == D("0.08375")
    assert t.counties["NEW YORK CITY"] == D("0.08875")
    assert t.cities["OLEAN"] == D("0.08") and t.cities["YONKERS"] == D("0.08875")
    assert "NEW YORK STATE ONLY" not in t.counties


def test_rows_use_city_then_county_and_map_nyc(monkeypatch):
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"10001": (40.75, -74.0), "10701": (40.93, -73.9),
                   "12203": (42.68, -73.8), "14760": (42.08, -78.43)},
        county={"10001": ("36061", "New York County"),
                "10701": ("36119", "Westchester County"),
                "12203": ("36001", "Albany County"),
                "14760": ("36009", "Cattaraugus County")},
        place={"10001": ("3651000", "New York city"), "10701": ("3684000", "Yonkers city"),
               "14760": ("3654881", "Olean city")},
    )
    rows = {r.zip: r for r in ny.NyAdapter().rows(c, date(2026, 9, 8))}
    assert rows["10001"].local_rate == D("0.04875") and rows["10001"].label == "New York City, NY"
    assert rows["10701"].local_rate == D("0.04875") and rows["10701"].label == "Yonkers, NY"
    assert rows["12203"].local_rate == D("0.04")
    assert rows["14760"].local_rate == D("0.04") and rows["14760"].label == "Olean, NY"


def test_parse_reads_the_live_pdf_lines():
    """Regression on the real extraction: every fraction, the `– except` suffix, the MCTD
    asterisk and the TAB-separated Suffolk row must all still read."""
    t = ny.parse_lines(LIVE_LINES)
    assert t.counties["DUTCHESS"] == D("0.08125")
    assert t.counties["ERIE"] == D("0.0875")
    assert t.counties["HERKIMER"] == D("0.0825")
    assert t.counties["NASSAU"] == D("0.08625")
    assert t.counties["ONTARIO"] == D("0.075")
    assert t.counties["SUFFOLK"] == D("0.0875")  # the TAB-separated row
    assert t.counties["ST. LAWRENCE"] == D("0.08")
    assert t.cities["ROME"] == D("0.0875") and t.cities["OGDENSBURG"] == D("0.08")
    # The borough cross-reference rows carry no rate and must not become counties.
    assert not {"BRONX", "RICHMOND (STATEN ISLAND)", "NEW YORK (MANHATTAN)"} & set(t.counties)
    # Nor may the MCTD footnote, whose "3/8%" is not a rate row.
    assert not any("MCTD" in n or "RATES IN THESE" in n for n in t.counties)


def test_parse_records_the_county_each_city_row_sits_in():
    """A `(city)` row is indented under its county's `– except` row and is taxed only
    inside that county, so the parse has to remember which county it was reading."""
    t = ny.parse_lines(LIVE_LINES)
    assert t.city_county["OLEAN"] == "CATTARAUGUS"
    assert t.city_county["ONEIDA"] == "MADISON"
    assert t.city_county["ROME"] == "ONEIDA" and t.city_county["UTICA"] == "ONEIDA"
    assert t.city_county["OGDENSBURG"] == "ST. LAWRENCE"
    assert t.city_county["YONKERS"] == "WESTCHESTER"


def test_a_city_row_applies_only_inside_its_own_county(monkeypatch):
    """`Oneida` names both a city taxed at 8% (in Madison County) and a county taxed at
    8.75%. Matching a ZIP on the place name alone would price every Oneida County ZIP
    whose place happened to read `Oneida` at the city's rate, so the city row is used
    only when the ZIP's own Census county is the county the city sits in."""
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LIVE_LINES)
    c = Census(
        centroids={"13421": (43.09, -75.65), "13435": (43.29, -75.4)},
        county={"13421": ("36053", "Madison County"), "13435": ("36065", "Oneida County")},
        # 13435 is a stand-in for the join the county gate has to refuse: a ZIP in Oneida
        # County whose place reads `Oneida` must pay the county's 8.75%, not the city's 8%.
        place={"13421": ("3654243", "Oneida city"), "13435": ("3654243", "Oneida city")},
    )
    rows = {r.zip: r for r in ny.NyAdapter().rows(c, date(2026, 9, 8))}
    assert rows["13421"].general_rate == D("0.08") and rows["13421"].label == "Oneida, NY"
    assert (rows["13435"].general_rate == D("0.0875")
            and rows["13435"].label == "Oneida County, NY")


def test_every_nyc_borough_takes_the_city_rate_whatever_its_place_says(monkeypatch):
    """Pub 718 files one 8.875% row for the whole of New York City and none for the five
    boroughs, which appear only as `– see New York City` cross-references. Census places
    inside the city are `New York city` for Manhattan but CDP-style names elsewhere, so
    the borough county is what decides, and the label is the city's."""
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LIVE_LINES)
    c = Census(
        centroids={z: (40.7, -74.0) for z in ("10001", "11201", "10451", "11373", "10301")},
        county={"10001": ("36061", "New York County"), "11201": ("36047", "Kings County"),
                "10451": ("36005", "Bronx County"), "11373": ("36081", "Queens County"),
                "10301": ("36085", "Richmond County")},
        place={"10001": ("3651000", "New York city")},
    )
    rows = list(ny.NyAdapter().rows(c, date(2026, 9, 8)))
    assert len(rows) == 5
    assert all(r.state == "NY" and r.state_rate == D("0.04") and r.local_rate == D("0.04875")
               and r.food_drug_rate is None and r.label == "New York City, NY" for r in rows)


def test_rows_fall_back_to_the_county_and_label_it(monkeypatch):
    """Unincorporated territory, a town, or a village Pub 718 does not tax separately:
    the ZIP pays its county's combined rate under the county's own label."""
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LIVE_LINES)
    c = Census(
        centroids={"13669": (44.6, -75.2), "12203": (42.68, -73.8)},
        county={"13669": ("36089", "St. Lawrence County"), "12203": ("36001", "Albany County")},
        place={"13669": ("3612023", "Canton village")},
    )
    rows = {r.zip: r for r in ny.NyAdapter().rows(c, date(2026, 9, 8))}
    assert rows["13669"].local_rate == D("0.04")
    assert rows["13669"].label == "St. Lawrence County, NY"
    assert rows["12203"].label == "Albany County, NY"


def test_rows_skip_a_zip_whose_county_has_no_row(monkeypatch):
    """Nothing in the publication covers the ZIP, so there is no rate to publish; it is
    dropped and counted rather than guessed at."""
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"12203": (42.68, -73.8), "14604": (43.16, -77.6)},
        county={"12203": ("36001", "Albany County"), "14604": ("36055", "Monroe County")},
        place={},
    )
    assert [r.zip for r in ny.NyAdapter().rows(c, date(2026, 9, 8))] == ["12203"]


def test_rows_skip_zips_outside_new_york_or_without_a_centroid(monkeypatch):
    monkeypatch.setattr(ny, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"12203": (42.68, -73.8)},
        county={"12203": ("36001", "Albany County"), "12208": ("36001", "Albany County"),
                "06901": ("09001", "Fairfield County")},
        place={},
    )
    assert [r.zip for r in ny.NyAdapter().rows(c, date(2026, 9, 8))] == ["12203"]


def test_parse_raises_when_too_few_county_rows(monkeypatch):
    """New York has 62 counties, 57 of them outside New York City, and the publication
    carries a row for every one. A PDF whose text layout shifted, or an error page, must
    fail the build rather than quietly publish 4% across the state."""
    monkeypatch.setattr(ny, "MIN_COUNTY_ROWS", 55)
    with pytest.raises(ValueError, match="only 6 county rows"):
        ny.parse_lines(LINES)


def test_parse_raises_when_the_new_york_city_row_is_missing(monkeypatch):
    """Every ZIP in the five boroughs depends on that single row; losing it would drop
    the state's largest market while the other 57 counties still parsed cleanly."""
    monkeypatch.setattr(ny, "MIN_COUNTY_ROWS", 1)
    with pytest.raises(ValueError, match="New York City"):
        ny.parse_lines([ln for ln in LINES if "New York City" not in ln])


def test_adapter_is_registered():
    assert any(a.name == "ny" and a.states == ("NY",) for a in ny.REGISTRY)
