from pathlib import Path

from pipeline.census import (
    Census,
    county_short,
    display_name,
    join_key,
    normalize_place,
    parse_gazetteer,
    parse_relationship,
)

FX = Path(__file__).parent / "fixtures"


def test_parse_gazetteer_strips_padding():
    c = parse_gazetteer((FX / "gaz_slice.txt").read_text(encoding="utf-8"))
    assert c["90012"] == (34.0522, -118.2437)
    assert c["98001"][1] == -122.2643
    assert len(c) == 3


def test_relationship_picks_largest_overlap_and_skips_headerless_rows():
    county = parse_relationship((FX / "zcta_county_slice.txt").read_text(encoding="utf-8-sig"),
                                "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20")
    assert county["75201"] == ("48113", "Dallas County")
    assert county["98001"] == ("53033", "King County")
    assert "" not in county
    place = parse_relationship((FX / "zcta_place_slice.txt").read_text(encoding="utf-8-sig"),
                               "GEOID_PLACE_20", "NAMELSAD_PLACE_20")
    assert place["98001"] == ("5323515", "Federal Way city")


def test_normalizers():
    assert normalize_place("Dallas city") == "DALLAS"
    assert normalize_place("St. Louis city") == "ST. LOUIS"
    assert normalize_place("Hollywood CDP") == "HOLLYWOOD"
    assert normalize_place("  Federal   Way city ") == "FEDERAL WAY"
    assert normalize_place("Cañon City city") == "CAÑON CITY"
    assert county_short("Dallas County") == "DALLAS"
    assert county_short("Orleans Parish") == "ORLEANS"
    assert county_short("Fairfax city") == "FAIRFAX CITY"
    assert county_short("Fairfax County") == "FAIRFAX"


def test_join_key_folds_the_spellings_the_rate_files_disagree_on():
    """Shared by every adapter that joins a state rate file to Census names (F1)."""
    # Word spacing: the Comptroller's DE SOTO vs the gazetteer's DESOTO (75115).
    assert join_key("De Soto") == join_key("DeSoto") == "DESOTO"
    assert join_key("La Salle") == join_key("LaSalle") == "LASALLE"
    # A leading Saint, abbreviated on either side: ST. HEDWIG vs SAINT HEDWIG (78152),
    # and Illinois's SAINT CLAIR COUNTY row vs the Census ST. CLAIR county name.
    assert join_key("St. Hedwig") == join_key("ST HEDWIG") == join_key("Saint Hedwig")
    assert join_key("ST. CLAIR COUNTY") == join_key("SAINT CLAIR COUNTY") == "SAINTCLAIRCOUNTY"
    # Interior periods go too; a Saint elsewhere in the name is left where it is.
    assert join_key("Mt. Prospect") == "MTPROSPECT"
    assert join_key("  fort   worth ") == "FORTWORTH"
    # `ST` only expands as a whole leading word: STERLING and STAUNTON are not Saints.
    assert join_key("Sterling") == "STERLING"
    assert join_key("Staunton") == "STAUNTON"


def test_display_name_fixes_mc_prefixes_and_afb_but_leaves_the_rest_to_title_case():
    # Live labels that .title() alone mangles.
    assert display_name("MCINTOSH") == "McIntosh"
    assert display_name("MCPHERSON") == "McPherson"
    assert display_name("MCCONNELL AFB") == "McConnell AFB"
    assert display_name("MCKINNEY") == "McKinney"
    # The Mc fix also applies after a hyphen, inside a compound word.
    assert display_name("CANDLER-MCAFEE") == "Candler-McAfee"
    # Mac... is not Mc...; multi-word, apostrophe and hyphenated names are already
    # correct once .title() runs and must not be touched further.
    assert display_name("MACON") == "Macon"
    assert display_name("O'FALLON") == "O'Fallon"
    assert display_name("WILKES-BARRE") == "Wilkes-Barre"
    assert display_name("PEND OREILLE") == "Pend Oreille"
    assert display_name("DE KALB") == "De Kalb"


def test_census_object_from_fixture_slices():
    c = Census(
        centroids=parse_gazetteer((FX / "gaz_slice.txt").read_text(encoding="utf-8")),
        county=parse_relationship(
            (FX / "zcta_county_slice.txt").read_text(encoding="utf-8-sig"),
            "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20",
        ),
        place=parse_relationship(
            (FX / "zcta_place_slice.txt").read_text(encoding="utf-8-sig"),
            "GEOID_PLACE_20", "NAMELSAD_PLACE_20",
        ),
    )
    assert c.zips_in_state("48") == ["75201"]
    assert c.county_name("75201") == "DALLAS"
    assert c.place_name("75201") == "DALLAS"
    assert c.place_name("00000") is None
