import pytest

from openzi_itworker.setup import contacts, registrar

_CONTACT = {
    "FirstName": "Ada", "LastName": "Lovelace", "AddressLine1": "1 Analytical Way",
    "City": "London", "CountryCode": "GB", "ZipCode": "EC1", "PhoneNumber": "+44.2071234567",
    "Email": "ada@example.com",
}


# ---------- contacts ----------

def test_build_contact_ok_defaults_person():
    detail = contacts.build_contact(_CONTACT)
    assert detail["ContactType"] == "PERSON"
    assert detail["Email"] == "ada@example.com"


def test_build_contact_passes_optional_fields():
    c = dict(_CONTACT, State="London", OrganizationName="Openzi", ContactType="COMPANY")
    detail = contacts.build_contact(c)
    assert detail["OrganizationName"] == "Openzi" and detail["ContactType"] == "COMPANY"


def test_build_contact_missing_field_raises():
    c = dict(_CONTACT)
    del c["Email"]
    with pytest.raises(contacts.ContactError, match="Email"):
        contacts.build_contact(c)


# ---------- registrar: register ----------

class FakeR53Domains:
    def __init__(self, statuses, owned=None):
        self._statuses = list(statuses)
        self._owned = owned or []
        self.register_kwargs = None

    def register_domain(self, **kwargs):
        self.register_kwargs = kwargs
        return {"OperationId": "op-1"}

    def get_operation_detail(self, OperationId):
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return {"Status": status, "Message": "m"}

    def list_domains(self, **kwargs):
        return {"Domains": [{"DomainName": d} for d in self._owned]}


def test_register_polls_to_success():
    r53d = FakeR53Domains(["IN_PROGRESS", "SUCCESSFUL"])
    registrar.register(r53d, _CONTACT, "example.com", lambda *_: None,
                       sleep=lambda *_: None, now=lambda: 0)
    assert r53d.register_kwargs["DomainName"] == "example.com"
    # all three contacts populated
    for role in ("AdminContact", "RegistrantContact", "TechContact"):
        assert r53d.register_kwargs[role]["Email"] == "ada@example.com"


def test_register_raises_on_failed_operation():
    r53d = FakeR53Domains(["FAILED"])
    with pytest.raises(registrar.RegistrarError):
        registrar.register(r53d, _CONTACT, "example.com", lambda *_: None,
                           sleep=lambda *_: None, now=lambda: 0)


# ---------- registrar: skip path ----------

class FakeR53:
    def __init__(self, records):
        self._records = records
        self.changes = None

    def list_hosted_zones_by_name(self, DNSName):
        return {"HostedZones": [{"Name": "example.com.", "Id": "/hostedzone/Z1",
                                 "Config": {"PrivateZone": False}}]}

    def get_paginator(self, op):
        recs = self._records

        class _P:
            def paginate(self, HostedZoneId):
                return [{"ResourceRecordSets": recs}]

        return _P()

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        self.changes = ChangeBatch


def test_hosted_zone_id_finds_public_zone():
    assert registrar.hosted_zone_id(FakeR53([]), "example.com") == "Z1"


def test_ensure_owned_and_clean_strips_non_apex(monkeypatch):
    records = [
        {"Name": "example.com.", "Type": "NS", "ResourceRecords": [{"Value": "ns"}]},
        {"Name": "example.com.", "Type": "SOA", "ResourceRecords": [{"Value": "soa"}]},
        {"Name": "example.com.", "Type": "A", "AliasTarget": {"DNSName": "x"}},
        {"Name": "_acme.example.com.", "Type": "CNAME", "ResourceRecords": [{"Value": "v"}]},
    ]
    r53 = FakeR53(records)
    r53d = FakeR53Domains(["SUCCESSFUL"], owned=["example.com"])
    registrar.ensure_owned_and_clean(r53d, r53, "example.com", lambda *_: None)
    deleted = {c["ResourceRecordSet"]["Type"] for c in r53.changes["Changes"]}
    assert deleted == {"A", "CNAME"}  # NS + SOA kept
    assert all(c["Action"] == "DELETE" for c in r53.changes["Changes"])


def test_ensure_owned_and_clean_rejects_unowned():
    r53d = FakeR53Domains(["SUCCESSFUL"], owned=[])
    with pytest.raises(registrar.RegistrarError, match="does not own"):
        registrar.ensure_owned_and_clean(r53d, FakeR53([]), "example.com", lambda *_: None)
