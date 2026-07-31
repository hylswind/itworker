"""Deploy the platform CloudFormation stack. The self-built app VPC + app ALB and
the control ALB/target-group/SG (in the default VPC) are all created here; the
control ASG + launch template that follow are wired by control.py, and the API key
is stored separately (CFN can't create a SecureString)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import cfn, config

_Log = Callable[[str], None]

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "cloudformation" / "platform_stack.yaml"


def deploy(cfn_client, cfg, zone_id: str, console_password: str,
           default_vpc: str, default_subnets: list[str], log: _Log = print) -> dict[str, str]:
    params = {
        "Domain": cfg.domain,
        "HostedZoneId": zone_id,
        "ConsolePassword": console_password,
        "DefaultVpcId": default_vpc,
        "DefaultSubnetA": default_subnets[0],
        "DefaultSubnetB": default_subnets[1],
    }
    log(f"  deploying stack {config.PLATFORM_STACK_NAME}")
    return cfn.deploy_stack(cfn_client, config.PLATFORM_STACK_NAME, TEMPLATE_PATH, params, log)
