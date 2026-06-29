from __future__ import annotations

import pytest

from portage_release_watch.versioning import compare_versions


def test_compare_versions_uses_portage_when_available():
    pytest.importorskip("portage.versions", reason="requires Gentoo Portage Python API")
    assert compare_versions("2.0", "1.9") > 0
    assert compare_versions("1.0", "1.0") == 0
