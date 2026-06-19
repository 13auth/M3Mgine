#!/usr/bin/env python3
"""runtime_gate.py — Runtime ENFORCE kapısı (deployment entegrasyonu).

Ajan bir cevabı kullanıcıya VERMEDEN önce burayı çağırır: çıktı tenant
kurallarını ihlal ediyorsa kapı 'block' der → ajan yeniden üretir/düzeltir.
"Hatayı tekrarlama" ancak çıktı dağıtımdan önce dayatılırsa gerçekleşir.

Kullanım (ajan hook / pre-output):
  echo "<aday cevap>" | python runtime_gate.py --tenant t_x --project Acme
  -> stdout JSON: {"allow":bool,"violations":[...]}
  -> exit 0 allow, 1 block (fail-closed: motor hatasında da block, --fail-open ile değiş)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402
from policy_engine import enforce  # noqa: E402


def gate(output: str, tenant_id: str, project: str | None = None,
         run_soft: bool = True, fail_open: bool = False, record: bool = True) -> dict:
    try:
        rules = store.get_rules(tenant_id, project)
        res = enforce(output, rules=rules, project=project, run_soft=run_soft)
        if record:
            store.record_enforcement(tenant_id, res.evaluated,
                                     [v.as_dict() for v in res.violations], source="gate")
        # FAIL-CLOSED: ihlal YA DA değerlendirilemeyen yüksek-önemli soft kural -> blokla
        allow = res.safe(fail_open=fail_open)
        return {"allow": allow,
                "violations": [v.as_dict() for v in res.violations],
                "unevaluated": res.unevaluated,
                "deferred": res.deferred,
                "blocked_reason": ("violation" if res.violations
                                   else "unevaluated_high_severity" if not allow else None)}
    except Exception as e:  # motor hatası -> fail-closed (güvenli) varsayılan
        return {"allow": bool(fail_open), "error": str(e), "violations": []}


def main():
    ap = argparse.ArgumentParser(description="Runtime enforce gate")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--project", default=None)
    ap.add_argument("--no-soft", action="store_true", help="soft (LLM) kuralları atla")
    ap.add_argument("--fail-open", action="store_true", help="motor hatasında engelleme (varsayılan: engelle)")
    ap.add_argument("text", nargs="*", help="aday çıktı (verilmezse stdin)")
    a = ap.parse_args()
    output = " ".join(a.text).strip() or sys.stdin.read().strip()
    if not output:
        print(json.dumps({"allow": True, "violations": [], "note": "empty output"}))
        return 0
    result = gate(output, a.tenant, a.project, run_soft=not a.no_soft, fail_open=a.fail_open)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["allow"] else 1


if __name__ == "__main__":
    sys.exit(main())
