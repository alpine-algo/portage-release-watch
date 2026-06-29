from __future__ import annotations

import json
from pathlib import Path

import pytest

from portage_release_watch.cli import main

from helpers import CACHE, OVERLAY, install_fake_portage


def test_cli_check_no_write_json_uses_fixture_cache(tmp_path, capsys, monkeypatch):
    install_fake_portage(monkeypatch)
    state = tmp_path / "state"
    code = main(["--overlay", str(OVERLAY), "--state-dir", str(state), "--cache-dir", str(CACHE), "check", "--json", "--no-write"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["cp"] for row in payload["updates"]} == {"app-emulation/qemu", "app-misc/fastfetch"}
    assert not (state / "latest-report.json").exists()


def test_default_command_is_status(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "latest-report.json").write_text(json.dumps({
        "generated_at": "2026-06-29T00:00:00Z",
        "overlay": str(OVERLAY),
        "updates": [],
        "manual": [],
        "live": [],
        "warnings": [],
        "packages": [],
        "summary": {},
    }))
    code = main(["--overlay", str(OVERLAY), "--state-dir", str(state), "--cache-dir", str(CACHE)])
    assert code == 0
    assert "Updates available: none" in capsys.readouterr().out


def test_install_system_dry_run_lists_files_without_writing(tmp_path, capsys):
    if not Path("/etc/cron.daily").is_dir():
        pytest.skip("/etc/cron.daily unavailable on this test host")
    prefix = tmp_path / "install"
    state = tmp_path / "var-lib"
    cache = tmp_path / "var-cache"
    code = main([
        "install-system", "--dry-run", "--overlay", str(OVERLAY), "--scheduler", "cron", "--postsync",
        "--prefix", str(prefix), "--state-dir", str(state), "--cache-dir", str(cache),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert str(prefix / "bin/portage-release-watch") in out
    assert str(prefix / "bin/prw") in out
    assert "/etc/cron.daily/portage-release-watch" in out
    assert "/etc/portage/postsync.d/90-portage-release-watch" in out
    assert not prefix.exists()
    assert not state.exists()
    assert not cache.exists()
