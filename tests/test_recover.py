import json

import pytest
from botocore.exceptions import ClientError
from fakes import FakeElb, FakeSsm, FakeCtx, platform

from openzp_itworker.server import actions


# ---------- signin teardown against the REAL botocore model ----------

def test_recover_signin_teardown_matches_the_real_model():
    """Validate the exact kwargs (lowerCamelCase targetId / statementId) against the
    real signin model via Stubber — no network. Guards a casing mistake a hand fake
    would accept."""
    import boto3
    from botocore.stub import Stubber

    signin = boto3.client("signin", region_name="us-east-1",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    stub = Stubber(signin)
    stub.add_response("delete_console_authorization_configuration",
                      {"targetId": "123456789012", "scope": "ACCOUNT",
                       "consoleAuthorizationEnabled": False},
                      {"targetId": "123456789012"})
    stub.add_response("list_resource_permission_statements",
                      {"permissionStatements": [{"sid": "stmt-1"}]}, {})
    stub.add_response("delete_resource_permission_statement", {}, {"statementId": "stmt-1"})
    with stub:
        actions._delete_signin_lock(signin, "123456789012")
    stub.assert_no_pending_responses()


def _client_error(code, op):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


class _MissingSignin:
    """An account the lock was never applied to — every call says it isn't there."""

    def delete_console_authorization_configuration(self, targetId):
        raise _client_error("ResourceNotFoundException", "DeleteConsoleAuthorizationConfiguration")

    def list_resource_permission_statements(self, **kwargs):
        raise _client_error("ResourceNotFoundException", "ListResourcePermissionStatements")


def test_signin_teardown_tolerates_nothing_to_undo():
    """recover must succeed on an account that was never locked (e.g. the itworker
    e2e, which never applies the lockout), and on a re-run of recover."""
    actions._delete_signin_lock(_MissingSignin(), "123456789012")


class _DeniedSignin:
    def delete_console_authorization_configuration(self, targetId):
        raise _client_error("AccessDeniedException", "DeleteConsoleAuthorizationConfiguration")


def test_signin_teardown_propagates_real_failures():
    """A denial must NOT be swallowed: reporting 'recovered' while the console stays
    sealed would lock the operator out for good."""
    with pytest.raises(ClientError):
        actions._delete_signin_lock(_DeniedSignin(), "123456789012")


# ---------- instance-termination gate ----------

class _FakeEc2Instances:
    def __init__(self, states):
        self._states = {i: list(seq) for i, seq in states.items()}

    def describe_instances(self, Filters):
        ids = next(f["Values"] for f in Filters if f["Name"] == "instance-id")
        insts = []
        for i in ids:
            seq = self._states.get(i)
            if not seq:
                continue
            state = seq.pop(0) if len(seq) > 1 else seq[0]
            insts.append({"InstanceId": i, "State": {"Name": state}})
        return {"Reservations": [{"Instances": insts}]}

    def describe_launch_templates(self, Filters):
        return {"LaunchTemplates": []}


def test_wait_instances_terminated_blocks_until_gone(monkeypatch):
    monkeypatch.setattr(actions, "_TERMINATE_POLL", 0)
    ec2 = _FakeEc2Instances({"i-1": ["running", "shutting-down", "terminated"]})
    actions._wait_instances_terminated(ec2, ["i-1"])


def test_wait_instances_terminated_retries_transient_error(monkeypatch):
    monkeypatch.setattr(actions, "_TERMINATE_POLL", 0)

    class _Flaky(_FakeEc2Instances):
        def __init__(self):
            super().__init__({"i-1": ["running", "terminated"]})
            self.calls = 0

        def describe_instances(self, Filters):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Throttling")
            return super().describe_instances(Filters)

    actions._wait_instances_terminated(_Flaky(), ["i-1"])


# ---------- full recover ----------

class _RecoverElb(FakeElb):
    def __init__(self, rules):
        super().__init__(rules=rules)
        self.deleted_rules, self.deleted_tgs = [], []

    def delete_rule(self, RuleArn):
        self.deleted_rules.append(RuleArn)

    def describe_target_groups(self):
        return {"TargetGroups": [{"TargetGroupName": "openzp-tg-1-abc1234",
                                  "TargetGroupArn": "arn:tg/1"}]}

    def delete_target_group(self, TargetGroupArn):
        self.deleted_tgs.append(TargetGroupArn)


class _RecoverAsg:
    def __init__(self, groups):
        self._groups = groups
        self.deleted = []

    def describe_auto_scaling_groups(self):
        return {"AutoScalingGroups": self._groups}

    def delete_auto_scaling_group(self, AutoScalingGroupName, ForceDelete):
        self.deleted.append(AutoScalingGroupName)


class _RecoverIam:
    def get_paginator(self, op):
        class _P:
            def paginate(self):
                return [{"Roles": []}]
        return _P()


class _RecoverSignin:
    def __init__(self):
        self.unlocked = False

    def delete_console_authorization_configuration(self, targetId):
        self.unlocked = True

    def list_resource_permission_statements(self, **kwargs):
        return {"permissionStatements": [{"sid": "stmt-1"}]}

    def delete_resource_permission_statement(self, statementId):
        pass


class _RecoverIb:
    def __init__(self):
        self.deleted = []

    def delete_image(self, imageBuildVersionArn):
        self.deleted.append(("image", imageBuildVersionArn))

    def delete_image_recipe(self, imageRecipeArn):
        self.deleted.append(("recipe", imageRecipeArn))

    def delete_component(self, componentBuildVersionArn):
        self.deleted.append(("component", componentBuildVersionArn))


def _recover_ctx(monkeypatch, groups, instance_states, ec2=None):
    monkeypatch.setattr(actions, "_ct_log_proof", lambda ctx: None)
    monkeypatch.setattr(actions, "_TERMINATE_POLL", 0)
    monkeypatch.setattr(actions.time, "sleep", lambda *_: None)
    ssm = FakeSsm({
        "/openzp/apps/app-one.dev": json.dumps({"app": "app-one.dev"}),
        "/openzp/versions/app-one.dev/abc1234": json.dumps(
            {"asg_name": "openzp-asg-1-abc1234", "ami": "ami-1", "image_arn": "arn:image/1",
             "recipe_arn": "arn:recipe/1", "component_arn": "arn:component/1"}),
        "/openzp/secrets/app-one.dev/abc1234": "secret",
        "/openzp/priority-counter": "1",
    })
    signin = _RecoverSignin()
    clients = {"elbv2": _RecoverElb([{"IsDefault": True}]),
               "autoscaling": _RecoverAsg(groups),
               "ec2": ec2 or _RecoverEc2(instance_states),
               "ssm": ssm, "iam": _RecoverIam(), "signin": signin,
               "imagebuilder": _RecoverIb()}
    return FakeCtx(clients, platform()), ssm, signin


class _RecoverEc2(_FakeEc2Instances):
    def __init__(self, states):
        super().__init__(states)
        self.deregistered, self.snapshots_deleted = [], []

    def describe_images(self, ImageIds):
        return {"Images": [{"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-1"}}]}]}

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)

    def delete_snapshot(self, SnapshotId):
        self.snapshots_deleted.append(SnapshotId)


def test_recover_wipes_apps_versions_secrets_and_unlocks_after_termination(monkeypatch):
    groups = [{"AutoScalingGroupName": "openzp-asg-1-abc1234", "Instances": [{"InstanceId": "i-1"}]}]
    ctx, ssm, signin = _recover_ctx(monkeypatch, groups, {"i-1": ["running", "terminated"]})
    actions.recover(ctx, {})
    assert not [n for n in ssm.params if n.startswith("/openzp/apps/")]
    assert not [n for n in ssm.params if n.startswith("/openzp/versions/")]
    assert not [n for n in ssm.params if n.startswith("/openzp/secrets/")]
    assert "/openzp/priority-counter" in ssm.params
    assert signin.unlocked


def test_recover_deletes_the_bake_artifacts_before_dropping_the_manifests(monkeypatch):
    """The AMI, its snapshots and the Image Builder trio carry a random name token, so
    no prefix sweep reaches them — the version manifest is the only thing that knows
    their arns, and recover deletes that manifest. Miss this and the snapshots bill
    forever with nothing left pointing at them."""
    groups = [{"AutoScalingGroupName": "openzp-asg-1-abc1234", "Instances": [{"InstanceId": "i-1"}]}]
    ctx, ssm, _ = _recover_ctx(monkeypatch, groups, {"i-1": ["running", "terminated"]})
    actions.recover(ctx, {})

    ec2, ib = ctx.client("ec2"), ctx.client("imagebuilder")
    assert ec2.deregistered == ["ami-1"]
    assert ec2.snapshots_deleted == ["snap-1"]
    # order is the dependency order: image built from recipe, recipe uses component
    assert ib.deleted == [("image", "arn:image/1"), ("recipe", "arn:recipe/1"),
                          ("component", "arn:component/1")]
    assert not [n for n in ssm.params if n.startswith("/openzp/versions/")]


def test_recover_unlocks_strictly_after_instances_terminate(monkeypatch):
    groups = [{"AutoScalingGroupName": "openzp-asg-1-abc1234", "Instances": [{"InstanceId": "i-1"}]}]
    holder = {}

    class _GatedEc2(_RecoverEc2):
        def describe_instances(self, Filters):
            assert holder["signin"].unlocked is False
            return super().describe_instances(Filters)

    ec2 = _GatedEc2({"i-1": ["running", "running", "terminated"]})
    ctx, ssm, signin = _recover_ctx(monkeypatch, groups, None, ec2=ec2)
    holder["signin"] = signin
    actions.recover(ctx, {})
    assert signin.unlocked
