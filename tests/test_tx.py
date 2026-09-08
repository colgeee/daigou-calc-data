from datetime import date
from decimal import Decimal as D
from pathlib import Path

from pipeline.census import Census
from pipeline.sources import tx

FX = Path(__file__).parent / "fixtures"

HEADER = "1\t20263\t202609\t2026 - 3rd\t0.0625\t0\t3715\tnotice text"


def tsv(*rows: str) -> str:
    """The Comptroller ships the file CRLF-terminated behind its header/notice line."""
    return "\r\n".join((HEADER, *rows)) + "\r\n"


def row(city: str, city_rate: str, county: str, county_rate: str,
        spd1: str = "0", spd2: str = "0") -> str:
    return "\t".join((f"{city} ", "2000000", city_rate, f"{county} ", "4000000", county_rate,
                      "Spd 1 ", "5000000", spd1, "Spd 2 ", "3000000", spd2))


def test_parse_skips_header_and_sums_spds():
    rows = tx.parse((FX / "tx_taxrates_slice.txt").read_text())
    assert len(rows) == 5
    d = next(r for r in rows if r.city == "DALLAS")
    assert (d.county, d.city_rate, d.county_rate, d.spd_rate) == (
        "DALLAS", D("0.01"), D("0"), D("0.01"))
    a = next(r for r in rows if r.city == "ACTON")
    assert (a.county_rate, a.spd_rate) == (D("0.005"), D("0.005"))


def test_parse_skips_every_numeric_preamble_line():
    """The live file follows its notice line with 16 filing-due-date rows that carry a
    period number where a jurisdiction name belongs; all of them must drop out."""
    due = "\r\n".join(f"20260{n}\tm\t{n}/20/2026" + "\t" * 9 for n in range(1, 10))
    rows = tx.parse(tsv(due, row("Dallas", "0.01", "Dallas", "0")))
    assert [r.city for r in rows] == ["DALLAS"]


def test_parse_reads_blank_and_na_cells_as_zero():
    rows = tx.parse(tsv("Riviera \tn/a\t0\tKleberg \t4137006\t0.005\tn/a\tn/a\tn/a\tn/a\tn/a\t"))
    assert (rows[0].city_rate, rows[0].county_rate, rows[0].spd_rate) == (
        D("0"), D("0.005"), D("0"))


def test_rows_place_match_then_county_fallback(monkeypatch):
    monkeypatch.setattr(tx, "_fetch_text", lambda: (FX / "tx_taxrates_slice.txt").read_text())
    c = Census(
        centroids={"75201": (32.78, -96.8), "76049": (32.4, -97.7), "79601": (32.45, -99.7)},
        county={"75201": ("48113", "Dallas County"), "76049": ("48221", "Hood County"),
                "79601": ("48441", "Taylor County")},
        place={"75201": ("4819000", "Dallas city"), "79601": ("4801000", "Abilene city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["75201"].local_rate == D("0.02") and rows["75201"].label == "Dallas, TX"
    assert rows["79601"].local_rate == D("0.02")                       # Abilene/Taylor row
    assert rows["76049"].local_rate == D("0.005")   # no place -> county rate only
    assert rows["76049"].label == "Hood County, TX"
    assert all(r.state == "TX" and r.state_rate == D("0.0625") and r.food_drug_rate is None
               for r in rows.values())


def test_rows_cap_the_local_rate_at_two_percent(monkeypatch):
    """Texas caps combined city + county + special-district tax at 2%. Easton (Rusk Co)
    composes to 3% across its columns and Fort Worth's Parker/Wise slices to 2.5%; both
    are collected at the ceiling, so every Texas ZIP tops out at 8.25%."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("Easton", "0.01", "Rusk", "0", spd1="0.01", spd2="0.01"),
        row("Fort Worth", "0.01", "Parker", "0.005", spd1="0.005", spd2="0.005"),
    ))
    c = Census(
        centroids={"75641": (32.3, -94.6), "76008": (32.6, -97.6)},
        county={"75641": ("48401", "Rusk County"), "76008": ("48367", "Parker County")},
        place={"75641": ("4822276", "Easton city"), "76008": ("4827000", "Fort Worth city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["75641"].local_rate == D("0.02") and rows["75641"].general_rate == D("0.0825")
    assert rows["76008"].local_rate == D("0.02") and rows["76008"].general_rate == D("0.0825")


def test_county_fallback_takes_the_highest_rate_filed_for_the_county(monkeypatch):
    """Where a city straddles a county line the Comptroller files a combined `City/County`
    row that zeroes the county column, because the city's overlay already fills the 2%
    cap. Reading the first row seen would leave Hays and Kleberg counties untaxed."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("Austin/Hays", "0.01", "Hays", "0", spd2="0.01"),   # sorts first in the file
        row("Buda", "0.015", "Hays", "0.005"),
    ))
    c = Census(
        centroids={"78620": (30.2, -98.1)},
        county={"78620": ("48209", "Hays County")},
        place={},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["78620"].local_rate == D("0.005")
    assert rows["78620"].label == "Hays County, TX"


def test_county_rates_key_a_county_that_levies_nothing():
    """Dallas County charges no county tax. It still has to be a key, or the fallback
    cannot tell "no county tax" from "the Comptroller files no row for this county"."""
    rates = tx._county_rates(tx.parse(tsv(
        row("Austin/Hays", "0.01", "Hays", "0", spd2="0.01"),
        row("Buda", "0.015", "Hays", "0.005"),
        row("Dallas", "0.01", "Dallas", "0", spd2="0.01"),
    )))
    assert rates == {"HAYS": D("0.005"), "DALLAS": D("0")}


def test_rows_keep_a_zip_whose_county_has_no_row_at_zero_local(monkeypatch):
    """Five counties (Glasscock, Kenedy, King, Loving, McMullen) levy nothing and so file
    no row at all; their ZIPs are still Texas ZIPs at the bare 6.25% state rate."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(row("Dallas", "0.01", "Dallas", "0")))
    c = Census(
        centroids={"79754": (31.8, -103.5)},
        county={"79754": ("48301", "Loving County")},
        place={},
    )
    rows = list(tx.TxAdapter().rows(c, date(2026, 9, 8)))
    assert [(r.zip, r.local_rate, r.label) for r in rows] == [
        ("79754", D("0"), "Loving County, TX")]


def test_rows_skip_zips_outside_texas_or_without_a_centroid(monkeypatch):
    monkeypatch.setattr(tx, "_fetch_text", lambda: (FX / "tx_taxrates_slice.txt").read_text())
    c = Census(
        centroids={"75201": (32.78, -96.8)},
        county={"75201": ("48113", "Dallas County"), "76049": ("48221", "Hood County"),
                "73301": ("40109", "Oklahoma County")},
        place={},
    )
    assert [r.zip for r in tx.TxAdapter().rows(c, date(2026, 9, 8))] == ["75201"]


def test_labels_use_the_shared_display_name_fixups(monkeypatch):
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("McKinney", "0.02", "Collin", "0"),
        row("Dyess AFB", "0.02", "Taylor", "0"),
    ))
    c = Census(
        centroids={"75070": (33.2, -96.7), "79607": (32.4, -99.8)},
        county={"75070": ("48085", "Collin County"), "79607": ("48441", "Taylor County")},
        place={"75070": ("4845744", "McKinney city"), "79607": ("4821916", "Dyess AFB CDP")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["75070"].label == "McKinney, TX"
    assert rows["79607"].label == "Dyess AFB, TX"


def test_adapter_is_registered():
    assert any(a.name == "tx" and a.states == ("TX",) for a in tx.REGISTRY)
