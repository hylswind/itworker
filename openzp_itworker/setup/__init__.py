"""setup mode: the one-shot bring-up the workflow-launched instance runs.

Order (see run()): wait out the audit window -> register (or, in skip mode, verify
+ clean) the domain -> deploy the platform stack -> wire this instance into the
control ASG/target group -> store the API key -> write the result marker -> exec
into server mode. The whole body is wrapped so a failure still writes a marker (a
DIFFERENT parameter name) — the workflow polls event history for either name and
never has to wait for its job to time out."""

from __future__ import annotations

from .config import SetupConfig
from .runner import run

__all__ = ["SetupConfig", "run"]
