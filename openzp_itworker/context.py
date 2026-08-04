"""Runtime context for the control-plane server: boto3 clients + the platform
facts the actions need (VPC, subnets, the app ALB listener, Image Builder config
arns, domain, hosted zone). CloudFormation writes these once to
/openzp/platform-config; the server loads them at startup so this code stays
account-agnostic."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields

from . import config


@dataclass
class Platform:
    region: str
    account_id: str
    vpc: str
    subnet_a: str
    subnet_b: str
    app_listener_arn: str
    instance_sg_id: str
    ib_infra_arn: str
    ib_dist_arn: str
    domain: str
    hosted_zone_id: str


class Ctx:
    """Lazily-created boto3 clients keyed by service, plus the loaded platform
    facts. One Ctx per action run."""

    def __init__(self, session, platform: Platform):
        self._session = session
        self._clients: dict = {}
        self.platform = platform

    def client(self, service: str):
        if service not in self._clients:
            self._clients[service] = self._session.client(service, region_name=self.platform.region)
        return self._clients[service]

    @classmethod
    def load_platform(cls, ssm) -> "Platform":
        cfg = json.loads(ssm.get_parameter(Name=config.PLATFORM_CONFIG_PARAM)["Parameter"]["Value"])
        return Platform(**{f.name: cfg[f.name] for f in fields(Platform)})
