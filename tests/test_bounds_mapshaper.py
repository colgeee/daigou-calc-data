import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.bounds import mapshaper
from pipeline.bounds.topo import polygons_from_topojson

FX = Path(__file__).parent / "fixtures"


def test_command_prefers_the_env_binary_and_pins_the_npx_version(monkeypatch):
    monkeypatch.setenv("MAPSHAPER", "mapshaper")
    monkeypatch.setattr(mapshaper.shutil, "which", lambda n: f"/usr/bin/{n}")
    assert mapshaper.command() == ["/usr/bin/mapshaper"]
    monkeypatch.delenv("MAPSHAPER")
    assert mapshaper.command() == ["/usr/bin/npx", "--yes", "mapshaper@0.6.121"]


def test_command_raises_a_named_error_when_the_binary_is_missing(monkeypatch):
    monkeypatch.delenv("MAPSHAPER", raising=False)
    monkeypatch.setattr(mapshaper.shutil, "which", lambda n: None)
    with pytest.raises(ValueError, match="npx.*not on PATH"):
        mapshaper.command()


def test_run_raises_with_the_exit_code_and_stderr(monkeypatch):
    monkeypatch.setattr(mapshaper, "command", lambda: ["ms"])
    monkeypatch.setattr(
        mapshaper.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Error: no such layer"))
    # `(?s)`: the message puts the command and the captured stderr on separate lines.
    with pytest.raises(ValueError, match=r"(?s)exited 1.*no such layer"):
        mapshaper.run(["-i", "x.json"])


def test_helpers_build_the_documented_argument_lists(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(mapshaper, "run", seen.append)
    mapshaper.to_geojson(Path("a.shp"), Path("b.json"))
    mapshaper.to_geojson(Path("c.json"), Path("d.json"), proj_from="EPSG:3857")
    mapshaper.simplify(Path("d.json"), Path("e.json"), pct=10)
    mapshaper.union([Path("cty.json"), Path("plc.json")], Path("u.json"),
                    fields=["CGEOID", "PGEOID"])
    mapshaper.merge_to_topojson([Path("e.json"), Path("u.json")], Path("t.topojson"))
    assert seen[0] == ["-i", "a.shp", "-proj", "wgs84", "-o", "b.json", "format=geojson"]
    assert seen[1][2:5] == ["-proj", "from=EPSG:3857", "wgs84"]
    # `keep-shapes` so no small city is simplified out of existence (spec §2.1).
    assert seen[2] == ["-i", "d.json", "-simplify", "10% keep-shapes",
                       "-o", "e.json", "format=geojson"]
    assert seen[3] == ["-i", "cty.json", "plc.json", "combine-files", "-union", "target=*",
                       "-filter-fields", "CGEOID,PGEOID", "-o", "u.json", "format=geojson"]
    assert seen[4] == ["-i", "e.json", "u.json", "combine-files", "-merge-layers", "force",
                       "-o", "t.topojson", "format=topojson", "precision=0.00001"]


@pytest.mark.skipif(shutil.which("npx") is None,
                    reason="mapshaper runs through npx; Node is not installed here")
def test_real_mapshaper_writes_a_shared_arc_topology(tmp_path):
    """The one test that runs the real binary (spec §4). Two triangles that share an edge
    must come back as a topology whose shared boundary is a single arc -- that sharing is
    why California is 352 KB rather than a megabyte."""
    dst = tmp_path / "t.topojson"
    mapshaper.merge_to_topojson([FX / "two_triangles.geojson"], dst)
    topo = json.loads(dst.read_text(encoding="utf-8"))
    transform, arcs, polygons = polygons_from_topojson(topo)
    assert transform is not None and len(polygons) == 2
    assert len(arcs) == 3, "the shared edge must be one arc, not two"
    assert {p["profile"] for p in polygons} == {"T-0.05-0-LEFT, T", "T-0.05-0-RIGHT, T"}
