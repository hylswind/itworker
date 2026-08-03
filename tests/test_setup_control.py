from dataclasses import dataclass

from openzi_itworker import config
from openzi_itworker.setup import control


@dataclass
class _Cfg:
    repo: str = "owner/openzi-itworker"
    commit: str = "abc1234"
    region: str = "us-east-1"


class FakeEc2:
    def __init__(self, existing_sgs):
        self._sgs = existing_sgs
        self.modified = None
        self.lt_data = None

    def describe_instances(self, InstanceIds):
        return {"Reservations": [{"Instances": [
            {"InstanceId": InstanceIds[0],
             "SecurityGroups": [{"GroupId": g} for g in self._sgs]}]}]}

    def modify_instance_attribute(self, InstanceId, Groups):
        self.modified = Groups

    def create_launch_template(self, **kwargs):
        self.lt_data = kwargs
        return {"LaunchTemplate": {"LaunchTemplateId": "lt-ctrl"}}


class FakeAsg:
    """Enforces the one rule EC2 enforces here: desired capacity must sit within
    [min, max]. A fake that accepted anything is why a MinSize=1/DesiredCapacity=0
    create — rejected by the real API — got as far as a live account."""

    def __init__(self):
        self.created = None
        self.attached = None
        self.min = self.max = self.desired = None

    def create_auto_scaling_group(self, **kwargs):
        self.created = kwargs
        self._set(kwargs["MinSize"], kwargs["MaxSize"], kwargs["DesiredCapacity"])

    def attach_instances(self, InstanceIds, AutoScalingGroupName):
        self.attached = (InstanceIds, AutoScalingGroupName)
        self._set(self.min, self.max, self.desired + len(InstanceIds))

    def update_auto_scaling_group(self, AutoScalingGroupName, **kwargs):
        self._set(kwargs.get("MinSize", self.min), kwargs.get("MaxSize", self.max),
                  kwargs.get("DesiredCapacity", self.desired))

    def _set(self, min_size, max_size, desired):
        if not min_size <= desired <= max_size:
            raise ValueError(f"Desired capacity:{desired} must be between the specified "
                             f"min size:{min_size} and max size:{max_size}")
        self.min, self.max, self.desired = min_size, max_size, desired


def test_wire_control_attaches_sg_lt_asg_and_instance():
    ec2 = FakeEc2(existing_sgs=["sg-default"])
    asg = FakeAsg()
    outputs = {"ControlSgId": "sg-ctrl", "ControlTgArn": "arn:tg/ctrl"}
    control.wire_control(ec2, asg, _Cfg(), outputs, "i-123", ["s-a", "s-b"], lambda *_: None)

    # existing SG preserved + control SG added
    assert set(ec2.modified) == {"sg-default", "sg-ctrl"}

    lt = ec2.lt_data["LaunchTemplateData"]
    assert ec2.lt_data["LaunchTemplateName"] == config.CONTROL_LT_NAME
    assert lt["IamInstanceProfile"]["Name"] == config.ADMIN_PROFILE_NAME
    assert lt["SecurityGroupIds"] == ["sg-ctrl"]
    import base64
    ud = base64.b64decode(lt["UserData"]).decode()
    assert "git checkout abc1234" in ud and "openzi_itworker server" in ud

    assert asg.created["AutoScalingGroupName"] == config.CONTROL_ASG_NAME
    assert asg.created["TargetGroupARNs"] == ["arn:tg/ctrl"]
    # born empty, so no second control instance is launched alongside this one
    assert asg.created["DesiredCapacity"] == 0 and asg.created["MinSize"] == 0
    assert asg.attached == (["i-123"], config.CONTROL_ASG_NAME)
    # ...and left holding exactly this instance, with a floor that makes the daily
    # restart's termination get replaced rather than scale the control plane to zero
    assert (asg.min, asg.desired) == (1, 1)
