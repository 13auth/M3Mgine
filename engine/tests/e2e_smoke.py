"""e2e_smoke.py — tüm stack'i CANLI sunucuda uçtan uca sürer (SDK + raw webhook).
Env: E2E_BASE, E2E_OWNER (bootstrap owner key), E2E_SECRET (webhook secret).
Sunucu + tenant/rules/subscription önceden kurulmuş olmalı (orkestrasyon dışarıda)."""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ["E2E_BASE"]
OWNER = os.environ["E2E_OWNER"]
SECRET = os.environ["E2E_SECRET"]
SUB = os.environ.get("E2E_SUB", "sub_e2e")  # link-subscription ref'iyle EŞLEŞMELİ
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from client import CCEClient, CCEError  # noqa: E402

FAILS = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


# health bekle
for _ in range(20):
    try:
        urllib.request.urlopen(BASE + "/health", timeout=2); break
    except Exception:
        time.sleep(0.3)

own = CCEClient(BASE, OWNER)
print("== auth + plan ==")
chk(own.usage()["plan"] == "growth", "owner plan=growth")

print("== accounts: owner member key üretir ==")
mk = own._req("POST", "/v1/keys", {"name": "m", "role": "member"})
mem = CCEClient(BASE, mk["api_key"])
chk(mk["api_key"].startswith("cce_"), "member key üretildi")

print("== memory: ingest -> recall ==")
r = mem.remember(facts=["Kullanıcı Python tercih eder", "Kullanıcı İstanbul'da yaşıyor"], user_id="u")
chk(r["added"] == 2, f"2 memory eklendi ({r['added']})")
chk(len(mem.recall("nerede yaşıyor", user_id="u", top_k=2)) >= 1, "recall sonuç döndü")

print("== enforce: hard kural ayırt eder ==")
bad = mem.check("binlerce kanal ve beIN Sports", project="Acme", run_soft=False)
good = mem.check("kendi M3U kaynağını ekle", project="Acme", run_soft=False)
chk(not bad["passed"] and len(bad["violations"]) >= 1, "kötü çıktıda hard ihlal")
chk(good["passed"], "temiz çıktıda hard ihlal yok")

print("== billing: metering işledi ==")
chk(mem.usage()["used"] > 0, f"kullanım metered ({mem.usage()['used']})")

print("== RBAC: member key üretemez (403) ==")
try:
    mem._req("POST", "/v1/keys", {"name": "x"}); chk(False, "member POST keys 403 vermeliydi")
except CCEError as e:
    chk("403" in str(e), "member POST keys -> 403")

print("== context: pack üretimi (member) ==")
pk = mem.context(query="nerede yaşıyor", project="Acme", user_id="u", render=True)
chk(pk["pack"]["counts"]["memories"] >= 1, "context pack hafıza içeriyor")
chk("rendered" in pk and "Context Pack" in pk["rendered"], "render döndü")

print("== handoff: snapshot + resume (ayrı user) ==")
hs = mem.handoff("e2e-sess", facts=["Oturum testi faktı"], summary="kaldığımız yer: API testi",
                 user_id="ho")
chk(hs["facts_added"] >= 1, "handoff fact ekledi")
rz = mem.resume("e2e-sess")
chk(rz["found"] and rz["summary"].startswith("kaldığımız"), "resume özet döndü")

print("== export/import (owner; member 403) ==")
bundle = own.export()
chk(bundle["counts"]["memories"] >= 1 and "api_keys" not in bundle, "export bilgi var, secret yok")
try:
    mem.export(); chk(False, "member export 403 vermeliydi")
except CCEError as e:
    chk("403" in str(e), "member export -> 403")
imp = own.import_bundle(bundle, mode="merge")
chk(imp["memories"]["added"] >= 0 and "rules" in imp, "import çalıştı (erasure-güvenli)")

print("== knowledge graph: ingest (triples) + search + bi-temporal ==")
mem.kg_add(triples=[{"subject": "Ekip", "predicate": "works_at", "object": "Acme",
                     "object_type": "entity", "fact": "Ekip Acme'da"}], valid_at=1000.0)
kgc = mem.kg_add(triples=[{"subject": "Ekip", "predicate": "works_at", "object": "ExampleCo",
                           "object_type": "entity", "fact": "Ekip ExampleCo'da"}], valid_at=2000.0)
chk(kgc["invalidated"] >= 1, "kg çelişki: eski works_at geçersizlendi")
kgh = mem.kg_search("Ekip nerede", top_k=3)
chk(any("ExampleCo" in (h.get("object") or "") for h in kgh), "kg search güncel (ExampleCo) döndü")
past = mem.kg_search("Ekip nerede", as_of=1500.0, top_k=3)
chk(any("Acme" in (h.get("object") or "") for h in past), "kg point-in-time (Acme) döndü")

print("== erasure: forget ==")
mid = mem.memories(user_id="u")[0]["id"]
mem.forget_memory(mid)
chk(len(mem.memories(user_id="u")) == 1, "forget sonrası 1 memory")

print("== webhook: imzalı cancel -> plan free ==")
ts = str(int(time.time()))
body = json.dumps({"id": "e2e-evt-1", "type": "subscription.canceled", "subscription_id": SUB}).encode()
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
req = urllib.request.Request(BASE + "/v1/webhooks/billing", data=body, method="POST")
req.add_header("X-Signature", sig); req.add_header("X-Timestamp", ts); req.add_header("Content-Type", "application/json")
urllib.request.urlopen(req, timeout=10)
chk(own.usage()["plan"] == "free", "webhook cancel -> plan free")

print("== accounts: owner member'ı iptal eder -> member 401 ==")
own._req("DELETE", "/v1/keys/" + mk["id"])
try:
    mem.usage(); chk(False, "revoked member 401 vermeliydi")
except CCEError as e:
    chk("401" in str(e), "revoked member -> 401")

print()
if FAILS:
    print(f"E2E SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("E2E SONUÇ: TÜM STACK UÇTAN UCA GEÇTİ")
