from __future__ import annotations

from portage_release_watch.models import PackageInfo
from portage_release_watch.overlay import scan_overlay
from portage_release_watch.sources import fetch_candidates, resolve_rule
from portage_release_watch.http import HttpClient


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
