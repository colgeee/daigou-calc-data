from datetime import date
from decimal import Decimal as D
from pathlib import Path

from pipeline.census import Census
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
    assert len(files) == 14
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
    county elsewhere."""
    c = Census(
        centroids={"15132": (40.34, -79.86), "15801": (41.12, -78.76), "23185": (37.27, -76.7),
                   "17545": (40.17, -76.42)},
        county={"15132": ("42003", "Allegheny County"), "15801": ("42033", "Clearfield County"),
                "23185": ("51830", "Williamsburg city"), "17545": ("42071", "Lancaster County")},
        place={"15132": ("4245728", "McKeesport city"), "15801": ("4219432", "DuBois city")},
    )
    rows = {r.zip: r for r in yaml_states.YamlStatesAdapter().rows(c, date(2026, 9, 8))}
    assert rows["15132"].label == "McKeesport, PA"
    assert rows["15801"].label == "DuBois, PA"
    assert rows["23185"].label == "Williamsburg, VA"  # an independent city is a county
    assert rows["17545"].label == "Lancaster, PA"          # no place: the county


def test_state_fips_map_covers_all_yaml_states():
    for f in RULES.glob("*.yaml"):
        assert f.stem.upper() in yaml_states.FIPS
