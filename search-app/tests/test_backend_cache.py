"""Cache raw results without caching permissions, failures, or mutable output."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from kosher_search import server as server_mod


def test_cache_keys_expiry_and_isolated_results(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: now[0])
    backend = server_mod.Backend()
    calls = []

    def fetch(*key):
        calls.append(key)
        return {"results": [{"url": "https://example.org", "title": key[0]}]}

    monkeypatch.setattr(backend, "_fetch", fetch)
    backend.search("hello", "general", 1)["results"].clear()
    assert backend.search("hello", "general", 1)["results"]
    assert len(calls) == 1
    backend.search("hello", "images", 1)
    backend.search("hello", "general", 2)
    backend.search("Hello", "general", 1)
    assert len(calls) == 4
    now[0] += server_mod.CACHE_SECONDS
    backend.search("hello", "general", 1)
    assert len(calls) == 5


def test_cache_is_bounded_and_keeps_recently_used_queries(monkeypatch):
    monkeypatch.setattr(server_mod, "CACHE_ENTRIES", 2)
    backend = server_mod.Backend()
    calls = []
    monkeypatch.setattr(backend, "_fetch", lambda *key:
                        calls.append(key) or {"results": [{"title": key[0]}]})
    for query in ("a", "b", "a", "c", "a", "b"):
        backend.search(query, "general", 1)
    assert [key[0] for key in calls] == ["a", "b", "c", "b"]
    assert len(backend._cache) == 2


def test_concurrent_identical_searches_share_one_request(monkeypatch):
    backend = server_mod.Backend()
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def fetch(*key):
        calls.append(key)
        started.set()
        assert release.wait(3)
        return {"results": [{"title": "hello"}]}

    monkeypatch.setattr(backend, "_fetch", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(backend.search, "hello", "general", 1)
        assert started.wait(3)
        pending = backend._pending[("hello", "general", 1)]
        original = pending.result

        def result(*args, **kwargs):
            waiting.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(pending, "result", result)
        second = pool.submit(backend.search, "hello", "general", 1)
        try:
            assert waiting.wait(3)
        finally:
            release.set()
        assert first.result() == second.result()
        assert first.result() is not second.result()
    assert len(calls) == 1
    assert not backend._pending


def test_failures_and_empty_results_are_not_cached(monkeypatch):
    backend = server_mod.Backend()
    responses = iter([OSError("offline"), {"results": []}, {"results": [{"title": "ok"}]}])

    def fetch(*key):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(backend, "_fetch", fetch)
    with pytest.raises(OSError):
        backend.search("hello", "general", 1)
    assert not backend._pending
    assert backend.search("hello", "general", 1) == {"results": []}
    assert backend.search("hello", "general", 1)["results"][0]["title"] == "ok"
    assert backend.search("hello", "general", 1)["results"][0]["title"] == "ok"


@pytest.mark.parametrize("raw", [b"[]", b'{"results":null}', b"not json", b"x" * 33])
def test_malformed_or_oversized_responses_never_enter_cache(monkeypatch, raw):
    import io
    monkeypatch.setattr(server_mod, "MAX_BACKEND_BYTES", 32)
    monkeypatch.setattr(server_mod.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(raw))
    backend = server_mod.Backend()
    with pytest.raises(ValueError):
        backend.search("hello", "general", 1)
    assert not backend._cache
    assert not backend._pending
