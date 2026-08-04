"""Default-VPC discovery + this instance's own id.

Everything the control plane owns lives in the DEFAULT VPC: the workflow launched
this instance there before any infrastructure existed, and an ALB can only target
instances in its own VPC — so the control ALB and this instance must share it."""

from __future__ import annotations

import urllib.request

_IMDS = "http://169.254.169.254"


class NetworkError(Exception):
    pass


def default_vpc(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}]).get("Vpcs", [])
    if not vpcs:
        raise NetworkError("no default VPC in this account/region")
    return vpcs[0]["VpcId"]


def two_subnets(ec2, vpc_id: str) -> list[str]:
    """Two default subnets in distinct AZs — an ALB needs at least two AZs."""
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "default-for-az", "Values": ["true"]}]).get("Subnets", [])
    by_az: dict[str, str] = {}
    for sn in subnets:
        by_az.setdefault(sn["AvailabilityZone"], sn["SubnetId"])
    if len(by_az) < 2:
        raise NetworkError("default VPC has fewer than two default subnets")
    return [by_az[az] for az in sorted(by_az)][:2]


def current_instance_id(*, opener=urllib.request.urlopen) -> str:
    """This instance's id via IMDSv2 (token then metadata)."""
    tok_req = urllib.request.Request(
        f"{_IMDS}/latest/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    token = opener(tok_req, timeout=5).read().decode()
    id_req = urllib.request.Request(
        f"{_IMDS}/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token})
    return opener(id_req, timeout=5).read().decode()
