# daigou-calc-data

Data pipeline for 代購算盤 Daigou Calc: it compiles US sales-tax rates by ZIP code from
official state and federal sources and publishes them as a static JSON file
(`rates.json.gz`) via GitHub Pages. It is rebuilt quarterly, since rates are effective from
calendar-quarter starts.

## Published files

| File | URL | Rebuilt |
| --- | --- | --- |
| Rates | <https://colgeee.github.io/daigou-calc-data/v1/rates.json.gz> | quarterly — 2 Jan / 2 Apr / 2 Jul / 2 Oct, 06:00 UTC (`.github/workflows/rates.yml`) |

## Exchange rates

Exchange rates are no longer published here — the app fetches them itself from
<https://api.exchangerate.fun/> (refreshed hourly, no API key) and lets the user override
the rate for the selected currency. The daily FX workflow that used to build `v1/fx.json`
is gone.

The file is served from the `gh-pages` branch, which `scripts/publish.sh` rewrites after a
successful build. GitHub Pages returns an `ETag`; the app sends it back as
`If-None-Match`, so an unchanged file costs a 304 and no download. An uncompressed
`v1/rates.json` sits beside the gzip for debugging.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash; use .venv\Scripts\activate on cmd/PowerShell
pip install -r requirements.txt
python -m pipeline rates --states WA
```

## Coverage

Every state + DC is published — 33 620 ZIPs in the current build, and the validator refuses
to publish a full build under 25 000. 42 states + DC carry local rates; CO, LA, AL, AK
(permanently) and SC, MO, AZ, NM (until Phase 2) carry the state rate only and are flagged
so the app asks for the local rate.

A flagged state still gets a row per Census ZIP, at its state rate with a zero local rate
(decision #26); the app reads the `localCoverage: false` flag and shows "unsupported area —
set your own rate". Publishing no rows at all would leave it unable to place the ZIP, so the
same phone would see "couldn't get a location" instead.

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

## Sources

- Census ZCTA centroids: <https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip>
- Census ZCTA→county: <https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt>
- Census ZCTA→place: <https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_place20_natl.txt>
- Streamlined Sales Tax mirror (24 states) — rates: <http://52.15.48.162/ratesandboundry/Rates/>
- Streamlined Sales Tax mirror (24 states) — boundaries: <http://52.15.48.162/ratesandboundry/Boundary/>
- California: <https://gis.data.ca.gov/api/download/v1/items/01883a79765a4afba132ba54da408d8b/csv?layers=1>
- Texas: <https://comptroller.texas.gov/data/edi/sales-tax/taxrates.txt>
- Illinois: <https://tax.illinois.gov/content/dam/soi/en/web/tax/research/taxrates/documents/salestaxrates/ordmache-current.txt>
- New York: <https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf>
- Florida: <https://floridarevenue.com/Forms_library/current/dr15dss.pdf>
- Virginia: <https://www.tax.virginia.gov/retail-sales-and-use-tax>

Remaining flat/regional states (DE, MT, NH, OR, PA, MA, CT, MD, ME, MS, ID, HI, DC, VA) and
the eight state-rate-only ones (CO, LA, AL, AK, SC, MO, AZ, NM) are hand-maintained in
`pipeline/rules/states/<st>.yaml` with a `source` field per state.

## Licence

The published tax-rate facts are public government/public data and are not subject to
copyright. The pipeline code in this repository is licensed under the MIT License; see
[LICENSE](LICENSE).
