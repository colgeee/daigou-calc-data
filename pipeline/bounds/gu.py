"""Guam's polygon: a rectangle in the ocean around the island (spec §2.2/§2.6).

Guam is one taxing authority with no consumer sales tax -- the Business Privilege Tax is
levied on the seller and embedded in the marked price -- so there is no internal boundary
to be wrong about and no rate to get wrong. A box drawn around the island answers "am I in
Guam?" exactly, which is the only question the overlay has to answer here (decision #68).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from pipeline.bounds import Collected, profile_entry
from pipeline.census import Census
from pipeline.model import ZipRate
from pipeline.sources.yaml_states import RULES_DIR, load_state, published_rates

SOURCE = "gu"


def rectangle(bounds: tuple[float, float, float, float]) -> dict:
    """Guam's polygon is a rectangle in the ocean around the island: one taxing authority,
    no consumer sales tax, so the box is exact and there is no boundary to be wrong about
    (spec §2.2). Counter-clockwise, first vertex repeated last, as GeoJSON wants."""
    lat_min, lat_max, lon_min, lon_max = bounds
    ring = [[lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
            [lon_min, lat_max], [lon_min, lat_min]]
    return {"type": "Polygon", "coordinates": [ring]}


def collect(work: Path, census: Census, on: date) -> Collected:
    """The one Guam feature, priced from `gu.yaml` the way its ZIP rows are.

    `census` and `on` are unused -- the territory is one jurisdiction whose rate is a
    hand-maintained constant -- and are here so every source's `collect` is called alike."""
    table = load_state(RULES_DIR / "gu.yaml")
    if table.bounds is None:
        raise ValueError("gu.yaml: no `bounds` rectangle -- the Guam polygon cannot be drawn")
    state_rate, local_rate = published_rates(table, Decimal("0"))
    pid, entry = profile_entry(ZipRate("", table.state, state_rate, local_rate,
                                       table.food_drug_rate, table.label or "Guam"))
    doc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"id": "gu:territory", "source": SOURCE, "profile": pid},
        "geometry": rectangle(table.bounds),
    }]}
    # No mapshaper: the rectangle is already WGS84, and there is nothing to reproject or
    # simplify. `build` simplifies it anyway, harmlessly -- four vertices survive
    # `keep-shapes`.
    path = work / "gu.geojson"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return Collected(path, {pid: entry},
                     {SOURCE: {"bounds": list(table.bounds), "polygons": 1}})
