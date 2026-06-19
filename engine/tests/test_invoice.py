"""test_invoice.py — dönem fatura/kullanım-özeti: KDV matematiği + op-aşımı (overage). Key'siz, taze DB."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_invoice.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import billing  # noqa: E402
import store  # noqa: E402

FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant("I", "I", "kI")

print("== 1) free: ücretsiz, toplam 0 ==")
inv = billing.build_invoice("I")
check(inv["plan"] == "free", "plan free")
check(inv["total_try"] == 0.0, f"toplam 0 (geldi {inv['total_try']})")
check(inv["preview"] is True, "preview bayrağı")

print("== 2) solo (349 KDV dahil): matrah 290.83 + kdv 58.17 = 349 ==")
store.set_plan("I", "solo")
inv = billing.build_invoice("I")
check(inv["base_price_try"] == 349, "base 349 (KDV dahil)")
check(abs(inv["matrah_try"] - 290.83) < 0.02, f"matrah ~290.83 (geldi {inv['matrah_try']})")
check(abs(inv["kdv_try"] - 58.17) < 0.02, f"kdv ~58.17 (geldi {inv['kdv_try']})")
check(inv["total_try"] == 349.0, f"toplam 349 (geldi {inv['total_try']})")
check(inv["ops_overage"] == 0.0, "aşım yok")

print("== 3) op-aşımı: limit 25k, kullanım 27k -> 2k aşım -> overage 24 TL (KDV hariç) ==")
store.add_usage("I", billing.current_period(), 27000)
inv2 = billing.build_invoice("I")
check(inv2["ops_used"] == 27000.0, f"kullanım 27000 (geldi {inv2['ops_used']})")
check(inv2["ops_overage"] == 2000.0, f"aşım 2000 (geldi {inv2['ops_overage']})")
check(abs(inv2["overage_net_try"] - 24.0) < 0.01, f"overage 24 net (geldi {inv2['overage_net_try']})")
# matrah = 290.83 + 24 = 314.83 ; total = 314.83 * 1.20 = 377.80
check(abs(inv2["matrah_try"] - 314.83) < 0.02, f"matrah ~314.83 (geldi {inv2['matrah_try']})")
check(abs(inv2["total_try"] - 377.80) < 0.05, f"toplam ~377.80 (geldi {inv2['total_try']})")

print("== 4) upgrade_hint: free -> solo + overage birim; enterprise üstü yok ==")
h = billing.upgrade_hint("free")
check(h.get("suggested_plan") == "solo", f"free -> solo (geldi {h.get('suggested_plan')})")
check(h.get("suggested_price_try") == 349, "solo fiyat 349")
check(h.get("overage_per_1k_try") == 12.0, "overage 12/1k")
check(h.get("checkout_path") == "/v1/billing/checkout", "checkout yolu")
he = billing.upgrade_hint("enterprise")
check("suggested_plan" not in he, "enterprise üstü yok")

if FAILS:
    print(f"test_invoice.py: FAIL ({len(FAILS)})")
    sys.exit(1)
print("test_invoice.py: PASS")
