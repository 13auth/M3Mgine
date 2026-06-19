"""test_billing.py — abonelik planı + metering + kota enforcement (key'siz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_billing.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import billing  # noqa: E402
import store  # noqa: E402

T = "t_bill"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "Bill", "k_bill")

print("== 1) varsayılan plan + boş kullanım ==")
q = billing.quota(T)
check(q["plan"] == "free" and q["limit"] == 2000, f"free plan, limit 2000 (tadımlık) ({q})")
check(q["used"] == 0 and q["allowed"], "başlangıç kullanım 0, izinli")

print("== 2) metering (op maliyetleri) ==")
billing.meter(T, "memory_add", 10)       # +10
billing.meter(T, "memory_search", 5)     # +0.2*5 = 1
billing.meter(T, "correct", 2)           # +5*2 = 10
used = store.get_usage(T, billing.current_period())
check(abs(used - 21.0) < 1e-9, f"toplam 21 op (10+1+10) ({used})")
check(billing.OP_COST["llm_extract"] == 40, "llm_extract ağır metrelenir (40 op) — pahalı LLM yolu kendi maliyetini öder")
check(billing.OP_COST["memory_reinforce"] == 0.0, "reinforce ücretsiz (COGS~0, müşteri over-charge edilmez)")
check(billing.OP_COST["induce"] == 0.5, "induce kayıt-başı 0.5 kredi (auto-kategorizasyon LLM kaçağı kapalı)")
# write-axis: reinforce metering'i ücretsiz -> usage'a 0 ekler
_b = store.get_usage(T, billing.current_period())
billing.meter(T, "memory_reinforce", 100)
check(abs(store.get_usage(T, billing.current_period()) - _b) < 1e-9, "100 reinforce -> usage DEĞİŞMEDİ (0 kredi)")

print("== 3) plan değiştir ==")
store.set_plan(T, "growth")
check(billing.quota(T)["limit"] == 300000, "growth limiti 300K (piyasa-ankrajlı)")
check(billing.breakeven_kur("growth") >= 100, "growth breakeven_kur >= 100 (FX tamponu)")
# Solo (Kişisel) giriş paketi: bireysel kullanıcı için ucuz tier
store.set_plan(T, "solo")
qs = billing.quota(T)
check(qs["limit"] == 25000 and qs["price_try"] == 349, f"solo 25K yazma / 349 TL ({qs.get('price_try')})")
check(billing.storage_limit("solo") == 60000 and billing.breakeven_kur("solo") >= 100, "solo storage 60K + FX güvende")
store.set_plan(T, "free")

print("== 4) kota AŞIMI -> charge bloklar, metering yapmaz ==")
store.add_usage(T, billing.current_period(), 2000)  # 21+2000 = 2021 > 2000
before = store.get_usage(T, billing.current_period())
allowed, q4 = billing.charge(T, "memory_add", 1)
after = store.get_usage(T, billing.current_period())
check(not allowed and not q4["allowed"], "aşımda charge reddetti")
check(abs(before - after) < 1e-9, "reddedilen op metering YAPMADI")

print("== 4b) cost-aware ATOMİK reserve (atomik kota işlemi) ==")
store.upsert_tenant("t_q", "Q", "kq")
store.set_plan("t_q", "free")  # 2000
store.add_usage("t_q", billing.current_period(), 1999.8)  # kalan 0.2 op
check(not billing.reserve("t_q", "correct"), "kalan 0.2'ye cost-5 'correct' SIĞMAZ (over-op sızıntısı yok)")
check(not billing.reserve("t_q", "check"), "cost-1 'check' de sığmaz")
check(billing.reserve("t_q", "memory_search"), "cost-0.2 tam sığar")
check(not billing.reserve("t_q", "memory_search"), "kota dolu -> sonraki reddedildi")
check(abs(store.get_usage("t_q", billing.current_period()) - 2000.0) < 1e-9, "tam limitte durdu")

print("== 5) enterprise sınırsız ==")
store.set_plan(T, "enterprise")
qe = billing.quota(T)
check(qe["limit"] is None and qe["allowed"], "enterprise limitsiz, izinli")
ok, _ = billing.charge(T, "correct", 3)
check(ok, "enterprise'da charge geçti")

print("== 6) period formatı ==")
import re as _re
check(bool(_re.match(r"^\d{4}-\d{2}$", billing.current_period())), "period YYYY-MM")

print("== 7) storage (stok) kotası — op ekseninden AYRI ==")
store.upsert_tenant("t_s", "S", "ks")
store.set_plan("t_s", "free")     # mem tavanı 1000 (tadımlık)
sq = billing.storage_quota("t_s")
check(sq["limit"] == 1000 and sq["used"] == 0 and sq["allowed"], f"free storage tavanı 1000, boş ({sq})")
store.add_memory({"id": "s1", "tenant_id": "t_s", "content": "alfa"})
store.add_memory({"id": "s2", "tenant_id": "t_s", "content": "beta"})
check(billing.storage_quota("t_s")["used"] == 2, "2 aktif hatıra -> storage used=2")
check(billing.storage_ok("t_s"), "tavan altında -> add kabul")
store.set_plan("t_s", "enterprise")
check(billing.storage_limit("enterprise") is None and billing.storage_ok("t_s"), "enterprise storage sınırsız")
store.set_plan("t_s", "starter")
q7 = billing.quota("t_s")
check(q7["storage"]["limit"] == 225000 and q7["price_try"] == 990, f"quota storage fair-use 2.5x (225K) + fiyat ({q7.get('price_try')})")
# write-axis: storage tavanı yazma limitinin ~2.5-3x'i (ilan edilen "cömert/pratikte sınırsız")
check(billing.storage_limit("starter") >= billing.plan_limit("starter") * 2, "Indie storage >= yazma*2 (cömert fair-use)")
check(billing.storage_limit("pro") >= billing.plan_limit("pro") * 2, "Business storage >= yazma*2")
check(billing.breakeven_kur("starter") >= 100, "Indie breakeven_kur >= 100 (FX tamponu)")
check(billing.OVERAGE["storage_per_gb_month_try"] == 50.0, "storage overage 50 TL/GB (yumuşak caydırıcı)")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM BILLING TESTLERİ GEÇTİ")
