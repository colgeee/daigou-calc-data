"""Census geography: ZCTA centroids and ZCTA -> county / place, keyed for every adapter."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

from pipeline.http import get_cached

GAZ_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip"
COUNTY_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"
PLACE_URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_place20_natl.txt"

# Every state + DC keyed to its 2-digit FIPS, the prefix `zips_in_state` matches on. The
# builder sizes each state's ZIP universe with it for the per-state coverage gate.
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
    "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}

## Census LSAD descriptor words are their own fixed casing -- almost always lowercase
# (``city``, ``town``, ``metro government``, ...), with ``CDP`` the sole uppercase
# exception -- and that casing is never the casing of a proper name that happens to end
# in the same word (Carson City, NV's place and county NAMELSAD is literally ``Carson
# City``: the capitalised ``City`` is part of the name, not an LSAD suffix). So every
# strip below matches the cased Census string exactly rather than case-insensitively --
# a case-insensitive match would treat ``Carson City`` as ``Carson`` + a stripped ``City``
# suffix and mislabel ZIPs 89701/89703 as plain ``Carson, NV``.
_PLACE_SUFFIX = re.compile(
    r"\s+(city|town|village|borough|CDP|municipality|city and borough|"
    r"consolidated government|metro government|urban county|metropolitan government|"
    r"unified government|corporation)$"
)
_COUNTY_SUFFIX = re.compile(r"\s+(County|Parish|Borough|Census Area|Municipio)$")
# Label-only cleanup the join key must not get (F1/F2): a Census `NAMELSAD` sometimes
# parenthesises which part of a consolidated city-county government a record covers --
# ``Indianapolis city (balance)`` -- and Alaska's independent boroughs spell their type
# word as a two-word phrase `_COUNTY_SUFFIX` cannot anchor a single trailing word on.
_BALANCE_SUFFIX = re.compile(r"\s+\(balance\)$")
_CITY_AND_BOROUGH_SUFFIX = re.compile(r"\s+City and Borough$")


def parse_gazetteer(text: str) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        row = {k.strip(): (v or "").strip() for k, v in row.items()}
        out[row["GEOID"]] = (float(row["INTPTLAT"]), float(row["INTPTLONG"]))
    return out


def parse_relationship(text: str, geoid_col: str, name_col: str) -> dict[str, tuple[str, str]]:
    best: dict[str, tuple[int, str, str]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        z = (row.get("GEOID_ZCTA5_20") or "").strip()
        if not z:
            continue
        area = int(row.get("AREALAND_PART") or 0)
        cur = best.get(z)
        if cur is None or area > cur[0]:
            best[z] = (area, row[geoid_col].strip(), row[name_col].strip())
    return {z: (g, n) for z, (_, g, n) in best.items()}


def _place_short(name: str) -> str:
    """Strip the place-type suffix, keeping the relationship file's own casing:
    ``McKinney city`` -> ``McKinney``, ``Cañon City city`` -> ``Cañon City``. The strip is
    case-sensitive on `_PLACE_SUFFIX`'s own casing (lowercase ``city``, uppercase ``CDP``,
    ...), so a proper name that merely ends in a capitalised ``City`` -- ``Carson City``'s
    own NAMELSAD, with no separate LSAD word appended -- is left untouched rather than
    read as ``Carson`` + a stripped ``City`` suffix."""
    return re.sub(r"\s+", " ", _PLACE_SUFFIX.sub("", name.strip()))


def _county_short(name: str) -> str:
    """Strip the county-type suffix, keeping the relationship file's own casing:
    ``DuPage County`` -> ``DuPage``, ``Fairfax city`` -> ``Fairfax city``. The lowercase
    ``city`` LSAD word is matched case-sensitively, same as `_place_short`: it never
    fires on a proper name that merely ends in a capitalised ``City`` (``Carson City``),
    which instead falls through to `_COUNTY_SUFFIX` untouched, since none of its
    alternatives match ``City`` either."""
    n = name.strip()
    if re.search(r"\scity$", n):
        return re.sub(r"\s+", " ", n)  # independent city (VA, MD, MO, NV)
    return re.sub(r"\s+", " ", _COUNTY_SUFFIX.sub("", n))


def normalize_place(name: str) -> str:
    """The uppercase join name of a place. `Census.place_display` returns the same name
    with the Census file's own casing kept, which is what a label wants."""
    return _place_short(name).upper()


def county_short(name: str) -> str:
    """The uppercase join name of a county. `Census.county_display` returns the same name
    with the Census file's own casing kept, which is what a label wants."""
    return _county_short(name).upper()


def join_key(name: str) -> str:
    """Key a jurisdiction name for joining a state's rate file to the Census gazetteer,
    tolerant of the spelling variants the two sources disagree on for the same place
    (F1). Uppercases, expands a leading ``ST``/``ST.`` to ``SAINT``, drops periods, then
    strips all remaining whitespace so word-spacing differences never matter. Live cases
    it settles: Census ``DESOTO`` vs the Texas Comptroller's ``DE SOTO`` (75115), Census
    ``ST. HEDWIG`` vs its ``SAINT HEDWIG`` (78152), and Census ``ST. CLAIR`` vs the
    Illinois file's ``SAINT CLAIR COUNTY`` row."""
    n = re.sub(r"^ST\.?\s+", "SAINT ", name.strip().upper())
    return re.sub(r"\s+", "", n.replace(".", ""))


def display_name(name: str) -> str:
    """Re-case a label that reaches an adapter already uppercased -- a rate file's own
    jurisdiction name, or the fallback for a ZIP the Census relationship files do not
    name. Where the Census does name it, `Census.county_display`/`place_display` carry
    the real casing and are used instead (C1).

    Title-cases, then fixes what `.title()` mangles:
    a leading ``Mc`` wants its next letter capitalised (``Mcintosh`` -> ``McIntosh``,
    ``Mckinney`` -> ``McKinney``, and after a hyphen too: ``Candler-Mcafee`` ->
    ``Candler-McAfee``), and the ``Afb`` token is an acronym (``Mcconnell Afb`` ->
    ``McConnell AFB``). ``Mac...`` words (``Macon``), apostrophes (``O'Fallon``) and
    multi-word names (``Pend Oreille``, ``De Kalb``) are already correct after
    `.title()` and are left alone."""

    def fix(token: str) -> str:
        if token.upper() == "AFB":
            return "AFB"
        if len(token) > 2 and token[:2] == "Mc":
            return "Mc" + token[2].upper() + token[3:]
        return token

    return " ".join(
        "-".join(fix(part) for part in word.split("-")) for word in name.title().split(" ")
    )


@dataclass
class Census:
    """`county` and `place` map a ZCTA to ``(GEOID, NAMELSAD)``, the relationship file's
    name kept exactly as it is published -- ``DuPage County``, ``O'Fallon city``,
    ``McKinney city``, ``Cañon City city``. `county_name`/`place_name` fold that to the
    uppercase name the rate-file joins key on; `county_display`/`place_display` keep the
    casing, which is what every adapter's label uses (C1), and additionally drop the
    Census entity phrasing a join key must keep untouched -- a trailing ``(balance)``,
    ``unified government``, ``City and Borough`` and the like (F1/F2). Re-casing an
    uppercased name with `display_name` is the fallback for a ZIP the relationship files
    do not name."""

    centroids: dict[str, tuple[float, float]]
    county: dict[str, tuple[str, str]]
    place: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def fetch(cls) -> Census:
        gz = zipfile.ZipFile(io.BytesIO(get_cached(GAZ_URL, ttl_days=30)))
        gaz_text = gz.read(gz.namelist()[0]).decode("utf-8")
        return cls(
            centroids=parse_gazetteer(gaz_text),
            county=parse_relationship(
                get_cached(COUNTY_URL, ttl_days=30).decode("utf-8-sig"),
                "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20",
            ),
            place=parse_relationship(
                get_cached(PLACE_URL, ttl_days=30).decode("utf-8-sig"),
                "GEOID_PLACE_20", "NAMELSAD_PLACE_20",
            ),
        )

    def zips_in_state(self, state_fips: str) -> list[str]:
        return sorted(z for z, (geoid, _) in self.county.items() if geoid.startswith(state_fips))

    def county_name(self, zcta: str) -> str | None:
        c = self.county.get(zcta)
        return None if c is None else county_short(c[1])

    def county_display(self, zcta: str) -> str | None:
        """`county_name`'s short form with the Census file's casing kept, but shaped for
        a label rather than a join key (F2): an independent city drops its trailing
        lowercase ` city` LSAD word (``Williamsburg city`` -> ``Williamsburg``;
        `county_short`, the join key, keeps it, which is what tells Fairfax city apart
        from Fairfax County), and Alaska's ``... City and Borough`` entities drop that
        whole phrase (``Juneau City and Borough`` -> ``Juneau``). The ` city` strip is
        case-sensitive, so a proper name that merely ends in a capitalised ``City``
        (``Carson City``, NV's own NAMELSAD) is left alone. Otherwise strips the same
        `_COUNTY_SUFFIX` as `county_short`: ``DuPage``, ``DeSoto``, ``LaSalle``,
        ``St. Clair``."""
        c = self.county.get(zcta)
        if c is None:
            return None
        n = _CITY_AND_BOROUGH_SUFFIX.sub("", c[1].strip())
        n = re.sub(r"\s+city$", "", n)
        return re.sub(r"\s+", " ", _COUNTY_SUFFIX.sub("", n))

    def county_fips3(self, zcta: str) -> str | None:
        c = self.county.get(zcta)
        return None if c is None else c[0][2:]

    def place_name(self, zcta: str) -> str | None:
        p = self.place.get(zcta)
        return None if p is None else normalize_place(p[1])

    def place_display(self, zcta: str) -> str | None:
        """`place_name` with the Census file's casing kept, plus label-only cleanup the
        join key does not get (F1): a trailing ` (balance)` is dropped, then any trailing
        entity phrase `_PLACE_SUFFIX` covers, and a name split by a `/` (a jointly-named
        city/county government) keeps only the part before it. ``McKinney city`` ->
        ``McKinney``, ``O'Fallon`` -> ``O'Fallon``, ``DeKalb`` -> ``DeKalb``,
        ``Cañon City`` -> ``Cañon City``, ``Indianapolis city (balance)`` ->
        ``Indianapolis``, ``Louisville/Jefferson County metro government (balance)`` ->
        ``Louisville``."""
        p = self.place.get(zcta)
        if p is None:
            return None
        name = _place_short(_BALANCE_SUFFIX.sub("", p[1].strip()))
        return name.split("/", 1)[0].strip() if "/" in name else name

    def place_fips5(self, zcta: str) -> str | None:
        p = self.place.get(zcta)
        return None if p is None else p[0][2:]
