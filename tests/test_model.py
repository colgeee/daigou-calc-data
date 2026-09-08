from decimal import Decimal as D

from pipeline.model import ZipRate, assemble, profile_id, rate_str, rule


def zr(zip_="90012", st="CA", sr="0.0725", lr="0.0225", fd=None, label="Los Angeles, CA"):
    return ZipRate(zip_, st, D(sr), D(lr), None if fd is None else D(fd), label)


def test_rate_str_normalises():
    assert rate_str(D("0.07250")) == "0.0725"
    assert rate_str(D("0")) == "0"
    assert rate_str(D("0.10")) == "0.1"
    assert rate_str(D("1E-3")) == "0.001"


def test_profile_id_is_stable_and_distinguishes_food_rate():
    a, b = zr(), zr(zip_="90210")
    assert profile_id(a) == profile_id(b) == "CA-0.0725-0.0225-LOS ANGELES, CA"
    assert profile_id(zr(fd="0")) == "CA-0.0725-0.0225-LOS ANGELES, CA-FD0"


def test_rule_stringifies_decimals():
    assert rule("threshold", limit=D("110"), above=rule("general")) == {
        "t": "threshold", "limit": "110", "above": {"t": "general"}}


def test_assemble_matches_frozen_schema():
    doc = assemble(
        [zr(), zr(zip_="90210"), zr(zip_="97205", st="OR", sr="0", lr="0", label="Portland, OR")],
        {"90012": (34.0522, -118.2437), "90210": (34.0901, -118.4065),
         "97205": (45.5202, -122.6863)},
        {"CA": {"localCoverage": True, "rules": {}, "confidence": {}},
         "OR": {"localCoverage": True, "rules": {}, "confidence": {}}},
        effective="2026-10-01", published="2026-09-08T00:00:00Z")
    assert doc["schemaVersion"] == "1"
    assert set(doc) == {
        "schemaVersion", "effectiveDate", "publishedAt", "profiles", "states", "zips"}
    assert doc["zips"] == [["90012", 34052200, -118243700, "CA-0.0725-0.0225-LOS ANGELES, CA"],
                           ["90210", 34090100, -118406500, "CA-0.0725-0.0225-LOS ANGELES, CA"],
                           ["97205", 45520200, -122686300, "OR-0-0-PORTLAND, OR"]]
    assert doc["profiles"]["OR-0-0-PORTLAND, OR"] == {
        "state": "OR", "label": "Portland, OR", "stateRate": "0", "localRate": "0",
        "foodDrugRate": None}
    assert len(doc["profiles"]) == 2


def test_assemble_drops_zips_without_centroid():
    doc = assemble(
        [zr(zip_="00000")], {}, {"CA": {"localCoverage": True, "rules": {}, "confidence": {}}},
        effective="2026-10-01", published="2026-09-08T00:00:00Z")
    assert doc["zips"] == [] and doc["profiles"] == {}
