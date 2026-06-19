"""test_portability.py — Export/Import (B). key'siz: embed stub.
Kritik: erasure TAŞINIR (forget edilen içerik import'ta dirilmez) + secret export'ta YOK."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_port.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import embeddings  # noqa: E402
import memory  # noqa: E402
import portability  # noqa: E402
import store  # noqa: E402


def _stub_embed(text):
    t = (text or "").lower()
    if "kahve" in t:
        return [1.0, 0.0, 0.0]
    if "gizli" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


embeddings.embed = _stub_embed

A, B = "t_src", "t_dst"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


# kaynak tenant: memory + rule + correction + bir forget (tombstone)
store.upsert_tenant(A, "Src", "k_src")
store.upsert_tenant(B, "Dst", "k_dst")
memory.ingest(A, "x", facts=["Kullanıcı kahveyi sever", "Kullanıcı Python yazar"])
store.add_rule({"id": "r1", "tenant_id": A, "project": "P", "type": "hard", "severity": "high",
                "message": "kural bir", "forbid_patterns": ["foo"]})
store.add_rule({"id": "r2", "tenant_id": A, "project": "P", "type": "soft", "severity": "high",
                "message": "kural iki", "rule": "niyet"})
store.disable_rule(A, "r2")  # disabled — yapışkanlık import'ta korunmalı
store.add_correction({"id": "c1", "tenant_id": A, "project": "P", "category": "style",
                      "what_was_wrong": "w", "correct_behavior": "cb", "reusable_rule": "rr"})
# forget edilecek içerik
memory.ingest(A, "x", facts=["Gizli bilgi silinecek"])
gid = [m["id"] for m in store.get_active_memories(A) if "gizli" in m["content"].lower()][0]
memory.forget(A, gid)

print("== 1) export: bilgi var, SECRET yok ==")
bundle = portability.export_tenant(A)
check(bundle["counts"]["memories"] == 2, f"2 aktif memory export'landı ({bundle['counts']['memories']})")
check("api_keys" not in bundle and "tenants" not in bundle, "bundle'da api_keys/tenants YOK")
check(bundle["counts"]["rules"] == 2, "disabled dahil 2 kural")
check(bundle["counts"]["tombstones"] >= 1, "tombstone export'landı (erasure taşınır)")
check(not any("gizli" in m["content"].lower() for m in bundle["memories"]),
      "forget edilen içerik bundle'da YOK (shred edildi)")

print("== 2) import (merge) -> hedef tenant doldu ==")
res = portability.import_tenant(B, bundle, mode="merge")
check(res["memories"]["added"] == 2, f"2 memory import edildi ({res['memories']})")
check(res["rules"] == 2 and res["corrections"] == 1, "kural+correction import edildi")
check(store.count_memories(B) == 2, "hedefte 2 aktif memory")

print("== 3) KRİTİK: erasure TAŞINDI — forget edilen içerik hedefte de DİRİLMEZ ==")
rr = memory.ingest(B, "x", facts=["Gizli bilgi silinecek"])
check(rr.get("blocked", 0) >= 1 and rr["added"] == 0, f"hedefte forget'li içerik bloklu ({rr})")

print("== 4) disabled kural yapışkanlığı korundu ==")
b_rules = {r["id"]: r for r in store.get_rules(B, active_only=False)}
check(b_rules.get("r2", {}).get("status") == "disabled", "import'ta r2 disabled kaldı")

print("== 5) izolasyon: import A'yı etkilemedi ==")
check(store.count_memories(A) == 2, "kaynak hâlâ 2 aktif memory")

print("== 6) sahte 'api_keys' bundle'ı YOK SAYILIR (allowlist) ==")
evil = {"bundle_version": 1, "memories": [{"content": "zarasız"}],
        "api_keys": [{"key_hash": "x", "role": "owner"}], "tenants": [{"tenant_id": B}]}
res2 = portability.import_tenant(B, evil, mode="merge")
check(res2["memories"]["added"] == 1, "sadece memory işlendi")
check(len(store.list_api_keys(B)) == 0, "sahte api_key import EDİLMEDİ")

print("== 7) replace mode: bilgiyi temizler ama hesabı/key'i SİLMEZ ==")
store.add_api_key(B, store._hash_key("realkey"), "k", "owner")
before_keys = len(store.list_api_keys(B))
res3 = portability.import_tenant(B, bundle, mode="replace")
check(res3.get("cleared") is not None, "replace clear_knowledge çalıştı")
check(len(store.list_api_keys(B)) == before_keys, "replace api_keys'i SİLMEDİ (lockout yok)")
check(store.tenant_by_key("realkey") == B, "key hâlâ tenant'a çözülüyor")

print("== 8) geçersiz bundle reddedilir ==")
try:
    portability.import_tenant(B, {"bundle_version": 99}, mode="merge")
    check(False, "bilinmeyen versiyon kabul edilmemeliydi")
except ValueError:
    check(True, "bilinmeyen bundle_version reddedildi")

print("== 9) CRITICAL: replace, HEDEFİN tombstone'unu korur (forget'li dirilmez) ==")
store.upsert_tenant("t_rep", "Rep", "k_rep")
memory.ingest("t_rep", "x", facts=["Hassas PII silinecek"])
pid = [m["id"] for m in store.get_active_memories("t_rep") if "pii" in m["content"].lower()][0]
memory.forget("t_rep", pid)
check(store.count_memories("t_rep") == 0, "forget sonrası 0 aktif")
benign = portability.export_tenant(A)   # A'da bu içeriğin tombstone'u YOK (iyi niyetli bundle)
portability.import_tenant("t_rep", benign, mode="replace")
rrep = memory.ingest("t_rep", "x", facts=["Hassas PII silinecek"])
check(rrep.get("blocked", 0) >= 1 and rrep["added"] == 0,
      f"replace SONRASI forget'li PII hâlâ bloklu (tombstone korundu) ({rrep})")

print("== 10) HIGH: export source + metadata scrub ==")
store.upsert_tenant("t_sec", "Sec", "k_sec")
memory.ingest("t_sec", "x", facts=["normal fact"], source="password=GIZLI12345")
b = portability.export_tenant("t_sec")
check(all("GIZLI12345" not in str(m.get("source")) for m in b["memories"]), "source export'ta scrub")
# metadata yalnız import ile dolar -> secret'lı metadata import et, sonra export'ta scrub bekle
portability.import_tenant("t_sec", {"bundle_version": 1, "memories": [
    {"content": "meta testi", "metadata": {"note": "token=ABCSECRET1234567890123"}}]}, mode="merge")
b2 = portability.export_tenant("t_sec")
mt = [m for m in b2["memories"] if "meta testi" in m["content"]][0]
check("ABCSECRET" not in str(mt["metadata"]), "metadata export'ta scrub")

print("== 11) LOW: non-str mode -> ValueError (500 değil) ==")
try:
    portability.import_tenant(B, {"bundle_version": 1, "memories": []}, mode=["x"])
    check(False, "non-str mode kabul edilmemeliydi")
except ValueError:
    check(True, "non-str mode ValueError verdi (TypeError sızmadı)")

print("== 12) LOW: import salience clamp (Inf -> [0,5]) ==")
portability.import_tenant("t_sec", {"bundle_version": 1, "memories": [
    {"content": "salience testi", "salience": float("inf")}]}, mode="merge")
ms = [m for m in store.get_active_memories("t_sec") if "salience testi" in m["content"]][0]
check(0.0 <= ms["salience"] <= 5.0, f"salience clamp edildi ({ms['salience']})")

print("== 13) fix-verify residual: COMPREHENSIVE egress scrub (key + tüm alanlar) ==")
store.upsert_tenant("t_cov", "Cov", "k_cov")
# metadata KEY'inde + nested key'de secret + correction category/tags + rule project'te secret
portability.import_tenant("t_cov", {"bundle_version": 1,
    "memories": [{"content": "kapsam testi",
                  "metadata": {"token=KEYLEAK1234567890ABCDEF": "v",
                               "nested": {"sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA": 1}}}],
    "rules": [{"id": "rc", "project": "token=PROJLEAK1234567890ABC", "type": "hard",
               "severity": "high", "message": "m", "forbid_patterns": ["foo"]}],
    "corrections": [{"id": "cc", "category": "token=CATLEAK1234567890ABCD",
                     "tags": ["sk-ant-TAGLEAK00000000000000000"], "what_was_wrong": "w",
                     "correct_behavior": "c", "reusable_rule": "r"}]}, mode="merge")
b3 = portability.export_tenant("t_cov")
blob = str(b3)
check("KEYLEAK" not in blob, "metadata KEY'indeki secret scrub'landı")
check("sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA" not in blob, "nested metadata key secret scrub'landı")
check("PROJLEAK" not in blob, "rule project'teki secret scrub'landı")
check("CATLEAK" not in blob, "correction category secret scrub'landı")
check("TAGLEAK" not in blob, "correction tags secret scrub'landı")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM PORTABILITY TESTLERİ GEÇTİ")
