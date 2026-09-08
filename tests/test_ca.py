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
    in ``JURIS_NAME``; both that and the fixture's spelling key to ``(county, "")``. Also
    covers multi-word county names in ``JURIS_NAME``, which don't get split apart because
    the county comes straight from ``County_name``, never parsed out of ``JURIS_NAME``."""
    t = ca.parse(
        csv_text(
            "1,UNINCORPORATED AREA-ALAMEDA,ALAMEDA,UNINCORPORATED,Unincorporated,0.1025,"
            f"4/1/2025 7:00:00 AM,{TAIL}",
            "2,UNINCORPORATED AREA-SAN LUIS OBISPO,SAN LUIS OBISPO,UNINCORPORATED,"
            f"Unincorporated,0.0725,4/1/2025 7:00:00 AM,{TAIL}",
            "3,UNINCORPORATED AREA-EL DORADO,EL DORADO,UNINCORPORATED,Unincorporated,"
            f"0.0725,4/1/2025 7:00:00 AM,{TAIL}",
        )
    )
    assert t[("ALAMEDA", "")] == (D("0.1025"), "Unincorporated")
    assert t[("SAN LUIS OBISPO", "")] == (D("0.0725"), "Unincorporated")
    assert t[("EL DORADO", "")] == (D("0.0725"), "Unincorporated")


def test_parse_keeps_the_latest_start_date_when_a_jurisdiction_repeats():
    t = ca.parse(
        csv_text(
            f"1,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.1025,4/1/2025 7:00:00 AM,{TAIL}",
            f"2,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.0975,1/1/2005 8:00:00 AM,{TAIL}",
        )
    )
    assert t[("ALAMEDA", "DUBLIN")][0] == D("0.1025")


def test_parse_ignores_a_future_dated_row_until_on_reaches_it():
    """CDTFA pre-publishes next quarter's rate ahead of its effective date. `parse` must
    not adopt a row whose `START_DATE` is still in the future relative to the build date,
    even though it is the latest `START_DATE` in the file."""
    text = csv_text(
        f"1,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.0975,1/1/2020 8:00:00 AM,{TAIL}",
        f"2,DUBLIN,ALAMEDA,DUBLIN,Dublin,0.1025,1/1/2030 7:00:00 AM,{TAIL}",
    )
    before = ca.parse(text, date(2026, 9, 8))
    assert before[("ALAMEDA", "DUBLIN")][0] == D("0.0975")
    on_date = ca.parse(text, date(2030, 1, 1))
    assert on_date[("ALAMEDA", "DUBLIN")][0] == D("0.1025")


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


def test_rows_uses_the_alias_for_a_divergent_city_name(monkeypatch):
    """Census normalises Angels Camp to the bare place name ``ANGELS``; CDTFA keys the
    jurisdiction ``ANGELS CAMP``. Without the alias this ZIP would silently fall back to
    the county's unincorporated rate instead of the city rate."""
    monkeypatch.setattr(
        ca,
        "_fetch_text",
        lambda: csv_text(
            f"1,ANGELS CAMP,CALAVERAS,ANGELS CAMP,Angels Camp,0.0875,4/1/2025 7:00:00 AM,{TAIL}",
            f"2,CALAVERAS,CALAVERAS,UNINCORPORATED,Unincorporated,0.0725,4/1/2025 7:00:00 AM,"
            f"{TAIL}",
        ),
    )
    c = Census(
        centroids={"95221": (38.07, -120.54)},
        county={"95221": ("06009", "Calaveras County")},
        place={"95221": ("0602182", "Angels city")},
    )
    rows = {r.zip: r for r in ca.CaAdapter().rows(c, date(2026, 9, 8))}
    assert rows["95221"].local_rate == D("0.0150")
    assert rows["95221"].label == "Angels Camp, CA"


def test_rows_falls_back_to_the_parenthetical_when_no_alias_covers_it(monkeypatch):
    """A Census place name with a parenthetical that isn't in ALIASES still resolves: the
    parenthetical alone is tried first, then the name with it stripped."""
    monkeypatch.setattr(
        ca,
        "_fetch_text",
        lambda: csv_text(
            f"1,BAR,TESTCOUNTY,BAR,Bar,0.08,4/1/2025 7:00:00 AM,{TAIL}",
            f"2,FOO,OTHERCOUNTY,FOO,Foo,0.085,4/1/2025 7:00:00 AM,{TAIL}",
        ),
    )
    c = Census(
        centroids={"00001": (0.0, 0.0), "00002": (0.0, 0.0)},
        county={
            "00001": ("06999", "Testcounty County"),
            "00002": ("06998", "Othercounty County"),
        },
        place={"00001": ("0600001", "Foo (Bar) city"), "00002": ("0600002", "Foo (Baz) city")},
    )
    rows = {r.zip: r for r in ca.CaAdapter().rows(c, date(2026, 9, 8))}
    assert rows["00001"].local_rate == D("0.0075")  # matched via the parenthetical alone
    assert rows["00002"].local_rate == D("0.0125")  # matched via the name with it stripped


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
