from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import WatchError

SYSTEM_STATE_DIR = Path("/var/lib/portage-release-watch")
SYSTEM_CACHE_DIR = Path("/var/cache/portage-release-watch")
USER_AGENT = "portage-release-watch/0.1 (+https://github.com/alpine-algo/portage-release-watch)"

if os.geteuid() == 0:
    _default_state = str(SYSTEM_STATE_DIR)
    _default_cache = str(SYSTEM_CACHE_DIR)
else:
    _default_state = str(Path.home() / ".local/state/portage-release-watch")
    _default_cache = str(Path.home() / ".cache/portage-release-watch")

DEFAULT_STATE_DIR = Path(os.environ.get("PORTAGE_RELEASE_WATCH_STATE", _default_state))
DEFAULT_CACHE_DIR = Path(os.environ.get("PORTAGE_RELEASE_WATCH_CACHE", _default_cache))
BUILTIN_CONFIG: dict[str, Any] = {
    "schema_version": 2,
    "dynamic": {"enabled": True},
    "notify_repeat_hours": 168,
    "notify_hooks_dir": "/etc/portage/release-watch.notify.d",
    "packages": {},
}


def _has_ebuilds(path: Path) -> bool:
    try:
        return any(path.glob("*/*/*.ebuild"))
    except OSError:
        return False


def _is_overlay(path: Path) -> bool:
    return ((path / "profiles/repo_name").is_file() or (path / "repo_name").is_file()) and _has_ebuilds(path)


def _validate_selected_overlay(path: Path, source: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise WatchError(
            f"overlay from {source} does not exist: {resolved}; choose an existing Portage overlay"
        )
    if not resolved.is_dir():
        raise WatchError(
            f"overlay from {source} is not a directory: {resolved}; choose a Portage overlay directory"
        )
    if not _is_overlay(resolved):
        raise WatchError(
            f"overlay from {source} is not recognized: {resolved}; "
            "expected profiles/repo_name or repo_name and at least one ebuild"
        )
    return resolved


def detect_default_overlay(cwd: Path, overlay_arg: Path | None = None) -> Path:
    if overlay_arg is not None:
        return _validate_selected_overlay(overlay_arg, "--overlay")

    env_overlay = os.environ.get("PORTAGE_RELEASE_WATCH_OVERLAY")
    if env_overlay:
        return _validate_selected_overlay(Path(env_overlay), "PORTAGE_RELEASE_WATCH_OVERLAY")

    cur = cwd.expanduser().resolve()
    for candidate in (cur, *cur.parents):
        if _is_overlay(candidate):
            return candidate

    local = Path("/var/db/repos/local")
    if _is_overlay(local):
        return local.resolve()

    raise WatchError("no overlay found; run from an overlay or pass --overlay /path/to/overlay")


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(old, value)
        else:
            merged[key] = value
    return merged


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise WatchError(f"cannot read config: {path} ({exc.strerror or 'filesystem error'})") from exc
    except UnicodeError as exc:
        raise WatchError(f"config is not valid UTF-8: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WatchError(
            f"invalid config JSON: {path} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise WatchError(f"config must be a JSON object: {path}")
    return data


def load_config(config_arg: Path | None, overlay: Path) -> tuple[dict[str, Any], list[str]]:
    config = json.loads(json.dumps(BUILTIN_CONFIG))
    loaded: list[str] = []

    for path in (Path("/etc/portage/release-watch.json"), overlay / ".release-watch.json"):
        if path.exists():
            config = _merge_dict(config, _read_json(path))
            loaded.append(str(path))

    if config_arg is not None:
        path = config_arg.expanduser()
        if not path.exists():
            raise WatchError(f"config not found: {path}")
        config = _merge_dict(config, _read_json(path))
        loaded.append(str(path))

    return config, loaded


def load_github_token(config: dict[str, Any]) -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("PORTAGE_RELEASE_WATCH_GITHUB_TOKEN")
    if token:
        return token

    token_file = config.get("github_token_file")
    if not token_file:
        return None
    path = Path(token_file)
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise WatchError(
            f"cannot read GitHub token file: {path} ({exc.strerror or 'filesystem error'})"
        ) from exc
    except UnicodeError as exc:
        raise WatchError(f"GitHub token file is not valid UTF-8: {path}") from exc
    if not token:
        raise WatchError(f"GitHub token file is empty: {path}")
    return token


def config_label(config_sources: list[str]) -> str:
    return config_sources[-1] if config_sources else "builtin"
