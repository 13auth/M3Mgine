#!/usr/bin/env python3
"""kg.py — Temporal Knowledge Graph motoru (Graphiti-eşdeğeri çekirdek).

Graphiti yaklaşımı: konuşmadan VARLIK + İLİŞKİ çıkarır, bunları BİLGİ GRAFİĞİNE
yazar, fact'lere bi-temporal damga verir, çelişen yeni bilgi gelince eski kenarı
OTOMATİK geçersizler (tarih korunur). Bu modül onu sıfırdan kuruyor (
katman değil). Depo store.kg_entities/kg_edges; SQLite v0 (ölçekte graph DB'ye porte).

  ingest(tenant, text|triples) -> extract -> entity resolve -> çelişki-invalidate -> kenar
  search(tenant, query, as_of=) -> hybrid (semantik fact + graph komşuluk), point-in-time
  neighbors(tenant, entity) -> bir düğümün güncel kenarları

Bi-temporal: valid_at/invalid_at = DÜNYA zamanı (ne zaman doğruydu); created_at/expired_at
= SİSTEM zamanı (ne zaman öğrendik / süperse ettik). İkisi ayrı = "T anında ne biliyorduk
ve dünyada ne doğruydu" ayrı ayrı sorgulanır.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import embeddings  # noqa: E402
import store  # noqa: E402
from llm import call_model  # noqa: E402
from memory import _rrf_scores  # noqa: E402  (RRF füzyonu paylaşımlı)
from redact import scrub_secrets  # noqa: E402

ENTITY_RESOLVE_COSINE = 0.92   # bunun üstü = aynı varlık (entity resolution)
_TOK = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", re.UNICODE)
MAX_KG_ENTITIES = int(os.environ.get("CCE_MAX_KG_ENTITIES", "50000"))  # per-tenant düğüm tavanı (DoS)
_RESOLVE_SCAN_CAP = int(os.environ.get("CCE_KG_RESOLVE_SCAN", "5000"))  # semantik aday tavanı (push-down)
CANDIDATE_K = int(os.environ.get("CCE_CANDIDATE_K", "256"))            # edge retrieval aday tavanı

# Çok-değerli yüklemler: aynı özne için birden çok nesne BİR ARADA yaşar (çelişki sayılmaz).
# Diğer tüm yüklemler tek-değerli kabul edilir (yeni nesne eskisini dünya-zamanında geçersizler).
# NOT: _norm_pred(fold) ile normalize EDİLEREK tanımlanır ('kullanıyor' ı->i fold'lanınca
# set'te bulunamıyordu -> Türkçe yüklem yanlışlıkla tek-değerli sayılıyordu).
_RAW_MULTI_VALUED = {"likes", "knows", "owns", "has", "related_to", "mentions", "met",
                     "visited", "uses", "sever", "biliyor", "kullanir", "kullanıyor",
                     "links_to", "links", "references", "tagged"}

EXTRACT_SYS = (
    "Sen bir bilgi-grafiği çıkarıcısın. Verilen metinden VARLIK-İLİŞKİ üçlüleri çıkar. "
    "Her üçlü: subject (varlık adı), predicate (kısa ilişki anahtarı, snake_case ör. works_at, "
    "lives_in, role_is), object (varlık adı VEYA değer), object_type ('entity' ya da 'literal'), "
    "subject_type/object_type varlık türü (person/org/place/concept), fact (tek cümle doğal dil). "
    "Sadece kalıcı, bilmeye değer ilişkiler. SADECE JSON dizi döndür: "
    '[{"subject":"..","subject_type":"..","predicate":"..","object":"..","object_type":"entity|literal","fact":".."}]. '
    "Yoksa []."
)


def _norm(s: str) -> str:
    return store._fold(s)   # Türkçe-güvenli fold (İ/ı dahil)


def _norm_pred(p: str) -> str:
    return re.sub(r"\s+", "_", store._fold(p)).strip("_")


# fold'lanmış yüklem kümesi (ingest'teki pred de fold'lu -> aynı uzayda karşılaştır)
MULTI_VALUED = frozenset(_norm_pred(p) for p in _RAW_MULTI_VALUED)


def _ent_id(tenant_id: str, norm: str) -> str:
    return "ent-" + hashlib.sha1(f"{tenant_id}|{norm}".encode("utf-8")).hexdigest()[:16]


def _edge_id(tenant_id: str, subj_id: str, pred: str, obj_key: str, valid_at: float) -> str:
    """valid_at DAHİL: boomerang (geçersizlenen nesnenin geri dönüşü) YENİ valid_at ile FARKLI id
    alır -> tarih korunur, çakışma/DO-NOTHING sessiz kaybı olmaz. Aynı (nesne,valid_at)
    re-assertion ise aynı id -> idempotent."""
    key = f"{tenant_id}|{subj_id}|{pred}|{obj_key}|{valid_at!r}"
    return "edg-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _lexical(a: str, b: str) -> float:
    ta = {t for t in _TOK.findall(a.lower()) if len(t) > 2}
    tb = {t for t in _TOK.findall(b.lower()) if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def extract_triples(text: str) -> list[dict]:
    """Metinden varlık-ilişki üçlüleri (LLM). scrub'lanmış metin gider."""
    cleaned, _ = scrub_secrets(text)
    out = call_model(EXTRACT_SYS, cleaned, max_tokens=900)
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return []
    if not isinstance(arr, list):
        return []
    triples = []
    for t in arr:
        if isinstance(t, dict) and t.get("subject") and t.get("predicate") and t.get("object"):
            triples.append(t)
    return triples


def resolve_entity(tenant_id: str, name: str, etype: str = "", emb=None,
                   _cache: dict | None = None) -> str:
    """Entity resolution: aynı varlığı tek düğüme indir. Önce fold'lu ad eşleşmesi, sonra
    semantik (cosine>=ENTITY_RESOLVE_COSINE), yoksa yeni düğüm. İçerik scrub'lı saklanır."""
    name, _ = scrub_secrets(str(name).strip())
    norm = _norm(name)
    if not norm:
        return ""
    # 1) exact (fold) eşleşme
    existing = store.get_entity_by_norm(tenant_id, norm)
    if existing:
        return existing["id"]
    # 2) semantik eşleşme — PUSH-DOWN: aday üretimi store.candidate_entities (büyük-N'de
    #    pgvector ORDER BY <=> LIMIT; küçük-N'de hepsi). _RESOLVE_SCAN_CAP aday tavanı = DoS sınırı.
    #    EN İYİ eşleşmeyi seç (orijinal davranış korunur).
    if emb is not None:
        best, best_id = 0.0, None
        et_want = (etype or "").strip().lower()
        for e in store.candidate_entities(tenant_id, emb, _RESOLVE_SCAN_CAP):
            if e.get("embedding"):
                # tip-koruması: iki taraf da TİPLİyse ve tipler farklıysa birleştirme
                # (homonim çökmesi: "Ali"(kişi) ile "Ali"(şirket) tek düğüme inmesin).
                # Taraflardan biri tipsiz(boş) ise eski davranış korunur (birleşmeye izin).
                et_cand = (e.get("type") or "").strip().lower()
                if et_want and et_cand and et_want != et_cand:
                    continue
                cs = embeddings.cosine(emb, e["embedding"])
                if cs > best:
                    best, best_id = cs, e["id"]
        if best >= ENTITY_RESOLVE_COSINE and best_id:
            return best_id
    # 3) yeni düğüm — per-tenant tavanı: doluysa FAIL-CLOSED (yeni düğüm açma, graf sınırsız büyümesin)
    if _cache is not None:
        n = _cache.get("ent_count")
        if n is None:
            n = store.count_entities(tenant_id)
            _cache["ent_count"] = n
    else:
        n = store.count_entities(tenant_id)
    if n >= MAX_KG_ENTITIES:
        return ""   # cap -> bu varlık (ve dolayısıyla üçlü) atlanır
    eid = _ent_id(tenant_id, norm)
    store.add_entity({"id": eid, "tenant_id": tenant_id, "name": name, "norm": norm,
                      "type": etype or "", "embedding": emb})
    if _cache is not None:
        if "ents" in _cache:
            _cache["ents"].append({"id": eid, "embedding": emb})
        if "ent_count" in _cache:
            _cache["ent_count"] += 1
    return eid


def ingest(tenant_id: str, text: str = "", triples: list[dict] | None = None,
           source: str = "conversation", valid_at: float | None = None) -> dict:
    """Tam akış: extract (veya enjekte) -> entity resolve -> ÇELİŞKİ invalidation -> kenar ekle.
    triples verilirse LLM çağrılmaz (test/offline)."""
    cleaned, _ = scrub_secrets(text)
    source = scrub_secrets(str(source))[0][:64]
    triples = triples if triples is not None else (extract_triples(cleaned) if cleaned else [])
    now = time.time()
    vt = float(valid_at) if valid_at is not None else now
    cache: dict = {}
    added, invalidated, ents_created_before = 0, 0, len(store.list_entities(tenant_id))

    for t in triples:
        if not isinstance(t, dict):
            continue
        subj = str(t.get("subject") or "").strip()
        pred = _norm_pred(str(t.get("predicate") or ""))
        obj = str(t.get("object") or "").strip()
        if not subj or not pred or not obj:
            continue
        fact, _ = scrub_secrets(str(t.get("fact") or f"{subj} {pred} {obj}"))
        is_literal = (t.get("object_type") == "literal")

        subj_emb = embeddings.embed(subj)
        subj_id = resolve_entity(tenant_id, subj, str(t.get("subject_type") or ""), subj_emb, cache)
        if not subj_id:
            continue
        if is_literal:
            obj_id, obj_value = None, scrub_secrets(obj)[0]
            obj_key = "lit:" + _norm(obj_value)
        else:
            obj_emb = embeddings.embed(obj)
            obj_id = resolve_entity(tenant_id, obj, str(t.get("object_type") or ""), obj_emb, cache)
            if not obj_id:   # cap/çözülemeyen entity nesne -> FAIL-CLOSED, üçlüyü atla (orphan edge yazma)
                continue
            obj_value = None
            obj_key = obj_id

        # re-assertion no-op: bu nesne ZATEN dünya-güncel (şimdi) -> kopya/yeni satır açma
        if any((c.get("object_id") or "lit:" + _norm(c.get("object_value") or "")) == obj_key
               for c in store.get_edges(tenant_id, subject_id=subj_id, predicate=pred, current_only=True)):
            continue

        # ÇELİŞKİ invalidation (tek-değerli yüklem): DÜNYA-ZAMAN SIRASINA SAYGILI.
        # TÜM retract-edilmemiş farklı-nesneli kenarları dünya-zaman pozisyonuna göre değerlendir
        # (sadece "vt anında güncel" değil) -> backfill/out-of-order/mid-timeline insert'i doğru böl.
        # KARAR (read-only): hangi çelişen kenarlar vt'de kapatılacak (bounds) + yeni kenarın
        # dünya-bitişi (new_invalid). Yazımlar AŞAĞIDA tek atomik transaction'da.
        new_invalid = None
        bounds: list = []
        if pred not in MULTI_VALUED:
            for cur in store.get_edges(tenant_id, subject_id=subj_id, predicate=pred,
                                       current_only=False):
                if cur.get("expired_at") is not None:   # sistemce retract edilmiş -> yok say
                    continue
                cur_key = cur.get("object_id") or ("lit:" + _norm(cur.get("object_value") or ""))
                if cur_key == obj_key:
                    continue
                ve = cur.get("valid_at") if cur.get("valid_at") is not None else vt
                ci = cur.get("invalid_at")
                if ve < vt:
                    # eski kenar; vt'de hâlâ açıksa yeni onu vt'de kapatır (süperse)
                    if ci is None or ci > vt:
                        bounds.append((cur["id"], vt))
                else:
                    # ve >= vt: bu (eşit/daha yeni) kenar YENİYİ sınırlar (backfill); eskiye dokunma
                    # -> en yeni gerçek korunur, ters/sıfır aralık yok
                    new_invalid = ve if new_invalid is None else min(new_invalid, ve)

        fact_emb = embeddings.embed(fact)
        # valid_at id'ye dahil -> boomerang yeni satır (tarih korunur); aynı (nesne,vt) -> idempotent
        eid = _edge_id(tenant_id, subj_id, pred, obj_key, vt)
        # ATOMİK: bound(lar) + add tek transaction (crash'te kısmi durum yok)
        res = store.invalidate_and_add_edge(
            tenant_id, bounds,
            {"id": eid, "tenant_id": tenant_id, "subject_id": subj_id, "predicate": pred,
             "object_id": obj_id, "object_value": obj_value, "fact": fact,
             "embedding": fact_emb, "valid_at": vt, "invalid_at": new_invalid,
             "created_at": now, "source": source})
        invalidated += res["invalidated"]
        added += res["added"]   # GERÇEK yazıma göre say (yalan added:1 yok)

    ents_created = max(0, len(store.list_entities(tenant_id)) - ents_created_before)
    return {"added": added, "invalidated": invalidated, "entities_created": ents_created,
            "triples": len(triples)}


def _name(tenant_id: str, ent_id: str | None, cache: dict) -> str:
    if not ent_id:
        return ""
    if ent_id not in cache:
        e = store.get_entity(tenant_id, ent_id)
        cache[ent_id] = e["name"] if e else ent_id
    return cache[ent_id]


def search(tenant_id: str, query: str, as_of: float | None = None, top_k: int = 5) -> list[dict]:
    """Hybrid retrieval: semantik (fact embedding) + graph komşuluk (sorgudaki varlığa bağlı
    kenarlara bonus) + lexical fallback. as_of verilirse DÜNYA point-in-time (o anda doğru olanlar)."""
    cleaned, _ = scrub_secrets(query)
    qv = embeddings.embed(cleaned)
    # PUSH-DOWN: güncel edge aday üretimi store.candidate_edges'e (pgvector büyük-N'de). as_of
    # (point-in-time) yolu tam tarih taraması olduğundan get_edges_asof kalır.
    edges = store.get_edges_asof(tenant_id, as_of) if as_of is not None \
        else store.candidate_edges(tenant_id, qv, CANDIDATE_K)

    # sorguda geçen/sorguya semantik yakın varlıklar -> graph bonus
    anchor_ids: set[str] = set()
    for e in store.list_entities(tenant_id):
        if _norm(e["name"]) and _norm(e["name"]) in _norm(cleaned):
            anchor_ids.add(e["id"])
        elif qv is not None and e.get("embedding") and embeddings.cosine(qv, e["embedding"]) >= 0.6:
            anchor_ids.add(e["id"])

    # HYBRID = RRF: DENSE(cosine) + SPARSE(lexical) + GRAPH(anchor komşuluğu). Linear toplam
    # yerine ölçek-bağımsız füzyon (memory.search ile tutarlı). audit top-sonuçları korunur.
    ncache: dict = {}
    sig = []
    for ed in edges:
        dense = max(0.0, embeddings.cosine(qv, ed["embedding"])) if (qv and ed.get("embedding")) else 0.0
        sparse = _lexical(cleaned, ed.get("fact", ""))
        graph = 1.0 if (ed.get("subject_id") in anchor_ids or ed.get("object_id") in anchor_ids) else 0.0
        rep = dense if (qv and ed.get("embedding")) else sparse
        sig.append({"ed": ed, "dense": dense, "sparse": sparse, "graph": graph, "rep": rep})
    # RELEVANCE = RRF(dense, sparse); graph (anchor komşuluğu) İKİNCİL küçük boost — binary graph'ı
    # eşit RRF sinyali yapmak güçlü dense eşleşmeyi eziyordu. _GBOOST < ardışık RRF
    # rank farkı -> yalnız yakın-skorları ayırır, baskın olmaz (memory.search recency/salience ile aynı).
    _GBOOST = 0.00015
    fused = _rrf_scores(sig, ("dense", "sparse"))
    scored = [(fused[i] + _GBOOST * sig[i]["graph"], sig[i]["rep"], sig[i]["ed"]) for i in range(len(sig))]
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, sem, ed in scored[:max(0, top_k)]:
        out.append({"id": ed["id"], "fact": ed["fact"], "predicate": ed["predicate"],
                    "subject": _name(tenant_id, ed.get("subject_id"), ncache),
                    "object": _name(tenant_id, ed.get("object_id"), ncache) or ed.get("object_value"),
                    "score": round(score, 4), "semantic": round(sem, 4),
                    "valid_at": ed.get("valid_at"), "source": ed.get("source")})
    return out


def neighbors(tenant_id: str, entity_name: str, current_only: bool = True) -> list[dict]:
    """Bir varlığın (özne olarak) güncel kenarları — graph gezinme/komşuluk."""
    norm = _norm(entity_name)
    e = store.get_entity_by_norm(tenant_id, norm)
    if not e:
        return []
    ncache: dict = {}
    res = []
    for ed in store.get_edges(tenant_id, subject_id=e["id"], current_only=current_only):
        res.append({"predicate": ed["predicate"], "fact": ed["fact"],
                    "object": _name(tenant_id, ed.get("object_id"), ncache) or ed.get("object_value"),
                    "valid_at": ed.get("valid_at")})
    return res
