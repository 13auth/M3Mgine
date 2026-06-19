"""test_async.py — Faz 2 async ingestion kuyruğu. key'siz: embed stub, enjekte facts/triples.
enqueue -> worker -> done + veri var; claim atomikliği (iki claim aynı işi almaz); dead-letter."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_async.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))
import hashlib  # noqa: E402

import embeddings  # noqa: E402
import store  # noqa: E402
import worker  # noqa: E402


def _stub_embed(t):
    # 'kahve' -> sabit vektör (idempotency/dedup testleri için); diğerleri METİN-BAŞINA AYIRT EDİLİR
    # (hash byte'larından, distinct metin -> cosine<0.95 -> dedup OLMAZ; aynı metin -> birebir -> dedup).
    s = (t or "").lower()
    if "kahve" in s:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    h = hashlib.sha1(s.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:8]]


embeddings.embed = _stub_embed

T = "t_async"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "Async", "k_async")

print("== 1) enqueue (memory + kg) -> queued ==")
jm = store.enqueue_job(T, "memory", {"facts": ["Kullanıcı kahveyi sever"], "user_id": "u"})
jk = store.enqueue_job(T, "kg", {"triples": [{"subject": "Ali", "predicate": "works_at",
                                              "object": "Acme", "object_type": "entity",
                                              "fact": "Ali Acme"}]})
check(store.get_job(T, jm)["status"] == "queued" and store.get_job(T, jk)["status"] == "queued",
      "iki iş queued")

print("== 2) worker drenajı -> done + veri yazıldı ==")
n = worker.run(max_idle_polls=1)
check(n >= 2, f"worker en az 2 iş işledi ({n})")
check(store.get_job(T, jm)["status"] == "done", "memory job done")
check(store.get_job(T, jk)["status"] == "done", "kg job done")
check(store.get_job(T, jm)["result"]["added"] == 1, "memory job sonucu: 1 eklendi")
check(store.count_memories(T, "u") == 1, "memory gerçekten yazıldı (recall edilebilir)")
ali = store.get_entity_by_norm(T, "ali")
check(ali is not None and len(store.get_edges(T, subject_id=ali["id"], current_only=True)) == 1,
      "kg kenarı gerçekten yazıldı")

print("== 3) claim atomikliği (SKIP LOCKED): iki claim AYNI işi almaz ==")
a = store.enqueue_job(T, "memory", {"facts": ["A"]})
b = store.enqueue_job(T, "memory", {"facts": ["B"]})
c1 = store.claim_job()
c2 = store.claim_job()
check(c1 and c2 and c1["id"] != c2["id"], f"iki farklı iş claim edildi ({c1['id'] if c1 else None}, {c2['id'] if c2 else None})")
check(store.get_job(T, c1["id"])["status"] == "running", "claim edilen iş 'running'")
check(store.claim_job() is None, "başka queued iş yok (hepsi claim'lendi)")
store.complete_job(T, c1["id"], {"ok": True}); store.complete_job(T, c2["id"], {"ok": True})

print("== 4) dead-letter: kalıcı hata MAX denemeden sonra 'error' ==")
bad = store.enqueue_job(T, "memory", {"facts": []})  # geçerli ama; kötü kind ile test edelim:
bad2 = store.enqueue_job(T, "BADKIND", {"x": 1})
worker.run(max_idle_polls=1)
jb = store.get_job(T, bad2)
check(jb["status"] == "error" and jb["attempts"] == store._MAX_JOB_ATTEMPTS,
      f"kötü iş {store._MAX_JOB_ATTEMPTS} denemeden sonra error ({jb['status']}, {jb['attempts']})")
check(jb.get("error") and "unknown job kind" in jb["error"], "hata mesajı kaydedildi")

print("== 5) idempotency: aynı memory işi tekrar -> diriltme/kopya yok ==")
j2 = store.enqueue_job(T, "memory", {"facts": ["Kullanıcı kahveyi sever"], "user_id": "u"})
worker.run(max_idle_polls=1)
check(store.get_job(T, j2)["status"] == "done" and store.count_memories(T, "u") == 1,
      "tekrar işlenen fact kopya açmadı (idempotent)")

print("== 6) tenant izolasyonu (job başka tenant'a sızmaz) ==")
check(store.get_job("t_other", jm) is None, "başka tenant job'ı göremez")

print("== 7) audit-fix: stale-running reaper (worker crash kalıntısı yeniden kuyruğa) ==")
jr = store.enqueue_job(T, "memory", {"facts": ["reaper test"], "user_id": "r"})
claimed = store.claim_job()   # bir işi 'running' yap (crash simülasyonu: complete edilmez)
check(store.get_job(T, claimed["id"])["status"] == "running", "iş running")
nrec = store.reclaim_stale_jobs(lease_seconds=-1.0)   # negatif lease -> tüm running'leri kurtar
check(nrec >= 1 and store.get_job(T, claimed["id"])["status"] == "queued",
      f"stale running iş queued'a döndü ({nrec})")
worker.run(max_idle_polls=1)   # reaper sonrası yeniden işlenir
check(store.get_job(T, claimed["id"])["status"] == "done", "reclaim edilen iş sonunda done")

print("== 8) audit-fix: async metering (worker kalan fact'leri metele, kota baypası yok) ==")
import billing  # noqa: E402
store.set_plan(T, "growth")
period = billing.current_period()
before = store.get_usage(T, period)
store.enqueue_job(T, "memory", {"facts": ["metre biri", "metre iki", "metre uc"], "user_id": "mu"})
worker.run(max_idle_polls=1)
after = store.get_usage(T, period)
check(after - before >= 2, f"3-fact job kalan ~2 op metelendi (delta={after - before})")

print()
if FAILS:
    print(f"SONUC: {len(FAILS)} BASARISIZ"); sys.exit(1)
print("SONUC: TUM ASYNC INGESTION TESTLERI GECTI")
