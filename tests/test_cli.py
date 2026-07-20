from __future__ import annotations

import json
from pathlib import Path

import pytest

import portage_release_watch.cli as cli_module
from portage_release_watch import __version__
from portage_release_watch.cli import build_parser, main

from helpers import CACHE, OVERLAY, install_fake_portage


def _cached_report() -> dict[str, object]:
    return {
        "generated_at": "2026-06-29T00:00:00Z",
        "overlay": str(OVERLAY),
        "updates": [],
        "manual": [],
        "live": [],
        "warnings": [],
        "packages": [],
        "summary": {},
    }


def _deny_read(monkeypatch, target: Path) -> None:
    original = Path.read_text

    def denied(path: Path, *args, **kwargs):
        if path == target:
            raise PermissionError(13, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)


def _state_snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_cli_check_no_write_json_uses_fixture_cache(tmp_path, capsys, monkeypatch):
    install_fake_portage(monkeypatch)
    state = tmp_path / "state"
    code = main(["--overlay", str(OVERLAY), "--state-dir", str(state), "--cache-dir", str(CACHE), "check", "--json", "--no-write"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["cp"] for row in payload["updates"]} == {"app-emulation/qemu", "app-misc/fastfetch"}
    assert not (state / "latest-report.json").exists()


def test_default_command_is_status_without_overlay(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "latest-report.json").write_text(json.dumps(_cached_report()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_OVERLAY", raising=False)
    code = main(["--state-dir", str(state), "--cache-dir", str(CACHE)])
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


@pytest.mark.parametrize("command", ["status", "list", "details", "live"])
def test_cached_commands_need_no_overlay_scan_or_network(
    command, tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    (state / "latest-report.json").write_text(json.dumps(_cached_report()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_OVERLAY", raising=False)

    def unexpected(*_args, **_kwargs):
        pytest.fail("cached report command attempted overlay or network access")

    monkeypatch.setattr(cli_module, "detect_default_overlay", unexpected)
    monkeypatch.setattr(cli_module, "scan_overlay", unexpected)
    monkeypatch.setattr(cli_module, "HttpClient", unexpected)

    assert main(["--state-dir", str(state), command]) == 0
    captured = capsys.readouterr()
    assert captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("command", "source", "path_kind", "expected"),
    [
        ("scan", "explicit", "missing", "does not exist"),
        ("check", "environment", "missing", "does not exist"),
        ("explain", "explicit", "file", "not a directory"),
        ("scan", "environment", "file", "not a directory"),
        ("check", "explicit", "unrecognized", "not recognized"),
        ("explain", "environment", "unrecognized", "not recognized"),
    ],
)
def test_overlay_consuming_commands_reject_selected_invalid_overlay(
    command, source, path_kind, expected, tmp_path, capsys, monkeypatch
):
    overlay = tmp_path / "selected-overlay"
    if path_kind == "file":
        overlay.write_text("not an overlay\n")
    elif path_kind == "unrecognized":
        overlay.mkdir()

    monkeypatch.chdir(tmp_path)
    command_args = {
        "scan": ["scan"],
        "check": ["check", "--no-write"],
        "explain": ["explain", "cat/pkg"],
    }[command]
    if source == "environment":
        monkeypatch.setenv("PORTAGE_RELEASE_WATCH_OVERLAY", str(overlay))
        argv = command_args
    else:
        monkeypatch.delenv("PORTAGE_RELEASE_WATCH_OVERLAY", raising=False)
        argv = ["--overlay", str(overlay), *command_args]

    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(overlay.resolve()) in captured.err
    assert expected in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.splitlines()) == 1


def test_package_check_prints_without_touching_canonical_state(
    tmp_path, capsys, monkeypatch
):
    install_fake_portage(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    for name in (
        "latest-report.json",
        "latest-notice.txt",
        "history.ndjson",
        "notify-state.json",
    ):
        (state / name).write_text(f"sentinel:{name}\n")
    before = _state_snapshot(state)

    def unexpected_notify(*_args, **_kwargs):
        pytest.fail("package-scoped check attempted notification")

    monkeypatch.setattr(cli_module, "maybe_notify", unexpected_notify)
    code = main(
        [
            "--overlay",
            str(OVERLAY),
            "--state-dir",
            str(state),
            "--cache-dir",
            str(CACHE),
            "check",
            "--package",
            "app-misc/fastfetch",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["cp"] for row in payload["packages"]] == ["app-misc/fastfetch"]
    assert _state_snapshot(state) == before


def test_package_check_rejects_notify(tmp_path, capsys):
    state = tmp_path / "state"
    assert (
        main(
            [
                "--overlay",
                str(OVERLAY),
                "--state-dir",
                str(state),
                "check",
                "--package",
                "app-misc/fastfetch",
                "--notify",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--package cannot be combined with --notify" in captured.err
    assert "Traceback" not in captured.err
    assert not state.exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("malformed", "invalid config JSON"), ("unreadable", "cannot read config")],
)
def test_config_read_failures_are_concise_and_do_not_write(
    failure, expected, tmp_path, capsys, monkeypatch
):
    config = tmp_path / "config.json"
    if failure == "malformed":
        config.write_text("{")
    else:
        config.write_text('{"secret": "must-not-appear"}')
        _deny_read(monkeypatch, config)
    state = tmp_path / "state"

    assert (
        main(
            [
                "--overlay",
                str(OVERLAY),
                "--config",
                str(config),
                "--state-dir",
                str(state),
                "check",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert str(config) in captured.err
    assert "must-not-appear" not in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.splitlines()) == 1
    assert not state.exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("malformed", "invalid cached report JSON"),
        ("unreadable", "cannot read cached report"),
    ],
)
def test_cached_report_read_failures_are_concise_and_do_not_write(
    failure, expected, tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    report = state / "latest-report.json"
    if failure == "malformed":
        report.write_text("{")
    else:
        report.write_text('{"secret": "must-not-appear"}')
        _deny_read(monkeypatch, report)
    before = report.read_bytes()
    monkeypatch.chdir(tmp_path)

    assert main(["--state-dir", str(state), "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert str(report) in captured.err
    assert "must-not-appear" not in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.splitlines()) == 1
    assert report.read_bytes() == before


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("missing", "cannot read GitHub token file"),
        ("unreadable", "cannot read GitHub token file"),
        ("empty", "GitHub token file is empty"),
    ],
)
def test_unusable_configured_token_file_fails_without_exposing_content(
    failure, expected, tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_GITHUB_TOKEN", raising=False)
    token = tmp_path / "github-token"
    secret = "ghp_must_not_appear"
    if failure == "unreadable":
        token.write_text(secret)
        _deny_read(monkeypatch, token)
    elif failure == "empty":
        token.write_text(" \n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"github_token_file": str(token)}))

    assert (
        main(
            [
                "--overlay",
                str(OVERLAY),
                "--config",
                str(config),
                "--state-dir",
                str(tmp_path / "state"),
                "check",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert str(token) in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert len(captured.err.splitlines()) == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--help"],
            (
                "--version",
                "Show the package version",
                "--overlay PATH",
                "Portage overlay for scan",
                "--config PATH",
                "JSON configuration override",
                "--state-dir PATH",
                "canonical reports and notification state",
                "--cache-dir PATH",
                "cached provider responses",
                "--system",
                "canonical root state",
                "--timeout-seconds SECONDS",
                "HTTP request timeout",
                "--max-age-hours HOURS",
                "Maximum age of a fresh provider cache entry",
                "Exit codes:",
            ),
        ),
        (["scan", "--help"], ("--json", "Emit JSON instead of human-readable text")),
        (
            ["check", "--help"],
            (
                "--package CP",
                "do not persist canonical state",
                "--json",
                "--quiet",
                "Suppress an unchanged",
                "--notify",
                "incompatible with --package",
                "--refresh",
                "Refresh provider responses",
                "--no-write",
                "without writing canonical state",
                "--fail-on-updates",
                "Exit 2",
            ),
        ),
        (["list", "--help"], ("--json", "Emit JSON instead of human-readable text")),
        (["status", "--help"], ("--json", "Emit JSON instead of human-readable text")),
        (["details", "--help"], ("--json", "Emit JSON instead of human-readable text")),
        (["live", "--help"], ("--json", "Emit JSON instead of human-readable text")),
        (["explain", "--help"], ("CP", "Category/package to explain")),
        (
            ["install-system", "--help"],
            (
                "--overlay PATH",
                "Portage overlay embedded",
                "--config PATH",
                "Configuration path embedded",
                "--prefix PATH",
                "Installation prefix",
                "--state-dir PATH",
                "State directory embedded",
                "--cache-dir PATH",
                "Cache directory embedded",
                "--notify-hooks-dir PATH",
                "executable notification hooks",
                "--scheduler {cron,none}",
                "Install a cron runner",
                "--postsync",
                "--no-postsync",
                "--alias-prw",
                "--no-alias-prw",
                "--dry-run",
                "without writing files",
            ),
        ),
    ],
)
def test_help_describes_every_option(argv, expected, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "240")
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for fragment in expected:
        assert fragment in captured.out


def test_version_uses_package_metadata_without_overlay(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    def unexpected(*_args, **_kwargs):
        pytest.fail("--version attempted overlay resolution")

    monkeypatch.setattr(cli_module, "detect_default_overlay", unexpected)
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.strip().endswith(__version__)
    assert len(captured.out.splitlines()) == 1


def test_install_config_omission_is_distinguishable_at_parse_boundary():
    parser = build_parser()
    assert parser.parse_args(["install-system"]).install_config is None
    explicit = Path("/tmp/custom-release-watch.json")
    assert (
        parser.parse_args(["install-system", "--config", str(explicit)]).install_config
        == explicit
    )


def test_full_check_still_writes_and_invokes_notification_contract(
    tmp_path, capsys, monkeypatch
):
    install_fake_portage(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PORTAGE_RELEASE_WATCH_GITHUB_TOKEN", raising=False)
    state = tmp_path / "state"
    hooks = tmp_path / "notify.d"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"notify_hooks_dir": str(hooks), "notify_repeat_hours": 42})
    )
    calls = []

    def record_notify(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(cli_module, "maybe_notify", record_notify)
    code = main(
        [
            "--overlay",
            str(OVERLAY),
            "--config",
            str(config),
            "--state-dir",
            str(state),
            "--cache-dir",
            str(CACHE),
            "check",
            "--json",
            "--notify",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert json.loads((state / "latest-report.json").read_text()) == payload
    notice = (state / "latest-notice.txt").read_text()
    history = [json.loads(line) for line in (state / "history.ndjson").read_text().splitlines()]
    assert history == [
        {"generated_at": payload["generated_at"], "summary": payload["summary"]}
    ]
    assert len(calls) == 1
    report_arg, notice_arg, state_arg, repeat_arg, logger_arg, hooks_arg = calls[0]
    assert report_arg == payload
    assert notice_arg == notice
    assert state_arg == state.resolve()
    assert repeat_arg == 42
    assert logger_arg is True
    assert hooks_arg == hooks
