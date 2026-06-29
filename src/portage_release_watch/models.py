from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PackageInfo:
    cp: str
    category: str
    pn: str
    pv: str | None
    pvr: str | None
    pf: str | None
    pr: str | None
    live: bool
    ebuilds: list[str]


@dataclass
class Candidate:
    raw: str
    version: str
    url: str
    source_id: str
    released_at: str | None = None
    asset_status: str | None = None


class WatchError(Exception):
    pass
