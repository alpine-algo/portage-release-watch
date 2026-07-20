from __future__ import annotations

import json

import pytest

import portage_release_watch.cli as cli_module
import portage_release_watch.report as report_module
from portage_release_watch.cli import main
from portage_release_watch.http import FetchResult, HttpClient
from portage_release_watch.models import Candidate, PackageInfo, WatchError
from portage_release_watch.overlay import scan_overlay
from portage_release_watch.report import build_report, evaluate_package, notice_text
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


def _package_info(cp: str = "app-misc/example") -> PackageInfo:
    category, pn = cp.split("/", 1)
    return PackageInfo(
        cp=cp,
        category=category,
        pn=pn,
        pv="1.0",
        pvr="1.0",
        pf=f"{pn}-1.0",
        pr=None,
        live=False,
        ebuilds=[f"{pn}-1.0.ebuild"],
    )


def test_stale_degradation_is_current_run_warning_without_status_change(tmp_path, monkeypatch):
    install_fake_portage(monkeypatch)
    candidate = Candidate(
        raw="2.0",
        version="2.0",
        url="https://example.invalid/releases/2.0",
        source_id="url:https://example.invalid/releases",
    )
    outcomes = iter([
        FetchResult([candidate], "TimeoutError: upstream unavailable"),
        FetchResult([candidate]),
        WatchError("https://example.invalid/releases: network unavailable"),
    ])

    def fetch_candidates(*args, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(report_module, "fetch_candidates", fetch_candidates)
    info = _package_info()
    rule = {
        "_origin": "explicit-config",
        "source": {
            "type": "url-regex",
            "url": "https://example.invalid/releases",
            "version_regex": r"(?P<version>\\d+\\.\\d+)",
        },
    }
    http = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)

    degraded = evaluate_package(info, rule, http, force=True)
    healthy = evaluate_package(info, rule, http, force=True)
    failed = evaluate_package(info, rule, http, force=True)

    assert degraded["status"] == "outdated"
    assert degraded["stale_error"] == "TimeoutError: upstream unavailable"
    assert healthy["status"] == "outdated"
    assert "stale_error" not in healthy
    assert failed["status"] == "failed"
    degraded_report = build_report([degraded], [], tmp_path)
    healthy_report = build_report([healthy], [], tmp_path)
    assert degraded_report["warnings"] == [degraded]
    assert healthy_report["warnings"] == []
    assert "[outdated; stale cache] TimeoutError: upstream unavailable" in notice_text(degraded_report)


@pytest.mark.parametrize(
    ("rows", "expected_exit"),
    [
        (
            [
                {
                    "cp": "app-misc/failed",
                    "status": "failed",
                    "local_pv": "1.0",
                    "local_pvr": "1.0",
                    "message": "provider unavailable",
                },
                {
                    "cp": "app-misc/outdated",
                    "status": "outdated",
                    "local_pv": "1.0",
                    "local_pvr": "1.0",
                    "message": "1.0 -> 2.0",
                    "upstream": {"raw": "2.0", "source_id": "test:outdated"},
                },
            ],
            1,
        ),
        (
            [
                {
                    "cp": "app-misc/outdated",
                    "status": "outdated",
                    "local_pv": "1.0",
                    "local_pvr": "1.0",
                    "message": "1.0 -> 2.0",
                    "upstream": {"raw": "2.0", "source_id": "test:outdated"},
                },
            ],
            2,
        ),
        (
            [
                {
                    "cp": "app-misc/current",
                    "status": "current",
                    "local_pv": "1.0",
                    "local_pvr": "1.0",
                    "message": "current at 1.0",
                    "stale_error": "TimeoutError: upstream unavailable",
                },
            ],
            0,
        ),
    ],
)
def test_check_exit_precedence_after_report_output_and_persistence(
    tmp_path,
    monkeypatch,
    capsys,
    rows,
    expected_exit,
):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    infos = {row["cp"]: _package_info(row["cp"]) for row in rows}
    rows_by_cp = {row["cp"]: row for row in rows}
    monkeypatch.setattr(cli_module, "_resolve_common_args", lambda args: None)
    monkeypatch.setattr(cli_module, "load_config", lambda *args: ({"schema_version": 2}, []))
    monkeypatch.setattr(cli_module, "load_github_token", lambda config: None)
    monkeypatch.setattr(cli_module, "scan_overlay", lambda path: infos)
    monkeypatch.setattr(cli_module, "resolve_rule", lambda *args: {"source": {"type": "url-regex"}})
    monkeypatch.setattr(
        cli_module,
        "evaluate_package",
        lambda info, rule, http, force: dict(rows_by_cp[info.cp]),
    )

    code = main([
        "--overlay",
        str(overlay),
        "--state-dir",
        str(state),
        "--cache-dir",
        str(cache),
        "check",
        "--json",
        "--fail-on-updates",
    ])

    payload = json.loads(capsys.readouterr().out)
    persisted = json.loads((state / "latest-report.json").read_text())
    assert code == expected_exit
    assert payload["packages"] == rows
    assert persisted["packages"] == rows
    if expected_exit == 1:
        assert payload["summary"] == {"failed": 1, "outdated": 1}
        assert payload["updates"]
    elif expected_exit == 0:
        assert payload["warnings"] == rows
