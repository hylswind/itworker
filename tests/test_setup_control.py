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
    def __init__(self):
        self.created = None
        self.attached = None

    def create_auto_scaling_group(self, **kwargs):
        self.created = kwargs

    def attach_instances(self, InstanceIds, AutoScalingGroupName):
        self.attached = (InstanceIds, AutoScalingGroupName)


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
    assert asg.created["DesiredCapacity"] == 0  # attach raises it to 1
    assert asg.attached == (["i-123"], config.CONTROL_ASG_NAME)
