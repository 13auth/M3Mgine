#!/usr/bin/env python3
"""detector.py — stage 1: UCUZ otomatik düzeltme tespiti.

"Ben /correct yazmayayım, sistem yakalasın." Üç katmanlı ucuz kapı:
  K0 heuristik (sıfır token): düzeltme dilsel imzası (olumsuzlama + yönerge).
  K1 lokal model doğrulama (opsiyonel, ~bedava): heuristik tetiklenince
     LOKAL modele "bu bir düzeltme mi?" sorulur. CCE_DETECTOR_MODEL ile.
  K2 pahalı compile SADECE onaylanınca (compiler.ingest).

Canlı (per-turn) veya batch (gece transcript madenciliği) kullanılabilir;
ikisi de heuristikle ön-eler, yani çoğu tur LLM'e hiç gitmez.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import compiler  # noqa: E402
from llm import LLMError, call_model, llm_available  # noqa: E402
from redact import scrub_secrets  # noqa: E402

# Düzeltme dilsel imzası (TR + EN). Olumsuzlama / itiraz / yönerge.
_NEGATION = re.compile(
    r"\b(hay[ıi]r|yanl[ıi][şs]|öyle de[ğg]il|olmaz|demedim|de[ğg]il mi|"
    r"no|wrong|incorrect|not right|that'?s not)\b", re.IGNORECASE)
_DIRECTIVE = re.compile(
    r"(şöyle olmal[ıi]|böyle yap|yapma\b|kald[ıi]r\b|tekrar yap|düzelt|"
    r"bir daha|asla|her zaman|lütfen .* yapma|"
    r"should (be|have)|don'?t\b|instead\b|actually\b|next time|always|never)\b",
    re.IGNORECASE)
# "demiştim / söylemiştim" gibi tekrar-hatası işaretleri
_REPEAT = re.compile(r"(yine|gene|demi[şs]tim|söylemi[şs]tim|again|told you|as i said)",
                     re.IGNORECASE)

DETECTOR_MODEL = os.environ.get("CCE_DETECTOR_MODEL")  # boşsa varsayılan model


def heuristic_score(user_msg: str) -> tuple[float, list[str]]:
    """Sıfır-token ön eleme. (skor 0..1, eşleşen sinyaller)."""
    signals = []
    if _NEGATION.search(user_msg):
        signals.append("negation")
    if _DIRECTIVE.search(user_msg):
        signals.append("directive")
    if _REPEAT.search(user_msg):
        signals.append("repeat")
    score = min(1.0, 0.5 * len(signals))
    return score, signals


def llm_verify(user_msg: str, prev_assistant: str | None) -> bool:
    """K1: lokal model 'bu bir düzeltme mi?' Tek kelime. has_key() yoksa çağrılmaz."""
    sys_p = ("Kullanıcının mesajı, AI'ın önceki cevabını DÜZELTİYOR mu (hata/itiraz/"
             "'şöyle olmalıydı')? Sadece 'EVET' veya 'HAYIR' yaz.")
    # secret/PII LLM'e gitmesin: iki girdiyi de scrub'la
    prev_s, _ = scrub_secrets(prev_assistant or "(yok)")
    user_s, _ = scrub_secrets(user_msg)
    user_p = f"AI önceki cevap:\n{prev_s[:600]}\n\nKullanıcı:\n{user_s[:600]}"
    kwargs = {}
    if DETECTOR_MODEL:
        # tek hat üstünden farklı (lokal) model istemek için geçici env
        prev = os.environ.get("CCE_LLM_MODEL")
        os.environ["CCE_LLM_MODEL"] = DETECTOR_MODEL
        try:
            out = call_model(sys_p, user_p, max_tokens=5)
        finally:
            if prev is None:
                os.environ.pop("CCE_LLM_MODEL", None)
            else:
                os.environ["CCE_LLM_MODEL"] = prev
    else:
        out = call_model(sys_p, user_p, max_tokens=5)
    return out.strip().upper().startswith("EVET") or out.strip().upper().startswith("YES")


def _correction_text(user_msg: str, prev_assistant: str | None) -> str:
    base = f"AI şunu yaptı: {prev_assistant[:300]}\n" if prev_assistant else ""
    return base + f"Kullanıcı düzeltmesi: {user_msg}"


def scan_turn(user_msg: str, prev_assistant: str | None, tenant_id: str,
              use_llm: bool = True, threshold: float = 0.5) -> dict:
    """Tek tur tara. Düzeltme ise compiler.ingest çağırır.
    Döner: {is_correction, signals, verified, ingested, rule?}"""
    score, signals = heuristic_score(user_msg)
    result = {"is_correction": False, "signals": signals, "score": score,
              "verified": None, "ingested": False, "rule": None}
    if score < threshold:
        return result  # K0 elemesi: çoğu tur burada durur, LLM'e gitmez

    verified = True
    if use_llm and llm_available():
        try:
            verified = llm_verify(user_msg, prev_assistant)
        except LLMError:
            verified = True  # LLM yoksa heuristiğe güven (fail-open tespit)
    result["verified"] = verified
    if not verified:
        return result

    result["is_correction"] = True
    try:
        out = compiler.ingest(_correction_text(user_msg, prev_assistant), tenant_id)
        result["ingested"] = bool(out["rule"].get("_written"))
        result["rule"] = out["rule"]
    except (LLMError, ValueError) as e:
        result["error"] = str(e)  # compile için key gerekli; yoksa tespit edildi ama compile olmadı
    return result


def scan_transcript(messages: list[dict], tenant_id: str, use_llm: bool = True) -> dict:
    """Batch: [{role, content}] listesini tara (gece cron modu).
    Her assistant→user düzeltme çiftini yakalar. Tur başına maliyet ~0 (heuristik ön-eleme)."""
    found, ingested = [], 0
    prev_assistant = None
    for m in messages:
        role, content = m.get("role"), m.get("content", "")
        if role == "user":
            r = scan_turn(content, prev_assistant, tenant_id, use_llm=use_llm)
            if r["is_correction"]:
                found.append({"user": content[:120], "signals": r["signals"],
                              "ingested": r["ingested"]})
                ingested += 1 if r["ingested"] else 0
        elif role == "assistant":
            prev_assistant = content
    return {"corrections_found": len(found), "ingested": ingested, "details": found}
