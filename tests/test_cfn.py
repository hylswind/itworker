"""Validate the platform template with cfn-lint and guard its content: the dropped
control channel (API Gateway / VPC link / control-AMI bake) must be gone, and the
new external control ALB + admin cert/routing must be present."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "cloudformation" / "platform_stack.yaml"


@pytest.fixture(scope="module")
def text():
    return TEMPLATE.read_text()


def test_cfn_lint_reports_no_errors():
    proc = subprocess.run([sys.executable, "-m", "cfnlint", "--format", "json", str(TEMPLATE)],
                          capture_output=True, text=True)
    findings = json.loads(proc.stdout or "[]")
    errors = [f for f in findings if f.get("Level") == "Error"]
    assert not errors, "\n".join(f"{f['Rule']['Id']} {f['Message']}" for f in errors)


def test_dropped_control_channel_is_gone(text):
    for banned in ("AWS::ApiGateway", "VpcLink", "BundleBucket",
                   "resolve:ssm:/openzp/ctrl-ami", "CtrlPlaceholderAmi"):
        assert banned not in text, f"{banned} should have been removed"


def test_external_control_alb_and_admin_routing_present(text):
    assert "openzp-ctrl-alb" in text
    assert text.count("Scheme: internet-facing") >= 2   # app ALB + control ALB
    assert "admin.${Domain}" in text                     # SAN + alias + host
    assert "SubjectAlternativeNames" in text
    assert "ControlTg" in text and "ControlInstanceSg" in text


def test_billing_user_scoped(text):
    assert "UserName: console" in text
    assert "arn:aws:iam::aws:policy/job-function/Billing" in text
    assert "arn:aws:iam::aws:policy/AWSSupportAccess" in text
    assert "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess" in text


def test_daily_restart_wiring(text):
    assert "Asia/Taipei" in text
    assert "openzp-restart" in text
    assert '"AutoScalingGroupNames": ["openzp-control"]' in text


def test_outputs_for_setup(text):
    assert "ControlTgArn" in text and "ControlSgId" in text
