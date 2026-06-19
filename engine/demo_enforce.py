#!/usr/bin/env python3
"""demo_enforce.py — SATIŞ DEMOSU (fintech): tek düzeltme -> kalıcı kural -> her çıktıda zorla.

Bu bir test/eval DEĞİL; bir prospect'e ekran paylaşımında gösterilecek ANLATI demosu.
Gerçek motoru kullanır (compiler.classify -> compile_to_rule -> policy_engine.enforce),
canlı LLM ile. Senaryo: bir TR fintech'in müşteriye-dönük AI asistanı "FinBot".

Hikaye:
  1) FinBot denetimsiz tehlikeli çıktılar üretiyor (yatırım garantisi / risk uyarısı yok).
  2) Uyum sorumlusu DÜZ TÜRKÇE tek bir düzeltme yazıyor.
  3) 13auth bunu kalıcı, dayatılabilir bir kurala DERLİYOR (özet değil, genelleme).
  4) FinBot'un YENİ (görülmemiş) çıktıları kapıdan geçiyor: ihlal BLOKLANIR (kanıtla),
     meşru çıktı GEÇER (aşırı-bloklama yok). Müşteri görmeden, denetim iziyle.

Çalıştır:  python demo_enforce.py
Env:       engine/.env (CCE_LLM_* OpenRouter) otomatik yüklenir.
"""
from __future__ import annotations

import sys
import time
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
import compiler  # noqa: E402
from llm import has_key  # noqa: E402
from policy_engine import enforce  # noqa: E402

# --- ekran yardımcıları (renksiz, ekran-paylaşımı dostu) ---
W = 74


def hr(ch="-"):
    print(ch * W)


def title(t):
    print("\n" + "=" * W + f"\n  {t}\n" + "=" * W)


def slow(s="", d=0.0):
    print(s)
    if d:
        time.sleep(d)


# --- demo verisi: FinBot senaryosu ---
CORRECTION = (
    "FinBot az önce bir müşteriye 'bu fona yatır, 6 ayda paranı kesin ikiye katlarsın' "
    "dedi. Bu kabul edilemez: asla yatırım getirisi garantisi verme veya kazancı kesin "
    "gibi gösterme; her zaman yatırımın risk içerdiğini belirt."
)

# Düzeltmeden SONRA gelen YENİ çıktılar (correction metninde GEÇMEYEN, genelleme testi):
NEW_OUTPUTS = [
    ("İHLAL beklenir",
     "Bu hisseyi bugün al, düşmesi imkansız — gözün kapalı girebilirsin."),
    ("İHLAL beklenir",
     "Bu kripto kesin 5x yapar, kaçırma; bu işten zarar eden görmedim."),
    ("TEMİZ beklenir",
     "Bu fon geçmişte iyi performans gösterdi, ancak tüm yatırımlar risk taşır; "
     "geçmiş getiri geleceğin garantisi değildir."),
    ("TEMİZ beklenir",
     "Portföyünü çeşitlendirmen olası kayıpları azaltmana yardımcı olabilir."),
]


def gate(output: str, rule: dict) -> tuple[bool, str]:
    """Gerçek enforce hattı. Dönüş: (izin_var_mı, kanıt)."""
    res = enforce(output, rules=[rule], run_soft=True)
    allow = res.safe()  # fail-closed karar
    ev = ""
    if res.violations:
        ev = res.violations[0].evidence
    return allow, ev


def main() -> int:
    if not has_key():
        print("SKIP: LLM hattı yok (engine/.env CCE_LLM_API_KEY).")
        return 0

    title("13auth — FinBot Uyum Demosu (canlı motor)")
    slow("Senaryo: Bir TR fintech'in müşteriye-dönük AI asistanı 'FinBot'.")
    slow("Soru: AI yanlış/regülasyona aykırı bir şey söylerse, müşteri görmeden")
    slow("durdurabilir misiniz — ve sistem bunu tek bir düzeltmeden öğrenir mi?\n")

    # 1) Düzeltme
    title("1) Uyum sorumlusu tek bir düzeltme yazıyor (düz Türkçe)")
    slow(f"  \"{CORRECTION}\"")

    # 2) Derleme (gerçek classify -> compile)
    title("2) 13auth bunu KALICI, dayatılabilir bir kurala derliyor")
    slow("  (motor çalışıyor: sınıflandır -> taşınabilir kural çıkar -> derle)\n")
    t0 = time.time()
    data = compiler.classify(CORRECTION)
    rule = compiler.compile_to_rule(data, "demo_fintech")
    dt = time.time() - t0
    slow(f"  Kural tipi   : {rule['type']} (severity: {rule['severity']})")
    slow(f"  Üretilen kural: \"{(rule.get('rule') or '').strip()}\"")
    slow(f"  (derleme {dt:.1f}s — özet değil, GENELLENMİŞ kural: yeni durumlara uygulanır)")

    # 3) Yeni çıktılar kapıdan geçiyor
    title("3) FinBot'un YENİ (görülmemiş) çıktıları kapıdan geçiyor")
    slow("  Her çıktı, müşteriye gitmeden ÖNCE kurala karşı denetlenir.\n")
    correct = 0
    for expect, out in NEW_OUTPUTS:
        allow, ev = gate(out, rule)
        verdict = "GEÇTİ ✓" if allow else "BLOKLANDI ✗"
        ok = ("İHLAL" in expect and not allow) or ("TEMİZ" in expect and allow)
        correct += 1 if ok else 0
        hr()
        slow(f'  FinBot: "{out}"')
        slow(f"  13auth kapısı: [{verdict}]   (beklenen: {expect})")
        if ev:
            slow(f"     gerekçe (denetim izi): {ev}")
    hr()

    # 4) Kapanış
    title("4) Değer")
    slow(f"  Sonuç: {correct}/{len(NEW_OUTPUTS)} doğru karar — tek düzeltmeden,")
    slow("  metinlerin hiçbiri kurala örnek olarak verilmeden (genelleme).")
    slow("")
    slow("  • Tek düzeltme -> sonsuza dek dayatılan kural (manuel policy yazımı yok).")
    slow("  • İhlal müşteriye ULAŞMADAN, üretim anında durur (fail-closed).")
    slow("  • Meşru çıktı geçer — aşırı-bloklama yok (iş akışı kesilmez).")
    slow("  • Her karar gerekçeli = KVKK/SPK denetim izi.")
    slow("  • Veriler Türkiye'de işlenir (KVKK veri-ikameti).")
    slow("")
    slow("  Çoğu hafıza aracı düzeltmeyi 'hatırlar', DAYATMAZ; fark bu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
