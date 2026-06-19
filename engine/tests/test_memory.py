"""test_memory.py — Faz 1 fact-memory motoru (key'siz: fact enjekte, embed stub)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_memory.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

# stub embed: anahtar kelimeye göre deterministik vektör (LLM/embed hattı gerekmesin)
def _stub_embed(text):
    t = text.lower()
    if "kahve" in t:
        return [1.0, 0.0, 0.0]
    if "istanbul" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


embeddings.embed = _stub_embed

TENANT = "t_mem"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(TENANT, "Mem", "k_mem")

print("== 1) ingest (fact enjekte) ==")
r = memory.ingest(TENANT, "konuşma", facts=["Kullanıcı kahveyi sever",
                                            "Kullanıcı İstanbul'da yaşıyor"])
check(r["added"] == 2, f"2 fact eklendi ({r['added']})")
check(store.count_memories(TENANT) == 2, "store'da 2 aktif memory")

print("== 2) exact dedup + reinforcement (aynı fact tekrar) ==")
r2 = memory.ingest(TENANT, "konuşma", facts=["Kullanıcı kahveyi sever"])
check(r2["reinforced"] == 1 and r2["added"] == 0, f"tekrar = reinforced ({r2})")
check(store.count_memories(TENANT) == 2, "kopya satır açılmadı (hâlâ 2)")
coffee = [m for m in store.get_active_memories(TENANT) if "kahve" in m["content"].lower()][0]
check(coffee["salience"] > 1.0, f"reinforcement salience artırdı ({coffee['salience']})")

print("== 3) SEMANTİK dedup (farklı kelime, aynı vektör) ==")
r3 = memory.ingest(TENANT, "konuşma", facts=["Kahve içmeyi çok sever"])  # 'kahve' -> aynı vektör
check(r3["reinforced"] == 1 and store.count_memories(TENANT) == 2,
      f"yakın-kopya pekiştirildi, yeni satır yok ({r3['reinforced']}, n={store.count_memories(TENANT)})")

print("== 4) hybrid search (semantik ranking) ==")
hits = memory.search(TENANT, "kahve sever misin", top_k=2)
check(hits and "kahve" in hits[0]["content"].lower(), f"en üst sonuç kahve memory ({hits[0]['content'] if hits else '-'})")
check(hits[0]["semantic"] >= 0.99, f"semantik skor yüksek ({hits[0]['semantic']})")

print("== 5) access_count touch (retrieval reinforcement) ==")
c2 = [m for m in store.get_active_memories(TENANT) if "kahve" in m["content"].lower()][0]
check((c2.get("access_count") or 0) >= 1, f"search sonrası access_count arttı ({c2.get('access_count')})")

print("== 6) tenant izolasyonu ==")
check(store.count_memories("t_other") == 0, "başka tenant memory görmüyor")

print("== 7) forget = CRYPTO-SHRED (KVKK Art.17) ==")
memory.forget(TENANT, coffee["id"])
check(store.count_memories(TENANT) == 1, "forget sonrası 1 aktif memory")
shred = store.get_memory(TENANT, coffee["id"])
check(shred["invalid_at"] is not None, "tombstone kaldı (invalid_at set)")
check(shred["content"] == "" and shred["embedding"] is None, "PII shred edildi (content boş, embedding NULL)")
check(not any("kahve" in h["content"].lower() for h in memory.search(TENANT, "kahve", top_k=5)),
      "forget edilen memory artık search'te yok")

print("== 8) audit CRITICAL: forget edilen fact re-ingest'te DİRİLMEZ ==")
r8 = memory.ingest(TENANT, "konuşma", facts=["Kullanıcı kahveyi sever"])
check(r8.get("blocked", 0) >= 1 and r8["added"] == 0, f"re-ingest engellendi (blocked, {r8})")
check(store.count_memories(TENANT) == 1, "diriltme yok (hâlâ 1 aktif)")

print("== 9) top_k=0 -> boş ==")
check(memory.search(TENANT, "herhangi", top_k=0) == [], "top_k=0 boş döner")

print("== 10) content-level erasure (başka user_id ile de dirilmez) ==")
r10 = memory.ingest(TENANT, "x", facts=["Kullanıcı kahveyi sever"], user_id="baska-user")
check(r10.get("blocked", 0) >= 1 and r10["added"] == 0, f"farklı user_id'de de bloklu ({r10})")
check(store.count_memories(TENANT, "baska-user") == 0, "başka user'da diriltme yok")

print("== 11) paraphrase semantik tombstone ile bloklu ==")
r11 = memory.ingest(TENANT, "x", facts=["Kahve harika bir içecek"], user_id="u3")  # 'kahve' -> aynı vektör
check(r11.get("blocked", 0) >= 1, f"forget edilen içeriğin paraphrase'i bloklu ({r11})")

print("== 12) audit-final #3: embed yokken semantik-tombstone varsa ingest fail-CLOSED ==")
_orig = embeddings.embed
embeddings.embed = lambda t: None          # embed hattı düştü
try:
    r12 = memory.ingest(TENANT, "x", facts=["Tamamen yeni alakasız bilgi"], user_id="zz")
    check(r12.get("blocked", 0) >= 1 and r12["added"] == 0,
          f"embed yok + tombstone var -> blocked (paraphrase sızmasın) ({r12})")
    store.upsert_tenant("t_noerase", "N", "kn")
    r12b = memory.ingest("t_noerase", "x", facts=["bilgi"])  # tombstone YOK
    check(r12b["added"] == 1, "tombstone yokken embed-down ingest'i bloklamaz (scoped)")
finally:
    embeddings.embed = _orig

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM FACT-MEMORY TESTLERİ GEÇTİ")
