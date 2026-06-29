from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .http import atomic_write_json


def maybe_notify(report: dict[str, Any], notice: str, state_dir: Path, repeat_hours: float, use_logger: bool, hooks_dir: Path | None) -> bool:
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
        if use_logger and shutil.which("logger"):
            first_line = notice.strip().splitlines()[0] if notice.strip() else "Local Portage overlay release report"
            subprocess.run(["logger", "-t", "portage-release-watch", first_line], check=False)
            for row in report.get("updates", []):
                upstream = row.get("upstream", {})
                subprocess.run(["logger", "-t", "portage-release-watch", f"update: {row['cp']} {row.get('local_pvr') or row.get('local_pv')} -> {upstream.get('raw')}"], check=False)
        if hooks_dir and hooks_dir.exists():
            report_path = state_dir / "latest-report.json"
            env = os.environ.copy()
            env["PORTAGE_RELEASE_WATCH_REPORT"] = str(report_path)
            env["PORTAGE_RELEASE_WATCH_STATUS"] = "updates" if report.get("updates") else "no_updates"
            for hook in sorted(hooks_dir.iterdir()):
                if hook.is_file() and os.access(hook, os.X_OK):
                    subprocess.run([str(hook), str(report_path)], env=env, check=False)
    return changed
