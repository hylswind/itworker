"""The four control-plane actions: init / deploy / delete / recover. Same SSM
registry/manifest layout, resource names, and GitHub-id bake contract as before;
recover's ordering is unchanged (wipe -> CT proof -> wait for termination ->
restore sign-in)."""

from __future__ import annotations

import base64
import json
import time

from botocore.exceptions import ClientError

from .. import config
from ..context import Ctx
from . import github, imagebuilder


class ActionError(Exception):
    """Raised with a client-facing message; the server records it on the job."""


# ---------- init ----------

def init(ctx: Ctx, payload: dict) -> dict:
    """Bind an app name to a GitHub repo forever and publish /{app}/info.json.
    Resumable: a rerun after a partial init finishes the info rule."""
    app, repo = payload["app"], payload["repo"]
    ssm = ctx.client("ssm")

    try:
        gh = github.resolve(repo)
    except github.RepoLookupError as exc:
        raise ActionError(f"RepoLookupFailed: {exc}") from exc

    # repo_at_init is the owner/name AS RESOLVED AT INIT — human-readable only, and a
    # SNAPSHOT: deploy pins to the immutable owner_id/repo_id, so a later rename
    # leaves this string stale. Never read by the deploy path.
    record = {"app": app, "owner_id": gh["owner_id"], "repo_id": gh["repo_id"],
              "repo_at_init": gh["full_name"]}
    name = config.APP_PARAM.format(app=app)
    try:
        ssm.put_parameter(Name=name, Value=json.dumps(record), Type="String", Overwrite=False)
    except ssm.exceptions.ParameterAlreadyExists:
        # resume: if the info rule already exists the app is fully initialized;
        # otherwise an earlier init died before creating it — finish from the
        # ORIGINAL stored record, not this rerun's fresh lookup.
        if _find_rule(ctx, f"/{app}/info.json"):
            raise ActionError("AppExists: the binding is immutable") from None
        record = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])

    priority = _alloc_priority(ctx)
    _create_info_rule(ctx, app, priority, record)
    return {"app": app, "priority": priority}


# ---------- deploy ----------

def deploy(ctx: Ctx, payload: dict) -> dict:
    app, commit = payload["app"], _version_id(payload["commit"])
    ssm = ctx.client("ssm")
    try:
        binding = json.loads(ssm.get_parameter(Name=config.APP_PARAM.format(app=app))["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        raise ActionError("AppNotFound: init the app first") from None

    manifest_name = config.VERSION_PARAM.format(app=app, commit=commit)
    try:
        ssm.get_parameter(Name=manifest_name)
        raise ActionError("AlreadyDeployed: delete the version first")
    except ssm.exceptions.ParameterNotFound:
        pass

    priority = _alloc_priority(ctx)
    _put_secret(ctx, app, commit)
    profile_arn, secret_role = _create_version_role(ctx, app, priority, commit)
    ami = _bake_ami(ctx, binding, app, commit)  # ~10 min — also lets the new IAM profile propagate
    tg_arn = _create_tg(ctx, app, priority, commit)
    lt_id = _create_lt(ctx, priority, commit, ami["ami_id"], profile_arn)
    asg_name = _create_asg(ctx, app, priority, commit, lt_id, tg_arn)
    rule_arn = _create_forward_rule(ctx, app, commit, priority, tg_arn)

    manifest = {"rule_arn": rule_arn, "asg_name": asg_name, "tg_arn": tg_arn, "lt_id": lt_id,
                "ami": ami["ami_id"], "component_arn": ami["component_arn"],
                "recipe_arn": ami["recipe_arn"], "image_arn": ami["image_arn"],
                "secret_role": secret_role}  # secret_param is derivable from app+commit
    ssm.put_parameter(Name=manifest_name, Value=json.dumps(manifest), Type="String", Overwrite=True)
    return {"app": app, "commit": commit, "priority": priority}


# ---------- delete ----------

def delete(ctx: Ctx, payload: dict) -> dict:
    app, commit = payload["app"], _version_id(payload["commit"])
    ssm = ctx.client("ssm")
    manifest_name = config.VERSION_PARAM.format(app=app, commit=commit)
    try:
        m = json.loads(ssm.get_parameter(Name=manifest_name)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        raise ActionError("VersionNotFound: never deployed, or already deleted") from None

    elb, asg, ec2, ib = (ctx.client("elbv2"), ctx.client("autoscaling"),
                         ctx.client("ec2"), ctx.client("imagebuilder"))
    # each step tolerates already-gone resources so re-runs converge.
    _swallow(elb.delete_rule, RuleArn=m["rule_arn"])
    _swallow(asg.delete_auto_scaling_group, AutoScalingGroupName=m["asg_name"], ForceDelete=True)
    time.sleep(60)  # let the ASG release the TG before deleting it
    _swallow(elb.delete_target_group, TargetGroupArn=m["tg_arn"])
    _swallow(ec2.delete_launch_template, LaunchTemplateId=m["lt_id"])
    _delete_ami(ctx, m["ami"])
    _swallow(ib.delete_image, imageBuildVersionArn=m["image_arn"])
    _swallow(ib.delete_image_recipe, imageRecipeArn=m["recipe_arn"])
    _swallow(ib.delete_component, componentBuildVersionArn=m["component_arn"])
    _swallow(ssm.delete_parameter, Name=config.SECRET_PARAM.format(app=app, commit=commit))
    _delete_version_role(ctx, m["secret_role"])
    _swallow(ssm.delete_parameter, Name=manifest_name)
    return {"app": app, "commit": commit}


# ---------- recover ----------

def recover(ctx: Ctx, payload: dict) -> dict:
    """Full reset: wipe every app (rules/ASGs/TGs/LTs by name wildcard + the
    /openzi/{secrets,versions,apps} SSM records) -> ACM recover.{domain} CT-log
    proof -> WAIT for the app instances to actually terminate -> delete the AWS
    Sign-In lockout, restoring root login. The wait is a security gate: a running
    instance still holds its version secret in memory."""
    p = ctx.platform
    elb, asg, ec2 = ctx.client("elbv2"), ctx.client("autoscaling"), ctx.client("ec2")

    for rule in elb.describe_rules(ListenerArn=p.app_listener_arn)["Rules"]:
        if not rule["IsDefault"]:
            _swallow(elb.delete_rule, RuleArn=rule["RuleArn"])
    # Capture each app ASG's instance ids as we delete it: ForceDelete only INITIATES
    # termination (async), and those instances still hold their OPENZI_VERSION_SECRET
    # in the running container's memory. We must confirm they are gone before
    # restoring console login (below).
    app_instance_ids: list[str] = []
    for group in asg.describe_auto_scaling_groups()["AutoScalingGroups"]:
        if group["AutoScalingGroupName"].startswith(config.ASG_PREFIX):
            app_instance_ids += [i["InstanceId"] for i in group.get("Instances", [])]
            _swallow(asg.delete_auto_scaling_group,
                     AutoScalingGroupName=group["AutoScalingGroupName"], ForceDelete=True)
    time.sleep(60)
    for tg in elb.describe_target_groups()["TargetGroups"]:
        if tg["TargetGroupName"].startswith(config.TG_PREFIX):
            _swallow(elb.delete_target_group, TargetGroupArn=tg["TargetGroupArn"])
    for lt in ec2.describe_launch_templates(
            Filters=[{"Name": "launch-template-name", "Values": [config.LT_PREFIX + "*"]}]).get("LaunchTemplates", []):
        _swallow(ec2.delete_launch_template, LaunchTemplateId=lt["LaunchTemplateId"])

    # Tear down every version's isolation resources BEFORE root login is restored:
    # the secret parameters, the per-version roles/profiles, AND the app bindings +
    # version manifests — a full reset, so a re-init/re-deploy after recover starts
    # clean instead of hitting AppExists/AlreadyDeployed against phantom records.
    ssm, iam = ctx.client("ssm"), ctx.client("iam")

    # Do this while the manifests still exist. The bake artifacts are named with a
    # random token, so no prefix sweep can find them — the manifest is the only record
    # of their arns, and it is deleted just below.
    _delete_bake_artifacts(ctx, ssm)

    names = [prm["Name"]
             for prefix in (config.SECRET_PREFIX, config.VERSION_PREFIX, config.APP_PREFIX)
             for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=prefix, Recursive=True)
             for prm in page.get("Parameters", [])]
    for i in range(0, len(names), 10):  # DeleteParameters takes up to 10 names
        _swallow(ssm.delete_parameters, Names=names[i:i + 10])
    for page in iam.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            if role["RoleName"].startswith(config.SECRET_ROLE_PREFIX):
                _delete_version_role(ctx, role["RoleName"])

    _ct_log_proof(ctx)

    # SECURITY GATE: only restore console login once every app instance is actually
    # terminated. Otherwise root regains access while a live instance still holds the
    # version secret in memory. This is the one step order truly matters for.
    _wait_instances_terminated(ec2, app_instance_ids)
    _delete_signin_lock(ctx.client("signin"), p.account_id)
    return {"recovered": True}


def _delete_bake_artifacts(ctx: Ctx, ssm) -> None:
    """Drop what each version manifest records from its bake: the AMI (with its
    snapshots, which bill for as long as they exist) and the Image Builder image,
    recipe and component. Same work `delete` does per version, in the same dependency
    order — the image is built from the recipe, the recipe references the component."""
    ib = ctx.client("imagebuilder")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(
            Path=config.VERSION_PREFIX, Recursive=True):
        for prm in page.get("Parameters", []):
            try:
                m = json.loads(prm.get("Value") or "")
            except ValueError:
                continue  # not a manifest we wrote; nothing to act on
            if m.get("ami"):
                _delete_ami(ctx, m["ami"])
            if m.get("image_arn"):
                _swallow(ib.delete_image, imageBuildVersionArn=m["image_arn"])
            if m.get("recipe_arn"):
                _swallow(ib.delete_image_recipe, imageRecipeArn=m["recipe_arn"])
            if m.get("component_arn"):
                _swallow(ib.delete_component, componentBuildVersionArn=m["component_arn"])


# App instances launch untagged (ASG tags aren't propagated), so we track them by
# the ids the ASGs held and poll those to a terminated state.
_TERMINATE_POLL = 10


def _wait_instances_terminated(ec2, instance_ids: list[str]) -> None:
    """Block until every listed instance is terminated. NO timeout on purpose: this
    gates the console unlock, so it must never give up early and let root back in
    while an instance still holds the version secret in memory. A `Filters` query
    never raises on a purged id, and any transient API error just retries — it must
    not be read as 'gone'."""
    pending = set(instance_ids)
    while pending:
        try:
            resp = ec2.describe_instances(
                Filters=[{"Name": "instance-id", "Values": list(pending)}])
        except Exception:  # noqa: BLE001 — throttle/transient: retry, never unlock early
            time.sleep(_TERMINATE_POLL)
            continue
        live = {inst["InstanceId"]
                for res in resp.get("Reservations", []) for inst in res.get("Instances", [])
                if inst["State"]["Name"] != "terminated"}
        pending = pending & live  # purged ids simply don't come back → drop out
        if pending:
            time.sleep(_TERMINATE_POLL)


def _delete_signin_lock(signin, account_id: str) -> None:
    """Restore console login: disable console-authorization enforcement, then delete
    every sign-in resource permission statement. We don't track a statement id —
    normally there is exactly one, but listing and deleting all is both correct and
    idempotent.

    ResourceNotFound is tolerated at each step: there is simply nothing to undo (an
    account the lock was never applied to, or a re-run of recover). Every OTHER error
    propagates — reporting "recovered" while the console is still sealed would be a
    lie, and this is the one step whose failure locks the operator out for good.

    The signin model uses lowerCamelCase params (targetId / statementId) — pinned by
    a Stubber test against the real service model."""
    _ignore_missing(signin.delete_console_authorization_configuration, targetId=account_id)
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        try:
            resp = signin.list_resource_permission_statements(**kwargs)
        except ClientError as exc:
            if _is_missing(exc):
                return
            raise
        for st in resp.get("permissionStatements", []):
            _ignore_missing(signin.delete_resource_permission_statement, statementId=st["sid"])
        token = resp.get("nextToken")
        if not token:
            return


def _is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def _ignore_missing(fn, **kwargs) -> None:
    """Call fn, tolerating only 'it isn't there' — never a denial or a throttle."""
    try:
        fn(**kwargs)
    except ClientError as exc:
        if not _is_missing(exc):
            raise


# ---------- shared helpers ----------

def _version_id(commit: str) -> str:
    """The 7-char, GitHub-style short sha used as the version identity everywhere:
    the URL path, the AWS resource names, the secret/manifest keys, and the bake's
    `git checkout`. The API accepts a full 40-char sha and this trims it."""
    return commit[:7]


def _alloc_priority(ctx: Ctx) -> int:
    ssm = ctx.client("ssm")
    current = int(ssm.get_parameter(Name=config.PRIORITY_COUNTER)["Parameter"]["Value"])
    priority = current + 1
    ssm.put_parameter(Name=config.PRIORITY_COUNTER, Value=str(priority), Type="String", Overwrite=True)
    return priority


def _put_secret(ctx: Ctx, app: str, commit: str) -> None:
    """Generate this version's 64-byte shared secret and store it encrypted (SSM
    SecureString, default aws/ssm key). base64 so the app gets printable bytes."""
    rnd = ctx.client("kms").generate_random(NumberOfBytes=64)["Plaintext"]
    ctx.client("ssm").put_parameter(
        Name=config.SECRET_PARAM.format(app=app, commit=commit),
        Value=base64.b64encode(rnd).decode(), Type="SecureString", Overwrite=True)


def _create_version_role(ctx: Ctx, app: str, priority: int, commit: str) -> tuple[str, str]:
    """The per-version instance role: it may read ONLY this version's secret
    parameter, so one version's host can't read another's. Returns (instance profile
    ARN, role name). Role and profile share the name."""
    iam, p = ctx.client("iam"), ctx.platform
    name = config.SECRET_ROLE_NAME.format(priority=priority, commit=commit)
    secret_arn = (f"arn:aws:ssm:{p.region}:{p.account_id}:parameter"
                  + config.SECRET_PARAM.format(app=app, commit=commit))
    iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                       "Action": "sts:AssumeRole"}]}))
    iam.put_role_policy(RoleName=name, PolicyName="read-secret", PolicyDocument=json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "ssm:GetParameter", "Resource": secret_arn},
            {"Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*",
             "Condition": {"StringEquals": {"kms:ViaService": f"ssm.{p.region}.amazonaws.com"}}}]}))
    iam.create_instance_profile(InstanceProfileName=name)
    iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
    return f"arn:aws:iam::{p.account_id}:instance-profile/{name}", name


def _delete_version_role(ctx: Ctx, name: str) -> None:
    iam = ctx.client("iam")
    _swallow(iam.remove_role_from_instance_profile, InstanceProfileName=name, RoleName=name)
    _swallow(iam.delete_instance_profile, InstanceProfileName=name)
    _swallow(iam.delete_role_policy, RoleName=name, PolicyName="read-secret")
    _swallow(iam.delete_role, RoleName=name)


def _find_rule(ctx: Ctx, path: str):
    elb = ctx.client("elbv2")
    for rule in elb.describe_rules(ListenerArn=ctx.platform.app_listener_arn)["Rules"]:
        for cond in rule.get("Conditions", []):
            values = cond.get("PathPatternConfig", {}).get("Values", [])
            if path in values:
                return rule
    return None


def _create_info_rule(ctx: Ctx, app: str, priority: int, record: dict) -> None:
    elb = ctx.client("elbv2")
    try:
        elb.create_rule(
            ListenerArn=ctx.platform.app_listener_arn, Priority=priority,
            Conditions=[{"Field": "path-pattern", "PathPatternConfig": {"Values": [f"/{app}/info.json"]}}],
            Actions=[{"Type": "fixed-response", "FixedResponseConfig": {
                "StatusCode": "200", "ContentType": "application/json",
                "MessageBody": json.dumps(record)}}])
    except elb.exceptions.PriorityInUseException as exc:
        raise ActionError("PriorityInUse: re-run the init (it resumes)") from exc


def _create_forward_rule(ctx: Ctx, app: str, commit: str, priority: int, tg_arn: str) -> str:
    elb = ctx.client("elbv2")
    try:
        resp = elb.create_rule(
            ListenerArn=ctx.platform.app_listener_arn, Priority=priority,
            Conditions=[{"Field": "path-pattern", "PathPatternConfig": {"Values": [f"/{app}/{commit}/*"]}}],
            Actions=[{"Type": "forward", "TargetGroupArn": tg_arn}])
    except elb.exceptions.PriorityInUseException as exc:
        raise ActionError("PriorityInUse: concurrent deploy race; re-run") from exc
    return resp["Rules"][0]["RuleArn"]


def _create_tg(ctx: Ctx, app: str, priority: int, commit: str) -> str:
    elb = ctx.client("elbv2")
    resp = elb.create_target_group(
        Name=config.TG_NAME.format(priority=priority, commit=commit), Protocol="HTTP",
        Port=config.APP_PORT, VpcId=ctx.platform.vpc, HealthCheckPort=str(config.HEALTH_PORT),
        HealthCheckPath="/", TargetType="instance",
        Tags=[{"Key": config.TAG_APP, "Value": app}, {"Key": config.TAG_COMMIT, "Value": commit}])
    return resp["TargetGroups"][0]["TargetGroupArn"]


def _create_lt(ctx: Ctx, priority: int, commit: str, ami_id: str, profile_arn: str) -> str:
    ec2 = ctx.client("ec2")
    resp = ec2.create_launch_template(
        LaunchTemplateName=config.LT_NAME.format(priority=priority, commit=commit),
        LaunchTemplateData={"ImageId": ami_id, "InstanceType": "t3.small",
                            "IamInstanceProfile": {"Arn": profile_arn},
                            "SecurityGroupIds": [ctx.platform.instance_sg_id],
                            # hop limit 1: the app runs in a bridged container, one hop
                            # from IMDS — this keeps it from reaching the host's
                            # per-version role (only the host reads the secret).
                            "MetadataOptions": {"HttpTokens": "required",
                                                "HttpPutResponseHopLimit": 1}})
    return resp["LaunchTemplate"]["LaunchTemplateId"]


def _create_asg(ctx: Ctx, app: str, priority: int, commit: str, lt_id: str, tg_arn: str) -> str:
    asg = ctx.client("autoscaling")
    name = config.ASG_NAME.format(priority=priority, commit=commit)
    asg.create_auto_scaling_group(
        AutoScalingGroupName=name, MinSize=1, MaxSize=3, DesiredCapacity=1,
        HealthCheckType="ELB", HealthCheckGracePeriod=120,
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        TargetGroupARNs=[tg_arn],
        VPCZoneIdentifier=f"{ctx.platform.subnet_a},{ctx.platform.subnet_b}",
        Tags=[{"Key": config.TAG_APP, "Value": app, "PropagateAtLaunch": False},
              {"Key": config.TAG_COMMIT, "Value": commit, "PropagateAtLaunch": False}])
    return name


def _bake_ami(ctx: Ctx, binding: dict, app: str, commit: str) -> dict:
    """Bake the app AMI (clone by bound repo_id, verify owner_id) via the shared
    Image Builder driver."""
    data = _bake_component_data(binding["repo_id"], binding["owner_id"], app, commit)
    try:
        return imagebuilder.bake(ctx.client("imagebuilder"), ctx.client("ssm"), data,
                                 ctx.platform.ib_infra_arn, ctx.platform.ib_dist_arn)
    except RuntimeError as exc:
        raise ActionError(f"ImageBuildFailed: {exc}") from exc


def _bake_component_data(repo_id: int, owner_id: int, app: str, commit: str) -> str:
    """The bake script — github-id clone + owner verify, docker build at bake time,
    platform-dictated `docker run`. app.service reads this version's 64-byte secret
    from SSM (the host's per-version role decrypts it) and injects it into the
    container as OPENZI_VERSION_SECRET; the container itself is IMDS-blocked."""
    return _COMPONENT_TEMPLATE.format(
        repo_id=repo_id, owner_id=owner_id, commit=commit,
        secret_param=config.SECRET_PARAM.format(app=app, commit=commit))


# app.service is written via a quoted heredoc so the bake shell does NOT expand the
# $(...) / $S — they must survive into the unit and run at instance boot.
_COMPONENT_TEMPLATE = r"""name: BuildApp
schemaVersion: 1.0
phases:
  - name: build
    steps:
      - name: build
        action: ExecuteBash
        inputs:
          commands:
            - dnf install -y git docker jq
            - systemctl enable docker
            - systemctl start docker
            - curl -fsSL https://api.github.com/repositories/{repo_id} -o /tmp/repo.json
            - jq -e ".owner.id == {owner_id}" /tmp/repo.json
            - git clone $(jq -re .clone_url /tmp/repo.json) /opt/app
            - cd /opt/app && git checkout {commit}
            - cd /opt/app && git rev-parse HEAD | grep -q "^{commit}" || exit 1
            - docker build -t app:{commit} /opt/app
            - |
              cat > /etc/systemd/system/app.service <<'UNIT'
              [Unit]
              Requires=docker.service
              After=docker.service network-online.target
              Wants=network-online.target
              [Service]
              Type=simple
              Restart=always
              RestartSec=5
              ExecStart=/bin/bash -c 'S=$(aws ssm get-parameter --name "{secret_param}" --with-decryption --query Parameter.Value --output text); exec docker run --rm --name app -p 80:8080 -p 8081:8081 -e OPENZI_VERSION_SECRET="$S" app:{commit}'
              [Install]
              WantedBy=multi-user.target
              UNIT
            - systemctl enable app.service
"""


def _delete_ami(ctx: Ctx, ami_id: str) -> None:
    ec2 = ctx.client("ec2")
    try:
        images = ec2.describe_images(ImageIds=[ami_id])["Images"]
    except Exception:  # noqa: BLE001
        return
    if not images:
        return
    snap_ids = [bdm["Ebs"]["SnapshotId"] for bdm in images[0].get("BlockDeviceMappings", [])
                if "Ebs" in bdm and "SnapshotId" in bdm["Ebs"]]
    _swallow(ec2.deregister_image, ImageId=ami_id)
    for snap in snap_ids:
        _swallow(ec2.delete_snapshot, SnapshotId=snap)


def _ct_log_proof(ctx: Ctx) -> None:
    """Request an ACM cert for recover.{domain} via DNS — its issuance is an
    immutable, public Certificate-Transparency record that the account was recovered."""
    acm, r53 = ctx.client("acm"), ctx.client("route53")
    p = ctx.platform
    cert_arn = acm.request_certificate(
        DomainName=f"recover.{p.domain}", ValidationMethod="DNS",
        IdempotencyToken="openzirecover")["CertificateArn"]

    record = None
    for _ in range(30):
        opts = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"].get(
            "DomainValidationOptions", [])
        if opts and opts[0].get("ResourceRecord"):
            record = opts[0]["ResourceRecord"]
            break
        time.sleep(10)
    if record is None:
        raise ActionError("CertFailed: no DNS validation record appeared")

    r53.change_resource_record_sets(
        HostedZoneId=p.hosted_zone_id,
        ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": record["Name"], "Type": "CNAME", "TTL": 300,
            "ResourceRecords": [{"Value": record["Value"]}]}}]})

    for _ in range(40):
        status = acm.describe_certificate(CertificateArn=cert_arn)["Certificate"]["Status"]
        if status == "ISSUED":
            return
        if status == "FAILED":
            raise ActionError("CertFailed: recover cert did not issue")
        time.sleep(30)
    raise ActionError("CertFailed: timed out waiting for issuance")


def _swallow(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except Exception:  # noqa: BLE001 — idempotent teardown tolerates already-gone
        pass


ACTIONS = {"init": init, "deploy": deploy, "delete": delete, "recover": recover}
