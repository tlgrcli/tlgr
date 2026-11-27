"""Install tests/delete_guard.py in Python processes the test suite starts.

`tests/delete_guard.py` puts this directory on `PYTHONPATH`, so a `python -m
tlgr ...` child spawned by a test refuses deletes outside the run's temp
roots exactly like the test process does. The guard is found next to this
directory rather than through an environment variable, so a test that
filters the child's environment cannot switch it off. Outside a test run
this directory is never on the path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_guard = Path(__file__).resolve().parent.parent / "delete_guard.py"
_spec = importlib.util.spec_from_file_location("tlgr_test_delete_guard", _guard)
if _spec is None or _spec.loader is None:
    sys.exit(f"tests/_guard_site: cannot load the delete guard from {_guard}")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_module.install()
