#!/usr/bin/env python3
"""webhooks.py — ödeme sağlayıcı (Polar/iyzico) abonelik olaylarını plana çevirir.

Güvenlik (audit Faz-2):
- HMAC-SHA256 imza, İMZALANAN payload = "{timestamp}.{raw}" + freshness penceresi
  (replay'i sınırlar). Secret/timestamp yoksa fail-CLOSED.
- event id ile idempotency/dedup (yeniden gönderim no-op).
- tenant body'deki tenant_id'den DEĞİL, sunucu-tarafı subscription eşlemesinden
  çözülür (checkout'ta store.link_subscription ile kurulur) -> IDOR/confused-deputy yok.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import billing  # noqa: E402
import store  # noqa: E402

WEBHOOK_SECRET = os.environ.get("CCE_WEBHOOK_SECRET", "")
MAX_SKEW = 300  # saniye: imza bu kadar eskiyse reddet (replay penceresi)


def verify(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """HMAC-SHA256("{ts}." + raw) + freshness. Secret/timestamp yok -> fail-closed."""
    if not WEBHOOK_SECRET or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > MAX_SKEW:
        return False
    signed = f"{ts}.".encode("utf-8") + (raw_body or b"")
    mac = hmac.new(WEBHOOK_SECRET.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature or "")


def handle(event: dict) -> dict:
    """Olayı plana çevir. event: {id, type, subscription_id|customer_id, plan}."""
    eid = event.get("id")
    if not eid:
        return {"ok": False, "error": "no_event_id"}
    ref = event.get("subscription_id") or event.get("customer_id")
    t = store.tenant_by_subscription(ref)  # body'deki tenant_id'ye GÜVENME
    if not t:
        return {"ok": False, "error": "unknown_subscription", "ref": ref}

    typ = str(event.get("type", ""))
    plan = event.get("plan")
    if typ in ("subscription.created", "subscription.updated", "subscription.active"):
        if plan not in billing.PLANS:
            return {"ok": False, "error": "unknown_plan", "plan": plan}
        target = plan
    elif typ in ("subscription.canceled", "subscription.cancelled", "subscription.expired"):
        target = "free"
    else:
        return {"ok": False, "error": "unhandled_event", "type": typ}

    # idempotency SON adım: sadece geçerli/uygulanabilir event'i işaretle (başarısızda retry mümkün)
    if store.seen_event(eid):
        return {"ok": True, "dedup": True, "event_id": eid}
    store.set_plan(t, target)
    store.record_admin(t, "billing_webhook", typ, detail=f"plan={target}")
    return {"ok": True, "tenant": t, "plan": target}
