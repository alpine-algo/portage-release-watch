from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from .config import USER_AGENT
from .models import WatchError


T = TypeVar("T")


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    body: T
    stale_error: str | None = None


class HttpClient:
    def __init__(self, cache_dir: Path, timeout: int, max_age_hours: float, token: str | None = None):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.max_age = max_age_hours * 3600
        self.token = token
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def get_json(self, url: str, *, force: bool = False) -> FetchResult[Any]:
        return self._get(url, force=force, accept="application/json", payload_kind="json")

    def get_text(
        self,
        url: str,
        *,
        force: bool = False,
        accept: str = "text/html, text/plain, */*",
    ) -> FetchResult[str]:
        return self._get(url, force=force, accept=accept, payload_kind="text")

    def get_bytes(
        self,
        url: str,
        *,
        force: bool = False,
        accept: str = "application/octet-stream, */*",
    ) -> FetchResult[bytes]:
        return self._get(url, force=force, accept=accept, payload_kind="bytes")

    def _get(self, url: str, *, force: bool, accept: str, payload_kind: str) -> FetchResult[Any]:
        path = self._cache_path(url)
        cached = self._read_cache(path)
        usable, cached_body = self._cached_body(cached, payload_kind)
        now = time.time()
        fetched_at = cached.get("fetched_at", 0) if cached else 0
        fresh = isinstance(fetched_at, (int, float)) and now - fetched_at < self.max_age
        if usable and not force and fresh:
            return FetchResult(cached_body)

        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token:
            target = urllib.parse.urlsplit(url)
            if target.scheme == "https" and target.hostname == "api.github.com":
                headers["Authorization"] = f"Bearer {self.token}"
        if cached and usable:
            if isinstance(cached.get("etag"), str):
                headers["If-None-Match"] = cached["etag"]
            if isinstance(cached.get("last_modified"), str):
                headers["If-Modified-Since"] = cached["last_modified"]

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = self._decode_response(resp.read(), payload_kind)
                payload = {
                    "url": url,
                    "status": resp.status,
                    "fetched_at": now,
                    "payload_kind": payload_kind,
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "rate_remaining": resp.headers.get("X-RateLimit-Remaining"),
                    "rate_reset": resp.headers.get("X-RateLimit-Reset"),
                }
                if payload_kind == "bytes":
                    payload["body_base64"] = base64.b64encode(body).decode("ascii")
                else:
                    payload["body"] = body
                atomic_write_json(path, payload)
                return FetchResult(body)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and usable and cached:
                cached["fetched_at"] = now
                cached["payload_kind"] = payload_kind
                cached.pop("stale_error", None)
                atomic_write_json(path, cached)
                return FetchResult(cached_body)
            return self._fallback_or_raise(url, usable, cached_body, exc)
        except Exception as exc:
            return self._fallback_or_raise(url, usable, cached_body, exc)

    def _cached_body(self, cached: dict[str, Any] | None, payload_kind: str) -> tuple[bool, Any]:
        if not cached:
            return False, None
        cached_kind = cached.get("payload_kind")
        if cached_kind is not None and cached_kind != payload_kind:
            return False, None
        if payload_kind == "json":
            return ("body" in cached), cached.get("body")
        if payload_kind == "text":
            body = cached.get("body")
            return isinstance(body, str), body
        encoded = cached.get("body_base64")
        if isinstance(encoded, str):
            try:
                return True, base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                pass
        body = cached.get("body")
        if isinstance(body, str):
            try:
                return True, body.encode("latin1")
            except UnicodeEncodeError:
                pass
        return False, None

    @staticmethod
    def _decode_response(raw: bytes, payload_kind: str) -> Any:
        if payload_kind == "json":
            return json.loads(raw.decode("utf-8", "replace"))
        if payload_kind == "text":
            return raw.decode("utf-8", "replace")
        return raw

    def _fallback_or_raise(
        self,
        url: str,
        usable: bool,
        cached_body: Any,
        exc: Exception,
    ) -> FetchResult[Any]:
        detail = self._failure_detail(exc)
        if usable:
            return FetchResult(cached_body, detail)
        raise WatchError(f"{self._sanitize(url)}: {detail}") from exc

    def _failure_detail(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            detail = f"HTTP {exc.code} {exc.reason}"
        else:
            detail = f"{type(exc).__name__}: {exc}"
        return self._sanitize(detail)

    def _sanitize(self, text: str) -> str:
        return text.replace(self.token, "[REDACTED]") if self.token else text


def atomic_write_json(path: Path, data: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
