from pipeline import http


class FakeResp:
    def __init__(self, content=b"hello", status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_get_cached_hits_network_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_CACHE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(http.requests, "get", lambda url, **kw: calls.append(url) or FakeResp())
    assert http.get_cached("https://example.test/a") == b"hello"
    assert http.get_cached("https://example.test/a") == b"hello"
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
