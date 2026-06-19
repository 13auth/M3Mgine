#!/usr/bin/env python3
"""run_ci.py — tek komutla TÜM doğrulama (offline paketler + canlı e2e orkestrasyonu).

Bu betik, README'deki manuel e2e adımlarını (init-tenant/plan/seed/link-subscription
+ server kaldır + e2e_smoke + teardown) KODA döker, böylece CI'da tek komutla koşar.

  python tests/run_ci.py

Davranış:
  - tests/test_*.py offline paketleri  -> ZORUNLU (biri patlarsa exit 1)
  - test_live_llm                       -> sadece LLM hattı varsa; yoksa SKIP
  - e2e_smoke                           -> server'ı kendi kaldırır, uçtan uca sürer
Harici servis gerekmez (SQLite + stdlib). Çıkış kodu: 0 = hepsi yeşil.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ENG = Path(__file__).resolve().parent.parent
TESTS = ENG / "tests"
PY = sys.executable
TMP = Path(tempfile.gettempdir())

# offline paketler ZORUNLU; live_llm anahtar varsa koşulur
LIVE = "test_live_llm.py"
OFFLINE = sorted(p.name for p in TESTS.glob("test_*.py") if p.name != LIVE)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(name: str, argv: list[str], env: dict, cwd: Path = ENG) -> bool:
    """alt-süreç koştur; çıktıyı akıt; True=geçti."""
    print(f"\n=== {name} ===", flush=True)
    r = subprocess.run(argv, cwd=str(cwd), env=env)
    ok = r.returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} (exit {r.returncode}) ---", flush=True)
    return ok


def main() -> int:
    base_env = dict(os.environ)
    results: list[tuple[str, str]] = []  # (name, PASS|FAIL|SKIP)

    # 1) OFFLINE paketler — her biri kendi temp DB'sini kuruyor (CCE_DB'yi ezmeyelim)
    off_env = dict(base_env)
    off_env.pop("CCE_DB", None)
    for t in OFFLINE:
        ok = _run(t, [PY, str(TESTS / t)], off_env)
        results.append((t, "PASS" if ok else "FAIL"))

    # 2) LIVE LLM — GERÇEK ping ile gate'le. (llm_available() loopback'te key yokken
    #    True döner ama gateway 401 verir; tek güvenilir sinyal: minik bir çağrı dene.)
    sys.path.insert(0, str(ENG))
    live_ok = False
    try:
        import llm  # noqa: E402
        if llm.llm_available():
            llm.call_model("pong yaz", "ping", max_tokens=5)
            live_ok = True
    except Exception as e:  # LLMError dahil -> skip
        print(f"[live] LLM kullanılamıyor ({type(e).__name__}); test_live_llm atlanacak")
    if live_ok:
        ok = _run(LIVE, [PY, str(TESTS / LIVE)], off_env)
        results.append((LIVE, "PASS" if ok else "FAIL"))
    else:
        print(f"\n=== {LIVE} ===\n--- {LIVE}: SKIP (LLM hattı yok: CCE_LLM_API_KEY/erişim) ---")
        results.append((LIVE, "SKIP"))

    # 3) E2E — orkestrasyon koda gömülü (server'ı kendimiz kaldırıyoruz)
    db = TMP / "cce_ci_e2e.db"
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        if p.exists():
            p.unlink()
    port = _free_port()
    secret = "ci_dev_secret_not_real"
    sub_ref = "sub_ci"
    e2e_env = dict(base_env)
    e2e_env.update({"CCE_DB": str(db), "CCE_WEBHOOK_SECRET": secret, "CCE_PORT": str(port),
                    "E2E_SUB": sub_ref})  # e2e_smoke webhook'u bu ref'le imzalar -> mapping eşleşir

    # 3a) tenant/plan/rules/subscription önceden kur (server kapalıyken)
    setup = [
        ["init-tenant", "--id", "t_ci", "--name", "CI", "--key", "cce_ci_owner"],
        ["plan", "--tenant", "t_ci", "--set", "growth"],
        ["seed", "--tenant", "t_ci", "--yaml", "policy_rules.yaml"],
        ["link-subscription", "--tenant", "t_ci", "--ref", sub_ref],
    ]
    setup_ok = True
    for argv in setup:
        r = subprocess.run([PY, str(ENG / "cli.py")] + argv, cwd=str(ENG), env=e2e_env,
                           stdout=subprocess.DEVNULL)
        if r.returncode != 0:
            print(f"[e2e] setup FAIL: {' '.join(argv)} (exit {r.returncode})")
            setup_ok = False
            break

    e2e_status = "FAIL"
    if setup_ok:
        # 3b) server'ı kaldır
        srv = subprocess.Popen([PY, str(ENG / "api.py")], cwd=str(ENG), env=e2e_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            base = f"http://127.0.0.1:{port}"
            up = False
            for _ in range(40):
                try:
                    urllib.request.urlopen(base + "/health", timeout=1)
                    up = True
                    break
                except Exception:
                    time.sleep(0.25)
            if not up:
                print("[e2e] server health zamanında gelmedi")
            else:
                e2e_env.update({"E2E_BASE": base, "E2E_OWNER": "cce_ci_owner", "E2E_SECRET": secret})
                ok = _run("e2e_smoke.py", [PY, str(TESTS / "e2e_smoke.py")], e2e_env)
                e2e_status = "PASS" if ok else "FAIL"
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                srv.kill()
    results.append(("e2e_smoke.py", e2e_status))

    # ÖZET
    print("\n" + "=" * 48)
    print("DOĞRULAMA ÖZETİ")
    print("=" * 48)
    width = max(len(n) for n, _ in results)
    for n, st in results:
        print(f"  {n.ljust(width)}  {st}")
    failed = [n for n, st in results if st == "FAIL"]
    passed = sum(1 for _, st in results if st == "PASS")
    skipped = sum(1 for _, st in results if st == "SKIP")
    print("-" * 48)
    print(f"  {passed} PASS · {len(failed)} FAIL · {skipped} SKIP")
    if failed:
        print(f"  BAŞARISIZ: {', '.join(failed)}")
        return 1
    print("  TÜM ZORUNLU DOĞRULAMA YEŞİL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
