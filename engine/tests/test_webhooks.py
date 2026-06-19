"""test_webhooks.py — ödeme webhook'u: imzalı-ts + freshness + dedup + subscription-mapping (key'siz)."""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_webhooks.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)
os.environ["CCE_WEBHOOK_SECRET"] = "testsecret"  # webhooks import'undan ÖNCE

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import store  # noqa: E402
import webhooks  # noqa: E402

T = "t_wh"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


def sign(ts: int, body: bytes) -> str:
    return hmac.new(b"testsecret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


store.upsert_tenant(T, "WH", "kwh")
store.link_subscription(T, "sub_123")  # checkout eşlemesi (BİZ kurarız)

print("== 1) imzalı-timestamp doğrulama + freshness ==")
body = json.dumps({"id": "ev1", "type": "subscription.updated", "subscription_id": "sub_123", "plan": "growth"}).encode()
now = int(time.time())
check(webhooks.verify(body, sign(now, body), str(now)), "taze+doğru imza geçer")
check(not webhooks.verify(body, sign(now, body), str(now - 9999)), "eski timestamp reddedilir (replay penceresi)")
check(not webhooks.verify(body, "bad", str(now)), "yanlış imza reddedilir")
check(not webhooks.verify(body, sign(now, body), ""), "timestamp yoksa reddedilir")

print("== 2) handle: subscription-mapping'den tenant çöz (body tenant_id'ye güvenmez) ==")
r = webhooks.handle(json.loads(body))
check(r["ok"] and store.get_plan(T) == "growth", "sub_123 -> tenant T, plan growth")

print("== 3) IDOR: body'de sahte tenant_id işe yaramaz ==")
store.set_plan(T, "free")
ev_idor = {"id": "ev2", "type": "subscription.updated", "subscription_id": "sub_123",
           "tenant_id": "attacker", "plan": "pro"}
r3 = webhooks.handle(ev_idor)
check(r3["ok"] and r3["tenant"] == T and store.get_plan(T) == "pro", "tenant mapping'den (attacker yok sayıldı)")
check(store.get_plan("attacker") == "free", "attacker tenant'ı etkilenmedi")

print("== 4) replay/dedup: aynı event id ikinci kez no-op ==")
store.set_plan(T, "starter")
ev = {"id": "ev_dup", "type": "subscription.updated", "subscription_id": "sub_123", "plan": "growth"}
r4a = webhooks.handle(ev)
check(r4a["ok"] and store.get_plan(T) == "growth", "ilk işlem uygulandı")
store.set_plan(T, "free")               # dışarıdan değişti
r4b = webhooks.handle(ev)               # aynı event id tekrar
check(r4b.get("dedup") and store.get_plan(T) == "free", "replay no-op (plan'ı geri flip etmedi)")

print("== 5) bilinmeyen subscription reddedilir ==")
r5 = webhooks.handle({"id": "ev3", "type": "subscription.updated", "subscription_id": "yok", "plan": "pro"})
check(not r5["ok"] and r5["error"] == "unknown_subscription", "eşlenmemiş ref reddedilir")

print("== 6) cancel -> free ==")
store.set_plan(T, "pro")
r6 = webhooks.handle({"id": "ev4", "type": "subscription.canceled", "subscription_id": "sub_123"})
check(r6["ok"] and store.get_plan(T) == "free", "cancel -> free")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM WEBHOOK TESTLERİ GEÇTİ")
