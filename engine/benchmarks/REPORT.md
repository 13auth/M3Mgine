# M3Mgine — Quality Benchmark

_Model: `google/gemini-2.5-flash-lite` · repeat: 3 · generated: 2026-06-19_

Reproducible, held-out quality measurements for the engine. The first three sections measure the **enforcement loop** — the part most memory tools do not have, and therefore do not measure. The last section is standard retrieval quality.

> Honesty note: datasets are deliberately small and adversarial (early-stage). Single model, single run unless `--repeat` raised. Numbers move run-to-run on a judge model; treat as directional, reproduce with `python bench.py`.

## 1. Enforcement gate (soft LLM-judge)

Does the judge correctly block violating output and pass clean output? 58 adversarial cases across 11 rule domains (evasion, embedded violations, false-positive bait, cross-lingual). **False-positive rate** matters most — blocking clean output makes the product unusable.

| Metric | Baseline prompt | Production (FP-discipline) |
|---|---|---|
| Accuracy | 95% | **100%** |
| F1 | 96% | **100%** |
| Precision | 95% | 100% |
| Recall | 97% | 100% |
| False-positive rate | 6% | **0%** |
| Format-fail rate | 0% | 0% |

## 2. Correction → rule generalization

The unique metric: take a single plain-language correction, compile it into a rule, and check it on **held-out** cases never seen at compile time. 8 corrections; each with unseen violations (must catch) and unseen clean outputs (must pass).

| Metric | Score |
|---|---|
| Generalization recall (catches unseen violations) | **100%** (48/48) |
| Specificity (passes unseen clean) | **100%** (48/48) |
| F1 | 100% |
| Compile failures | 0 |

## 3. Memory extraction

From raw messages, extract durable atomic facts; drop pleasantries, questions, transient state. 7 messages, 45 gold facts (one message is deliberately fact-free — over-extraction test).

| Metric | Score |
|---|---|
| Recall (gold facts extracted) | **100%** (45/45) |
| Noise leak (junk wrongly extracted) | 0 |
| Over-extraction (facts from fact-free message) | 0 |

## 4. Retrieval quality

Hybrid retrieval over a 66-memory corpus, 35 Turkish queries (inflection, paraphrase, synonym, typo, acronym, multi-gold, ellipsis). Held-out split is closed to tuning. Ablation: sparse (lexical/BM25) vs hybrid (dense + lexical + RRF).

| Configuration | R@1 | R@5 | R@10 | MRR | NDCG@10 |
|---|---|---|---|---|---|
| Sparse (lexical/BM25) | 0.71 | 1.00 | 1.00 | 0.85 | 0.89 |
| Sparse — held-out | 0.70 | 1.00 | 1.00 | 0.85 | 0.90 |
| Hybrid (dense+lex+RRF) | 0.91 | 1.00 | 1.00 | 0.96 | 0.97 |
| Hybrid — held-out | 0.90 | 1.00 | 1.00 | 0.95 | 0.97 |

## Reproduce

```bash
cd engine
python bench.py            # needs an LLM line configured (see README → Configuration)
```

Raw numbers: [`results.json`](results.json).
