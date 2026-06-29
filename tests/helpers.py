from __future__ import annotations

import re
import sys
import types
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
OVERLAY = FIXTURES / "overlay"
CACHE = FIXTURES / "cache"


def _version_parts(value: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", value)]


def install_fake_portage(monkeypatch):
    portage = types.ModuleType("portage")
    versions = types.ModuleType("portage.versions")

    def vercmp(a, b):
        aa, bb = _version_parts(a), _version_parts(b)
        return (aa > bb) - (aa < bb)

    def pkgsplit(pf):
        if "-" not in pf:
            return None
        pn, pv = pf.rsplit("-", 1)
        pr = "r0"
        m = re.match(r"(.+)-r(\d+)$", pv)
        if m:
            pv, pr = m.group(1), "r" + m.group(2)
        return pn, pv, pr

    versions.vercmp = vercmp
    versions.pkgsplit = pkgsplit
    portage.versions = versions
    monkeypatch.setitem(sys.modules, "portage", portage)
    monkeypatch.setitem(sys.modules, "portage.versions", versions)
