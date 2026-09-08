import copy
from decimal import Decimal as D

from pipeline.model import ZipRate, assemble
from pipeline.validate import validate

STATES = {
    "CA": {"localCoverage": True,
           "rules": {"clothing": {"t": "threshold", "limit": "110", "above": {"t": "general"}}},
           "confidence": {}}}


def doc():
    return assemble(
        [ZipRate("90012", "CA", D("0.0725"), D("0.0225"), None, "Los Angeles, CA")],
        {"90012": (34.05, -118.24)}, STATES,
        effective="2026-10-01", published="2026-09-08T00:00:00Z")


def test_good_doc_passes_with_low_floor():
    assert validate(doc(), min_zips=1) == []


def test_min_zips():
    assert any(e.startswith("zips:") for e in validate(doc(), min_zips=38000))


def test_rate_out_of_range():
    d = doc()
    next(iter(d["profiles"].values()))["localRate"] = "0.20"
    assert any("out of range" in e for e in validate(d, min_zips=1))


def test_missing_state_rules():
    d = doc()
    d["states"] = {}
    assert any("no state rules" in e for e in validate(d, min_zips=1))


def test_malformed_zip_and_unknown_profile():
    d = doc()
    d["zips"][0][0] = "9001"
    d["zips"].append(["90013", 1, 1, "nope"])
    errs = validate(d, min_zips=1)
    assert any("malformed" in e for e in errs) and any("unknown profile" in e for e in errs)


def test_rates_must_be_strings():
    d = copy.deepcopy(doc())
    next(iter(d["profiles"].values()))["stateRate"] = 0.0725
    assert any("must be a string" in e for e in validate(d, min_zips=1))


def test_nested_rule_rate_checked():
    d = doc()
    d["states"]["CA"]["rules"]["clothing"]["above"] = {"t": "combined", "rate": "0.3"}
    assert any("rule clothing" in e for e in validate(d, min_zips=1))
