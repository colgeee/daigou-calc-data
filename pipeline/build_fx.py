"""Build out/v1/fx.json: ECB reference rates via Frankfurter, plus a TWD rate.

TWD is not an ECB reference currency, so it comes from a second source. The primary is
Taiwan's central bank interbank closing rate (新臺幣對美元銀行間收盤匯率) -- the rate the
banks actually closed at, with no retail middleman's spread on it (decision #24). The
open ExchangeRate-API endpoint is the fallback, and doubles as a cross-check: when both
sources answer and they disagree by more than 2%, the build refuses to publish rather
than risk shipping a rate scraped out of the wrong table cell.

Unlike every other source in this pipeline, FX is never served from the on-disk cache --
a day-old exchange rate is a wrong exchange rate -- so this module talks to `requests`
directly instead of going through `pipeline.http.get_cached`.
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import time
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import requests

from pipeline import build_date, published_at
from pipeline.http import UA
from pipeline.model import rate_str

FRANKFURTER = "https://api.frankfurter.dev/v1/latest?base=USD"
ERAPI = "https://open.er-api.com/v6/latest/USD"
# Overridable so a live run can exercise the fallback path without editing the module.
CBC_URL = os.environ.get("CBC_URL", "https://www.cbc.gov.tw/tw/lp-645-1.html")

TIMEOUT = 20
SIX = Decimal("0.000001")

# TWD has traded in the high 20s to low 30s for decades; anything outside this band is a
# parse or upstream error, not a devaluation we should quietly publish.
TWD_MIN = Decimal("20")
TWD_MAX = Decimal("45")
# Taiwan's Lunar New Year closure can run nine days, so a fortnight-wide window would be
# too loose and a week too tight.
CBC_MAX_AGE_DAYS = 10
# The two sources quote the same market and normally sit ~0.01% apart. Anything wider
# means one of them is wrong.
TWD_MAX_SPREAD = Decimal("0.02")
# ECB publishes on TARGET business days, so a weekend build legitimately carries Friday's
# date -- a Monday UTC build after a Friday ECB date is already three days behind, and a
# TARGET holiday such as Easter Monday pushes that to four, so the gate needs headroom
# above four rather than sitting right on it.
MAX_RATE_AGE_DAYS = 5

# The live ECB reference table currently carries exactly 30 currencies, so gating on that
# exact count would break every build the moment a single currency is retired.
MIN_CURRENCIES = 25

# Observed live: the CBC page intermittently answers 200 with a stub body that carries no
# rate table. Falling back on the first blip would publish the retail-spread rate when the
# interbank one was a second away, so the primary gets a couple of retries first.
CBC_ATTEMPTS = 3
RETRY_BACKOFF = 2.0

_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _get(url: str) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def _get_json(url: str) -> dict:
    return _get(url).json()


def _get_text(url: str) -> str:
    resp = _get(url)
    # The CBC page is UTF-8; requests would otherwise fall back to ISO-8859-1 for an
    # HTML response that carries no charset in its Content-Type header.
    resp.encoding = "utf-8"
    return resp.text


def _s(v: float | int | str | Decimal) -> str:
    """Normalise a rate to a plain decimal string of at most six fractional digits."""
    return rate_str(Decimal(str(v)).quantize(SIX, rounding=ROUND_HALF_UP))


def _cells(page: str) -> list[str]:
    """Every <td>/<th> in `page` as whitespace-collapsed plain text."""
    out = []
    for m in _CELL_RE.finditer(page):
        text = html_mod.unescape(_TAG_RE.sub(" ", m.group(1)))
        out.append(" ".join(text.split()))
    return out


def parse_cbc(page: str) -> tuple[date, Decimal]:
    """Read the newest (date, NTD/USD closing rate) pair out of the CBC rate table.

    The table is a two-column `標題(日期)` / `NTD/USD` grid ordered newest first, so the
    first dated row after the `NTD/USD` header is today's close. Keying off the header
    text rather than a column index keeps the parser honest if the page gains a column.
    """
    cells = _cells(page)
    header = next((i for i, c in enumerate(cells) if c.upper() == "NTD/USD"), None)
    if header is None:
        raise ValueError(f"CBC page: no NTD/USD column header among {len(cells)} cells")
    pending: date | None = None
    for cell in cells[header + 1:]:
        m = _DATE_RE.match(cell)
        if m:
            pending = date(int(m[1]), int(m[2]), int(m[3]))
        elif pending is not None and _NUM_RE.match(cell):
            return pending, Decimal(cell)
        else:
            pending = None
    raise ValueError("CBC page: no dated NTD/USD row under the header")


def _check_band(rate: Decimal, what: str) -> Decimal:
    if not TWD_MIN <= rate <= TWD_MAX:
        raise ValueError(f"{what} {rate} is outside the {TWD_MIN}-{TWD_MAX} sanity band")
    return rate


def _fetch_cbc() -> tuple[date, Decimal]:
    """Fetch and parse the CBC table, retrying transient stub responses."""
    last: Exception | None = None
    for attempt in range(1, CBC_ATTEMPTS + 1):
        try:
            return parse_cbc(_get_text(CBC_URL))
        except Exception as e:
            last = e
            if attempt < CBC_ATTEMPTS:
                print(f"[fx] central bank page attempt {attempt}/{CBC_ATTEMPTS} failed "
                      f"({type(e).__name__}: {e}); retrying")
                time.sleep(RETRY_BACKOFF * 2 ** (attempt - 1))
    assert last is not None
    raise last


def _twd_from_cbc(on: date) -> tuple[Decimal, date]:
    quoted, rate = _fetch_cbc()
    _check_band(rate, "CBC NTD/USD")
    if abs((on - quoted).days) > CBC_MAX_AGE_DAYS:
        raise ValueError(f"CBC closing rate is dated {quoted}, more than "
                         f"{CBC_MAX_AGE_DAYS} days from {on}")
    return rate, quoted


def _twd_from_erapi() -> Decimal:
    data = _get_json(ERAPI)
    try:
        raw = data["rates"]["TWD"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"ER-API response has no rates.TWD: {e}") from e
    try:
        rate = Decimal(str(raw))
    except InvalidOperation as e:
        raise ValueError(f"ER-API rates.TWD is not a number: {raw!r}") from e
    return _check_band(rate, "ER-API TWD")


def fetch_twd(on: date) -> tuple[Decimal, str]:
    """Return `(rate, source)` for USD->TWD, source being "cbc" or "erapi".

    Both sources are always consulted: the second one is the fallback when the central
    bank's page is unreachable, and a cross-check on the scrape when it is not.
    """
    # Each source is caught broadly on purpose: whether the page 503s, the markup moved,
    # or the scrape hit a bug we did not foresee, the answer is the same -- treat this
    # source as failed and let the other one carry the build.
    cbc: tuple[Decimal, date] | None = None
    try:
        cbc = _twd_from_cbc(on)
    except Exception as e:
        print(f"[fx] central bank NTD/USD unavailable: {type(e).__name__}: {e}")
    erapi: Decimal | None = None
    try:
        erapi = _twd_from_erapi()
    except Exception as e:
        print(f"[fx] ER-API TWD unavailable: {type(e).__name__}: {e}")
    if cbc is not None and erapi is not None:
        spread = abs(cbc[0] - erapi) / erapi
        if spread > TWD_MAX_SPREAD:
            raise ValueError(f"TWD sources disagree by {spread:.2%} (max "
                             f"{TWD_MAX_SPREAD:.0%}): CBC {cbc[0]} vs ER-API {erapi}")
        print(f"[fx] TWD cross-check: CBC {cbc[0]} vs ER-API {erapi}, {spread:.3%} apart")
    if cbc is not None:
        print(f"[fx] TWD {cbc[0]} from cbc ({cbc[1]})")
        return cbc[0], "cbc"
    if erapi is not None:
        print(f"[fx] TWD {erapi} from erapi ({on})")
        return erapi, "erapi"
    raise ValueError("no TWD source available: both the central bank page and ER-API failed")


def compose(frankfurter: dict, twd: Decimal, published: str) -> dict:
    """The app's frozen FxDataset shape -- these four keys, nothing else."""
    rates = {code: _s(v) for code, v in frankfurter["rates"].items() if code != "USD"}
    rates["TWD"] = _s(twd)
    return {"publishedAt": published, "rateDate": frankfurter["date"], "base": "USD",
            "rates": dict(sorted(rates.items()))}


def validate_fx(doc: dict) -> list[str]:
    errors: list[str] = []
    if doc.get("base") != "USD":
        errors.append(f"base: expected USD, got {doc.get('base')!r}")
    rates = doc.get("rates", {})
    if "TWD" not in rates:
        errors.append("rates: missing TWD")
    if len(rates) < MIN_CURRENCIES:
        errors.append(f"rates: expected at least {MIN_CURRENCIES} currencies, got {len(rates)}")
    for code, value in sorted(rates.items()):
        try:
            if Decimal(value) <= 0:
                errors.append(f"rate {code}: must be positive, got {value!r}")
        except InvalidOperation:
            errors.append(f"rate {code}: not a decimal string, got {value!r}")
    try:
        quoted = date.fromisoformat(doc["rateDate"])
        published = datetime.fromisoformat(doc["publishedAt"].replace("Z", "+00:00")).date()
        if (published - quoted).days > MAX_RATE_AGE_DAYS:
            errors.append(f"rateDate {quoted} is stale relative to {published}")
    except (KeyError, ValueError, AttributeError) as e:
        errors.append(f"dates: {e}")
    return errors


def _write_atomic(path: Path, text: str) -> None:
    """Write via a same-directory `.tmp` plus `os.replace`, so a failed write can never
    leave a truncated fx.json behind; `newline="\\n"` keeps LF endings on Windows."""
    tmp = path.parent / (path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(out_dir: str) -> int:
    try:
        frankfurter = _get_json(FRANKFURTER)
        twd, _source = fetch_twd(build_date())
        doc = compose(frankfurter, twd, published_at())
    except Exception as e:
        # Any failure to assemble the document is a refusal, never a partial write.
        print(f"[fx] REFUSING TO WRITE — {type(e).__name__}: {e}")
        return 1
    errors = validate_fx(doc)
    if errors:
        print("[fx] REFUSING TO WRITE — validation failed:")
        for e in errors[:50]:
            print("   ", e)
        return 1
    out = Path(out_dir) / "v1"
    out.mkdir(parents=True, exist_ok=True)
    _write_atomic(out / "fx.json",
                  json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"[fx] wrote {out / 'fx.json'} — {len(doc['rates'])} currencies, "
          f"TWD {doc['rates']['TWD']}, rateDate {doc['rateDate']}")
    return 0
