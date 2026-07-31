"""Entry point: `python -m openzi_itworker <setup|server>`.

- setup:  read config from the environment (set by the launch user-data), build a
          boto3 session on the instance profile, and run the one-shot bring-up.
- server: run the long-lived control-plane HTTP server.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else ""

    if mode == "setup":
        import boto3

        from .setup import SetupConfig, run
        cfg = SetupConfig.from_env()
        run(cfg, boto3.Session())
        return 0

    if mode == "server":
        from .server.server import main as serve
        serve()
        return 0

    sys.stderr.write("usage: python -m openzi_itworker <setup|server>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
