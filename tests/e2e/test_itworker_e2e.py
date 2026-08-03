"""End-to-end: bring itworker up in a real account and exercise the full lifecycle
— setup → control-plane health → init → deploy → serve → delete → recover.

The driver plays the GitHub workflow's role, minus the destructive half: it creates
the admin role and launches the instance, but uses NO root key and never locks the
console. So this can rerun against the same test account indefinitely, which makes
it the main iteration tool for itworker. (The workflow's own e2e, in the
openzi-workflow repo, covers the destructive path.)

Opt-in; skipped unless OPENZI_E2E=1. One round takes ~40-60 min and creates real,
billable resources (two ALBs, EC2, Image Builder).

Env:
  OPENZI_E2E=1                enable
  OPENZI_ASSUME_ROLE_ARN      role in the test account to assume (from a management
                              account, so a wedged round is still recoverable)
  OPENZI_DOMAIN               a domain the test account ALREADY OWNS (skip-domain
                              path — no purchase); its hosted zone gets cleaned
  OPENZI_API_KEY              control-plane bearer key to install and use
  OPENZI_ITWORKER_REPO        owner/name to clone      (default hylswind/itworker)
  OPENZI_ITWORKER_COMMIT      commit/ref to check out  (default main)
                              NOTE: the instance clones from GitHub, so this must be
                              PUSHED — local uncommitted work is not what runs.
  OPENZI_APP_REPO             owner/name of an app with a Dockerfile (optional; the
  OPENZI_APP_COMMIT           deploy phase is skipped when either is unset)
  OPENZI_E2E_KEEP=1           leave everything standing for debugging (no teardown)
"""

import os
import time
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.e2e

if os.environ.get("OPENZI_E2E") != "1":
    pytest.skip("set OPENZI_E2E=1 to run the itworker e2e", allow_module_level=True)

import boto3  # noqa: E402

import driver  # noqa: E402  (tests/e2e is on sys.path via conftest)

ASSUME = os.environ["OPENZI_ASSUME_ROLE_ARN"]
DOMAIN = os.environ["OPENZI_DOMAIN"]
API_KEY = os.environ["OPENZI_API_KEY"]
ITWORKER_REPO = os.environ.get("OPENZI_ITWORKER_REPO", "hylswind/itworker")
ITWORKER_COMMIT = os.environ.get("OPENZI_ITWORKER_COMMIT", "main")
APP_REPO = os.environ.get("OPENZI_APP_REPO")
APP_COMMIT = os.environ.get("OPENZI_APP_COMMIT")

SETUP_TIMEOUT = float(os.environ.get("OPENZI_E2E_SETUP_TIMEOUT", 3600))
DEPLOY_TIMEOUT = float(os.environ.get("OPENZI_E2E_DEPLOY_TIMEOUT", 2400))
ACTION_TIMEOUT = float(os.environ.get("OPENZI_E2E_ACTION_TIMEOUT", 600))
RECOVER_TIMEOUT = float(os.environ.get("OPENZI_E2E_RECOVER_TIMEOUT", 1800))


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


@pytest.fixture(scope="module")
def session():
    """Credentials for the test account, assumed from the management account."""
    creds = boto3.client("sts").assume_role(
        RoleArn=ASSUME, RoleSessionName="openzi-itworker-e2e")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                         aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"],
                         region_name=driver.config.REGION)


@pytest.fixture(scope="module")
def platform(session):
    """Bring itworker up, yield a control-plane client, then always tear down."""
    ec2, ssm, iam = session.client("ec2"), session.client("ssm"), session.client("iam")
    try:
        log("phase 1: create the admin role (the workflow's step 1)")
        profile = driver.create_admin_role(iam, log)

        log("phase 2: launch the itworker instance")
        # end = now: setup waits until end+1s, so it starts essentially immediately.
        end_epoch = int(datetime.now(timezone.utc).timestamp())
        user_data = driver.build_setup_userdata(
            repo=ITWORKER_REPO, commit=ITWORKER_COMMIT, region=driver.config.REGION,
            domain=DOMAIN, end_epoch=end_epoch, api_key=API_KEY)
        driver.launch(ec2, ssm, user_data, profile, log)

        log("phase 3: wait for the setup marker (domain check, stack, control wiring)")
        result, detail = driver.await_marker(ssm, SETUP_TIMEOUT, log=log)
        assert result == "ok", f"itworker setup reported failure: {detail}"
        log("  setup reported success")

        control = driver.ControlClient(f"https://admin.{DOMAIN}", API_KEY, log)
        log("phase 4: wait for the control plane to answer")
        control.wait_healthy(timeout=900)
        log("  control plane healthy")
        yield control
    finally:
        if os.environ.get("OPENZI_E2E_KEEP") == "1":
            log("OPENZI_E2E_KEEP=1 — leaving everything standing")
        else:
            log("teardown: cleaning the account")
            driver.cleanup(session, log)


def test_control_plane_requires_the_api_key(platform):
    """The control ALB is internet-facing, so the bearer key is the only gate. (Takes
    `platform` so it runs against a live control plane, not before bring-up.)"""
    status, _ = driver.fetch(f"https://admin.{DOMAIN}/status?id=whatever")
    assert status == 403


@pytest.mark.skipif(not (APP_REPO and APP_COMMIT),
                    reason="set OPENZI_APP_REPO + OPENZI_APP_COMMIT to exercise deploy")
def test_app_lifecycle(platform):
    """init → deploy → the app is served → delete."""
    app = os.environ.get("OPENZI_APP_NAME", "e2e.dev")
    short = APP_COMMIT[:7]

    log(f"init {app} -> {APP_REPO}")
    platform.run("init", {"app": app, "repo": APP_REPO}, ACTION_TIMEOUT)
    status, body = driver.fetch(f"https://{DOMAIN}/{app}/info.json")
    assert status == 200, f"info.json not published: {status}"
    assert APP_REPO.split("/")[-1] in body or "repo_id" in body

    log(f"deploy {app}@{short} (Image Builder bake, ~10-15 min)")
    platform.run("deploy", {"app": app, "commit": APP_COMMIT}, DEPLOY_TIMEOUT)

    log("fetching the deployed app")
    driver.wait_for_app(f"https://{DOMAIN}/{app}/{short}/", timeout=900, log=log)

    log(f"delete {app}@{short}")
    platform.run("delete", {"app": app, "commit": APP_COMMIT}, ACTION_TIMEOUT)


def test_recover_tears_the_platform_down(platform):
    """recover is the last phase: it wipes apps and restores console access. (This
    account was never locked, so the unlock step has nothing to undo — which recover
    tolerates.)"""
    log("recover (waits for every app instance to terminate)")
    platform.run("recover", {}, RECOVER_TIMEOUT)
    log("  recovered")
