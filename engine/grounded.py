#!/usr/bin/env python3
"""grounded.py — present-anchored, provenance-tagli, ENFORCE'li karar-cevabi (13auth wedge).

Bir AI asistaninin/insanin manuel yaptigi "iyi karar" akisini urune porte eder:
  donmus model bilgisi yerine CANLI-SIMDI'yi cek -> tenant'in DURUMUNA (hafiza+kural) capala ->
  ileriye PROJEKTE et -> her iddiayi OLGU/PRIOR/ONGORU + KAYNAK diye etiketle -> ENFORCE'tan
  gecir -> denetim izi (EU AI Act Art.12 tarzi, disa-aktarilabilir) uret.

Lab'larin yapisal olarak yapamadigi sey: onlar akici/kayipli sentez optimize eder ve provenance/
audit'i kaybeder (OpenAI Dreaming audit trail'i kaybediyor); biz iddia-kaynak-zaman izini TUTAR
ve cikti uretilmeden once kurali DAYATIRIZ.

Canli retrieval: OpenRouter ':online' web-plugin (mevcut LLM anahtari, yeni bagimlilik yok) -> kaynaklar.
Provenance tipleri:
  OLGU    = canli/dogrulanabilir dunya olgusu (kaynak beklenir)
  PRIOR   = modelin egitim-bilgisi / genel ilke (bayat olabilir, kaynaksiz)
  ONGORU  = ileri-projeksiyon (belirsiz, kaynakla degil gerekceyle)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import LLM_BASE_URL, LLM_MODEL, LLMError, _key, call_model  # noqa: E402
from policy_engine import enforce  # noqa: E402
from redact import scrub_secrets  # noqa: E402

PROV = {"olgu", "prior", "ongoru"}


def _online(system: str, user: str, max_tokens: int = 700) -> tuple[str, list[dict]]:
    """OpenRouter ':online' ile canli-web cevabi + kaynaklar (annotations). Hat yoksa LLMError."""
    model = LLM_MODEL if LLM_MODEL.endswith(":online") else LLM_MODEL + ":online"
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(LLM_BASE_URL + "/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    k = _key()
    if k:
        req.add_header("Authorization", f"Bearer {k}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode("utf-8"))
        msg = body["choices"][0]["message"]
    except urllib.error.HTTPError as e:
        raise LLMError(f"online HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, KeyError, IndexError) as e:
        raise LLMError(f"online baglanti: {e}") from None
    content = msg.get("content") or ""
    cites = []
    for a in (msg.get("annotations") or []):
        u = a.get("url_citation") or {}
        if u.get("url"):
            cites.append({"title": (u.get("title") or "").strip()[:120], "url": u["url"]})
    return content, cites


_TAG_SYS = (
    "Bir AI cevabini denetlenebilir iddialara ayir. Sana CEVAP ve numarali KAYNAKLAR verilecek. "
    "Her ANLAMLI iddia icin tip belirle:\n"
    "- olgu: canli/dogrulanabilir dunya olgusu (mumkunse bir KAYNAK numarasi ekle)\n"
    "- prior: genel ilke / model bilgisi (kaynaksiz, bayat olabilir)\n"
    "- ongoru: ileriye-donuk tahmin/projeksiyon\n"
    "EN FAZLA 8 EN ONEMLI iddia. Kisa tut. markdown/``` YOK.\n"
    'SADECE JSON dizi dondur: [{"text":"kisa iddia","type":"olgu|prior|ongoru","source":<kaynak no veya null>}]'
)


def _strip_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    return re.sub(r"\s*```$", "", s).strip()


def _tag_claims(answer: str, cites: list[dict]) -> list[dict]:
    """Cevabi provenance-etiketli iddialara ayir (ucuz, web'siz ikinci gecis)."""
    src = "\n".join(f"[{i+1}] {c['title']} {c['url']}" for i, c in enumerate(cites)) or "(kaynak yok)"
    user = f"CEVAP:\n{answer}\n\nKAYNAKLAR:\n{src}"
    try:
        raw = _strip_fence(call_model(_TAG_SYS, user, max_tokens=1200))
    except LLMError:
        return []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    chunk = m.group(0)
    try:
        arr = json.loads(chunk)
    except (json.JSONDecodeError, ValueError):
        # kesilmis dizi kurtarma: son tam '}' kadar al, diziyi kapat
        cut = chunk.rfind("}")
        if cut == -1:
            return []
        try:
            arr = json.loads(chunk[:cut + 1] + "]")
        except (json.JSONDecodeError, ValueError):
            return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict) or not it.get("text"):
            continue
        t = str(it.get("type", "prior")).strip().lower()
        t = t if t in PROV else "prior"
        si = it.get("source")
        src_url = None
        if isinstance(si, int) and 1 <= si <= len(cites):
            src_url = cites[si - 1]["url"]
        out.append({"text": str(it["text"]).strip(), "type": t, "source": src_url})
    return out


def grounded_answer(question: str, tenant_facts: list[str] | None = None,
                    rules: list[dict] | None = None, now: datetime | None = None) -> dict:
    """Tenant durumuna capali, canli-grounded, provenance-etiketli, ENFORCE'li karar-cevabi + denetim izi."""
    now = now or datetime.now(timezone.utc)
    facts = tenant_facts or []
    ctx = "\n".join(f"- {f}" for f in facts) or "(durum bilgisi yok)"
    sys_prompt = (
        "Sen bir kurumun karar-destek asistanisin. Asagidaki TENANT DURUMU'na (kalici gercekler + "
        "kisitlar) MUTLAKA capala; durumla celisen sey onerme. Gerektiginde CANLI WEB'den guncel "
        "bilgi kullan. Uygun yerde olceulu bir ileri-projeksiyon ver ama tahmin oldugunu belli et. "
        "Kisa, net, tarafsiz ol; uydurma."
    )
    user = f"TENANT DURUMU:\n{ctx}\n\nSORU:\n{question}"

    # 1) canli-grounded taslak + kaynaklar
    answer, cites = _online(sys_prompt, user)
    # 2) provenance etiketleme
    claims = _tag_claims(answer, cites)
    # 3) ENFORCE (cikti uretilmeden once kurali dayat) — scrub'lu, fail-closed
    safe_answer, _ = scrub_secrets(answer)
    res = enforce(safe_answer, rules=rules or [], run_soft=bool(rules))
    allow = res.safe()

    # 4) denetim izi (EU AI Act Art.12 tarzi: ne, ne zaman, hangi kaynak, hangi karar)
    audit = {
        "at": now.isoformat(),
        "question": question,
        "tenant_state": facts,
        "answer": answer,
        "claims": claims,
        "sources": cites,
        "enforce": {
            "allow": allow,
            "violations": [v.as_dict() for v in res.violations],
            "checked": res.checked,
        },
        "model": (LLM_MODEL if LLM_MODEL.endswith(":online") else LLM_MODEL + ":online"),
        "provenance_summary": {
            "olgu": sum(1 for c in claims if c["type"] == "olgu"),
            "prior": sum(1 for c in claims if c["type"] == "prior"),
            "ongoru": sum(1 for c in claims if c["type"] == "ongoru"),
            "kaynak_sayisi": len(cites),
        },
    }
    return audit


def export_audit(audit: dict) -> str:
    """Denetim izini dosyaya/aktarima uygun JSON string (Art.12 saklama icin)."""
    return json.dumps(audit, ensure_ascii=False, indent=2)
