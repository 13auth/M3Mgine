#!/usr/bin/env python3
"""routing.py — içerik-farkında kategori routing (AACRP topic ekseni).

Kademeler (detector.py K0/K1/K2 deseni):
  K1 = embedding-nearest-centroid (ucuz, 0-LLM) — sonraki dilim (taxonomy_nodes ile).
  K2 = küçük-LLM sınıflandırma — belirsizde/preview. Bu ilk dilim K2'yi sağlar.
Egress'te scrub; LLM yoksa LLMError (çağıran yakalar).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import call_model  # noqa: E402
from redact import scrub_secrets  # noqa: E402


def classify_llm(text: str, labels: list[str]) -> dict:
    """Metni verilen kategorilerden EN UYGUN birine ata (küçük LLM). Hiçbiri uymazsa 'DIGER'.
    Dönüş: {label, source:'llm'}. Sır/PII scrub'lanır; LLM yoksa LLMError fırlatır."""
    clean = scrub_secrets(text or "")[0][:2000]
    cats = [str(c).strip() for c in (labels or []) if str(c).strip()][:50]
    if not clean or not cats:
        return {"label": "DIGER", "source": "llm"}
    sys_p = ("Verilen metni SADECE şu kategorilerden EN UYGUN olan BİRİNE ata. "
             "Kategoriler: " + ", ".join(cats) + ". "
             "Hiçbiri tam uymuyorsa 'DIGER' yaz. SADECE kategori adını yaz, başka hiçbir şey ekleme.")
    out = (call_model(sys_p, clean, max_tokens=20) or "").strip()
    return {"label": _match_label(out, cats) or "DIGER", "source": "llm", "raw": out[:40]}


def _match_label(text: str, cats: list[str]) -> str | None:
    """LLM çıktısını geçerli etikete normalize et: ÖNCE tam eşleşme, yoksa EN UZUN içerme.
    (Substring naif eşleşme 'Sağlık'ı 'Sağlık Sigortası'ndan önce yanlış seçiyordu.)"""
    low = (text or "").lower()
    exact = next((c for c in cats if c.lower() == low), None)
    if exact:
        return exact
    subs = [c for c in cats if c.lower() in low]
    return max(subs, key=len) if subs else None


def classify_batch(texts: list[str], labels: list[str]) -> list:
    """Birden çok metni TEK LLM çağrısıyla kategorilere ata (induce hızlı yolu, ~25x az çağrı).
    Sıra korunur; her metin için geçerli etiket veya 'DIGER'. Parse başarısız -> hepsi 'DIGER'."""
    cats = [str(c).strip() for c in (labels or []) if str(c).strip()][:50]
    out = ["DIGER"] * len(texts)
    if not cats or not texts:
        return out
    lines = [f"{i+1}. {scrub_secrets(t or '')[0][:400].strip()}" for i, t in enumerate(texts)]
    sys_p = ("Aşağıda numaralı metinler var. HER metni SADECE şu kategorilerden EN UYGUN BİRİNE ata. "
             "Kategoriler: " + ", ".join(cats) + ". Hiçbiri uymazsa 'DIGER'. "
             'SADECE şu formatta JSON dizi döndür: [{"n":1,"k":"KategoriAdı"}, ...]. Başka hiçbir şey yazma.')
    raw = call_model(sys_p, "\n".join(lines), max_tokens=min(3000, 60 + 14 * len(texts)))
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return out
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return out
    for item in arr:
        if not isinstance(item, dict):
            continue
        n = item.get("n")
        k = str(item.get("k", "")).strip()
        if isinstance(n, int) and 1 <= n <= len(texts):
            out[n - 1] = _match_label(k, cats) or "DIGER"   # tam-eşleşme öncelikli + en-uzun içerme
    return out
