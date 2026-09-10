"""Streamlined Sales Tax rate + boundary files for 24 member states (spec §3.2)."""
from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urljoin

from pipeline.census import Census, display_name
from pipeline.http import get_cached
from pipeline.model import ZipRate
from pipeline.sources import REGISTRY, JurisdictionKey, geoid_index

MIRROR = "http://52.15.48.162/ratesandboundry"
# The mirror's per-state payloads are legitimately tiny wherever a state has one statewide
# rate and one statewide ZIP row: the live rate files run from 48 bytes (KY, MI, RI) to
# 38 kB, and the boundary files from 121 bytes to 26 MB, so `http.MIN_BODY_BYTES` -- sized
# for this pipeline's whole-document sources -- rejects real files here. This floor only has
# to catch an empty or truncated-to-nothing body; a served error page is caught downstream
# instead, by `unpack` (no `.csv` member in the archive), `parse_rate_file` + `compose` (no
# current jtype-45 row) and the builder's own per-state coverage gate. The two directory
# indexes are whole documents and keep the default floor: both run over 2 kB.
MIN_PAYLOAD_BYTES = 32
STATES = {
    "AR": "05", "GA": "13", "IA": "19", "IN": "18", "KS": "20", "KY": "21",
    "MI": "26", "MN": "27", "NC": "37", "ND": "38", "NE": "31", "NJ": "34",
    "NV": "32", "OH": "39", "OK": "40", "RI": "44", "SD": "46", "TN": "47",
    "UT": "49", "VT": "50", "WA": "53", "WI": "55", "WV": "54", "WY": "56",
}
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_FILE = re.compile(r"/([A-Z]{2})([RB])(\d{4})Q(\d)([A-Z]{3})(\d{1,2})\.(?:csv|zip)", re.I)
_HREF = re.compile(r'HREF="([^"]+)"', re.I)
OPEN_END = date(9999, 12, 31)
# A floor on the state's own share of the composed rate, for a state whose SST file does
# not put it in the state-level (jtype 45) row.
#
# Nevada files its 45 row at 0 and carries the whole 6.85 % statewide minimum in county
# rows, so every Nevada profile published a `stateRate` of "0" with 6.85-8.375 % as local:
# an app that shows the split, or lets the user override the local part, reads that as a
# state with no sales tax at all. The floor is the statutory statewide minimum -- 2 % State
# (NRS 372.105/.185) + 2.6 % Local School Support + 2.25 % Basic City-County Relief -- so
# `compose` publishes 6.85 % as the state share and only what a county levies above it as
# local. Nevada's lowest county rate is exactly 6.85 %, so no ZIP goes negative, and the
# general rate (state + local) is identical either way.
STATE_RATE_FLOOR: dict[str, Decimal] = {"NV": Decimal("0.0685")}


@dataclass(frozen=True)
class RateRow:
    jtype: int
    code: str
    general: Decimal
    food: Decimal
    begin: date
    end: date


@dataclass(frozen=True)
class ZipRow:
    zip_low: str
    zip_high: str
    county: str
    place: str
    districts: tuple[str, ...]
    begin: date
    end: date


def _rate(s: str) -> Decimal:
    """VT leaves the food/drug columns empty on some rows; an absent rate is zero."""
    return Decimal(s) if s else Decimal(0)


def _d(s: str) -> date:
    y, m, dd = int(s[:4]), int(s[4:6]), int(s[6:8])
    return OPEN_END if y >= 2999 else date(y, m, dd)


def latest_files(index_html: str, kind: str) -> dict[str, str]:
    best: dict[str, tuple[tuple[int, int, int, int], str]] = {}
    for href in _HREF.findall(index_html):
        m = _FILE.search(href)
        if not m or m.group(2).upper() != kind:
            continue
        st = m.group(1).upper()
        key = (int(m.group(3)), int(m.group(4)), _MONTHS[m.group(5).upper()], int(m.group(6)))
        if st not in best or key > best[st][0]:
            best[st] = (key, href)
    return {st: href for st, (_, href) in best.items()}


def unpack(data: bytes, name: str) -> str:
    """Decode a rate/boundary payload; several states ship a UTF-8 BOM on the first row."""
    if name.lower().endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(data))
        members = z.namelist()
        csv_name = next((n for n in members if n.lower().endswith(".csv")), None)
        if csv_name is None:
            raise ValueError(f"{name}: no .csv member in archive {members}")
        data = z.read(csv_name)
    return data.decode("utf-8-sig", "replace")


def parse_rate_file(text: str) -> list[RateRow]:
    rows = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 9 or not f[0].isdigit():
            continue
        jtype = int(f[1])
        code = f[2].zfill(3) if jtype == 0 else f[2].zfill(5) if jtype == 1 else f[2]
        rows.append(RateRow(jtype, code, _rate(f[3]), _rate(f[5]), _d(f[7]), _d(f[8])))
    return rows


def parse_boundary_zips(text: str) -> list[ZipRow]:
    out = []
    for line in text.splitlines():
        if line[:2].upper() != "Z,":  # MN ships its ZIP records as lowercase "z"
            continue
        f = line.split(",")
        if len(f) < 32:
            continue
        districts = tuple(
            f[i + 1].strip() for i in range(29, min(len(f) - 1, 29 + 60), 3) if f[i + 1].strip()
        )
        out.append(
            ZipRow(
                f[17].strip(),
                (f[19] or f[17]).strip(),
                f[24].strip(),
                f[25].strip(),
                districts,
                _d(f[1]),
                _d(f[2]),
            )
        )
    return out


def _current(rows: Iterable[RateRow], on: date) -> dict[tuple[int, str], RateRow]:
    cur: dict[tuple[int, str], RateRow] = {}
    for r in rows:
        if r.begin <= on <= r.end:
            key = (r.jtype if r.jtype in (0, 1, 45) else 2, r.code)
            cur[key] = r
    return cur


def compose(
    state: str, rates: list[RateRow], zips: list[ZipRow], census: Census, on: date
) -> list[ZipRate]:
    cur = _current(rates, on)
    st = next((r for (t, _), r in cur.items() if t == 45), None)
    if st is None:
        raise ValueError(f"{state}: no current state-level (45) rate row")
    # The published state share: the file's own 45 row, unless the state files it below the
    # statutory statewide minimum (Nevada files 0), in which case the minimum stands in and
    # the local share is what a jurisdiction levies on top of it. `general` is untouched.
    state_rate = max(st.general, STATE_RATE_FLOOR.get(state, st.general))
    best: dict[str, ZipRate] = {}
    # A Z row's range is only a hint: it can span a county line into a neighbour state
    # (or reach ZIPs that were retired), so the census's own state membership gates
    # every candidate ZIP, on both the wide and the narrow enumeration below.
    in_state = set(census.zips_in_state(STATES[state]))
    for z in zips:
        if not (z.begin <= on <= z.end):
            continue
        general, food = st.general, st.food
        if z.county and (0, z.county) in cur:
            general += cur[(0, z.county)].general
            food += cur[(0, z.county)].food
        if z.place and (1, z.place) in cur:
            general += cur[(1, z.place)].general
            food += cur[(1, z.place)].food
        for code in z.districts:
            if (2, code) in cur:
                general += cur[(2, code)].general
                food += cur[(2, code)].food
        # What the jurisdictions on this row levy above the published state share. Nevada
        # files its lowest county at exactly the 6.85 % floor, so nothing composes below it
        # today; a row that did would mean the floor is wrong for the state, and the build
        # must fail rather than publish a negative local rate.
        local_rate = general - state_rate
        if local_rate < 0:
            raise ValueError(
                f"{state}: ZIPs {z.zip_low}-{z.zip_high} (county {z.county!r}, place "
                f"{z.place!r}) compose to {general}, below the published state share "
                f"{state_rate} -- the state-rate floor is wrong for this state"
            )
        lo = z.zip_low.zfill(5)
        hi = (z.zip_high or z.zip_low).zfill(5)
        if int(hi) - int(lo) > 100:
            # IN/KY/MI/NJ/RI cover the whole state with one wide Z row; enumerating it
            # would walk thousands of numbers that are not ZIPs, so ask the census.
            candidates: Iterable[str] = (z5 for z5 in in_state if lo <= z5 <= hi)
        else:
            candidates = _zip_range(lo, hi)
        for zip5 in candidates:
            if zip5 not in in_state or zip5 not in census.centroids:
                continue
            # The Census's own casing is the label (C1). A ZIP with no place reads as its
            # county the way the Census names it, entity word and all (`King County`,
            # `Aleutians East Borough`); `display_name` re-cases only the last-ditch
            # fallback, for a ZIP the relationship files name neither a place nor a county
            # for -- which leaves the state code itself as the only label.
            name = census.place_display(zip5) or census.county_label(zip5)
            if not name:
                name = display_name(census.place_name(zip5) or census.county_name(zip5) or state)
            label = f"{name}, {state}"
            # The food/drug columns repeat the general rate where a state has no reduced
            # grocery rate; that is not a food rate, so only a genuinely lower one is kept.
            cand = ZipRate(
                zip5, state, state_rate, local_rate,
                None if food == general else food, label,
            )
            if zip5 not in best or cand.general_rate > best[zip5].general_rate:
                best[zip5] = cand
    return sorted(best.values(), key=lambda r: r.zip)


def _zip_range(lo: str, hi: str) -> Iterable[str]:
    """Enumerate a narrow, already zero-padded range; `compose` resolves `hi` and the
    wide-range split before calling this, so there is nothing left to clamp here."""
    for n in range(int(lo), int(hi) + 1):
        yield f"{n:05d}"


class SstAdapter:
    name = "sst"
    states = tuple(STATES)
    # The mirror covers 24 states, but only Nevada prices every address in a county at the
    # county's own rate with no sub-county district on top, so only Nevada's polygons can be
    # drawn from a county boundary alone (spec §2.3).
    bounds_states = ("NV",)

    def jurisdictions(self, census: Census, on: date) -> dict[JurisdictionKey, ZipRate]:
        """The counties of each `bounds_states` state, keyed by TIGER GEOID. Only the rate
        file is fetched -- the geography comes from TIGER, not from the boundary file's ZIP
        ranges -- and the state share is split at `STATE_RATE_FLOOR` exactly as `compose`
        splits it, so a Clark County polygon and a Clark County ZIP row mint one profile."""
        rate_files = latest_files(get_cached(f"{MIRROR}/Rates/").decode("utf-8", "replace"), "R")
        out: dict[JurisdictionKey, ZipRate] = {}
        for st in self.bounds_states:
            if st not in rate_files:
                print(f"[sst] {st}: no rate file on mirror, no jurisdictions to draw")
                continue
            rate_url = urljoin(f"{MIRROR}/", rate_files[st])
            rates = parse_rate_file(
                unpack(
                    get_cached(rate_url, ttl_days=7, min_bytes=MIN_PAYLOAD_BYTES),
                    rate_files[st],
                )
            )
            cur = _current(rates, on)
            state_row = next((r for (t, _), r in cur.items() if t == 45), None)
            if state_row is None:
                raise ValueError(f"{st}: no current state-level (45) rate row")
            state_rate = max(state_row.general, STATE_RATE_FLOOR.get(st, state_row.general))
            counties, _places = geoid_index(census, STATES[st])
            for county_geoid, county_label in counties.values():
                row = cur.get((0, county_geoid[2:]))
                if row is None:
                    # Every county of a bounds state has to be priced: the polygon covers
                    # the whole county, so there is no ZIP path underneath to answer for
                    # it. Without this the state share alone composes below the floor and
                    # the *state-rate-floor* error fires, naming the wrong cause entirely.
                    raise ValueError(
                        f"{st}: county {county_geoid} ({county_label}) has no current "
                        f"county (type 0) rate row in {rate_files[st]} -- every county the "
                        f"state's polygons are drawn from must have one"
                    )
                general = state_row.general + row.general
                food = state_row.food + row.food
                local_rate = general - state_rate
                if local_rate < 0:
                    raise ValueError(
                        f"{st}: county {county_geoid} ({county_label}) composes to "
                        f"{general}, below the published state share {state_rate} -- the "
                        f"state-rate floor is wrong for this state"
                    )
                out[county_geoid] = ZipRate(
                    "", st, state_rate, local_rate,
                    None if food == general else food, f"{county_label}, {st}",
                )
        return out

    def rows(self, census: Census, on: date) -> Iterable[ZipRate]:
        rate_files = latest_files(get_cached(f"{MIRROR}/Rates/").decode("utf-8", "replace"), "R")
        bound_files = latest_files(
            get_cached(f"{MIRROR}/Boundary/").decode("utf-8", "replace"), "B"
        )
        for st in self.states:
            if st not in rate_files or st not in bound_files:
                print(
                    f"[sst] {st}: missing on mirror "
                    f"(rates={st in rate_files}, boundary={st in bound_files})"
                )
                continue
            rate_url = urljoin(f"{MIRROR}/", rate_files[st])
            bound_url = urljoin(f"{MIRROR}/", bound_files[st])
            rates = parse_rate_file(
                unpack(
                    get_cached(rate_url, ttl_days=7, min_bytes=MIN_PAYLOAD_BYTES),
                    rate_files[st],
                )
            )
            zips = parse_boundary_zips(
                unpack(
                    get_cached(bound_url, ttl_days=7, min_bytes=MIN_PAYLOAD_BYTES),
                    bound_files[st],
                )
            )
            yield from compose(st, rates, zips, census, on)


REGISTRY.append(SstAdapter())
