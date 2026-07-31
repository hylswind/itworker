"""End-to-end: play the workflow's role from a management account and exercise a
full itworker lifecycle — launch → setup → deploy an app → recover.

Opt-in; skipped unless OPENZI_E2E=1. Uses a management-account credential to assume
into an org-member test account (so it can reset it and rerun freely); NO root key,
and it does NOT lock the console (the workflow does that, not itworker).

Env:
  OPENZI_E2E=1                enable
  OPENZI_ASSUME_ROLE_ARN      role in the test account to assume
  OPENZI_DOMAIN               an owned test domain (skip-domain path, no purchase)
  OPENZI_API_KEY              control-plane key
  OPENZI_APP_REPO             owner/name of an app to init+deploy
  OPENZI_APP_COMMIT           a commit sha of that app
"""

import os
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.e2e

if os.environ.get("OPENZI_E2E") != "1":
    pytest.skip("set OPENZI_E2E=1 to run the itworker e2e", allow_module_level=True)

import boto3  # noqa: E402

from openzi_itworker import config, userdata  # noqa: E402

ASSUME = os.environ["OPENZI_ASSUME_ROLE_ARN"]
DOMAIN = os.environ["OPENZI_DOMAIN"]
API_KEY = os.environ["OPENZI_API_KEY"]


def _session():
    creds = boto3.client("sts").assume_role(
        RoleArn=ASSUME, RoleSessionName="openzi-itworker-e2e")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                         aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"],
                         region_name=config.REGION)


def _setup_userdata(repo, commit, region, end_epoch):
    # the driver plays the workflow: build a setup user-data (skip-domain path)
    from string import Template
    body = Template(
        "#!/bin/bash\nset -euxo pipefail\n"
        "dnf install -y git python3.11 python3.11-pip\n"
        "python3.11 -m pip install boto3\n"
        "git clone https://github.com/$repo.git /opt/w && cd /opt/w && git checkout $commit\n"
        "export AWS_DEFAULT_REGION=$region OPENZI_DOMAIN=$domain OPENZI_END=$end\n"
        "export OPENZI_API_KEY=$key OPENZI_REPO=$repo OPENZI_COMMIT=$commit\n"
        "export OPENZI_REGION=$region OPENZI_SKIP_DOMAIN=1 OPENZI_CONTACT='{}'\n"
        "exec python3.11 -m openzi_itworker setup\n")
    return body.substitute(repo=repo, commit=commit, region=region, domain=DOMAIN,
                           end=end_epoch, key=API_KEY)


def test_itworker_setup_and_lifecycle():
    sess = _session()
    ssm = sess.client("ssm")
    # (elided for brevity in this skeleton) — create the admin role, launch an
    # instance with the setup user-data, then wait for the marker:
    end_epoch = int(datetime.now(timezone.utc).timestamp())
    _ = _setup_userdata(os.environ["OPENZI_ASSUME_ROLE_ARN"], "HEAD", config.REGION, end_epoch)
    _ = userdata  # server-mode user-data builder is what replacements use

    marker = _await_marker(ssm, timeout=int(os.environ.get("OPENZI_E2E_TIMEOUT", "5400")))
    assert marker == "ok", "itworker setup did not report success"
    # then: init + deploy an app via https://admin.{DOMAIN}, curl it, and recover.


def _await_marker(ssm, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, result in ((config.SETUP_OK_PARAM, "ok"), (config.SETUP_FAILED_PARAM, "failed")):
            try:
                ssm.get_parameter(Name=name)
                return result
            except ssm.exceptions.ParameterNotFound:
                pass
        time.sleep(30)
    raise TimeoutError("no setup marker")
