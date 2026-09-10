"""Every coordinate operation in this pipeline (spec §2.3).

mapshaper reads shapefiles, reprojects, overlays, simplifies and writes TopoJSON, so Python
never touches a coordinate except to draw Guam's rectangle and to package the result. That
is what keeps the repo free of shapely, pyproj and pyshp. The version is pinned so a build
run six months from now simplifies exactly the way the sizes in spec §2.1 were measured."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PINNED_VERSION = "0.6.121"


def command() -> list[str]:
    """The mapshaper invocation: the binary named by `MAPSHAPER` (CI installs it once with
    `npm i -g mapshaper@0.6.121`), else `npx --yes mapshaper@0.6.121`. Resolved through
    `shutil.which` because on Windows `npx` is `npx.cmd`, which `subprocess` will not find
    on its own."""
    name = os.environ.get("MAPSHAPER", "").strip()
    if name:
        found = shutil.which(name)
        if found is None:
            raise ValueError(f"mapshaper: MAPSHAPER={name!r} is not on PATH")
        return [found]
    found = shutil.which("npx")
    if found is None:
        raise ValueError(
            "mapshaper: 'npx' is not on PATH -- the bounds build needs Node 18+. Install "
            f"Node, or `npm i -g mapshaper@{PINNED_VERSION}` and set MAPSHAPER=mapshaper"
        )
    return [found, "--yes", f"mapshaper@{PINNED_VERSION}"]


def run(args: list[str]) -> None:
    cmd = command() + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ValueError(
            f"mapshaper exited {proc.returncode} for: {' '.join(cmd)}\n"
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )


def to_geojson(src: Path, dst: Path, *, proj_from: str | None = None) -> None:
    """Read anything mapshaper reads and write WGS84 GeoJSON. With no `proj_from`,
    `-proj wgs84` reprojects from the file's own `.prj` (Washington's State Plane feet);
    the CDTFA export declares no `.prj`, so it names EPSG:3857 explicitly. No `precision=`
    here: quantising before the overlay in Task 6 would open slivers along shared edges."""
    proj = ["-proj", "wgs84"] if proj_from is None else ["-proj", f"from={proj_from}", "wgs84"]
    run(["-i", str(src), *proj, "-o", str(dst), "format=geojson"])


def simplify(src: Path, dst: Path, pct: int = 10) -> None:
    """Simplify one source **on its own** (spec §2.3): `-simplify` ranks vertices across
    the whole dataset it is given, so one merged run would trade California's detail
    against TIGER's coastlines in a way nobody has measured. `keep-shapes` so no small city
    is simplified out of existence.

    The percentage and the flag are separate argv entries: mapshaper parses one `10%
    keep-shapes` string as the percentage itself and exits 1 with `Invalid percentage`
    (0.6.121, verified 2026-09-10) -- a shell would have split them, `subprocess` does
    not."""
    run(["-i", str(src), "-simplify", f"{pct}%", "keep-shapes", "-o", str(dst),
         "format=geojson"])


def union(srcs: list[Path], dst: Path, *, fields: list[str]) -> None:
    """Overlay every input layer: one output feature per distinct combination of the
    inputs' areas, so a municipality that straddles a county line becomes one piece per
    county and what is left of a county is its unincorporated territory."""
    run(["-i", *[str(s) for s in srcs], "combine-files", "-union", "target=*",
         "-filter-fields", ",".join(fields), "-o", str(dst), "format=geojson"])


def merge_to_topojson(srcs: list[Path], dst: Path, precision: str = "0.00001") -> None:
    """Merge the simplified sources into one topology. `precision=0.00001` is ~1.1 m of
    lon/lat quantisation, the setting every size in spec §2.1 was measured at.

    mapshaper prints `Ignoring precision=0.00001 -- this option only works with
    no-quantization` here and then honours it anyway: the written `transform.scale` is
    *approximately* the requested precision, while dropping the option falls back to
    mapshaper's own default grid, which is thousands of times coarser. Do not "fix" the
    warning away.

    Approximately, not exactly: mapshaper snaps the quantisation grid to the dataset's own
    extent, so what it writes is the requested step rounded to a whole number of grid cells
    across that extent. The two-triangle fixture happens to land on exactly
    `[1e-05, 1e-05]`; the real merged build writes
    `[9.999999783645851e-06, 9.999998584150907e-06]` (0.6.121, verified 2026-09-10). Any
    test of this belongs within a tolerance -- an exact-equality assertion would pass on the
    fixture and fail on the shipped file."""
    run(["-i", *[str(s) for s in srcs], "combine-files", "-merge-layers", "force",
         "-o", str(dst), "format=topojson", f"precision={precision}"])
