"""A repo-wide guard: a test that reaches the network fails, loudly, naming the fetch.

**The bug this exists for.** `pipeline/sources/il.py` grew a second IDOR fetch --
`_fetch_grocery_text`, alongside the existing `_fetch_text` -- and three tests in
`test_jurisdictions.py` went on patching only the first. They did not error. They priced a
*synthetic* ordinance table against the **live** grocery file, pulled over the network, and
produced perfectly plausible profile ids mixing a fixture local rate with whatever IDOR
publishes today. It surfaced only because those ids happened to miss an assertion; a looser
one and the suite would have stayed green while quietly depending on the network and on a
third party's current data. The per-test fix -- `test_jurisdictions.py`'s `_il_grocery`
fixture -- is opt-in, so the next Illinois test added there re-creates the hole. This file is
the part that is not opt-in.

**Where it binds.** Every network read in this repo goes through `get_cached` or `get_polled`
in `pipeline/http.py`, and every caller reaches them by `from pipeline.http import ...`, which
copies the function object into the caller's own namespace at import time. Patching
`pipeline.http.get_cached` alone would therefore bind nothing at all: `pipeline.sources.il`
would still be holding its own reference to the real one. So the guard patches *every module's
own reference*, found by walking the `pipeline` package rather than read from a list kept here
-- a list kept here is the same kind of object as the opt-in fixture, one more thing a new
source module has to be remembered into. Under the sweep sits `requests.get` itself, the
single call `pipeline/http.py` makes, so a third fetch helper added to that file is covered on
the day it is written rather than the day someone remembers to update this one.

**Why `pytest.fail` and not `raise`.** `pipeline/bounds/build.py` wraps the whole build in
`except Exception` and prints REFUSING TO WRITE, deliberately, because a traceback is the
wrong answer when a source file shifts a column. An ordinary exception raised from here would
be swallowed by that handler into a quiet non-zero return -- the silent success this guard
exists to end. `pytest.fail` raises a `BaseException`, which walks straight out through it.
Blocking at the socket instead (pytest-socket, or a `socket.socket` stub) was rejected for the
standing guard: it costs a dependency or a monkeypatched stdlib, and its failure names neither
the URL nor the module, which is the whole of what the next person needs to see. A socket stub
is the right tool for the one thing this file cannot prove about itself -- that the suite owes
the network nothing -- and that is how the guard was accepted: a full run with every socket
dead and `PIPELINE_CACHE_DIR` pointed at an empty directory, so not even a warm cache entry
could stand in for a fetch. Re-run it that way if you ever doubt this file is still earning
its place.

**Opting out.** `@pytest.mark.no_network_guard` stands the guard aside for a test that drives
the fetch machinery itself with its own fakes. `tests/test_http.py` is the only such file in
the suite -- verified by spying on every binding below across a full run, not assumed. No
test here deliberately hits the live network, and none can be added without saying so in that
marker: the suite is required to pass with the machine offline.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Callable
from types import ModuleType

import pytest

import pipeline
from pipeline import http

# Captured at import, before any test can patch them, so the sweep recognises a module's
# imported reference by identity on every test rather than only on the first one.
_FETCHERS = {name: getattr(http, name) for name in ("get_cached", "get_polled")}

# The sweep *is* the guard: if it silently came back empty -- the helpers renamed, the package
# laid out differently, `walk_packages` handed a path it cannot read -- every test in this
# suite would go on passing while fetching freely, which is exactly the failure this file
# exists to end. The live tree binds 13 (eleven caller modules, plus `pipeline.http`'s own two
# definitions); a floor well under that catches a sweep that broke without pinning a number
# that every new source module has to be edited past.
MIN_BINDINGS = 8


def _reraise(name: str) -> None:
    """`walk_packages` swallows an `ImportError` raised while importing a package to recurse
    into it, which would leave a whole subpackage unguarded and say nothing. Re-raise it: a
    guard that covers less than it claims to is worse than no guard."""
    raise


def _bindings() -> list[tuple[ModuleType, str]]:
    """Every `(module, attribute)` pair in the `pipeline` package that names one of the two
    fetchers -- `pipeline.http`'s own definitions included, since a caller may equally reach
    them as `http.get_cached`. Importing the whole package first is the point: a source module
    no test has imported yet still gets its reference patched, and one added tomorrow is
    covered without an edit here."""
    for info in pkgutil.walk_packages(pipeline.__path__, f"{pipeline.__name__}.", _reraise):
        # `pipeline.__main__` holds no fetcher and is the module `python -m pipeline` already
        # runs under the name `__main__`; importing a second copy of it here buys nothing.
        if not info.name.endswith(".__main__"):
            importlib.import_module(info.name)
    found = []
    for name, mod in sorted(sys.modules.items()):
        if mod is None or not (name == "pipeline" or name.startswith("pipeline.")):
            continue
        found += [(mod, attr) for attr, fn in _FETCHERS.items()
                  if getattr(mod, attr, None) is fn]
    if len(found) < MIN_BINDINGS:
        raise RuntimeError(
            f"tests/conftest.py: the network guard found only {len(found)} fetch bindings in "
            f"the pipeline package, expected at least {MIN_BINDINGS} -- it would be guarding "
            f"almost nothing. Did `pipeline.http` rename get_cached/get_polled?"
        )
    return found


_BINDINGS = _bindings()

# What to do about it, said once each. The wrapper clause in the first carries its weight:
# the Illinois tests patch `il._fetch_text`, not `il.get_cached`, and patching the wrapper is
# usually the readable fix -- it is also precisely what let the second fetch slip through.
_PATCH_IT = (
    "Patch that name -- or the wrapper around it, the way the Illinois tests patch "
    "`il._fetch_text` -- for this test. A source module that has grown a SECOND fetch needs a "
    "second patch, and that is the bug this guard exists for: see tests/conftest.py's "
    "docstring. A test that drives the fetch machinery itself carries "
    "@pytest.mark.no_network_guard."
)
_NEW_HELPER = (
    "That is the floor under the per-module sweep: something in pipeline/http.py reached the "
    "network without going through `get_cached` or `get_polled`, so it is a fetch helper this "
    "file does not yet know to patch in its callers. Add it to `_FETCHERS` above and patch "
    "the caller for this test -- or carry @pytest.mark.no_network_guard if the test under it "
    "is pipeline/http.py's own."
)


def _refuse(where: str, advice: str) -> Callable[..., bytes]:
    """A stand-in for one fetch entry point, failing with the two facts the next person needs
    and cannot get from a stack trace alone: which binding was still live, and for what URL."""
    def guarded(url: str, *_args: object, **_kwargs: object) -> bytes:
        pytest.fail(f"unpatched network fetch: {where} asked for {url}\n{advice}")

    return guarded


@pytest.fixture(autouse=True)
def _no_unpatched_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Autouse, so a test costs nothing to protect and has to say so to escape.

    It runs before the fixtures a test asks for by name -- pytest orders autouse fixtures
    first within a scope -- so a test that patches `sst.get_cached` itself still wins, and a
    test that patches only some of a module's fetches is left holding this failure for the
    rest."""
    if request.node.get_closest_marker("no_network_guard"):
        return
    for mod, attr in _BINDINGS:
        monkeypatch.setattr(mod, attr, _refuse(f"{mod.__name__}.{attr}", _PATCH_IT))
    monkeypatch.setattr(http.requests, "get",
                        _refuse("pipeline.http.requests.get", _NEW_HELPER))
