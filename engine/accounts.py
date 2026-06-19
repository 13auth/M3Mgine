#!/usr/bin/env python3
"""accounts.py — çoklu API-key yönetimi (Faz 2 hesap katmanı).

Bir org (tenant) birden çok isimli, rollü, iptal-edilebilir key üretebilir.
Düz key SADECE üretimde bir kez döner; store'da yalnızca hash tutulur. Auth
çözümlemesi store.tenant_by_key içinde (önce api_keys aktif, sonra bootstrap key).
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store  # noqa: E402

ROLES = ("owner", "member")


def create_key(tenant_id: str, name: str = "key", role: str = "member") -> dict:
    """Yeni key üret. Dönen 'api_key' düz değer SADECE burada görülür (saklanmaz)."""
    if role not in ROLES:
        role = "member"
    raw = "cce_" + secrets.token_urlsafe(32)
    kid = store.add_api_key(tenant_id, store._hash_key(raw), name, role)
    return {"id": kid, "api_key": raw, "name": name, "role": role}


def revoke_key(tenant_id: str, key_id: str) -> int:
    return store.revoke_api_key(tenant_id, key_id)


def list_keys(tenant_id: str) -> list[dict]:
    return store.list_api_keys(tenant_id)
