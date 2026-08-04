"""Persist the runtime secrets/config the server (and replacement instances) read
from SSM: the control-plane API key + billing console password (both SecureString),
and the pinned repo/commit (so a replacement clones exactly this code)."""

from __future__ import annotations

from .. import config


def store(ssm, api_key: str, commit: str, repo: str, console_password: str) -> None:
    ssm.put_parameter(Name=config.API_KEY_PARAM, Value=api_key,
                      Type="SecureString", Overwrite=True)
    ssm.put_parameter(Name=config.CONSOLE_PASSWORD_PARAM, Value=console_password,
                      Type="SecureString", Overwrite=True)
    ssm.put_parameter(Name=config.PINNED_COMMIT_PARAM, Value=commit,
                      Type="String", Overwrite=True)
    ssm.put_parameter(Name=config.REPO_PARAM, Value=repo,
                      Type="String", Overwrite=True)
