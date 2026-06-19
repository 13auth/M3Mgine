#!/usr/bin/env python3
"""demo_grounded.py — SATIS/KANIT DEMOSU: present-anchored + enforce'li + provenance'li karar-cevabi.

"Claude'un az once manuel yaptigini urune aktar" — canli calisan hali. Gercek motor:
grounded.grounded_answer (OpenRouter :online canli web + tenant durumu + ileri-projeksiyon +
provenance etiket + policy_engine enforce + denetim izi).

Senaryo: TR fintech 'FinBot'. Tenant kurali (bir duzeltmeden turedi) + musteri durumu (hafiza) +
guncel-bilgi gerektiren soru -> donmus model degil, CANLI + DURUMA-CAPALI + ENFORCE'li cevap,
her iddia kaynagiyla etiketli, denetim izi disa-aktarilir.

Calistir: python demo_grounded.py  (engine/.env CCE_LLM_* OpenRouter, :online destekli)
"""
from __future__ import annotations

import sys
from pathlib import Path

ENG = Path(__file__).resolve().parent
sys.path.insert(0, str(ENG))


def _load_env() -> None:
    import os
    envp = ENG / ".env"
    if not envp.exists():
        return
    for line in envp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()
import grounded  # noqa: E402
from llm import has_key  # noqa: E402

W = 78


def hr(c="-"):
    print(c * W)


def title(t):
    print("\n" + "=" * W + f"\n  {t}\n" + "=" * W)


TENANT_FACTS = [
    "Musteri emeklilige yakin; dusuk risk toleransi belirtti.",
    "Musteri daha once 'paramin guvende olmasi onceligim' dedi.",
    "Kurum yalnizca TEFAS yatirim fonlari sunar; kripto ve turev urun sunmaz.",
]

# Bir duzeltmeden turemis enforce kurali (compiler.ingest ile uretilebilir; demoda inline).
RULE = {
    "id": "fin-no-guarantee", "type": "soft", "severity": "high",
    "rule": ("Cikti bir yatirimin getiri/kar GARANTISI verdigini ima ediyorsa, kesin kazanc / "
             "kayip imkansiz / 'kesinlikle kazanirsin' gibi ifade kullaniyorsa IHLAL'dir. "
             "Risk oldugunu belirtiyorsa ihlal YOK."),
    "message": "Yatirim getirisi garantisi yasak; her zaman risk belirt.",
    "forbid_patterns": [], "require_patterns": [],
}

QUESTION = ("Musteri 'birikimimi guncel ortamda nasil degerlendireyim, ne onerirsin?' diye "
            "soruyor. Ona nasil yanit verelim?")

PROV_LABEL = {"olgu": "OLGU ", "prior": "PRIOR", "ongoru": "ONGORU"}


def main() -> int:
    if not has_key():
        print("SKIP: LLM hatti yok (engine/.env CCE_LLM_API_KEY).")
        return 0

    title("13auth — Present-Anchored + Enforce'li + Provenance'li Karar-Cevabi (canli)")
    print("Lab'lar: akici/kayipli sentez, audit trail'i kaybeder, donmus bilgi.")
    print("13auth: canli-simdi + tenant durumuna capa + ileri-projeksiyon + KURAL DAYATMA")
    print("        + her iddia kaynagiyla = denetim izi (EU AI Act Art.12 tarzi).")

    title("1) Tenant durumu (hafiza) + kural (bir duzeltmeden turedi)")
    for f in TENANT_FACTS:
        print(f"  - {f}")
    print(f"  [KURAL] {RULE['message']}")

    title("2) Soru (guncel bilgi gerektiriyor)")
    print(f"  {QUESTION}")

    title("3) Motor calisiyor: canli web + capa + projeksiyon + provenance + enforce")
    print("  (OpenRouter :online — birkac saniye)\n")
    audit = grounded.grounded_answer(QUESTION, tenant_facts=TENANT_FACTS, rules=[RULE])

    print("  CEVAP:")
    for line in (audit["answer"] or "").splitlines():
        print("   ", line)

    title("4) Provenance — her iddia etiketli (olgu/prior/ongoru + kaynak)")
    if not audit["claims"]:
        print("  (iddia ayristirilamadi)")
    for c in audit["claims"]:
        lbl = PROV_LABEL.get(c["type"], c["type"])
        src = f"  <- {c['source']}" if c.get("source") else ""
        print(f"  [{lbl}] {c['text']}{src}")

    title("5) Enforce karari (cikti uretilmeden once kural dayatildi)")
    e = audit["enforce"]
    print(f"  Karar: {'GECTI (izinli)' if e['allow'] else 'BLOKLANDI'}   (denetlenen kural: {e['checked']})")
    for v in e["violations"]:
        print(f"    [IHLAL] {v['rule_id']}: {v['evidence']}")

    title("6) Denetim izi (disa-aktarilir — Art.12 saklama)")
    ps = audit["provenance_summary"]
    print(f"  zaman={audit['at']}")
    print(f"  iddia dagilimi: olgu={ps['olgu']} prior={ps['prior']} ongoru={ps['ongoru']} "
          f"| kaynak={ps['kaynak_sayisi']} | model={audit['model']}")
    print("  -> tam JSON kaydi grounded.export_audit ile disa-aktarilir.")

    title("Deger")
    print("  - Cevap DONMUS degil: canli web + bu musterinin durumuna capali (TEFAS-only, dusuk risk).")
    print("  - Her iddia OLGU/PRIOR/ONGORU + kaynak: denetcinin dogrulayabilecegi sey.")
    print("  - Kural cikti uretilmeden ONCE dayatildi (garanti dili gecemez).")
    print("  - Tam denetim izi tutuldu = lab'larin (Dreaming) KAYBETTIGI sey.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
