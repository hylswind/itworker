"""Build the EC2 user-data (a cloud-init shell script) for a control-plane
*replacement* instance — the one the ASG launches after the daily restart.

It clones this repo at the workflow-pinned commit (both baked in literally, and
also readable from SSM) and boots straight into server mode. The first instance is
launched by the workflow with a different, setup-mode user-data (built on the
workflow side); both converge on the same server process."""

from __future__ import annotations

_SERVER_TEMPLATE = r"""#!/bin/bash
set -euxo pipefail
dnf install -y git python3.11 python3.11-pip
python3.11 -m pip install boto3
rm -rf /opt/openzp-itworker
git clone https://github.com/{repo}.git /opt/openzp-itworker
cd /opt/openzp-itworker
git checkout {commit}
export AWS_DEFAULT_REGION={region}
exec python3.11 -m openzp_itworker server
"""


def build_server_userdata(repo: str, commit: str, region: str) -> str:
    """repo is owner/name; commit is the pinned sha. The clone path assumes the
    repo root is the package parent (post repo-split layout)."""
    return _SERVER_TEMPLATE.format(repo=repo, commit=commit, region=region)
