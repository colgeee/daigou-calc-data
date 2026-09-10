"""The only module that talks to the network. Everything is cached on disk."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import requests

UA = "daigou-calc-data/1 (+https://github.com/colgeee/daigou-calc-data)"
# The default floor on a response body worth caching. Every whole-document source this
# pipeline fetches -- a gazetteer archive, a relationship file, a rate CSV, a PDF, a
# directory index -- runs to kilobytes, so a body this short is a truncated transfer, a stub
# error page or an empty answer, never the real file. Caching one would poison the cache for
# a whole TTL and hand the adapter a document it would either misparse or read as "no rows";
# counting it as a failed attempt instead lets the retry loop try again, and fails the build
# loudly if all three are short.
#
# It is a per-call default, not a hard rule, because one source genuinely ships tiny files:
# the Streamlined mirror's per-state payloads bottom out at 48 bytes for a state with a
# single statewide rate row (see `sst.MIN_PAYLOAD_BYTES`).
MIN_BODY_BYTES = 512


def cache_dir() -> Path:
    p = Path(os.environ.get("PIPELINE_CACHE_DIR", "cache"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_cache(path: Path, body: bytes) -> None:
    """Write `body` to `path` through a same-directory `.tmp` file plus `os.replace`, the
    way `build_rates._write_atomic` writes the published files. A killed or failing write
    can then never leave a truncated body at `path` for the next run to read straight back
    as a cache hit; any failure removes the `.tmp` rather than leaving it behind."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def get_cached(url: str, *, ttl_days: float = 1.0, min_bytes: int = MIN_BODY_BYTES) -> bytes:
    """Fetch `url`, serving it from the on-disk cache while that copy is under `ttl_days`
    old. A body under `min_bytes` is refused rather than cached; pass a lower `min_bytes`
    for a source whose real files are legitimately small."""
    path = cache_dir() / hashlib.sha1(url.encode()).hexdigest()
    if path.is_file() and (time.time() - path.stat().st_mtime) < ttl_days * 86400:
        return path.read_bytes()
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=120)
            resp.raise_for_status()
            body = resp.content
            if len(body) >= min_bytes:
                _write_cache(path, body)
                return body
            # An implausibly short body is a failed attempt, not a document: fall through
            # to the sleep and retry, and raise after the last attempt like any other.
            last = ValueError(
                f"{url}: response body is {len(body)} bytes, under the "
                f"{min_bytes}-byte floor -- refusing to cache it"
            )
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
        time.sleep(2**attempt)
    assert last is not None
    raise last


def get_polled(url: str, *, ttl_days: float = 7.0, min_bytes: int = MIN_BODY_BYTES,
               poll_seconds: float = 10.0, attempts: int = 30) -> bytes:
    """`get_cached` for a source that answers `202` with a job-status body while it builds
    the file, then `200` with the file itself. The on-disk cache is consulted exactly as
    `get_cached` consults it, so a rerun inside the TTL never re-polls; only a 200 body is
    ever written. `get_cached` cannot do this: `raise_for_status` passes a 202 through and
    the 183-byte status JSON would be cached as the layer for a whole TTL."""
    path = cache_dir() / hashlib.sha1(url.encode()).hexdigest()
    if path.is_file() and (time.time() - path.stat().st_mtime) < ttl_days * 86400:
        return path.read_bytes()
    for _ in range(attempts):
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=120)
        if resp.status_code == 200:
            body = resp.content
            if len(body) < min_bytes:
                raise ValueError(
                    f"{url}: response body is {len(body)} bytes, under the "
                    f"{min_bytes}-byte floor -- refusing to cache it"
                )
            _write_cache(path, body)
            return body
        if resp.status_code != 202:
            resp.raise_for_status()
            raise ValueError(f"{url}: unexpected status {resp.status_code} while polling")
        time.sleep(poll_seconds)
    raise ValueError(
        f"{url}: still answering 202 after {attempts} polls -- the export job did not finish"
    )
