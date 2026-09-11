from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .http import atomic_write_json
from .config import trusted_path


@contextmanager
def state_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ".check.lock"
    if os.geteuid() == 0:
        trusted_path(path, missing_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def maybe_notify(report: dict[str, Any], notice: str, state_dir: Path, repeat_hours: float, use_logger: bool, hooks_dir: Path | None) -> bool:
    if os.geteuid() == 0 and hooks_dir is not None:
        trusted_path(hooks_dir, missing_ok=True)
    signal = {
        "updates": sorted((r["cp"], r.get("upstream", {}).get("raw")) for r in report.get("updates", [])),
        "manual": sorted(r["cp"] for r in report.get("manual", [])),
        "warnings": sorted((r["cp"], r["status"], r.get("message", "")) for r in report.get("warnings", [])),
    }
    state_path = state_dir / "notify-state.json"
    old = None
    try:
        old = json.loads(state_path.read_text())
    except Exception:
        pass
    now = time.time()
    changed = old is None or old.get("signal") != signal or now - old.get("notified_at", 0) > repeat_hours * 3600
    if changed:
        atomic_write_json(state_path, {"signal": signal, "notified_at": now, "generated_at": report["generated_at"]})
        logger = "/usr/bin/logger" if os.geteuid() == 0 else shutil.which("logger")
        if use_logger and logger and Path(logger).is_file():
            first_line = notice.strip().splitlines()[0] if notice.strip() else "Local Portage overlay release report"
            subprocess.run([logger, "-t", "portage-release-watch", first_line], check=False)
            for row in report.get("updates", []):
                upstream = row.get("upstream", {})
                subprocess.run([logger, "-t", "portage-release-watch", f"update: {row['cp']} {row.get('local_pvr') or row.get('local_pv')} -> {upstream.get('raw')}"], check=False)
        if hooks_dir and hooks_dir.exists():
            report_path = state_dir / "latest-report.json"
            env = os.environ.copy()
            if os.geteuid() == 0:
                env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8"}
            env["PORTAGE_RELEASE_WATCH_REPORT"] = str(report_path)
            env["PORTAGE_RELEASE_WATCH_STATUS"] = "updates" if report.get("updates") else "no_updates"
            for hook in sorted(hooks_dir.iterdir()):
                if hook.is_file() and os.access(hook, os.X_OK):
                    if os.geteuid() == 0:
                        trusted_path(hook)
                    subprocess.run([str(hook), str(report_path)], env=env, check=False)
    return changed
