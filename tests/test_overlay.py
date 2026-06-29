from __future__ import annotations

from portage_release_watch.overlay import scan_overlay

from helpers import OVERLAY


def test_scan_overlay_selects_newest_fixed_and_marks_live_present():
    infos = scan_overlay(OVERLAY)
    qemu = infos["app-emulation/qemu"]
    assert qemu.pvr == "11.0.1"
    assert qemu.live is True
    assert sorted(qemu.ebuilds) == [
        "app-emulation/qemu/qemu-11.0.1.ebuild",
        "app-emulation/qemu/qemu-9999.ebuild",
    ]
