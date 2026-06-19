"""test_admin.py — kurumsal sertleştirme: admin log, retention prune, rate limit (key'siz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_admin.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import store  # noqa: E402
import api  # noqa: E402  (rate limiter)

TENANT = "t_adm"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(TENANT, "Adm", "k_adm")
store.add_rule({"id": "r1", "tenant_id": TENANT, "project": None, "type": "hard",
                "severity": "high", "source": "t", "message": "m", "rule": "",
                "forbid_patterns": ["yasak"], "require_patterns": []})

print("== admin log: sil + purge, purge SONRASI bile yaşar ==")
store.delete_rule(TENANT, "r1")
store.purge_tenant(TENANT)
log = store.get_admin_log(TENANT)
actions = {r["action"] for r in log}
check("delete_rule" in actions and "purge_tenant" in actions,
      f"admin log purge sonrası korunuyor: {actions}")
check(store.get_rules(TENANT) == [], "purge sonrası kural yok")

print("== retention prune ==")
store.record_check(TENANT, "rX", True, "runtime")
store.record_check(TENANT, "rX", False, "runtime")
pruned = store.prune_telemetry(days=-1)  # cutoff gelecekte -> hepsini sil
check(pruned["checks"] >= 2, f"prune eski checks'i sildi ({pruned})")
check(store.compliance_by_rule(TENANT) == [], "prune sonrası ölçüm boş")

print("== per-tenant rate limit ==")
api.RATE_RPM = 3
api._RATE.clear()
oks = [api._rate_ok("z") for _ in range(5)]
check(oks[:3] == [True, True, True] and oks[3] is False,
      f"3 istek geçti, 4. bloklandı: {oks}")
check(api._rate_ok("other") is True, "farklı tenant ayrı kovada (izole)")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM ADMIN/RETENTION/RATE TESTLERİ GEÇTİ")
