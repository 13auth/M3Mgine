"""test_checkout.py — checkout başlatma -> link -> webhook -> plan yükselir (stub provider, gerçek tahsilat YOK).

Ödemenin LOKAL akışını uçtan uca doğrular: payments.create_checkout (stub) -> store.link_subscription ->
imzalı sağlayıcı webhook'u -> store.set_plan. Canlı creds GEREKMEZ. Hem sqlite hem postgres'te koşar.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CCE_PAYMENT_PROVIDER", "stub")
os.environ.setdefault("CCE_WEBHOOK_SECRET", "test_secret_checkout")
_tmp = Path(tempfile.gettempdir()) / "cce_test_checkout.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import billing  # noqa: E402
import payments  # noqa: E402
import store  # noqa: E402
import webhooks  # noqa: E402

SECRET = b"test_secret_checkout"


def fail(m):
    print("test_checkout.py: FAIL -", m)
    sys.exit(1)


def signed(event: dict):
    raw = json.dumps(event).encode("utf-8")
    ts = str(int(time.time()))
    mac = hmac.new(SECRET, f"{ts}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()
    return raw, mac, ts


store.upsert_tenant("T", "T", "kT")
if store.get_plan("T") != "free":
    fail("baslangic plan free olmali")

# 1) checkout başlat (stub) + eşleme kur
co = payments.create_checkout("T", "starter")
if not co.get("url") or not co.get("ref"):
    fail("checkout url/ref dönmedi")
store.link_subscription("T", co["ref"])
if store.tenant_by_subscription(co["ref"]) != "T":
    fail("subscription eşleme kurulmadı")

# 2) geçersiz planlar reddedilmeli (free/enterprise self-serve değil)
for bad in ("free", "enterprise", "yok"):
    try:
        payments.create_checkout("T", bad)
        fail(f"gecersiz plan kabul edildi: {bad}")
    except payments.PaymentError:
        pass

# 3) imzalı webhook -> plan yükselir
evt = {"id": "co_evt1", "type": "subscription.active", "subscription_id": co["ref"], "plan": "starter"}
raw, mac, ts = signed(evt)
if not webhooks.verify(raw, mac, ts):
    fail("webhook imza dogrulanmadi")
res = webhooks.handle(evt)
if not res.get("ok") or store.get_plan("T") != "starter":
    fail(f"webhook plani yukseltmedi: {res} plan={store.get_plan('T')}")

# 4) idempotency: aynı event tekrar -> dedup, plan değişmez
res2 = webhooks.handle(evt)
if not res2.get("dedup"):
    fail("ayni event dedup edilmedi")

# 5) cancel -> free
ev2 = {"id": "co_evt2", "type": "subscription.canceled", "subscription_id": co["ref"]}
webhooks.handle(ev2)
if store.get_plan("T") != "free":
    fail("cancel sonrasi free olmali")

# 6) iyzico müşteri builder: eksik alan -> hata; tam -> iyzico şekli (network yok)
try:
    payments._iyzico_customer({"name": "A"})
    fail("eksik customer kabul edildi")
except payments.PaymentError:
    pass
full = {"name": "Test", "surname": "User", "email": "test@example.com", "gsmNumber": "+905550000000",
        "identityNumber": "11111111111", "city": "İstanbul", "address": "Örnek Mah. No:1"}
ic = payments._iyzico_customer(full)
if ic["billingAddress"]["contactName"] != "Test User" or ic["billingAddress"]["country"] != "Turkey":
    fail("iyzico customer şekli yanlış")
if ic["shippingAddress"]["city"] != "İstanbul":
    fail("shippingAddress kurulmadı")

print("test_checkout.py: PASS")
