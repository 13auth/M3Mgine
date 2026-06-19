#!/usr/bin/env python3
"""compiler.py — DÖNGÜ KAPANIŞI: correction -> compiled rule.

stage 1-2: ham düzeltmeyi al, secret'ları temizle, modele sınıflandır+kural-çıkar
dedir, dönen yapıyı bir RULE'a derle ve store'a yaz. Artık düzeltme otomatik
DAYATILABİLİR bir kurala dönüşür — elle adım yok.

Üretilen kural varsayılan 'soft' (semantik niyet). Model net "yasak ifadeler"
verirse 'hard' regex desenleri de eklenir (deterministik, sıfır-LLM yakalama).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pii  # noqa: E402  (PII kategori desenleri -> hard forbid enjeksiyonu)
import store  # noqa: E402
from llm import call_model  # noqa: E402
from redact import scrub_secrets  # noqa: E402  (paylaşılan secret filtresi)


CLASSIFY_SYS = """Sen bir hafıza damıtma motorusun. Kullanıcının verdiği ham bir
'correction' (AI bir hata yaptı, kullanıcı düzeltti) gelecek. Görevin sınıflandır
VE ondan TAŞINABİLİR bir kural çıkar.

ÖZET DEĞİL, GENELLEME: 'reusable_rule' bu spesifik olayın özeti olmamalı; benzer
ama farklı durumlara uygulanabilen, soyutlanmış, EYLEM belirten, test edilebilir
bir kural olmalı. ANCAK denetim için gereken SOMUT KISITI KORU: ör. 'yalnızca
e-posta desteği var, telefon/canlı destek yok' veya 'iade 14 günle sınırlı' gibi
gerçeği at(ma)kma — aşırı soyutlama ('olmayan özellik vaat etme') kuralı
denetlenemez yapar çünkü denetçi neyin var/yok olduğunu bilemez.

forbid_terms: çıktıda görülmesi TEK BAŞINA KESİN ihlal olan SABİT, ÇOK-KELİMELİ
ibareler veya ÖZEL marka/lig adları (örn. 'binlerce kanal', 'beIN Sports',
'Şampiyonlar Ligi'). TEK GENEL KELİME VERME (örn. 'film', 'maç', 'kimlik' gibi geniş
kelimeler yanlış yakalar). Bir VERİ KATEGORİSİNİ tarif eden ifadeleri ('kart
numarası', 'kimlik numarası', 'IBAN' gibi) forbid_terms'e KOYMA: ihlal o verinin
GERÇEKTEN ifşası olduğunda doğar, kelimenin geçmesiyle değil ('kimlik doğrulaması'
masum) — bu tür kuralları semantik (soft) bırakmak için forbid_terms'i BOŞ ver.
Emin değilsen boş liste döndür.

SADECE şu JSON'ı döndür (markdown fence yok). project: gerçek proje adı (ör. Acme)
ya da JSON null — şablon metnini KOPYALAMA:
{"category":"preference|procedure|project|integration|skill-fix",
 "project":null,"what_was_wrong":"...","correct_behavior":"...",
 "reusable_rule":"taşınabilir kural","severity":"low|medium|high",
 "tags":["..."],"forbid_terms":[]}"""


def _parse_json(s: str) -> dict:
    s = re.sub(r"^```(?:json)?\s*", "", s.strip())
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        raise ValueError(f"Modelden JSON çıkmadı: {s[:200]}")
    return json.loads(m.group(0))


def classify(raw: str) -> dict:
    """Ham correction -> yapılandırılmış sınıflandırma (LLM)."""
    cleaned, _ = scrub_secrets(raw)
    return _parse_json(call_model(CLASSIFY_SYS, cleaned, max_tokens=1200))


_PLACEHOLDER_PROJECT = {"", "null", "none", "-", "yok", "ad veya null", "proje adı veya null"}


def _clean_project(p) -> str | None:
    """Zayıf modelin döndürdüğü placeholder/çöp project değerlerini None'a indir."""
    if not p or not isinstance(p, str):
        return None
    s = p.strip()
    if s.lower() in _PLACEHOLDER_PROJECT or "veya null" in s.lower() or s.startswith("<"):
        return None
    return s


def _clean_forbid(terms) -> list[str]:
    """Sadece güvenli yasak terimleri tut: çok-kelimeli VEYA >=5 karakter.
    Tek genel kısa kelime (film, maç) substring olarak yanlış yakalar -> at."""
    out = []
    for t in terms or []:
        if not t or not isinstance(t, str):
            continue
        s = t.strip()
        if " " in s or len(s) >= 5:
            out.append(re.escape(s))
    return out


def _rule_id(project: str | None, rtype: str, reusable_rule: str) -> str:
    """DETERMİNİSTİK içerik-hash'li id. Aynı correction tekrar gelince AYNI id ->
    ON CONFLICT in-place günceller, kopya birikmez. (timestamp YOK.)"""
    # Türkçe-güvenli fold: İÇERİK/içerik aynı base -> aynı id (store._fold ile aynı mantık)
    base = (project or "global").replace("İ", "i").replace("I", "i").replace("ı", "i").strip().casefold().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", reusable_rule.lower())[:40].strip("-")
    norm = " ".join((reusable_rule or "").lower().split())
    h = hashlib.sha1(f"{base}|{rtype}|{norm}".encode("utf-8")).hexdigest()[:8]
    return f"{base}-{slug or 'rule'}-{h}"


_SEV_SET = {"low", "medium", "high", "critical"}


def _norm_sev(s) -> str:
    """Severity'i canonical lowercase'e indir; tanınmayan -> 'high' (fail-closed,
    gatekeeper baypası olmasın)."""
    s = str(s or "").strip().lower()
    return s if s in _SEV_SET else "high"


_PII_HINT = re.compile(
    r"kimlik\s*num|\btckn\b|t\.?\s?c\.?\s*kimlik|kart\s*num|kredi\s*kart|\biban\b|"
    r"telefon\s*num|hassas\s*(kişisel|kisisel)|(kişisel|kisisel)\s*ver|\bpii\b", re.I)


def _is_pii(data: dict) -> bool:
    """Düzeltme PII ifşası hakkında mı? (reusable_rule/what_was_wrong/correct_behavior/tags
    metninde kimlik/kart/IBAN/telefon kategori ipucu). PII'de literal tek örnek yerine
    kanonik desen enjekte edilir -> görülmeyen her instance'a genelleşir."""
    blob = " ".join(str(data.get(k, "")) for k in
                    ("reusable_rule", "what_was_wrong", "correct_behavior"))
    blob += " " + " ".join(str(t) for t in (data.get("tags") or []))
    return bool(_PII_HINT.search(blob))


def compile_to_rule(data: dict, tenant_id: str, correction_id: str | None = None) -> dict:
    """Sınıflandırma çıktısını bir RULE dict'ine derle."""
    project = _clean_project(data.get("project"))
    if _is_pii(data):
        # PII: bir TC/kart numarasının literal örneği genelleşmez. Kanonik PII desenlerini
        # hard forbid olarak enjekte et (scrub'dan önce, her görülmeyen instance'ı yakalar).
        forbid = pii.forbid_patterns()
        rtype = "hard"
    else:
        # Derlenen düzeltme HER ZAMAN soft (semantik judge GÖRÜLMEYEN ihlalleri de yakalar;
        # hard/soft kararını modelin forbid_terms verip vermemesine bırakmak genellemeyi
        # model-varyansına bağlar). forbid_terms varsa hard accelerator olarak korunur:
        # enforce bunları zero-LLM ön-katman olarak çalıştırır, judge geri kalanı genelleştirir.
        forbid = _clean_forbid(data.get("forbid_terms", []))
        rtype = "soft"
    reusable = data.get("reusable_rule", "")
    rule = {
        "id": _rule_id(project, rtype, reusable),
        "tenant_id": tenant_id,
        "project": project,
        "type": rtype,
        "severity": _norm_sev(data.get("severity")),
        "source": f"correction:{data.get('what_was_wrong','')[:80]}",
        "correction_id": correction_id,  # provenance: kuralı doğuran correction
        "message": reusable,
        "rule": reusable,
        "forbid_patterns": forbid,
        "require_patterns": [],
    }
    return rule


def ingest(raw: str, tenant_id: str, classification: dict | None = None) -> dict:
    """Tam akış: classify (veya enjekte) -> correction kaydı + rule -> store.
    classification verilirse LLM çağrılmaz (test/offline için)."""
    data = classification or classify(raw)
    project = _clean_project(data.get("project"))

    # deterministik correction id -> re-ingest audit log'da kopya üretmez
    corr_key = f"{tenant_id}|{data.get('reusable_rule','')}|{data.get('what_was_wrong','')}"
    corr_id = "corr-" + hashlib.sha1(corr_key.encode("utf-8")).hexdigest()[:12]
    corr = {
        "id": corr_id,
        "tenant_id": tenant_id, "project": project,
        "category": data.get("category", "procedure"),
        "what_was_wrong": data.get("what_was_wrong", ""),
        "correct_behavior": data.get("correct_behavior", ""),
        "reusable_rule": data.get("reusable_rule", ""),
        "severity": data.get("severity", "medium"),
        "tags": data.get("tags", []),
    }
    store.add_correction(corr)

    rule = compile_to_rule(data, tenant_id, correction_id=corr_id)
    # dedup: aynı normalize mesajlı kural farklı id ile varsa onun id'sini kullan
    # (deterministik id zaten birebir aynıyı yakalar; bu, yakın-kopyayı birleştirir)
    existing = store.rule_exists_like(tenant_id, project, rule["message"], rtype=rule["type"])
    if existing:
        rule["id"] = existing
    # skill-fix otomatik dayatılmaz; öneri olarak kalır (kural yazılmaz)
    if data.get("category") != "skill-fix":
        store.add_rule(rule)
        rule["_written"] = True
    else:
        rule["_written"] = False
    return {"correction": corr, "rule": rule}
