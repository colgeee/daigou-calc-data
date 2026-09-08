from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.census import Census
from pipeline.sources import sst

FX = Path(__file__).parent / "fixtures"
ON = date(2026, 9, 8)


def census():
    return Census(
        centroids={
            "98001": (47.3, -122.2),
            "83856": (48.2, -117.0),
            "98002": (47.31, -122.21),
        },
        county={
            "98001": ("53033", "King County"),
            "83856": ("53051", "Pend Oreille County"),
            "98002": ("53033", "King County"),
        },
        place={
            "98001": ("5303180", "Auburn city"),
            "98002": ("5303180", "Auburn city"),
        },
    )


def test_latest_files_picks_newest_per_state_case_insensitively():
    files = sst.latest_files((FX / "sst_rates_index.html").read_text(), "R")
    assert files["WA"] == "/ratesandboundry/Rates/WAR2026Q4AUG27.zip"
    assert files["TN"].endswith("TNR2026Q4AUG21.csv")
    assert files["WY"].endswith("WYR2026Q4AUG20.CSV")
    assert files["IN"].endswith("INR2008Q4MAY7.csv")
    assert "B" not in files and len(files) == 4


def test_parse_rate_file_normalises_types_and_dates():
    rows = sst.parse_rate_file((FX / "sst_wa_rate.csv").read_text())
    assert len(rows) == 5
    st = rows[0]
    assert (st.jtype, st.code, st.general, st.food) == (45, "53", D("0.065"), D("0.065"))
    assert rows[1].jtype == 0 and rows[1].code == "051"
    assert rows[2].jtype == 1 and rows[2].code == "03180"
    assert rows[3].end == date(2024, 3, 31) and rows[4].end == date(9999, 12, 31)
    # VT leaves the food/drug columns empty on some rows
    blank = sst.parse_rate_file("50,02,00501,0.01,0.01,,,20061001,20221231\n")
    assert blank[0].general == D("0.01") and blank[0].food == D(0)


def test_parse_boundary_zips_reads_only_z_rows_and_the_documented_columns():
    zs = sst.parse_boundary_zips((FX / "sst_wa_boundary.csv").read_text())
    assert len(zs) == 5
    assert zs[0].zip_low == "98001" and zs[0].districts == ("L1702",)
    assert zs[0].county == "" and zs[0].place == ""
    assert zs[2].county == "051" and zs[2].districts == ()
    assert zs[4].place == "03180" and zs[4].districts == ("L1702",)
    # MN ships its ZIP records with a lowercase record type
    lower = (FX / "sst_wa_boundary.csv").read_text().replace("Z,", "z,", 1)
    assert len(sst.parse_boundary_zips(lower)) == 5


def test_compose_sums_state_county_place_districts_and_keeps_highest_current_row():
    rates = sst.parse_rate_file((FX / "sst_wa_rate.csv").read_text())
    zs = sst.parse_boundary_zips((FX / "sst_wa_boundary.csv").read_text())
    out = {r.zip: r for r in sst.compose("WA", rates, zs, census(), ON)}
    # current L1702 row, not 0.030
    assert out["98001"].state_rate == D("0.065") and out["98001"].local_rate == D("0.035")
    assert out["98001"].food_drug_rate == D("0.065")  # WA food rate = state portion only
    assert out["98001"].label == "Auburn, WA"
    # No place: the county, named the way the Census names it, entity word and all (F5).
    assert out["83856"].local_rate == D("0.012")
    assert out["83856"].label == "Pend Oreille County, WA"
    # 98002 has two current rows: county-only (0.012) and
    # place+district (0.021+0.035) -> highest wins
    assert out["98002"].local_rate == D("0.056")


def test_labels_keep_the_census_casing():
    """C1: the label is the Census place, or failing that its county, with the casing the
    Census publishes -- `O'Fallon`, `DeKalb` -- not what re-casing an uppercased join
    name makes of them (`Dekalb`). Georgia and Tennessee are SST states and both file a
    DeKalb County; O'Fallon stands in for the apostrophe names. The county form is the
    Census `NAMELSAD` verbatim (F5), so it carries its own entity word."""
    rates = sst.parse_rate_file("53,45,53,0.065,0.065,0.065,0.065,19830301,99991231\n")
    zs = [sst.ZipRow("98001", "98002", "", "", (), date(2024, 4, 1), sst.OPEN_END)]
    c = census()
    c.place["98001"] = ("5303180", "O'Fallon city")
    c.county["98002"] = ("53037", "DeKalb County")  # the casing is the point, not the map
    c.place.pop("98002")
    out = {r.zip: r for r in sst.compose("WA", rates, zs, c, ON)}
    assert out["98001"].label == "O'Fallon, WA"        # the place wins where there is one
    assert out["98002"].label == "DeKalb County, WA"   # otherwise the county, as published


def nv_census():
    return Census(
        centroids={"89701": (39.16, -119.75), "89101": (36.17, -115.14)},
        county={"89701": ("32510", "Carson City"), "89101": ("32003", "Clark County")},
        place={"89701": ("3212000", "Carson City"), "89101": ("3240000", "Las Vegas city")},
    )


def test_nevada_publishes_the_statutory_state_share_not_the_files_zero():
    """Nevada files its state-level (45) row at 0 and carries the whole 6.85 % statewide
    minimum in county rows, so every Nevada profile published `stateRate` "0" with the
    entire rate as local -- an app that shows the split, or lets the user override the
    local part, reads that as a state with no sales tax. `STATE_RATE_FLOOR` publishes the
    statutory minimum (2 % state + 2.6 % LSST + 2.25 % city-county relief) as the state
    share and only what a county levies above it as local. The general rate is untouched:
    Carson City's 7.6 % and Clark County's 8.375 % are exactly what the file composes."""
    rates = sst.parse_rate_file(
        "32,45,32,0.00000,0.00000,0.00000,0.00000,20090701,99991231\n"
        "32,00,510,0.07600,0.07600,0.07600,0.07600,20140401,99991231\n"
        "32,00,003,0.08375,0.08375,0.08375,0.08375,20200101,99991231\n"
    )
    zs = [
        sst.ZipRow("89701", "89701", "510", "", (), date(2024, 4, 1), sst.OPEN_END),
        sst.ZipRow("89101", "89101", "003", "", (), date(2024, 4, 1), sst.OPEN_END),
    ]
    out = {r.zip: r for r in sst.compose("NV", rates, zs, nv_census(), ON)}
    assert out["89701"].state_rate == D("0.0685")
    assert out["89701"].local_rate == D("0.0075")
    assert out["89701"].general_rate == D("0.076")
    assert out["89701"].label == "Carson City, NV"
    assert out["89101"].state_rate == D("0.0685")
    assert out["89101"].local_rate == D("0.01525")
    assert out["89101"].general_rate == D("0.08375")


def test_the_state_rate_floor_only_applies_to_the_states_it_names():
    """Washington files a real 45 row, so its own state share stands and the split is
    exactly what it was before the floor existed."""
    assert "WA" not in sst.STATE_RATE_FLOOR
    rates = sst.parse_rate_file((FX / "sst_wa_rate.csv").read_text())
    zs = sst.parse_boundary_zips((FX / "sst_wa_boundary.csv").read_text())
    out = {r.zip: r for r in sst.compose("WA", rates, zs, census(), ON)}
    assert out["98001"].state_rate == D("0.065") and out["98001"].local_rate == D("0.035")


def test_compose_refuses_a_composed_rate_below_the_state_floor():
    """Nevada's lowest county rate is exactly the 6.85 % floor, so nothing can go negative
    today; a file that fell below it would mean the floor is wrong for the state, and the
    build must fail rather than publish a negative local rate."""
    rates = sst.parse_rate_file(
        "32,45,32,0.00000,0.00000,0.00000,0.00000,20090701,99991231\n"
        "32,00,510,0.06000,0.06000,0.06000,0.06000,20140401,99991231\n"
    )
    zs = [sst.ZipRow("89701", "89701", "510", "", (), date(2024, 4, 1), sst.OPEN_END)]
    with pytest.raises(ValueError, match="below the published state share 0.0685"):
        sst.compose("NV", rates, zs, nv_census(), ON)


def ri_census():
    """RI-shaped census: one out-of-state ZIP, one in-state ZIP with no centroid."""
    counties = {
        "01001": "25013", "02801": "44005", "02840": "44005",
        "02888": "44007", "02940": "44007", "02999": "44007",
    }
    return Census(
        centroids={z: (41.5, -71.3) for z in counties if z != "02888"},
        county={z: (g, "Newport County") for z, g in counties.items()},
        place={"02840": ("4404950", "Newport city")},
    )


def test_compose_expands_a_wide_statewide_range_from_the_census():
    # IN/KY/MI/NJ/RI ship a single statewide Z row; RI writes its low ZIP with 4 digits.
    rates = sst.parse_rate_file("44,45,44,0.07000,0.07000,0.07000,0.07000,19830301,99991231\n")
    zs = [sst.ZipRow("2801", "2940", "", "", (), date(2024, 4, 1), sst.OPEN_END)]
    out = sst.compose("RI", rates, zs, ri_census(), ON)
    # 01001 is out of state, 02999 is above the range, 02888 has no centroid
    assert [r.zip for r in out] == ["02801", "02840", "02940"]
    assert all(r.state_rate == D("0.07") and r.local_rate == D(0) for r in out)
    assert out[0].label == "Newport County, RI"


def test_compose_drops_the_food_rate_when_it_equals_the_general_rate():
    # The live files repeat the general rate in the food/drug columns wherever the state
    # has no reduced grocery rate, so the composed food rate is not a food rate at all.
    text = (FX / "sst_wa_rate.csv").read_text().replace(
        "53,00,051,0.01200,0.01200,0.00000,0.00000",
        "53,00,051,0.01200,0.01200,0.01200,0.01200",
    )
    rates = sst.parse_rate_file(text)
    zs = sst.parse_boundary_zips((FX / "sst_wa_boundary.csv").read_text())
    out = {r.zip: r for r in sst.compose("WA", rates, zs, census(), ON)}
    assert out["83856"].general_rate == D("0.077")
    assert out["83856"].food_drug_rate is None
    assert out["98001"].food_drug_rate == D("0.065")  # still a genuinely reduced rate


def test_compose_drops_zips_whose_census_county_is_in_another_state():
    # A narrow Z row can still cross a county/state line even though it is short; the
    # census's own state membership must gate every candidate, not just the wide path.
    c = census()
    c.centroids["83857"] = (48.2, -117.0)
    c.county["83857"] = ("16021", "Bonner County")  # Idaho, not WA
    rates = sst.parse_rate_file((FX / "sst_wa_rate.csv").read_text())
    zs = [sst.ZipRow("83856", "83857", "", "", (), date(2024, 4, 1), sst.OPEN_END)]
    out = {r.zip for r in sst.compose("WA", rates, zs, c, ON)}
    assert out == {"83856"}


def test_compose_skips_zips_without_a_current_row_or_centroid():
    rates = sst.parse_rate_file((FX / "sst_wa_rate.csv").read_text())
    zs = sst.parse_boundary_zips((FX / "sst_wa_boundary.csv").read_text())
    c = census()
    c.centroids.pop("83856")
    out = {r.zip for r in sst.compose("WA", rates, zs, c, ON)}
    assert out == {"98001", "98002"}


def test_unpack_handles_zip_and_plain_csv():
    import io
    import zipfile

    buf = io.BytesIO()
    row = "53,45,53,0.065,0.065,0.065,0.065,19830301,99991231\n"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("WAR2026Q4AUG27.csv", row)
    assert sst.unpack(buf.getvalue(), "x.zip").startswith("53,45")
    assert sst.unpack(row.encode(), "x.csv").startswith("53,45")
    # IA ships its state-level row first, behind a UTF-8 BOM
    assert sst.unpack(b"\xef\xbb\xbf" + row.encode(), "x.csv").startswith("53,45")


def test_unpack_picks_the_first_csv_member_and_rejects_an_archive_without_one():
    import io
    import zipfile

    row = "53,45,53,0.065,0.065,0.065,0.065,19830301,99991231\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("WAR2026Q4AUG27/", "")  # directory entry, not a file
        z.writestr("WAR2026Q4AUG27/readme.txt", "not the data")
        z.writestr("WAR2026Q4AUG27/WAR2026Q4AUG27.CSV", row)
    assert sst.unpack(buf.getvalue(), "x.zip").startswith("53,45")

    no_csv = io.BytesIO()
    with zipfile.ZipFile(no_csv, "w") as z:
        z.writestr("readme.txt", "nothing here")
    try:
        sst.unpack(no_csv.getvalue(), "x.zip")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "readme.txt" in str(e)


def test_sst_adapter_rows_requests_flat_urls_and_yields_wa_rows(monkeypatch):
    import io
    import zipfile

    index_html = (
        b"<html><body><pre>\n"
        b'<A HREF="/ratesandboundry/Rates/WAR2026Q4AUG27.csv">WAR2026Q4AUG27.csv</A>\n'
        b'<A HREF="/ratesandboundry/Boundary/WAB2026Q4AUG27.zip">WAB2026Q4AUG27.zip</A>\n'
        b"</pre></body></html>\n"
    )

    rate_bytes = (FX / "sst_wa_rate.csv").read_bytes()
    boundary_text = (FX / "sst_wa_boundary.csv").read_text()
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("WAB2026Q4AUG27.csv", boundary_text)
    boundary_zip_bytes = zbuf.getvalue()

    rate_url = "http://52.15.48.162/ratesandboundry/Rates/WAR2026Q4AUG27.csv"
    boundary_url = "http://52.15.48.162/ratesandboundry/Boundary/WAB2026Q4AUG27.zip"
    requested: list[str] = []

    def fake_get_cached(url, *, ttl_days: float = 1.0) -> bytes:
        requested.append(url)
        if url in (f"{sst.MIRROR}/Rates/", f"{sst.MIRROR}/Boundary/"):
            return index_html
        if url == rate_url:
            return rate_bytes
        if url == boundary_url:
            return boundary_zip_bytes
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(sst, "get_cached", fake_get_cached)
    adapter = sst.SstAdapter()
    monkeypatch.setattr(adapter, "states", ("WA",))

    rows = list(adapter.rows(census(), ON))

    # No doubled /ratesandboundry/ratesandboundry/... path.
    assert rate_url in requested
    assert boundary_url in requested
    assert rows and all(r.state == "WA" for r in rows)
