#!/usr/bin/env python3
"""evaluator.py — stage 6 MEASURE: kural-bazlı uyum + held-out ayrımı.

Sistem gerçekten öğreniyor mu? Tek cevap bu. Her test senaryosunun çıktısını
(ajandan üretilmiş veya verilmiş) tenant kurallarına karşı enforce eder, kural
bazında ihlali sayar, İKİ KOVAYI AYRI raporlar:
  from-corrections -> ezber/overfit ölçer
  held-out         -> GERÇEK genelleme ölçer (düşükse sistem ezberlemiş)

Bir senaryo 'pass' = enforce hiç ihlal bulmadı.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store  # noqa: E402
from llm import LLMError, call_model, llm_available  # noqa: E402
from policy_engine import enforce  # noqa: E402
from redact import scrub_secrets  # noqa: E402

AGENT_SYS = ("Sen bir ajansın. Verilen görevi yap. Çıktın kısa ve doğrudan olsun.")

_FIELD = re.compile(r"^\s*(Input|Expected behavior|Failure pattern|Pass criteria|Output)\s*:\s*(.*)$",
                    re.IGNORECASE)
_KEYMAP = {"input": "input", "expected behavior": "expected", "failure pattern": "failure",
           "pass criteria": "pass", "output": "output"}


def parse_trap(text: str) -> dict:
    fields = {"input": "", "expected": "", "failure": "", "pass": "", "output": ""}
    cur = None
    for line in text.splitlines():
        m = _FIELD.match(line)
        if m:
            cur = _KEYMAP[m.group(1).lower()]
            if m.group(2).strip():
                fields[cur] += m.group(2).strip() + "\n"
            continue
        if cur and line.strip() and not line.startswith("#"):
            fields[cur] += line.strip() + "\n"
    return {k: v.strip() for k, v in fields.items()}


def load_cases(folder: Path, bucket: str) -> list[dict]:
    cases = []
    if not folder.exists():
        return cases
    for f in sorted(folder.glob("*.md")):
        d = parse_trap(f.read_text(encoding="utf-8"))
        if d["input"]:
            cases.append({"name": f.stem, "bucket": bucket, "input": d["input"],
                          "output": d.get("output") or None})
    return cases


def _agent_output(case: dict) -> str:
    if case.get("output"):
        return case["output"]
    clean, _ = scrub_secrets(case["input"])  # LLM'e giden girdiyi scrub'la
    return call_model(AGENT_SYS, clean, max_tokens=500)


def run_eval(tenant_id: str, cases: list[dict], project: str | None = None,
             record: bool = True, run_soft: bool | None = None) -> dict:
    # run_soft=None -> LLM varsa soft kuralları da ölç; yoksa hard-only
    if run_soft is None:
        run_soft = llm_available()
    rules = store.get_rules(tenant_id, project)
    buckets: dict[str, dict] = {}
    per_rule: dict[str, int] = {}
    skipped = 0

    for case in cases:
        b = buckets.setdefault(case["bucket"], {"total": 0, "passed": 0, "cases": []})
        try:
            output = _agent_output(case)
        except LLMError as e:
            skipped += 1
            b["cases"].append({"name": case["name"], "status": "SKIP", "reason": str(e)})
            continue
        res = enforce(output, rules=rules, project=project, run_soft=run_soft)
        b["total"] += 1
        ok = res.passed
        b["passed"] += 1 if ok else 0
        for v in res.violations:
            per_rule[v.rule_id] = per_rule.get(v.rule_id, 0) + 1
        if record:
            # checks (denominator) + violations -> compliance_by_rule gerçek oran verebilsin
            store.record_enforcement(tenant_id, res.evaluated,
                                     [v.as_dict() for v in res.violations],
                                     source=case["bucket"], trace=case["name"])
        b["cases"].append({"name": case["name"], "status": "PASS" if ok else "FAIL",
                           "violations": [v.rule_id for v in res.violations]})

    report = {"buckets": {}, "per_rule": per_rule, "skipped": skipped}
    for name, b in buckets.items():
        rate = (b["passed"] / b["total"]) if b["total"] else None
        report["buckets"][name] = {"passed": b["passed"], "total": b["total"],
                                   "compliance": rate, "cases": b["cases"]}

    fc = report["buckets"].get("from-corrections", {}).get("compliance")
    ho = report["buckets"].get("held-out", {}).get("compliance")
    notes = []
    if fc is not None and ho is not None:
        if ho < 0.6:
            notes.append("Held-out düşük: sistem GENELLEMİYOR, düzeltmeleri ezberliyor.")
        if fc - ho > 0.3:
            notes.append(f"Overfit: from-corrections {fc:.0%} >> held-out {ho:.0%}. Kurallar fazla spesifik.")
        if ho >= 0.6 and fc - ho <= 0.3:
            notes.append("Held-out sağlam, uçurum dar: sistem genelliyor gibi.")
    report["notes"] = notes
    return report


def save_report(report: dict, path: str) -> None:
    import json
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def load_report(path: str) -> dict:
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))


def regression(baseline: dict, current: dict) -> dict:
    """CI-for-rules: current'ı baseline'a göre kıyasla. Gerileyen kova/kural = gate FAIL.
    Araştırmanın 'kimse yapmıyor' dediği longitudinal, kural-bazlı regresyon takibi."""
    out = {"regressed_buckets": [], "worse_rules": [], "gate_pass": True}
    for b, cur in current.get("buckets", {}).items():
        base = baseline.get("buckets", {}).get(b)
        if not base or cur.get("compliance") is None or base.get("compliance") is None:
            continue
        if cur["compliance"] < base["compliance"] - 1e-9:
            out["regressed_buckets"].append({"bucket": b, "from": base["compliance"],
                                             "to": cur["compliance"]})
            out["gate_pass"] = False
    base_pr = baseline.get("per_rule", {})
    for rid, n in current.get("per_rule", {}).items():
        prev = base_pr.get(rid, 0)
        if n > prev:
            out["worse_rules"].append({"rule": rid, "from": prev, "to": n})
            out["gate_pass"] = False
    return out


def format_regression(reg: dict) -> str:
    lines = ["== REGRESYON KAPISI =="]
    if reg["gate_pass"]:
        lines.append("  [GEÇTİ] gerileme yok")
    else:
        lines.append("  [BLOKLA] gerileme tespit edildi:")
        for b in reg["regressed_buckets"]:
            lines.append(f"    kova {b['bucket']}: {b['from']:.0%} -> {b['to']:.0%}")
        for r in reg["worse_rules"]:
            lines.append(f"    kural {r['rule']}: ihlal {r['from']} -> {r['to']}")
    return "\n".join(lines)


def format_report(report: dict) -> str:
    lines = ["== EVAL (kural-bazlı uyum) =="]
    for name, b in report["buckets"].items():
        comp = f"{b['compliance']:.0%}" if b["compliance"] is not None else "-"
        lines.append(f"  [{name}] uyum {comp}  ({b['passed']}/{b['total']})")
        for c in b["cases"]:
            extra = (" -> " + ", ".join(c["violations"])) if c.get("violations") else ""
            lines.append(f"      [{c['status']}] {c['name']}{extra}")
    if report["per_rule"]:
        lines.append("  -- kural başına ihlal --")
        for rid, n in sorted(report["per_rule"].items(), key=lambda x: -x[1]):
            lines.append(f"      {rid}: {n}")
    if report.get("skipped"):
        lines.append(f"  [ATLANAN: {report['skipped']} (LLM yok, output verilmemiş)]")
    for note in report.get("notes", []):
        lines.append(f"  [YORUM] {note}")
    return "\n".join(lines)
