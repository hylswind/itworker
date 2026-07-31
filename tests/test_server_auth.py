"""The control server's x-api-key gate. We drive the Handler methods directly with
a recording _json, so no sockets are involved."""

from fakes import FakeSsm

from openzi_itworker.server import jobs
from openzi_itworker.server.server import Handler


def _handler(api_key="secret", supplied="secret", path="/", ssm=None):
    h = Handler.__new__(Handler)
    h.api_key = api_key
    h.headers = {"x-api-key": supplied} if supplied is not None else {}
    h.path = path
    h.ssm = ssm
    h.recorded = []
    h._json = lambda code, obj: h.recorded.append((code, obj))
    return h


def test_authed_true_only_on_exact_match():
    assert _handler(api_key="k", supplied="k")._authed() is True
    assert _handler(api_key="k", supplied="nope")._authed() is False
    assert _handler(api_key="k", supplied=None)._authed() is False
    assert _handler(api_key=None, supplied="k")._authed() is False  # key not loaded


def test_health_check_needs_no_auth():
    h = _handler(supplied=None, path="/")
    Handler.do_GET(h)
    assert h.recorded == [(200, {"ok": True})]


def test_status_requires_key():
    h = _handler(supplied="wrong", path="/status?id=x")
    Handler.do_GET(h)
    assert h.recorded[0][0] == 403


def test_status_with_key_reads_job():
    ssm = FakeSsm()
    jid = jobs.create(ssm, "init", {"app": "a"})
    h = _handler(supplied="secret", path=f"/status?id={jid}", ssm=ssm)
    Handler.do_GET(h)
    code, obj = h.recorded[0]
    assert code == 200 and obj["status"] == "RUNNING"


def test_post_requires_key():
    h = _handler(supplied="wrong", path="/deploy")
    Handler.do_POST(h)
    assert h.recorded[0][0] == 403
