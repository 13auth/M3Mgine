#!/usr/bin/env python3
"""worker.py — async ingestion worker (Faz 2). Kuyruktan iş çeker (store.claim_job, Postgres'te
FOR UPDATE SKIP LOCKED -> çoklu worker güvenli), aynı memory.ingest/kg.ingest'i çağırır, sonucu
yazar. İdempotent: ingest deterministik id kullanır -> aynı işin tekrarı güvenli.

  python worker.py            # sonsuz döngü (üretim; birden çok kopya çalıştırılabilir)
Read path (<150ms) yazma yükünden etkilenmesin diye ağır extract+embed+contradiction buraya alınır.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import billing  # noqa: E402
import kg  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402
import taxonomy  # noqa: E402


def _process(job: dict) -> dict:
    kind = job.get("kind")
    p = job.get("payload") or {}
    tenant = job["tenant_id"]
    if kind == "memory":
        return memory.ingest(tenant, p.get("text", ""), user_id=p.get("user_id", "default"),
                             facts=p.get("facts"), source=p.get("source", "async"))
    if kind == "kg":
        return kg.ingest(tenant, text=p.get("text", ""), triples=p.get("triples"),
                         source=p.get("source", "async"), valid_at=p.get("valid_at"))
    if kind == "induce":   # AACRP: otomatik kategorizasyon (uzun sürer -> async)
        return taxonomy.induce(tenant)
    raise ValueError(f"unknown job kind: {kind}")


def process_one() -> bool:
    """Bir iş işle. True=iş vardı/işlendi, False=kuyruk boş. Hata -> fail_job (retry/dead-letter)."""
    job = store.claim_job()   # cross-tenant (owner); aşağıda işlenen tenant'a bağlam kurulur
    if not job:
        return False
    tok = store.set_tenant(job["tenant_id"])   # RLS: bu iş tenant'ının bağlamı (ingest RLS tablolarına yazar)
    try:
        res = _process(job)
        # METERING: enqueue'da 1 op rezerve edildi; KALANI burada metele (sync /v1/memories ile parity).
        # SADECE yeni 'added' faturalanır (reinforce COGS~0, ücretsiz). induce enqueue'da rezerve edildi -> burada yok.
        extra = max(0, res.get("added", 0) - 1)
        if extra:
            billing.meter(job["tenant_id"], "memory_add", extra)
        p = job.get("payload") or {}   # ham metin -> LLM extraction çalıştı; pahalı op'u ayrı metrele (memory+KG)
        items = p.get("facts") if job.get("kind") == "memory" else p.get("triples")
        if job.get("kind") in ("memory", "kg") and not items and p.get("text"):
            billing.meter(job["tenant_id"], "llm_extract", 1)
        store.complete_job(job["tenant_id"], job["id"], res)
    except Exception as e:   # worker dayanıklı: hiçbir iş döngüyü çökertmesin (retry'de metering YOK)
        store.fail_job(job["tenant_id"], job["id"], f"{type(e).__name__}: {e}",
                       int(job.get("attempts", 1)))
    finally:
        store.reset_tenant(tok)   # RLS bağlamını bırak (bir sonraki işe sızmasın)
    return True


def run(max_idle_polls: int = 0, poll_interval: float = 0.5, reclaim_every: int = 120) -> int:
    """Worker döngüsü. max_idle_polls=0 -> sonsuz (üretim). >0 -> N ardışık boş poll'dan sonra dur
    (test/batch). reclaim_every>0: periyodik stale-running kurtarma (crash dayanıklılığı)."""
    store.reclaim_stale_jobs()   # başlangıçta önceki crash kalıntılarını kurtar
    processed = 0
    idle = 0
    while True:
        if process_one():
            processed += 1
            idle = 0
        else:
            idle += 1
            if reclaim_every and idle % reclaim_every == 0:
                store.reclaim_stale_jobs()
            if max_idle_polls and idle >= max_idle_polls:
                return processed
            time.sleep(poll_interval)


if __name__ == "__main__":
    print(f"worker başladı (backend={store.BACKEND})")
    run()
