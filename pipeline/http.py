"""The only module that talks to the network. Everything is cached on disk."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import requests

UA = "daigou-calc-data/1 (+https://github.com/colgeee/daigou-calc-data)"
# The smallest response body worth caching. Every source this pipeline fetches -- a
# directory index, a rate CSV, a PDF, a gazetteer archive -- runs to kilobytes at least, so
# a body this short is a truncated transfer, a stub error page or an empty answer, never the
# real file. Caching one would poison the cache for a whole TTL and hand the adapter a
# document it would either misparse or read as "no rows"; counting it as a failed attempt
# instead lets the retry loop try again, and fails the build loudly if all three are short.
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


def get_cached(url: str, *, ttl_days: float = 1.0) -> bytes:
    path = cache_dir() / hashlib.sha1(url.encode()).hexdigest()
    if path.is_file() and (time.time() - path.stat().st_mtime) < ttl_days * 86400:
        return path.read_bytes()
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=120)
            resp.raise_for_status()
            body = resp.content
            if len(body) >= MIN_BODY_BYTES:
                _write_cache(path, body)
                return body
            # An implausibly short body is a failed attempt, not a document: fall through
            # to the sleep and retry, and raise after the last attempt like any other.
            last = ValueError(
                f"{url}: response body is {len(body)} bytes, under the "
                f"{MIN_BODY_BYTES}-byte floor -- refusing to cache it"
            )
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
        time.sleep(2**attempt)
    assert last is not None
    raise last
