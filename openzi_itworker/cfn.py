"""CloudFormation deploy + wait helpers. Create-or-update a stack from a template
file, stream its events to a log callback, and return its outputs. Idempotent;
a stack stranded in an unrecoverable state is deleted and recreated."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import config

_Log = Callable[[str], None]

_TERMINAL_OK = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_TERMINAL_FAIL = {
    "CREATE_FAILED", "ROLLBACK_IN_PROGRESS", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED", "DELETE_FAILED",
}
# A stack left in one of these can't be updated — only deleted and recreated.
_UNRECOVERABLE = {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "REVIEW_IN_PROGRESS", "CREATE_FAILED"}


class StackError(Exception):
    pass


def deploy_stack(
    cfn,
    stack_name: str,
    template_path: str | Path,
    parameters: dict[str, str],
    log: _Log,
    capabilities: tuple[str, ...] = ("CAPABILITY_NAMED_IAM",),
) -> dict[str, str]:
    body = Path(template_path).read_text()
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()]
    caps = list(capabilities)

    status = _stack_status(cfn, stack_name)
    if status in _UNRECOVERABLE:
        log(f"  deleting unrecoverable stack {stack_name} ({status})")
        cfn.delete_stack(StackName=stack_name)
        _wait_deleted(cfn, stack_name)
        status = None

    if status is None:
        cfn.create_stack(StackName=stack_name, TemplateBody=body, Parameters=params, Capabilities=caps)
    else:
        try:
            cfn.update_stack(StackName=stack_name, TemplateBody=body, Parameters=params, Capabilities=caps)
        except cfn.exceptions.ClientError as exc:
            if "No updates are to be performed" in str(exc):
                log(f"  stack {stack_name} already up to date")
                return stack_outputs(cfn, stack_name)
            raise

    wait_for_stack(cfn, stack_name, log)
    return stack_outputs(cfn, stack_name)


def _stack_status(cfn, stack_name: str) -> str | None:
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def _wait_deleted(cfn, stack_name: str) -> None:
    deadline = time.monotonic() + config.CFN_CREATE_TIMEOUT
    while _stack_status(cfn, stack_name) is not None:
        if time.monotonic() >= deadline:
            raise StackError(f"stack {stack_name} delete timed out")
        time.sleep(config.CFN_POLL_INTERVAL)


def wait_for_stack(cfn, stack_name: str, log: _Log) -> None:
    seen: set[str] = set()
    deadline = time.monotonic() + config.CFN_CREATE_TIMEOUT
    while True:
        for e in reversed(cfn.describe_stack_events(StackName=stack_name)["StackEvents"]):
            if e["EventId"] in seen:
                continue
            seen.add(e["EventId"])
            _log_event(log, e)
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        if status in _TERMINAL_OK:
            return
        if status in _TERMINAL_FAIL:
            raise StackError(f"stack {stack_name} failed: {status}")
        if time.monotonic() >= deadline:
            raise StackError(f"stack {stack_name} timed out (status={status})")
        time.sleep(config.CFN_POLL_INTERVAL)


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _log_event(log: _Log, e: dict) -> None:
    status = e["ResourceStatus"]
    reason = e.get("ResourceStatusReason", "")
    if status == "CREATE_IN_PROGRESS" and not reason:
        return
    rtype = e["ResourceType"].removeprefix("AWS::")
    line = f"    {status:<22} {e['LogicalResourceId']} ({rtype})"
    if reason and reason != "Resource creation Initiated":
        line += f" — {reason}"
    log(line)
