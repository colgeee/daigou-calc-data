"""The polygon overlay build: `v1/bounds.json.gz` (spec §2.3)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.model import ZipRate, profile_id, rate_str


@dataclass(frozen=True)
class Collected:
    """What one source hands the builder: a WGS84 GeoJSON FeatureCollection whose features
    carry `id`, `source` and `profile`; the profiles those ids name, in the rates file's
    own shape; and the per-source bookkeeping the document's `sources` block prints."""

    path: Path
    profiles: dict[str, dict]
    notes: dict[str, dict]


def profile_entry(r: ZipRate) -> tuple[str, dict]:
    """The bounds file's own copy of a profile and the id `model.profile_id` mints for it.

    The bounds file carries its own profiles rather than pointing at the rates file's
    (spec §2.3, decision #63): an id embeds the rate and the label, so two files refreshed
    on different weeks would dangle. Minting through `profile_id` on a `ZipRate`-shaped
    record is what makes a polygon's id byte-identical to the ZIP row's for the same
    jurisdiction -- the property Task 5's tests pin."""
    return profile_id(r), {
        "state": r.state,
        "label": r.label,
        "stateRate": rate_str(r.state_rate),
        "localRate": rate_str(r.local_rate),
        "foodDrugRate": None if r.food_drug_rate is None else rate_str(r.food_drug_rate),
    }
