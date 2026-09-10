import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from pipeline.bounds import tiger
from pipeline.model import ZipRate

FX = Path(__file__).parent / "fixtures"
IL = tiger.Layer("tiger-il", "IL", "17", tiger.IL_COUNTIES, True, 1)
NV = tiger.Layer("tiger-nv", "NV", "32", None, False, 1)


def table() -> dict:
    return {
        "17031": ZipRate("", "IL", D("0.0625"), D("0.0375"), D("0.01"), "Cook County, IL"),
        ("1714000", "17031"): ZipRate("", "IL", D("0.0625"), D("0.04"), D("0.01"), "Chicago, IL"),
        ("1714000", "17043"): ZipRate("", "IL", D("0.0625"), D("0.0225"), D("0.01"), "Chicago, IL"),
    }


def test_the_illinois_layer_is_the_six_metro_counties():
    assert tiger.IL_COUNTIES == ("17031", "17043", "17089", "17097", "17111", "17197")
    assert {layer.source for layer in tiger.LAYERS} == {
        "tiger-il", "tiger-ny", "tiger-nv", "tiger-hi"}
    assert [layer.overlay_places for layer in tiger.LAYERS] == [True, True, False, False]


def test_plan_filters_by_geoid_renames_the_id_fields_and_unions_only_rated_places(
        tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(tiger.mapshaper, "run", seen.append)
    monkeypatch.setattr(tiger, "shapefile", lambda url, work, name: Path(f"{name}.shp"))
    tiger.plan(tmp_path, IL, {"1714000"})
    joined = " ".join(" ".join(a) for a in seen)
    # The place layer has no COUNTYFP, so both layers rename GEOID before the union or the
    # two fields would collide and the county id would be lost.
    assert "CGEOID=GEOID" in joined and "PGEOID=GEOID" in joined
    assert '"17031"' in joined and '"17197"' in joined
    assert '"1714000"' in joined
    assert "-union" in joined
    # Nevada and Hawaii are the county polygons as they are: no place layer, no union.
    seen.clear()
    tiger.plan(tmp_path, NV, set())
    assert "-union" not in " ".join(" ".join(a) for a in seen)


def test_js_list_refuses_a_value_that_is_not_a_geoid():
    """`_js_list` is interpolated into a JavaScript expression mapshaper evaluates, and
    every value in it comes from a downloaded Census file, so anything but digits is
    refused rather than executed."""
    assert tiger._js_list({"17031", "17043"}) == '["17031","17043"]'
    with pytest.raises(ValueError, match="not a numeric GEOID"):
        tiger._js_list({'17031"] || true || ["'})


def test_to_features_joins_place_pieces_by_place_and_county_and_remainders_by_county():
    doc = json.loads((FX / "tiger_union_slice.geojson").read_text(encoding="utf-8"))
    feats, profiles, _fell_back = tiger.to_features(doc, IL, table())
    by_id = {f["properties"]["id"]: f["properties"]["profile"] for f in feats}
    assert by_id["tiger-il:1714000-17031"] == "IL-0.0625-0.04-CHICAGO, IL-FD0.01"
    # What is left of a county is its unincorporated territory, at the county's rate.
    assert by_id["tiger-il:17031"] == "IL-0.0625-0.0375-COOK COUNTY, IL-FD0.01"
    # The DuPage sliver of Chicago is a second polygon at DuPage's rate, not Cook's.
    assert by_id["tiger-il:1714000-17043"] == "IL-0.0625-0.0225-CHICAGO, IL-FD0.01"
    assert all(f["properties"]["source"] == "tiger-il" for f in feats)
    assert len(profiles) == 3


def test_a_piece_whose_place_has_no_row_in_that_county_falls_back_to_the_county():
    doc = json.loads((FX / "tiger_union_slice.geojson").read_text(encoding="utf-8"))
    t = table()
    del t[("1714000", "17043")]
    t["17043"] = ZipRate("", "IL", D("0.0625"), D("0.0125"), D("0.01"), "DuPage County, IL")
    feats, _, fell_back = tiger.to_features(doc, IL, t)
    by_id = {f["properties"]["id"]: f["properties"]["profile"] for f in feats}
    assert by_id["tiger-il:17043"] == "IL-0.0625-0.0125-DUPAGE COUNTY, IL-FD0.01"
    # The fallback is counted: a place-to-rate join that quietly stopped matching keeps
    # every polygon and every floor green and only makes each municipality cheaper, so the
    # count is the one number that would show it (`sources.<layer>.fell_back`).
    assert fell_back == 1
    assert tiger.to_features(doc, IL, table())[2] == 0


def test_to_features_drops_a_piece_with_no_row_at_all_and_floors_the_count(monkeypatch):
    doc = json.loads((FX / "tiger_union_slice.geojson").read_text(encoding="utf-8"))
    feats, _, _fell_back = tiger.to_features(doc, IL, {"17031": table()["17031"]})
    assert len(feats) == 2                     # the DuPage piece has nothing to price
    with pytest.raises(ValueError, match="only 2 polygons"):
        tiger.to_features(doc, tiger.Layer("tiger-il", "IL", "17", tiger.IL_COUNTIES, True, 300),
                          {"17031": table()["17031"]})
