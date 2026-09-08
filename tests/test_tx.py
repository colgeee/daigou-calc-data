from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.census import Census
from pipeline.sources import tx

FX = Path(__file__).parent / "fixtures"

HEADER = "1\t20263\t202609\t2026 - 3rd\t0.0625\t0\t3715\tnotice text"


@pytest.fixture(autouse=True)
def _low_min_rows(monkeypatch):
    """F3's row-count guard defaults to 3000; every test here builds a handful of rows
    inline, so lower it for all of them. Tests that exercise the guard itself override
    it back up (or down further) within their own body."""
    monkeypatch.setattr(tx, "MIN_DATA_ROWS", 1)


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


def test_parse_collapses_internal_whitespace():
    """M4: `parse` collapses runs of internal whitespace the way `census.normalize_place`
    does. `census.join_key` (F1) removes whitespace entirely for join purposes and so
    subsumes this for matching, but `parse`'s own output should still read cleanly on its
    own."""
    rows = tx.parse(tsv(row("Fort  Worth", "0.01", "Parker", "0.005")))
    assert rows[0].city == "FORT WORTH"


def test_parse_raises_when_too_few_rows(monkeypatch):
    """F3: an error page or a file whose rows silently dropped out must fail the build,
    not publish 6.25% statewide."""
    monkeypatch.setattr(tx, "MIN_DATA_ROWS", 3000)
    with pytest.raises(ValueError, match="only 2 data rows"):
        tx.parse(tsv(
            row("Dallas", "0.01", "Dallas", "0"),
            row("Acton", "0", "Hood", "0.005", spd1="0.005"),
        ))


def test_parse_raises_when_no_row_names_a_county():
    """F3: a reordered file could still parse >= MIN_DATA_ROWS "rows" while every county
    cell lands empty; that must fail too, not just a raw row-count shortfall."""
    with pytest.raises(ValueError, match="county"):
        tx.parse(tsv(row("Dallas", "0.01", "", "0")))


def test_parse_does_not_raise_once_min_rows_is_met():
    rows = tx.parse(tsv(
        row("Dallas", "0.01", "Dallas", "0"),
        row("Acton", "0", "Hood", "0.005", spd1="0.005"),
    ))
    assert len(rows) == 2


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
    # No place for Hood -> fallback to the county's highest filed row (D23): Acton's own
    # combined local is 0+0.005+0.005 = 0.01, the only row filed for Hood County.
    assert rows["76049"].local_rate == D("0.01")
    assert rows["76049"].label == "Hood County, TX"
    assert all(r.state == "TX" and r.state_rate == D("0.0625") and r.food_drug_rate is None
               for r in rows.values())


def test_rows_cap_the_local_rate_at_two_percent(monkeypatch):
    """Texas caps combined city + county + special-district tax at 2%. Easton (Rusk Co)
    composes to 3% across its columns and Fort Worth's Parker slice composes to 2.5%;
    both are collected at the ceiling, so every Texas ZIP tops out at 8.25%."""
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


def test_county_fallback_takes_the_highest_combined_local_across_any_row(monkeypatch):
    """D23: the fallback rate for a ZIP with no matching city row is the highest
    city+county+SPD combined local of ANY row filed for its county, capped at 2% -- not
    just the county column (often zeroed on a combined City/County row, e.g. real-world
    Austin/Hays and Corpus Christi/Kleberg Co) and not automatically the county-only
    row, when a city row in the county composes higher.
    Row A: 0.01+0+0.01 = 0.02. Row B: 0.005+0+0 = 0.005. County-only row: 0+0.005+0 =
    0.005. Highest is A's 0.02."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("Alpha", "0.01", "Bell", "0", spd2="0.01"),   # A: 0.02
        row("Beta", "0.005", "Bell", "0"),                 # B: 0.005
        row("Bell Co", "0", "Bell", "0.005"),               # county-only row: 0.005
    ))
    c = Census(
        centroids={"76500": (31.0, -97.4)},
        county={"76500": ("48027", "Bell County")},
        place={},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["76500"].local_rate == D("0.02")
    assert rows["76500"].label == "Bell County, TX"


def test_county_fallback_uses_the_countys_only_row_when_sub_cap(monkeypatch):
    """D23's second case: a county whose only filed row composes to 0+0.005+0.005 gives
    fallback ZIPs 1%, well under the cap."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("Coke Co", "0", "Coke", "0.005", spd1="0.005"),
    ))
    c = Census(
        centroids={"76905": (31.9, -100.4)},
        county={"76905": ("48153", "Coke County")},
        place={},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["76905"].local_rate == D("0.01")


def test_county_max_local_keys_every_county_named_even_at_zero():
    """Every county named anywhere in the file gets a key, one whose rows compose to a
    combined local of zero included, so the fallback can tell "0% local" from "the
    Comptroller files no row for this county" (a missing key)."""
    rates = tx._county_max_local(tx.parse(tsv(
        row("Somewhere", "0", "Zero", "0"),
        row("Dallas", "0.01", "Dallas", "0", spd2="0.01"),
    )))
    assert rates == {"ZERO": D("0"), "DALLAS": D("0.02")}


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


def test_labels_keep_the_census_casing(monkeypatch):
    """C1: the label is the Census name with its own casing, suffix stripped -- `DeSoto`,
    not the `Desoto` that re-casing the uppercased join name gives, and `McKinney` and
    `Dyess AFB` without needing `display_name`'s fixups at all. The county fallback reads
    the same way."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("McKinney", "0.02", "Collin", "0"),
        row("Dyess AFB", "0.02", "Taylor", "0"),
        row("De Soto", "0.02", "Dallas", "0"),
        row("Anywhere", "0.01", "DeWitt", "0.005"),
    ))
    c = Census(
        centroids={"75070": (33.2, -96.7), "79607": (32.4, -99.8), "75115": (32.6, -96.9),
                   "77954": (29.1, -97.1)},
        county={"75070": ("48085", "Collin County"), "79607": ("48441", "Taylor County"),
                "75115": ("48113", "Dallas County"), "77954": ("48123", "DeWitt County")},
        place={"75070": ("4845744", "McKinney city"), "79607": ("4821916", "Dyess AFB CDP"),
               "75115": ("4819972", "DeSoto city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["75070"].label == "McKinney, TX"
    assert rows["79607"].label == "Dyess AFB, TX"
    assert rows["75115"].label == "DeSoto, TX"
    assert rows["77954"].label == "DeWitt County, TX"


def test_place_join_tolerates_de_soto_spelling(monkeypatch):
    """F1: the Comptroller files it as two words, `DE SOTO`; the Census gazetteer's place
    name normalizes to the compact `DESOTO` (75115). Without a spelling-insensitive join
    this ZIP would silently fall back to the county rate (6.25%) instead of the city's
    8.25%."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(row("De Soto", "0.02", "Dallas", "0")))
    c = Census(
        centroids={"75115": (32.6, -96.9)},
        county={"75115": ("48113", "Dallas County")},
        place={"75115": ("4819972", "DeSoto city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["75115"].local_rate == D("0.02") and rows["75115"].general_rate == D("0.0825")


def test_place_join_tolerates_st_hedwig_spelling(monkeypatch):
    """F1: the Comptroller spells it out, `SAINT HEDWIG`; the Census gazetteer abbreviates
    to `St. Hedwig` (78152). Both must key to the same join."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(row("Saint Hedwig", "0.02", "Bexar", "0")))
    c = Census(
        centroids={"78152": (29.4, -98.1)},
        county={"78152": ("48029", "Bexar County")},
        place={"78152": ("4869752", "St. Hedwig city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["78152"].local_rate == D("0.02") and rows["78152"].general_rate == D("0.0825")


def test_place_join_disambiguates_by_the_zips_own_county(monkeypatch):
    """F2: Abilene is filed at different rates in Taylor and Jones counties. The
    (place, county) join key must pick the row for the ZIP's own Census county, not
    just any row named Abilene."""
    monkeypatch.setattr(tx, "_fetch_text", lambda: tsv(
        row("Abilene", "0.02", "Taylor", "0"),
        row("Abilene", "0.01", "Jones", "0"),
    ))
    c = Census(
        centroids={"79601": (32.45, -99.7), "79541": (32.75, -99.9)},
        county={"79601": ("48441", "Taylor County"), "79541": ("48253", "Jones County")},
        place={"79601": ("4801000", "Abilene city"), "79541": ("4801000", "Abilene city")},
    )
    rows = {r.zip: r for r in tx.TxAdapter().rows(c, date(2026, 9, 8))}
    assert rows["79601"].local_rate == D("0.02")
    assert rows["79541"].local_rate == D("0.01")


def test_adapter_is_registered():
    assert any(a.name == "tx" and a.states == ("TX",) for a in tx.REGISTRY)
