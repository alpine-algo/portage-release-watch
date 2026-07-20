from __future__ import annotations

import json

import pytest

from portage_release_watch.config import detect_default_overlay, load_config
from portage_release_watch.models import WatchError


def test_load_config_merges_overlay_and_cli_recursively(tmp_path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / ".release-watch.json").write_text(json.dumps({"dynamic": {"enabled": False}, "packages": {"cat/pkg": {"status": "manual"}}}))
    cli = tmp_path / "release-watch.json"
    cli.write_text(json.dumps({"dynamic": {"enabled": True}}))
    config, sources = load_config(cli, overlay)
    assert config["schema_version"] == 2
    assert config["dynamic"]["enabled"] is True
    assert config["packages"]["cat/pkg"]["status"] == "manual"
    assert sources[-1] == str(cli)


def test_missing_explicit_config_raises_exact_message(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(WatchError, match=f"config not found: {missing}"):
        load_config(missing, tmp_path)


def test_detect_default_overlay_from_cwd(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay"
    pkg = overlay / "cat/pkg"
    pkg.mkdir(parents=True)
    (overlay / "profiles").mkdir()
    (overlay / "profiles/repo_name").write_text("local\n")
    (pkg / "pkg-1.0.0.ebuild").write_text("EAPI=8\n")
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_OVERLAY", raising=False)
    assert detect_default_overlay(pkg) == overlay.resolve()
