"""Turn mapshaper's TopoJSON into the shipped document, and decode a ring for validation.

`transform` and `arcs` are kept exactly as mapshaper writes them -- quantised integers,
delta-encoded after the first vertex -- because arc sharing is what makes California 352 KB
rather than a megabyte (spec §2.1/§2.3). A ring is a list of arc indices where a negative
index `-1-i` means arc `i` walked backwards."""
from __future__ import annotations


def _arc_points(topo: dict, i: int) -> list[tuple[float, float]]:
    arc = topo["arcs"][i]
    if "transform" not in topo:
        # TopoJSON only delta-encodes a quantised topology; an unquantised one is absolute.
        return [(p[0], p[1]) for p in arc]
    x = y = 0
    out = []
    for dx, dy in arc:
        x += dx
        y += dy
        out.append((x, y))
    return out


def decode_ring(topo: dict, ring: list[int]) -> list[tuple[float, float]]:
    """Absolute `(lon, lat)` for one ring, joints de-duplicated and the transform applied.
    Used by `validate_bounds` to prove every ring closes; the app has its own decoder."""
    pts: list[tuple[float, float]] = []
    for idx in ring:
        i = idx if idx >= 0 else -1 - idx
        seg = _arc_points(topo, i)
        if idx < 0:
            seg = list(reversed(seg))
        if pts and pts[-1] == seg[0]:
            seg = seg[1:]
        pts.extend(seg)
    t = topo.get("transform")
    if t is None:
        return [(float(x), float(y)) for x, y in pts]
    (sx, sy), (tx, ty) = t["scale"], t["translate"]
    return [(x * sx + tx, y * sy + ty) for x, y in pts]


def polygons_from_topojson(topo: dict) -> tuple[dict | None, list, list[dict]]:
    """`(transform, arcs, polygons)`. Each polygon is
    `{"id", "source", "profile", "rings"}`, `rings` listing **every** ring of every part of
    a MultiPolygon in one flat list -- the app tests containment with the even-odd rule
    over all of them, which handles holes and multi-part features without knowing which is
    which (spec §2.3)."""
    polygons: list[dict] = []
    for layer in topo.get("objects", {}).values():
        geoms = layer["geometries"] if layer.get("type") == "GeometryCollection" else [layer]
        for geom in geoms:
            kind = geom.get("type")
            if kind == "Polygon":
                parts = [geom["arcs"]]
            elif kind == "MultiPolygon":
                parts = geom["arcs"]
            else:
                continue
            props = geom.get("properties") or {}
            polygons.append({
                "id": props["id"], "source": props["source"], "profile": props["profile"],
                "rings": [list(ring) for part in parts for ring in part],
            })
    return topo.get("transform"), topo.get("arcs", []), polygons
