# daigou-calc-data

Data pipeline for 代購算盤 Daigou Calc: it compiles US sales-tax rates by ZIP code from
official state and federal sources and publishes them as static JSON files (`rates.json.gz`,
plus the jurisdiction polygon overlay `bounds.json.gz`) via GitHub Pages. It is rebuilt
quarterly, since rates are effective from calendar-quarter starts.

## Published files

| File | URL | Rebuilt |
| --- | --- | --- |
| Rates | <https://colgeee.github.io/daigou-calc-data/v1/rates.json.gz> | quarterly — 2 Jan / 2 Apr / 2 Jul / 2 Oct, 06:00 UTC (`.github/workflows/rates.yml`) |
| Boundaries | <https://colgeee.github.io/daigou-calc-data/v1/bounds.json.gz> | quarterly — the same run, the same commit |

The files are served from the `gh-pages` branch, which `scripts/publish.sh` rewrites after a
successful build. Both land in **one commit**: the overlay is validated against the rates
file built beside it, and a client must never fetch a new `bounds.json.gz` against an old
`rates.json.gz`, so `publish.sh` refuses to publish unless it has all four files. GitHub
Pages returns an `ETag`; the app sends it back as `If-None-Match`, so an unchanged file costs
a 304 and no download. Uncompressed `v1/rates.json` and `v1/bounds.json` sit beside the gzips
for debugging.

## Boundaries

`v1/bounds.json.gz` places a phone *inside the taxing jurisdiction it is standing in*, where
the rates file can only place it by the centroid of its ZIP. That is the difference between
quoting Santa Monica's 10.75 % and Los Angeles's 9.75 % on a split ZIP. It is a second file
rather than a bigger rates file: geometry changes rarely and outweighs the rates, so folding
it in would turn every quarterly rate change into a multi-megabyte download for every phone.

The file is **self-contained**. Geometry is TopoJSON exactly as mapshaper writes it — a
quantising `transform`, shared `arcs`, and each ring a list of arc indices — and the document
carries its **own `profiles` map** in the rates file's shape (`state`, `label`, `stateRate`,
`localRate`, `foodDrugRate`), so a polygon never points at a rates-file profile that a
differently-dated rates file might not have. The only thing it borrows from the rates file is
the two-letter state code, looked up in that file's `states` block for the category rules; a
code the loaded rates file does not know makes the polygon a miss, and the ZIP path answers
instead. A polygon's profile id is minted the same way a ZIP row's is, so both sides of a
jurisdiction share one id.

Where the polygons come from:

| State | Source | Granularity |
| --- | --- | --- |
| CA | CDTFA tax-rate-area polygons — the GeoJSON export of the same ArcGIS item the rates build reads as CSV (540 areas) | city / unincorporated county, exact to the tax area |
| WA | DOR sales-tax jurisdiction boundaries (`LOCCODE_PUBLIC_<YY>Q<N>.zip`, 403 polygons) joined to the DOR quarterly rate table on the four-character location code | one rate area per polygon, RTA and PTBA districts included |
| IL | TIGER 2024 places overlaid on TIGER 2024 counties, **for the six Chicago-metro counties only** (Cook, DuPage, Kane, Lake, McHenry, Will), joined to the IDOR table by name | municipality-in-county, and each county's unincorporated remainder |
| NY | TIGER 2024 counties (62) with the places that have their own Publication 718 row overlaid on them | county / separately-taxed city |
| NV | TIGER 2024 counties (17, Carson City included) | county — Nevada levies no city sales tax |
| HI | TIGER 2024 counties (5, Kalawao included) | county — Hawaii levies no sub-county tax |
| GU | a hand-written bounding box in `pipeline/rules/states/gu.yaml` | territory — one taxing authority, so a rectangle is exact |

`python -m pipeline bounds` **validates before it writes**: every polygon's profile must
exist, every rate must sit in [0, 0.15], every Illinois profile must carry a food/drug rate,
every arc index must be in range and every ring closed, per-source polygon counts must clear
their floors, the `effectiveDate` must equal the rates file's, and the gzipped file must be
**at most 1.5 MB** (the current build is ~620 KB). Any failure prints *REFUSING TO WRITE*,
exits 1, and `publish.sh` never runs.

**Building it needs Node.** Every coordinate operation — reading shapefiles, reprojecting,
overlaying, simplifying, writing TopoJSON — is done by
[mapshaper](https://github.com/mbloch/mapshaper), pinned to **0.6.121**, which is what keeps
this repo free of shapely, pyproj and pyshp (`requirements.txt` is unchanged). So the bounds
build needs **Node ≥ 18** and **`mapshaper@0.6.121`** — install it once, which is what CI
does and what keeps the build offline and pinned:

```bash
npm i -g mapshaper@0.6.121
export MAPSHAPER=mapshaper
```

or leave `MAPSHAPER` unset and let the pipeline shell out to `npx --yes mapshaper@0.6.121`
(same pinned version, fetched on first use), or point `MAPSHAPER` at a binary of your own.
Nothing in the app or its tests needs Node; this is a build-time dependency of this repo
alone.

## Exchange rates

Exchange rates are no longer published here — the app fetches them itself from
<https://api.exchangerate.fun/> (refreshed hourly, no API key) and lets the user override
the rate for the selected currency. The daily FX workflow that used to build `v1/fx.json`
is gone.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash; use .venv\Scripts\activate on cmd/PowerShell
pip install -r requirements.txt
npm i -g mapshaper@0.6.121     # the bounds build only; Node ≥ 18
python -m pipeline rates --states WA
```

A full build is the three commands CI runs, in this order — `bounds` is validated against the
`rates.json` that `rates` just wrote, and `diff-rates` compares the new table against the one
currently published and fails on a swing that looks like a broken source rather than a rate
change:

```bash
python -m pipeline rates
python -m pipeline bounds
python -m pipeline diff-rates --out out --published https://colgeee.github.io/daigou-calc-data/v1/rates.json.gz
```

## Coverage

Every state, DC and Guam is published — 33 627 ZIPs in the current build, and the validator
refuses to publish a full build under 25 000. 42 states, DC and Guam carry a complete rate;
CO, LA, AL, AK (permanently) and SC, MO, AZ, NM (until Phase 2) carry the state rate only and
are flagged so the app asks for the local rate.

A flagged state still gets a row per Census ZIP, at its state rate with a zero local rate
(decision #26); the app reads the `localCoverage: false` flag and shows "unsupported area —
set your own rate". Publishing no rows at all would leave it unable to place the ZIP, so the
same phone would see "couldn't get a location" instead.

Polygons are published for **California, Washington, the six Chicago-metro counties of
Illinois, New York, Nevada, Hawaii and Guam** (see [Boundaries](#boundaries)), so a GPS fix
inside one of them is priced by the jurisdiction it stands in. Everything else — Texas, the
rest of Illinois, every other state — stays on the ZIP path, and so does any fix that lands
outside every polygon.

## Known limitations

- The Streamlined boundary files are ZIP-level, so sub-ZIP local taxes are missed: Vermont's
  1 % local option, West Virginia's 1 % municipal taxes and most Kansas districts are not
  captured.
- Texas ZIPs that match no city take their county's highest filed combined local rate.
- Illinois jurisdictions whose "rate varies" flag is set (Metro-East business districts and
  home-rule slivers) are published at their low rate, the one every other address in the
  jurisdiction pays; municipalities IDOR taxes by address are dropped, so those ZIPs fall
  back to their county rate.
- California and Texas ZIPs that match no incorporated place fall back to the county's
  unincorporated rate.
- New York cities without their own Pub 718 row take the county rate.
- Florida's $5 000 cap on the single-item discretionary surtax is not modelled.
- Streamlined food and drug rates are published only where a state actually has a reduced
  rate for them.
- Illinois's polygons stop at the six Chicago-metro counties; the rest of the state is on the
  ZIP path. Inside those counties, the address-override ("rate varies") jurisdictions have no
  polygon either — a place with no rate row is never overlaid, so a fix there resolves to its
  county, which is the rate the ZIP path already answers.
- A place the Census ZCTA relationship files never name has no polygon and falls to its
  county.
- Seams between separately sourced layers (a CDTFA edge against a TIGER one) can differ by
  tens of metres, so a fix can land in a gap or an overlap between two of them; the app's
  accuracy sweep absorbs that by answering the dearer of the neighbours it finds.
- Hawaii publishes the **maximum pass-on rate** — 4.712 % on Oahu, Hawaii, Kauai and Maui,
  4.167 % in Kalawao. The GET is levied on the seller and may be passed on at that grossed-up
  figure, which is what a Honolulu receipt adds; a retailer who passes on less is over-quoted.
- Guam is published at 0 %. Its 4.5 % Business Privilege Tax is levied on the seller and is
  already inside the marked price, so nothing is added at the register and quoting it would
  over-quote every Guam order.

## Sources

- Census ZCTA centroids: <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip>
- Census ZCTA→county: <https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt>
- Census ZCTA→place: <https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_place20_natl.txt>
- Streamlined Sales Tax mirror (24 states) — rates: <http://52.15.48.162/ratesandboundry/Rates/>
- Streamlined Sales Tax mirror (24 states) — boundaries: <http://52.15.48.162/ratesandboundry/Boundary/>
- California: <https://gis.data.ca.gov/api/download/v1/items/01883a79765a4afba132ba54da408d8b/csv?layers=1>
- California polygons (CDTFA tax-rate areas, GeoJSON export of the same item): <https://gis.data.ca.gov/api/download/v1/items/01883a79765a4afba132ba54da408d8b/geojson?layers=1>
- Texas: <https://comptroller.texas.gov/data/edi/sales-tax/taxrates.txt>
- Illinois: <https://tax.illinois.gov/content/dam/soi/en/web/tax/research/taxrates/documents/salestaxrates/ordmache-current.txt>
- New York: <https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf>
- Florida: <https://floridarevenue.com/Forms_library/current/dr15dss.pdf>
- Virginia: <https://www.tax.virginia.gov/retail-sales-and-use-tax>
- Washington polygons (DOR jurisdiction boundaries): <https://dor.wa.gov/taxes-rates/sales-tax-jurisdiction-boundaries>
- Washington rates (DOR downloadable database): <https://dor.wa.gov/taxes-rates/sales-and-use-tax-rates/downloadable-database>
- Census TIGER 2024 counties: <https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip>
- Census TIGER 2024 places (per state FIPS): <https://www2.census.gov/geo/tiger/TIGER2024/PLACE/tl_2024_17_place.zip>
- Hawaii county surcharges: <https://tax.hawaii.gov/geninfo/countysurcharge/>
- Guam Business Privilege Tax: <https://www.guamtax.com/info/structure.html>

Remaining flat/regional states (DE, MT, NH, OR, PA, MA, CT, MD, ME, MS, ID, HI, DC, VA), Guam
and the eight state-rate-only ones (CO, LA, AL, AK, SC, MO, AZ, NM) are hand-maintained in
`pipeline/rules/states/<st>.yaml` with a `source` field per state.

## Licence

The published tax-rate facts are public government/public data and are not subject to
copyright. The pipeline code in this repository is licensed under the MIT License; see
[LICENSE](LICENSE).
