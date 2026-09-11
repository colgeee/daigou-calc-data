import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.bounds import ca
from pipeline.census import Census

FX = Path(__file__).parent / "fixtures"
ON = date(2026, 9, 9)


@pytest.fixture(autouse=True)
def _low_floor(monkeypatch):
    monkeypatch.setattr(ca, "MIN_POLYGONS", 1)


def geojson() -> dict:
    return json.loads((FX / "cdtfa_slice.geojson").read_text(encoding="utf-8"))


def drop_noise(doc: dict) -> dict:
    doc["features"] = [f for f in doc["features"] if f["properties"]["JURIS_NAME"] != "NOISE"]
    return doc


def test_parse_rate_reads_the_json_number_as_an_exact_decimal():
    """The GeoJSON export types RATE as a JSON number, not the CSV's string. `float` ->
    `str` -> `Decimal` keeps 0.1075 exact, so `localRate` is 0.035 and the profile id is
    byte-identical to the ZIP row's."""
    assert ca.parse_rate(0.1075) == D("0.1075")
    assert ca.parse_rate(0.1075) - ca.STATE_RATE == D("0.035")


def test_parse_rate_refuses_more_than_five_decimal_places():
    """CDTFA rates are multiples of 0.00125, so anything longer is float noise from the
    export -- and `validate`'s [0, 0.15] range check would let it straight through."""
    with pytest.raises(ValueError, match="five decimal places"):
        ca.parse_rate(0.107500000000001)


def test_parse_rate_refuses_a_rate_below_the_state_rate():
    with pytest.raises(ValueError, match="below the state rate"):
        ca.parse_rate(0.06)


def test_parse_start_reads_rfc_1123_and_falls_back_to_the_csv_form():
    assert ca.parse_start("Fri, 01 Apr 2022 07:00:00 GMT") == date(2022, 4, 1)
    assert ca.parse_start("4/1/2019 7:00:00 AM") == date(2019, 4, 1)
    assert ca.parse_start("") == date.min


def test_to_features_keeps_the_current_row_per_jurisdiction_and_mints_profiles():
    feats, profiles = ca.to_features(drop_noise(geojson()), ON)
    ids = {f["properties"]["id"] for f in feats}
    assert ids == {"cdtfa:SANTA MONICA", "cdtfa:UNINCORPORATED AREA-LOS ANGELES",
                   "cdtfa:MCFARLAND"}
    assert all(f["properties"]["source"] == "cdtfa" for f in feats)
    by_id = {f["properties"]["id"]: f["properties"]["profile"] for f in feats}
    # The 2031 duplicate is not adopted until the build date reaches it.
    assert by_id["cdtfa:SANTA MONICA"] == "CA-0.0725-0.035-SANTA MONICA, CA"
    assert profiles["CA-0.0725-0.035-SANTA MONICA, CA"] == {
        "state": "CA", "label": "Santa Monica, CA", "stateRate": "0.0725",
        "localRate": "0.035", "foodDrugRate": None, "groceryRate": None}
    # `display_name` fixes the one CDTFA name that still needs it.
    assert profiles[by_id["cdtfa:MCFARLAND"]]["label"] == "McFarland, CA"
    later, _ = ca.to_features(drop_noise(geojson()), date(2031, 6, 1))
    assert {f["properties"]["profile"] for f in later
            if f["properties"]["id"] == "cdtfa:SANTA MONICA"} == {
        "CA-0.0725-0.04-SANTA MONICA, CA"}


def test_the_unincorporated_label_is_the_zip_paths_county_label():
    """A polygon carries no ZIP, so the county label has to be rebuilt from the county
    name. For California -- no parishes, no boroughs, no independent cities -- that is
    exactly what `Census.county_label` answers for a ZIP in the same county, which is what
    makes a polygon and a ZIP row share one profile."""
    _feats, profiles = ca.to_features(drop_noise(geojson()), ON)
    assert any(p["label"] == "Los Angeles County, CA" for p in profiles.values())
    c = Census(centroids={"91001": (34.19, -118.13)},
               county={"91001": ("06037", "Los Angeles County")}, place={})
    assert ca.label_for("LOS ANGELES", "Unincorporated", True) == \
        f"{c.county_label('91001')}, CA"
    for namelsad in ("San Luis Obispo County", "Del Norte County", "Contra Costa County"):
        short = namelsad.removesuffix(" County").upper()
        assert ca.label_for(short, "", True) == f"{namelsad}, CA"


def test_to_features_raises_below_the_polygon_floor(monkeypatch):
    monkeypatch.setattr(ca, "MIN_POLYGONS", 500)
    with pytest.raises(ValueError, match="only 3 polygons"):
        ca.to_features(drop_noise(geojson()), ON)


def test_collect_reprojects_from_web_mercator(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "fetch", lambda: json.dumps(drop_noise(geojson())).encode())
    seen: list[tuple] = []
    monkeypatch.setattr(ca.mapshaper, "to_geojson",
                        lambda src, dst, *, proj_from=None: seen.append((src, dst, proj_from))
                        or dst.write_text("{}", encoding="utf-8"))
    out = ca.collect(tmp_path, Census(centroids={}, county={}, place={}), ON)
    assert seen[0][2] == "EPSG:3857"       # the export declares Web Mercator metres
    assert out.notes["cdtfa"]["url"] == ca.GEOJSON_URL
    assert out.notes["cdtfa"]["polygons"] == 3
