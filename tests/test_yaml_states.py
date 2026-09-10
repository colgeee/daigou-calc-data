from datetime import date
from decimal import Decimal as D
from pathlib import Path

from pipeline.census import Census
from pipeline.model import profile_id
from pipeline.sources import yaml_states

RULES = Path("pipeline/rules/states")


def census():
    return Census(
        centroids={
            "19103": (39.95, -75.16), "15222": (40.44, -79.99), "17601": (40.07, -76.31),
            "39201": (32.3, -90.18), "22201": (38.88, -77.09), "23185": (37.27, -76.7),
        },
        county={
            "19103": ("42101", "Philadelphia County"), "15222": ("42003", "Allegheny County"),
            "17601": ("42071", "Lancaster County"), "39201": ("28049", "Hinds County"),
            "22201": ("51013", "Arlington County"), "23185": ("51830", "Williamsburg city"),
        },
        place={
            "19103": ("4260000", "Philadelphia city"), "39201": ("2836000", "Jackson city"),
            "22201": ("5103000", "Arlington CDP"),
        },
    )


def test_every_yaml_loads_and_has_required_fields():
    files = sorted(RULES.glob("*.yaml"))
    assert len(files) == 23   # 15 flat/regional (Guam included) + 8 state-rate-only (decision #26)
    for f in files:
        t = yaml_states.load_state(f)
        assert t.state == f.stem.upper() and D("0") <= t.state_rate <= D("0.08"), f
        locals_ = list(t.county_rates.values()) + list(t.place_rates.values())
        assert all(D("0") <= v <= D("0.03") for v in locals_), f


def test_pa_county_rates_and_default():
    all_rows = yaml_states.YamlStatesAdapter().rows(census(), date(2026, 9, 8))
    rows = {r.zip: r for r in all_rows if r.state == "PA"}
    assert rows["19103"].local_rate == D("0.02") and rows["19103"].label == "Philadelphia, PA"
    assert rows["15222"].local_rate == D("0.01")
    assert rows["17601"].local_rate == D("0") and rows["17601"].state_rate == D("0.06")


def test_ms_place_rate_and_va_regions():
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(census(), date(2026, 9, 8))}
    assert rows["39201"].local_rate == D("0.01")            # Jackson MS
    assert rows["22201"].local_rate == D("0.007")           # Arlington VA
    assert rows["23185"].local_rate == D("0.017")           # Williamsburg VA (independent city)


def test_labels_keep_the_census_casing():
    """C1: `McKeesport`, `DuBois` and the independent city `Williamsburg` all come out of
    the Census with their own casing; `.title()` on the uppercased join name gave
    `Mckeesport` and `Dubois`. A ZIP the place file does not name reads as its county.
    Williamsburg's trailing ` city` marker is dropped for the label (F2); the join key
    that priced it still carries it, which is what keeps it apart from a same-named
    county elsewhere. A ZIP with no place reads as its county the way the Census names
    it, entity word and all (F5) -- `Lancaster County`, not the bare `Lancaster` this
    adapter used to publish, and `Acadia Parish` rather than a Louisiana parish miscalled
    a county."""
    c = Census(
        centroids={"15132": (40.34, -79.86), "15801": (41.12, -78.76), "23185": (37.27, -76.7),
                   "17545": (40.17, -76.42), "70518": (30.1, -92.0)},
        county={"15132": ("42003", "Allegheny County"), "15801": ("42033", "Clearfield County"),
                "23185": ("51830", "Williamsburg city"), "17545": ("42071", "Lancaster County"),
                "70518": ("22001", "Acadia Parish")},
        place={"15132": ("4245728", "McKeesport city"), "15801": ("4219432", "DuBois city")},
    )
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(c, date(2026, 9, 8))}
    assert rows["15132"].label == "McKeesport, PA"
    assert rows["15801"].label == "DuBois, PA"
    assert rows["23185"].label == "Williamsburg, VA"  # an independent city is a county
    assert rows["17545"].label == "Lancaster County, PA"    # no place: the county
    assert rows["70518"].label == "Acadia Parish, LA"       # a parish, not a "County"


def test_state_fips_map_covers_all_yaml_states():
    for f in RULES.glob("*.yaml"):
        assert f.stem.upper() in yaml_states.FIPS
    assert len(yaml_states.YamlStatesAdapter().states) == 23


def test_state_rate_only_states_publish_the_state_rate_and_no_local():
    """Decision #26: the eight states with no local-rate source still get a row per ZIP, at
    the state rate with `local_rate` 0 and no food rate, so the app can place the ZIP and
    flag it as `localCoverage: false` rather than failing with "couldn't get a location"."""
    c = Census(
        centroids={"80202": (39.75, -104.99), "99501": (61.21, -149.87)},
        county={"80202": ("08031", "Denver County"),
                "99501": ("02020", "Anchorage Municipality")},
        place={"80202": ("0820000", "Denver city"), "99501": ("0203000", "Anchorage municipality")},
    )
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(c, date(2026, 9, 8))}
    assert rows["80202"].state == "CO"
    assert rows["80202"].state_rate == D("0.029")
    assert rows["80202"].local_rate == D("0")
    assert rows["80202"].food_drug_rate is None
    assert rows["80202"].label == "Denver, CO"
    assert rows["99501"].state == "AK"
    assert rows["99501"].state_rate == D("0")     # Alaska levies no state sales tax
    assert rows["99501"].local_rate == D("0")
    assert rows["99501"].food_drug_rate is None


def test_pass_on_split_grosses_up_once_on_the_combined_rate():
    """Hawaii's GET is on the seller; a retailer may pass it on at the grossed-up maximum
    pass-on rate, which is what a Honolulu receipt adds (spec §2.6, decision #67). The
    gross-up is done once on the combined rate so the two published parts sum to the
    official figure: 0.041885 + 0.005235 = 0.047120, DoTax's own 4.712%."""
    state, local = yaml_states.pass_on_split(D("0.04"), D("0.005"))
    assert (state, local) == (D("0.041885"), D("0.005235"))
    assert state + local == D("0.047120")
    # Kalawao adopted no surcharge: 4% grossed up on its own is 4.1667%.
    assert yaml_states.pass_on_split(D("0.04"), D("0")) == (D("0.041667"), D("0"))


def test_hawaii_publishes_the_pass_on_rate_for_every_county_including_maui():
    c = Census(
        centroids={"96813": (21.31, -157.85), "96793": (20.88, -156.47),
                   "96742": (21.19, -156.98), "96720": (19.71, -155.09)},
        county={"96813": ("15003", "Honolulu County"), "96793": ("15009", "Maui County"),
                "96742": ("15005", "Kalawao County"), "96720": ("15001", "Hawaii County")},
        place={"96813": ("1571550", "Honolulu CDP"), "96793": ("1571850", "Wailuku CDP")},
    )
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(c, date(2026, 9, 9))}
    assert (rows["96813"].state_rate, rows["96813"].local_rate) == (D("0.041885"), D("0.005235"))
    # Maui adopted 0.5% on 2024-01-01; the file used to say it had none.
    assert (rows["96793"].state_rate, rows["96793"].local_rate) == (D("0.041885"), D("0.005235"))
    assert (rows["96742"].state_rate, rows["96742"].local_rate) == (D("0.041667"), D("0"))
    assert rows["96720"].general_rate == D("0.047120")
    assert rows["96813"].label == "Honolulu, HI"


def test_guam_rows_are_zero_rated_and_all_read_guam():
    """Spec §2.6, decision #68: one taxing authority, no consumer sales tax. The `label`
    override makes every row read `Guam` rather than `Merizo, GU`, so one profile covers
    the territory and the polygon carries the same label."""
    c = Census(
        centroids={"96910": (13.45, 144.75), "96929": (13.56, 144.87)},
        county={"96910": ("66010", "Guam"), "96929": ("66010", "Guam")},
        place={"96910": ("6634370", "Hagatna village")},
    )
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(c, date(2026, 9, 9))}
    assert rows["96910"].state == "GU"
    assert rows["96910"].state_rate == D("0") and rows["96910"].local_rate == D("0")
    assert rows["96910"].label == rows["96929"].label == "Guam"
    assert profile_id(rows["96910"]) == "GU-0-0-GUAM"


def test_guam_yaml_carries_the_bounds_rectangle_task_6_reads():
    t = yaml_states.load_state(RULES / "gu.yaml")
    assert t.bounds == (13.1, 13.75, 144.5, 145.1)
    assert t.label == "Guam" and t.pass_on is False
    assert yaml_states.load_state(RULES / "hi.yaml").bounds is None
