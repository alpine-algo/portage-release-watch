from __future__ import annotations

import re

from .models import Candidate, PackageInfo, WatchError

PRERELEASE_RE = re.compile(r"(?:alpha|beta|rc|pre|preview|dev|nightly|snapshot)", re.I)
PORTAGE_REQUIRED = "Gentoo Portage Python API is required for version comparison; run on Gentoo with sys-apps/portage installed."


def compare_versions(a: str, b: str) -> int:
    try:
        from portage.versions import vercmp
    except Exception as exc:
        raise WatchError(PORTAGE_REQUIRED) from exc
    result = vercmp(a, b)
    if result is None:
        raise WatchError(f"Portage could not compare versions: {a!r} vs {b!r}")
    return result


def newest_infos(infos: list[PackageInfo]) -> PackageInfo:
    best = infos[0]
    for info in infos[1:]:
        if best.pv is None:
            best = info
            continue
        if info.pv is None:
            continue
        cmp = compare_versions(info.pv, best.pv)
        if cmp > 0 or (cmp == 0 and (info.pr or "r0") > (best.pr or "r0")):
            best = info
    return best


def extract_version(raw: str, source: dict) -> str | None:
    regex = source.get("version_regex")
    if regex:
        m = re.search(regex, raw)
        if not m:
            return None
        if "version" in m.groupdict():
            return m.group("version")
        if m.groups():
            return m.group(1)
    version = raw.strip()
    if version.startswith(("v", "V")) and re.match(r"^[vV]\d", version):
        version = version[1:]
    return version


def candidate_allowed(raw: str, version: str, source: dict) -> bool:
    include = source.get("include_prereleases", False)
    if not include and (PRERELEASE_RE.search(raw) or PRERELEASE_RE.search(version)):
        return False
    include_regex = source.get("include_regex")
    if include_regex and not re.search(include_regex, raw):
        return False
    exclude_regex = source.get("exclude_regex")
    if exclude_regex and re.search(exclude_regex, raw):
        return False
    return True


def best_candidate(candidates: list[Candidate]) -> Candidate | None:
    best: Candidate | None = None
    for cand in candidates:
        if best is None or compare_versions(cand.version, best.version) > 0:
            best = cand
    return best
