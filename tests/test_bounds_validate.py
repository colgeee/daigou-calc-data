import copy

from pipeline.bounds.validate import validate_bounds

RATES = {"effectiveDate": "2026-07-01",
         "states": {"CA": {"localCoverage": True, "rules": {}, "confidence": {}},
                    "IL": {"localCoverage": True, "rules": {}, "confidence": {}}}}
MIN = {"cdtfa": 1, "tiger-il": 1}


def doc() -> dict:
    return {
        "schemaVersion": "1", "effectiveDate": "2026-07-01",
        "publishedAt": "2026-09-10T06:11:02Z",
        "transform": {"scale": [1e-05, 1e-05], "translate": [0, 0]},
        "arcs": [[[0, 200000], [200000, -200000]],
                 [[200000, 0], [-200000, 0], [0, 200000]],
                 [[0, 200000], [200000, 0], [0, -200000]]],
        "profiles": {
            "CA-0.0725-0.035-SANTA MONICA, CA": {
                "state": "CA", "label": "Santa Monica, CA", "stateRate": "0.0725",
                "localRate": "0.035", "foodDrugRate": None},
            "IL-0.0625-0.04-CHICAGO, IL-FD0.01": {
                "state": "IL", "label": "Chicago, IL", "stateRate": "0.0625",
                "localRate": "0.04", "foodDrugRate": "0.01"}},
        "polygons": [
            {"id": "cdtfa:SANTA MONICA", "source": "cdtfa",
             "profile": "CA-0.0725-0.035-SANTA MONICA, CA", "rings": [[0, 1]]},
            {"id": "tiger-il:1714000-17031", "source": "tiger-il",
             "profile": "IL-0.0625-0.04-CHICAGO, IL-FD0.01", "rings": [[2, -1]]}],
        "sources": {"cdtfa": {"count": 1}, "tiger-il": {"count": 1}},
    }


def errs(mutate=None, **kw):
    d = copy.deepcopy(doc())
    if mutate:
        mutate(d)
    return validate_bounds(d, rates_doc=kw.pop("rates_doc", RATES),
                           min_by_source=kw.pop("min_by_source", MIN), **kw)


def test_a_good_document_passes():
    assert errs() == []


def test_missing_schema_key_and_wrong_version():
    assert any("missing key" in e for e in errs(lambda d: d.pop("arcs")))
    assert any("schemaVersion" in e for e in errs(lambda d: d.update(schemaVersion="2")))


def test_dangling_profile():
    def m(d):
        d["polygons"][0]["profile"] = "nope"
    assert any("unknown profile nope" in e for e in errs(m))


def test_rate_must_be_a_decimal_string_in_range():
    def as_float(d):
        d["profiles"]["CA-0.0725-0.035-SANTA MONICA, CA"]["stateRate"] = 0.0725
    assert any("must be a string" in e for e in errs(as_float))

    def too_high(d):
        d["profiles"]["CA-0.0725-0.035-SANTA MONICA, CA"]["localRate"] = "0.2"
    assert any("out of range" in e for e in errs(too_high))

    def general_too_high(d):
        p = d["profiles"]["CA-0.0725-0.035-SANTA MONICA, CA"]
        p["stateRate"], p["localRate"] = "0.09", "0.09"
    assert any("general rate out of range" in e for e in errs(general_too_high))


def test_an_illinois_profile_must_carry_a_food_drug_rate():
    """Illinois taxes qualifying food and drugs at a reduced rate in every jurisdiction, so
    a null there means the join lost it and a Chicago grocery quote would be 1.75 pt high."""
    def m(d):
        d["profiles"]["IL-0.0625-0.04-CHICAGO, IL-FD0.01"]["foodDrugRate"] = None
    assert any("foodDrugRate" in e and "IL" in e for e in errs(m))


def test_a_state_the_rates_file_does_not_know_is_an_error():
    thin = {"effectiveDate": "2026-07-01", "states": {"CA": {}}}
    assert any("no state rules for IL" in e for e in errs(rates_doc=thin))


def test_arc_index_out_of_range_and_an_open_ring():
    assert any("arc index" in e for e in errs(lambda d: d["polygons"][0].update(rings=[[9]])))

    def open_ring(d):
        # Arc 1 on its own runs three vertices and never returns to its start.
        d["polygons"][0]["rings"] = [[1]]
    assert any("does not close" in e for e in errs(open_ring))


def test_per_source_floors_and_the_sources_block():
    assert any("cdtfa" in e and "at least 2" in e
               for e in errs(min_by_source={"cdtfa": 2, "tiger-il": 1}))
    assert any("wa-dor" in e and "no polygons" in e
               for e in errs(min_by_source={"cdtfa": 1, "tiger-il": 1, "wa-dor": 1}))
    assert any("sources.cdtfa.count" in e
               for e in errs(lambda d: d["sources"]["cdtfa"].update(count=99)))


def test_gzip_size_gate():
    assert any("over the 10-byte gate" in e for e in errs(max_gz_bytes=10))
    assert errs(max_gz_bytes=1_500_000) == []


def test_effective_date_must_equal_the_rates_file_just_built():
    """Both files are stamped by the same run and published in one commit; a mismatch means
    the bounds build ran against a different quarter's rates (spec §2.3)."""
    assert any("effectiveDate" in e and "2026-10-01" in e
               for e in errs(rates_doc={**RATES, "effectiveDate": "2026-10-01"}))


def test_a_profile_shared_with_the_rates_file_must_carry_the_same_body_there():
    """The id is minted from the body, so a shared id with a different body means one side
    re-derived a field the id does not distinguish -- in the live build, `Seatac, WA` from
    `display_name` against the Census's `SeaTac, WA`. The README claims both sides of a
    jurisdiction share one id; without this the string was shared and the thing was not."""
    shared = {**RATES, "profiles": {
        "CA-0.0725-0.035-SANTA MONICA, CA": {
            "state": "CA", "label": "Santa monica, CA", "stateRate": "0.0725",
            "localRate": "0.035", "foodDrugRate": None}}}
    out = errs(rates_doc=shared)
    assert out == ["profile CA-0.0725-0.035-SANTA MONICA, CA: differs from the rates file "
                   "(label 'Santa Monica, CA' vs 'Santa monica, CA')"]
    # An id the rates file does not carry at all is not a difference: the bounds file is
    # self-contained and draws jurisdictions no ZIP row names.
    assert errs(rates_doc={**RATES, "profiles": {}}) == []


def test_a_profile_the_two_files_agree_on_is_not_reported():
    same = {**RATES, "profiles": copy.deepcopy(doc()["profiles"])}
    assert errs(rates_doc=same) == []


def test_a_polygon_with_no_rings_is_reported():
    """A polygon that encloses nothing can never answer a fix, and is as unusable as the
    open ring the geometry gate already refuses."""
    assert any("no rings" in e for e in errs(lambda d: d["polygons"][0].update(rings=[])))
    assert any("no rings" in e for e in errs(lambda d: d["polygons"][0].pop("rings")))


def test_a_bad_ring_does_not_hide_a_later_bad_ring():
    """`ok` is per ring: a polygon whose first ring dangles must still have its second ring
    checked, or a bad index would mask an open ring behind it."""
    def m(d):
        d["polygons"][0]["rings"] = [[9], [1]]
    out = errs(m)
    assert any("arc index" in e for e in out)
    assert any("does not close" in e for e in out)
