"""Async job tracking in SSM. Every action runs in a background thread; its status
lives in /openzp/jobs/{id} so it survives the daily instance restart — a client can
still poll a job whose worker was killed by the restart (it reads FAILED). Terminal
records carry a `finished_at` epoch so the boot-time sweep can prune old ones."""

from __future__ import annotations

import json
import time
import uuid

from .. import config

_TERMINAL = (config.JOB_SUCCEEDED, config.JOB_FAILED)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def create(ssm, action: str, payload: dict) -> str:
    job_id = new_id()
    _put(ssm, job_id, {"status": config.JOB_RUNNING, "action": action, "payload": payload})
    return job_id


def succeed(ssm, job_id: str, action: str, payload: dict, result: dict | None = None) -> None:
    _put(ssm, job_id, {"status": config.JOB_SUCCEEDED, "action": action,
                       "payload": payload, "result": result or {}, "finished_at": int(time.time())})


def fail(ssm, job_id: str, action: str, payload: dict, error: str) -> None:
    _put(ssm, job_id, {"status": config.JOB_FAILED, "action": action,
                       "payload": payload, "error": error, "finished_at": int(time.time())})


def get(ssm, job_id: str) -> dict | None:
    try:
        raw = ssm.get_parameter(Name=config.JOB_PARAM.format(job_id=job_id))["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    return json.loads(raw)


def sweep(ssm) -> tuple[int, int]:
    """Boot-time housekeeping over /openzp/jobs/*, one pass:
      - reap: a still-RUNNING job's worker died with the previous instance -> FAILED
        (with a fresh finished_at, so a client polling after the restart still sees
        a terminal state and it survives the TTL);
      - prune: delete terminal jobs finished more than JOB_TTL_SECONDS ago (and any
        legacy terminal record with no finished_at at all).
    Returns (reaped, pruned)."""
    reaped = pruned = 0
    cutoff = int(time.time()) - config.JOB_TTL_SECONDS
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path="/openzp/jobs/", Recursive=False):
        for param in page.get("Parameters", []):
            doc = json.loads(param["Value"])
            if doc.get("status") == config.JOB_RUNNING:
                doc["status"] = config.JOB_FAILED
                doc["error"] = "interrupted by control-plane restart"
                doc["finished_at"] = int(time.time())
                ssm.put_parameter(Name=param["Name"], Value=json.dumps(doc),
                                  Type="String", Overwrite=True)
                reaped += 1
            elif doc.get("status") in _TERMINAL and doc.get("finished_at", 0) < cutoff:
                ssm.delete_parameter(Name=param["Name"])
                pruned += 1
    return reaped, pruned


def _put(ssm, job_id: str, doc: dict) -> None:
    ssm.put_parameter(Name=config.JOB_PARAM.format(job_id=job_id),
                      Value=json.dumps(doc), Type="String", Overwrite=True)
