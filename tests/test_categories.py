from pipeline import categories

ALL = ("AL AK AR AZ CA CO CT DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS MT NC ND "
       "NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY GU").split()
# Streamlined member states: all 24 exempt prescription drugs (rulings on decision #22).
SST = "AR GA IA IN KS KY MI MN NC ND NE NJ NV OH OK RI SD TN UT VT WA WI WV WY".split()
# SST states whose food rate equals the general rate -> explicit grocery rule.
GROCERY_EXEMPT = "WA MN MI IN KY NJ NV ND NE OH RI WI WY VT".split()
# Reduced food rate (plus IL) -> grocery omitted so the per-ZIP food rate applies.
NO_GROCERY = "AR GA IA KS NC OK TN UT WV IL".split()


def test_every_state_present_with_shape():
    s = categories.load()
    assert set(s) == set(ALL) and len(ALL) == 52
    for code, entry in s.items():
        assert set(entry) == {"localCoverage", "rules", "confidence"}, code
        assert isinstance(entry["localCoverage"], bool)
        assert set(entry["rules"]) <= set(categories.CATS), code


def test_defaults_and_overrides():
    s = categories.load()
    assert s["CA"]["rules"]["grocery"] == {"t": "exempt"}
    assert s["CA"]["rules"]["prescription"] == {"t": "exempt"}
    assert "supplement" not in s["CA"]["rules"]  # a defaulted `general` is implied by absence
    assert s["CA"]["confidence"] == {"supplement": "high"}
    assert s["NY"]["rules"]["clothing"] == {
        "t": "threshold", "limit": "110", "above": {"t": "general"}}
    assert s["MN"]["rules"]["alcohol"] == {"t": "surcharge", "extra": "0.025"}
    assert s["MO"]["rules"]["grocery"] == {"t": "stateReplaced", "rate": "0.01225"}
    assert s["VA"]["rules"]["grocery"] == {"t": "combined", "rate": "0.01"}
    assert s["AL"]["rules"]["grocery"] == {"t": "stateReplaced", "rate": "0.03"}
    assert s["LA"]["rules"]["grocery"] == {"t": "localOnly"}
    assert s["CO"]["localCoverage"] is False and s["WA"]["localCoverage"] is True
    # Decision #26: every state published at the state rate alone carries the flag, so the
    # app shows "unsupported area -- set your own rate" instead of failing to place the ZIP.
    # CO/LA/AL/AK are home rule and stay flagged; SC/MO/AZ/NM flip when Phase 2 lands.
    assert [c for c, e in s.items() if not e["localCoverage"]] == [
        "CO", "LA", "AL", "AK", "SC", "MO", "AZ", "NM"]
    assert "grocery" not in s["IL"]["rules"] and "prescription" not in s["IL"]["rules"]


def test_sst_grocery_and_prescription_are_explicit():
    """Decision #22: an explicit rule beats the per-ZIP food/drug rate, which is the fallback."""
    s = categories.load()
    assert len(SST) == 24
    for code in SST:
        assert s[code]["rules"]["prescription"] == {"t": "exempt"}, code
    for code in GROCERY_EXEMPT:
        assert s[code]["rules"]["grocery"] == {"t": "exempt"}, code
    assert s["SD"]["rules"]["grocery"] == {"t": "general"}
    assert len(GROCERY_EXEMPT) + 1 == 15
    for code in NO_GROCERY:
        assert "grocery" not in s[code]["rules"], code


def test_supplement_rulings():
    """The 2026-09-10 supplement promotion, per state.  Each ruling below was researched from
    primary sources and survived three adversarial refuters; see the app repo's
    docs/superpowers/research/2026-09-10-supplement-rules.md.  A state whose ruling is `general`
    carries NO supplement rule: an omitted key already means the general rate, and writing an
    explicit `general` would emit a rule the app does not need (see the CA pin above)."""
    s = categories.load()
    # Ruling `exempt` -- supplements are not taxed at any rate in these states.
    for code in "NY NJ TX PA FL MD CT".split():
        assert s[code]["rules"]["supplement"] == {"t": "exempt"}, code
        assert s[code]["confidence"]["supplement"] == "high", code
    # Ruling `general` -- verified taxable at the full combined rate, so no rule key.
    for code in "CA WA MA GA VA NV HI AZ".split():
        assert "supplement" not in s[code]["rules"], code
        assert s[code]["confidence"]["supplement"] == "high", code
    # No sales tax at all (+ Guam, 0% at the register, spec §2.6): every category is 0, so the
    # unverified marker was never information.  High without research.
    for code in "DE MT NH OR GU".split():
        assert "supplement" not in s[code]["rules"], code
        assert s[code]["confidence"]["supplement"] == "high", code
    # Everything still unverified, listed explicitly so lowering or raising one is deliberate.
    # IL is low by ruling, not by omission: supplements follow the Illinois grocery rate, which
    # is NOT the published foodDrugRate column, and no rule type in the vocabulary can say that.
    assert sorted(c for c, e in s.items() if e["confidence"]["supplement"] == "low") == sorted(
        "AK AL CO IL LA TN UT".split())
    assert sorted(c for c, e in s.items() if e["confidence"]["supplement"] == "medium") == sorted(
        "AR DC IA ID IN KS KY ME MI MN MO MS NC ND NE NM OH OK RI SC SD VT WI WV WY".split())
    assert {e["confidence"]["supplement"] for e in s.values()} == {"high", "medium", "low"}


def test_rates_in_range():
    from decimal import Decimal
    for code, entry in categories.load().items():
        for r in entry["rules"].values():
            for k in ("rate", "extra"):
                if k in r:
                    assert Decimal("0") <= Decimal(r[k]) <= Decimal("0.15"), (code, r)


def test_guam_is_general_everywhere_and_locally_covered():
    """Guam carries no rule of its own, so it takes the file's defaults. Every rule resolves to
    0% because the state rate is 0, and `localCoverage` stays true: Guam is not a state the app
    should ask the user for a local rate in -- there is nothing to add (spec §2.6). Its only
    override is `supplement: high`: at 0% the unverified marker was never information."""
    s = categories.load()
    assert s["GU"] == {"localCoverage": True, "rules": {"prescription": {"t": "exempt"}},
                       "confidence": {"supplement": "high"}}
