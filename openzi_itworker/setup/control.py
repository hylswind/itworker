"""Wire this instance into the control plane so the daily restart can replace it
seamlessly.

The CFN stack created the control ALB, its target group, and a control security
group (ingress :8080 from the control ALB) — all in the default VPC. Here we:
  1. attach that control SG to this running instance (it launched before the SG
     existed, so its health check on :8080 would otherwise be unreachable);
  2. create the control launch template (boots straight into server mode under the
     admin instance profile) and the control ASG;
  3. attach this instance to the ASG, which registers it into the control target
     group and raises desired capacity to 1.
After the daily restart terminates this instance, the ASG relaunches from the LT."""

from __future__ import annotations

import base64
from typing import Callable

from .. import config, userdata

_Log = Callable[[str], None]


def wire_control(ec2, asg, cfg, outputs: dict, instance_id: str,
                 default_subnets: list[str], log: _Log = print) -> None:
    control_sg = outputs["ControlSgId"]
    control_tg = outputs["ControlTgArn"]

    log(f"  attaching control SG {control_sg} to {instance_id}")
    _attach_sg(ec2, instance_id, control_sg)

    log("  creating control launch template + ASG")
    lt_id = _create_control_lt(ec2, cfg, control_sg)
    asg.create_auto_scaling_group(
        AutoScalingGroupName=config.CONTROL_ASG_NAME,
        MinSize=1, MaxSize=2, DesiredCapacity=0,
        HealthCheckType="ELB", HealthCheckGracePeriod=180,
        LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"},
        TargetGroupARNs=[control_tg],
        VPCZoneIdentifier=",".join(default_subnets))

    log(f"  attaching {instance_id} to {config.CONTROL_ASG_NAME}")
    asg.attach_instances(InstanceIds=[instance_id],
                         AutoScalingGroupName=config.CONTROL_ASG_NAME)


def _attach_sg(ec2, instance_id: str, sg_id: str) -> None:
    """Add sg_id to the instance's security groups (ModifyInstanceAttribute replaces
    the whole set, so preserve the existing ones)."""
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    inst = reservations[0]["Instances"][0]
    groups = [g["GroupId"] for g in inst.get("SecurityGroups", [])]
    if sg_id not in groups:
        groups.append(sg_id)
    ec2.modify_instance_attribute(InstanceId=instance_id, Groups=groups)


def _create_control_lt(ec2, cfg, sg_id: str) -> str:
    ud = userdata.build_server_userdata(cfg.repo, cfg.commit, cfg.region)
    resp = ec2.create_launch_template(
        LaunchTemplateName=config.CONTROL_LT_NAME,
        LaunchTemplateData={
            "ImageId": f"resolve:ssm:{config.BASE_AMI_PARAM}",
            "InstanceType": "t3.small",
            "IamInstanceProfile": {"Name": config.ADMIN_PROFILE_NAME},
            "SecurityGroupIds": [sg_id],
            "UserData": base64.b64encode(ud.encode()).decode(),
            "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
        })
    return resp["LaunchTemplate"]["LaunchTemplateId"]
