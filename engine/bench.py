#!/usr/bin/env python3
"""bench.py — M3Mgine yayınlanabilir kalite benchmark'ı (tek komut, gerçek model).

Üç ENFORCE-wedge metriğini (çoğu hafıza aracının ÖLÇMEDİĞİ şey) + retrieval kalitesini
tek raporda toplar. Çıktı:
  benchmarks/results.json   — makine-okur (tüm metrikler + meta)
  benchmarks/REPORT.md      — insan-okur (yöntem + sonuç + tekrar talimatı)

  python bench.py                  # pinli model, repeat=1
  python bench.py --repeat 3       # gürültü azalt (matrisi N koşu üstünde topla)
  python bench.py --model X        # model override
  python bench.py --no-retrieval   # sadece wedge metrikleri (embed gerekmez)

Gerçek LLM çağırır = token harcar. CI'da DEĞİL (kasıtlı, on-demand).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ENG = Path(__file__).resolve().parent
sys.path.insert(0, str(ENG))
sys.path.insert(0, str(ENG / "tests"))

DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
OUT_DIR = ENG / "benchmarks"


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


# Retrieval eval gerçek belleğe yazar; store import-anında CCE_DB'ye bağlanır.
# İzole + GÜNCEL şemalı taze bir temp DB'yi eval import'larından ÖNCE kur (yoksa eski
# şemalı default db'ye bağlanıp 'no column subject_party' verir).
import os  # noqa: E402
import tempfile  # noqa: E402

_BENCH_DB = Path(tempfile.gettempdir()) / "cce_bench.db"
for _s in ("", "-wal", "-shm"):
    _p = Path(str(_BENCH_DB) + _s)
    if _p.exists():
        _p.unlink()
os.environ["CCE_DB"] = str(_BENCH_DB)

_load_env()
import eval_correction as ec  # noqa: E402
import eval_enforce as ee  # noqa: E402
import eval_extract as ex  # noqa: E402
from llm import LLMError, call_model, has_key  # noqa: E402


def _round(d: dict, keys: list[str]) -> dict:
    out = {}
    for k in keys:
        v = d.get(k)
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def _enforce(model: str, repeat: int) -> dict:
    """Baseline (eski prompt) vs production (canlı FP-disiplin judge = policy_engine.JUDGE_SYS)."""
    base = ee.run(model, "baseline", repeat=repeat)
    prod = ee.run(model, "fpdisc", repeat=repeat)
    keys = ["n", "acc", "f1", "prec", "rec", "fp", "fp_rate", "fn", "fmt_fail_rate"]
    return {
        "n_cases": len(ee.CASES),
        "n_rules": len(ee.RULES),
        "repeat": repeat,
        "baseline": _round(base, keys),
        "production": _round(prod, keys),
    }


def _correction(model: str, repeat: int) -> dict:
    r = ec.run(model, repeat=repeat)
    return {
        "n_corrections": len(ec.DATASET),
        "repeat": repeat,
        **_round(r, ["recall", "spec", "f1", "caught", "total_v", "passed",
                     "total_c", "compile_fail"]),
    }


def _extract(model: str, repeat: int) -> dict:
    r = ex.run(model, repeat=repeat)
    return {
        "n_messages": len(ex.DATASET),
        "n_gold": r["total_g"],
        "repeat": repeat,
        **_round(r, ["recall", "cov", "total_g", "leak", "empty_extra", "total_extracted"]),
    }


def _retrieval() -> dict | None:
    """sparse (her zaman) + hybrid (embed varsa); full + held-out split."""
    try:
        import test_engine_quality as tq
    except Exception as e:
        print(f"  retrieval: ATLA (import hatası: {type(e).__name__}: {e})")
        return None
    keys = ["n", "recall@1", "recall@5", "recall@10", "mrr", "ndcg@10"]
    out = {
        "corpus_size": len(tq.MEMS),
        "n_queries": len([g for g in tq.GOLD]),
        "sparse": _round(tq.run_eval("b_sp", lambda *a, **k: None), keys),
        "sparse_heldout": _round(tq.run_eval("b_sp_ho", lambda *a, **k: None, split="heldout"), keys),
    }
    if tq._embed_available():
        out["hybrid"] = _round(tq.run_eval("b_hy", tq._REAL_EMBED), keys)
        out["hybrid_heldout"] = _round(tq.run_eval("b_hy_ho", tq._REAL_EMBED, split="heldout"), keys)
    else:
        out["hybrid"] = None
        out["hybrid_heldout"] = None
    return out


def _pct(x) -> str:
    return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "—"


def render_md(res: dict) -> str:
    m = res["model"]
    en = res["enforce"]
    co = res["correction"]
    xt = res["extract"]
    rt = res.get("retrieval")
    L = []
    L.append("# M3Mgine — Quality Benchmark")
    L.append("")
    L.append(f"_Model: `{m}` · repeat: {res['repeat']} · generated: {res['generated_at']}_")
    L.append("")
    L.append("Reproducible, held-out quality measurements for the engine. The first three "
             "sections measure the **enforcement loop** — the part most memory tools do not "
             "have, and therefore do not measure. The last section is standard retrieval quality.")
    L.append("")
    L.append("> Honesty note: datasets are deliberately small and adversarial (early-stage). "
             "Single model, single run unless `--repeat` raised. Numbers move run-to-run on a "
             "judge model; treat as directional, reproduce with `python bench.py`.")
    L.append("")

    # 1. Enforcement
    L.append("## 1. Enforcement gate (soft LLM-judge)")
    L.append("")
    L.append(f"Does the judge correctly block violating output and pass clean output? "
             f"{en['n_cases']} adversarial cases across {en['n_rules']} rule domains "
             f"(evasion, embedded violations, false-positive bait, cross-lingual). "
             f"**False-positive rate** matters most — blocking clean output makes the product unusable.")
    L.append("")
    L.append("| Metric | Baseline prompt | Production (FP-discipline) |")
    L.append("|---|---|---|")
    b, p = en["baseline"], en["production"]
    L.append(f"| Accuracy | {_pct(b['acc'])} | **{_pct(p['acc'])}** |")
    L.append(f"| F1 | {_pct(b['f1'])} | **{_pct(p['f1'])}** |")
    L.append(f"| Precision | {_pct(b['prec'])} | {_pct(p['prec'])} |")
    L.append(f"| Recall | {_pct(b['rec'])} | {_pct(p['rec'])} |")
    L.append(f"| False-positive rate | {_pct(b['fp_rate'])} | **{_pct(p['fp_rate'])}** |")
    L.append(f"| Format-fail rate | {_pct(b['fmt_fail_rate'])} | {_pct(p['fmt_fail_rate'])} |")
    L.append("")

    # 2. Correction -> rule generalization
    L.append("## 2. Correction → rule generalization")
    L.append("")
    L.append(f"The unique metric: take a single plain-language correction, compile it into a "
             f"rule, and check it on **held-out** cases never seen at compile time. "
             f"{co['n_corrections']} corrections; each with unseen violations (must catch) and "
             f"unseen clean outputs (must pass).")
    L.append("")
    L.append("| Metric | Score |")
    L.append("|---|---|")
    L.append(f"| Generalization recall (catches unseen violations) | **{_pct(co['recall'])}** ({co['caught']}/{co['total_v']}) |")
    L.append(f"| Specificity (passes unseen clean) | **{_pct(co['spec'])}** ({co['passed']}/{co['total_c']}) |")
    L.append(f"| F1 | {_pct(co['f1'])} |")
    L.append(f"| Compile failures | {co['compile_fail']} |")
    L.append("")

    # 3. Extraction
    L.append("## 3. Memory extraction")
    L.append("")
    L.append(f"From raw messages, extract durable atomic facts; drop pleasantries, questions, "
             f"transient state. {xt['n_messages']} messages, {xt['n_gold']} gold facts "
             f"(one message is deliberately fact-free — over-extraction test).")
    L.append("")
    L.append("| Metric | Score |")
    L.append("|---|---|")
    L.append(f"| Recall (gold facts extracted) | **{_pct(xt['recall'])}** ({xt['cov']}/{xt['total_g']}) |")
    L.append(f"| Noise leak (junk wrongly extracted) | {xt['leak']} |")
    L.append(f"| Over-extraction (facts from fact-free message) | {xt['empty_extra']} |")
    L.append("")

    # 4. Retrieval
    L.append("## 4. Retrieval quality")
    L.append("")
    if rt:
        L.append(f"Hybrid retrieval over a {rt['corpus_size']}-memory corpus, {rt['n_queries']} "
                 f"Turkish queries (inflection, paraphrase, synonym, typo, acronym, multi-gold, "
                 f"ellipsis). Held-out split is closed to tuning. Ablation: sparse (lexical/BM25) "
                 f"vs hybrid (dense + lexical + RRF).")
        L.append("")
        L.append("| Configuration | R@1 | R@5 | R@10 | MRR | NDCG@10 |")
        L.append("|---|---|---|---|---|---|")

        def _row(label, d):
            if not d:
                return f"| {label} | — | — | — | — | — |"
            return (f"| {label} | {d['recall@1']:.2f} | {d['recall@5']:.2f} | "
                    f"{d['recall@10']:.2f} | {d['mrr']:.2f} | {d['ndcg@10']:.2f} |")
        L.append(_row("Sparse (lexical/BM25)", rt["sparse"]))
        L.append(_row("Sparse — held-out", rt["sparse_heldout"]))
        L.append(_row("Hybrid (dense+lex+RRF)", rt.get("hybrid")))
        L.append(_row("Hybrid — held-out", rt.get("hybrid_heldout")))
        L.append("")
    else:
        L.append("_Skipped._")
        L.append("")

    L.append("## Reproduce")
    L.append("")
    L.append("```bash")
    L.append("cd engine")
    L.append("python bench.py            # needs an LLM line configured (see README → Configuration)")
    L.append("```")
    L.append("")
    L.append("Raw numbers: [`results.json`](results.json).")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="M3Mgine yayınlanabilir kalite benchmark'ı")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--no-retrieval", action="store_true")
    args = ap.parse_args()

    if not has_key():
        print("SKIP: LLM hattı yok (CCE_LLM_API_KEY). engine/.env kontrol et.")
        return 0

    # model-probe: erişilemeyen model fail-closed FP'lerle yanıltır -> önce ping
    try:
        call_model("Reply with the single word ok.", "ping", max_tokens=5, model=args.model)
    except LLMError as e:
        print(f"HATA: model erişilemiyor ({args.model}): {str(e)[:100]}")
        return 1

    print(f"== M3Mgine benchmark | model={args.model} | repeat={args.repeat} ==\n")
    t0 = time.time()

    print("  [1/4] enforce gate (baseline vs production)...", flush=True)
    enforce = _enforce(args.model, args.repeat)
    print(f"        production: acc={_pct(enforce['production']['acc'])} "
          f"FP-rate={_pct(enforce['production']['fp_rate'])}")

    print("  [2/4] correction -> rule generalization...", flush=True)
    correction = _correction(args.model, args.repeat)
    print(f"        recall={_pct(correction['recall'])} spec={_pct(correction['spec'])}")

    print("  [3/4] memory extraction...", flush=True)
    extract = _extract(args.model, args.repeat)
    print(f"        recall={_pct(extract['recall'])} noise-leak={extract['leak']}")

    retrieval = None
    if not args.no_retrieval:
        print("  [4/4] retrieval quality (sparse + hybrid)...", flush=True)
        retrieval = _retrieval()
        if retrieval:
            hy = retrieval.get("hybrid")
            tag = f"hybrid R@5={hy['recall@5']:.2f}" if hy else "sparse-only (no embed key)"
            print(f"        {tag}")

    res = {
        "model": args.model,
        "repeat": args.repeat,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "enforce": enforce,
        "correction": correction,
        "extract": extract,
        "retrieval": retrieval,
    }

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    (OUT_DIR / "REPORT.md").write_text(render_md(res), encoding="utf-8")
    print(f"\n  yazıldı: benchmarks/results.json + benchmarks/REPORT.md  ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
