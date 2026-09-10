"""Washington's polygons: the DOR's own sales-tax jurisdiction boundary layer, joined to
the DOR's quarterly rate table by location code.

Washington prices by rate area, not by ZIP, and the DOR publishes the two halves of that
picture as separate quarterly downloads: a shapefile of every rate area's boundary, keyed
by its four-digit location code, and a CSV of every rate area's state and local rate, keyed
by the same code. Neither carries the other's payload, so this module scrapes both download
pages, picks the right quarter of each, and joins them on the code.

Two rules the DOR's publishing habits force (spec §2.3):

* The boundary file is picked as the **newest at or before** the build's quarter -- a
  quarter with no boundary change publishes no file, and the previous one still applies.
* The rate file must be the build's quarter **exactly** -- an older one would publish last
  quarter's numbers under this quarter's `effectiveDate`.

Neither URL is derivable: the path carries the Drupal upload month (`/2026-05/` for a 26Q3
file) and sometimes a `_0` dedupe suffix, so the pages are scraped rather than synthesised.

Rate areas are labelled in the DOR's own namespace (decision #71): `SEATTLE` and
`KING COUNTY NON-RTA` are the DOR's names for its own areas, and re-deriving them from the
Census would rename a rate area after a place whose boundary is not the same shape.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin

from pipeline.bounds import Collected, mapshaper, profile_entry
from pipeline.census import Census, display_name
from pipeline.http import get_cached
from pipeline.model import ZipRate

BASE = "https://dor.wa.gov"
BOUNDARIES_URL = f"{BASE}/taxes-rates/sales-tax-jurisdiction-boundaries"
RATES_URL = f"{BASE}/taxes-rates/sales-and-use-tax-rates/downloadable-database"
# The live boundary layer is 403 polygons and the live rate table 403 rows, one per rate
# area; the two code sets are equal as sets. A floor under either is a truncated download or
# a page whose table moved.
MIN_POLYGONS = 380
MIN_RATE_ROWS = 380
# A code with no rate row is a polygon nobody can price. One or two is a DOR bookkeeping
# lag; a tenth of the state is a broken join, and every King County ZIP would silently fall
# back to the centroid path.
MAX_DROP_SHARE = 0.03
_LOCCODE = re.compile(r'href="([^"]*LOCCODE_Public_(\d{2})Q(\d)(?:_\d+)?\.zip)"', re.I)
_RATES = re.compile(r'href="([^"]*Rates_(\d{2})Q(\d)(?:_\d+)?\.zip)"', re.I)


def pick_quarter(html: str, pattern: re.Pattern, on: date, *, what: str,
                 exact: bool = False) -> tuple[str, str]:
    """The download URL for `on`'s quarter and the quarter it names, e.g. `"26Q3"`."""
    want = (on.year % 100, (on.month - 1) // 3 + 1)
    found = {(int(yy), int(qq)): href for href, yy, qq in pattern.findall(html)}
    if exact:
        href = found.get(want)
        if href is None:
            raise ValueError(
                f"DOR page: no {what} file for {want[0]:02d}Q{want[1]} -- the rates must be "
                f"the quarter the build stamps, and the page lists {sorted(found)}"
            )
        return urljoin(BASE, href), f"{want[0]:02d}Q{want[1]}"
    older = sorted(k for k in found if k <= want)
    if not older:
        raise ValueError(
            f"DOR page: no {what} file at or before {want[0]:02d}Q{want[1]} -- the download "
            f"table may have moved or its filenames changed"
        )
    key = older[-1]
    return urljoin(BASE, found[key]), f"{key[0]:02d}Q{key[1]}"


def parse_rates(text: str) -> dict[str, tuple[Decimal, Decimal, str]]:
    """Location code -> (state rate, local rate, the DOR's own name for the area).

    `Local` is in the file, but `Rate - State` is the number that adds back to the total the
    DOR prints, and it carries the RTA and other add-on components a bare `Local` omits."""
    rows = csv.DictReader(io.StringIO(text))
    out: dict[str, tuple[Decimal, Decimal, str]] = {}
    for row in rows:
        code = str(row["Code"]).strip().zfill(4)
        state = Decimal(str(row["State"]).strip())
        local = Decimal(str(row["Rate"]).strip()) - state
        out[code] = (state, local, str(row["Name"]).strip())
    if len(out) < MIN_RATE_ROWS:
        raise ValueError(
            f"DOR rates: only {len(out)} rate rows parsed, expected at least "
            f"{MIN_RATE_ROWS} of the live 403 -- the download may be truncated or its "
            f"columns may have shifted"
        )
    return out


def to_features(geojson: dict, rates: dict[str, tuple[Decimal, Decimal, str]],
                quarter: str) -> tuple[list[dict], dict[str, dict]]:
    """One feature per priced rate area, carrying `id`, `source` and its profile id.

    A polygon whose code has no rate row is dropped -- there is no rate to give it -- but
    only a handful may be, or the join itself is broken."""
    feats: list[dict] = []
    profiles: dict[str, dict] = {}
    dropped: list[str] = []
    total = 0
    for feature in geojson.get("features") or []:
        total += 1
        props = feature.get("properties") or {}
        code = str(props.get("LOCCODE") or "").strip().zfill(4)
        row = rates.get(code)
        if row is None:
            dropped.append(code)
            continue
        state, local, name = row
        label = f"{display_name(name)}, WA"
        pid, entry = profile_entry(ZipRate("", "WA", state, local, None, label))
        profiles.setdefault(pid, entry)
        feats.append({
            "type": "Feature",
            "properties": {"id": f"wa-dor:{code}", "source": "wa-dor", "profile": pid},
            "geometry": feature.get("geometry"),
        })

    share = len(dropped) / total if total else 0.0
    # `MAX_DROP_SHARE` of the layer, but never fewer than one: a single stale code is the
    # DOR bookkeeping lag the constant's comment describes, and on a three-polygon test
    # slice a bare share test would call it a broken join.
    if len(dropped) > max(1.0, MAX_DROP_SHARE * total):
        raise ValueError(
            f"WA boundaries {quarter}: {len(dropped)} of {total} polygons ({share:.1%}) "
            f"have no rate row, over the {MAX_DROP_SHARE:.0%} limit: "
            f"{sorted(dropped)[:10]}"
        )
    if len(feats) < MIN_POLYGONS:
        raise ValueError(
            f"WA boundaries {quarter}: only {len(feats)} polygons priced, expected at "
            f"least {MIN_POLYGONS}"
        )
    print(
        f"[bounds:wa] {len(feats)} polygons, {len(profiles)} rate areas, "
        f"{len(dropped)} unpriced ({quarter})"
    )
    return feats, profiles


def _extract_shapefile(data: bytes, dest: Path) -> Path:
    """Unpack the boundary zip into `dest` and return its single `.shp`, sidecars beside it
    (mapshaper reads the `.prj` and `.dbf` off disk, so the whole set has to land)."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    shps = sorted(p for p in dest.rglob("*") if p.suffix.lower() == ".shp")
    if len(shps) != 1:
        raise ValueError(
            f"DOR boundary zip: expected exactly one .shp, found {len(shps)} "
            f"({[p.name for p in shps]})"
        )
    return shps[0]


def _rates_text(data: bytes) -> str:
    """The single CSV inside the rates zip, decoded."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".csv", ".txt")) and not n.endswith("/")]
        if len(names) != 1:
            raise ValueError(
                f"DOR rates zip: expected exactly one CSV, found {len(names)} ({names})"
            )
        return z.read(names[0]).decode("utf-8-sig", errors="replace")


def collect(work: Path, census: Census, on: date) -> Collected:
    """Scrape both DOR pages, download the quarter's boundary and rate files, and join.

    `census` is unused: a rate area carries no ZIP and the DOR names its own areas
    (decision #71); the argument is here so every source's `collect` is called alike.
    mapshaper reads the shapefile's `.prj` (State Plane WA-South, feet), so no `proj_from`
    is named here the way `bounds/ca.py` has to name one."""
    boundary_url, quarter = pick_quarter(
        get_cached(BOUNDARIES_URL).decode("utf-8", errors="replace"), _LOCCODE, on,
        what="boundaries")
    rates_url, rates_quarter = pick_quarter(
        get_cached(RATES_URL).decode("utf-8", errors="replace"), _RATES, on,
        what="rates", exact=True)
    print(f"[bounds:wa] boundaries {quarter}, rates {rates_quarter}")

    rates = parse_rates(_rates_text(get_cached(rates_url, ttl_days=30)))
    shp = _extract_shapefile(get_cached(boundary_url, ttl_days=30), work / "wa_shp")
    raw = work / "wa_raw.geojson"
    mapshaper.to_geojson(shp, raw)
    doc = json.loads(raw.read_text(encoding="utf-8-sig"))

    feats, profiles = to_features(doc, rates, quarter)
    dst = work / "wa.geojson"
    dst.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8"
    )
    return Collected(dst, profiles, {"wa-dor": {
        "file": Path(boundary_url).name,
        "rates": Path(rates_url).name,
        "polygons": len(feats),
    }})
