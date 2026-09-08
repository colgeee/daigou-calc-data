import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import requests

from pipeline import build_fx

FX = Path(__file__).parent / "fixtures"

FRANK = {
    "amount": 1.0,
    "base": "USD",
    "date": "2026-09-07",
    "rates": {f"C{i}": 1.5 for i in range(29)} | {"EUR": 0.918234567, "JPY": 148.2},
}
ERAPI = {
    "result": "success",
    "time_last_update_utc": "Tue, 08 Sep 2026 00:02:31 +0000",
    "rates": {"USD": 1, "TWD": 31.53815, "EUR": 0.92},
}
CBC_HTML = (FX / "cbc_ntd_usd.html").read_text(encoding="utf-8")
TODAY = date(2026, 9, 8)


def _erapi(twd=31.53815):
    return ERAPI | {"rates": ERAPI["rates"] | {"TWD": twd}}


def _patch(monkeypatch, *, html=CBC_HTML, erapi=None, frank=None):
    """Wire both network seams to canned payloads. A payload may be an exception to raise."""
    erapi = _erapi() if erapi is None else erapi
    frank = FRANK if frank is None else frank
    monkeypatch.setattr(build_fx, "RETRY_BACKOFF", 0)
    # A list of pages is served one per call, then the last one repeats.
    seq = list(html) if isinstance(html, list) else [html]

    def get_text(url):
        page = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(page, Exception):
            raise page
        return page

    def get_json(url):
        payload = frank if "frankfurter" in url else erapi
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(build_fx, "_get_text", get_text)
    monkeypatch.setattr(build_fx, "_get_json", get_json)


# --- parse_cbc ------------------------------------------------------------------


def test_parse_cbc_reads_the_newest_closing_rate():
    assert build_fx.parse_cbc(CBC_HTML) == (date(2026, 9, 8), D("31.535"))


def test_parse_cbc_rejects_a_page_without_the_column():
    with pytest.raises(ValueError, match="NTD/USD"):
        build_fx.parse_cbc("<table><tr><th>EUR/USD</th></tr><tr><td>2026/09/08</td></tr></table>")


def test_parse_cbc_rejects_a_column_with_no_rows():
    with pytest.raises(ValueError, match="no dated"):
        build_fx.parse_cbc("<table><tr><th>x</th><th>NTD/USD</th></tr></table>")


# --- fetch_twd ------------------------------------------------------------------


def test_fetch_twd_prefers_the_central_bank(monkeypatch):
    _patch(monkeypatch)
    assert build_fx.fetch_twd(TODAY) == (D("31.535"), "cbc")


def test_fetch_twd_falls_back_to_erapi_when_the_cbc_page_fails(monkeypatch):
    _patch(monkeypatch, html=requests.HTTPError("503 Server Error"))
    rate, source = build_fx.fetch_twd(TODAY)
    assert source == "erapi" and rate == D("31.53815")


def test_fetch_twd_retries_a_transient_stub_page(monkeypatch):
    # The live page intermittently answers 200 with a body carrying no table; one blip
    # must not cost us the interbank rate.
    _patch(monkeypatch, html=["<html><body></body></html>", CBC_HTML])
    assert build_fx.fetch_twd(TODAY) == (D("31.535"), "cbc")


def test_fetch_twd_gives_up_after_the_retry_budget(monkeypatch):
    stub = "<html><body></body></html>"
    _patch(monkeypatch, html=[stub] * build_fx.CBC_ATTEMPTS + [CBC_HTML])
    assert build_fx.fetch_twd(TODAY)[1] == "erapi"


def test_fetch_twd_falls_back_when_the_cbc_markup_moved(monkeypatch):
    # A restyled page must degrade to the fallback, not blow up the build.
    _patch(monkeypatch, html="<html><body><p>maintenance</p></body></html>")
    assert build_fx.fetch_twd(TODAY)[1] == "erapi"


def test_fetch_twd_falls_back_on_an_unexpected_scraper_error(monkeypatch):
    _patch(monkeypatch, html=RuntimeError("some bug we did not foresee"))
    assert build_fx.fetch_twd(TODAY)[1] == "erapi"


def test_fetch_twd_falls_back_when_the_cbc_rate_is_stale(monkeypatch):
    # 12 days is past the 10-day holiday allowance, so the primary is treated as failed.
    _patch(monkeypatch)
    rate, source = build_fx.fetch_twd(date(2026, 9, 20))
    assert source == "erapi" and rate == D("31.53815")


def test_fetch_twd_raises_when_both_sources_fail(monkeypatch):
    _patch(monkeypatch, html=requests.ConnectionError("dns"),
           erapi=requests.ConnectionError("dns"))
    with pytest.raises(ValueError, match="no TWD source"):
        build_fx.fetch_twd(TODAY)


def test_fetch_twd_raises_when_the_two_sources_disagree(monkeypatch):
    # A parse that grabbed the wrong cell must never publish: 3% apart is past the 2% band.
    _patch(monkeypatch, erapi=_erapi(32.48))
    with pytest.raises(ValueError, match="disagree"):
        build_fx.fetch_twd(TODAY)


def test_fetch_twd_rejects_an_out_of_band_erapi_rate(monkeypatch):
    _patch(monkeypatch, html=requests.ConnectionError("dns"), erapi=_erapi(3.15))
    with pytest.raises(ValueError, match="no TWD source"):
        build_fx.fetch_twd(TODAY)


# --- compose / validate_fx ------------------------------------------------------


def test_compose_merges_only_twd_and_stringifies():
    doc = build_fx.compose(FRANK, D("31.53815"), published="2026-09-08T17:00:00Z")
    assert doc["base"] == "USD"
    assert doc["rateDate"] == "2026-09-07"
    assert doc["publishedAt"] == "2026-09-08T17:00:00Z"
    assert doc["rates"]["TWD"] == "31.53815"
    assert doc["rates"]["EUR"] == "0.918235"
    assert doc["rates"]["JPY"] == "148.2"
    assert "USD" not in doc["rates"] and len(doc["rates"]) == 32
    assert list(doc) == ["publishedAt", "rateDate", "base", "rates"]
    assert list(doc["rates"]) == sorted(doc["rates"])
    assert build_fx.validate_fx(doc) == []


def test_validate_fx_gates():
    good = build_fx.compose(FRANK, D("31.53815"), published="2026-09-08T17:00:00Z")
    bad = json.loads(json.dumps(good))
    bad["rates"].pop("TWD")
    assert any("TWD" in e for e in build_fx.validate_fx(bad))
    stale = json.loads(json.dumps(good))
    stale["rateDate"] = "2026-08-01"
    assert any("stale" in e for e in build_fx.validate_fx(stale))
    few = json.loads(json.dumps(good))
    few["rates"] = {"TWD": "31", "EUR": "0.9"}
    assert any("at least 30" in e for e in build_fx.validate_fx(few))
    neg = json.loads(json.dumps(good))
    neg["rates"]["EUR"] = "0"
    assert any("positive" in e for e in build_fx.validate_fx(neg))
    wrong_base = json.loads(json.dumps(good))
    wrong_base["base"] = "EUR"
    assert any("USD" in e for e in build_fx.validate_fx(wrong_base))


# --- main -----------------------------------------------------------------------


def test_main_writes_file(tmp_path, monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(build_fx, "build_date", lambda: TODAY)
    monkeypatch.setattr(build_fx, "published_at", lambda: "2026-09-08T17:00:00Z")
    assert build_fx.main(str(tmp_path)) == 0
    raw = (tmp_path / "v1" / "fx.json").read_bytes()
    assert b"\r\n" not in raw
    doc = json.loads(raw.decode("utf-8"))
    assert doc["rates"]["TWD"] == "31.535"  # the central bank's closing rate, not ER-API's
    assert doc["rateDate"] == "2026-09-07" and doc["base"] == "USD"


def test_main_refuses_to_write_when_no_twd_source_is_available(tmp_path, monkeypatch):
    _patch(monkeypatch, html=requests.ConnectionError("dns"),
           erapi=requests.ConnectionError("dns"))
    monkeypatch.setattr(build_fx, "build_date", lambda: TODAY)
    assert build_fx.main(str(tmp_path)) == 1
    assert not (tmp_path / "v1" / "fx.json").exists()


def test_main_refuses_to_write_a_thin_rate_table(tmp_path, monkeypatch):
    _patch(monkeypatch, frank=FRANK | {"rates": {"EUR": 0.92, "JPY": 148.2}})
    monkeypatch.setattr(build_fx, "build_date", lambda: TODAY)
    monkeypatch.setattr(build_fx, "published_at", lambda: "2026-09-08T17:00:00Z")
    assert build_fx.main(str(tmp_path)) == 1
    assert not (tmp_path / "v1" / "fx.json").exists()
