import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.bounds import wa

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


def test_parse_rates_splits_state_and_local_and_pads_the_code():
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"))
    assert rates["1726"] == (D("0.065"), D("0.0405"), "SEATTLE")
    assert rates["4000"][1] == D("0.024")


def test_to_features_joins_on_the_code_labels_in_the_dor_namespace_and_counts_drops():
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"))
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    feats, profiles = wa.to_features(doc, rates, "26Q3")
    assert [f["properties"]["id"] for f in feats] == ["wa-dor:1726", "wa-dor:4000"]
    assert all(f["properties"]["source"] == "wa-dor" for f in feats)
    labels = {p["label"] for p in profiles.values()}
    # The DOR names rate areas in its own namespace, not the Census's (decision #71); the
    # acronyms Task 0 taught `display_name` survive.
    assert labels == {"Seattle, WA", "King County Non-RTA, WA"}
    assert profiles["WA-0.065-0.0405-SEATTLE, WA"]["localRate"] == "0.0405"


def test_to_features_fails_when_too_many_polygons_have_no_rate_row():
    rates = {"1726": (D("0.065"), D("0.0405"), "SEATTLE")}
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match=r"2 of 3 polygons \(66.7%\)"):
        wa.to_features(doc, rates, "26Q3")


def test_to_features_fails_below_the_polygon_floor(monkeypatch):
    monkeypatch.setattr(wa, "MIN_POLYGONS", 380)
    monkeypatch.setattr(wa, "MAX_DROP_SHARE", 1.0)
    rates = wa.parse_rates((FX / "wa_rates_slice.csv").read_text(encoding="utf-8"))
    doc = json.loads((FX / "wa_loccode_slice.geojson").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="only 2 polygons"):
        wa.to_features(doc, rates, "26Q3")
