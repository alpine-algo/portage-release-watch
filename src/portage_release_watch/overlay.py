from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .models import PackageInfo
from .versioning import newest_infos


def split_pf(pf: str, pn: str) -> tuple[str, str | None, str | None]:
    try:
        from portage.versions import pkgsplit
    except Exception:
        pkgsplit = None
    if pkgsplit is not None:
        split = pkgsplit(pf)
        if split:
            split_pn, pv, pr = split
            return split_pn, pv, pr
    prefix = pn + "-"
    pv = pf[len(prefix):] if pf.startswith(prefix) else None
    return pn, pv, "r0"


def scan_overlay(overlay: Path) -> dict[str, PackageInfo]:
    grouped: dict[str, list[PackageInfo]] = {}
    for ebuild in sorted(overlay.glob("*/*/*.ebuild")):
        rel = ebuild.relative_to(overlay)
        category, pn = rel.parts[0], rel.parts[1]
        pf = ebuild.stem
        split_pn, pv, pr = split_pf(pf, pn)
        live = pv == "9999" or (pv or "").endswith("9999")
        cp = f"{category}/{pn}"
        pvr = None if pv is None else (pv if pr in (None, "r0") else f"{pv}-{pr}")
        info = PackageInfo(cp, category, split_pn, pv, pvr, pf, pr, live, [str(rel)])
        grouped.setdefault(cp, []).append(info)

    selected: dict[str, PackageInfo] = {}
    for cp, infos in grouped.items():
        fixed = [i for i in infos if not i.live]
        live_present = any(i.live for i in infos)
        chosen = newest_infos(fixed) if fixed else newest_infos(infos)
        chosen.live = live_present
        chosen.ebuilds = sorted(e for info in infos for e in info.ebuilds)
        selected[cp] = chosen
    return selected


def selected_ebuild_path(overlay: Path, info: PackageInfo) -> Path | None:
    if not info.pf:
        return None
    path = overlay / info.category / info.pn / f"{info.pf}.ebuild"
    return path if path.exists() else None


def package_metadata_path(overlay: Path, info: PackageInfo) -> Path:
    return overlay / info.category / info.pn / "metadata.xml"


def read_metadata_remote_ids(overlay: Path, info: PackageInfo) -> list[tuple[str, str]]:
    path = package_metadata_path(overlay, info)
    if not path.exists():
        return []
    try:
        root = ET.fromstring(path.read_text())
    except Exception:
        return []
    remotes: list[tuple[str, str]] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "remote-id":
            continue
        typ = elem.attrib.get("type")
        val = (elem.text or "").strip()
        if typ and val:
            remotes.append((typ, val))
    return remotes


def read_selected_ebuild(overlay: Path, info: PackageInfo) -> str:
    path = selected_ebuild_path(overlay, info)
    if not path:
        return ""
    try:
        return path.read_text(errors="replace")
    except Exception:
        return ""
