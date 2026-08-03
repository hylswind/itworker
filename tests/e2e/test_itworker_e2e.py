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
  OPENZI_APP_COMMIT           deploy phase is skipped when either is unset). Accepts
                              a comma-separated list — two or more versions is what
                              proves per-version secret isolation.
  OPENZI_E2E_KEEP=1           leave everything standing for debugging (no teardown)
"""

import os
import re
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
APP_COMMITS = [c.strip() for c in os.environ.get("OPENZI_APP_COMMIT", "").split(",") if c.strip()]

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


_SHA256 = re.compile(r"\b[0-9a-f]{64}\b")


def _served_secret_hash(url: str) -> str:
    """The example app publishes sha256(OPENZI_VERSION_SECRET) — the per-version
    secret made observable without exposing it. Pull it back out of the page."""
    status, body = driver.fetch(url)
    assert status == 200, f"{url} stopped serving after a later deploy: {status}"
    match = _SHA256.search(body)
    assert match, f"no version-secret hash at {url} (app got no secret?): {body[:300]}"
    return match.group()


@pytest.mark.skipif(not (APP_REPO and APP_COMMITS),
                    reason="set OPENZI_APP_REPO + OPENZI_APP_COMMIT to exercise deploy")
def test_app_lifecycle(platform):
    """init → deploy each version → every version is served on its own path with its
    own secret → delete each.

    Deploying more than one version is the part that proves the platform's central
    claim: the same app, at the same instant, hands each version a different
    OPENZI_VERSION_SECRET, so one version cannot read another's."""
    app = os.environ.get("OPENZI_APP_NAME", "e2e.dev")
    shorts = [c[:7] for c in APP_COMMITS]

    log(f"init {app} -> {APP_REPO}")
    platform.run("init", {"app": app, "repo": APP_REPO}, ACTION_TIMEOUT)
    status, body = driver.fetch(f"https://{DOMAIN}/{app}/info.json")
    assert status == 200, f"info.json not published: {status}"
    assert APP_REPO.split("/")[-1] in body or "repo_id" in body

    for commit, short in zip(APP_COMMITS, shorts):
        log(f"deploy {app}@{short} (Image Builder bake, ~10-15 min)")
        platform.run("deploy", {"app": app, "commit": commit}, DEPLOY_TIMEOUT)
        driver.wait_for_app(f"https://{DOMAIN}/{app}/{short}/", timeout=900, log=log)

    # Read every version only now, after the last deploy: an earlier version still
    # answering on its own path is what makes them concurrent rather than sequential.
    hashes = {short: _served_secret_hash(f"https://{DOMAIN}/{app}/{short}/") for short in shorts}
    for short, digest in hashes.items():
        log(f"  {short} version-secret sha256 {digest}")
    assert len(set(hashes.values())) == len(hashes), \
        f"versions share a secret — per-version isolation is broken: {hashes}"

    for commit, short in zip(APP_COMMITS, shorts):
        log(f"delete {app}@{short}")
        platform.run("delete", {"app": app, "commit": commit}, ACTION_TIMEOUT)


def test_recover_tears_the_platform_down(platform):
    """recover is the last phase: it wipes apps and restores console access. (This
    account was never locked, so the unlock step has nothing to undo — which recover
    tolerates.)"""
    log("recover (waits for every app instance to terminate)")
    platform.run("recover", {}, RECOVER_TIMEOUT)
    log("  recovered")
