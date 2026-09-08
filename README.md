# daigou-calc-data

Data pipeline for 代購算盤 Daigou Calc: it compiles US sales-tax rates by ZIP code from
official state and federal sources, plus a daily USD exchange-rate snapshot, and publishes
them as two static JSON files (`rates.json.gz`, `fx.json`) via GitHub Pages. Rates are
rebuilt quarterly (rates are effective from calendar-quarter starts); FX is refreshed daily.

## Run locally

```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash; use .venv\Scripts\activate on cmd/PowerShell
pip install -r requirements.txt
python -m pipeline fx
python -m pipeline rates --states WA
```

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
- FX (ECB via Frankfurter): <https://api.frankfurter.dev/v1/latest?base=USD>
- FX (TWD via ExchangeRate-API): <https://open.er-api.com/v6/latest/USD>

Remaining flat/regional states (DE, MT, NH, OR, PA, MA, CT, MD, ME, MS, ID, HI, DC) are
hand-maintained in `rules/states/<st>.yaml` with a `source` field per state.

## Licence

The published tax-rate and exchange-rate facts are public government/public data and are
not subject to copyright. The pipeline code in this repository is licensed under the MIT
License.
