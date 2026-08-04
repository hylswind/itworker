"""Hand-written boto3 fakes shared across the offline tests."""

from __future__ import annotations

import json


class FakeParamNotFound(Exception):
    pass


class FakeParamExists(Exception):
    pass


class FakeSsm:
    class exceptions:
        ParameterNotFound = FakeParamNotFound
        ParameterAlreadyExists = FakeParamExists

    def __init__(self, initial=None):
        self.params = dict(initial or {})
        self.puts = []  # (Name, Value, Type) history

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.params:
            raise FakeParamNotFound(Name)
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, Name, Value, Type="String", Overwrite=True):
        if not Overwrite and Name in self.params:
            raise FakeParamExists(Name)
        self.params[Name] = Value
        self.puts.append((Name, Value, Type))

    def delete_parameter(self, Name):
        self.params.pop(Name, None)

    def delete_parameters(self, Names):
        for n in Names:
            self.params.pop(n, None)

    def get_paginator(self, op):
        params = self.params

        class _P:
            def paginate(self, Path, Recursive=False):
                items = [{"Name": n, "Value": v} for n, v in params.items() if n.startswith(Path)]
                return [{"Parameters": items}]

        return _P()


class FakePriorityInUse(Exception):
    pass


class FakeElb:
    class exceptions:
        PriorityInUseException = FakePriorityInUse

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.created = []

    def describe_rules(self, ListenerArn):
        return {"Rules": self.rules}

    def create_rule(self, ListenerArn, Priority, Conditions, Actions):
        self.created.append({"Priority": Priority, "Conditions": Conditions, "Actions": Actions})
        return {"Rules": [{"RuleArn": f"arn:rule/{Priority}"}]}


class FakeKms:
    def generate_random(self, NumberOfBytes):
        assert NumberOfBytes == 64
        return {"Plaintext": b"\x00" * 64}


class FakeIam:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(**kw):
            self.calls.append((name, kw))
            return {}
        return rec


class FakeCtx:
    def __init__(self, clients, platform):
        self._clients = clients
        self.platform = platform

    def client(self, service):
        return self._clients[service]


def platform():
    from openzp_itworker.context import Platform
    return Platform(region="us-east-1", account_id="123456789012", vpc="vpc-1",
                    subnet_a="s-a", subnet_b="s-b", app_listener_arn="arn:listener",
                    instance_sg_id="sg-1", ib_infra_arn="arn:infra", ib_dist_arn="arn:dist",
                    domain="example.com", hosted_zone_id="Z1")
