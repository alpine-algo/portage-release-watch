from __future__ import annotations

import io
import json
import re
import tarfile
import urllib.parse
from pathlib import Path
from typing import Any

from .http import FetchResult, HttpClient
from .models import Candidate, PackageInfo, WatchError
from .overlay import read_metadata_remote_ids, read_selected_ebuild
from .versioning import candidate_allowed, extract_version

DEFAULT_INFERRED_VERSION_RE = r"(?P<version>\d+(?:\.\d+)+(?:[._-]?[A-Za-z][A-Za-z0-9]*)*)"
DEFAULT_INFERRED_VERSION_REGEX = rf"^v?{DEFAULT_INFERRED_VERSION_RE}$"


def source_from_remote_id(kind: str, value: str, info: PackageInfo) -> dict[str, Any] | None:
    if kind == "github" and "/" in value:
        return {"type": "github", "repo": value, "mode": "auto", "version_regex": DEFAULT_INFERRED_VERSION_REGEX}
    if kind == "gitlab" and "/" in value:
        return {"type": "gitlab", "host": "gitlab.com", "project": value, "version_regex": DEFAULT_INFERRED_VERSION_REGEX, "exclude_regex": r"(?:alpha|beta|rc)"}
    if kind in ("gnome-gitlab", "gnomegitlab") and "/" in value:
        return {"type": "gitlab", "host": "gitlab.gnome.org", "project": value, "version_regex": DEFAULT_INFERRED_VERSION_REGEX, "exclude_regex": r"(?:alpha|beta|rc|\.9\d$)"}
    if kind == "pypi":
        return {"type": "pypi", "project": value, "version_regex": r"^(?P<version>\d+(?:\.\d+)+(?:[._-]?\w+)*)$"}
    return None


def _replace_known_vars(text: str, info: PackageInfo) -> str:
    return (text.replace("${PN}", info.pn)
                .replace("${CATEGORY}", info.category)
                .replace("${PV}", info.pv or "")
                .replace("${P}", f"{info.pn}-{info.pv or ''}"))


def github_repo_from_url(url: str, info: PackageInfo) -> str | None:
    url = _replace_known_vars(url, info)
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/#?\s)\"]+)", url)
    if not m:
        return None
    owner = m.group("owner")
    repo = re.sub(r"\.git$", "", m.group("repo"))
    if "$" in owner or "$" in repo:
        return None
    return f"{owner}/{repo}"


def gitlab_project_from_url(url: str, info: PackageInfo) -> tuple[str, str] | None:
    url = _replace_known_vars(url, info)
    m = re.search(r"""https?://(?P<host>gitlab(?:\.gnome)?\.org)/(?P<project>[^\s)"']+?)(?:\.git|/-/|/archive|$)""", url)
    if not m:
        return None
    project = m.group("project").strip("/")
    if "$" in project or "/" not in project:
        return None
    return m.group("host"), project


def source_urls_from_ebuild(text: str) -> list[str]:
    scan_text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#") and "HOMEPAGE" not in line
    )
    urls = re.findall(r"""https?://[^\s"')]+""", scan_text)
    cleaned = []
    for url in urls:
        url = url.rstrip(",.;")
        if any(part in url for part in ("/issues", "/pull", "/wiki", "/blob/", "bugs.gentoo.org")):
            continue
        cleaned.append(url)
    return cleaned


def github_source_from_ebuild(text: str, info: PackageInfo) -> dict[str, Any] | None:
    urls = source_urls_from_ebuild(text)
    scored: list[tuple[int, str, str]] = []
    for url in urls:
        repo = github_repo_from_url(url, info)
        if not repo:
            continue
        if repo.startswith("gentoo-crate-dist/"):
            continue
        if repo.startswith("KhronosGroup/") and info.pn.lower() not in repo.lower():
            continue
        score = 0
        if "releases/download" in url:
            score += 50
        if "/archive/" in url or "/archive/refs/tags/" in url:
            score += 40
        if "EGIT_REPO_URI" in text and repo.lower().endswith("/" + info.pn.lower()):
            score += 10
        if info.pn.lower().replace("-bin", "") in repo.lower():
            score += 10
        if "${PV}" in url or (info.pv and info.pv in url):
            score += 10
        if score:
            scored.append((score, repo, url))
    if not scored:
        return None
    scored.sort(reverse=True)
    _score, repo, url = scored[0]
    mode = "releases" if "releases/download" in url else "auto"
    prefix = "^v?" if "v${PV" in url or re.search(r"/v?\$\{PV", url) else "^v?"
    return {"type": "github", "repo": repo, "mode": mode, "version_regex": prefix + DEFAULT_INFERRED_VERSION_RE + "$", "exclude_regex": r"(?:alpha|beta|rc|nightly)"}


def gitlab_source_from_ebuild(text: str, info: PackageInfo) -> dict[str, Any] | None:
    for url in source_urls_from_ebuild(text):
        found = gitlab_project_from_url(url, info)
        if found:
            host, project = found
            return {"type": "gitlab", "host": host, "project": project, "version_regex": DEFAULT_INFERRED_VERSION_REGEX, "exclude_regex": r"(?:alpha|beta|rc)"}
    return None


def pypi_source_from_ebuild(text: str, info: PackageInfo) -> dict[str, Any] | None:
    if re.search(r"\binherit\b.*\bpypi\b", text):
        return {"type": "pypi", "project": info.pn, "version_regex": r"^(?P<version>\d+(?:\.\d+)+(?:[._-]?\w+)*)$"}
    return None


def infer_rule(info: PackageInfo, overlay: Path) -> dict[str, Any] | None:
    text = read_selected_ebuild(overlay, info)
    if info.live and not any(not e.endswith("9999.ebuild") for e in info.ebuilds):
        return {"status": "live_only", "source": {"type": "live"}, "note": "Dynamically inferred live-only package; fixed-release comparison suppressed.", "_origin": "inferred:live"}
    for maker, origin in ((pypi_source_from_ebuild, "inferred:pypi"), (github_source_from_ebuild, "inferred:github-src-uri"), (gitlab_source_from_ebuild, "inferred:gitlab-src-uri")):
        src = maker(text, info)
        if src:
            return {"status": "auto", "source": src, "note": "Dynamically inferred from selected ebuild.", "_origin": origin}
    for kind, value in read_metadata_remote_ids(overlay, info):
        src = source_from_remote_id(kind, value, info)
        if src:
            return {"status": "auto", "source": src, "note": f"Dynamically inferred from metadata.xml remote-id {kind}:{value}.", "_origin": "inferred:metadata"}
    if any(url in text for url in ("parsec.app", "bitwig.com", "zoom.us", "blackmagicdesign.com")):
        return {"status": "manual_no_fetch", "source": {"type": "manual"}, "note": "Dynamically inferred vendor/manual package; no stable release API configured.", "_origin": "inferred:manual"}
    return None


def resolve_rule(config: dict[str, Any], info: PackageInfo, overlay: Path) -> dict[str, Any] | None:
    explicit = config.get("packages", {}).get(info.cp)
    if explicit:
        rule = json.loads(json.dumps(explicit))
        rule["_origin"] = "explicit-config"
        return rule
    if not config.get("dynamic", {}).get("enabled", True):
        return None
    return infer_rule(info, overlay)


def github_candidates(source: dict[str, Any], http: HttpClient, force: bool) -> FetchResult[list[Candidate]]:
    repo = source["repo"]
    mode = source.get("mode", "releases")
    source_id = f"github:{repo}"
    candidates: list[Candidate] = []
    stale_error: str | None = None
    if mode in ("releases", "release", "max_release", "auto", "releases_then_tags"):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
        result = http.get_json(url, force=force)
        data = result.body
        stale_error = result.stale_error
        if not isinstance(data, list):
            raise WatchError(f"{source_id}: unexpected releases response")
        for rel in data:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not source.get("include_prereleases", False):
                continue
            raw = rel.get("tag_name") or rel.get("name") or ""
            version = extract_version(raw, source)
            if not version or not candidate_allowed(raw, version, source):
                continue
            asset_regex = source.get("asset_regex")
            asset_status = None
            if asset_regex:
                pattern = asset_regex.replace("{version}", re.escape(version))
                assets = [a.get("name", "") for a in rel.get("assets", [])]
                if not any(re.search(pattern, name) for name in assets):
                    continue
                asset_status = "matched"
            candidates.append(Candidate(raw, version, rel.get("html_url") or f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(raw)}", source_id, rel.get("published_at"), asset_status))
    if mode in ("auto", "releases_then_tags") and candidates:
        return FetchResult(candidates, stale_error)
    if mode in ("tags", "tag", "max_tag", "auto", "releases_then_tags"):
        url = f"https://api.github.com/repos/{repo}/tags?per_page=100"
        result = http.get_json(url, force=force)
        data = result.body
        if result.stale_error:
            stale_error = "; ".join(part for part in (stale_error, result.stale_error) if part)
        if not isinstance(data, list):
            raise WatchError(f"{source_id}: unexpected tags response")
        for tag in data:
            raw = tag.get("name") or ""
            version = extract_version(raw, source)
            if not version or not candidate_allowed(raw, version, source):
                continue
            candidates.append(Candidate(raw, version, f"https://github.com/{repo}/releases/tag/{urllib.parse.quote(raw)}", source_id))
    elif mode not in ("releases", "release", "max_release", "tags", "tag", "max_tag", "auto", "releases_then_tags"):
        raise WatchError(f"{source_id}: unknown GitHub mode {mode}")
    return FetchResult(candidates, stale_error)


def gitlab_candidates(source: dict[str, Any], http: HttpClient, force: bool) -> FetchResult[list[Candidate]]:
    host = source.get("host", "gitlab.com")
    project = source["project"]
    encoded = urllib.parse.quote(project, safe="")
    source_id = f"gitlab:{host}/{project}"
    url = f"https://{host}/api/v4/projects/{encoded}/repository/tags?per_page=100&order_by=version&sort=desc"
    result = http.get_json(url, force=force)
    data = result.body
    if not isinstance(data, list):
        raise WatchError(f"{source_id}: unexpected tags response")
    candidates: list[Candidate] = []
    for tag in data:
        raw = tag.get("name") or ""
        version = extract_version(raw, source)
        if not version or not candidate_allowed(raw, version, source):
            continue
        rel = tag.get("release") or {}
        candidates.append(Candidate(raw, version, f"https://{host}/{project}/-/tags/{urllib.parse.quote(raw)}", source_id, rel.get("released_at")))
    return FetchResult(candidates, result.stale_error)


def pypi_candidates(source: dict[str, Any], http: HttpClient, force: bool) -> FetchResult[list[Candidate]]:
    project = source["project"]
    source_id = f"pypi:{project}"
    url = f"https://pypi.org/pypi/{urllib.parse.quote(project)}/json"
    result = http.get_json(url, force=force)
    data = result.body
    releases = data.get("releases", {}) if isinstance(data, dict) else {}
    candidates: list[Candidate] = []
    for raw, files in releases.items():
        version = extract_version(raw, source)
        if not version or not candidate_allowed(raw, version, source):
            continue
        if files and all(f.get("yanked") for f in files if isinstance(f, dict)):
            continue
        released_at = None
        for f in files or []:
            if isinstance(f, dict) and f.get("upload_time_iso_8601"):
                released_at = f["upload_time_iso_8601"]
                break
        candidates.append(Candidate(raw, version, f"https://pypi.org/project/{project}/{raw}/", source_id, released_at))
    return FetchResult(candidates, result.stale_error)


def url_regex_candidates(source: dict[str, Any], http: HttpClient, force: bool) -> FetchResult[list[Candidate]]:
    url = source["url"]
    result = http.get_text(url, force=force)
    candidates = []
    for m in re.finditer(source["version_regex"], result.body):
        raw = m.group("version") if "version" in m.groupdict() else (m.group(1) if m.groups() else m.group(0))
        version = extract_version(raw, {k: v for k, v in source.items() if k != "version_regex"}) or raw
        if candidate_allowed(raw, version, source):
            candidates.append(Candidate(raw, version, url, f"url:{url}"))
    return FetchResult(candidates, result.stale_error)




def _debian_control_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line:
            current = None
            continue
        if line[0].isspace() and current:
            fields[current] += "\n" + line.strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current = key.lower()
        fields[current] = value.strip()
    return fields


def _control_fields_from_deb(deb: bytes, source_id: str) -> dict[str, str]:
    if not deb.startswith(b"!<arch>\n"):
        raise WatchError(f"{source_id}: not a Debian ar archive")
    offset = 8
    while offset + 60 <= len(deb):
        header = deb[offset:offset + 60]
        name = header[:16].decode("utf-8", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise WatchError(f"{source_id}: invalid ar member size") from exc
        start = offset + 60
        end = start + size
        if end > len(deb):
            raise WatchError(f"{source_id}: truncated ar member")
        if name.startswith("control.tar"):
            try:
                with tarfile.open(fileobj=io.BytesIO(deb[start:end]), mode="r:*") as tar:
                    for member in tar.getmembers():
                        if member.name.lstrip("./") != "control" or not member.isfile():
                            continue
                        extracted = tar.extractfile(member)
                        if extracted is None:
                            break
                        return _debian_control_fields(extracted.read().decode("utf-8", "replace"))
            except tarfile.TarError as exc:
                raise WatchError(f"{source_id}: invalid control archive") from exc
            raise WatchError(f"{source_id}: missing control file")
        offset = end + (end % 2)
    raise WatchError(f"{source_id}: missing control archive")


def deb_control_candidates(source: dict[str, Any], http: HttpClient, force: bool) -> FetchResult[list[Candidate]]:
    url = source["url"]
    source_id = f"deb-control:{url}"
    result = http.get_bytes(
        url,
        force=force,
        accept="application/vnd.debian.binary-package, application/octet-stream, */*",
    )
    fields = _control_fields_from_deb(result.body, source_id)
    package = source.get("package")
    if package and fields.get("package") != package:
        return FetchResult([], result.stale_error)
    raw = fields.get(source.get("field", "Version").lower())
    if not raw:
        return FetchResult([], result.stale_error)
    normalize = source.get("normalize")
    if normalize is None:
        version = raw
    elif normalize == "debian-hyphen-to-gentoo-dot":
        version = raw.replace("-", ".")
    else:
        raise WatchError(f"{source_id}: unsupported normalize {normalize!r}")
    if not candidate_allowed(raw, version, source):
        return FetchResult([], result.stale_error)
    return FetchResult([Candidate(raw, version, url, source_id)], result.stale_error)


def fetch_candidates(
    source: dict[str, Any],
    http: HttpClient,
    force: bool = False,
) -> FetchResult[list[Candidate]]:
    typ = source.get("type")
    if typ == "github":
        return github_candidates(source, http, force)
    if typ == "gitlab":
        return gitlab_candidates(source, http, force)
    if typ == "pypi":
        return pypi_candidates(source, http, force)
    if typ == "deb-control":
        return deb_control_candidates(source, http, force)
    if typ == "url-regex":
        return url_regex_candidates(source, http, force)
    if typ in ("manual", "live"):
        return FetchResult([])
    raise WatchError(f"unknown source type {typ!r}")
