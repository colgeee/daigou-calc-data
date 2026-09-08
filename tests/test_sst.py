from datetime import date
from decimal import Decimal as D
from pathlib import Path

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
    assert out["83856"].local_rate == D("0.012") and out["83856"].label == "Pend Oreille, WA"
    # 98002 has two current rows: county-only (0.012) and
    # place+district (0.021+0.035) -> highest wins
    assert out["98002"].local_rate == D("0.056")


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
