import pytest

from pipeline import http

# Over `MIN_BODY_BYTES`, so it is a body worth caching rather than a failed attempt.
BODY = b"hello world " * 64


class FakeResp:
    def __init__(self, content=BODY, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_get_cached_hits_network_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: calls.append(url) or FakeResp())
    assert http.get_cached("https://example.test/a") == BODY
    assert http.get_cached("https://example.test/a") == BODY
    assert calls == ["https://example.test/a"]
    assert any(p.is_file() for p in tmp_path.iterdir())


def test_get_cached_retries_then_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    n = {"c": 0}

    def boom(url, **kw):
        n["c"] += 1
        raise http.requests.ConnectionError("down")

    monkeypatch.setattr(http.requests, "get", boom)
    try:
        http.get_cached("https://example.test/b")
        raise AssertionError("expected failure")
    except http.requests.ConnectionError:
        pass
    assert n["c"] == 3


def test_get_cached_writes_through_a_tmp_file(tmp_path, monkeypatch):
    """The body reaches its cache path by `os.replace` from a same-directory `.tmp`, so a
    reader never sees a half-written file."""
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: FakeResp())
    seen: list[tuple[str, str]] = []
    real_replace = http.os.replace
    monkeypatch.setattr(
        http.os, "replace",
        lambda src, dst: seen.append((str(src), str(dst))) or real_replace(src, dst),
    )
    assert http.get_cached("https://example.test/c") == BODY
    assert len(seen) == 1 and seen[0][0].endswith(".tmp") and seen[0][0] != seen[0][1]
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_cache_write_leaves_no_partial_or_tmp_file(tmp_path, monkeypatch):
    """A write that fails at the rename leaves neither a truncated cache entry (which the
    next run would read straight back as a hit) nor a leftover `.tmp`. A disk error is not
    a network failure, so it propagates rather than burning the retries."""
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: FakeResp())

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(http.os, "replace", boom)
    with pytest.raises(OSError):
        http.get_cached("https://example.test/d")
    assert list(tmp_path.iterdir()) == []


def test_an_undersized_body_is_retried_then_raised(tmp_path, monkeypatch):
    """A body under `MIN_BODY_BYTES` is a truncated transfer or a stub error page, never a
    real source file: it counts as a failed attempt inside the retry loop, is never cached
    (which would poison the cache for a whole TTL), and raises after the last attempt."""
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    n = {"c": 0}

    def short(url, **kw):
        n["c"] += 1
        return FakeResp(content=b"x" * (http.MIN_BODY_BYTES - 1))

    monkeypatch.setattr(http.requests, "get", short)
    with pytest.raises(ValueError, match=f"{http.MIN_BODY_BYTES - 1} bytes"):
        http.get_cached("https://example.test/e")
    assert n["c"] == 3
    assert list(tmp_path.iterdir()) == []


def test_a_body_exactly_at_the_floor_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    body = b"y" * http.MIN_BODY_BYTES
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: FakeResp(content=body))
    assert http.get_cached("https://example.test/f") == body
    assert [p.read_bytes() for p in tmp_path.iterdir()] == [body]
