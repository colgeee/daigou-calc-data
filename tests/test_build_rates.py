import gzip
import json
import struct
from datetime import date
from decimal import Decimal as D

import pytest

from pipeline import build_rates
from pipeline.census import STATE_FIPS, Census
from pipeline.model import ZipRate

# Captured before the autouse fixture below neutralises the module attribute, so the tests
# that exercise the gate through `build` can put the real one back.
REAL_CHECK_PARTITION = build_rates.check_partition


class FakeAdapter:
    def __init__(self, name, states, rows):
        self.name, self.states, self._rows = name, states, rows

    def rows(self, census, on):
        return iter(self._rows)


@pytest.fixture(autouse=True)
def _skip_partition(monkeypatch):
    """F4's partition gate demands all 50 states + DC + GU on every full build; the fakes below
    claim one or two states apiece and a fake census knows only a couple of ZIPs, so the
    gate is neutralised here the way `test_tx.py` lowers `MIN_DATA_ROWS`. The tests that
    exercise the gate call `check_partition` directly, or restore it explicitly."""
    monkeypatch.setattr(build_rates, "check_partition", lambda adapters: None)


def partitioning_adapters():
    """One adapter per state code, so the union is exactly `STATE_FIPS` with no state
    claimed twice."""
    return [FakeAdapter(f"a{st}", (st,), []) for st in STATE_FIPS]


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


def test_build_assembles_validates_and_filters_states():
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


def test_unknown_state_code_raises_value_error_not_key_error():
    a = FakeAdapter("a", ("PR",),
                    [ZipRate("00901", "PR", D("0.105"), D("0"), None, "San Juan, PR")])
    with pytest.raises(ValueError, match="PR"):
        build_rates.build(census(), date(2026, 9, 8), adapters=[a])


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


def test_gzip_bytes_are_deterministic():
    """F1: the same document bytes gzip to identical output every time, and the header's
    MTIME field (bytes 4-7 of the gzip member) is zeroed rather than carrying the build's
    wall-clock time."""
    data = json.dumps({"schemaVersion": "1", "zips": []}).encode("utf-8")
    one = build_rates._gzip_bytes(data)
    two = build_rates._gzip_bytes(data)
    assert one == two
    assert struct.unpack("<I", one[4:8])[0] == 0


def test_rates_json_has_no_crlf(tmp_path, monkeypatch):
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    monkeypatch.setattr(build_rates, "_census", census)
    monkeypatch.setattr(build_rates, "_adapters", lambda: [a])
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 1)
    assert build_rates.main(str(tmp_path), []) == 0
    assert b"\r" not in (tmp_path / "v1" / "rates.json").read_bytes()


def test_write_failure_leaves_no_partial_or_tmp_files(tmp_path, monkeypatch):
    """F2: a write that fails partway through (here, `os.replace` on the gzip file)
    leaves neither a partial final file nor a leftover `.tmp`."""
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    monkeypatch.setattr(build_rates, "_census", census)
    monkeypatch.setattr(build_rates, "_adapters", lambda: [a])
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 1)

    def boom(*_a, **_kw):
        raise OSError("disk full")
    monkeypatch.setattr(build_rates.os, "replace", boom)

    with pytest.raises(OSError):
        build_rates.main(str(tmp_path), [])
    v1 = tmp_path / "v1"
    assert not (v1 / "rates.json.gz").exists()
    assert not (v1 / "rates.json").exists()
    assert list(v1.glob("*.tmp")) == []


def test_main_prints_the_five_sections(tmp_path, monkeypatch, capsys):
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    monkeypatch.setattr(build_rates, "_census", census)
    monkeypatch.setattr(build_rates, "_adapters", lambda: [a])
    monkeypatch.setattr(build_rates, "MIN_ZIPS", 1)
    assert build_rates.main(str(tmp_path), []) == 0
    out = capsys.readouterr().out
    assert "[build] adapter a: 1 rows" in out
    assert "duplicate ZIP rows resolved to the higher rate" in out
    assert "state" in out and "emitted" in out and "census" in out and "cover" in out
    assert "CA" in out and "100.0%" in out
    assert "[build] total: 1 ZIPs across 1 state codes" in out
    assert "[build] state-rate-only: 0 of 1 states with rows are localCoverage false" in out
    assert "[build] validation: []" in out


def test_check_partition_passes_on_an_exact_partition():
    assert REAL_CHECK_PARTITION(partitioning_adapters()) is None


def test_check_partition_raises_when_a_state_is_unclaimed():
    """F4: an adapter dropped from the registry, or one whose `states` tuple loses an
    entry, takes its states out of the build without tripping the coverage gate -- which
    only measures the states an adapter actually claims."""
    short = [a for a in partitioning_adapters() if a.states != ("WY",)]
    with pytest.raises(ValueError, match="no adapter claims WY"):
        REAL_CHECK_PARTITION(short)


def test_check_partition_raises_when_a_state_is_claimed_twice():
    """The other half of the invariant: two adapters racing for the same ZIPs, resolved
    only by whichever composes the higher rate."""
    doubled = [*partitioning_adapters(), FakeAdapter("greedy", ("NV", "UT"), [])]
    with pytest.raises(ValueError, match=r"NV \(aNV\+greedy\), UT \(aUT\+greedy\)"):
        REAL_CHECK_PARTITION(doubled)


def test_check_partition_names_a_code_that_is_not_a_state():
    extra = [*partitioning_adapters(), FakeAdapter("pr", ("PR",), [])]
    with pytest.raises(ValueError, match="not a state code: PR"):
        REAL_CHECK_PARTITION(extra)


def test_the_registered_adapters_partition_every_state():
    """The gate's real subject: the shipped registry. sst (24) + ca + tx + il + ny + fl +
    yaml (23) must come to exactly the 50 states + DC + Guam, each claimed once."""
    adapters = build_rates._adapters()
    assert sum(len(a.states) for a in adapters) == len(STATE_FIPS) == 52
    assert REAL_CHECK_PARTITION(adapters) is None


def test_a_full_build_runs_the_partition_gate(monkeypatch):
    monkeypatch.setattr(build_rates, "check_partition", REAL_CHECK_PARTITION)
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    # "52 state codes", not "52 states + DC": the count is `len(STATE_FIPS)`, which already
    # has DC and GU inside it, so the old wording read as 52 + 1.
    with pytest.raises(ValueError, match="must partition all 52 state codes"):
        build_rates.build(census(), date(2026, 9, 8), adapters=[a])


def test_a_filtered_build_skips_the_partition_gate(monkeypatch):
    """`--states CA` is deliberately partial, so the partition is an invariant of the full
    quarterly build alone."""
    monkeypatch.setattr(build_rates, "check_partition", REAL_CHECK_PARTITION)
    a = FakeAdapter("a", ("CA",),
                    [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")])
    doc = build_rates.build(census(), date(2026, 9, 8), states=["CA"], adapters=[a])
    assert [z[0] for z in doc["zips"]] == ["90012"]


def test_summary_counts_the_state_rate_only_states(capsys):
    """Decision #26: the summary names the states published at the state rate alone, so a
    local-rate adapter that silently disappears -- or newly lands -- shows in the log."""
    c = Census(centroids={"80202": (39.75, -104.99), "90012": (34.05, -118.24)},
               county={"80202": ("08031", "Denver County"),
                       "90012": ("06037", "Los Angeles County")})
    a = FakeAdapter("a", ("CO", "CA"), [
        ZipRate("80202", "CO", D("0.029"), D("0"), None, "Denver, CO"),
        ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA"),
    ])
    build_rates.build(c, date(2026, 9, 8), adapters=[a])
    out = capsys.readouterr().out
    assert "[build] state-rate-only: 1 of 2 states with rows are localCoverage false (CO)" in out
