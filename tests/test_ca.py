from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.census import Census
from pipeline.sources import ca

FX = Path(__file__).parent / "fixtures"

HEADER = (
    "OBJECTID,JURIS_NAME,County_name,City_name,City_Name_Proper,RATE,"
    "START_DATE,DateStamp,DateCreated,Shape__Area,Shape__Length"
)
TAIL = "7/1/2026 7:00:00 AM,5/11/2026 7:00:00 AM,1,1"


def csv_text(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


def test_parse_keys_cities_and_unincorporated():
    t = ca.parse((FX / "ca_rates_slice.csv").read_text(encoding="utf-8-sig"))
    assert t[("LOS ANGELES", "LOS ANGELES")] == (D("0.095"), "Los Angeles")
    assert t[("LOS ANGELES", "")] == (D("0.095"), "Unincorporated Area-Los Angeles")
    assert t[("ALPINE", "")][0] == D("0.0725")


def test_parse_keys_the_live_unincorporated_shape():
    """CDTFA ships ``City_name`` as a bare ``UNINCORPORATED`` and carries the county only
    in ``JURIS_NAME``; both that and the fixture's spelling key to ``(county, "")``."""
    t = ca.parse(
        csv_text(
            "1,UNINCORPORATED AREA-ALAMEDA,ALAMEDA,UNINCORPORATED,Unincorporated,0.1025,"
            f"4/1/2025 7:00:00 AM,{TAIL}",
        )
    )
    assert t[("ALAMEDA", "")] == (D("0.1025"), "Unincorporated")


def test_parse_keeps_the_latest_start_date_when_a_jurisdiction_repeats():
    t = ca.parse(
        csv_text(
            f"1,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.1025,4/1/2025 7:00:00 AM,{TAIL}",
            f"2,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.0975,1/1/2005 8:00:00 AM,{TAIL}",
        )
    )
    assert t[("ALAMEDA", "DUBLIN")][0] == D("0.1025")


def test_parse_rejects_a_rate_below_the_state_rate():
    with pytest.raises(ValueError, match="BOGUS"):
        ca.parse(csv_text(f"1,BOGUS,ALPINE,BOGUS,Bogus,0.06,4/1/2025 7:00:00 AM,{TAIL}"))


def test_rows_match_place_then_unincorporated_then_drop(monkeypatch):
    monkeypatch.setattr(
        ca, "_fetch_text", lambda: (FX / "ca_rates_slice.csv").read_text(encoding="utf-8-sig")
    )
    c = Census(
        centroids={
            "90012": (34.05, -118.24), "91001": (34.19, -118.13),
            "96120": (38.7, -119.8), "94103": (37.77, -122.41),
        },
        county={
            "90012": ("06037", "Los Angeles County"), "91001": ("06037", "Los Angeles County"),
            "96120": ("06003", "Alpine County"), "94103": ("06075", "San Francisco County"),
        },
        place={
            "90012": ("0644000", "Los Angeles city"), "91001": ("0602252", "Altadena CDP"),
            "94103": ("0667000", "San Francisco city"),
        },
    )
    rows = {r.zip: r for r in ca.CaAdapter().rows(c, date(2026, 9, 8))}
    assert rows["90012"].local_rate == D("0.0225") and rows["90012"].label == "Los Angeles, CA"
    assert rows["91001"].local_rate == D("0.0225")
    assert rows["91001"].label == "Los Angeles County, CA"  # CDP -> unincorporated
    assert rows["96120"].local_rate == D("0")
    assert rows["94103"].local_rate == D("0.01375")
    assert set(rows) == {"90012", "91001", "96120", "94103"}


def test_rows_drop_a_zip_whose_county_has_no_unincorporated_row(monkeypatch):
    """San Francisco is a consolidated city-county: CDTFA files no unincorporated row for
    it, so a San Francisco ZIP whose Census place is not the city has nowhere to land."""
    monkeypatch.setattr(
        ca, "_fetch_text", lambda: (FX / "ca_rates_slice.csv").read_text(encoding="utf-8-sig")
    )
    c = Census(
        centroids={"94128": (37.62, -122.38)},
        county={"94128": ("06075", "San Francisco County")},
        place={},
    )
    assert list(ca.CaAdapter().rows(c, date(2026, 9, 8))) == []


def test_adapter_is_registered():
    assert any(a.name == "ca" and a.states == ("CA",) for a in ca.REGISTRY)
