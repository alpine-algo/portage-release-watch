from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import hashlib
from pathlib import Path
from typing import Any

from .config import USER_AGENT
from .models import WatchError


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

    def get_json(self, url: str, *, force: bool = False) -> Any:
        path = self._cache_path(url)
        cached = self._read_cache(path)
        now = time.time()
        if cached and not force and now - cached.get("fetched_at", 0) < self.max_age:
            return cached["body"]

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token and "api.github.com" in url:
            headers["Authorization"] = f"Bearer {self.token}"
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                body = json.loads(raw)
                payload = {
                    "url": url,
                    "status": resp.status,
                    "fetched_at": now,
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "rate_remaining": resp.headers.get("X-RateLimit-Remaining"),
                    "rate_reset": resp.headers.get("X-RateLimit-Reset"),
                    "body": body,
                }
                atomic_write_json(path, payload)
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                cached["fetched_at"] = now
                atomic_write_json(path, cached)
                return cached["body"]
            if cached:
                cached["stale_error"] = f"HTTP {exc.code} {exc.reason}"
                atomic_write_json(path, cached)
                return cached["body"]
            raise WatchError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
        except Exception as exc:
            if cached:
                cached["stale_error"] = repr(exc)
                atomic_write_json(path, cached)
                return cached["body"]
            raise WatchError(f"{url}: {exc}") from exc


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
