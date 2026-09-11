import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from pipeline.bounds import Collected, build, tiger
from pipeline.bounds.validate import KEYS

ON = date(2026, 9, 9)
FX = Path(__file__).parent / "fixtures"

RATES = {"schemaVersion": "1", "effectiveDate": "2026-07-01", "publishedAt": "x",
         "profiles": {}, "states": {"T": {"localCoverage": True, "rules": {}, "confidence": {}}},
         "zips": []}
PROFILES = {"T-0.05-0-LEFT, T": {"state": "T", "label": "Left, T", "stateRate": "0.05",
                                 "localRate": "0", "foodDrugRate": None,
                                 "groceryRate": None},
            "T-0.05-0-RIGHT, T": {"state": "T", "label": "Right, T", "stateRate": "0.05",
                                  "localRate": "0", "foodDrugRate": None,
                                  "groceryRate": None}}


def fake_source(name: str):
    """A source that hands the builder the two-triangle FeatureCollection under its own
    file name, so two of them in one build do not overwrite each other."""
    def collect(work, census, on):
        p = work / f"{name}.geojson"
        p.write_text((FX / "two_triangles.geojson").read_text(encoding="utf-8"), encoding="utf-8")
        return Collected(p, dict(PROFILES), {name: {"polygons": 2}})
    return collect


def stub_geometry(monkeypatch):
    """mapshaper is monkeypatched everywhere but Task 1's one integration test."""
    monkeypatch.setattr(build.mapshaper, "simplify",
                        lambda src, dst, pct=10: dst.write_text(
                            src.read_text(encoding="utf-8"), encoding="utf-8"))
    monkeypatch.setattr(build.mapshaper, "merge_to_topojson",
                        lambda srcs, dst, precision="0.00001": dst.write_text(
                            (FX / "two_triangles.topojson").read_text(encoding="utf-8"),
                            encoding="utf-8"))


def test_build_assembles_the_documented_shape(tmp_path, monkeypatch):
    stub_geometry(monkeypatch)
    monkeypatch.setattr(build, "SOURCES", {"t": fake_source("t")})
    doc = build.build(tmp_path, census=None, on=ON, sources=["t"])
    assert set(doc) == KEYS
    assert doc["schemaVersion"] == "1"
    assert doc["effectiveDate"] == "2026-07-01"          # the quarter the build sits in
    assert doc["transform"] == {"scale": [1e-05, 1e-05], "translate": [0, 0]}
    assert len(doc["arcs"]) == 3 and len(doc["polygons"]) == 2
    assert doc["polygons"][0]["rings"] == [[0, 1]]
    assert doc["profiles"] == PROFILES
    assert doc["sources"] == {"t": {"polygons": 2, "count": 2}}


def test_build_simplifies_each_source_on_its_own(tmp_path, monkeypatch):
    """`-simplify` ranks vertices across the whole dataset it is given, so one merged run
    would trade California's detail against TIGER's coastlines (spec §2.3). The merged
    topology carries one triangle per source here, so the `sources` block's pre-merge and
    post-merge counts are both pinned on a two-source build."""
    calls: list[int] = []
    topo = json.loads((FX / "two_triangles.topojson").read_text(encoding="utf-8"))
    for geom, name in zip(topo["objects"]["tri"]["geometries"], ("a", "b"), strict=True):
        geom["properties"]["source"] = name

    def one_triangle(name: str):
        def collect(work, census, on):
            p = work / f"{name}.geojson"
            p.write_text((FX / "two_triangles.geojson").read_text(encoding="utf-8"),
                         encoding="utf-8")
            return Collected(p, dict(PROFILES), {name: {"polygons": 1}})
        return collect

    monkeypatch.setattr(build.mapshaper, "simplify",
                        lambda src, dst, pct=10: calls.append(pct) or dst.write_text(
                            src.read_text(encoding="utf-8"), encoding="utf-8"))
    monkeypatch.setattr(build.mapshaper, "merge_to_topojson",
                        lambda srcs, dst, precision="0.00001": (
                            calls.append(len(srcs)),
                            dst.write_text(json.dumps(topo), encoding="utf-8")))
    monkeypatch.setattr(build, "SOURCES", {"a": one_triangle("a"), "b": one_triangle("b")})
    doc = build.build(tmp_path, census=None, on=ON, sources=["a", "b"])
    assert calls == [10, 10, 2]
    assert doc["sources"] == {"a": {"polygons": 1, "count": 1},
                              "b": {"polygons": 1, "count": 1}}


def test_main_refuses_without_a_rates_file(tmp_path, capsys):
    assert build.main(str(tmp_path), ["t"]) == 1
    out = capsys.readouterr().out
    assert "REFUSING TO WRITE" in out and "rates.json" in out and "pipeline rates" in out


def test_main_writes_both_files_atomically_and_prints_the_sizes(tmp_path, monkeypatch, capsys):
    stub_geometry(monkeypatch)
    monkeypatch.setattr(build, "SOURCES", {"t": fake_source("t")})
    monkeypatch.setattr(build, "MIN_BY_SOURCE", {"t": 1})
    monkeypatch.setattr(build, "_census", lambda: None)
    monkeypatch.setattr(build, "build_date", lambda: ON)
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "rates.json").write_text(json.dumps(RATES), encoding="utf-8")
    assert build.main(str(tmp_path), ["t"]) == 0
    doc = json.loads(gzip.decompress((v1 / "bounds.json.gz").read_bytes()).decode("utf-8"))
    assert len(doc["polygons"]) == 2
    assert not list(v1.glob("*.tmp"))
    assert b"\r\n" not in (v1 / "bounds.json").read_bytes()
    assert "[bounds] wrote" in capsys.readouterr().out


def test_assemble_refuses_when_the_merge_returns_a_different_number_of_polygons(
        tmp_path, monkeypatch):
    """The one step nothing else measures: `topo.polygons_from_topojson` skips any geometry
    that is not a Polygon or a MultiPolygon, so a feature lost in mapshaper or in the read
    back is invisible to every gate but the coarse floors."""
    stub_geometry(monkeypatch)

    def collect(work, census, on):
        p = work / "t.geojson"
        p.write_text((FX / "two_triangles.geojson").read_text(encoding="utf-8"),
                     encoding="utf-8")
        return Collected(p, dict(PROFILES), {"t": {"polygons": 3}})

    monkeypatch.setattr(build, "SOURCES", {"t": collect})
    with pytest.raises(ValueError, match="3 features went into the merge but 2 polygons"):
        build.build(tmp_path, census=None, on=ON, sources=["t"])


def test_main_prints_the_house_line_for_an_exception_that_is_not_a_value_error(
        tmp_path, monkeypatch, capsys):
    """A shifted CDTFA or DOR column raises KeyError, a truncated download raises
    BadZipFile; a traceback is the wrong answer at the moment a maintainer needs to be told
    the file was not written and why."""
    def collect(work, census, on):
        raise KeyError("Expiration Date")

    monkeypatch.setattr(build, "SOURCES", {"t": collect})
    monkeypatch.setattr(build, "_census", lambda: None)
    monkeypatch.setattr(build, "build_date", lambda: ON)
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "rates.json").write_text(json.dumps(RATES), encoding="utf-8")
    assert build.main(str(tmp_path), ["t"]) == 1
    out = capsys.readouterr().out
    assert "REFUSING TO WRITE — KeyError: 'Expiration Date'" in out
    assert not (v1 / "bounds.json.gz").exists()


def test_main_refuses_when_validation_fails(tmp_path, monkeypatch, capsys):
    stub_geometry(monkeypatch)
    monkeypatch.setattr(build, "SOURCES", {"t": fake_source("t")})
    monkeypatch.setattr(build, "MIN_BY_SOURCE", {"t": 500})
    monkeypatch.setattr(build, "_census", lambda: None)
    monkeypatch.setattr(build, "build_date", lambda: ON)
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "rates.json").write_text(json.dumps(RATES), encoding="utf-8")
    assert build.main(str(tmp_path), ["t"]) == 1
    assert "REFUSING TO WRITE" in capsys.readouterr().out
    assert not (v1 / "bounds.json.gz").exists()


def test_the_illinois_floor_is_most_of_the_live_layer_not_a_token():
    """The overlay is the build's most fragile step, and its floor was written from a spec
    estimate of ~292 pieces before the layer existed. The built layer is 1 392, so a token
    200 would have let a collapsed place-to-IDOR join publish Chicago at Cook County's rate
    and still pass. The floor is now three quarters of the real count."""
    assert build.MIN_BY_SOURCE["tiger-il"] == 1000


def test_every_tiger_layer_floor_is_at_least_its_own():
    """The layer's own `min_polygons` guards the source before the merge and
    `MIN_BY_SOURCE` guards it after; a merged floor below a layer floor would be dead
    weight, and every source name a layer produces has to have a floor at all."""
    for layer in tiger.LAYERS:
        assert build.MIN_BY_SOURCE[layer.source] >= layer.min_polygons


def test_the_illinois_layer_floor_agrees_with_the_merged_one():
    """Two different numbers for one layer is drift the next reader has to re-derive, and
    the pre-merge check -- with the better message -- was the one being skipped."""
    by_source = {layer.source: layer.min_polygons for layer in tiger.LAYERS}
    assert by_source["tiger-il"] == build.MIN_BY_SOURCE["tiger-il"] == 1000
