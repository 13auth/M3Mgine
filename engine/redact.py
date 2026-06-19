#!/usr/bin/env python3
"""redact.py — paylaşılan secret filtresi. HEM ingest HEM enforce yolu kullanır.
(Audit bulgusu: enforce, output'u LLM judge'a scrub'sız yolluyordu.)"""
from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)(api[\s_-]?key|access[\s_-]?token|token|secret|password|passwd|pwd|bearer)\s*[:=]\s*\S+"),
]


def scrub_secrets(text: str) -> tuple[str, bool]:
    found = False
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            found = True
            text = pat.sub("[REDACTED_SECRET]", text)
    return text, found


def scrub_obj(obj, _depth: int = 0):
    """İç içe dict/list/tuple/str içindeki secret'ları RECURSIVE scrub'lar (kopya döndürür).
    serbest-form JSON (metadata) ve export kayıtlarının egress'inde kullanılır. dict
    KEY'leri de scrub'lanır (secret bir anahtar adında olabilir). Derinlik sınırı: kötü-
    biçimli/çok-derin yapı patlamasın."""
    if _depth > 8:
        return obj
    if isinstance(obj, str):
        return scrub_secrets(obj)[0]
    if isinstance(obj, dict):
        return {scrub_secrets(str(k))[0]: scrub_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_obj(v, _depth + 1) for v in obj)
    return obj
