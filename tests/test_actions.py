import base64
import json

import pytest
from fakes import FakeCtx, FakeElb, FakeIam, FakeKms, FakeSsm, platform

from openzi_itworker import config
from openzi_itworker.server import actions, github


# ---------- init ----------

def test_init_binds_and_publishes(monkeypatch):
    monkeypatch.setattr(github, "resolve",
                        lambda repo: {"owner_id": 7, "repo_id": 42, "full_name": repo})
    ssm = FakeSsm({config.PRIORITY_COUNTER: "0"})
    elb = FakeElb()
    ctx = FakeCtx({"ssm": ssm, "elbv2": elb}, platform())
    out = actions.init(ctx, {"app": "demo.dev", "repo": "o/r"})
    assert out["priority"] == 1
    record = json.loads(ssm.params["/openzi/apps/demo.dev"])
    assert record["repo_id"] == 42 and record["owner_id"] == 7
    body = elb.created[0]["Actions"][0]["FixedResponseConfig"]["MessageBody"]
    assert json.loads(body)["app"] == "demo.dev"


def test_init_rejects_reinit_of_complete_app(monkeypatch):
    monkeypatch.setattr(github, "resolve",
                        lambda repo: {"owner_id": 7, "repo_id": 42, "full_name": repo})
    existing = json.dumps({"app": "demo.dev", "owner_id": 7, "repo_id": 42, "repo_at_init": "o/r"})
    ssm = FakeSsm({"/openzi/apps/demo.dev": existing, config.PRIORITY_COUNTER: "3"})
    elb = FakeElb(rules=[{"IsDefault": False,
                          "Conditions": [{"PathPatternConfig": {"Values": ["/demo.dev/info.json"]}}]}])
    ctx = FakeCtx({"ssm": ssm, "elbv2": elb}, platform())
    with pytest.raises(actions.ActionError, match="AppExists"):
        actions.init(ctx, {"app": "demo.dev", "repo": "o/r"})


def test_init_resumes_partial(monkeypatch):
    monkeypatch.setattr(github, "resolve",
                        lambda repo: {"owner_id": 7, "repo_id": 42, "full_name": repo})
    existing = json.dumps({"app": "demo.dev", "owner_id": 7, "repo_id": 42, "repo_at_init": "o/r"})
    ssm = FakeSsm({"/openzi/apps/demo.dev": existing, config.PRIORITY_COUNTER: "3"})
    elb = FakeElb(rules=[])
    ctx = FakeCtx({"ssm": ssm, "elbv2": elb}, platform())
    out = actions.init(ctx, {"app": "demo.dev", "repo": "o/r"})
    assert out["priority"] == 4 and elb.created


# ---------- deploy / delete guards ----------

def test_deploy_requires_initialized_app():
    ctx = FakeCtx({"ssm": FakeSsm()}, platform())
    with pytest.raises(actions.ActionError, match="AppNotFound"):
        actions.deploy(ctx, {"app": "x", "commit": "abc1234"})


def test_deploy_rejects_duplicate_version():
    ssm = FakeSsm({"/openzi/apps/x": json.dumps({"owner_id": 1, "repo_id": 2}),
                   "/openzi/versions/x/abc1234": "{}"})
    ctx = FakeCtx({"ssm": ssm}, platform())
    with pytest.raises(actions.ActionError, match="AlreadyDeployed"):
        actions.deploy(ctx, {"app": "x", "commit": "abc1234"})


def test_delete_missing_version():
    ctx = FakeCtx({"ssm": FakeSsm()}, platform())
    with pytest.raises(actions.ActionError, match="VersionNotFound"):
        actions.delete(ctx, {"app": "x", "commit": "abc1234"})


# ---------- launch template / secret / role ----------

class FakeEc2Lt:
    def __init__(self):
        self.lt_kwargs = None

    def create_launch_template(self, **kwargs):
        self.lt_kwargs = kwargs
        return {"LaunchTemplate": {"LaunchTemplateId": "lt-1"}}


def test_app_launch_template_blocks_container_imds_and_uses_version_profile():
    ec2 = FakeEc2Lt()
    ctx = FakeCtx({"ec2": ec2}, platform())
    actions._create_lt(ctx, priority=1, commit="abc1234", ami_id="ami-1",
                       profile_arn="arn:aws:iam::123:instance-profile/openzi-secret-1-abc1234")
    md = ec2.lt_kwargs["LaunchTemplateData"]["MetadataOptions"]
    assert md["HttpPutResponseHopLimit"] == 1 and md["HttpTokens"] == "required"
    assert ec2.lt_kwargs["LaunchTemplateData"]["IamInstanceProfile"]["Arn"].endswith("openzi-secret-1-abc1234")


def test_put_secret_stores_securestring():
    ssm = FakeSsm()
    ctx = FakeCtx({"kms": FakeKms(), "ssm": ssm}, platform())
    actions._put_secret(ctx, "demo.dev", "abc1234")
    stored = ssm.params["/openzi/secrets/demo.dev/abc1234"]
    assert base64.b64decode(stored) == b"\x00" * 64


def test_version_role_scoped_to_its_own_secret():
    iam = FakeIam()
    ctx = FakeCtx({"iam": iam}, platform())
    profile_arn, name = actions._create_version_role(ctx, "demo.dev", 3, "abc1234")
    assert name == "openzi-secret-3-abc1234"
    assert profile_arn.endswith("instance-profile/openzi-secret-3-abc1234")
    policy = next(kw["PolicyDocument"] for n, kw in iam.calls if n == "put_role_policy")
    assert "parameter/openzi/secrets/demo.dev/abc1234" in policy
    assert '"kms:ViaService": "ssm.us-east-1.amazonaws.com"' in policy


# ---------- bake script ----------

def test_version_id_trims_full_sha_to_seven():
    assert actions._version_id("f0b6369afbd673ba81dd89f33a7749e0a78de338") == "f0b6369"
    assert actions._version_id("f0b6369") == "f0b6369"


def test_bake_component_has_dockerfile_contract():
    data = actions._bake_component_data(repo_id=42, owner_id=7, app="demo.dev", commit="abc1234")
    assert "https://api.github.com/repositories/42" in data
    assert '".owner.id == 7"' in data
    assert "docker build -t app:abc1234" in data
    assert "-p 80:8080 -p 8081:8081" in data
    assert config.SECRET_PARAM.format(app="demo.dev", commit="abc1234") in data
    assert "OPENZI_VERSION_SECRET" in data
