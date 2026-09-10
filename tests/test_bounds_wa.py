import io
import json
import zipfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.bounds import wa
from pipeline.census import Census

FX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _low_floors(monkeypatch):
    monkeypatch.setattr(wa, "MIN_POLYGONS", 1)
    monkeypatch.setattr(wa, "MIN_RATE_ROWS", 1)


def page(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


def test_pick_quarter_takes_the_newest_file_at_or_before_the_build_quarter():
    """Some quarters publish no boundary file -- "no rate changes" -- and the previous one
    still applies; the URL's month segment is the Drupal upload month and is not derivable,
    so the page is scraped, never synthesised (spec §2.3)."""
    url, q = wa.pick_quarter(page("wa_boundaries_page.html"), wa._LOCCODE,
                             date(2026, 9, 9), what="boundaries")
    assert q == "26Q3"
    assert url == "https://dor.wa.gov/sites/default/files/2026-05/LOCCODE_PUBLIC_26Q3.zip"
    # 25Q4 has no file: 25Q3 would answer, and here the newest at or before 26Q2 is 26Q1.
    _u, q2 = wa.pick_quarter(page("wa_boundaries_page.html"), wa._LOCCODE,
                             date(2026, 5, 2), what="boundaries")
    assert q2 == "26Q1"


def test_pick_quarter_tolerates_the_drupal_dedupe_suffix_and_mixed_case():
    url, q = wa.pick_quarter(page("wa_boundaries_page.html"), wa._LOCCODE,
                             date(2023, 5, 2), what="boundaries")
    assert q == "23Q2" and url.endswith("LOCCODE_PUBLIC_23Q2_0.zip")
    url, q = wa.pick_quarter(page("wa_boundaries_page.html"), wa._LOCCODE,
                             date(2020, 2, 2), what="boundaries")
    assert q == "20Q1" and url.endswith("LOCCODE_Public_20Q1.zip")


def test_pick_quarter_raises_when_nothing_is_old_enough():
    with pytest.raises(ValueError, match="no boundaries file at or before 19Q4"):
        wa.pick_quarter(page("wa_boundaries_page.html"), wa._LOCCODE,
                        date(2019, 12, 1), what="boundaries")


def test_the_rate_file_must_be_the_build_quarter_exactly():
    """The rates are stamped for the quarter the build sits in, so an older rate file would
    publish last quarter's numbers under this quarter's `effectiveDate`."""
    url, q = wa.pick_quarter(page("wa_rates_page.html"), wa._RATES, date(2026, 9, 9),
                             what="rates", exact=True)
    assert q == "26Q3" and url.endswith("Rates_26Q3.zip")
    with pytest.raises(ValueError, match="no rates file for 27Q1"):
        wa.pick_quarter(page("wa_rates_page.html"), wa._RATES, date(2027, 1, 5),
                        what="rates", exact=True)


ON = date(2026, 9, 9)
HEADER = "Name,Code,State,Local,RTA,Rate,Effective Date,Expiration Date\n"


def rates_csv(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def test_parse_rates_splits_state_and_local_and_pads_the_code():
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"), ON)
    assert rates["1726"] == (D("0.065"), D("0.0405"), "SEATTLE")
    assert rates["4000"][1] == D("0.024")


def test_parse_rates_refuses_a_location_code_that_repeats():
    """One code names one rate area. A file that ever carried a second row for a code would
    otherwise collapse to whichever the reader saw last, at full row count, with no drops
    and every floor green -- and `diff-rates` cannot see it, because Washington's ZIP rows
    come from SST, not from this download."""
    text = rates_csv("SEATTLE,1726,0.065,0.0405,0,0.1055,20260701,20260930",
                     "SEATTLE ANNEX,1726,0.065,0.05,0,0.115,20260701,20260930")
    with pytest.raises(ValueError, match=r"location code 1726 has two rows in force"):
        wa.parse_rates(text, ON)


def test_parse_rates_ignores_a_row_not_in_force_on_the_build_date():
    """The DOR's own `Effective Date`/`Expiration Date` decide, so a file carrying more than
    one quarter's rows publishes the quarter the build is stamped for, not the last row."""
    text = rates_csv("SEATTLE,1726,0.065,0.0355,0,0.1005,20260401,20260630",
                     "SEATTLE,1726,0.065,0.0405,0,0.1055,20260701,20260930",
                     "SEATTLE,1726,0.065,0.0505,0,0.1155,20261001,20261231")
    assert wa.parse_rates(text, ON)["1726"] == (D("0.065"), D("0.0405"), "SEATTLE")


def test_a_code_with_no_row_in_force_is_absent_so_the_drop_gate_counts_it():
    """Not an error on its own: an area whose rows all expired is one unpriced polygon, and
    the existing drop gate is what decides whether that is a lag or a broken join."""
    text = rates_csv("SEATTLE,1726,0.065,0.0405,0,0.1055,20260701,20260930",
                     "KING COUNTY NON-RTA,4000,0.065,0.024,0,0.089,20250101,20250331")
    assert set(wa.parse_rates(text, ON)) == {"1726"}


def test_parse_rates_refuses_a_date_column_that_is_not_a_date():
    with pytest.raises(ValueError, match=r"code 1726 has an effective date 'Q3'"):
        wa.parse_rates(rates_csv("SEATTLE,1726,0.065,0.0405,0,0.1055,Q3,20260930"), ON)


def test_the_rates_csv_is_decoded_strictly_so_a_changed_encoding_fails_loudly():
    """`errors="replace"` would publish a rate area whose name carries a replacement
    character instead of failing; a mangled label is a wrong published name."""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as z:
        z.writestr("rates.csv", HEADER.encode() + b"CA\xf1ON,1726,0.065,0.0405,0,0.1055,"
                                                 b"20260701,20260930\n")
    with pytest.raises(UnicodeDecodeError):
        wa._rates_text(payload.getvalue())


def test_to_features_joins_on_the_code_labels_in_the_dor_namespace_and_counts_drops():
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"), ON)
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    feats, profiles = wa.to_features(doc, rates, "26Q3", {})
    assert [f["properties"]["id"] for f in feats] == ["wa-dor:1726", "wa-dor:4000"]
    assert all(f["properties"]["source"] == "wa-dor" for f in feats)
    labels = {p["label"] for p in profiles.values()}
    # The DOR names rate areas in its own namespace, not the Census's (decision #71); the
    # acronyms Task 0 taught `display_name` survive.
    assert labels == {"Seattle, WA", "King County Non-RTA, WA"}
    assert profiles["WA-0.065-0.0405-SEATTLE, WA"]["localRate"] == "0.0405"


def test_a_dor_name_that_is_a_census_place_keeps_the_census_casing():
    """`display_name` title-cases the DOR's uppercase `Name`, which writes `Seatac` where
    the Census (and so the rates file's ZIP row for the same jurisdiction) writes `SeaTac`.
    The profile id uppercases the label, so the two files shared the id and disagreed on the
    body under it. The DOR's own namespace (`KING COUNTY NON-RTA`) is left alone."""
    census = Census(
        centroids={"98188": (47.44, -122.28)},
        county={"98188": ("53033", "King County")},
        place={"98188": ("5362288", "SeaTac city")},
    )
    casing = wa.census_casing(census)
    assert casing["SEATAC"] == "SeaTac"
    rates = {"1726": (D("0.065"), D("0.0405"), "SEATAC"),
             "4000": (D("0.065"), D("0.024"), "KING COUNTY NON-RTA")}
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    _feats, profiles = wa.to_features(doc, rates, "26Q3", casing)
    assert {p["label"] for p in profiles.values()} == {"SeaTac, WA", "King County Non-RTA, WA"}


def test_census_casing_is_washingtons_places_only():
    census = Census(
        centroids={"98188": (47.44, -122.28), "97201": (45.5, -122.68)},
        county={"98188": ("53033", "King County"), "97201": ("41051", "Multnomah County")},
        place={"98188": ("5362288", "SeaTac city"), "97201": ("4159000", "Portland city")},
    )
    assert set(wa.census_casing(census)) == {"SEATAC"}


def test_to_features_fails_when_too_many_polygons_have_no_rate_row():
    rates = {"1726": (D("0.065"), D("0.0405"), "SEATTLE")}
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match=r"2 of 3 polygons \(66.7%\)"):
        wa.to_features(doc, rates, "26Q3", {})


def test_to_features_fails_below_the_polygon_floor(monkeypatch):
    monkeypatch.setattr(wa, "MIN_POLYGONS", 380)
    monkeypatch.setattr(wa, "MAX_DROP_SHARE", 1.0)
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"), ON)
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="only 2 polygons"):
        wa.to_features(doc, rates, "26Q3", {})
