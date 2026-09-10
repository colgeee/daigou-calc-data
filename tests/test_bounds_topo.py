import json
from pathlib import Path

from pipeline.bounds.topo import decode_ring, polygons_from_topojson

FX = Path(__file__).parent / "fixtures"


def topo() -> dict:
    return json.loads((FX / "two_triangles.topojson").read_text(encoding="utf-8"))


def test_polygons_keep_every_ring_of_every_part_and_the_arcs_verbatim():
    t = topo()
    transform, arcs, polygons = polygons_from_topojson(t)
    assert transform == {"scale": [1e-05, 1e-05], "translate": [0, 0]}
    assert arcs is t["arcs"]                       # kept verbatim: arc sharing is the payload
    assert [p["id"] for p in polygons] == ["t:left", "t:right"]
    assert polygons[0] == {"id": "t:left", "source": "t", "profile": "T-0.05-0-LEFT, T",
                           "rings": [[0, 1]]}
    # A MultiPolygon flattens: every ring of every part in one list, which is what the app's
    # even-odd containment test wants (spec §2.3).
    assert polygons[1]["rings"] == [[2, -1]]


def test_decode_ring_walks_arcs_forwards_and_backwards_and_closes():
    t = topo()
    left = decode_ring(t, [0, 1])
    assert left == [(0.0, 2.0), (2.0, 0.0), (0.0, 0.0), (0.0, 2.0)]
    # `-1` is `~0`: arc 0 walked backwards. The joint vertex is not repeated mid-ring.
    right = decode_ring(t, [2, -1])
    assert right == [(0.0, 2.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)]
    for ring in (left, right):
        assert ring[0] == ring[-1] and len(ring) >= 4


def test_decode_ring_applies_the_transform():
    t = topo()
    t["transform"] = {"scale": [1e-05, 1e-05], "translate": [-118.0, 34.0]}
    assert decode_ring(t, [0, 1])[0] == (-118.0, 36.0)
    assert decode_ring(t, [0, 1])[1] == (-116.0, 34.0)


def test_decode_ring_de_duplicates_the_joint_between_arcs():
    """Arc 0 ends where arc 1 begins. A decoder that concatenated blindly would emit that
    vertex twice and hand the app's even-odd test a zero-length edge."""
    t = topo()
    assert len(decode_ring(t, [0, 1])) == 4        # 2 + 3 vertices, one joint dropped


def test_decode_ring_reads_an_untransformed_topology_as_absolute():
    """TopoJSON only delta-encodes when it is quantised. mapshaper always writes a
    transform for us, but a decoder that assumed one would silently mangle a topology
    written without `precision=`."""
    t = {"arcs": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    assert decode_ring(t, [0]) == [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]
