import gzip
import json
from datetime import date
from decimal import Decimal as D

import pytest

from pipeline import build_rates
from pipeline.census import Census
from pipeline.model import ZipRate


class FakeAdapter:
    def __init__(self, name, states, rows):
        self.name, self.states, self._rows = name, states, rows

    def rows(self, census, on):
        return iter(self._rows)


def census():
    return Census(
        centroids={"90012": (34.05, -118.24), "97205": (45.52, -122.69)},
        county={"90012": ("06037", "Los Angeles County"),
                "97205": ("41051", "Multnomah County")},
    )


def wide_census(n=10):
    """A census whose California has `n` ZCTAs, for the per-state coverage gate."""
    zips = [f"9{i:04d}" for i in range(1, n + 1)]
    return Census(
        centroids={z: (34.0, -118.0) for z in zips},
        county={z: ("06037", "Los Angeles County") for z in zips},
    )


def test_build_assembles_validates_and_filters_states(tmp_path):
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    b = FakeAdapter("b", ("OR",), [ZipRate("97205", "OR", D("0"), D("0"), None, "Portland, OR")])
    doc = build_rates.build(census(), date(2026, 9, 8), adapters=[a, b])
    assert [z[0] for z in doc["zips"]] == ["90012", "97205"]
    assert doc["effectiveDate"] == "2026-07-01"
    assert set(doc["states"]) >= {"CA", "OR", "WA", "CO"}
    only = build_rates.build(census(), date(2026, 9, 8), states=["OR"], adapters=[a, b])
    assert [z[0] for z in only["zips"]] == ["97205"]


def test_duplicate_zip_keeps_higher_rate():
    a = FakeAdapter("a", ("CA",), [ZipRate("90012", "CA", D("0.0725"), D("0.01"), None, "X, CA")])
    b = FakeAdapter("b", ("CA",), [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Y, CA")])
    doc = build_rates.build(census(), date(2026, 9, 8), adapters=[a, b])
    assert doc["zips"][0][3].endswith("Y, CA")


def test_state_coverage_gate_rejects_a_short_state():
    c = wide_census(10)
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90001", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    with pytest.raises(ValueError, match="CA"):
        build_rates.build(c, date(2026, 9, 8), adapters=[a])


def test_state_coverage_gate_passes_above_the_floor():
    c = wide_census(10)
    a = FakeAdapter("a", ("CA",), [
        ZipRate(f"9{i:04d}", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")
        for i in range(1, 8)
    ])
    doc = build_rates.build(c, date(2026, 9, 8), adapters=[a])
    assert len(doc["zips"]) == 7


def test_state_coverage_gate_skips_states_outside_the_filter():
    c = wide_census(10)
    a = FakeAdapter("a", ("CA", "OR"), [
        ZipRate(f"9{i:04d}", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")
        for i in range(1, 9)
    ])
    doc = build_rates.build(c, date(2026, 9, 8), states=["CA"], adapters=[a])
    assert len(doc["zips"]) == 8


def test_main_writes_gzip_and_refuses_invalid(tmp_path, monkeypatch):
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    monkeypatch.setattr(build_rates, "_census", census)
    monkeypatch.setattr(build_rates, "_adapters", lambda: [a])
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 1)
    assert build_rates.main(str(tmp_path), []) == 0
    with gzip.open(tmp_path / "v1" / "rates.json.gz", "rt", encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["zips"][0][0] == "90012" and (tmp_path / "v1" / "rates.json").exists()
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 25000)
    assert build_rates.main(str(tmp_path / "again"), []) == 1
    assert not (tmp_path / "again" / "v1" / "rates.json.gz").exists()


def test_main_refuses_when_a_state_is_short(tmp_path, monkeypatch):
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90001", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    monkeypatch.setattr(build_rates, "_census", lambda: wide_census(10))
    monkeypatch.setattr(build_rates, "_adapters", lambda: [a])
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 1)
    assert build_rates.main(str(tmp_path), []) == 1
    assert not (tmp_path / "v1").exists()
