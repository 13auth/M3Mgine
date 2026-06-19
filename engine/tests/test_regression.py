"""test_regression.py — CI-for-rules: baseline'a göre gerileme yakalama (key'siz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_regress.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import store  # noqa: E402
from evaluator import regression, run_eval  # noqa: E402

TENANT = "t_reg"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


store.upsert_tenant(TENANT, "Regress Test", "k_reg")
store.add_rule({"id": "no-poster", "tenant_id": TENANT, "project": "Acme", "type": "hard",
                "severity": "high", "source": "test", "message": "poster yok",
                "rule": "", "forbid_patterns": ["film posteri"], "require_patterns": []})

clean = [
    {"name": "fc1", "bucket": "from-corrections", "input": "x", "output": "kendi kaynağını ekle"},
    {"name": "ho1", "bucket": "held-out", "input": "y", "output": "oynatıcıdır, içerik senin"},
]
broken = [
    {"name": "fc1", "bucket": "from-corrections", "input": "x", "output": "kendi kaynağını ekle"},
    {"name": "ho1", "bucket": "held-out", "input": "y", "output": "yeni film posteri galerisi!"},
]

print("== baseline (temiz) vs current (bozuk) ==")
baseline = run_eval(TENANT, clean, project="Acme", record=False, run_soft=False)
current = run_eval(TENANT, broken, project="Acme", record=False, run_soft=False)
check(baseline["buckets"]["held-out"]["compliance"] == 1.0, "baseline held-out %100")
check(current["buckets"]["held-out"]["compliance"] == 0.0, "current held-out %0 (gerileme)")

reg = regression(baseline, current)
check(not reg["gate_pass"], "regresyon kapısı BLOKLADI (gate_pass False)")
check(any(b["bucket"] == "held-out" for b in reg["regressed_buckets"]), "held-out gerilemesi raporlandı")
check(any(r["rule"] == "no-poster" for r in reg["worse_rules"]), "kural-bazlı gerileme yakalandı")

print("== gerileme yoksa geçer ==")
reg2 = regression(baseline, baseline)
check(reg2["gate_pass"], "aynı rapor -> gate GEÇTİ")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM TESTLER GEÇTİ")
