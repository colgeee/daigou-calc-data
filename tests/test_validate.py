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


def test_grocery_rate_range_is_checked():
    d = doc()
    next(iter(d["profiles"].values()))["groceryRate"] = "0.9"
    assert any("groceryRate" in e and "out of range" in e for e in validate(d, min_zips=1))


def test_illinois_profiles_must_carry_a_grocery_rate():
    d = assemble(
        [ZipRate("60601", "IL", D("0.0625"), D("0.0425"), D("0.025"), "Chicago, IL")],
        {"60601": (41.88, -87.62)},
        {"IL": {"localCoverage": True, "rules": {}, "confidence": {}}},
        effective="2026-10-01", published="2026-09-11T00:00:00Z")
    assert any("must carry a groceryRate" in e for e in validate(d, min_zips=1))


def test_a_state_naming_the_grocery_rule_must_publish_the_rate():
    """The gate that survives someone dropping IL from GROCERY_REQUIRED, and the one that
    catches a future state pointed at a rate its adapter never emits: the app would resolve
    that category to the general rate behind a low-confidence marker, silently, in exactly
    the state the rule was written for."""
    d = doc()
    d["states"]["CA"]["rules"]["supplement"] = {"t": "groceryRate"}
    assert any("must carry a groceryRate" in e for e in validate(d, min_zips=1))


def test_grocery_rule_type_is_known():
    d = doc()
    d["states"]["CA"]["rules"]["supplement"] = {"t": "groceryRate"}
    assert not any("unknown rule type" in e for e in validate(d, min_zips=1))


def test_a_misspelled_grocery_rule_is_still_refused():
    d = doc()
    d["states"]["CA"]["rules"]["supplement"] = {"t": "groceryrate"}
    assert any("unknown rule type" in e for e in validate(d, min_zips=1))
