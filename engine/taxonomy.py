#!/usr/bin/env python3
"""taxonomy.py — AACRP otonom taksonomi indüksiyonu (TOPIC ekseni).

induce(tenant): tenant'ın hafızasından MODELİN KENDİSİ kategori ağacını çıkarır
(biz açmayız) ve tüm kayıtları o ağaca route eder. LLM-tabanlı (gpt-4o-mini):
  1) çeşitli örnekten ~8-15 tenant'a-özgü kategori öner (developer != restoran),
  2) tüm kayıtları batch-classify ile kategorilere ata (~25x az çağrı),
  3) taksonomi + her kaydın node/path'ini sakla.
Maliyet ölçüldü: gpt-4o-mini ~10k kayıt başına ~$1. Embedding-only %43 yetersizdi.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import routing  # noqa: E402
import store  # noqa: E402
from llm import LLMError, call_model, llm_available  # noqa: E402
from redact import scrub_secrets  # noqa: E402

MIN_FOR_INDUCE = 8     # bu sayının altında induce etme (cold-start: çeşitlilik yok)
SAMPLE = 40            # kategori önerisi için örnek sayısı
BATCH = 18             # batch-classify başına metin


def propose_taxonomy(samples: list[str], want: str = "6-10") -> list[dict]:
    """Örnek notlardan tenant'a-özgü kategori öner (gpt-4o-mini). Dönüş: [{slug,label}].
    Parçalanmayı önle: az sayıda, ÖRTÜŞMEYEN, içeriği KAPSAYAN kategoriler."""
    joined = "\n".join(f"- {scrub_secrets(s)[0][:200]}" for s in samples[:SAMPLE] if (s or "").strip())
    sys_p = (f"Bir kişinin/işletmenin notlarını organize etmek için {want} KATEGORİ öner. "
             "Kategoriler bu kişinin ALANINA özgü olsun (yazılımcıysa proje/teknik, restoransa menü/tedarik gibi). "
             "KURALLAR: (1) Birbiriyle ÖRTÜŞEN kategoriler AÇMA — benzer konuları TEK kategoride birleştir "
             "(örn. 'Teknik', 'Geliştirme', 'Dokümantasyon' hepsi tek kategori olmalı). "
             "(2) Verilen notların TÜM konularını kapsa, hiçbir konuyu dışarıda bırakma. "
             "(3) Çok dar/tekil kategori açma. Somut ama geniş ol. "
             'SADECE JSON dizi döndür: [{"slug":"kisa-slug","label":"Okunabilir Ad"}]. Başka hiçbir şey yazma.')
    raw = call_model(sys_p, joined, max_tokens=700)
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    out, seen = [], set()
    for x in arr:
        if isinstance(x, dict) and x.get("label"):
            lab = scrub_secrets(str(x["label"]))[0][:60].strip()
            slug = scrub_secrets(str(x.get("slug") or lab))[0][:40].strip().lower().replace(" ", "-")
            if not lab or lab.lower() in seen:
                continue
            seen.add(lab.lower())
            out.append({"slug": slug or ("k" + str(len(out))), "label": lab})
    return out[:20]


def induce(tenant_id: str) -> dict:
    """Otomatik taksonomi çıkar + tüm kayıtları route et. Mevcut taksonomiyi DEĞİŞTİRİR."""
    if not llm_available():
        raise LLMError("induce icin LLM hatti yok")
    mems = [m for m in store.get_active_memories(tenant_id) if (m.get("content") or "").strip()]
    if len(mems) < MIN_FOR_INDUCE:
        return {"ok": False, "error": "too_few_memories", "count": len(mems), "min": MIN_FOR_INDUCE}

    step = max(1, len(mems) // SAMPLE)
    sample = [mems[i]["content"] for i in range(0, len(mems), step)][:SAMPLE]
    nodes = propose_taxonomy(sample)
    if not nodes:
        return {"ok": False, "error": "taxonomy_proposal_failed"}

    def _node_id(label: str) -> str:
        # id LABEL'dan türetilir (slug DEĞİL): slug[:40] truncation iki farklı label'ı aynı id'ye
        # düşürüp duplicate-PK ile TÜM induce'i çökertiyordu. label-dedup ile tutarlı.
        return "tax-" + hashlib.sha1(f"{tenant_id}|{label.strip().lower()}".encode("utf-8")).hexdigest()[:12]

    seen_ids: set = set()
    deduped = []
    for n in nodes:
        nid = _node_id(n["label"])
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        n["id"] = nid
        n["path"] = n["label"]
        n["member_count"] = 0
        deduped.append(n)
    nodes = deduped
    by_label = {n["label"]: n for n in nodes}
    labels = [n["label"] for n in nodes]

    texts = [m["content"] for m in mems]

    def classify_all(txts: list[str], labs: list[str]) -> list:
        out: list = []
        for s in range(0, len(txts), BATCH):
            out.extend(routing.classify_batch(txts[s:s + BATCH], labs))
        return out

    assigns = classify_all(texts, labels)

    def _label_counts() -> dict:
        c: dict = {}
        for lab in assigns:
            if by_label.get(lab):
                c[lab] = c.get(lab, 0) + 1
        return c

    # 2. GEÇİŞ — düşen alan kurtarma: sınıflandırılamayanlar bir küme oluşturuyorsa
    # (öneri o konuyu kaçırmış, ör. sağlık) onlardan EK kategori öner + sadece onları yeniden route et.
    # Mutlak eşik (5+): 300-not vault'ta 27 unsorted gibi kümeleri yakalar (%10 eşiği kaçırıyordu).
    uns_idx = [i for i, lab in enumerate(assigns) if not by_label.get(lab)]
    if len(uns_idx) >= 5:
        extra = propose_taxonomy([texts[i] for i in uns_idx], want="2-5")
        for e in extra:
            eid = _node_id(e["label"])
            if e["label"] not in by_label and eid not in seen_ids:   # hem label hem id çakışmasını engelle
                seen_ids.add(eid)
                e["id"] = eid
                e["path"] = e["label"]
                e["member_count"] = 0
                nodes.append(e)
                by_label[e["label"]] = e
        re_assign = classify_all([texts[i] for i in uns_idx], [n["label"] for n in nodes])
        for k, i in enumerate(uns_idx):
            assigns[i] = re_assign[k]

    # TIGHTENING — minik kategorileri (üye < MERGE_MIN) BÜYÜK kategorilere yeniden-route et, sonra at.
    # Parçalanmayı kırar (singleton/2-üyeli kovalar). Uymayan -> unsorted (singleton'dan iyi).
    MERGE_MIN = 3
    counts0 = _label_counts()
    big = [lab for lab, c in counts0.items() if c >= MERGE_MIN]
    tiny = {lab for lab, c in counts0.items() if c < MERGE_MIN}
    if big and tiny:
        move_idx = [i for i, lab in enumerate(assigns) if lab in tiny]
        re3 = classify_all([texts[i] for i in move_idx], big)
        for k, i in enumerate(move_idx):
            assigns[i] = re3[k]

    rows, counts = [], {}
    for m, lab in zip(mems, assigns):
        node = by_label.get(lab)
        nid = node["id"] if node else None
        if node:
            counts[nid] = counts.get(nid, 0) + 1
        rows.append({"id": m["id"], "node_id": nid,
                     "path": node["path"] if node else "Sınıflandırılmamış",
                     "tags": [], "confidence": 1.0 if node else 0.0})

    # BOŞ kategorileri at (üye=0) — parçalanma/boş-kova göstermeyi önle
    nodes = [n for n in nodes if counts.get(n["id"], 0) > 0]
    for n in nodes:
        n["member_count"] = counts.get(n["id"], 0)

    store.replace_taxonomy(tenant_id, nodes)
    store.bulk_set_routing(tenant_id, rows)
    unsorted = sum(1 for r in rows if not r["node_id"])
    return {"ok": True, "total": len(mems), "unsorted": unsorted,
            "nodes": [{"label": n["label"], "slug": n["slug"], "members": n["member_count"]}
                      for n in sorted(nodes, key=lambda x: -x["member_count"])]}
