"""test_metrics.py — istek sayaçları: id-şablonlama + prometheus format (key'siz)."""
import sys
from pathlib import Path

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))
import metrics  # noqa: E402

FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


print("== id-şablonlama (cardinality kontrol) ==")
metrics.record("GET", "/v1/rules/rule-abc", 200)
metrics.record("GET", "/v1/rules/rule-xyz", 200)   # aynı şablon -> 2
metrics.record("DELETE", "/v1/memories/mem-123", 200)
metrics.record("POST", "/v1/check", 429)
snap = metrics.snapshot()
check(snap.get("GET /v1/rules/{id} 200") == 2, f"id'ler {{id}}'e indi, sayı 2 ({snap.get('GET /v1/rules/{id} 200')})")
check("DELETE /v1/memories/{id} 200" in snap, "memories id şablonlandı")
check(snap.get("POST /v1/check 429") == 1, "şablonsuz path aynen, 429 sayıldı")

print("== prometheus format ==")
prom = metrics.prometheus()
check("cce_requests_total" in prom, "metrik adı var")
check('path="/v1/rules/{id}"' in prom and 'status="429"' in prom, "label'lar doğru")
check(prom.startswith("# HELP"), "HELP/TYPE başlığı var")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM METRICS TESTLERİ GEÇTİ")
