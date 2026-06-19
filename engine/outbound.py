#!/usr/bin/env python3
"""outbound.py — giden (outbound) webhook teslimatı.

Müşteri kayıtlı bir URL verir; olay olunca (örn. memory.created) o URL'ye
imzalı (HMAC-SHA256) bir POST gider. Tasarım:
- best-effort, ARKA-PLAN thread (istek yolunu bloklamaz), kısa timeout. v0: tek deneme (retry yok).
- secret crypto.encrypt ile saklanır (imzalamak için geri-çözülmeli); düz değer SADECE üretimde döner.
- SSRF koruması: hedef host private/loopback/link-local/reserved çözülürse REDDEDİLİR
  (create'te + her teslimatta — DNS-rebind'e karşı). 169.254.169.254 (bulut metadata) bloklu.
- payload egress'te scrub'lanır (secret sızıntısı yok).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets as _secrets
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import crypto  # noqa: E402
import store  # noqa: E402
from redact import scrub_obj  # noqa: E402

TIMEOUT = 4.0
EVENTS = ("memory.created", "kg.updated", "rule.created", "ping")


def safe_url(url: str) -> bool:
    """SSRF guard: http(s) + host private/loopback/link-local/reserved/multicast DEĞİL."""
    try:
        p = urlparse(url or "")
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for fam, _t, _pr, _c, sockaddr in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


def _post(url: str, body: bytes, sig: str, event: str) -> str:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-13auth-Event": event,
        "X-13auth-Signature": "sha256=" + sig,
        "User-Agent": "13auth-webhooks/1",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (scheme safe_url ile doğrulandı)
        return str(getattr(resp, "status", 0) or resp.getcode())


def _sign(secret: str, body: bytes) -> str:
    return hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()


def _payload(event: str, data: dict) -> bytes:
    return json.dumps({"event": event, "data": scrub_obj(data or {}), "ts": int(time.time())},
                      ensure_ascii=False).encode("utf-8")


def _deliver_one(tenant_id: str, hook: dict, event: str, body: bytes) -> str:
    if not safe_url(hook.get("url", "")):       # teslimat anında tekrar kontrol (DNS-rebind)
        status = "err: unsafe_url"
    else:
        secret = crypto.decrypt(hook.get("secret_hash") or "") or ""
        try:
            status = _post(hook["url"], body, _sign(secret, body), event)
        except Exception as e:                  # best-effort: müşteri ucu hatası bizi etkilemez
            status = f"err: {type(e).__name__}"
    try:
        store.touch_webhook(tenant_id, hook["id"], status)
    except Exception:
        pass
    return status


def _deliver_all(tenant_id: str, event: str, data: dict) -> None:
    try:
        hooks = [h for h in store.list_webhooks(tenant_id, active_only=True)
                 if not h["events"] or event in h["events"]]
    except Exception:
        return
    if not hooks:
        return
    body = _payload(event, data)
    for h in hooks:
        _deliver_one(tenant_id, h, event, body)


def fire(tenant_id: str, event: str, data: dict) -> None:
    """Bloklamadan tetikle (best-effort, arka-plan thread). İstek yolundan güvenle çağrılır."""
    try:
        threading.Thread(target=_deliver_all, args=(tenant_id, event, dict(data or {})),
                         daemon=True).start()
    except Exception:
        pass


def create(tenant_id: str, url: str, events: list[str]) -> dict:
    """Yeni webhook kaydı. secret üretilir, crypto ile ŞİFRELİ saklanır, düz değer SADECE burada döner
    (müşteri imza doğrulaması için saklar). Bilinmeyen event'ler atılır; boş = tüm event'ler."""
    evs = [e for e in (events or []) if e in EVENTS]
    sec = "whsec_" + _secrets.token_urlsafe(24)
    wid = store.add_webhook(tenant_id, url, crypto.encrypt(sec), ",".join(evs))
    return {"id": wid, "url": url, "events": evs, "secret": sec}


def test(tenant_id: str, webhook_id: str) -> dict:
    """Tek bir webhook'a senkron 'ping' gönder (UI 'Test gönder' butonu) -> {ok, status}."""
    hooks = [h for h in store.list_webhooks(tenant_id) if h["id"] == webhook_id]
    if not hooks:
        return {"ok": False, "error": "not_found"}
    body = _payload("ping", {"message": "13auth test event"})
    status = _deliver_one(tenant_id, hooks[0], "ping", body)
    return {"ok": status == "200", "status": status}
