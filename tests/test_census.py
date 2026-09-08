from pathlib import Path

from pipeline.census import (
    Census,
    county_short,
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
