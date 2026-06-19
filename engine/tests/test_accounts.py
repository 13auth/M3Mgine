"""test_accounts.py — çoklu API-key: üret/çöz/iptal/rol/izolasyon (key'siz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_accounts.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import accounts  # noqa: E402
import store  # noqa: E402

T = "t_acc"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "Acc", "boot_key")

print("== 1) bootstrap key (owner) çözülür ==")
check(store.tenant_by_key("boot_key") == T, "bootstrap key -> tenant")
check(store.role_by_key("boot_key") == "owner", "bootstrap rol = owner")

print("== 2) yeni key üret -> çözülür, raw bir kez ==")
k = accounts.create_key(T, name="ci", role="member")
raw = k["api_key"]
check(raw.startswith("cce_") and len(raw) > 20, "raw key üretildi")
check(store.tenant_by_key(raw) == T, "yeni key -> tenant")
check(store.role_by_key(raw) == "member", "yeni key rol = member")

print("== 3) izolasyon + geçersiz key ==")
check(store.tenant_by_key("bogus-key") is None, "geçersiz key -> None")
check(store.tenant_by_key(raw) == T and store.tenant_by_key(raw) != "t_other", "key sadece kendi tenant'ı")

print("== 4) list ==")
ks = accounts.list_keys(T)
check(len(ks) == 1 and ks[0]["role"] == "member" and not ks[0]["revoked"], f"1 aktif key ({ks})")

print("== 5) revoke -> artık çözülmez ==")
n = store.revoke_api_key(T, k["id"])
check(n == 1, "revoke 1 satır")
check(store.tenant_by_key(raw) is None, "iptal edilen key çözülmüyor")
check(accounts.list_keys(T)[0]["revoked"], "list'te revoked görünür")

print("== 6) bootstrap key revoke sonrası da çalışır ==")
check(store.tenant_by_key("boot_key") == T, "bootstrap key etkilenmedi")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM ACCOUNTS TESTLERİ GEÇTİ")
