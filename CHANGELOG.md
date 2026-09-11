# Changelog

## Unreleased

- Isolate installed privileged execution from user-writable checkouts and Python environment overrides.
- Add receipt-aware system upgrades, refusing unrecorded or locally modified files and recognizing only exact supported legacy launchers.
- Separate trusted operational policy, token files, and executable notification hooks from package mappings.
- Validate privileged paths and private caches, constrain HTTP schemes and authorization redirects, and serialize canonical checks.
- Add validated calendar and Python-to-Portage version normalization and immutable Mozilla nightly discovery.
- Add the `packaging` runtime dependency and infer nested GitLab.com projects from source URLs.
- Handle disabled notification hooks without an exception and use the correct installed CLI program name.

## 0.1.0 - 2026-06-29

- Initial public extraction of the Gentoo Portage local overlay release watcher.
- Dynamic source inference for GitHub, GitLab, PyPI, live-only ebuilds, and manual/no-fetch vendors.
- JSON and human-readable reports, cache-aware provider checks, cron/postsync installer, and fixture tests.
