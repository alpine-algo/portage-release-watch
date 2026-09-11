from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import SYSTEM_POLICY_PATH, prepare_root_directory, trusted_path
from .models import WatchError


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: str
    mode: int


def _shell_quote(path: Path | str) -> str:
    return shlex.quote(str(path))


def _module_wrapper(runtime: Path) -> str:
    # -I ignores PYTHON* and user site packages, but retains system Portage.
    bootstrap = (
        "import os,stat,sys; from pathlib import Path; "
        f"p=Path({str(runtime)!r}); "
        "paths=[*reversed(p.parents),p,p/'portage_release_watch']; "
        "paths += list((p/'portage_release_watch').glob('*.py')); "
        "safe=all(not x.is_symlink() and x.stat().st_uid==0 and not x.stat().st_mode & 0o022 for x in paths); "
        "safe or sys.exit('portage-release-watch: untrusted runtime'); "
        "sys.path.insert(0,str(p)); from portage_release_watch.cli import main; sys.exit(main())"
    )
    return "#!/bin/sh\nexec /usr/bin/python3 -I -B -c " + shlex.quote(bootstrap) + ' "$@"\n'


def _runner_content(executable: Path, overlay: Path, config: Path | None, state_dir: Path,
                    cache_dir: Path, timeout_seconds: int, policy: Path | None = None) -> str:
    command = [_shell_quote(executable), "--overlay", _shell_quote(overlay)]
    if config is not None:
        command.extend(("--config", _shell_quote(config)))
    if policy is not None:
        command.extend(("--policy", _shell_quote(policy)))
    command.extend(("--state-dir", _shell_quote(state_dir), "--cache-dir", _shell_quote(cache_dir),
                    "--timeout-seconds", str(timeout_seconds), "--max-age-hours", "24", "check", "--quiet", "--notify"))
    return "#!/bin/sh\nexec " + " ".join(command) + "\n"


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _check_replacement(item: PlannedFile, receipts: dict[str, str], upgrade: bool,
                       legacy: set[str]) -> None:
    if item.path.is_symlink():
        raise WatchError(f"refusing to overwrite symlink: {item.path}")
    if not item.path.exists():
        return
    if not item.path.is_file():
        raise WatchError(f"refusing to overwrite non-file: {item.path}")
    try:
        current = item.path.read_text()
    except UnicodeError as exc:
        raise WatchError(f"refusing to overwrite non-text file: {item.path}") from exc
    if current == item.content:
        return
    if upgrade and (receipts.get(str(item.path)) == _digest(current) or current in legacy):
        return
    raise WatchError(f"refusing to overwrite existing file: {item.path}; --upgrade only replaces recorded files or exact supported legacy scripts")


def _write_file(path: Path, content: str, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_system(args) -> int:
    overlay = args.install_overlay.resolve()
    config = args.install_config.resolve() if args.install_config is not None else None
    prefix = args.prefix.absolute()
    state_dir = args.install_state_dir.absolute()
    cache_dir = args.install_cache_dir.absolute()
    notify_hooks_dir = args.notify_hooks_dir.absolute()
    policy = args.install_policy.absolute()
    runtime = prefix / "lib/portage-release-watch"
    receipt_path = runtime / "installed-files.json"

    if os.geteuid() != 0 and not args.dry_run:
        raise WatchError("install-system requires root; re-run with sudo or use --dry-run")
    if args.scheduler == "cron" and not Path("/etc/cron.daily").is_dir():
        raise WatchError("/etc/cron.daily does not exist; install a cron implementation or use --scheduler none")
    if config is not None:
        try:
            with config.open("rb"):
                pass
        except OSError as exc:
            raise WatchError(f"cannot read config: {config} ({exc.strerror or 'filesystem error'})") from exc

    executable = prefix / "bin/portage-release-watch"
    wrapper = _module_wrapper(runtime)
    files = [PlannedFile(executable, wrapper, 0o755)]
    if args.alias_prw:
        files.append(PlannedFile(prefix / "bin/prw", wrapper, 0o755))
    package_dir = Path(__file__).resolve().parent
    for source in sorted(package_dir.glob("*.py")):
        files.append(PlannedFile(runtime / "portage_release_watch" / source.name, source.read_text(), 0o644))
    legacy_by_path: dict[Path, set[str]] = {
        prefix / "bin" / name: {'#!/bin/sh\nexec python3 -m portage_release_watch.cli "$@"\n'}
        for name in ("portage-release-watch", "prw")
    }
    if args.legacy_source is not None:
        legacy_wrapper = "#!/bin/sh\nPYTHONPATH=" + _shell_quote(args.legacy_source.resolve()) + '${PYTHONPATH:+:$PYTHONPATH} exec python3 -m portage_release_watch.cli "$@"\n'
        for name in ("portage-release-watch", "prw"):
            legacy_by_path[prefix / "bin" / name].add(legacy_wrapper)
    for enabled, path, timeout in (
        (args.scheduler == "cron", Path("/etc/cron.daily/portage-release-watch"), 30),
        (args.postsync, Path("/etc/portage/postsync.d/90-portage-release-watch"), 8),
    ):
        if enabled:
            files.append(PlannedFile(path, _runner_content(executable, overlay, config, state_dir, cache_dir, timeout, policy), 0o755))
            legacy_by_path[path] = {_runner_content(executable, overlay, config, state_dir, cache_dir, timeout)}

    dirs = {item.path.parent: 0o755 for item in files}
    dirs.update({state_dir: 0o755, cache_dir: 0o700, cache_dir / "http": 0o700, notify_hooks_dir: 0o755})
    print("Planned portage-release-watch system install:")
    for directory, mode in dirs.items():
        print(f"  dir  {directory} mode={mode:04o}")
    for item in files:
        print(f"  file {item.path} mode={item.mode:04o}")
    print(f"  root policy {policy} (admin-managed, not overwritten)")
    if args.dry_run:
        print("Dry run: wrote nothing; destination trust and replacement checks run during installation.")
        return 0

    # Validate the entire plan before any mutation, including all existing ancestors.
    for path in (*dirs, *(item.path for item in files), receipt_path, policy):
        trusted_path(path, missing_ok=True)
    receipts: dict[str, str] = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
        except (ValueError, UnicodeError) as exc:
            raise WatchError(f"invalid installation receipt: {receipt_path}") from exc
        if not isinstance(receipt, dict) or receipt.get("format") != "portage-release-watch.install.v1":
            raise WatchError(f"not a portage-release-watch installation receipt: {receipt_path}")
        receipts = receipt.get("files")
        if not isinstance(receipts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in receipts.items()):
            raise WatchError(f"invalid installation receipt: {receipt_path}")
    for item in files:
        _check_replacement(item, receipts, args.upgrade, legacy_by_path.get(item.path, set()))
    for directory, mode in dirs.items():
        prepare_root_directory(directory, mode)
    for item in files:
        _write_file(item.path, item.content, item.mode)
        receipts[str(item.path)] = _digest(item.content)
    _write_file(receipt_path, json.dumps({"format": "portage-release-watch.install.v1", "files": receipts}, indent=2, sort_keys=True) + "\n", 0o600)
    return 0
