"""Make the code under test importable however pytest is invoked.

Bare `pytest` — what the README documents — does not put the invocation directory
on sys.path the way `python -m pytest` does, so without this only the module form
collects. `tests/e2e` goes on too, so the e2e can `import driver`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for path in (ROOT, ROOT / "tests" / "e2e"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
