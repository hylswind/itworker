"""The setup orchestrator. Creates boto3 clients from a session (the instance
profile), runs the bring-up in order, and always writes a result marker — success
or, on any exception, failure — before re-raising. On success it execs into server
mode. Sub-steps take explicit clients so they can be unit-tested with fakes."""

from __future__ import annotations

from typing import Callable

from .. import util
from . import apikey, contacts, control, marker, network, platform, registrar, wait

_Log = Callable[[str], None]


def run(cfg, session, log: _Log = print, *, serve=None) -> None:
    region = cfg.region
    ssm = session.client("ssm", region_name=region)
    try:
        log(f"  waiting until end+1s ({cfg.end_epoch + 1})")
        wait.until(cfg.end_epoch + 1)

        ec2 = session.client("ec2", region_name=region)
        r53d = session.client("route53domains", region_name=region)
        r53 = session.client("route53", region_name=region)
        cfn_client = session.client("cloudformation", region_name=region)
        asg = session.client("autoscaling", region_name=region)

        if cfg.skip_domain:
            registrar.ensure_owned_and_clean(r53d, r53, cfg.domain, log)
        else:
            registrar.register(r53d, cfg.contact, cfg.domain, log)
        zone_id = registrar.hosted_zone_id(r53, cfg.domain)

        vpc = network.default_vpc(ec2)
        subnets = network.two_subnets(ec2, vpc)
        console_password = util.generate_password()
        outputs = platform.deploy(cfn_client, cfg, zone_id, console_password, vpc, subnets, log)

        instance_id = network.current_instance_id()
        control.wire_control(ec2, asg, cfg, outputs, instance_id, subnets, log)

        apikey.store(ssm, cfg.api_key, cfg.commit, cfg.repo, console_password)
        marker.write_success(ssm)
        log("  setup complete")
    except Exception as exc:  # noqa: BLE001 — always signal the workflow, then re-raise
        log(f"  setup FAILED: {exc}")
        try:
            marker.write_failure(ssm, exc)
        except Exception as mexc:  # noqa: BLE001 — best effort; workflow falls back to its timeout
            log(f"  could not write failure marker: {mexc}")
        raise

    # success -> become the server (replaces this process)
    if serve is None:
        from ..server.server import main as serve
    serve()
