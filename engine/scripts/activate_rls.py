#!/usr/bin/env python3
"""activate_rls.py — RLS (DB-katmanı tenant izolasyonu) PROD aktivasyonu (idempotent, tek-seferlik).

NE YAPAR:
  1. OWNER DSN (store_pg._DSN / CCE_DB) ile bağlanıp store.apply_rls('cce_app') çağırır
     -> non-owner 'cce_app' rolü + tüm DATA tablolarında fail-closed RLS policy kurar.
  2. Owner DSN'den non-owner APP DSN'i türetir (user=cce_app, password=<üretilen>).
  3. APP DSN'i STDOUT'a basar -> operatör Render'da CCE_RLS_DSN env'ine yapıştırır.

GÜVENLİK: app parolası STDOUT'a basılır (operatörün kopyalaması için). CI/loglu ortamda ÇALIŞTIRMA.
Prod DB'de rol+policy oluşturur (idempotent: tekrar güvenli). Yalnızca postgres backend.

KULLANIM (operatör, owner DSN env'de):
  CCE_STORE_BACKEND=postgres CCE_DB="<owner_dsn>" python engine/scripts/activate_rls.py

SONRA: dönen CCE_RLS_DSN'i Render'a ekle -> API + worker RESTART ->
       doğrula: CCE_STORE_BACKEND=postgres CCE_DB="<owner_dsn>" python engine/tests/test_rls.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402

if store.BACKEND != "store_pg":
    print("activate_rls: HATA — postgres backend gerekir (CCE_STORE_BACKEND=postgres).")
    raise SystemExit(1)

import store_pg  # noqa: E402


def _app_dsn(owner_dsn: str, pw: str, role: str = "cce_app") -> str | None:
    """Owner DSN (keyword formatı: 'host=.. dbname=.. user=.. password=..') -> non-owner APP DSN.
    URL formatındaysa (postgres://...) None döner; operatör elle türetir."""
    if "=" not in owner_dsn or owner_dsn.strip().startswith("postgres"):
        return None
    try:
        parts = dict(kv.split("=", 1) for kv in owner_dsn.split())
    except ValueError:
        return None
    parts["user"] = role
    parts["password"] = pw
    return " ".join(f"{k}={v}" for k, v in parts.items())


def main() -> int:
    owner = store_pg._DSN
    pw = store.apply_rls(app_role="cce_app")   # idempotent: rol + grant + policy + RLS enable
    print("activate_rls: OK — 'cce_app' rolü + RLS policy uygulandı (idempotent).")
    app_dsn = _app_dsn(owner, pw)
    print()
    if app_dsn:
        print("--- Render env'e ekle (TEK SATIR, GİZLİ) ---")
        print("CCE_RLS_DSN=" + app_dsn)
        print("--- /son ---")
    else:
        print("Owner DSN URL formatında; APP DSN'i elle kur:")
        print("  user=cce_app  password=" + pw)
        print("  (host/port/dbname owner ile aynı) -> CCE_RLS_DSN olarak Render'a ekle.")
    print()
    print("Sonraki: Render'a CCE_RLS_DSN ekle -> API + worker RESTART -> doğrula:")
    print("  python engine/tests/test_rls.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
