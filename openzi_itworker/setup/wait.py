"""Wait until an absolute wall-clock deadline (epoch seconds).

setup waits until end_epoch + 1s: by then the workflow has deleted the root key
and enabled the console lockout, so no operator can interfere, and the [start,end]
audit window stays free of itworker's own activity."""

from __future__ import annotations

import time


def until(epoch: float, *, poll: float = 5.0, sleep=time.sleep, now=time.time) -> None:
    while now() < epoch:
        sleep(min(poll, epoch - now()))
