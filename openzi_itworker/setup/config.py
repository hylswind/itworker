"""Parse setup-mode configuration from the environment. The workflow's launch
user-data sets these before running `python -m openzi_itworker setup`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .. import config


@dataclass
class SetupConfig:
    domain: str
    end_epoch: int          # absolute epoch seconds; setup waits until end_epoch + 1
    api_key: str
    repo: str               # owner/name to clone on replacement instances
    commit: str             # the workflow-pinned sha
    contact: dict           # domain-registration contact fields (from workflow input)
    skip_domain: bool       # test path: don't buy; verify ownership + clean the zone
    region: str = config.REGION

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SetupConfig":
        e = os.environ if env is None else env
        missing = [k for k in ("OPENZI_DOMAIN", "OPENZI_END", "OPENZI_API_KEY",
                               "OPENZI_REPO", "OPENZI_COMMIT") if not e.get(k)]
        if missing:
            raise ValueError(f"setup: missing env {missing}")
        contact_raw = e.get("OPENZI_CONTACT") or "{}"
        try:
            contact = json.loads(contact_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"setup: OPENZI_CONTACT is not valid JSON: {exc}") from exc
        return cls(
            domain=e["OPENZI_DOMAIN"],
            end_epoch=int(e["OPENZI_END"]),
            api_key=e["OPENZI_API_KEY"],
            repo=e["OPENZI_REPO"],
            commit=e["OPENZI_COMMIT"],
            contact=contact,
            skip_domain=e.get("OPENZI_SKIP_DOMAIN", "0") in ("1", "true", "True"),
            region=e.get("OPENZI_REGION") or config.REGION,
        )
