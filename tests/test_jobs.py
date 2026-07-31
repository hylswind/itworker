import json

from fakes import FakeSsm

from openzi_itworker import config
from openzi_itworker.server import jobs


def test_job_lifecycle():
    ssm = FakeSsm()
    jid = jobs.create(ssm, "deploy", {"app": "x"})
    assert jobs.get(ssm, jid)["status"] == "RUNNING"
    jobs.succeed(ssm, jid, "deploy", {"app": "x"}, {"ok": 1})
    assert jobs.get(ssm, jid)["status"] == "SUCCEEDED"
    assert jobs.get(ssm, "missing") is None


def test_sweep_reaps_running_and_keeps_recent_terminal():
    ssm = FakeSsm()
    running = jobs.create(ssm, "deploy", {})
    done = jobs.create(ssm, "init", {})
    jobs.succeed(ssm, done, "init", {})
    reaped, pruned = jobs.sweep(ssm)
    assert (reaped, pruned) == (1, 0)
    assert jobs.get(ssm, running)["status"] == "FAILED"
    assert jobs.get(ssm, done)["status"] == "SUCCEEDED"
    assert jobs.get(ssm, running)["finished_at"]


def test_sweep_prunes_terminal_jobs_past_ttl(monkeypatch):
    ssm = FakeSsm()
    old = jobs.create(ssm, "deploy", {})
    fresh = jobs.create(ssm, "deploy", {})
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0)
    jobs.fail(ssm, old, "deploy", {}, "boom")
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0 + config.JOB_TTL_SECONDS + 1)
    jobs.succeed(ssm, fresh, "deploy", {})
    reaped, pruned = jobs.sweep(ssm)
    assert (reaped, pruned) == (0, 1)
    assert jobs.get(ssm, old) is None
    assert jobs.get(ssm, fresh)["status"] == "SUCCEEDED"


def test_sweep_prunes_legacy_terminal_without_finished_at():
    ssm = FakeSsm()
    ssm.params[config.JOB_PARAM.format(job_id="legacy")] = json.dumps(
        {"status": config.JOB_SUCCEEDED, "action": "init", "payload": {}})
    reaped, pruned = jobs.sweep(ssm)
    assert (reaped, pruned) == (0, 1)
    assert jobs.get(ssm, "legacy") is None
