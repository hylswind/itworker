"""Domain acquisition.

Normal path: RegisterDomain (async; poll the operation to SUCCESSFUL). Registration
auto-creates the public hosted zone.

Skip path (test reruns, to avoid the ~$3 purchase): require the account to already
own the domain, then strip the hosted zone back to just the apex NS/SOA so the
platform stack deploys from a clean slate (old ACM validation records, alias
records, etc. from a prior run are removed)."""

from __future__ import annotations

import time
from typing import Callable

from .. import config
from . import contacts

_Log = Callable[[str], None]

_APEX_KEEP = {"NS", "SOA"}


class RegistrarError(Exception):
    pass


def register(r53d, contact: dict, domain: str, log: _Log = print,
             *, sleep=time.sleep, now=time.monotonic) -> None:
    detail = contacts.build_contact(contact)
    log(f"  registering {domain}")
    op = r53d.register_domain(
        DomainName=domain, DurationInYears=config.DOMAIN_REGISTER_YEARS, AutoRenew=False,
        AdminContact=detail, RegistrantContact=detail, TechContact=detail)
    _wait_operation(r53d, op["OperationId"], log, sleep=sleep, now=now)
    log(f"  {domain} registered")


def ensure_owned_and_clean(r53d, r53, domain: str, log: _Log = print) -> None:
    if not _owns(r53d, domain):
        raise RegistrarError(
            f"skip_domain set but the account does not own {domain} — register it first")
    log(f"  {domain} already owned; cleaning hosted zone")
    zone_id = hosted_zone_id(r53, domain)
    _clean_zone(r53, zone_id, domain)


def hosted_zone_id(r53, domain: str) -> str:
    """The account's PUBLIC hosted zone for the apex domain (the one RegisterDomain
    created)."""
    resp = r53.list_hosted_zones_by_name(DNSName=domain)
    want = domain.rstrip(".") + "."
    for zone in resp.get("HostedZones", []):
        if zone["Name"] == want and not zone.get("Config", {}).get("PrivateZone", False):
            return zone["Id"].split("/")[-1]
    raise RegistrarError(f"no public hosted zone for {domain}")


def _owns(r53d, domain: str) -> bool:
    want = domain.rstrip(".").lower()
    token = None
    while True:
        resp = r53d.list_domains(**({"Marker": token} if token else {}))
        for d in resp.get("Domains", []):
            if d["DomainName"].rstrip(".").lower() == want:
                return True
        token = resp.get("NextPageMarker")
        if not token:
            return False


def _clean_zone(r53, zone_id: str, domain: str) -> None:
    """Delete every record set except the apex NS and SOA."""
    apex = domain.rstrip(".") + "."
    changes = []
    for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id):
        for rr in page.get("ResourceRecordSets", []):
            if rr["Name"] == apex and rr["Type"] in _APEX_KEEP:
                continue
            changes.append({"Action": "DELETE", "ResourceRecordSet": rr})
    if changes:
        r53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": changes})


def _wait_operation(r53d, op_id: str, log: _Log, *, sleep, now) -> None:
    deadline = now() + config.DOMAIN_OP_TIMEOUT
    while True:
        detail = r53d.get_operation_detail(OperationId=op_id)
        status = detail["Status"]
        if status == "SUCCESSFUL":
            return
        if status in ("ERROR", "FAILED"):
            raise RegistrarError(f"domain operation {status}: {detail.get('Message')}")
        if now() >= deadline:
            raise RegistrarError(f"domain operation timed out (status={status})")
        sleep(config.DOMAIN_OP_POLL_INTERVAL)
