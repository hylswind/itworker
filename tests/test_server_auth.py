"""The control server's x-api-key gate. We drive the Handler methods directly with
a recording _json, so no sockets are involved."""

import fakes
from fakes import FakeSsm

from openzp_itworker import config
from openzp_itworker.server import jobs
from openzp_itworker.server.server import Handler


def _handler(api_key="secret", supplied="secret", path="/", ssm=None):
    h = Handler.__new__(Handler)
    h.api_key = api_key
    h.headers = {"x-api-key": supplied} if supplied is not None else {}
    h.path = path
    h.ssm = ssm
    h.platform = fakes.platform()
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


def test_console_password_requires_key():
    """The whole point of this route is that it hands out a credential, so the gate
    matters more here than anywhere else."""
    h = _handler(supplied="wrong", path="/console-password",
                 ssm=FakeSsm({config.CONSOLE_PASSWORD_PARAM: "pw"}))
    Handler.do_GET(h)
    assert h.recorded == [(403, {"error": "forbidden"})]


def test_console_password_returns_the_billing_login():
    ssm = FakeSsm({config.CONSOLE_PASSWORD_PARAM: "s3cret-pw"})
    h = _handler(path="/console-password", ssm=ssm)
    Handler.do_GET(h)
    code, obj = h.recorded[0]
    assert code == 200
    assert obj["password"] == "s3cret-pw"
    assert obj["user"] == config.BILLING_CONSOLE_USER
    # the operator has no console URL of their own once root is gone
    assert obj["signin_url"] == "https://123456789012.signin.aws.amazon.com/console"


def test_console_password_404_when_never_stored():
    h = _handler(path="/console-password", ssm=FakeSsm())
    Handler.do_GET(h)
    assert h.recorded[0][0] == 404
