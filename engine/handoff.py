#!/usr/bin/env python3
"""handoff.py — Session handoff (C): uzun bir oturumu devam-edilebilir snapshot'a indir,
resume'da "kaldığın yer" + ilgili context'i geri ver. (Claude Code'un context aktarımının
ürün-içi karşılığı.)

  snapshot(tenant, session_id, text|facts, summary) -> kalıcı fact + özet
  resume(tenant, session_id, query) -> {summary, pack}  (Context Pack ile birleşir = A)

Kaliteli özet/extraction LLM ister; LLM yoksa GRACEFUL: verilen fact'ler saklanır,
özet boş kalır (sahte özet üretmez). Tüm metin LLM'e/saklamaya gitmeden scrub'lanır.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import context  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402
from llm import LLMError, call_model, llm_available  # noqa: E402
from redact import scrub_secrets  # noqa: E402

HANDOFF_SYS = (
    "Sen bir oturum özetleyicisin. Verilen konuşmadan, işe DEVAM etmek için gereken durumu "
    "kısa ve operasyonel özetle: ne yapıldı, nerede kalındı, açık konular ve sonraki adım. "
    "Nezaket/boş laf yok. En fazla 6 madde, her madde tek satır."
)


def snapshot(tenant_id: str, session_id: str, text: str | None = None,
             facts: list | None = None, summary: str | None = None,
             user_id: str = "default") -> dict:
    """Oturumu snapshot'la: (1) kalıcı fact'leri hafızaya yaz (recall edilebilir),
    (2) "kaldığımız yer" özetini üret/sakla. facts verilirse LLM çağrılmaz."""
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id required")

    cleaned = scrub_secrets(text)[0] if text else ""

    # --- kalıcı fact'ler ---
    ing = {"added": 0, "reinforced": 0, "skipped": 0, "blocked": 0, "ids": [], "facts": []}
    # extraction LLM ister: facts verilmemiş + LLM yoksa extraction'ı atla (sahte/çökme yok)
    if facts is None and not llm_available():
        facts = []
    if facts is not None or cleaned:
        try:
            ing = memory.ingest(tenant_id, cleaned, user_id=user_id, facts=facts,
                                source=f"handoff:{session_id}")
        except LLMError:
            pass   # extraction başarısız -> fact eklenmez, özet yine de saklanır

    # --- özet ---
    llm_used = False
    if summary:
        final_summary = scrub_secrets(str(summary))[0]
    elif cleaned and llm_available():
        try:
            # model çıktısı da egress (resume pack'inde döner) -> scrub (input scrub'lı ama defense)
            final_summary = scrub_secrets(call_model(HANDOFF_SYS, cleaned, max_tokens=400).strip())[0]
            llm_used = True
        except LLMError:
            final_summary = ""   # graceful: özet üretilemedi (sahte özet yok)
    else:
        final_summary = ""

    store.add_handoff(tenant_id, session_id, final_summary, fact_ids=ing.get("ids", []),
                      metadata={"user_id": user_id})
    return {"session_id": session_id, "summary": final_summary, "llm_used": llm_used,
            "facts_added": ing.get("added", 0), "facts_reinforced": ing.get("reinforced", 0),
            "fact_ids": ing.get("ids", [])}


def resume(tenant_id: str, session_id: str, query: str | None = None,
           token_budget: int = 2000, user_id: str = "default") -> dict:
    """Snapshot'ı geri yükle: "kaldığımız yer" özeti + ilgili Context Pack (A ile birleşir).
    Handoff yoksa found=False döner."""
    h = store.get_handoff(tenant_id, session_id)
    if not h:
        return {"found": False, "session_id": session_id}
    uid = (h.get("metadata") or {}).get("user_id") or user_id
    pack = context.build_pack(tenant_id, query=query, user_id=uid, token_budget=token_budget)
    return {"found": True, "session_id": session_id, "summary": h.get("summary") or "",
            "fact_ids": h.get("fact_ids") or [], "updated_at": h.get("updated_at"),
            "pack": pack}


def render_resume(res: dict) -> str:
    """resume çıktısını tek metne çevir (özet + context pack)."""
    if not res.get("found"):
        return f"(handoff yok: {res.get('session_id')})"
    lines = [f"# Devam: {res.get('session_id')}"]
    if res.get("summary"):
        lines.append("\n## Kaldığımız yer")
        lines.append(res["summary"])
    lines.append("\n" + context.render_pack(res.get("pack") or {}))
    return "\n".join(lines)
