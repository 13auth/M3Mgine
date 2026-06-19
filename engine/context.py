#!/usr/bin/env python3
"""context.py — Context Pack üretimi (A): hafıza+kuralları token-bütçeli, taşınabilir
tek pakete derler; herhangi bir LLM'in context'ine enjekte edilir veya dışa aktarılır.

Bu, "veri bizde durur ama BYO-model" tezinin okuma-tarafı: merkezi hafıza, her modele
"context" olarak akar. Retrieval (memory.search) + enforce kuralları (store.get_rules)
üstüne ince bir derleme katmanı.

  build_pack(tenant, query, project, ...) -> dict   (yapılandırılmış, taşınabilir)
  render_pack(pack) -> str                            (LLM prompt'una enjekte edilir metin)

Egress'te secret scrub edilir (defense-in-depth). Tüm okuma tek tenant'a izole.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import memory  # noqa: E402
import store  # noqa: E402
from redact import scrub_secrets  # noqa: E402

PACK_VERSION = 1
_SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
# yaklaşık token ~ karakter/4 (tokenizer bağımlılığı yok; bütçeleme için yeterli heuristik)
_CHARS_PER_TOKEN = 4


def _est_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN)


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


def _rank_rules(rules: list[dict]) -> list[dict]:
    """Önem (critical>...>low) sonra tip (hard önce): enforcement-kritik olan üste."""
    return sorted(
        rules,
        key=lambda r: (-_SEV_RANK.get(str(r.get("severity") or "").lower(), 2),
                       0 if r.get("type") == "hard" else 1,
                       str(r.get("id") or "")),
    )


def _rule_tokens(r: dict) -> int:
    """Bir kuralın render'da kaplayacağı yaklaşık token (mesaj + niyet + pattern'ler)."""
    parts = [str(r.get("message") or ""), str(r.get("rule") or ""),
             " ".join(r.get("forbid_patterns") or []), " ".join(r.get("require_patterns") or [])]
    return _est_tokens(" ".join(parts)) + 6  # başlık/etiket payı


def build_pack(tenant_id: str, query: str | None = None, project: str | None = None,
               user_id: str = "default", token_budget: int = 2000,
               max_memories: int = 20, max_rules: int = 30,
               include_memories: bool = True, include_rules: bool = True,
               rule_share: float = 0.5, touch: bool = False) -> dict:
    """Tenant'ın ilgili hafıza+kurallarını token-bütçeli pakete derle.

    - query verilirse hafıza hybrid retrieval ile (memory.search); yoksa salience'a göre.
    - kurallar enforce-kritik olduğu için bütçenin `rule_share`'ı ÖNCE onlara ayrılır,
      kalan + artan hafızaya gider (kurallar hafıza tarafından aç bırakılmaz).
    - touch=False: paket üretimi saf okuma (access_count'u şişirmez; tekrar üretilebilir).
    """
    token_budget = _clamp(int(token_budget or 0), 1, 200_000)
    max_memories = _clamp(int(max_memories or 0), 0, 500)
    max_rules = _clamp(int(max_rules or 0), 0, 500)
    rule_share = rule_share if 0.0 <= rule_share <= 1.0 else 0.5

    # ---- kurallar (enforce, deterministik) ----
    sel_rules: list[dict] = []
    total_rules = 0
    if include_rules:
        all_rules = _rank_rules(store.get_rules(tenant_id, project))
        total_rules = len(all_rules)
        rule_cap = int(token_budget * rule_share)
        used = 0
        for r in all_rules[:max_rules]:
            cost = _rule_tokens(r)
            if sel_rules and used + cost > rule_cap:   # en az 1 kural geçsin, sonra bütçe
                break
            msg, _ = scrub_secrets(str(r.get("message") or ""))
            intent, _ = scrub_secrets(str(r.get("rule") or ""))
            # pattern'ler de egress (render -> LLM, ayrıca JSON döner): secret içerebilir -> scrub
            sel_rules.append({
                "id": r.get("id"), "type": r.get("type"), "severity": r.get("severity"),
                "project": scrub_secrets(str(r.get("project") or ""))[0], "message": msg, "rule": intent,
                "forbid_patterns": [scrub_secrets(str(p))[0] for p in (r.get("forbid_patterns") or [])],
                "require_patterns": [scrub_secrets(str(p))[0] for p in (r.get("require_patterns") or [])],
            })
            used += cost
        rule_tokens_used = used
    else:
        rule_tokens_used = 0

    # ---- hafıza (fact-memory) ----
    sel_mems: list[dict] = []
    total_mems = 0
    if include_memories:
        if query:
            hits = memory.search(tenant_id, query, user_id=user_id,
                                 top_k=max_memories, touch=touch)
            cand = [{"content": h.get("content", ""), "score": h.get("score"),
                     "salience": h.get("salience"), "source": h.get("source")} for h in hits]
        else:  # sorgusuz: salience sonra recency. KVKK: 3.şahıs PII'si context paketine SIZMAZ (query yoluyla parite)
            act = store.get_active_memories(tenant_id, user_id, exclude_third=True)
            act.sort(key=lambda m: (-(m.get("salience") or 0), -(m.get("valid_at") or 0)))
            cand = [{"content": m.get("content", ""), "score": None,
                     "salience": m.get("salience"), "source": m.get("source")}
                    for m in act[:max_memories]]
        total_mems = len(cand)
        mem_cap = token_budget - rule_tokens_used   # kalan bütçe (kurallardan artan dahil)
        used = 0
        for m in cand:
            content, _ = scrub_secrets(str(m.get("content") or ""))
            if not content:
                continue
            cost = _est_tokens(content) + 2
            if sel_mems and used + cost > mem_cap:
                break
            sel_mems.append({"content": content, "score": m.get("score"),
                             "salience": m.get("salience"),
                             "source": scrub_secrets(str(m.get("source") or ""))[0]})  # egress defense-in-depth
            used += cost

    truncated = (len(sel_rules) < total_rules) or (len(sel_mems) < total_mems)
    pack = {
        "pack_version": PACK_VERSION,
        "tenant_id": tenant_id,
        # echo alanları da egress (pack JSON + render_pack -> LLM): çağıranın girdisi scrub'lanır
        "query": scrub_secrets(query)[0] if query else query,
        "project": scrub_secrets(project)[0] if project else project,
        "user_id": scrub_secrets(str(user_id))[0],
        "created_at": time.time(),
        "rules": sel_rules,
        "memories": sel_mems,
        "counts": {"rules": len(sel_rules), "rules_total": total_rules,
                   "memories": len(sel_mems), "memories_total": total_mems},
        "token_budget": token_budget,
        "truncated": truncated,
    }
    pack["token_estimate"] = _est_tokens(render_pack(pack))
    pack["hash"] = _hash_pack(pack)
    return pack


def _hash_pack(pack: dict) -> str:
    """İçerik bütünlüğü: kurallar+hafıza üzerinden deterministik sha256 (zaman/hariç)."""
    payload = json.dumps({"rules": pack.get("rules"), "memories": pack.get("memories"),
                          "project": pack.get("project")},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_pack(pack: dict) -> str:
    """Paketi herhangi bir LLM'in context'ine enjekte edilebilir metne çevir (Markdown)."""
    lines = ["# Context Pack"]
    if pack.get("project"):
        lines.append(f"_proje: {pack['project']}_")
    mems = pack.get("memories") or []
    if mems:
        lines.append("\n## Bilinen gerçekler (memory)")
        for m in mems:
            lines.append(f"- {m.get('content', '')}")
    rules = pack.get("rules") or []
    if rules:
        lines.append("\n## Uygulanacak kurallar (policy)")
        for r in rules:
            tag = f"[{str(r.get('type') or '').upper()}/{r.get('severity')}]"
            lines.append(f"- {tag} {r.get('message', '')}")
            if r.get("rule"):
                lines.append(f"    niyet: {r['rule']}")
            if r.get("forbid_patterns"):
                lines.append(f"    yasak: {', '.join(r['forbid_patterns'])}")
            if r.get("require_patterns"):
                lines.append(f"    zorunlu: {', '.join(r['require_patterns'])}")
    if pack.get("truncated"):
        lines.append("\n_(token bütçesi nedeniyle kırpıldı)_")
    return "\n".join(lines)
