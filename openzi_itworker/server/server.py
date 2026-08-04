"""The control-plane HTTP server. Reached over the internet through the control
ALB (admin.{domain} -> control TG -> this instance :8080), so every route except
the ALB health check requires the bearer key in the `x-api-key` header. Each write
action runs in a background thread with its status in SSM; the client polls
GET /status?id=. GET /console-password returns the billing user's login — the only
way to reach it once the account has no root key and no console.

On startup it loads the API key from SSM (SecureString) and sweeps /openzi/jobs/*:
orphaned RUNNING jobs (workers killed by the daily restart) become FAILED, and
terminal records past their TTL are pruned."""

from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import config
from ..context import Ctx
from . import actions, jobs

BIND = ("0.0.0.0", config.CONTROL_PORT)
_WRITE_ACTIONS = ("init", "deploy", "delete", "recover")


def _session_and_region():
    import boto3

    sess = boto3.Session()
    region = sess.region_name or os.environ.get("AWS_REGION") or config.REGION
    return sess, region


def _load_api_key(ssm) -> str:
    return ssm.get_parameter(Name=config.API_KEY_PARAM, WithDecryption=True)["Parameter"]["Value"]


class Handler(BaseHTTPRequestHandler):
    # set once in main(): boto3 clients are thread-safe, and platform-config +
    # the API key are immutable for the instance's life.
    session = None
    ssm = None
    platform = None
    api_key = None

    def _authed(self) -> bool:
        supplied = self.headers.get("x-api-key") or ""
        return bool(self.api_key) and hmac.compare_digest(supplied, self.api_key)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._json(200, {"ok": True})  # ALB health check — unauthenticated
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        if parsed.path == "/status":
            ids = parse_qs(parsed.query).get("id", [])
            if not ids:
                return self._json(400, {"error": "missing id"})
            doc = jobs.get(self.ssm, ids[0])
            if doc is None:
                return self._json(404, {"error": "unknown job"})
            return self._json(200, {"status": doc["status"], **doc})
        if parsed.path == "/console-password":
            return self._console_password()
        return self._json(404, {"error": "not found"})

    def _console_password(self):
        """Hand back the billing user's login, which is otherwise unreachable.

        Once the workflow has deleted the root key and sealed console sign-in, the
        operator holds exactly one credential: this API key. The billing user is
        deliberately exempt from the lockout so bills can still be paid — but its
        password lives in an SSM SecureString, and reading SSM needs the AWS access
        the operator no longer has. Without this route the only way back in is
        `recover`, which razes the platform to restore root login."""
        try:
            password = self.ssm.get_parameter(
                Name=config.CONSOLE_PASSWORD_PARAM, WithDecryption=True)["Parameter"]["Value"]
        except self.ssm.exceptions.ParameterNotFound:
            return self._json(404, {"error": "no console password stored"})
        return self._json(200, {
            "user": config.BILLING_CONSOLE_USER,
            "password": password,
            "signin_url": f"https://{self.platform.account_id}.signin.aws.amazon.com/console",
        })

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return self._json(403, {"error": "forbidden"})
        action = urlparse(self.path).path.lstrip("/")
        if action not in _WRITE_ACTIONS:
            return self._json(404, {"error": "not found"})
        payload = self._body()
        if payload is None:
            return self._json(400, {"error": "invalid json"})
        job_id = jobs.create(self.ssm, action, payload)
        threading.Thread(target=self._run, args=(action, job_id, payload), daemon=True).start()
        return self._json(202, {"job": job_id})

    def _run(self, action, job_id, payload):
        try:
            ctx = Ctx(self.session, self.platform)
            result = actions.ACTIONS[action](ctx, payload)
            jobs.succeed(self.ssm, job_id, action, payload, result)
        except Exception as exc:  # noqa: BLE001 — record any failure on the job
            jobs.fail(self.ssm, job_id, action, payload, str(exc))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def main() -> None:
    session, region = _session_and_region()
    ssm = session.client("ssm", region_name=region)
    Handler.session = session
    Handler.ssm = ssm
    Handler.platform = Ctx.load_platform(ssm)
    Handler.api_key = _load_api_key(ssm)
    reaped, pruned = jobs.sweep(ssm)
    print(f"swept jobs: reaped {reaped} orphan(s), pruned {pruned} old; serving on {BIND}")
    ThreadingHTTPServer(BIND, Handler).serve_forever()


if __name__ == "__main__":
    main()
