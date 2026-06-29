from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .config import DEFAULT_CACHE_DIR, DEFAULT_STATE_DIR, SYSTEM_CACHE_DIR, SYSTEM_STATE_DIR, detect_default_overlay, load_config
from .http import HttpClient, atomic_write_json, atomic_write_text
from .install import install_system
from .models import WatchError
from .notify import maybe_notify
from .overlay import scan_overlay
from .report import build_report, details_text, evaluate_package, live_text, load_latest_report, notice_text, report_path_for_read
from .sources import resolve_rule


def command_scan(args: argparse.Namespace) -> int:
    config, sources = load_config(args.config, args.overlay)
    infos = scan_overlay(args.overlay)
    rows = []
    for cp, info in sorted(infos.items()):
        rule = resolve_rule(config, info, args.overlay)
        rows.append({
            "cp": cp,
            "local_pvr": info.pvr,
            "live_present": info.live,
            "mapped": bool(rule),
            "mapping_origin": (rule or {}).get("_origin"),
            "status": (rule or {}).get("status", "unmapped"),
            "source": (rule or {}).get("source"),
            "ebuilds": info.ebuilds,
        })
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            mapped = "mapped" if row["mapped"] else "UNMAPPED"
            live = " live" if row["live_present"] else ""
            print(f"{row['cp']:<34} {row.get('local_pvr') or '-':<14} {mapped:<8} {row['status']}{live}")
    return 0


def command_check(args: argparse.Namespace) -> int:
    config, sources = load_config(args.config, args.overlay)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("PORTAGE_RELEASE_WATCH_GITHUB_TOKEN")
    token_file = config.get("github_token_file")
    if not token and token_file:
        try:
            token = Path(token_file).read_text().strip()
        except Exception:
            token = None
    http = HttpClient(args.cache_dir / "http", args.timeout_seconds, args.max_age_hours, token)
    infos = scan_overlay(args.overlay)
    selected = sorted(infos)
    if args.package:
        selected = [cp for cp in selected if cp == args.package]
        if not selected:
            raise WatchError(f"package not found in overlay: {args.package}")
    rows = [evaluate_package(infos[cp], resolve_rule(config, infos[cp], args.overlay), http, args.refresh) for cp in selected]
    report = build_report(rows, sources, args.overlay)
    notice = notice_text(report)
    if not args.no_write:
        atomic_write_json(args.state_dir / "latest-report.json", report, mode=0o644)
        atomic_write_text(args.state_dir / "latest-notice.txt", notice, mode=0o644)
        hist = args.state_dir / "history.ndjson"
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a") as f:
            f.write(json.dumps({"generated_at": report["generated_at"], "summary": report["summary"]}, sort_keys=True) + "\n")
    changed = False
    if args.notify and not args.no_write:
        hooks_dir = Path(config.get("notify_hooks_dir", args.state_dir / "notify.d"))
        changed = maybe_notify(report, notice, args.state_dir, config.get("notify_repeat_hours", 168), True, hooks_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    elif not args.quiet or report.get("updates") or report.get("warnings") or changed:
        print(notice, end="")
    return 2 if args.fail_on_updates and report.get("updates") else 0


def command_list(args: argparse.Namespace) -> int:
    report = load_latest_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(notice_text(report), end="")
    return 0


def command_details(args: argparse.Namespace) -> int:
    report = load_latest_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(details_text(report), end="")
    return 0


def command_live(args: argparse.Namespace) -> int:
    report = load_latest_report(args)
    rows = [r for r in report.get("packages", []) if r.get("live_present") or r.get("live_divergence") or r.get("status") == "live"]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(live_text(report), end="")
    return 0


def command_explain(args: argparse.Namespace) -> int:
    config, _sources = load_config(args.config, args.overlay)
    infos = scan_overlay(args.overlay)
    if args.package not in infos:
        raise WatchError(f"package not found: {args.package}")
    info = infos[args.package]
    rule = resolve_rule(config, info, args.overlay)
    payload = {"package": asdict(info), "mapping": rule}
    report_path = report_path_for_read(args)
    if report_path.exists():
        report = json.loads(report_path.read_text())
        payload["report_path"] = str(report_path)
        payload["latest_report_row"] = next((r for r in report.get("packages", []) if r.get("cp") == args.package), None)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Gentoo Portage local overlay ebuilds for newer upstream releases")
    parser.add_argument("--overlay", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--system", action="store_true", help="Read/write canonical root state under /var/lib and /var/cache")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--max-age-hours", type=float, default=24)
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan local overlay and mapping without network")
    _add_json(p_scan)
    p_scan.set_defaults(func=command_scan)

    p_check = sub.add_parser("check", help="Check upstream sources and write report")
    p_check.add_argument("--package")
    _add_json(p_check)
    p_check.add_argument("--quiet", action="store_true")
    p_check.add_argument("--notify", action="store_true")
    p_check.add_argument("--refresh", action="store_true")
    p_check.add_argument("--no-write", action="store_true")
    p_check.add_argument("--fail-on-updates", action="store_true")
    p_check.set_defaults(func=command_check)

    p_list = sub.add_parser("list", help="Show latest cached report")
    _add_json(p_list)
    p_list.set_defaults(func=command_list)

    p_status = sub.add_parser("status", help="Alias for list; default when no command is given")
    _add_json(p_status)
    p_status.set_defaults(func=command_list)

    p_details = sub.add_parser("details", help="Show full cached report details without network")
    _add_json(p_details)
    p_details.set_defaults(func=command_details)

    p_live = sub.add_parser("live", help="Show 9999/live divergence from latest cached report")
    _add_json(p_live)
    p_live.set_defaults(func=command_live)

    p_explain = sub.add_parser("explain", help="Explain one package mapping and latest result")
    p_explain.add_argument("package")
    p_explain.set_defaults(func=command_explain)

    p_install = sub.add_parser("install-system", help="Install system wrappers and optional cron/postsync hooks")
    p_install.add_argument("--overlay", dest="install_overlay", type=Path, default=None)
    p_install.add_argument("--config", dest="install_config", type=Path, default=Path("/etc/portage/release-watch.json"))
    p_install.add_argument("--prefix", type=Path, default=Path("/usr/local"))
    p_install.add_argument("--state-dir", dest="install_state_dir", type=Path, default=Path("/var/lib/portage-release-watch"))
    p_install.add_argument("--cache-dir", dest="install_cache_dir", type=Path, default=Path("/var/cache/portage-release-watch"))
    p_install.add_argument("--notify-hooks-dir", type=Path, default=Path("/etc/portage/release-watch.notify.d"))
    p_install.add_argument("--scheduler", choices=("cron", "none"), default="none")
    p_install.add_argument("--postsync", dest="postsync", action="store_true", default=False)
    p_install.add_argument("--no-postsync", dest="postsync", action="store_false")
    p_install.add_argument("--alias-prw", dest="alias_prw", action="store_true", default=True)
    p_install.add_argument("--no-alias-prw", dest="alias_prw", action="store_false")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=install_system)
    return parser


def _resolve_common_args(args: argparse.Namespace) -> None:
    if args.command == "install-system":
        if args.install_overlay is None:
            args.install_overlay = detect_default_overlay(Path.cwd())
        return
    args.overlay = (args.overlay.expanduser().resolve() if args.overlay is not None else detect_default_overlay(Path.cwd()))
    if args.config is not None:
        args.config = args.config.expanduser().resolve()
    if args.system:
        args.state_dir = SYSTEM_STATE_DIR
        args.cache_dir = SYSTEM_CACHE_DIR
    args.state_dir = args.state_dir.expanduser().resolve()
    args.cache_dir = args.cache_dir.expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "status"
        args.json = False
        args.func = command_list
    try:
        _resolve_common_args(args)
        return args.func(args)
    except WatchError as exc:
        print(f"portage-release-watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
