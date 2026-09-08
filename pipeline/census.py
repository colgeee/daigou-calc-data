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

_PLACE_SUFFIX = re.compile(
    r"\s+(city|town|village|borough|CDP|municipality|city and borough|"
    r"consolidated government|metro government|urban county|metropolitan government)$",
    re.I,
)
_COUNTY_SUFFIX = re.compile(r"\s+(County|Parish|Borough|Census Area|Municipio)$", re.I)


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


def normalize_place(name: str) -> str:
    return re.sub(r"\s+", " ", _PLACE_SUFFIX.sub("", name.strip())).upper()


def county_short(name: str) -> str:
    n = name.strip()
    if re.search(r"\scity$", n, re.I):
        return re.sub(r"\s+", " ", n).upper()  # independent city (VA, MD, MO, NV)
    return re.sub(r"\s+", " ", _COUNTY_SUFFIX.sub("", n)).upper()


@dataclass
class Census:
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

    def county_fips3(self, zcta: str) -> str | None:
        c = self.county.get(zcta)
        return None if c is None else c[0][2:]

    def place_name(self, zcta: str) -> str | None:
        p = self.place.get(zcta)
        return None if p is None else normalize_place(p[1])

    def place_fips5(self, zcta: str) -> str | None:
        p = self.place.get(zcta)
        return None if p is None else p[0][2:]
