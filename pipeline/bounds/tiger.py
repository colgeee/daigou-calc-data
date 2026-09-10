"""Census TIGER county and place polygons for the states whose rates key on them.

Four states are drawn from the Census's own 2024 boundary files rather than from a state
agency's layer, because the state prices by county (and, in two of them, by municipality)
and publishes no geometry of its own (spec §2.3):

* **IL** and **NY** overlay TIGER places onto TIGER counties. mapshaper's `-union` cuts one
  piece per (place, county) pair plus what is left of each county, so a municipality that
  straddles a county line becomes one polygon per county at that county's own combined rate
  -- Chicago is 10.5 % in Cook and 8.5 % in its DuPage sliver -- and a county's remainder is
  its unincorporated territory at the county rate. A place with no rate row in the state's
  table is never overlaid: it is unincorporated, or one of IDOR's ~200 address-override
  jurisdictions, and the county rate is what the ZIP path already answers for it.
* **NV** and **HI** are the county polygons as they are: Nevada levies no city sales tax and
  Hawaii no sub-county tax, so a county boundary is exact for both.

Every rate here comes from the adapter's own `jurisdictions(census, on)` table, keyed by
TIGER GEOID, so a polygon's profile id is byte-identical to the ZIP row's for the same
jurisdiction (spec §2.3, decision #71). Nothing in this module touches a coordinate:
mapshaper filters, renames, unions and reprojects, and Python only reads the ids back off
the GeoJSON it hands over.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pipeline.bounds import Collected, mapshaper, profile_entry
from pipeline.census import Census
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import JurisdictionKey, il, ny, sst, yaml_states

TIGER = "https://www2.census.gov/geo/tiger/TIGER2024"
COUNTY_URL = f"{TIGER}/COUNTY/tl_2024_us_county.zip"      # 80 MB, cached 30 days
TTL_DAYS = 30
# Illinois is limited to the six Chicago-metro counties (decision #71): the rest of the
# state stays on the ZIP path, and the whole-state place layer is 435 KB gzipped for
# jurisdictions no 代購 shopper stands in.
IL_COUNTIES = ("17031", "17043", "17089", "17097", "17111", "17197")


@dataclass(frozen=True)
class Layer:
    source: str
    state: str
    fips: str
    counties: tuple[str, ...] | None      # None = every county in the state
    overlay_places: bool                  # False = the county polygons as they are
    min_polygons: int


LAYERS = (
    Layer("tiger-il", "IL", "17", IL_COUNTIES, True, 200),
    Layer("tiger-ny", "NY", "36", None, True, 60),
    # Nevada has no city sales tax and Hawaii no sub-county tax, so their counties are exact.
    Layer("tiger-nv", "NV", "32", None, False, 15),
    Layer("tiger-hi", "HI", "15", None, False, 5),
)


def place_url(fips: str) -> str:
    return f"{TIGER}/PLACE/tl_2024_{fips}_place.zip"


def _js_list(values: Iterable[str]) -> str:
    return "[" + ",".join(f'"{v}"' for v in sorted(values)) + "]"


def shapefile(url: str, work: Path, name: str) -> Path:
    """Download a TIGER zip and unpack it under `work/<name>/`, returning its `.shp`.

    The sidecars have to land beside it: mapshaper reads the `.dbf` for the attributes this
    module filters on and the `.prj` for the projection (TIGER ships NAD83, which `-proj
    wgs84` shifts by a metre or two).

    An extraction already sitting in `work/<name>/` is reused. The national county file is
    80 MB and all four layers cut from it, so the name is shared and it is unpacked once a
    build rather than four times."""
    dest = work / name
    already = sorted(p for p in dest.rglob("*") if p.suffix.lower() == ".shp") \
        if dest.is_dir() else []
    if len(already) == 1:
        return already[0]
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(get_cached(url, ttl_days=TTL_DAYS))) as z:
        z.extractall(dest)
    shps = sorted(p for p in dest.rglob("*") if p.suffix.lower() == ".shp")
    if len(shps) != 1:
        raise ValueError(
            f"TIGER zip {url}: expected exactly one .shp, found {len(shps)} "
            f"({[p.name for p in shps]})"
        )
    return shps[0]


def plan(work: Path, layer: Layer, wanted_places: set[str]) -> list[Path]:
    """Run one layer's mapshaper chain and return the GeoJSON files it wrote.

    Both inputs are filtered down before anything is overlaid -- the county file is the
    whole 3 234-county national layer -- and both rename `GEOID` first: the place file
    carries no `COUNTYFP`, so an unrenamed union would collide the two `GEOID` fields and
    lose the county id that prices the piece."""
    county_shp = shapefile(COUNTY_URL, work, "tiger_county")
    counties = work / f"{layer.source}_counties.geojson"
    county_filter = (f'{_js_list(layer.counties)}.indexOf(GEOID) > -1'
                     if layer.counties else f'STATEFP === "{layer.fips}"')
    mapshaper.run(["-i", str(county_shp), "-filter", county_filter,
                   "-rename-fields", "CGEOID=GEOID", "-filter-fields", "CGEOID",
                   "-proj", "wgs84", "-o", str(counties), "format=geojson"])
    if not layer.overlay_places:
        return [counties]
    place_shp = shapefile(place_url(layer.fips), work, f"tiger_{layer.fips}_place")
    places = work / f"{layer.source}_places.geojson"
    mapshaper.run(["-i", str(place_shp), "-filter",
                   f"{_js_list(wanted_places)}.indexOf(GEOID) > -1",
                   "-rename-fields", "PGEOID=GEOID", "-filter-fields", "PGEOID",
                   "-proj", "wgs84", "-o", str(places), "format=geojson"])
    unioned = work / f"{layer.source}_union.geojson"
    mapshaper.union([counties, places], unioned, fields=["CGEOID", "PGEOID"])
    return [unioned]


def to_features(geojson: dict, layer: Layer,
                table: dict[JurisdictionKey, ZipRate]) -> tuple[list[dict], dict[str, dict]]:
    """One feature per priced piece, carrying `id`, `source` and its profile id.

    A piece inside a place takes `table[(place, county)]`; a piece with no place -- the
    county's unincorporated remainder -- takes `table[county]`, and so does a place piece
    the state files no row for in *that* county. A piece with neither is dropped: there is
    no rate to give it, and the ZIP path underneath still answers for the address."""
    feats: list[dict] = []
    profiles: dict[str, dict] = {}
    seen: dict[str, int] = {}
    dropped = 0
    for feature in geojson.get("features") or []:
        props = feature.get("properties") or {}
        cgeoid = str(props.get("CGEOID") or "").strip()
        pgeoid = str(props.get("PGEOID") or "").strip()
        row = table.get((pgeoid, cgeoid)) if pgeoid else None
        fid = f"{layer.source}:{pgeoid}-{cgeoid}"
        if row is None:
            row = table.get(cgeoid)
            fid = f"{layer.source}:{cgeoid}"
        if row is None:
            dropped += 1
            continue
        pid, entry = profile_entry(row)
        profiles.setdefault(pid, entry)
        # A place piece that falls back to its county's rate carries the county's id, and so
        # does that county's own remainder, so the two can collide. Both are real, distinct
        # areas at one rate; the second one on is numbered rather than silently shadowing
        # the first, because an id has to name one polygon.
        n = seen.get(fid, 0) + 1
        seen[fid] = n
        feats.append({
            "type": "Feature",
            "properties": {"id": fid if n == 1 else f"{fid}-{n}",
                           "source": layer.source, "profile": pid},
            "geometry": feature.get("geometry"),
        })

    if len(feats) < layer.min_polygons:
        raise ValueError(
            f"{layer.source}: only {len(feats)} polygons, expected at least "
            f"{layer.min_polygons} -- the TIGER download or the rate join may have shifted"
        )
    print(f"[bounds:{layer.source}] {len(feats)} polygons, {len(profiles)} jurisdictions, "
          f"{dropped} unpriced pieces dropped")
    return feats, profiles


def _county_of(key: JurisdictionKey) -> str:
    return key[1] if isinstance(key, tuple) else key


def tables(census: Census, on: date) -> dict[str, dict[JurisdictionKey, ZipRate]]:
    """Every bounds state's jurisdiction table, one `jurisdictions` call per adapter.

    The adapters are asked once each and not once per layer: `sst` and `yaml_states` each
    fetch and parse a rate file to answer, and Illinois's is a 200-character fixed-width
    download."""
    out: dict[str, dict[JurisdictionKey, ZipRate]] = {}
    for adapter in (il.IlAdapter(), ny.NyAdapter(), sst.SstAdapter(),
                    yaml_states.YamlStatesAdapter()):
        table = adapter.jurisdictions(census, on)
        for st in adapter.bounds_states:
            out[st] = {k: v for k, v in table.items() if v.state == st}
    return out


def collect(work: Path, census: Census, on: date) -> Collected:
    """Draw all four TIGER layers into one GeoJSON, with one `notes` entry per layer."""
    by_state = tables(census, on)
    feats: list[dict] = []
    profiles: dict[str, dict] = {}
    notes: dict[str, dict] = {}
    for layer in LAYERS:
        full = by_state.get(layer.state)
        if full is None:
            raise ValueError(
                f"{layer.source}: no adapter answered {layer.state}'s jurisdictions -- the "
                f"layer has geometry but nothing to price it with"
            )
        if layer.counties is not None:
            table = {k: v for k, v in full.items() if _county_of(k) in set(layer.counties)}
        else:
            table = {k: v for k, v in full.items() if _county_of(k).startswith(layer.fips)}
        wanted = {k[0] for k in table if isinstance(k, tuple)}
        n = 0
        for path in plan(work, layer, wanted):
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            layer_feats, layer_profiles = to_features(doc, layer, table)
            feats.extend(layer_feats)
            profiles.update(layer_profiles)
            n += len(layer_feats)
        notes[layer.source] = {
            "state": layer.state,
            "polygons": n,
            "url": place_url(layer.fips) if layer.overlay_places else COUNTY_URL,
        }
    dst = work / "tiger.geojson"
    dst.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    return Collected(dst, profiles, notes)
