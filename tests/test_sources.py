from __future__ import annotations

import base64
import io
import json
import tarfile

from portage_release_watch.models import PackageInfo
from portage_release_watch.overlay import scan_overlay
from portage_release_watch.sources import fetch_candidates, resolve_rule
from portage_release_watch.http import HttpClient


def _ar_member(name: str, data: bytes) -> bytes:
    header = (
        f"{name + '/':<16}"
        f"{0:<12}"
        f"{0:<6}"
        f"{0:<6}"
        f"{0o100644:<8}"
        f"{len(data):<10}"
        "`\n"
    ).encode("ascii")
    padding = b"\n" if len(data) % 2 else b""
    return header + data + padding


def _deb_fixture(control: str) -> bytes:
    control_tar = io.BytesIO()
    with tarfile.open(fileobj=control_tar, mode="w:gz") as tar:
        data = control.encode()
        info = tarfile.TarInfo("./control")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return (
        b"!<arch>\n"
        + _ar_member("debian-binary", b"2.0\n")
        + _ar_member("control.tar.gz", control_tar.getvalue())
    )


def _cache_bytes(http: HttpClient, url: str, body: bytes) -> None:
    http._cache_path(url).write_text(json.dumps({
        "fetched_at": 4102444800,
        "body_base64": base64.b64encode(body).decode("ascii"),
    }) + "\n")


def test_dynamic_inference_prefers_src_uri_over_metadata(tmp_path):
    overlay = tmp_path / "overlay"
    pkg = overlay / "app-misc/source-priority"
    pkg.mkdir(parents=True)
    (overlay / "profiles").mkdir()
    (overlay / "profiles/repo_name").write_text("priority\n")
    (pkg / "source-priority-1.0.0.ebuild").write_text(
        'EAPI=8\nSRC_URI="https://github.com/correct/source-priority/archive/refs/tags/${PV}.tar.gz -> ${P}.tar.gz"\n'
    )
    (pkg / "metadata.xml").write_text(
        '<pkgmetadata><upstream><remote-id type="github">wrong/project</remote-id></upstream></pkgmetadata>\n'
    )
    info = scan_overlay(overlay)["app-misc/source-priority"]
    rule = resolve_rule({"dynamic": {"enabled": True}, "packages": {}}, info, overlay)
    assert rule["_origin"] == "inferred:github-src-uri"
    assert rule["source"]["repo"] == "correct/source-priority"


def test_inferred_github_regex_rejects_package_suffix_tags(tmp_path):
    overlay = tmp_path / "overlay"
    pkg = overlay / "games-util/mangohud"
    pkg.mkdir(parents=True)
    (overlay / "profiles").mkdir()
    (overlay / "profiles/repo_name").write_text("suffix-tags\n")
    (pkg / "mangohud-0.8.4.ebuild").write_text(
        'EAPI=8\nSRC_URI="https://github.com/flightlessmango/MangoHud/archive/refs/tags/v${PV}.tar.gz -> ${P}.tar.gz"\n'
    )
    info = scan_overlay(overlay)["games-util/mangohud"]
    rule = resolve_rule({"dynamic": {"enabled": True}, "packages": {}}, info, overlay)
    assert rule["_origin"] == "inferred:github-src-uri"

    http = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    releases_url = "https://api.github.com/repos/flightlessmango/MangoHud/releases?per_page=100"
    tags_url = "https://api.github.com/repos/flightlessmango/MangoHud/tags?per_page=100"
    http._cache_path(releases_url).write_text('{"fetched_at":4102444800,"body":[]}\n')
    http._cache_path(tags_url).write_text(
        '{"fetched_at":4102444800,"body":[{"name":"0.6.9-1"},{"name":"0.8.4"},{"name":"v0.8.4"}]}\n'
    )

    candidates = fetch_candidates(rule["source"], http)

    assert [c.raw for c in candidates] == ["0.8.4", "v0.8.4"]
    assert [c.version for c in candidates] == ["0.8.4", "0.8.4"]


def test_prerelease_tags_filtered_by_default(tmp_path, monkeypatch):
    from helpers import install_fake_portage
    install_fake_portage(monkeypatch)
    cache = tmp_path / "cache"
    http = HttpClient(cache, timeout=1, max_age_hours=24)
    url = "https://api.github.com/repos/owner/repo/tags?per_page=100"
    path = http._cache_path(url)
    path.write_text('{"fetched_at":4102444800,"body":[{"name":"v1.2.0-beta1"},{"name":"v1.1.0"}]}\n')
    source = {"type": "github", "repo": "owner/repo", "mode": "tags", "version_regex": "^v?(?P<version>\\d+(?:\\.\\d+)+(?:[._-]?\\w+)*)$"}
    candidates = fetch_candidates(source, http)
    assert [c.raw for c in candidates] == ["v1.1.0"]
    source["include_prereleases"] = True
    candidates = fetch_candidates(source, http)
    assert [c.raw for c in candidates] == ["v1.2.0-beta1", "v1.1.0"]


def test_deb_control_candidates_normalize_debian_version(tmp_path):
    http = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    url = "https://example.invalid/parsec-linux.deb"
    _cache_bytes(http, url, _deb_fixture("Package: parsec\nVersion: 150-97c\n"))

    candidates = fetch_candidates({
        "type": "deb-control",
        "url": url,
        "package": "parsec",
        "normalize": "debian-hyphen-to-gentoo-dot",
    }, http)

    assert len(candidates) == 1
    assert candidates[0].raw == "150-97c"
    assert candidates[0].version == "150.97c"
    assert candidates[0].url == url
    assert candidates[0].source_id == f"deb-control:{url}"


def test_deb_control_candidates_reject_package_mismatch(tmp_path):
    http = HttpClient(tmp_path / "cache", timeout=1, max_age_hours=24)
    url = "https://example.invalid/parsec-linux.deb"
    _cache_bytes(http, url, _deb_fixture("Package: other\nVersion: 150-97c\n"))

    candidates = fetch_candidates({
        "type": "deb-control",
        "url": url,
        "package": "parsec",
        "normalize": "debian-hyphen-to-gentoo-dot",
    }, http)

    assert candidates == []
