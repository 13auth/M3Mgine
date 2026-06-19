#!/usr/bin/env python3
"""iyzico_setup.py — (tek seferlik) abonelik ÜRÜN + 4 PLAN oluştur, pricingPlanReferenceCode'ları al.

engine/.env'den CCE_IYZICO_* okur, payments._iyzico_auth (IYZWSv2) ile imzalar.
Çıktıdaki CCE_IYZICO_PLAN_* satırlarını .env'e ekle (ref'ler GİZLİ DEĞİL).
Sandbox'ta çalıştır: python engine/scripts/iyzico_setup.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))


def load_env(p: Path):
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env(ENG / ".env")
import payments  # noqa: E402  (_iyzico_auth imzası — bu çağrı onu da doğrular)

BASE = os.environ.get("CCE_IYZICO_BASE", "https://sandbox-api.iyzipay.com").rstrip("/")
API = os.environ.get("CCE_IYZICO_API_KEY", "")
SECRET = os.environ.get("CCE_IYZICO_SECRET", "")
if not API or not SECRET:
    print("HATA: CCE_IYZICO_API_KEY/SECRET .env'de yok"); sys.exit(1)


def call(method: str, uri: str, body: dict) -> dict:
    body_str = json.dumps(body)
    auth, rnd = payments._iyzico_auth(uri, body_str, API, SECRET)
    req = urllib.request.Request(BASE + uri, data=body_str.encode("utf-8"), method=method, headers={
        "Authorization": auth, "x-iyzi-rnd": rnd, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8")[:600]}
    except Exception as e:
        return {"_err": str(e)}


prod = call("POST", "/v2/subscription/products", {"locale": "tr", "name": "13auth", "description": "13auth abonelik"})
print("PRODUCT ->", json.dumps(prod)[:500])
ref = (prod.get("data") or {}).get("referenceCode")
if not ref:
    print("\nÜrün oluşturulamadı (yukarıdaki yanıta bak: auth mı format mı abonelik-kapalı mı).")
    sys.exit(1)
print("productReferenceCode:", ref)

PLANS = [("SOLO", "Kişisel", 349), ("STARTER", "Indie", 990), ("GROWTH", "Pro", 3990), ("PRO", "Business", 12990)]
out = {}
for suffix, name, price in PLANS:
    pl = call("POST", f"/v2/subscription/products/{ref}/pricing-plans", {
        "locale": "tr", "name": name, "price": f"{price}.0", "currencyCode": "TRY",
        "paymentInterval": "MONTHLY", "paymentIntervalCount": 1, "planPaymentType": "RECURRING"})
    pref = (pl.get("data") or {}).get("referenceCode")
    print(f"PLAN {name} ({price}) -> {pref or json.dumps(pl)[:400]}")
    if pref:
        out[suffix] = pref

print("\n=== .env'e ekle (ref'ler gizli değil) ===")
print(f"CCE_IYZICO_PRODUCT={ref}")
for k, v in out.items():
    print(f"CCE_IYZICO_PLAN_{k}={v}")
print(f"\n{len(out)}/4 plan oluşturuldu.")
