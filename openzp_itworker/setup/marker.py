"""Write the setup result marker: a single SSM PutParameter whose PARAMETER NAME
encodes success vs failure. The workflow polls CloudTrail event history for one of
these two names (event history exposes the name of a PutParameter, never the value),
so it learns the outcome without waiting for its job to time out. The value carries
detail for a human debugging via the management account."""

from __future__ import annotations

import time

from .. import config


def write_success(ssm) -> None:
    ssm.put_parameter(Name=config.SETUP_OK_PARAM, Value=str(int(time.time())),
                      Type="String", Overwrite=True)


def write_failure(ssm, exc: BaseException) -> None:
    ssm.put_parameter(Name=config.SETUP_FAILED_PARAM, Value=str(exc)[:1024],
                      Type="String", Overwrite=True)
