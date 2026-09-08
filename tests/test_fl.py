from datetime import date
from decimal import Decimal as D

import pytest

from pipeline.census import Census
from pipeline.sources import fl

LINES = [
    "Alachua 1.5%          (.5%) Jan 1, 2019    Dec 31, 2030",
    "(1%) Jan 1, 2023    Dec 31, 2032",
    "Baker 1% Jan 1, 1994    None",
    "Citrus None",
    "DeSoto 1.5% (1%) Jan 1, 1988    None",
    "Hernando .5% Jan 1, 2016    Dec 31, 2035",
    "Miami-Dade 1% (.5%) Jan 1, 1992    None",
    "St. Johns .5% Jan 1, 2023    Dec 31, 2032",
    "Hamilton 2%             (1%) Jul 1, 1990    Dec 31, 2029",
    # The form's own title line, which is what dates it (C4): every adapter test needs
    # one to get past the staleness check. It is not shaped like a rate row, so it does
    # not become a county.
    "Discretionary Sales Surtax Information for Calendar Year 2025 DR-15DSS",
]

# Verbatim lines from `pypdf`'s extraction of the live DR-15DSS (R. 11/24, "Discretionary
# Sales Surtax Information for Calendar Year 2025"), fetched 2026-09-08 and kept exactly as
# extracted -- trailing spaces, run-on padding and all -- so a change in Florida's
# typesetting or in pypdf's text layout fails here rather than silently repricing the
# state. They cover every shape the two-page form uses: a single-surtax row, a
# multi-surtax row whose components continue on unindented `(...)` lines, a `None` row, a
# two-word county, a hyphenated county, a county with a period in its name, the page-1
# column headings and footer, and -- crucially -- the page-2 "rate changes" narrative,
# whose `<County> <rate>% Total Surtax Rate` headings look exactly like table rows to a
# naive row regex.
LIVE_LINES = [
    "Alachua 1.5%          (.5%) Jan 1, 2019    Dec 31, 2030",
    "(1%) Jan 1, 2023    Dec 31, 2032",
    "Baker 1% Jan 1, 1994    None",
    "Citrus None",
    "Collier None   ",
    "DeSoto 1.5% (1%) Jan 1, 1988    None",
    "(.5%) Jan 1, 2015    Dec 31, 2035",
    "Hamilton 2%             (1%) Jul 1, 1990    Dec 31, 2029",
    "Hernando .5% Jan 1, 2016    Dec 31, 2035",
    "Hillsborough 1.5% (.5%) Dec 1, 1996    Dec 31, 2041",
    "Indian River 1% Jun 1, 1989    Dec 31, 2034",
    "Miami-Dade 1% (.5%) Jan 1, 1992    None",
    "Orange .5% Jan 1, 2003    Dec 31, 2035",
    "Palm Beach 1% Jan 1, 2017    Dec 31, 2026",
    "St. Johns .5% Jan 1, 2016    Dec 31, 2035",
    "St. Lucie 1%         (.5%) Jul 1, 1996    Dec 31, 2036",
    "Santa Rosa 1% (.5%) Oct 1, 1998    Dec 31, 2028",
    "Discretionary Sales Surtax Information for Calendar Year 2025 DR-15DSS",
    "R. 11/24",
    "Page 1 of 2",
    "  County       Surtax Rate    Date   Date",
    "County     Surtax Rate  Date                   Date",
    "See TIP 24A01-15",
    "about suspended rates.",
    # Page 2: the "rate changes" narrative. Every one of these headings restates a county
    # already listed on page 1, and two of them spell St. Johns / St. Lucie without the
    # period, so letting them through both inflates the row count and forks the join key.
    "Hamilton         2% Total Surtax Rate",
    "Hernando       .5% Total Surtax Rate",
    "Hillsborough  1.5% Total Surtax Rate",
    "Orange            .5% Total Surtax Rate",
    "St Johns         .5% Total Surtax Rate",
    "St Lucie          1% Total Surtax Rate",
    "                                �E   New 1% enhanced fire protection and rescue "
    "services surtax begins 1/1/2025 and                                                ",
    "                                    and expires 12/31/2041. See TIP 24A01-15 about "
    "surtax suspension.",
    "R. 11/24",
    "Page 2 of 2",
]


@pytest.fixture(autouse=True)
def _low_min_county_rows(monkeypatch):
    """The parse sanity gate defaults to whole-form scale (60 of Florida's 67 counties);
    every test here builds a handful of rows inline, so lower it for all of them. The test
    that exercises the gate itself raises it back up within its own body."""
    monkeypatch.setattr(fl, "MIN_COUNTY_ROWS", 0)


def test_parse_lines():
    t = fl.parse_lines(LINES)
    assert t["ALACHUA"] == D("0.015") and t["BAKER"] == D("0.01") and t["CITRUS"] == D("0")
    assert t["DESOTO"] == D("0.015") and t["HERNANDO"] == D("0.005") and t["HAMILTON"] == D("0.02")
    assert t["MIAMI-DADE"] == D("0.01") and t["ST. JOHNS"] == D("0.005")
    assert len(t) == 8


def test_parse_reads_the_live_pdf_lines():
    """Regression on the real extraction: the total-surtax column, `None`, two-word and
    hyphenated county names and the `(...)` component continuation lines must all still
    read the way they did on 2026-09-08."""
    t = fl.parse_lines(LIVE_LINES)
    assert t["COLLIER"] == D("0") and t["CITRUS"] == D("0")
    assert t["HILLSBOROUGH"] == D("0.015") and t["INDIAN RIVER"] == D("0.01")
    assert t["ORANGE"] == D("0.005") and t["PALM BEACH"] == D("0.01")
    assert t["ST. JOHNS"] == D("0.005") and t["ST. LUCIE"] == D("0.01")
    assert t["SANTA ROSA"] == D("0.01") and t["MIAMI-DADE"] == D("0.01")
    # The `(.5%) Jan 1, 2015 ...` continuation lines are components of the county above,
    # not rows: DeSoto's total is the 1.5% on its own line, never the 1% or the .5%.
    assert t["DESOTO"] == D("0.015")
    # Column headings, the form footer and the TIP cross-reference are not counties.
    assert not {"COUNTY", "DISCRETIONARY", "SEE", "PAGE", "R."} & set(t)
    assert not any("SURTAX" in n or "TIP" in n for n in t)


def test_parse_ignores_the_page_two_rate_change_narrative():
    """Page 2's `<County> <rate>% Total Surtax Rate` headings match a row regex exactly.
    They restate page-1 counties, and `St Johns` / `St Lucie` drop the period there, so
    admitting them would inflate the row count the sanity gate reads and fork the county
    key the Census join depends on."""
    t = fl.parse_lines(LIVE_LINES)
    assert "ST JOHNS" not in t and "ST LUCIE" not in t
    assert len(t) == 15  # the 15 distinct counties on page 1 of the fixture, and no more


def test_rows_by_county(monkeypatch):
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"33131": (25.77, -80.19), "32601": (29.65, -82.32), "34428": (28.9, -82.6)},
        county={"33131": ("12086", "Miami-Dade County"),
                "32601": ("12001", "Alachua County"),
                "34428": ("12017", "Citrus County")},
        place={"33131": ("1245000", "Miami city")},
    )
    rows = {r.zip: r for r in fl.FlAdapter().rows(c, date(2026, 9, 8))}
    assert rows["33131"].local_rate == D("0.01")
    assert rows["32601"].local_rate == D("0.015") and rows["32601"].label == "Alachua County, FL"
    assert rows["34428"].local_rate == D("0")


def test_every_label_is_the_county_even_where_a_place_is_known(monkeypatch):
    """Florida's surtax is imposed by the county and by nothing below it, so the Census
    place a ZIP sits in never changes the rate and must never change the label either --
    33131 is `Miami city` to the Census and still `Miami-Dade County, FL` here."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"33131": (25.77, -80.19), "32601": (29.65, -82.32)},
        county={"33131": ("12086", "Miami-Dade County"), "32601": ("12001", "Alachua County")},
        place={"33131": ("1245000", "Miami city")},
    )
    rows = {r.zip: r for r in fl.FlAdapter().rows(c, date(2026, 9, 8))}
    assert rows["33131"].label == "Miami-Dade County, FL"
    assert rows["32601"].label == "Alachua County, FL"


def test_rows_carry_the_state_rate_and_no_food_drug_rate(monkeypatch):
    """Florida taxes at 6% statewide with no reduced food/drug rate to publish; grocery
    food and prescription drugs are exempt outright rather than rate-reduced."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"33131": (25.77, -80.19), "34428": (28.9, -82.6)},
        county={"33131": ("12086", "Miami-Dade County"), "34428": ("12017", "Citrus County")},
        place={},
    )
    rows = {r.zip: r for r in fl.FlAdapter().rows(c, date(2026, 9, 8))}
    assert all(r.state == "FL" and r.state_rate == D("0.06") and r.food_drug_rate is None
               for r in rows.values())
    assert rows["33131"].general_rate == D("0.07")
    assert rows["34428"].general_rate == D("0.06")  # a county with no surtax pays 6% flat


def test_the_county_join_tolerates_spelling_divergence(monkeypatch):
    """`join_key` on both sides, as Texas and Illinois do: the Census and the DR-15DSS
    disagree about `St.` vs `Saint` for the same county, and a raw string compare would
    drop that county's entire ZIP set to the missing-county counter."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"32084": (29.9, -81.3)},
        county={"32084": ("12109", "Saint Johns County")},  # DR-15DSS files `St. Johns`
        place={},
    )
    rows = {r.zip: r for r in fl.FlAdapter().rows(c, date(2026, 9, 8))}
    assert rows["32084"].local_rate == D("0.005")


def test_rows_skip_a_zip_whose_county_has_no_row(monkeypatch, capsys):
    """Nothing in the form covers the county, so there is no rate to publish: the ZIP is
    dropped and the county counted rather than guessed at as 6% flat."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"32601": (29.65, -82.32), "33907": (26.55, -81.87)},
        county={"32601": ("12001", "Alachua County"), "33907": ("12071", "Lee County")},
        place={},
    )
    assert [r.zip for r in fl.FlAdapter().rows(c, date(2026, 9, 8))] == ["32601"]
    out = capsys.readouterr().out
    assert "1 dropped across 1 counties" in out and "LEE" in out


def test_rows_skip_zips_outside_florida_or_without_a_centroid(monkeypatch):
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"32601": (29.65, -82.32)},
        county={"32601": ("12001", "Alachua County"),
                "32603": ("12001", "Alachua County"),      # no centroid
                "31401": ("13051", "Chatham County")},     # Georgia
        place={},
    )
    assert [r.zip for r in fl.FlAdapter().rows(c, date(2026, 9, 8))] == ["32601"]


def test_calendar_year_accepts_last_years_form_and_rejects_an_older_one():
    """C4: the fetch URL is Florida's `/current/` one, so a stale form is served as if it
    were current. On 2026-09-08 that URL still served the CY2025 form and its surtaxes
    were still the ones in force, so one year of slack has to pass; a form two years
    behind means Florida stopped updating it and the build must fail."""
    on = date(2026, 9, 8)
    assert fl.check_calendar_year(LIVE_LINES, on) == 2025          # the live 2026-09-08 form
    stale = [ln.replace("Calendar Year 2025", "Calendar Year 2024") for ln in LIVE_LINES]
    with pytest.raises(ValueError, match="calendar year 2024"):
        fl.check_calendar_year(stale, on)
    # A build a year later would no longer accept CY2025 either.
    with pytest.raises(ValueError, match="calendar year 2025"):
        fl.check_calendar_year(LIVE_LINES, date(2027, 1, 1))


def test_calendar_year_raises_when_the_title_line_carries_no_year():
    """A form whose title changed would leave the staleness check silently blind, which
    is the failure it exists to prevent."""
    with pytest.raises(ValueError, match="no `Calendar Year"):
        fl.check_calendar_year(["Alachua 1.5%"], date(2026, 9, 8))


def test_rows_refuse_a_stale_form(monkeypatch):
    """The check runs from the adapter, before any ZIP is priced."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: [
        ln.replace("Calendar Year 2025", "Calendar Year 2019") for ln in LINES])
    c = Census(centroids={"32601": (29.65, -82.32)},
               county={"32601": ("12001", "Alachua County")}, place={})
    with pytest.raises(ValueError, match="calendar year 2019"):
        list(fl.FlAdapter().rows(c, date(2026, 9, 8)))


def test_the_county_label_keeps_the_census_casing(monkeypatch):
    """C1: `DeSoto County, FL`, not the `Desoto` that re-casing an uppercased name gives.
    Miami-Dade also has to survive the hyphen, which `.title()` alone would keep."""
    monkeypatch.setattr(fl, "_fetch_lines", lambda: LINES)
    c = Census(
        centroids={"34266": (27.2, -81.8), "33131": (25.77, -80.19)},
        county={"34266": ("12027", "DeSoto County"), "33131": ("12086", "Miami-Dade County")},
        place={},
    )
    rows = {r.zip: r for r in fl.FlAdapter().rows(c, date(2026, 9, 8))}
    assert rows["34266"].label == "DeSoto County, FL"
    assert rows["33131"].label == "Miami-Dade County, FL"


def test_parse_raises_when_too_few_county_rows(monkeypatch):
    """Florida has 67 counties and the DR-15DSS carries a row for every one, `None`
    included. A PDF whose text layout shifted, or an error page served in its place, must
    fail the build loudly rather than quietly publish 6% across the state."""
    monkeypatch.setattr(fl, "MIN_COUNTY_ROWS", 60)
    with pytest.raises(ValueError, match="only 8 county rows"):
        fl.parse_lines(LINES)


def test_adapter_is_registered():
    assert any(a.name == "fl" and a.states == ("FL",) for a in fl.REGISTRY)
