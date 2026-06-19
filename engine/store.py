#!/usr/bin/env python3
"""store.py - backend dispatcher (CCE_STORE_BACKEND=sqlite|postgres).

SQLite = dev/test default + conformance oracle. Postgres = production substrate.
The chosen backend's full public API AND internal helpers (_fold / _hash_key /
_safe_salience) are re-exported into this module namespace, so every existing
`import store; store.x(...)` call site (258 of them) and all test suites work
unchanged. Switch backend with the CCE_STORE_BACKEND env var.
"""
from __future__ import annotations

import os as _os

_BACKEND = _os.environ.get("CCE_STORE_BACKEND", "sqlite").strip().lower()
if _BACKEND in ("postgres", "pg"):
    import store_pg as _impl
else:
    import store_sqlite as _impl

BACKEND = _impl.__name__


def _reexport() -> None:
    g = globals()
    for _n in dir(_impl):
        if _n.startswith("__"):
            continue
        g[_n] = getattr(_impl, _n)


_reexport()
