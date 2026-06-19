"""test_kg.py — Temporal Knowledge Graph (temporal KG çekirdek). key'siz: embed stub."""
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_kg.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import embeddings  # noqa: E402
import kg  # noqa: E402
import store  # noqa: E402


def _stub_embed(text):
    t = (text or "").lower()
    v = [0.0, 0.0, 0.0, 0.0]
    if "ali" in t:
        v[0] = 1.0
    if "acme" in t:
        v[1] = 1.0
    if "exampleco" in t:
        v[2] = 1.0
    if "python" in t:
        v[3] = 1.0
    return v


embeddings.embed = _stub_embed

T = "t_kg"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "KG", "k_kg")

# zaman çizgisi (DÜNYA): T1<T2<T3<T4
T1, T2, T3, T4 = 1000.0, 2000.0, 3000.0, 4000.0

print("== 1) ingest üçlü -> varlık + kenar ==")
r1 = kg.ingest(T, triples=[{"subject": "Ali", "subject_type": "person", "predicate": "works_at",
                            "object": "Acme", "object_type": "entity",
                            "fact": "Ali Acme'da çalışır"}], valid_at=T1)
check(r1["added"] == 1 and r1["entities_created"] == 2, f"1 kenar + 2 varlık ({r1})")
check(len(store.get_edges(T, current_only=True)) == 1, "1 güncel kenar")

print("== 2) entity resolution: aynı ad -> aynı düğüm (yeni varlık açmaz) ==")
r2 = kg.ingest(T, triples=[{"subject": "Ali", "predicate": "role_is", "object": "tasarımcı",
                            "object_type": "literal", "fact": "Ali tasarımcı"}], valid_at=T1)
check(r2["entities_created"] == 0, f"Ali tekrar -> yeni varlık yok ({r2['entities_created']})")
check(len(store.list_entities(T)) == 2, "hâlâ 2 varlık (Ali, Acme)")

print("== 3) SEMANTİK entity resolution: 'Ali Veli' -> Ali düğümü ==")
r3 = kg.ingest(T, triples=[{"subject": "Ali Veli", "predicate": "knows", "object": "Python",
                            "object_type": "entity", "fact": "Ali Python biliyor"}], valid_at=T1)
ents = {e["name"]: e["id"] for e in store.list_entities(T)}
check("Ali Veli" not in ents, "'Ali Veli' ayrı düğüm açılmadı (semantik merge)")
ali_id = store.get_entity_by_norm(T, "ali")["id"]
knows = store.get_edges(T, subject_id=ali_id, predicate="knows", current_only=True)
check(len(knows) == 1, "knows kenarı Ali düğümüne bağlandı")

print("== 4) ÇELİŞKİ invalidation: works_at Acme -> ExampleCo (eski geçersizlenir) ==")
r4 = kg.ingest(T, triples=[{"subject": "Ali", "predicate": "works_at", "object": "ExampleCo",
                            "object_type": "entity", "fact": "Ali ExampleCo'da çalışır"}], valid_at=T2)
check(r4["invalidated"] == 1 and r4["added"] == 1, f"eski works_at geçersizlendi ({r4})")
cur = store.get_edges(T, subject_id=ali_id, predicate="works_at", current_only=True)
check(len(cur) == 1 and (store.get_entity(T, cur[0]["object_id"]) or {}).get("name") == "ExampleCo",
      "güncel works_at = ExampleCo (Acme değil)")

print("== 5) Bİ-TEMPORAL point-in-time: T1-T2 arası Acme doğruydu ==")
asof = store.get_edges_asof(T, 1500.0)
wa = [e for e in asof if e["predicate"] == "works_at"]
check(len(wa) == 1 and (store.get_entity(T, wa[0]["object_id"]) or {}).get("name") == "Acme",
      "T=1500'de works_at = Acme (tarih korundu)")
hits_past = kg.search(T, "Ali nerede çalışıyor", as_of=1500.0, top_k=3)
check(any("Acme" in (h.get("object") or "") for h in hits_past), "as_of search Acme döndürdü")

print("== 6) çok-değerli yüklem: likes coffee + likes tea BİR ARADA (çelişki değil) ==")
kg.ingest(T, triples=[{"subject": "Ali", "predicate": "likes", "object": "coffee",
                       "object_type": "literal", "fact": "Ali kahve sever"}], valid_at=T3)
r6 = kg.ingest(T, triples=[{"subject": "Ali", "predicate": "likes", "object": "tea",
                            "object_type": "literal", "fact": "Ali çay sever"}], valid_at=T4)
likes = store.get_edges(T, subject_id=ali_id, predicate="likes", current_only=True)
check(r6["invalidated"] == 0 and len(likes) == 2, f"likes çoklu -> 2 güncel ({len(likes)})")

print("== 7) re-assertion no-op: aynı güncel fact tekrar -> yeni kenar yok ==")
before = len(store.get_edges(T, current_only=False))
kg.ingest(T, triples=[{"subject": "Ali", "predicate": "works_at", "object": "ExampleCo",
                       "object_type": "entity", "fact": "Ali ExampleCo'da çalışır"}], valid_at=T2)
check(len(store.get_edges(T, current_only=False)) == before, "aynı fact tekrar -> kopya kenar yok")

print("== 8) hybrid retrieval: güncel doğru fact üste ==")
hits = kg.search(T, "Ali nerede çalışıyor", top_k=5)
top_wa = [h for h in hits if h["predicate"] == "works_at"]
check(top_wa and top_wa[0]["object"] == "ExampleCo", f"güncel works_at = ExampleCo döndü ({top_wa[:1]})")

print("== 9) neighbors: düğümün güncel kenarları ==")
nb = kg.neighbors(T, "Ali")
preds = {n["predicate"] for n in nb}
check({"works_at", "role_is", "knows", "likes"} <= preds, f"komşu yüklemler ({preds})")
check(not any(n.get("object") == "Acme" for n in nb), "geçersizlenen Acme komşularda yok")

print("== 10) tenant izolasyonu ==")
check(len(store.list_entities("t_other")) == 0 and kg.search("t_other", "Ali") == [],
      "başka tenant grafiği görmüyor")


def _obj_name(e):
    return (store.get_entity(T, e["object_id"]) or {}).get("name") if e.get("object_id") else e.get("object_value")


print("== 11) boomerang A->B->A güncel=A, tarih korunur, added DOĞRU ==")
kg.ingest(T, triples=[{"subject": "Bob", "predicate": "works_at", "object": "Acme",
                       "object_type": "entity", "fact": "Bob Acme"}], valid_at=100.0)
kg.ingest(T, triples=[{"subject": "Bob", "predicate": "works_at", "object": "Beta",
                       "object_type": "entity", "fact": "Bob Beta"}], valid_at=200.0)
b3 = kg.ingest(T, triples=[{"subject": "Bob", "predicate": "works_at", "object": "Acme",
                            "object_type": "entity", "fact": "Bob Acme yine"}], valid_at=300.0)
bob = store.get_entity_by_norm(T, "bob")["id"]
cur = store.get_edges(T, subject_id=bob, predicate="works_at", current_only=True)
check(b3["added"] == 1 and len(cur) == 1, f"boomerang: 1 güncel + added doğru ({b3}, n={len(cur)})")
check(_obj_name(cur[0]) == "Acme", "boomerang güncel = Acme (geri döndü, kaybolmadı)")
a150 = [e for e in store.get_edges_asof(T, 150.0) if e["subject_id"] == bob]
a250 = [e for e in store.get_edges_asof(T, 250.0) if e["subject_id"] == bob]
check(a150 and _obj_name(a150[0]) == "Acme" and a250 and _obj_name(a250[0]) == "Beta",
      "tarih korundu: asof150=Acme, asof250=Beta")

print("== 12) backfill/out-of-order yeniyi SİLMEZ, ters aralık YOK ==")
kg.ingest(T, triples=[{"subject": "Cem", "predicate": "lives_in", "object": "Izmir",
                       "object_type": "entity", "fact": "Cem Izmir"}], valid_at=300.0)
kg.ingest(T, triples=[{"subject": "Cem", "predicate": "lives_in", "object": "Ankara",
                       "object_type": "entity", "fact": "Cem Ankara eski"}], valid_at=100.0)
cem = store.get_entity_by_norm(T, "cem")["id"]
curc = store.get_edges(T, subject_id=cem, predicate="lives_in", current_only=True)
check(len(curc) == 1 and _obj_name(curc[0]) == "Izmir", "backfill sonrası güncel hâlâ Izmir (yeni korundu)")
allc = store.get_edges(T, subject_id=cem, predicate="lives_in", current_only=False)
check(all(e["invalid_at"] is None or e["invalid_at"] >= e["valid_at"] for e in allc), "ters/negatif aralık yok")
ac150 = [e for e in store.get_edges_asof(T, 150.0) if e["subject_id"] == cem]
check(ac150 and _obj_name(ac150[0]) == "Ankara", "asof150 = Ankara (backfill dönemi doğru)")

print("== 13) future-dated 'şu an'ı değiştirmez (current valid_at-duyarlı) ==")
NOW = time.time()
kg.ingest(T, triples=[{"subject": "Cep", "predicate": "works_at", "object": "NowCorp",
                       "object_type": "entity", "fact": "Cep NowCorp"}], valid_at=NOW - 100)
kg.ingest(T, triples=[{"subject": "Cep", "predicate": "works_at", "object": "FutureCorp",
                       "object_type": "entity", "fact": "Cep FutureCorp"}], valid_at=NOW + 1e6)
cep = store.get_entity_by_norm(T, "cep")["id"]
curf = store.get_edges(T, subject_id=cep, predicate="works_at", current_only=True)
check(len(curf) == 1 and _obj_name(curf[0]) == "NowCorp", "şu an = NowCorp (gelecek-tarihli güncel sayılmadı)")
af = [e for e in store.get_edges_asof(T, NOW + 1e6) if e["subject_id"] == cep]
check(af and _obj_name(af[0]) == "FutureCorp", "asof(gelecek) = FutureCorp")

print("== 14) aynı valid_at -> ilk korunur, ters/sıfır-genişlik korupsiyonu yok ==")
kg.ingest(T, triples=[{"subject": "Deniz", "predicate": "role_is", "object": "X",
                       "object_type": "literal", "fact": "Deniz X"}], valid_at=5000.0)
kg.ingest(T, triples=[{"subject": "Deniz", "predicate": "role_is", "object": "Y",
                       "object_type": "literal", "fact": "Deniz Y"}], valid_at=5000.0)
den = store.get_entity_by_norm(T, "deniz")["id"]
ad = [e for e in store.get_edges_asof(T, 5000.0) if e["subject_id"] == den]
check(len(ad) == 1 and (ad[0].get("object_value") == "X"), f"aynı-an: ilk (X) point-in-time görünür ({ad})")

print("== 15) Türkçe 'kullanıyor' (fold) çok-değerli kalır (yanlış invalidation yok) ==")
kg.ingest(T, triples=[{"subject": "Ela", "predicate": "kullanıyor", "object": "Python",
                       "object_type": "entity", "fact": "Ela Python kullanıyor"}], valid_at=100.0)
re15 = kg.ingest(T, triples=[{"subject": "Ela", "predicate": "kullanıyor", "object": "Rust",
                              "object_type": "entity", "fact": "Ela Rust kullanıyor"}], valid_at=200.0)
ela = store.get_entity_by_norm(T, "ela")["id"]
ek = store.get_edges(T, subject_id=ela, current_only=True)
check(re15["invalidated"] == 0 and len(ek) == 2, f"Türkçe çok-değerli: 2 birlikte ({re15}, n={len(ek)})")

print("== 16) per-tenant entity cap fail-closed ==")
_orig_cap = kg.MAX_KG_ENTITIES
kg.MAX_KG_ENTITIES = 2
try:
    store.upsert_tenant("t_cap", "Cap", "k_cap")
    kg.ingest("t_cap", triples=[{"subject": "S1", "predicate": "rel", "object": "O1",
                                 "object_type": "entity", "fact": "f1"}], valid_at=1.0)
    rc = kg.ingest("t_cap", triples=[{"subject": "S2", "predicate": "rel", "object": "O2",
                                      "object_type": "entity", "fact": "f2"}], valid_at=2.0)
    check(store.count_entities("t_cap") == 2 and rc["added"] == 0,
          f"özne cap'te yeni düğüm/kenar açılmadı ({store.count_entities('t_cap')}, {rc})")
    # fix-verify yeni-bulgu: özne CAP-ALTI ama entity-NESNE cap-üstü -> orphan edge YAZMA (fail-closed)
    rc2 = kg.ingest("t_cap", triples=[{"subject": "S1", "predicate": "rel2", "object": "O_NEW",
                                       "object_type": "entity", "fact": "f3"}], valid_at=3.0)
    orphans = [e for e in store.get_edges("t_cap", current_only=False) if not e.get("object_id")
               and e.get("object_value") is None]
    check(rc2["added"] == 0 and not orphans,
          f"nesne cap -> orphan edge yok, added=0 ({rc2}, orphans={len(orphans)})")
finally:
    kg.MAX_KG_ENTITIES = _orig_cap

print("== 17) entity type-guard — semantik homonim çökmesi (farklı tip birleşmez) ==")
TH = "t_homonym"
store.upsert_tenant(TH, "H", "k_h")
# 'Ali Yardımcı'(person) ve 'Ali Holding'(organization): stub embed ikisine de [1,0,0,0] verir
# (cosine=1.0) AMA tipler farklı -> BİRLEŞMEMELİ (eskiden çökerdi).
kg.ingest(TH, triples=[{"subject": "Ali Yardımcı", "subject_type": "person", "predicate": "role_is",
                        "object": "danışman", "object_type": "literal", "fact": "Ali Yardımcı danışman"}], valid_at=T1)
kg.ingest(TH, triples=[{"subject": "Ali Holding", "subject_type": "organization", "predicate": "located_in",
                        "object": "Ankara", "object_type": "entity", "fact": "Ali Holding Ankara"}], valid_at=T1)
hum = [e for e in store.list_entities(TH) if e["name"] in ("Ali Yardımcı", "Ali Holding")]
check(len(hum) == 2, f"farklı-tip homonim ayrı kaldı (person vs organization) ({len(hum)})")
# kontrol: AYNI tip + aynı embedding -> birleşmeli (aşırı-düzeltme yok)
kg.ingest(TH, triples=[{"subject": "Ali Holding AŞ", "subject_type": "organization", "predicate": "located_in",
                        "object": "Ankara", "object_type": "entity", "fact": "Ali Holding AŞ Ankara"}], valid_at=T2)
orgs = [e for e in store.list_entities(TH) if (e.get("type") or "").lower() == "organization"]
check(len(orgs) == 1, f"aynı-tip + aynı-embedding birleşti (aşırı-düzeltme yok) ({len(orgs)})")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM KNOWLEDGE-GRAPH TESTLERİ GEÇTİ")
