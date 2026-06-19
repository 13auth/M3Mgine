#!/usr/bin/env python3
"""eval_extract.py — hafıza ÇIKARIM (extract_facts) kalite eval'i (gerçek model, on-demand).

Hafıza ürününün çekirdeği: ham mesajdan KALICI, atomik gerçekleri çıkar; nezaket/soru/
geçici durumu AT. Ölçüm yoksa EXTRACT_SYS tuning'i tahmin. CI'ya GİRMEZ (token).

Metrik (deterministik, anchor-term — judge subjektifliği yok, embedding gerekmez):
  - RECALL: gold kalıcı gerçeklerin kaçı çıkarıldı (her gold'un ayırt-edici kök terimi
    çıkarılan fact'lerde geçiyor mu; TR çekim-eki için kök/varyant listesi).
  - NOISE-LEAK: çıkarılmaması gereken gürültü (nezaket/soru/geçici) terimi kaç kez sızdı.
  - VERBOSITY: toplam çıkarılan fact sayısı (aşırı-çıkarım sinyali).

Kullanım:
  python eval_extract.py --repeat 3
  python eval_extract.py --models "google/gemini-2.5-flash-lite,openai/gpt-4o-mini" --verbose
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
import memory  # noqa: E402
from llm import has_key  # noqa: E402

# her vaka: metin + gold (etiket, [kök-varyant anchor'ları]) + sızmaması gereken gürültü terimleri
DATASET = [
    {
        "text": "Merhaba! Bu arada ben İzmir'de yaşıyorum ve vegan besleniyorum. Bugün hava çok güzel, yardımın için teşekkürler.",
        "gold": [("İzmir'de yaşıyor", ["izmir"]),
                 ("vegan", ["vegan", "et yem", "et tüketm"])],
        "noise": ["teşekkür", "hava güzel"],
    },
    {
        "text": "Adım Selin, 28 yaşındayım. Kız kardeşimin adı Deniz. Şu an biraz yorgunum ama genelde iyiyim.",
        "gold": [("adı Selin", ["selin"]),
                 ("28 yaşında", ["28"]),
                 ("kardeşi Deniz", ["deniz"])],
        "noise": ["yorgun"],
    },
    {
        "text": "Kedimin adı Pamuk, 3 yaşında bir tekir. Acaba kedilere çikolata vermek zararlı mıdır?",
        "gold": [("kedisi Pamuk", ["pamuk"]),
                 ("kedisi 3 yaşında", ["3 ya", "üç ya"]),
                 ("tekir", ["tekir"])],
        "noise": ["çikolata", "zararlı"],
    },
    {
        "text": "Geçen yıl Ankara'dan İstanbul'a taşındım, yazılım mühendisiyim. Galatasaray taraftarıyım bu arada.",
        "gold": [("İstanbul'da", ["istanbul"]),
                 ("yazılım mühendisi", ["mühendis", "yazılım"]),
                 ("Galatasaray", ["galatasaray"])],
        "noise": [],
    },
    {
        "text": "Çok teşekkürler, harika bir gün geçirdim. Yarın sınavım var, biraz stresliyim açıkçası.",
        "gold": [],   # kalıcı gerçek YOK -> hiçbir şey çıkmamalı (aşırı-çıkarım testi)
        "noise": ["teşekkür", "stres", "sınav", "harika bir gün"],
    },
    {
        "text": "Kahveyi sütsüz severim, ayrıca laktoz intoleransım var. Bugün toplantı vardı, baya yoruldum.",
        "gold": [("kahveyi sütsüz", ["sütsüz"]),
                 ("laktoz intoleransı", ["laktoz"])],
        "noise": ["toplantı", "yoruldu", "yorgun"],
    },
    {
        "text": "Oğlum 5 yaşında ve fıstık alerjisi var, çok dikkat ediyoruz. Sana gerçekten güveniyorum.",
        "gold": [("oğlu 5 yaşında", ["5 ya", "beş ya"]),
                 ("fıstık alerjisi", ["fıstık", "alerji"])],
        "noise": ["güveni"],
    },
]


def _fold(s: str) -> str:
    """TR-güvenli fold: İ/I/ı'yı lower()'dan ÖNCE indir (store._fold ile aynı). Yoksa
    'İzmir'.lower() -> 'i̇zmir' (combining dot) ascii anchor'la eşleşmez = sahte miss."""
    return (s or "").replace("İ", "i").replace("I", "i").replace("ı", "i").lower()


def _covered(anchors, blob: str) -> bool:
    return any(_fold(a) in blob for a in anchors)


def run(model: str, repeat: int = 1):
    import llm
    orig = llm.LLM_MODEL
    llm.LLM_MODEL = model
    t0 = time.time()
    cov = total_g = 0
    leak = 0
    empty_extra = 0     # gold-boş mesajdan çıkarılan fact sayısı (aşırı-çıkarım)
    total_extracted = 0
    fails = []
    try:
        jobs = [(ci, case) for ci, case in enumerate(DATASET) for _ in range(repeat)]

        def _one(job):
            _ci, case = job
            facts = memory.extract_facts(case["text"])
            return case, facts

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_one, jobs))

        for case, facts in results:
            blob = _fold(" ".join(facts))
            total_extracted += len(facts)
            for label, anchors in case["gold"]:
                total_g += 1
                if _covered(anchors, blob):
                    cov += 1
                else:
                    fails.append(("KAÇAN-GOLD", label, blob[:60]))
            for nz in case["noise"]:
                if _fold(nz) in blob:
                    leak += 1
                    fails.append(("GÜRÜLTÜ-SIZDI", nz, blob[:60]))
            if not case["gold"]:
                empty_extra += len(facts)
    finally:
        llm.LLM_MODEL = orig

    dt = time.time() - t0
    recall = cov / total_g if total_g else 0.0
    return dict(model=model, recall=recall, cov=cov, total_g=total_g, leak=leak,
                empty_extra=empty_extra, total_extracted=total_extracted, sec=dt, fails=fails)


def main() -> int:
    ap = argparse.ArgumentParser(description="extract_facts kalite eval'i")
    ap.add_argument("--models", default="google/gemini-2.5-flash-lite")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not has_key():
        print("SKIP: LLM hattı yok (CCE_LLM_API_KEY).")
        return 0

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"== extract_facts eval | {len(DATASET)} mesaj × {args.repeat} tekrar | modeller={models} ==")
    ng = sum(len(c["gold"]) for c in DATASET)
    print(f"   (gold {ng} kalıcı gerçek; 1 mesaj kasıtlı gold-boş = aşırı-çıkarım testi)\n")

    results = []
    for model in models:
        try:
            r = run(model, args.repeat)
        except Exception as e:
            print(f"  {model:<34} HATA: {type(e).__name__}: {e}")
            continue
        results.append(r)
        print(f"  {model:<34} recall={r['recall']*100:4.0f}% ({r['cov']}/{r['total_g']}) "
              f"gürültü-sızıntı={r['leak']} gold-boş-çıkarım={r['empty_extra']} "
              f"toplam-fact={r['total_extracted']} {r['sec']:.0f}s")
        if args.verbose:
            seen = set()
            for kind, a, blob in r["fails"]:
                if (kind, a) in seen:
                    continue
                seen.add((kind, a))
                print(f"        ! {kind}: {a}  | çıkarılan: {blob}")
        print()

    if results:
        best = max(results, key=lambda r: (r["recall"], -r["leak"]))
        print("=" * 70)
        print(f"EN İYİ: {best['model']} -> recall={best['recall']*100:.0f}% "
              f"gürültü-sızıntı={best['leak']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
