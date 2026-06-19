#!/usr/bin/env python3
"""portability.py — Export / Import (B): bir tenant'ın TÜM hafıza+kurallarını taşınabilir
bir bundle'a çıkar / başka bir tenant'a geri yükle. Backup-restore, tenant->tenant taşıma,
instance göçü. KVKK "verini al-götür" (data portability) hakkıyla da örtüşür.

  export_tenant(tenant) -> dict (bundle)
  import_tenant(target, bundle, mode) -> dict (counts)

GÜVENLİK invariantları:
  - Export ASLA secret içermez: api_keys / tenants.api_key / subscriptions / usage HARİÇ.
    Sadece BİLGİ taşınır (memory, rules, corrections, tombstones). İçerik egress'te scrub'lanır.
  - Import yalnız ALLOWLIST anahtarları okur (bundle'daki sahte 'api_keys' vb. yok sayılır).
  - Import ERASURE'ı korur: tombstone'lar ÖNCE yüklenir, memory'ler memory.import_records
    (ingest ile aynı tombstone guard) üzerinden gider -> forget edilen içerik dirilmez.
  - Tüm yazımlar `target` tenant'a izole; kaynak tenant body'den DEĞİL çağrı argümanından.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory  # noqa: E402
import store  # noqa: E402
from redact import scrub_obj  # noqa: E402

BUNDLE_VERSION = 1
MAX_RECORDS = int(os.environ.get("CCE_IMPORT_MAX_RECORDS", "5000"))    # tablo başına tavan (DoS)
MAX_TOTAL = int(os.environ.get("CCE_IMPORT_MAX_TOTAL", "20000"))       # tüm tablolar toplam tavan
MAX_EMB_DIM = int(os.environ.get("CCE_IMPORT_MAX_EMB_DIM", "4096"))    # tombstone emb boyut tavanı (O(M*T*D) sınırı)
_VALID_MODES = {"merge", "replace"}


def _scrub_record(rec: dict, keep: tuple = ("embedding",)) -> dict:
    """Bir export kaydının TÜM string alanlarını (key+value, iç içe) scrub'la. `keep`
    alanları (embedding float vektörü) taranmaz — secret içermez, taramak gereksiz/maliyetli."""
    kept = {k: rec[k] for k in keep if k in rec}
    out = scrub_obj({k: v for k, v in rec.items() if k not in keep})
    out.update(kept)
    return out


def export_tenant(tenant_id: str) -> dict:
    """Taşınabilir bundle üret. Secret YOK (sadece bilgi).
    EGRESS scrub COMPREHENSIVE: her kaydın TÜM string alanları (key+value, iç içe) `_scrub_record`
    ile taranır — alan-alan scrub'ın kaçırdığı metadata-key/category/tags/project/correction_id
    gibi kanalları kapatır (audit fix-verify residual'ları). embedding/sayısal alan taranmaz."""
    mems = []
    for m in store.get_active_memories(tenant_id):   # sadece aktif (forget'lenmiş zaten shred)
        if not str(m.get("content") or "").strip():
            continue
        mems.append(_scrub_record({
            "content": m.get("content"), "embedding": m.get("embedding"),
            "user_id": m.get("user_id") or "default", "salience": m.get("salience"),
            "source": m.get("source"), "metadata": m.get("metadata") or {},
            "created_at": m.get("created_at")}))

    rules = []
    for r in store.get_rules(tenant_id, active_only=False):   # disabled dahil (yapışkanlık korunsun)
        rules.append(_scrub_record({
            "id": r.get("id"), "project": r.get("project"), "type": r.get("type"),
            "severity": r.get("severity"), "source": r.get("source"),
            "correction_id": r.get("correction_id"), "message": r.get("message"),
            "rule": r.get("rule"), "forbid_patterns": r.get("forbid_patterns") or [],
            "require_patterns": r.get("require_patterns") or [], "status": r.get("status")}))

    corrections = []
    for c0 in store.get_corrections(tenant_id):
        corrections.append(_scrub_record({
            "id": c0.get("id"), "project": c0.get("project"), "category": c0.get("category"),
            "what_was_wrong": c0.get("what_was_wrong"), "correct_behavior": c0.get("correct_behavior"),
            "reusable_rule": c0.get("reusable_rule"), "severity": c0.get("severity"),
            "tags": c0.get("tags") or []}))

    tombs = store.get_tombstones(tenant_id)   # erasure'ı taşı (forget hedefte de geçerli kalsın)

    return {
        "bundle_version": BUNDLE_VERSION,
        "exported_at": time.time(),
        "source_tenant": tenant_id,
        "memories": mems,
        "rules": rules,
        "corrections": corrections,
        "tombstones": [{"content_hash": t["content_hash"], "embedding": t.get("embedding")}
                       for t in tombs],
        "counts": {"memories": len(mems), "rules": len(rules),
                   "corrections": len(corrections), "tombstones": len(tombs)},
    }


def _as_list(bundle: dict, key: str) -> list:
    v = bundle.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError(f"{key} must be a list")
    if len(v) > MAX_RECORDS:
        raise ValueError(f"{key} too large (>{MAX_RECORDS})")
    return v


def import_tenant(target_tenant_id: str, bundle: dict, mode: str = "merge") -> dict:
    """Bundle'ı `target_tenant_id`'ye yükle. mode: 'merge' (ekle) | 'replace' (önce bilgiyi temizle).

    Sıra KRİTİK: (1) tombstone'lar ÖNCE -> (2) memory'ler erasure-guard'lı import ->
    (3) rules -> (4) corrections. Böylece forget edilen içerik import'ta dirilmez."""
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be an object")
    if bundle.get("bundle_version") not in (None, BUNDLE_VERSION):
        raise ValueError(f"unsupported bundle_version: {bundle.get('bundle_version')}")
    if not isinstance(mode, str) or mode not in _VALID_MODES:  # non-str mode TypeError'a düşmesin
        raise ValueError(f"unknown mode: {mode}")

    # allowlist: sadece bu anahtarlar okunur (sahte api_keys/tenants yok sayılır)
    mems = _as_list(bundle, "memories")
    rules = _as_list(bundle, "rules")
    corrections = _as_list(bundle, "corrections")
    tombs = _as_list(bundle, "tombstones")
    if len(mems) + len(rules) + len(corrections) + len(tombs) > MAX_TOTAL:  # toplam kap (DoS)
        raise ValueError(f"bundle too large (>{MAX_TOTAL} total records)")

    cleared = None
    if mode == "replace":
        cleared = store.clear_knowledge(target_tenant_id)

    # (1) tombstone'lar ÖNCE — memory import'u bunlara karşı kontrol edilsin (erasure korunur)
    tomb_n = 0
    for t in tombs:
        if not isinstance(t, dict):
            continue
        ch = t.get("content_hash")
        if not isinstance(ch, str) or not ch:
            continue
        emb = t.get("embedding")
        # emb boyut tavanı: O(M*T*D) cosine patlamasını sınırla
        store.add_tombstone(target_tenant_id, ch,
                            emb if (isinstance(emb, list) and len(emb) <= MAX_EMB_DIM) else None)
        tomb_n += 1

    # (2) memory'ler — ingest ile AYNI tombstone guard (forget edilen dirilmez), izole
    mem_res = memory.import_records(target_tenant_id, mems, source="import")

    # (3) kurallar — store.add_rule canonicalize/merge eder (disabled-yapışkan, monotonik severity)
    rule_n = 0
    for r in rules:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        rr = dict(r)
        rr["tenant_id"] = target_tenant_id          # izolasyon: hedefe sabitle
        store.add_rule(rr)
        rule_n += 1

    # (4) corrections (audit kaynağı)
    corr_n = 0
    for c0 in corrections:
        if not isinstance(c0, dict) or not c0.get("id"):
            continue
        cc = dict(c0)
        cc["tenant_id"] = target_tenant_id
        store.add_correction(cc)
        corr_n += 1

    store.record_admin(target_tenant_id, "import", bundle.get("source_tenant", "") or "",
                       detail=f"mode={mode} mem+{mem_res['added']} rules+{rule_n}")
    out = {"mode": mode, "tombstones": tomb_n, "memories": mem_res, "rules": rule_n,
           "corrections": corr_n}
    if cleared is not None:
        out["cleared"] = cleared
    return out
