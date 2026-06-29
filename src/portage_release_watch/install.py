from __future__ import annotations

import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .models import WatchError


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: str
    mode: int
    kind: str = "file"


def _shell_quote(path: Path | str) -> str:
    return shlex.quote(str(path))


def _module_wrapper() -> str:
    package_dir = Path(__file__).resolve().parent
    src_dir = package_dir.parent
    if package_dir.parent.name == "src":
        return "#!/bin/sh\nPYTHONPATH=" + _shell_quote(src_dir) + "${PYTHONPATH:+:$PYTHONPATH} exec python3 -m portage_release_watch.cli \"$@\"\n"
    return "#!/bin/sh\nexec python3 -m portage_release_watch.cli \"$@\"\n"


def _cron_content(overlay: Path, config: Path, state_dir: Path, cache_dir: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"exec /usr/local/bin/portage-release-watch --system --overlay {_shell_quote(overlay)} --config {_shell_quote(config)} --state-dir {_shell_quote(state_dir)} --cache-dir {_shell_quote(cache_dir)} --timeout-seconds 30 --max-age-hours 24 check --quiet --notify\n"
    )


def _postsync_content(overlay: Path, config: Path, state_dir: Path, cache_dir: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"exec /usr/local/bin/portage-release-watch --system --overlay {_shell_quote(overlay)} --config {_shell_quote(config)} --state-dir {_shell_quote(state_dir)} --cache-dir {_shell_quote(cache_dir)} --timeout-seconds 8 --max-age-hours 24 check --quiet --notify\n"
    )


def _ensure_same_or_missing(path: Path, content: str) -> None:
    if path.exists() and path.is_file():
        try:
            if path.read_text() == content:
                return
        except UnicodeDecodeError:
            pass
        raise WatchError(f"refusing to overwrite existing file: {path}")
    if path.exists() and not path.is_file():
        raise WatchError(f"refusing to overwrite existing file: {path}")


def _write_file(path: Path, content: str, mode: int) -> None:
    _ensure_same_or_missing(path, content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def install_system(args) -> int:
    overlay = args.install_overlay.resolve()
    config = args.install_config.resolve()
    prefix = args.prefix.resolve()
    state_dir = args.install_state_dir.resolve()
    cache_dir = args.install_cache_dir.resolve()
    notify_hooks_dir = args.notify_hooks_dir.resolve()

    if os.geteuid() != 0 and not args.dry_run:
        raise WatchError("install-system requires root; re-run with sudo or use --dry-run")
    if args.scheduler == "cron" and not Path("/etc/cron.daily").is_dir():
        raise WatchError("/etc/cron.daily does not exist; install a cron implementation or use --scheduler none")

    dirs = [state_dir, cache_dir, cache_dir / "http", notify_hooks_dir]
    if args.postsync:
        dirs.append(Path("/etc/portage/postsync.d"))

    files: list[PlannedFile] = [PlannedFile(prefix / "bin/portage-release-watch", _module_wrapper(), 0o755)]
    if args.alias_prw:
        files.append(PlannedFile(prefix / "bin/prw", _module_wrapper(), 0o755))
    if args.scheduler == "cron":
        files.append(PlannedFile(Path("/etc/cron.daily/portage-release-watch"), _cron_content(overlay, config, state_dir, cache_dir), 0o755))
    if args.postsync:
        files.append(PlannedFile(Path("/etc/portage/postsync.d/90-portage-release-watch"), _postsync_content(overlay, config, state_dir, cache_dir), 0o755))

    print("Planned portage-release-watch system install:")
    for directory in dirs:
        print(f"  dir  {directory} mode=0755")
    for item in files:
        print(f"  file {item.path} mode={item.mode:o}")

    if args.dry_run:
        print("Dry run: wrote nothing.")
        return 0

    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o755)
    for item in files:
        _write_file(item.path, item.content, item.mode)
    return 0
