#!/usr/bin/env python3
"""metrics.py — hafif in-process istek sayaçları (observability).

Prometheus text + JSON snapshot. Path'ler id'ler sıyrılarak şablonlanır (cardinality
patlamasını önler). PII/payload TUTMAZ — sadece method/path-şablon/status sayıları.
Çok-process'te her process kendi sayar (ölçekte Prometheus/StatsD'ye taşı).
"""
from __future__ import annotations

import re
import threading

_lock = threading.Lock()
_counts: dict[str, int] = {}

# /v1/rules/rule-abc, /v1/memories/mem-xyz, /v1/keys/key-1 -> /v1/<x>/{id}
_ID = re.compile(r"^(/v1/(?:rules|memories|keys))/[^/]+$")


def _template(path: str) -> str:
    return _ID.sub(r"\1/{id}", path)


def record(method: str, path: str, status: int) -> None:
    key = f"{method} {_template(path)} {status}"
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1


def snapshot() -> dict:
    with _lock:
        return dict(_counts)


def prometheus() -> str:
    lines = ["# HELP cce_requests_total CCE HTTP istek sayısı",
             "# TYPE cce_requests_total counter"]
    with _lock:
        for k, v in sorted(_counts.items()):
            method, path, status = k.split(" ", 2)
            lines.append(f'cce_requests_total{{method="{method}",path="{path}",status="{status}"}} {v}')
    return "\n".join(lines) + "\n"
