from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import portage_release_watch.install as install_module
from portage_release_watch.install import PlannedFile, install_system
from portage_release_watch.models import WatchError


def _args(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = {
        "install_overlay": tmp_path / "overlay",
        "install_config": None,
        "prefix": tmp_path / "prefix",
        "install_state_dir": tmp_path / "state",
        "install_cache_dir": tmp_path / "cache",
        "notify_hooks_dir": tmp_path / "notify.d",
        "scheduler": "none",
        "postsync": False,
        "alias_prw": True,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("with_config", [False, True])
def test_generated_runners_preserve_quoted_paths_and_optional_config(
    with_config, tmp_path, monkeypatch
):
    prefix = tmp_path / "prefix with spaces"
    overlay = tmp_path / "overlay;$(unsafe)"
    state_dir = tmp_path / "state dir"
    cache_dir = tmp_path / "cache dir"
    config = tmp_path / "config 'quoted'.json" if with_config else None
    if config is not None:
        config.write_text("{}")

    args = _args(
        tmp_path,
        install_overlay=overlay,
        install_config=config,
        prefix=prefix,
        install_state_dir=state_dir,
        install_cache_dir=cache_dir,
        scheduler="cron",
        postsync=True,
    )
    planned: list[PlannedFile] = []
    real_is_dir = Path.is_dir

    def is_dir(path: Path) -> bool:
        if path == Path("/etc/cron.daily"):
            return True
        return real_is_dir(path)

    def capture(path: Path, content: str, mode: int) -> None:
        planned.append(PlannedFile(path, content, mode))

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(install_module.os, "chmod", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(install_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install_module, "_write_file", capture)

    assert install_system(args) == 0

    executable = prefix.resolve() / "bin/portage-release-watch"
    cron_path = Path("/etc/cron.daily/portage-release-watch")
    postsync_path = Path("/etc/portage/postsync.d/90-portage-release-watch")
    by_path = {item.path: item for item in planned}
    assert set(by_path) == {
        executable,
        prefix.resolve() / "bin/prw",
        cron_path,
        postsync_path,
    }
    assert all(item.mode == 0o755 for item in planned)

    common = [
        "exec",
        str(executable),
        "--overlay",
        str(overlay.resolve()),
    ]
    quoted_paths = [executable, overlay.resolve(), state_dir.resolve(), cache_dir.resolve()]
    if config is not None:
        common.extend(("--config", str(config.resolve())))
        quoted_paths.append(config.resolve())
    common.extend(
        (
            "--state-dir",
            str(state_dir.resolve()),
            "--cache-dir",
            str(cache_dir.resolve()),
        )
    )

    for runner_path, timeout in ((cron_path, "30"), (postsync_path, "8")):
        content = by_path[runner_path].content
        command_line = content.splitlines()[1]
        assert content.startswith("#!/bin/sh\n")
        assert content.endswith("\n")
        assert shlex.split(command_line) == [
            *common,
            "--timeout-seconds",
            timeout,
            "--max-age-hours",
            "24",
            "check",
            "--quiet",
            "--notify",
        ]
        assert "--system" not in command_line
        assert ("--config" in command_line) is with_config
        for path in quoted_paths:
            assert shlex.quote(str(path)) in command_line


def test_scheduler_and_postsync_remain_opt_in(tmp_path, capsys):
    args = _args(tmp_path, dry_run=True)

    assert install_system(args) == 0

    output = capsys.readouterr().out
    assert "/etc/cron.daily/portage-release-watch" not in output
    assert "/etc/portage/postsync.d/90-portage-release-watch" not in output
    assert str(tmp_path / "prefix/bin/portage-release-watch") in output
    assert str(tmp_path / "prefix/bin/prw") in output
    assert "Dry run: wrote nothing." in output


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("missing", "No such file or directory"), ("unreadable", "Permission denied")],
)
def test_explicit_unreadable_config_fails_before_install_writes(
    failure, expected, tmp_path, capsys, monkeypatch
):
    config = tmp_path / "release-watch.json"
    if failure == "unreadable":
        config.write_text("{}")
        target = config.resolve()
        real_open = Path.open

        def denied(path: Path, *args, **kwargs):
            if path == target:
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)

    def unexpected(*_args, **_kwargs):
        pytest.fail("installer mutated the filesystem before validating config")

    monkeypatch.setattr(install_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "mkdir", unexpected)
    monkeypatch.setattr(install_module.os, "chmod", unexpected)
    monkeypatch.setattr(install_module, "_write_file", unexpected)

    with pytest.raises(WatchError, match="cannot read config") as exc_info:
        install_system(_args(tmp_path, install_config=config))

    assert expected in str(exc_info.value)
    assert str(config.resolve()) in str(exc_info.value)
    assert capsys.readouterr().out == ""
