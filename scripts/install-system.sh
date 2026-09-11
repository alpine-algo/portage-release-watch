#!/bin/sh
set -eu
# Import only from a private staged copy, never a privileged PYTHONPATH checkout.
exec /usr/bin/python3 -I -B -c '
import os
import shutil
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv.pop(1)).resolve().parent.parent / "src/portage_release_watch"
parent = Path("/root") if os.geteuid() == 0 else None
if parent is not None:
    for path in (*reversed(parent.parents), parent):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            sys.exit("portage-release-watch: installer staging ancestor is not trusted")
with tempfile.TemporaryDirectory(prefix="portage-release-watch-install-", dir=parent) as temporary:
    package = Path(temporary) / "portage_release_watch"
    package.mkdir(mode=0o755)
    for module in source.glob("*.py"):
        target = package / module.name
        shutil.copyfile(module, target)
        target.chmod(0o644)
    sys.path.insert(0, temporary)
    from portage_release_watch.cli import main
    result = main(["install-system", *sys.argv[1:]])
sys.exit(result)
' "$0" "$@"
