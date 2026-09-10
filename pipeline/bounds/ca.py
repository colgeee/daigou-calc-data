"""California's polygons: CDTFA's own tax-area layer, one feature per jurisdiction.

The same Hub item `sources/ca.py` reads as a CSV also publishes the geometry, so the
polygons and the ZIP rows come out of one table and carry one rate per jurisdiction. That
is what lets a polygon's profile id be byte-identical to the ZIP row's (spec §2.3): the
label is rebuilt the way the ZIP path builds it, and the id is minted by `model.profile_id`
on a `ZipRate`-shaped record rather than assembled by hand.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pipeline.bounds import Collected, mapshaper, profile_entry
from pipeline.census import Census, display_name
from pipeline.http import get_polled
from pipeline.model import ZipRate
from pipeline.sources.ca import STATE_RATE, UNINC
from pipeline.sources.ca import _start as csv_start

GEOJSON_URL = ("https://gis.data.ca.gov/api/download/v1/items/"
               "01883a79765a4afba132ba54da408d8b/geojson?layers=1")
# The live layer is 540 features -- the same 540 jurisdictions `sources/ca.py` parses out of
# the same item's attribute table. CDTFA has served a 200 with a well-formed but empty body
# before; a layer this far below the live count is that, not California.
MIN_POLYGONS = 500
MAX_DECIMALS = 5


def fetch() -> bytes:
    return get_polled(GEOJSON_URL, ttl_days=7)


def parse_rate(value: object) -> Decimal:
    """The GeoJSON export types `RATE` as a JSON number where the CSV types it as a string,
    so the value arrives as a `float`. `str` then `Decimal` recovers the exact printed
    rate -- `Decimal(0.1075)` would not -- which is what keeps `localRate` at `0.035` and
    the profile id identical to the ZIP row's."""
    d = Decimal(str(value))
    if -d.as_tuple().exponent > MAX_DECIMALS:
        raise ValueError(
            f"CDTFA GeoJSON: RATE {value!r} has more than five decimal places -- "
            f"CDTFA rates are multiples of 0.00125, so this is binary noise from the "
            f"export and validate's [0, 0.15] range check would let it through"
        )
    if d < STATE_RATE:
        raise ValueError(f"CDTFA GeoJSON: RATE {d} is below the state rate {STATE_RATE}")
    return d


def parse_start(value: str) -> date:
    """The export dates rows RFC 1123 (`Fri, 01 Apr 2022 07:00:00 GMT`) where the CSV uses
    `M/D/YYYY`; a Hub change back to the CSV form must not silently date every row to
    `date.min` and adopt a future quarter's rate."""
    s = (value or "").strip()
    if not s:
        return date.min
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %Z").date()
    except ValueError:
        return csv_start(s)


def label_for(county: str, city_proper: str, unincorporated: bool) -> str:
    """The label `sources/ca.py` gives the same jurisdiction, rebuilt without a ZIP: a
    matched city keeps CDTFA's own `City_Name_Proper` through `display_name` (which fixes
    `Mcfarland`), and unincorporated territory reads as the county the way the Census names
    it. California has no parishes, boroughs or independent cities, so
    `display_name(county) + " County"` is `Census.county_label` for every one of its 58."""
    if unincorporated:
        return f"{display_name(county)} County, CA"
    return f"{display_name(city_proper)}, CA"


def to_features(geojson: dict, on: date) -> tuple[list[dict], dict[str, dict]]:
    """One feature per jurisdiction, carrying `id`, `source` and the profile id its rate
    and label mint, plus those profiles.

    Rows are keyed `(COUNTY, CITY)` exactly as `sources/ca.py` keys them -- the county-wide
    unincorporated row as `(COUNTY, "")` -- and within a key the row with the latest
    `START_DATE` at or before `on` wins, so CDTFA's pre-published next-quarter rate is not
    adopted until the build date reaches it."""
    best: dict[tuple[str, str], tuple[date, dict]] = {}
    future = 0
    for feature in geojson.get("features") or []:
        props = feature.get("properties") or {}
        county = str(props["County_name"]).strip().upper()
        city = str(props["City_name"]).strip().upper()
        juris = str(props["JURIS_NAME"]).strip().upper()
        start = parse_start(props.get("START_DATE") or "")
        if start > on:
            future += 1
            continue
        key = (county, "") if city.startswith(UNINC) or juris.startswith(UNINC) else (county, city)
        current = best.get(key)
        if current is not None and start < current[0]:
            continue
        best[key] = (start, feature)

    feats: list[dict] = []
    profiles: dict[str, dict] = {}
    seen: set[str] = set()
    for (county, city), (_start, feature) in best.items():
        props = feature["properties"]
        juris = str(props["JURIS_NAME"]).strip().upper()
        rate = parse_rate(props["RATE"])
        label = label_for(county, str(props.get("City_Name_Proper") or "").strip(), city == "")
        pid, entry = profile_entry(
            ZipRate("", "CA", STATE_RATE, rate - STATE_RATE, None, label)
        )
        profiles.setdefault(pid, entry)
        fid = f"cdtfa:{juris}"
        if fid in seen:
            raise ValueError(
                f"CDTFA GeoJSON: two tax areas share the id {fid!r} -- an id must name one "
                f"polygon, or the overlay in Task 6 cannot say which rate an area carries"
            )
        seen.add(fid)
        feats.append({
            "type": "Feature",
            "properties": {"id": fid, "source": "cdtfa", "profile": pid},
            "geometry": feature["geometry"],
        })

    if len(feats) < MIN_POLYGONS:
        raise ValueError(
            f"CDTFA GeoJSON: only {len(feats)} polygons parsed, expected at least "
            f"{MIN_POLYGONS} of the live 540 -- the download may be an empty or error "
            f"response, or its columns may have shifted"
        )
    print(
        f"[bounds:ca] {len(feats)} polygons, {len(profiles)} jurisdictions, "
        f"{future} future rows skipped"
    )
    return feats, profiles


def collect(work: Path, census: Census, on: date) -> Collected:
    """Fetch the layer, keep the current row per jurisdiction and reproject to WGS84.

    The export declares Web Mercator metres (`EPSG:3857`) and ships no `.prj`, so the
    projection is named to mapshaper rather than read off the file. `census` is unused:
    a polygon carries no ZIP, so its label is rebuilt from CDTFA's own columns, and the
    argument is here so every source's `collect` is called the same way."""
    doc = json.loads(fetch().decode("utf-8-sig"))
    feats, profiles = to_features(doc, on)
    raw = work / "cdtfa_raw.geojson"
    raw.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    dst = work / "cdtfa.geojson"
    mapshaper.to_geojson(raw, dst, proj_from="EPSG:3857")
    return Collected(dst, profiles, {"cdtfa": {"url": GEOJSON_URL, "polygons": len(feats)}})
