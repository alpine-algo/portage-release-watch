from __future__ import annotations

import pytest

from portage_release_watch.models import PackageInfo
from portage_release_watch.versioning import compare_versions, newest_infos

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
