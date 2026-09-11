from __future__ import annotations

import pytest

from portage_release_watch.models import PackageInfo, WatchError
from portage_release_watch.versioning import compare_versions, extract_version, newest_infos

from helpers import install_fake_portage


def test_compare_versions_uses_portage_when_available():
    pytest.importorskip("portage.versions", reason="requires Gentoo Portage Python API")
    assert compare_versions("2.0", "1.9") > 0
    assert compare_versions("1.0", "1.0") == 0


def test_newest_infos_uses_complete_pvr_ordering(monkeypatch):
    install_fake_portage(monkeypatch)

    def info(pv: str, pr: str = "r0") -> PackageInfo:
        pvr = pv if pr == "r0" else f"{pv}-{pr}"
        return PackageInfo(
            cp="cat/pkg",
            category="cat",
            pn="pkg",
            pv=pv,
            pvr=pvr,
            pf=f"pkg-{pvr}",
            pr=pr,
            live=False,
            ebuilds=[],
        )

    r0 = info("1.0")
    r2 = info("1.0", "r2")
    r10 = info("1.0", "r10")
    higher_pv = info("1.1")

    assert newest_infos([r2, r10]) is r10
    assert newest_infos([r10, r2]) is r10
    assert newest_infos([r0, r2]) is r2
    assert newest_infos([r10, higher_pv]) is higher_pv


def test_date_normalization_is_opt_in_and_rejects_invalid_dates():
    assert extract_version("v2026.07.29", {}) == "2026.07.29"
    source = {"normalize": "date-dotted-to-compact", "version_regex": r"^release-(?P<version>.+)$"}
    assert extract_version("release-2026.07.29", source) == "20260729"
    assert extract_version("release-2026.02.30", source) is None
    assert extract_version("release-2.6.7", source) is None


def test_python_normalization_preserves_development_prerelease_and_postrelease_order():
    pytest.importorskip("portage.versions", reason="requires Gentoo Portage Python API")
    source = {"normalize": "python-to-gentoo"}
    releases = [
        "6.0.0.dev2", "6.0.0.dev10", "6.0.0a0.dev1", "6.0.0a0",
        "6.0.0a6", "6.0.0a9", "6.0.0b0.dev1", "6.0.0b0",
        "6.0.0rc1", "6.0.0", "6.0.0.post0.dev1", "6.0.0.post0",
    ]
    normalized = [extract_version(version, source) for version in releases]
    assert extract_version("6.0.0a9", source) == "6.0.0_alpha9"
    assert all(compare_versions(left, right) < 0 for left, right in zip(normalized, normalized[1:]))


def test_python_normalization_rejects_unrepresentable_versions():
    source = {"normalize": "python-to-gentoo"}
    assert extract_version("not-a-version", source) is None
    for version in ("1!6.0.0", "6.0.0+vendor"):
        with pytest.raises(WatchError):
            extract_version(version, source)
