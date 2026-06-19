"""test_compliance.py — per-rule sağlık/metering (offline, token-siz, embed-siz).
rule_health: bayat (N gün atışsız) + hiç-tetiklenmemiş kuralları yüzeye çıkarıyor mu,
compliance oranı doğru mu, sıralama (bayat+severity önce) ve record_enforcement yazma yolu."""
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_compliance.db"
for _s in ("", "-wal", "-shm"):
    _p = Path(str(_tmp) + _s)
    if _p.exists():
        _p.unlink()
os.environ["CCE_DB"] = str(_tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compiler  # noqa: E402
import store  # noqa: E402

T = "t_comp"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _mkrule(rid, sev):
    # gerçek üretim yolu (compile_to_rule -> add_rule) ile uyumlu, doğru serileştirme
    r = compiler.compile_to_rule(
        {"reusable_rule": f"kural {rid}", "severity": sev, "forbid_terms": [],
         "what_was_wrong": "x"}, T)
    r["id"] = rid
    return r


store.upsert_tenant(T, "Compliance Test", "k_comp")
for rid, sev in [("r_healthy", "high"), ("r_stale", "critical"), ("r_never", "medium")]:
    store.add_rule(_mkrule(rid, sev))

now = time.time()
# r_healthy: 4 kontrol, 3 geçti (compliance %75), bugün
for p in (True, True, True, False):
    store.record_check(T, "r_healthy", p, "test")
# r_stale: 10 gün önce tek kontrol (bayat eşiği 7)
store.record_check(T, "r_stale", True, "test", at=now - 10 * 86400)
# r_never: hiç kontrol yok (configured ama trafik dokunmamış)

H = {h["rule_id"]: h for h in store.rule_health(T, stale_days=7)}
check(len(H) == 3, f"3 aktif kural raporlandı ({len(H)})")

hv = H.get("r_healthy", {})
check(hv.get("checks") == 4 and hv.get("passed") == 3, f"r_healthy 3/4 ({hv.get('passed')}/{hv.get('checks')})")
check(abs((hv.get("compliance") or 0) - 0.75) < 1e-6, f"r_healthy compliance %75 ({hv.get('compliance')})")
check(hv.get("stale") is False and hv.get("never_fired") is False, "r_healthy bayat değil")

st = H.get("r_stale", {})
check(st.get("stale") is True and (st.get("idle_days") or 0) >= 9, f"r_stale bayat, idle≈{st.get('idle_days')}")
check(st.get("checks") == 1, f"r_stale 1 kontrol ({st.get('checks')})")

nv = H.get("r_never", {})
check(nv.get("never_fired") is True and nv.get("stale") is True, "r_never hiç tetiklenmedi + bayat")
check(nv.get("checks") == 0 and nv.get("compliance") is None, "r_never 0 kontrol, compliance None")

# sıralama: bayat kurallar önce (severity'ye göre); r_stale(critical) ilk, r_healthy(sağlıklı) son
order = [h["rule_id"] for h in store.rule_health(T, stale_days=7)]
check(order[0] == "r_stale", f"bayat+critical ilk sırada ({order})")
check(order[-1] == "r_healthy", f"sağlıklı kural en sonda ({order})")

# record_enforcement (CLI/api kalıcılık yolu) checks'e yazıyor + aggregation görüyor
store.record_enforcement(T, [{"rule_id": "r_healthy", "passed": True}], [], source="cli")
cbr = {c["rule_id"]: c for c in store.compliance_by_rule(T)}
check(cbr["r_healthy"]["checks"] == 5, f"record_enforcement check ekledi (5 bekleniyor: {cbr['r_healthy']['checks']})")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ")
    sys.exit(1)
print("SONUÇ: COMPLIANCE METERING DOĞRULANDI")
