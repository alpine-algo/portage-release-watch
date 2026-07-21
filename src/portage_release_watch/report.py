from __future__ import annotations

import argparse
import datetime as _dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import SYSTEM_STATE_DIR, config_label
from .http import HttpClient
from .models import PackageInfo, WatchError
from .sources import fetch_candidates, resolve_rule
from .versioning import best_candidate, compare_versions


def evaluate_package(info: PackageInfo, rule: dict[str, Any] | None, http: HttpClient, force: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cp": info.cp,
        "pn": info.pn,
        "local_pv": info.pv,
        "local_pvr": info.pvr,
        "live_present": info.live,
        "ebuilds": info.ebuilds,
    }
    if not rule:
        row.update({"status": "unmapped", "message": "No release-watch mapping configured or inferred"})
        return row
    row["mapping_origin"] = rule.get("_origin", "unknown")
    status = rule.get("status", "auto")
    row["mapping_status"] = status
    if status in ("manual", "manual_no_fetch"):
        row.update({"status": "manual", "message": rule.get("note", "Manual upstream check required"), "source": rule.get("source")})
        return row
    if status == "live_only":
        row.update({"status": "live", "message": rule.get("note", "Live-only package; fixed-release comparison suppressed"), "source": rule.get("source")})
        return row
    source = rule.get("source")
    if not source:
        row.update({"status": "unmapped", "message": "Mapping has no source"})
        return row
    try:
        result = fetch_candidates(source, http, force)
        if result.stale_error:
            row["stale_error"] = result.stale_error
        cand = best_candidate(result.body)
    except WatchError as exc:
        row.update({"status": "failed", "message": str(exc), "source": source})
        return row
    except Exception as exc:
        row.update({"status": "failed", "message": repr(exc), "source": source})
        return row
    if not cand:
        row.update({"status": "no_candidate", "message": "No upstream candidate matched filters", "source": source})
        return row
    row["upstream"] = asdict(cand)
    row["source"] = source
    if info.pv is None or info.live and not info.pvr:
        row.update({"status": "live", "message": f"Latest upstream candidate is {cand.raw}; live-only comparison suppressed"})
        return row
    cmp = compare_versions(cand.version, info.pv)
    row["compare"] = cmp
    if cmp > 0:
        row["status"] = "outdated"
        row["message"] = f"{info.pvr or info.pv} -> {cand.raw}"
        if info.live:
            row["live_divergence"] = f"9999 ebuild present; fixed ebuild {info.pvr or info.pv} trails latest matched release {cand.raw}"
    elif cmp == 0:
        row["status"] = "current"
        row["message"] = f"current at {info.pvr or info.pv}"
        if info.live:
            row["live_divergence"] = f"9999 ebuild present; fixed ebuild matches latest matched release {cand.raw}"
    else:
        row["status"] = "ahead"
        row["message"] = f"local {info.pvr or info.pv} is newer than matched upstream {cand.raw}"
    return row


def build_report(rows: list[dict[str, Any]], config_sources: list[str], overlay: Path) -> dict[str, Any]:
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary: dict[str, int] = {}
    for row in rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    return {
        "generated_at": now,
        "overlay": str(overlay),
        "config": config_label(config_sources),
        "config_sources": config_sources,
        "summary": summary,
        "updates": [r for r in rows if r["status"] == "outdated"],
        "manual": [r for r in rows if r["status"] == "manual"],
        "live": [r for r in rows if r["status"] == "live" or r.get("live_divergence")],
        "warnings": [r for r in rows if r["status"] in ("failed", "unmapped", "no_candidate", "ahead") or r.get("stale_error")],
        "packages": rows,
    }


def notice_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Local Portage overlay release report ({report['generated_at']})")
    lines.append(f"Overlay: {report['overlay']}")
    lines.append("")
    updates = report.get("updates", [])
    if updates:
        lines.append("Updates available:")
        for row in updates:
            upstream = row.get("upstream", {})
            lines.append(f"  {row['cp']:<34} {row.get('local_pvr') or row.get('local_pv') or '-':<14} -> {upstream.get('raw', '-')}  {upstream.get('source_id', '')}")
            if row.get("live_divergence"):
                lines.append(f"    live: {row['live_divergence']}")
    else:
        lines.append("Updates available: none")
    manual = report.get("manual", [])
    if manual:
        lines.append("")
        lines.append("Manual/no-fetch checks:")
        for row in manual:
            lines.append(f"  {row['cp']:<34} {row.get('local_pvr') or '-':<14} {row.get('message', '')}")
    live = [r for r in report.get("live", []) if r.get("status") == "live"]
    if live:
        lines.append("")
        lines.append("Live-only / 9999 status:")
        for row in live:
            lines.append(f"  {row['cp']:<34} {row.get('message', '')}")
    warnings = report.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for row in warnings:
            if row.get("stale_error"):
                lines.append(f"  {row['cp']:<34} [{row['status']}; stale cache] {row['stale_error']}")
            else:
                lines.append(f"  {row['cp']:<34} [{row['status']}] {row.get('message', '')}")
    return "\n".join(lines) + "\n"


def report_path_for_read(args: argparse.Namespace) -> Path:
    system_report = SYSTEM_STATE_DIR / "latest-report.json"
    user_report = args.state_dir / "latest-report.json"
    if getattr(args, "system", False):
        return system_report
    if user_report.exists():
        return user_report
    if system_report.exists() and system_report.is_file():
        try:
            with system_report.open():
                pass
            return system_report
        except OSError:
            pass
    return user_report


def load_latest_report(args: argparse.Namespace) -> dict[str, Any]:
    path = report_path_for_read(args)
    if not path.exists():
        raise WatchError(f"no report found: {path}; run check first")
    try:
        text = path.read_text()
    except OSError as exc:
        raise WatchError(
            f"cannot read cached report: {path} ({exc.strerror or 'filesystem error'})"
        ) from exc
    except UnicodeError as exc:
        raise WatchError(f"cached report is not valid UTF-8: {path}") from exc
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WatchError(
            f"invalid cached report JSON: {path} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(report, dict):
        raise WatchError(f"cached report must be a JSON object: {path}")
    return report


def details_text(report: dict[str, Any]) -> str:
    lines = [notice_text(report).rstrip(), "", "All packages:"]
    for row in sorted(report.get("packages", []), key=lambda r: r.get("cp", "")):
        upstream = row.get("upstream") or {}
        source = upstream.get("source_id") or (row.get("source") or {}).get("type") or "-"
        latest = upstream.get("raw") or "-"
        origin = row.get("mapping_origin") or "-"
        lines.append(f"  {row.get('cp','-'):<34} {row.get('status','-'):<12} local={row.get('local_pvr') or row.get('local_pv') or '-':<14} latest={latest:<24} source={source} origin={origin}")
    return "\n".join(lines) + "\n"


def live_text(report: dict[str, Any]) -> str:
    rows = [r for r in report.get("packages", []) if r.get("live_present") or r.get("live_divergence") or r.get("status") == "live"]
    lines = [f"Local Portage overlay live/9999 report ({report.get('generated_at','unknown')})", ""]
    if not rows:
        lines.append("No live/9999 packages in latest report.")
    for row in rows:
        upstream = row.get("upstream") or {}
        lines.append(f"{row.get('cp','-'):<34} fixed={row.get('local_pvr') or row.get('local_pv') or '-':<14} latest={upstream.get('raw','-'):<18} status={row.get('status','-')}")
        if row.get("live_divergence"):
            lines.append(f"  {row['live_divergence']}")
        elif row.get("message"):
            lines.append(f"  {row['message']}")
    return "\n".join(lines) + "\n"
