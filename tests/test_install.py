from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from portage_release_watch.install import PlannedFile, _check_replacement, _runner_content
from portage_release_watch.models import WatchError


def test_upgrade_refuses_unrecorded_or_locally_modified_files(tmp_path):
    path = tmp_path / "command"
    original = "#!/bin/sh\necho original\n"
    item = PlannedFile(path, "#!/bin/sh\necho upgraded\n", 0o755)
    path.write_text(original)
    with pytest.raises(WatchError):
        _check_replacement(item, {}, True, set())
    receipts = {str(path): hashlib.sha256(original.encode()).hexdigest()}
    with pytest.raises(WatchError):
        _check_replacement(item, receipts, False, set())
    _check_replacement(item, receipts, True, set())
    path.write_text("#!/bin/sh\necho local-customization\n")
    with pytest.raises(WatchError):
        _check_replacement(item, receipts, True, set())
    assert path.read_text().endswith("echo local-customization\n")


def test_upgrade_only_accepts_exact_legacy_script_and_never_symlink(tmp_path):
    path = tmp_path / "prw"
    original = '#!/bin/sh\nexec python3 -m portage_release_watch.cli "$@"\n'
    item = PlannedFile(path, "replacement", 0o755)
    path.write_text(original)
    _check_replacement(item, {}, True, {original})
    path.write_text(original + "echo injected\n")
    with pytest.raises(WatchError):
        _check_replacement(item, {}, True, {original})
    target = tmp_path / "other"
    target.write_text(original)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(WatchError):
        _check_replacement(item, {}, True, {original})
    assert target.read_text() == original


@pytest.mark.parametrize("with_config", [False, True])
def test_runner_passes_paths_as_arguments_not_shell_programs(tmp_path, with_config):
    executable = tmp_path / "command with spaces"
    executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o755)
    overlay = tmp_path / "overlay;$(unsafe)"
    config = tmp_path / "config 'quoted'.json" if with_config else None
    policy = tmp_path / "policy file"
    state = tmp_path / "state dir"
    cache = tmp_path / "cache dir"
    runner = tmp_path / "runner"
    runner.write_text(_runner_content(executable, overlay, config, state, cache, 8, policy))
    result = subprocess.run(["/bin/sh", str(runner)], check=True, text=True, capture_output=True)
    expected = ["--overlay", str(overlay)]
    if config:
        expected += ["--config", str(config)]
    expected += ["--policy", str(policy), "--state-dir", str(state), "--cache-dir", str(cache),
                 "--timeout-seconds", "8", "--max-age-hours", "24", "check", "--quiet", "--notify"]
    assert json.loads(result.stdout) == expected
