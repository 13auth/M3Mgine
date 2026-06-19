#!/usr/bin/env python3
"""memory.py — genel FACT-MEMORY motoru (Faz 1).

"rule-only"den gerçek hafıza platformuna: herhangi bir bilgiyi sakla / getir /
güncelle. Blueprint (araştırma): extraction → dedup → store → hybrid retrieve →
temporal/soft-delete. store + embeddings + llm + redact üstüne kurulu.

  ingest(tenant, text)        -> metinden atomik fact'ler çıkar, dedup'la, sakla
  search(tenant, query)       -> hybrid (semantik+lexical+recency+salience) top-k
  forget(tenant, id)          -> soft-delete (invalid_at)

Üretimde yazma async olmalı (latency); v0 senkron. Embed yoksa lexical'e düşer.
Secret'lar HEM saklamadan HEM LLM'e gitmeden önce scrub'lanır.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import embeddings  # noqa: E402
import pii  # noqa: E402
import routing  # noqa: E402  (inline kategorizasyon — acyclic: routing memory'i import etmez)
import store  # noqa: E402
from llm import LLMError, call_model, llm_available  # noqa: E402
from redact import scrub_obj, scrub_secrets  # noqa: E402

DEDUP_COSINE = 0.95     # bunun üstü = aynı fact (semantik dedup)
RECENCY_TAU_DAYS = 30.0  # recency decay zaman sabiti
W_SEM, W_REC, W_SAL = 0.7, 0.2, 0.1
CANDIDATE_K = int(os.environ.get("CCE_CANDIDATE_K", "256"))  # ANN aday tavanı (push-down)
_IMPORT_MAX_EMB_DIM = int(os.environ.get("CCE_IMPORT_MAX_EMB_DIM", "4096"))  # import emb boyut tavanı (DoS)

EXTRACT_SYS = (
    "Sen bir hafıza çıkarıcısın. Verilen metinden, gelecekte kullanıcı/konu hakkında "
    "bilmeye DEĞER KALICI, atomik, kendine yeten gerçekleri çıkar.\n"
    "ÇIKAR: kalıcı tercih, kimlik, ilişki, sağlık durumu, meslek, konum, kalıcı özellik/kısıt.\n"
    "ÇIKARMA (geçici/önemsiz): selam-nezaket-teşekkür; sorular; ruh hâli (yorgun, stresli, "
    "mutlu, iyiyim); yakın-tarihli veya tek-seferlik olaylar ('yarın sınavım var', 'bugün "
    "toplantı vardı', 'dün spor yaptım'); günlük aktivite ve anlık durumlar.\n"
    "DAYANIKLILIK TESTİ: 'Bu bilgi 6 ay sonra hâlâ doğru ve işe yarar mı?' Hayırsa ÇIKARMA.\n"
    "ÖNEMLİ: nezaket/geçici laf bir cümleye KARIŞMIŞSA bile içindeki kalıcı gerçeği yine "
    "çıkar — sağlık/alerji/kimlik/ilişki gibi gerçekleri ASLA atlama.\n"
    "Örnek girdi: 'Selam! Babam şeker hastası, bugün hava çok yağmurlu.'\n"
    'Örnek çıktı: ["Kullanıcının babası şeker hastası"]  (selam + hava atıldı, sağlık gerçeği tutuldu)\n'
    "Her fact tek cümle, bağlamsız anlaşılır olsun. "
    'SADECE JSON dizi döndür: ["fact 1","fact 2"]. Çıkacak bir şey yoksa [].'
)

_TOK = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", re.UNICODE)


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _mem_id(tenant_id: str, user_id: str, content: str) -> str:
    h = hashlib.sha1(f"{tenant_id}|{user_id}|{_norm(content)}".encode("utf-8")).hexdigest()[:16]
    return "mem-" + h


def _content_hash(content: str) -> str:
    """tenant+user'dan BAĞIMSIZ içerik fingerprint'i. (aynı içerik başka user_id ile
    dirilmesin.) Tenant izolasyonu tombstones.tenant_id KOLONUNDADIR; hash'in kendisi
    tenant-bağımsız ki export/import'ta erasure TAŞINSIN — forget edilen içerik başka
    instance/tenant'a yüklenince de forget'li kalsın (portability erasure invariantı)."""
    return hashlib.sha1(_norm(content).encode("utf-8")).hexdigest()


def _tombstone_blocks(tenant_id: str, fact: str, emb, tomb_hashes: set, tomb_embs: list) -> bool:
    """ERASURE bütünlüğü (ingest + import ORTAK): forget edilen içerik dirilmesin.
      1) content-level: aynı içerik (user'dan bağımsız) tombstone'da mı,
      2) fail-CLOSED: embed hattı yok ama semantik tombstone var -> paraphrase doğrulanamaz, blokla,
      3) semantik: forget edilen vektörün paraphrase'i (cosine>=DEDUP) bloklu.
    Tek noktada tutulur ki import erasure'ı baypas etmesin (güvenlik invariantı)."""
    if _content_hash(fact) in tomb_hashes:
        return True
    if emb is None and tomb_embs:
        return True
    if emb is not None and any(embeddings.cosine(emb, te) >= DEDUP_COSINE for te in tomb_embs):
        return True
    return False


def _terms(text: str) -> list[str]:
    """TR-güvenli token'lar (store._fold: İ/ı dahil locale-bağımsız), len>=3."""
    return [store._fold(t) for t in _TOK.findall(text or "") if len(t) > 2]


def _term_match(q: str, d: str) -> bool:
    """Çekim-eki toleranslı: tam eşleşme VEYA biri diğerinin prefix'i (min 4) — 'rapor'~'raporlar'."""
    return q == d or (min(len(q), len(d)) >= 4 and (d.startswith(q) or q.startswith(d)))


def _bm25(query: str, docs: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """SPARSE sinyali = BM25 (IDF/TF + uzunluk-norm) + TR prefix-stem. Jaccard'ın yerine:
    nadir terim ağırlığı (IDF), uzun-doc cezası yok, çekim eki prefix ile eşleşir (TR morfolojisi)."""
    qts = list(dict.fromkeys(_terms(query)))   # benzersiz sorgu terimleri (sıra korunur)
    n = len(docs)
    if not qts or not n:
        return [0.0] * n
    doc_terms = [_terms(d) for d in docs]
    avgdl = (sum(len(dt) for dt in doc_terms) / n) or 1.0
    df = {t: sum(1 for dt in doc_terms if any(_term_match(t, x) for x in dt)) for t in qts}
    out = []
    for dt in doc_terms:
        dl = len(dt) or 1
        s = 0.0
        for t in qts:
            tf = sum(1 for x in dt if _term_match(t, x))
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
        out.append(s)
    return out


_RRF_K = 60  # Reciprocal Rank Fusion sabiti (standart ~60)


def _rrf_scores(items: list[dict], keys) -> dict:
    """Ağırlıklı çok-sinyal RRF: keys = [key | (key, weight)]. Her sinyal için DEĞERİ>0 satırları
    azalan sırala, rank ata, Σ weight/(K+rank). Ölçek-bağımsız. recency/salience düşük ağırlıkla
    GERÇEKTEN katkı verir (eski _BOOST=0.00015 pratikte etkisizdi). Sıfır-değer katkı vermez."""
    n = len(items)
    scores = {i: 0.0 for i in range(n)}
    for key in keys:
        k, w = key if isinstance(key, tuple) else (key, 1.0)
        ranked = sorted((i for i in range(n) if (items[i].get(k) or 0) > 0),
                        key=lambda j: -items[j][k])
        for pos, i in enumerate(ranked):
            scores[i] += w / (_RRF_K + pos + 1)
    return scores


def extract_facts(raw: str) -> list[str]:
    """Metinden atomik fact listesi (LLM). scrub'lanmış metin gider."""
    cleaned, _ = scrub_secrets(raw)
    out = call_model(EXTRACT_SYS, cleaned, max_tokens=800)
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return []
    import json
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    if not isinstance(arr, list):
        return []
    # sadece string elemanlar (dict/nested list'i repr'lemek yerine at)
    return [x.strip() for x in arr if isinstance(x, str) and x.strip()]


def ingest(tenant_id: str, text: str, user_id: str = "default",
           facts: list[str] | None = None, source: str = "conversation") -> dict:
    """Tam akış: extract (veya enjekte) -> her fact: scrub, embed, dedup, upsert.
    facts verilirse LLM çağrılmaz (test/offline)."""
    cleaned, _ = scrub_secrets(text)
    source = scrub_secrets(str(source))[0][:64]   # source body'den gelebilir -> scrub + cap (egress sızıntısı)
    facts = facts if facts is not None else extract_facts(cleaned)
    tombs = store.get_tombstones(tenant_id)                   # erasure tombstone'ları (1 kez yükle)
    tomb_hashes = {t["content_hash"] for t in tombs}
    tomb_embs = [t["embedding"] for t in tombs if t["embedding"]]
    added, reinforced, skipped, blocked = 0, 0, 0, 0
    out_ids: list = []
    to_route: list = []   # yeni eklenen (id, content) -> taksonomi varsa inline route

    for fact in facts:
        fact, _ = scrub_secrets(str(fact).strip())
        if not fact:
            skipped += 1
            continue
        emb = embeddings.embed(fact)
        # ERASURE bütünlüğü (content-level + fail-closed + semantik) — tek nokta, import ile ortak
        if _tombstone_blocks(tenant_id, fact, emb, tomb_hashes, tomb_embs):
            blocked += 1
            continue

        mid = _mem_id(tenant_id, user_id, fact)  # id HER ZAMAN içerik-hash'i (hash-vs-dupid sapması yok)
        # aktif yakın-kopya varsa YENİ satır açma, onu pekiştir. PUSH-DOWN: aday üretimi
        # store.candidate_memories (büyük-N'de pgvector; küçük-N'de hepsi -> dedup davranışı korunur).
        # Batch içi: add_memory commit'lediği için sonraki fact'ler öncekini DB'den görür.
        dup_id = None
        if emb is not None:
            best, best_id = 0.0, None
            for mm in store.candidate_memories(tenant_id, user_id, emb, CANDIDATE_K):
                if mm.get("embedding"):
                    cs = embeddings.cosine(emb, mm["embedding"])
                    if cs > best:
                        best, best_id = cs, mm["id"]
            if best >= DEDUP_COSINE and best_id and best_id != mid:
                dup_id = best_id
        # reinforce dönüşünü KONTROL et — hedef eşzamanlı forget edildiyse (0 döner)
        # fact'i düşürme, aşağıda kendi id'siyle kalıcılaştır (veri kaybı yok)
        if dup_id and store.reinforce_memory(tenant_id, dup_id):
            reinforced += 1
            out_ids.append(dup_id)
            continue

        was = store.get_memory(tenant_id, mid)
        was_active = bool(was) and was.get("invalid_at") is None
        pi = pii.classify(fact)   # AACRP: PII tespit + atıf (3. şahıs PII'sini self'e karıştırma)
        store.add_memory({"id": mid, "tenant_id": tenant_id, "user_id": user_id,
                          "content": fact, "embedding": emb, "source": source,
                          "subject_party": pi["subject_party"], "pii_flags": pi["pii_flags"]})
        out_ids.append(mid)
        if was_active:
            reinforced += 1
        else:
            added += 1
            to_route.append((mid, fact))

    # INLINE ROUTING: taksonomi VARSA yeni kayıtları anında kategorile (induce'a kadar
    # 'Sınıflandırılmamış' kalmasın). Taksonomi yoksa LLM çağrısı YOK (maliyet sınırlı).
    # Best-effort: hata/LLM-yok -> atla, ingest'i ASLA bozma.
    if to_route:
        try:
            tax = store.list_taxonomy(tenant_id)
            if tax and llm_available():
                by_label = {t["label"]: t for t in tax}
                preds = routing.classify_batch([c for _i, c in to_route], list(by_label.keys()))
                rows = [{"id": mid, "node_id": node["id"], "path": node.get("path") or node["label"],
                         "tags": [], "confidence": 1.0}
                        for (mid, _c), lab in zip(to_route, preds) if (node := by_label.get(lab))]
                if rows:
                    store.bulk_set_routing(tenant_id, rows)
        except Exception:
            pass

    return {"added": added, "reinforced": reinforced, "skipped": skipped,
            "blocked": blocked, "facts": facts, "ids": out_ids}


def search(tenant_id: str, query: str, user_id: str = "default", top_k: int = 5,
           touch: bool = True, include_third_party: bool = False) -> list[dict]:
    """Hybrid retrieval: semantik (embedding) varsa o, yoksa lexical; + recency + salience.
    query LLM'e/embed'e gitmeden scrub'lanır. top_k=0 -> boş.
    KVKK: varsayılan subject_party='third_party' kayıtlar SELF-context recall'a SIZMAZ
    (3.şahıs PII'si kullanıcının bağlamına karışmasın); include_third_party=True ile dahil."""
    cleaned, _ = scrub_secrets(query)
    qv = embeddings.embed(cleaned)
    ex = not include_third_party
    # PUSH-DOWN: aday üretimini store'a it (büyük-N'de pgvector ORDER BY <=>; küçük-N'de hepsi).
    mems = store.candidate_memories(tenant_id, user_id, qv, CANDIDATE_K, exclude_third=ex)
    # İKİ-YÖNLÜ HİBRİT: dense-ANN top-k, lexical-only kayıtları (terim eşleşir ama embed örtüşmez)
    # büyük-N'de kaçırır -> blind lexical index ile birleştir (küçük-N'de candidate_memories zaten
    # hepsini döndüğünden union yeni satır eklemez = mevcut davranış birebir korunur).
    qh = store._lex_index(cleaned)
    if qh:
        seen = {m["id"] for m in mems}
        for m in store.candidate_memories_lexical(tenant_id, user_id, qh, CANDIDATE_K, exclude_third=ex):
            if m["id"] not in seen:
                seen.add(m["id"])
                mems.append(m)
    now = time.time()
    # HYBRID = ağırlıklı RRF: DENSE(cosine) + SPARSE(BM25+TR-prefix) birincil; recency + salience ikincil.
    # SPARSE artık aday-kümesi üzerinde gerçek BM25 (IDF/TF + çekim-eki prefix) — Jaccard'ın TR morfoloji
    # ölü-noktasını kapatır. recency/salience düşük-ağırlıklı RRF sinyali = artık GERÇEKTEN katkı verir.
    sparse_scores = _bm25(cleaned, [m.get("content", "") for m in mems])
    sig = []
    for i, m in enumerate(mems):
        dense = max(0.0, embeddings.cosine(qv, m["embedding"])) if (qv and m.get("embedding")) else 0.0
        sparse = sparse_scores[i]
        age_days = max(0.0, (now - (m.get("valid_at") or now)) / 86400.0)
        rec = math.exp(-age_days / RECENCY_TAU_DAYS)
        sal = min((m.get("salience") or 1.0) / 5.0, 1.0)
        rep = dense if (qv and m.get("embedding")) else (1.0 if sparse > 0 else 0.0)
        sig.append({"m": m, "dense": dense, "sparse": sparse, "rec": rec, "sal": sal, "rep": rep})
    # ağırlıklı RRF: dense/sparse=1.0 (birincil), recency/salience=W_REC/W_SAL (ikincil, etkili tiebreak+boost)
    fused = _rrf_scores(sig, [("dense", 1.0), ("sparse", 1.0), ("rec", W_REC), ("sal", W_SAL)])
    scored = [(fused[i], sig[i]["rep"], sig[i]["sparse"], sig[i]["m"]) for i in range(len(sig))]
    scored.sort(key=lambda x: -x[0])
    top = scored[:max(0, top_k)]
    if touch:  # YALNIZ gerçek-ilgili (dense/sparse hit -> fused>0) satırları pekiştir; sıfır-ilgi salience'ı kirletmesin
        store.touch_memories(tenant_id, [m["id"] for sc, _, _, m in top if sc > 0])
    return [{"id": m["id"], "content": m["content"], "score": round(sc, 4),
             "semantic": round(rep, 4), "sparse": round(sparse, 3), "source": m.get("source"),
             "salience": m.get("salience"), "created_at": m.get("created_at")}
            for sc, rep, sparse, m in top]


def forget(tenant_id: str, mem_id: str) -> int:
    """erasure: (1) shred ÖNCESİ içeriği content-level + semantik tombstone'a yaz (re-ingest/
    paraphrase engeli, user'dan bağımsız), (2) crypto-shred, (3) admin-log (accountability)."""
    m = store.get_memory(tenant_id, mem_id)
    if not m or m.get("invalid_at") is not None:
        return 0  # yok veya zaten silinmiş
    store.add_tombstone(tenant_id, _content_hash(m.get("content", "")), m.get("embedding"))
    n = store.invalidate_memory(tenant_id, mem_id)
    if n:
        store.record_admin(tenant_id, "forget_memory", mem_id)
    return n


def forget_by_subject(tenant_id: str, subject_party: str) -> int:
    """KVKK toplu silme: belirli subject_party'li ('third_party' vb.) TÜM aktif kayıtları
    erasure-güvenli sil (her biri tombstone + crypto-shred + admin-log). 'Bu kişinin/3.şahsın
    tüm verisini sil' talebini deterministik karşılar (attribution ekseninin gerçek tüketicisi)."""
    if subject_party not in ("third_party", "unknown", "self"):
        return 0
    # NULL attribution kanonik olarak 'self' sayılır (pii_summary/exclude_third ile tutarlı) —
    # 'self' silinirken NULL satırlar da kapsanmalı, yoksa "tüm self verimi sil" eksik kalır.
    def _match(m):
        sp = m.get("subject_party")
        return sp == subject_party or (subject_party == "self" and sp is None)
    targets = [m for m in store.get_active_memories(tenant_id) if _match(m)]
    n = sum(forget(tenant_id, m["id"]) for m in targets)
    store.record_admin(tenant_id, "forget_by_subject", subject_party, detail=f"count={n}")
    return n


def import_records(tenant_id: str, records: list[dict], source: str = "import") -> dict:
    """Export bundle'ından memory satırlarını içe aktar — ERASURE-GÜVENLİ.
    Hedef tenant'ın tombstone'larına karşı kontrol eder (ingest ile AYNI guard):
    forget edilmiş içerik import yoluyla DİRİLMEZ. Salience/embedding/metadata korunur.
    Yazımlar yalnız `tenant_id`'ye (izolasyon). Bundle embed taşımıyorsa yeniden üretir."""
    tombs = store.get_tombstones(tenant_id)
    tomb_hashes = {t["content_hash"] for t in tombs}
    tomb_embs = [t["embedding"] for t in tombs if t["embedding"]]
    added, blocked, skipped = 0, 0, 0
    for rec in records:
        if not isinstance(rec, dict):
            skipped += 1
            continue
        content, _ = scrub_secrets(str(rec.get("content") or "").strip())
        if not content:
            skipped += 1
            continue
        emb = rec.get("embedding")
        # emb: liste DEĞİLSE veya boyut tavanını aşıyorsa yeniden üret (O(M*T*D) DoS sınırı)
        if not isinstance(emb, list) or len(emb) > _IMPORT_MAX_EMB_DIM:
            emb = embeddings.embed(content)
        if _tombstone_blocks(tenant_id, content, emb, tomb_hashes, tomb_embs):
            blocked += 1
            continue
        uid = (rec.get("user_id") or "default")
        if not isinstance(uid, str) or len(uid) > 128:
            uid = "default"
        mid = _mem_id(tenant_id, uid, content)
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
        meta = scrub_obj(meta)   # serbest-form metadata egress'te de scrub (defense-in-depth)
        # salience: sonlu + [0,5]'e clamp (NaN/Inf/çöp ranking'i/JSON'u bozmasın)
        sal = store._safe_salience(rec.get("salience", 1.0))
        # PII atfını YERELDE yeniden hesapla — gelen 'self' etiketine GÜVENME (3.şahıs PII'si
        # import yoluyla self olarak sızmasın; KVKK round-trip güvenliği).
        pi = pii.classify(content)
        store.add_memory({"id": mid, "tenant_id": tenant_id, "user_id": uid,
                          "content": content, "embedding": emb, "source": source,
                          "salience": sal, "metadata": meta,
                          "subject_party": pi["subject_party"], "pii_flags": pi["pii_flags"]})
        added += 1
    return {"added": added, "blocked": blocked, "skipped": skipped}
