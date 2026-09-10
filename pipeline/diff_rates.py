"""Compare the rates file just built against the one currently published (spec §2.7,
decision #69).

Every source this pipeline reads can change shape without changing status code -- a CDTFA
export with no rows, a Pub 718 whose text layout shifted, an IDOR file whose columns moved.
The per-source floors catch a collapse; this catches the subtler failure, where a join
half-breaks and thousands of ZIPs quietly move to their county's rate. The thresholds are
sized so a real quarter passes: the largest single legitimate move on record is Hawaii's
Maui correction at 0.712 pp, and 97 Hawaii ZIPs are 0.29% of the table.

`--allow-swings` turns a failure into a printed, on-the-record warning. It is a
`workflow_dispatch` boolean, empty on the schedule, so the quarterly run is always strict."""
from __future__ import annotations

import gzip
import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pipeline.http import get_cached

MAX_CHANGED_SHARE = 0.05
MAX_MOVE = Decimal("0.02")
MAX_COUNT_DROP = 0.01
MAX_STATE_DROP = 0.05
# A published rates.json runs ~9 MB pretty / ~610 KB gzipped, so `http.MIN_BODY_BYTES`
# would wave a truncated Pages response straight through.
MIN_PUBLISHED_BYTES = 100_000


def _pp(d: Decimal) -> str:
    """A rate difference as percentage points, e.g. `Decimal("0.00712")` -> `"0.712"`."""
    return format((d * 100).normalize(), "f")


def _index(doc: dict) -> dict[str, tuple[str, Decimal]]:
    """`{zip: (state, general rate)}`, read through the document's own profile map so a
    ZIP's rate is whatever the app would show for it."""
    profiles = doc["profiles"]
    out: dict[str, tuple[str, Decimal]] = {}
    for row in doc["zips"]:
        p = profiles[row[3]]
        out[row[0]] = (p["state"], Decimal(p["stateRate"]) + Decimal(p["localRate"]))
    return out


@dataclass(frozen=True)
class DiffReport:
    added: list[str]
    dropped: list[str]
    changed: list[tuple[str, Decimal, Decimal]]        # zip, old general, new general
    old_count: int
    new_count: int
    per_state_old: dict[str, int]
    per_state_new: dict[str, int]

    def _moves(self) -> list[tuple[str, Decimal, Decimal]]:
        """The changed ZIPs, largest absolute move first."""
        return sorted(self.changed, key=lambda c: (abs(c[2] - c[1]), c[0]), reverse=True)

    def lines(self) -> list[str]:
        share = len(self.changed) / self.old_count if self.old_count else 0.0
        out = [
            f"[diff-rates] {self.old_count} ZIPs published, {self.new_count} built",
            f"[diff-rates] {len(self.added)} added, {len(self.dropped)} dropped, "
            f"{len(self.changed)} ZIPs changed general rate ({share:.2%})",
        ]
        moves = self._moves()[:10]
        if moves:
            out.append("[diff-rates] largest moves:")
            for z, a, b in moves:
                out.append(f"    {z}  {_pp(a)}% -> {_pp(b)}%  ({_pp(b - a)} pp)")
        out.append(f"[diff-rates] {'state':<7}{'published':>11}{'built':>9}{'delta':>9}")
        for st in sorted(set(self.per_state_old) | set(self.per_state_new)):
            was, now = self.per_state_old.get(st, 0), self.per_state_new.get(st, 0)
            out.append(f"[diff-rates] {st:<7}{was:>11}{now:>9}{now - was:>+9}")
        return out

    def failures(self) -> list[str]:
        out: list[str] = []
        if self.old_count:
            share = len(self.changed) / self.old_count
            if share > MAX_CHANGED_SHARE:
                out.append(
                    f"{len(self.changed)} of {self.old_count} ZIPs ({share:.2%}) changed "
                    f"general rate, over the {MAX_CHANGED_SHARE:.0%} ceiling"
                )
        for z, a, b in self._moves():
            if abs(b - a) > MAX_MOVE:
                out.append(f"{z} moved {_pp(a)}% -> {_pp(b)}%, {_pp(abs(b - a))} pp, over the "
                           f"{_pp(MAX_MOVE)} pp ceiling")
        if self.old_count and self.new_count < self.old_count:
            drop = (self.old_count - self.new_count) / self.old_count
            if drop > MAX_COUNT_DROP:
                out.append(
                    f"the ZIP count fell from {self.old_count} to {self.new_count} "
                    f"({drop:.2%}), over the {MAX_COUNT_DROP:.0%} ceiling"
                )
        for st in sorted(self.per_state_old):
            was, now = self.per_state_old[st], self.per_state_new.get(st, 0)
            if now == 0:
                out.append(f"{st} vanished: all {was} of its ZIPs are gone")
            elif (was - now) / was > MAX_STATE_DROP:
                out.append(
                    f"{st} lost {was - now} of its {was} ZIPs "
                    f"({(was - now) / was:.2%}), over the {MAX_STATE_DROP:.0%} ceiling"
                )
        return out


def load_published(ref: str) -> dict:
    """Read the currently published rates document. `ref` is `git:<rev>:<path>` (what CI
    passes, where the checkout already has full history), an `http(s)://` URL, or a local
    path. A gzipped body is unwrapped either way."""
    if ref.startswith("git:"):
        _, rev, path = ref.split(":", 2)
        proc = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"git show {rev}:{path} failed with exit {proc.returncode}: {err}")
        body = proc.stdout
    elif ref.startswith(("http://", "https://")):
        # ttl_days=0 so a rerun always sees what is published right now, and a floor sized
        # for a whole rates file rather than `http.MIN_BODY_BYTES`.
        body = get_cached(ref, ttl_days=0, min_bytes=MIN_PUBLISHED_BYTES)
    else:
        body = Path(ref).read_bytes()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def diff(old: dict, new: dict) -> DiffReport:
    o, n = _index(old), _index(new)
    return DiffReport(
        added=sorted(set(n) - set(o)),
        dropped=sorted(set(o) - set(n)),
        changed=[(z, o[z][1], n[z][1]) for z in sorted(set(o) & set(n)) if o[z][1] != n[z][1]],
        old_count=len(o),
        new_count=len(n),
        per_state_old=dict(Counter(st for st, _ in o.values())),
        per_state_new=dict(Counter(st for st, _ in n.values())),
    )


def main(out_dir: str, published: str, *, allow_swings: bool = False) -> int:
    new = json.loads((Path(out_dir) / "v1" / "rates.json").read_text(encoding="utf-8"))
    report = diff(load_published(published), new)
    text = "\n".join(report.lines())
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    fails = report.failures()
    if not fails:
        return 0
    if allow_swings:
        print("[diff-rates] WARNING — published anyway because --allow-swings was passed:")
    else:
        print("[diff-rates] REFUSING TO PUBLISH — the built rates file swings too far from "
              "the published one:")
    for f in fails:
        print("   ", f)
    return 0 if allow_swings else 1
