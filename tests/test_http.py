from __future__ import annotations

import base64
import json
import urllib.error
from email.message import Message

import pytest

from portage_release_watch.http import HttpClient
from portage_release_watch.models import WatchError


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {
            "ETag": '"new"',
            "Last-Modified": "Tue, 21 Jul 2026 12:00:00 GMT",
            "X-RateLimit-Remaining": "42",
            "X-RateLimit-Reset": "1784638800",
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


@pytest.mark.parametrize(
    ("method", "cached_body", "response_body", "expected", "accept"),
    [
        ("get_json", {"body": {"version": 1}}, b'{"version": 2}', {"version": 2}, "application/json"),
        ("get_text", {"body": "version 1"}, b"version 2", "version 2", "text/html, text/plain, */*"),
        (
            "get_bytes",
            {"body_base64": base64.b64encode(b"version 1").decode("ascii")},
            b"version 2",
            b"version 2",
            "application/octet-stream, */*",
        ),
    ],
)
def test_payload_kinds_force_revalidate_then_use_fresh_cache(
    tmp_path,
    monkeypatch,
    method,
    cached_body,
    response_body,
    expected,
    accept,
):
    client = HttpClient(tmp_path / "cache", timeout=7, max_age_hours=24)
    url = f"https://example.invalid/{method}"
    cache_path = client._cache_path(url)
    cache_path.write_text(json.dumps({
        "fetched_at": 4102444800,
        "etag": '"old"',
        "last_modified": "Mon, 20 Jul 2026 12:00:00 GMT",
        "stale_error": "old failure that must not leak",
        **cached_body,
    }))
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return _Response(response_body)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = getattr(client, method)(url, force=True)

    assert result.body == expected
    assert result.stale_error is None
    request, timeout = requests.pop()
    assert timeout == 7
    assert request.get_header("Accept") == accept
    assert request.get_header("If-none-match") == '"old"'
    assert request.get_header("If-modified-since") == "Mon, 20 Jul 2026 12:00:00 GMT"
    cache = json.loads(cache_path.read_text())
    assert "stale_error" not in cache
    if method == "get_bytes":
        assert base64.b64decode(cache["body_base64"]) == expected
        assert "body" not in cache
    else:
        assert cache["body"] == expected
        assert "body_base64" not in cache

    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("fresh cache hit attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    fresh = getattr(client, method)(url)
    assert fresh.body == expected
    assert fresh.stale_error is None


def test_304_recovery_clears_historical_stale_marker(tmp_path, monkeypatch):
    client = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    url = "https://example.invalid/releases.json"
    cache_path = client._cache_path(url)
    cache_path.write_text(json.dumps({
        "fetched_at": 0,
        "etag": '"cached"',
        "body": {"version": 1},
        "stale_error": "historical failure",
    }))

    def not_modified(*args, **kwargs):
        raise urllib.error.HTTPError(url, 304, "Not Modified", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", not_modified)
    result = client.get_json(url, force=True)

    assert result.body == {"version": 1}
    assert result.stale_error is None
    cache = json.loads(cache_path.read_text())
    assert cache["fetched_at"] > 0
    assert "stale_error" not in cache

    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("fresh 304 cache attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    assert client.get_json(url).stale_error is None


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (
            urllib.error.HTTPError(
                "https://api.github.com/repos/example/project/tags",
                503,
                "upstream rejected secret-token",
                Message(),
                None,
            ),
            "HTTP 503 upstream rejected [REDACTED]",
        ),
        (OSError("socket rejected secret-token"), "OSError: socket rejected [REDACTED]"),
    ],
)
def test_current_failure_uses_stale_cache_without_persisting_error(
    tmp_path,
    monkeypatch,
    error,
    expected_error,
):
    client = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24, token="secret-token")
    url = "https://api.github.com/repos/example/project/tags"
    cache_path = client._cache_path(url)
    cache_path.write_text(json.dumps({"fetched_at": 4102444800, "body": [{"name": "v1.0"}]}))

    def fail(request, timeout):
        assert request.get_header("Authorization") == "Bearer secret-token"
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = client.get_json(url, force=True)

    assert result.body == [{"name": "v1.0"}]
    assert result.stale_error == expected_error
    assert result.stale_error is not None
    assert "secret-token" not in result.stale_error
    persisted = cache_path.read_text()
    assert "secret-token" not in persisted
    assert "stale_error" not in json.loads(persisted)


def test_unusable_cache_does_not_mask_fetch_failure(tmp_path, monkeypatch):
    client = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    url = "https://example.invalid/archive.deb"
    client._cache_path(url).write_text(json.dumps({
        "fetched_at": 4102444800,
        "body_base64": "not valid base64!",
    }))

    def fail(*args, **kwargs):
        raise OSError("network unavailable")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(WatchError, match=r"archive\.deb: OSError: network unavailable"):
        client.get_bytes(url)


def test_bytes_reads_legacy_text_body_cache(tmp_path, monkeypatch):
    client = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    url = "https://example.invalid/legacy.deb"
    client._cache_path(url).write_text(json.dumps({
        "fetched_at": 4102444800,
        "body": "\u00fflegacy",
    }))

    def unexpected_urlopen(*args, **kwargs):
        raise AssertionError("compatible cache attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    result = client.get_bytes(url)

    assert result.body == b"\xfflegacy"
    assert result.stale_error is None
