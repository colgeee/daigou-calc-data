"""The only module that talks to the network. Everything is cached on disk."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import requests

UA = "daigou-calc-data/1 (+https://github.com/colgeee/daigou-calc-data)"


def cache_dir() -> Path:
    p = Path(os.environ.get("PIPELINE_CACHE_DIR", "cache"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_cached(url: str, *, ttl_days: float = 1.0) -> bytes:
    path = cache_dir() / hashlib.sha1(url.encode()).hexdigest()
    if path.is_file() and (time.time() - path.stat().st_mtime) < ttl_days * 86400:
        return path.read_bytes()
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=120)
            resp.raise_for_status()
            path.write_bytes(resp.content)
            return resp.content
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            time.sleep(2**attempt)
    assert last is not None
    raise last
