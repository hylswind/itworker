"""The setup orchestrator always writes a result marker — success on the happy
path, failure (a DIFFERENT parameter name) on any exception — before proceeding or
re-raising."""

import pytest
from fakes import FakeSsm

from openzp_itworker import config
from openzp_itworker.setup import runner
from openzp_itworker.setup.config import SetupConfig


def _cfg():
    return SetupConfig(domain="example.com", end_epoch=0, api_key="k", repo="o/r",
                       commit="abc1234", contact={}, skip_domain=True, region="us-east-1")


class FakeSession:
    def __init__(self, ssm):
        self.ssm = ssm

    def client(self, name, region_name=None):
        return self.ssm if name == "ssm" else object()


def _patch_happy(monkeypatch):
    monkeypatch.setattr(runner.registrar, "ensure_owned_and_clean", lambda *a, **k: None)
    monkeypatch.setattr(runner.registrar, "register", lambda *a, **k: None)
    monkeypatch.setattr(runner.registrar, "hosted_zone_id", lambda *a, **k: "Z1")
    monkeypatch.setattr(runner.network, "default_vpc", lambda ec2: "vpc-def")
    monkeypatch.setattr(runner.network, "two_subnets", lambda ec2, vpc: ["s-a", "s-b"])
    monkeypatch.setattr(runner.network, "current_instance_id", lambda: "i-123")
    monkeypatch.setattr(runner.control, "wire_control", lambda *a, **k: None)


def test_runner_writes_success_marker_and_serves(monkeypatch):
    ssm = FakeSsm()
    _patch_happy(monkeypatch)
    monkeypatch.setattr(runner.platform, "deploy",
                        lambda *a, **k: {"ControlSgId": "sg", "ControlTgArn": "tg"})
    served = []
    runner.run(_cfg(), FakeSession(ssm), log=lambda *_: None, serve=lambda: served.append(True))
    assert config.SETUP_OK_PARAM in ssm.params
    assert config.SETUP_FAILED_PARAM not in ssm.params
    assert served == [True]


def test_runner_writes_failure_marker_on_error_and_does_not_serve(monkeypatch):
    ssm = FakeSsm()
    _patch_happy(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("stack blew up")

    monkeypatch.setattr(runner.platform, "deploy", boom)
    served = []
    with pytest.raises(RuntimeError, match="stack blew up"):
        runner.run(_cfg(), FakeSession(ssm), log=lambda *_: None, serve=lambda: served.append(True))
    assert config.SETUP_FAILED_PARAM in ssm.params
    assert config.SETUP_OK_PARAM not in ssm.params
    assert served == []
