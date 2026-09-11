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




def test_detect_default_overlay_from_cwd(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay"
    pkg = overlay / "cat/pkg"
    pkg.mkdir(parents=True)
    (overlay / "profiles").mkdir()
    (overlay / "profiles/repo_name").write_text("local\n")
    (pkg / "pkg-1.0.0.ebuild").write_text("EAPI=8\n")
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_OVERLAY", raising=False)
    assert detect_default_overlay(pkg) == overlay.resolve()


def test_root_overlay_cannot_select_hooks_or_token_file(tmp_path, monkeypatch):
    import portage_release_watch.config as config_module
    monkeypatch.setattr(config_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config_module, "SYSTEM_POLICY_PATH", tmp_path / "absent-policy")
    (tmp_path / ".release-watch.json").write_text(json.dumps({
        "notify_hooks_dir": "/attacker/hooks",
        "github_token_file": "/attacker/token",
        "packages": {"cat/pkg": {"status": "manual"}},
    }))
    config, _ = load_config(None, tmp_path)
    assert config["notify_hooks_dir"] != "/attacker/hooks"
    assert "github_token_file" not in config
    assert config["packages"]["cat/pkg"]["status"] == "manual"


def test_trusted_path_rejects_writable_ancestor_and_symlinks(tmp_path, monkeypatch):
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    from portage_release_watch.config import trusted_path

    real_lstat = Path.lstat
    def root_owned(path):
        info = real_lstat(path)
        mode = info.st_mode & ~0o022
        if path == tmp_path:
            mode |= stat.S_IWGRP
        return SimpleNamespace(st_uid=0, st_mode=mode)
    monkeypatch.setattr(Path, "lstat", root_owned)
    with pytest.raises(WatchError, match="trusted path"):
        trusted_path(tmp_path / "runtime", missing_ok=True)

    def root_safe(path):
        info = real_lstat(path)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
    monkeypatch.setattr(Path, "lstat", root_safe)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(WatchError):
        trusted_path(link, missing_ok=True)


def test_root_ignores_environment_token_and_uses_only_private_policy_file(tmp_path, monkeypatch):
    import portage_release_watch.config as config_module
    monkeypatch.setattr(config_module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("GITHUB_TOKEN", "untrusted-environment-token")
    monkeypatch.setenv("PORTAGE_RELEASE_WATCH_GITHUB_TOKEN", "other-untrusted-token")
    assert config_module.load_github_token({}) is None
    token = tmp_path / "token"
    token.write_text("test-token")
    # Ancestor rejection is tested above; isolate the token's privacy boundary here.
    monkeypatch.setattr(config_module, "trusted_path", lambda path: path)
    token.chmod(0o644)
    with pytest.raises(WatchError, match="root-private"):
        config_module.load_github_token({"github_token_file": str(token)})
    token.chmod(0o600)
    assert config_module.load_github_token({"github_token_file": str(token)}) == "test-token"
