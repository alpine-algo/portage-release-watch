from __future__ import annotations

from portage_release_watch.cli import main
from portage_release_watch.http import HttpClient
from portage_release_watch.overlay import scan_overlay
from portage_release_watch.report import build_report, evaluate_package
from portage_release_watch.sources import resolve_rule

from helpers import CACHE, OVERLAY, install_fake_portage


def test_check_report_contains_update_manual_and_live_divergence(monkeypatch):
    install_fake_portage(monkeypatch)
    config = {"schema_version": 2, "dynamic": {"enabled": True}, "packages": {}}
    http = HttpClient(CACHE / "http", timeout=1, max_age_hours=24)
    infos = scan_overlay(OVERLAY)
    rows = [evaluate_package(info, resolve_rule(config, info, OVERLAY), http, False) for _cp, info in sorted(infos.items())]
    report = build_report(rows, [], OVERLAY)
    updates = {row["cp"]: row for row in report["updates"]}
    assert updates["app-misc/fastfetch"]["upstream"]["raw"] == "2.65.2"
    assert updates["app-emulation/qemu"]["upstream"]["raw"] == "v11.0.2"
    assert "live_divergence" in updates["app-emulation/qemu"]
    assert {row["cp"] for row in report["manual"]} == {"media-video/davinci-resolve"}
    assert {row["cp"] for row in report["live"] if row["status"] == "live"} == {"www-client/firefox-nightly-bin"}
