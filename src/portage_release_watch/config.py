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


def detect_default_overlay(cwd: Path) -> Path:
    env_overlay = os.environ.get("PORTAGE_RELEASE_WATCH_OVERLAY")
    if env_overlay:
        return Path(env_overlay).expanduser().resolve()

    cur = cwd.expanduser().resolve()
    for candidate in (cur, *cur.parents):
        if _is_overlay(candidate):
            return candidate

    local = Path("/var/db/repos/local")
    if local.exists():
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
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise WatchError(f"config must be a JSON object: {path}")
    return data


def load_config(config_arg: Path | None, overlay: Path) -> tuple[dict[str, Any], list[str]]:
    config = json.loads(json.dumps(BUILTIN_CONFIG))
    loaded: list[str] = []

    for path in (Path("/etc/portage/release-watch.json"), overlay / ".release-watch.json"):
        if path.is_file() and os.access(path, os.R_OK):
            config = _merge_dict(config, _read_json(path))
            loaded.append(str(path))

    if config_arg is not None:
        path = config_arg.expanduser()
        if not path.is_file():
            raise WatchError(f"config not found: {path}")
        config = _merge_dict(config, _read_json(path))
        loaded.append(str(path))

    return config, loaded


def config_label(config_sources: list[str]) -> str:
    return config_sources[-1] if config_sources else "builtin"
