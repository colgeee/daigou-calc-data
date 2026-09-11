"""Turns rules/categories.yaml into the dataset's `states` map."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from pipeline.model import rule

PATH = Path(__file__).resolve().parent / "rules" / "categories.yaml"
# States where an omitted grocery/prescription falls back to the per-ZIP food/drug rate instead of
# the general rate (app decision #22).  An explicit rule in the YAML always wins over that rate.
# IL is half an exception: its omitted `prescription` does take that rate (IDOR's Drug & Medical
# column IS Illinois' medicine rate), but its omitted `grocery` takes it only until the app's
# resolver ladder lands -- via foodDrugRate today, via the per-ZIP `groceryRate` after that,
# which is why no explicit rule is written for it (rules/categories.yaml note 1; app decision #92).
SST_LIKE = {"AR", "GA", "IA", "IN", "KS", "KY", "MI", "MN", "NC", "ND", "NE", "NJ", "NV", "OH",
            "OK", "RI", "SD", "TN", "UT", "VT", "WA", "WI", "WV", "WY", "IL"}
CATS = ("grocery", "clothing", "prescription", "supplement", "alcohol")
GENERAL = {"t": "general"}


def _rule(spec: object) -> dict:
    if isinstance(spec, str):
        return rule(spec)
    assert isinstance(spec, dict) and len(spec) == 1, spec
    (t, arg), = spec.items()
    if t == "threshold":
        return rule("threshold", limit=str(arg["limit"]), above=_rule(arg.get("above", "general")))
    key = "extra" if t == "surcharge" else "rate"
    return rule(t, **{key: Decimal(str(arg))})


def load(path: Path = PATH) -> dict[str, dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults, states = doc["defaults"], doc["states"]
    out: dict[str, dict] = {}
    for code, entry in states.items():
        entry = entry or {}
        rules: dict[str, dict] = {}
        for cat in CATS:
            if cat in entry:
                # An explicit rule is kept even when it is `general`: it beats the per-ZIP rate.
                rules[cat] = _rule(entry[cat])
            elif cat in defaults and not (code in SST_LIKE and cat in ("grocery", "prescription")):
                r = _rule(defaults[cat])
                if r != GENERAL:  # a defaulted `general` is what an absent category already means
                    rules[cat] = r
        conf = {**defaults.get("confidence", {}), **(entry.get("confidence") or {})}
        out[code] = {"localCoverage": bool(entry.get("localCoverage", True)),
                     "rules": rules,
                     "confidence": conf}
    return out
