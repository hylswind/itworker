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

from openzp_itworker import config

E2E_INSTANCE_TAG = "openzp-itworker-e2e"

_EC2_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]})

# The instance clones itworker from GitHub, so the e2e exercises a PUSHED commit —
# uncommitted local work is not what runs.
#
# Kept deliberately identical to openzp_workflow.userdata._SETUP_TEMPLATE, down to
# the shell quoting: the point of this driver is that itworker sees exactly what the
# workflow would hand it. If the two drift, the e2e stops testing the real thing.
_SETUP_USERDATA = r"""#!/bin/bash
set -euxo pipefail
dnf install -y git python3.11 python3.11-pip
python3.11 -m pip install boto3
rm -rf /opt/openzp-itworker
git clone https://github.com/{repo}.git /opt/openzp-itworker
cd /opt/openzp-itworker
git checkout {commit}
export AWS_DEFAULT_REGION={region}
export OPENZP_DOMAIN={domain}
export OPENZP_END={end_epoch}
export OPENZP_API_KEY={api_key}
export OPENZP_REPO={repo}
export OPENZP_COMMIT={commit}
export OPENZP_REGION={region}
export OPENZP_SKIP_DOMAIN={skip_domain}
export OPENZP_CONTACT={contact_shell}
exec python3.11 -m openzp_itworker setup
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


def _shquote(value: str) -> str:
    """Single-quote a value for safe use in the exported shell assignment."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_setup_userdata(*, repo: str, commit: str, region: str, domain: str,
                         end_epoch: int, api_key: str, skip_domain: bool = True,
                         contact: dict | None = None) -> str:
    """Mirror of the workflow's setup user-data. skip_domain defaults to True — a
    reused domain costs nothing per round — but passing False exercises the real
    RegisterDomain path, which BUYS the domain (~$3, non-refundable) and needs a
    contact carrying every field in contacts.REQUIRED."""
    return _SETUP_USERDATA.format(
        repo=repo, commit=commit, region=region, domain=domain, end_epoch=end_epoch,
        api_key=_shquote(api_key), skip_domain="1" if skip_domain else "0",
        contact_shell=_shquote(json.dumps(contact or {})))


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
    """Talks to https://admin.{domain} the way deploy_client/openzp.sh does."""

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

    def get(self, path: str) -> dict:
        """A read-only route: no job to poll, the answer comes straight back."""
        return self._request("GET", path)

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
    """Status and body. A network-level failure — DNS that hasn't propagated, a TLS
    reset from an ALB still coming up — comes back as status 0 instead of raising,
    so a polling caller keeps polling. Raising there would abandon a 15-minute wait
    over one blip. (HTTPError first: it is a subclass of the OSError catch.)"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except OSError as exc:  # URLError, socket timeout, TLS — all transient here
        return 0, str(exc)


def wait_for_200(url: str, timeout: float, interval: float = 15, log=print) -> str:
    """Poll until the URL serves 200. Nothing here is ready the instant the API call
    that created it returns: an ALB rule takes seconds to reach every node (until it
    does, the listener's default 404 answers), and a freshly deployed app instance
    has to boot and pass its health check first."""
    deadline = time.monotonic() + timeout
    while True:
        status, body = fetch(url)
        if status == 200:
            return body
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{url} never served 200 (last {status}): {body[:200]}")
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

    # Take each group's instance ids BEFORE deleting it: ForceDelete only INITIATES
    # termination, so once the group is gone there is nothing left to read them from
    # while the instances are still alive. recover does this for the same reason.
    log("  deleting the control ASG and any app ASGs")
    ids: list[str] = []
    for group in asg.describe_auto_scaling_groups().get("AutoScalingGroups", []):
        name = group["AutoScalingGroupName"]
        # App ASGs are usually gone by now — recover removes them — but a round that
        # died between deploy and recover leaves them holding instances INSIDE the
        # stack's VPC, and a VPC whose ENIs are still in use cannot be deleted.
        if name != config.CONTROL_ASG_NAME and not name.startswith(config.ASG_PREFIX):
            continue
        ids += [i["InstanceId"] for i in group.get("Instances", [])]
        _swallow(asg.delete_auto_scaling_group, AutoScalingGroupName=name, ForceDelete=True)

    log("  terminating instances")
    ids += [i["InstanceId"]  # launched outside any ASG, and tagged only by this driver
            for r in ec2.describe_instances(
                Filters=[{"Name": "tag:Name", "Values": [E2E_INSTANCE_TAG]},
                         {"Name": "instance-state-name",
                          "Values": ["pending", "running", "stopping", "stopped"]}]
            ).get("Reservations", [])
            for i in r.get("Instances", [])]
    ids = sorted(set(ids))
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

    log("  wiping /openzp/ SSM parameters")
    names = [p["Name"]
             for page in ssm.get_paginator("describe_parameters").paginate(
                 ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": ["/openzp"]}])
             for p in page.get("Parameters", [])]
    for i in range(0, len(names), 10):
        _swallow(ssm.delete_parameters, Names=names[i:i + 10])


def _wait_terminated(ec2, ids, log, timeout=600) -> None:
    """Filters, not InstanceIds: the list can now include instances that terminated
    long ago (an app ASG left behind by an earlier failed round), and AWS purges those
    records — an InstanceIds query raises on a purged id, a Filters query just omits
    it, which reads correctly as 'gone'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = {i["State"]["Name"]
                  for r in ec2.describe_instances(
                      Filters=[{"Name": "instance-id", "Values": ids}]).get("Reservations", [])
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
