from __future__ import annotations

import re
from datetime import datetime

from packaging.version import InvalidVersion, Version

from .models import Candidate, PackageInfo, WatchError

PRERELEASE_RE = re.compile(r"(?:alpha|beta|rc|pre|preview|dev|nightly|snapshot)", re.I)
PORTAGE_REQUIRED = "Gentoo Portage Python API is required for version comparison; run on Gentoo with sys-apps/portage installed."
_PYTHON_PRE_SUFFIXES = {"a": "alpha", "b": "beta", "rc": "rc"}


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
        cmp = compare_versions(info.pvr or info.pv, best.pvr or best.pv)
        if cmp > 0:
            best = info
    return best


def normalize_version(version: str, source: dict) -> str | None:
    normalization = source.get("normalize")
    if normalization is None:
        return version
    if normalization == "debian-hyphen-to-gentoo-dot":
        return version.replace("-", ".")
    if normalization == "date-dotted-to-compact":
        if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", version):
            return None
        try:
            datetime.strptime(version, "%Y.%m.%d")
        except ValueError:
            return None
        return version.replace(".", "")
    if normalization == "python-to-gentoo":
        try:
            parsed = Version(version)
        except InvalidVersion:
            return None
        if parsed.epoch or parsed.local:
            raise WatchError(f"Python version epochs and local identifiers cannot map to Portage: {version!r}")
        normalized = ".".join(map(str, parsed.release))
        if parsed.pre is not None:
            kind, number = parsed.pre
            normalized += f"_{_PYTHON_PRE_SUFFIXES[kind]}{number}"
        if parsed.post is not None:
            normalized += f"_p{parsed.post}"
        if parsed.dev is not None:
            if parsed.pre is None and parsed.post is None:
                normalized += "_alpha0_alpha0"
            normalized += f"_pre{parsed.dev}"
        return normalized
    raise WatchError(f"unsupported normalize {normalization!r}")


def extract_version(raw: str, source: dict) -> str | None:
    version = raw.strip()
    regex = source.get("version_regex")
    if regex:
        match = re.search(regex, raw)
        if not match:
            return None
        if "version" in match.groupdict():
            version = match.group("version")
        elif match.groups():
            version = match.group(1)
    if version.startswith(("v", "V")) and re.match(r"^[vV]\d", version):
        version = version[1:]
    return normalize_version(version, source)


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
