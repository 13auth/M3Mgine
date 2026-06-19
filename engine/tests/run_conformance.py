#!/usr/bin/env python3
"""run_conformance.py — Faz 1 conformance gate: AYNI offline test paketlerini HEM sqlite
HEM postgres backend'inde koşar. İki backend de geçmeli (davranış paritesi kanıtı).

Postgres: her test dosyasından ÖNCE store_pg.reset_all() (truncate) ile izolasyon.
Çıkış kodu 0 = her iki backend tüm offline paketlerde yeşil.

  python tests/run_conformance.py
Postgres DSN: CCE_DATABASE_URL (yoksa local cce-pg container).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENG = Path(__file__).resolve().parent.parent
TESTS = ENG / "tests"
PY = sys.executable
sys.path.insert(0, str(ENG))

OFFLINE = sorted(p.name for p in TESTS.glob("test_*.py") if p.name != "test_live_llm.py")


def _run_backend(backend: str) -> list[tuple[str, str]]:
    env = dict(os.environ)
    env["CCE_STORE_BACKEND"] = backend
    results: list[tuple[str, str]] = []
    for t in OFFLINE:
        if backend == "postgres":
            try:
                import importlib
                import store_pg
                importlib.reload(store_pg)  # taze DSN/schema durumu
                store_pg.reset_all()
            except Exception as e:
                results.append((t, f"RESET-ERR:{type(e).__name__}"))
                continue
        r = subprocess.run([PY, str(TESTS / t)], cwd=str(ENG), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        ok = r.returncode == 0
        if ok:
            results.append((t, "PASS"))
        else:
            tail = (r.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1][:160]
            results.append((t, f"FAIL: {tail}"))
    return results


def main() -> int:
    print("=" * 60)
    matrix = {}
    for backend in ("sqlite", "postgres"):
        print(f"\n###### BACKEND: {backend} ######")
        res = _run_backend(backend)
        matrix[backend] = res
        for name, st in res:
            mark = "PASS" if st == "PASS" else st
            print(f"  [{backend:8}] {name:22} {mark}")

    print("\n" + "=" * 60)
    print("CONFORMANCE ÖZETİ")
    width = max(len(n) for n, _ in matrix["sqlite"])
    all_ok = True
    for i, (name, _) in enumerate(matrix["sqlite"]):
        s = matrix["sqlite"][i][1]
        p = matrix["postgres"][i][1]
        s_ok = s == "PASS"
        p_ok = p == "PASS"
        all_ok = all_ok and s_ok and p_ok
        print(f"  {name.ljust(width)}  sqlite={'OK' if s_ok else 'X'}  postgres={'OK' if p_ok else 'X'}"
              + ("" if p_ok else f"   <- {p}"))
    print("-" * 60)
    print("SONUC: " + ("HER IKI BACKEND YESIL" if all_ok else "FARK VAR - postgres portu eksik"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
