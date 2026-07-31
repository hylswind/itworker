"""GitHub repo lookups over the public API (stdlib urllib — no dependency). Used
by init to resolve owner/name -> immutable ids; the bake re-resolves by id to get
the current clone URL and verify the owner still matches."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .. import config


class RepoLookupError(Exception):
    pass


def resolve(repo: str) -> dict:
    """owner/name -> {owner_id, repo_id, full_name}. Raises RepoLookupError on
    typo / private repo / rate limit."""
    data = _get(config.GH_REPO_URL.format(repo=repo))
    return {"owner_id": data["owner"]["id"], "repo_id": data["id"], "full_name": data["full_name"]}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "openzi-control-plane"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
        raise RepoLookupError(f"GitHub lookup failed for {url}: {exc}") from exc
