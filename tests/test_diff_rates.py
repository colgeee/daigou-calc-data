import gzip
import json
import subprocess
from decimal import Decimal as D

from pipeline import diff_rates


def doc(rows: dict[str, tuple[str, str, str]]) -> dict:
    """`{zip: (state, stateRate, localRate)}` -> a rates document."""
    profiles, zips = {}, []
    for z, (st, sr, lr) in sorted(rows.items()):
        pid = f"{st}-{sr}-{lr}-{z}"
        profiles[pid] = {"state": st, "label": z, "stateRate": sr, "localRate": lr,
                         "foodDrugRate": None, "groceryRate": None}
        zips.append([z, 0, 0, pid])
    return {"schemaVersion": "1", "effectiveDate": "2026-07-01", "publishedAt": "x",
            "profiles": profiles, "states": {}, "zips": zips}


BASE = {f"9{i:04d}": ("CA", "0.0725", "0.0225") for i in range(100)}


def test_a_quiet_quarter_passes():
    r = diff_rates.diff(doc(BASE), doc(BASE))
    assert r.failures() == [] and r.changed == []
    assert any("0 ZIPs changed" in line for line in r.lines())


def test_more_than_five_per_cent_of_zips_changing_fails():
    """The rule is `share > MAX_CHANGED_SHARE`, so exactly 5 of 100 passes and 6 fails.
    Hawaii's 97 ZIPs are 0.29% of the live table, well inside it."""
    def moved(n: int) -> dict:
        out = dict(BASE)
        for i in range(n):
            out[f"9{i:04d}"] = ("CA", "0.0725", "0.0325")
        return out
    assert diff_rates.diff(doc(BASE), doc(moved(5))).failures() == []
    assert any("changed general rate" in f
               for f in diff_rates.diff(doc(BASE), doc(moved(6))).failures())


def test_a_single_two_point_move_fails_on_its_own():
    """Hawaii's Maui correction is 0.712 pp and must pass; a 2.5 pp move is a source swing."""
    small = dict(BASE)
    small["90000"] = ("CA", "0.0725", "0.0296")            # +0.71 pp
    assert diff_rates.diff(doc(BASE), doc(small)).failures() == []
    big = dict(BASE)
    big["90000"] = ("CA", "0.0725", "0.0475")              # +2.5 pp
    report = diff_rates.diff(doc(BASE), doc(big))
    assert report.changed == [("90000", D("0.0950"), D("0.1200"))]
    assert any("90000" in f and "2 pp" in f for f in report.failures())


def test_the_zip_count_falling_more_than_one_per_cent_fails():
    fewer = {z: v for z, v in list(BASE.items())[:98]}
    assert any("ZIP count fell" in f for f in diff_rates.diff(doc(BASE), doc(fewer)).failures())
    # Added ZIPs never fire a rule: Guam's seven arrive that way.
    more = dict(BASE) | {"96910": ("GU", "0", "0")}
    assert diff_rates.diff(doc(BASE), doc(more)).failures() == []


def test_a_state_losing_five_per_cent_or_vanishing_fails():
    old = dict(BASE) | {f"8{i:04d}": ("NV", "0.0685", "0.01525") for i in range(20)}
    gone = dict(BASE)
    fails = diff_rates.diff(doc(old), doc(gone)).failures()
    assert any("NV" in f and "vanished" in f for f in fails)
    thinned = dict(BASE) | {f"8{i:04d}": ("NV", "0.0685", "0.01525") for i in range(18)}
    thin_fails = diff_rates.diff(doc(old), doc(thinned)).failures()
    assert any("NV" in f and "lost" in f for f in thin_fails)


def test_allow_swings_prints_the_report_and_returns_zero(tmp_path, capsys):
    v1 = tmp_path / "v1"
    v1.mkdir()
    big = dict(BASE)
    big["90000"] = ("CA", "0.0725", "0.0475")
    (v1 / "rates.json").write_text(json.dumps(doc(big)), encoding="utf-8")
    published = tmp_path / "published.json"
    published.write_text(json.dumps(doc(BASE)), encoding="utf-8")
    assert diff_rates.main(str(tmp_path), str(published)) == 1
    assert "REFUSING TO PUBLISH" in capsys.readouterr().out
    assert diff_rates.main(str(tmp_path), str(published), allow_swings=True) == 0
    out = capsys.readouterr().out
    assert "--allow-swings" in out and "90000" in out


def test_the_report_goes_to_the_step_summary_when_github_sets_one(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "rates.json").write_text(json.dumps(doc(BASE)), encoding="utf-8")
    published = tmp_path / "published.json"
    published.write_text(json.dumps(doc(BASE)), encoding="utf-8")
    assert diff_rates.main(str(tmp_path), str(published)) == 0
    assert "ZIPs changed" in summary.read_text(encoding="utf-8")


def test_load_published_reads_a_gzip_a_plain_file_and_a_git_ref(tmp_path, monkeypatch):
    plain = tmp_path / "a.json"
    plain.write_text(json.dumps(doc(BASE)), encoding="utf-8")
    assert len(diff_rates.load_published(str(plain))["zips"]) == 100
    gz = tmp_path / "a.json.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    assert len(diff_rates.load_published(str(gz))["zips"]) == 100
    monkeypatch.setattr(diff_rates.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, plain.read_bytes(), b""))
    assert len(diff_rates.load_published("git:origin/gh-pages:v1/rates.json")["zips"]) == 100


def test_a_failing_git_ref_raises_naming_the_ref_and_gits_stderr(monkeypatch):
    monkeypatch.setattr(diff_rates.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 128, b"", b"fatal: invalid object name 'origin/gh-pages'"))
    try:
        diff_rates.load_published("git:origin/gh-pages:v1/rates.json")
    except ValueError as e:
        assert "origin/gh-pages:v1/rates.json" in str(e) and "invalid object name" in str(e)
    else:
        raise AssertionError("a failing git show must raise")


def test_an_http_ref_goes_through_the_cache_with_a_published_sized_floor(monkeypatch):
    seen = {}

    def fake(url, *, ttl_days, min_bytes):
        seen.update(url=url, ttl_days=ttl_days, min_bytes=min_bytes)
        return gzip.compress(json.dumps(doc(BASE)).encode())

    monkeypatch.setattr(diff_rates, "get_cached", fake)
    assert len(diff_rates.load_published("https://example.test/v1/rates.json.gz")["zips"]) == 100
    assert seen == {"url": "https://example.test/v1/rates.json.gz", "ttl_days": 0,
                    "min_bytes": diff_rates.MIN_PUBLISHED_BYTES}
