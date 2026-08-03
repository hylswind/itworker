"""Real-AWS helpers for the itworker e2e.

Two groups:
  1. the pieces the GitHub workflow normally performs — the admin role and the
     instance launch — so the e2e can bring itworker up without a root key and
     without locking the console;
  2. a small client for the control-plane API (submit an action, poll the job).

Everything takes explicit boto3 clients, so this module is safe to import without
credentials.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from botocore.exceptions import ClientError

from openzi_itworker import config

E2E_INSTANCE_TAG = "openzi-itworker-e2e"

_EC2_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]})

# The instance clones itworker from GitHub, so the e2e exercises a PUSHED commit —
# uncommitted local work is not what runs.
_SETUP_USERDATA = r"""#!/bin/bash
set -euxo pipefail
dnf install -y git python3.11 python3.11-pip
python3.11 -m pip install boto3
rm -rf /opt/openzi-itworker
git clone https://github.com/{repo}.git /opt/openzi-itworker
cd /opt/openzi-itworker
git checkout {commit}
export AWS_DEFAULT_REGION={region}
export OPENZI_DOMAIN={domain}
export OPENZI_END={end_epoch}
export OPENZI_API_KEY='{api_key}'
export OPENZI_REPO={repo}
export OPENZI_COMMIT={commit}
export OPENZI_REGION={region}
export OPENZI_SKIP_DOMAIN=1
export OPENZI_CONTACT='{{}}'
exec python3.11 -m openzi_itworker setup
"""


# ---------- 1. the workflow's role ----------

def create_admin_role(iam, log=print) -> str:
    """The workflow's step 1 (admin half): the role + instance profile itworker runs
    under. Idempotent, so a rerun after a failed round works."""
    name = config.ADMIN_PROFILE_NAME
    _ignore_exists(iam.create_role, RoleName=name, AssumeRolePolicyDocument=_EC2_TRUST)
    iam.attach_role_policy(RoleName=name,
                           PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess")
    _ignore_exists(iam.create_instance_profile, InstanceProfileName=name)
    _ignore_exists(iam.add_role_to_instance_profile, InstanceProfileName=name, RoleName=name)
    log(f"  admin role + instance profile {name} ready")
    return name


def build_setup_userdata(*, repo: str, commit: str, region: str, domain: str,
                         end_epoch: int, api_key: str) -> str:
    """Mirror of the workflow's setup user-data, pinned to the skip-domain path (the
    e2e reuses an owned domain, so no ~$3 purchase per run)."""
    return _SETUP_USERDATA.format(repo=repo, commit=commit, region=region, domain=domain,
                                  end_epoch=end_epoch, api_key=api_key)


def launch(ec2, ssm, user_data: str, profile_name: str, log=print) -> str:
    """The workflow's step 2. Retries the instance-profile eventual-consistency
    error the same way the real launch does."""
    ami = ssm.get_parameter(Name=config.BASE_AMI_PARAM)["Parameter"]["Value"]
    subnet = _default_public_subnet(ec2)
    deadline = time.monotonic() + 90
    while True:
        try:
            resp = ec2.run_instances(
                ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1,
                IamInstanceProfile={"Name": profile_name},
                NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet,
                                    "AssociatePublicIpAddress": True,
                                    "DeleteOnTermination": True}],
                UserData=user_data,
                TagSpecifications=[{"ResourceType": "instance",
                                    "Tags": [{"Key": "Name", "Value": E2E_INSTANCE_TAG}]}])
            iid = resp["Instances"][0]["InstanceId"]
            log(f"  launched {iid}")
            return iid
        except ClientError as exc:
            if "Instance Profile" in str(exc) and time.monotonic() < deadline:
                time.sleep(3)
                continue
            raise


def await_marker(ssm, timeout: float, interval: float = 30, log=print) -> tuple[str, str]:
    """Poll for itworker's result marker. Returns ('ok'|'failed', detail). The marker
    is a parameter NAME, so success and failure are distinguishable."""
    deadline = time.monotonic() + timeout
    while True:
        for name, result in ((config.SETUP_OK_PARAM, "ok"), (config.SETUP_FAILED_PARAM, "failed")):
            try:
                value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
                return result, value
            except ssm.exceptions.ParameterNotFound:
                pass
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "no setup marker: itworker never finished. Use SSM Session Manager on the "
                "instance to read `journalctl` / /var/log/cloud-init-output.log")
        log(f"  ...waiting for setup ({int(deadline - time.monotonic())}s left)")
        time.sleep(interval)


# ---------- 2. the control-plane API ----------

class ControlClient:
    """Talks to https://admin.{domain} the way deploy_client/openzi.sh does."""

    def __init__(self, base: str, api_key: str, log=print):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.log = log

    def wait_healthy(self, timeout: float, interval: float = 20) -> None:
        """The ALB, its DNS record, and the target's health check all have to settle."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._request("GET", "/").get("ok") is True:
                    return
            except Exception as exc:  # noqa: BLE001 — DNS/TLS/503 while it settles
                last = exc
            if time.monotonic() >= deadline:
                raise TimeoutError(f"control plane never became healthy: {last}")
            time.sleep(interval)

    def run(self, action: str, body: dict, timeout: float, interval: float = 20) -> dict:
        """Submit an action and poll its job to a terminal state."""
        job = self._request("POST", f"/{action}", body)["job"]
        self.log(f"  {action} job {job}")
        deadline = time.monotonic() + timeout
        while True:
            doc = self._request("GET", f"/status?id={job}")
            status = doc.get("status")
            if status == config.JOB_SUCCEEDED:
                return doc
            if status == config.JOB_FAILED:
                raise AssertionError(f"{action} failed: {doc.get('error')}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{action} did not finish in {timeout}s")
            time.sleep(interval)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"x-api-key": self.api_key,
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def fetch(url: str, timeout: float = 30) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def wait_for_app(url: str, timeout: float, interval: float = 15, log=print) -> str:
    """An app instance needs a moment past deploy to boot and pass its health check."""
    deadline = time.monotonic() + timeout
    while True:
        status, body = fetch(url)
        if status == 200:
            return body
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{url} never served 200 (last {status})")
        log(f"  ...waiting for {url} (last {status})")
        time.sleep(interval)


# ---------- teardown ----------

def cleanup(session, log=print) -> None:
    """Best-effort: leave the account reusable for the next round. The domain and its
    hosted zone are kept on purpose — the skip-domain path needs them."""
    ec2 = session.client("ec2")
    asg = session.client("autoscaling")
    cfn = session.client("cloudformation")
    iam = session.client("iam")
    ssm = session.client("ssm")

    log("  deleting the control ASG")
    _swallow(asg.delete_auto_scaling_group,
             AutoScalingGroupName=config.CONTROL_ASG_NAME, ForceDelete=True)

    log("  terminating e2e instances")
    ids = [i["InstanceId"]
           for r in ec2.describe_instances(
               Filters=[{"Name": "tag:Name", "Values": [E2E_INSTANCE_TAG]},
                        {"Name": "instance-state-name",
                         "Values": ["pending", "running", "stopping", "stopped"]}]
           ).get("Reservations", [])
           for i in r.get("Instances", [])]
    if ids:
        _swallow(ec2.terminate_instances, InstanceIds=ids)
        _wait_terminated(ec2, ids, log)

    _swallow(ec2.delete_launch_template, LaunchTemplateName=config.CONTROL_LT_NAME)

    log(f"  deleting stack {config.PLATFORM_STACK_NAME}")
    _swallow(cfn.delete_stack, StackName=config.PLATFORM_STACK_NAME)
    _wait_stack_gone(cfn, log)

    log("  deleting the admin role + profile")
    _swallow(iam.remove_role_from_instance_profile,
             InstanceProfileName=config.ADMIN_PROFILE_NAME, RoleName=config.ADMIN_PROFILE_NAME)
    _swallow(iam.delete_instance_profile, InstanceProfileName=config.ADMIN_PROFILE_NAME)
    _swallow(iam.detach_role_policy, RoleName=config.ADMIN_PROFILE_NAME,
             PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess")
    _swallow(iam.delete_role, RoleName=config.ADMIN_PROFILE_NAME)

    log("  wiping /openzi/ SSM parameters")
    names = [p["Name"]
             for page in ssm.get_paginator("describe_parameters").paginate(
                 ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": ["/openzi"]}])
             for p in page.get("Parameters", [])]
    for i in range(0, len(names), 10):
        _swallow(ssm.delete_parameters, Names=names[i:i + 10])


def _wait_terminated(ec2, ids, log, timeout=600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = {i["State"]["Name"]
                  for r in ec2.describe_instances(InstanceIds=ids).get("Reservations", [])
                  for i in r.get("Instances", [])}
        if states <= {"terminated"}:
            return
        time.sleep(15)
    log("  (instances still terminating; continuing)")


def _wait_stack_gone(cfn, log, timeout=1800) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cfn.describe_stacks(StackName=config.PLATFORM_STACK_NAME)
        except ClientError as exc:
            if "does not exist" in str(exc):
                return
            raise
        time.sleep(20)
    log("  (stack still deleting; continuing)")


# ---------- small helpers ----------

def _default_public_subnet(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}]).get("Vpcs", [])
    if not vpcs:
        raise RuntimeError("no default VPC in this account/region")
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]},
                 {"Name": "default-for-az", "Values": ["true"]}]).get("Subnets", [])
    if not subnets:
        raise RuntimeError("default VPC has no default subnet")
    return sorted(subnets, key=lambda s: s["AvailabilityZone"])[0]["SubnetId"]


def _ignore_exists(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("EntityAlreadyExists",
                                                            "LimitExceeded"):
            raise


def _swallow(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except Exception:  # noqa: BLE001 — teardown tolerates already-gone
        pass
