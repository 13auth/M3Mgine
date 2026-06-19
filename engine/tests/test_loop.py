"""test_loop.py — uçtan uca döngü testi, STUB sınıflandırma (key gerekmez).
correction -> compile -> store -> enforce -> evaluate. Hepsi gerçek kod yolu.
"""
import os
import sys
import tempfile
from pathlib import Path

# Temp DB — gerçek veriye dokunma. store import'undan ÖNCE set edilmeli.
_tmp = Path(tempfile.gettempdir()) / "cce_test.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import compiler  # noqa: E402
import store  # noqa: E402
from policy_engine import enforce  # noqa: E402
from evaluator import run_eval, format_report  # noqa: E402

TENANT = "t_demo"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


print("== 1) tenant + döngü kapanışı (correction -> rule) ==")
store.upsert_tenant(TENANT, "Demo Studio", "key_demo_123")

classification = {
    "category": "project", "project": "Acme",
    "what_was_wrong": "App store görselinde telifli film posteri kullandı",
    "correct_behavior": "Markasız placeholder kullanmalı",
    "reusable_rule": "IPTV/medya görsellerinde dahil içerik ima etme; markasız placeholder kullan",
    "severity": "high", "tags": ["acme", "copyright"],
    "forbid_terms": ["film posteri", "beIN Sports"],
}
out = compiler.ingest("Acme store screenshot'ına film posteri koydu, kaldır.",
                      TENANT, classification=classification)
check(out["rule"]["_written"], "correction compiled -> rule yazıldı")
check(out["rule"]["forbid_patterns"] and out["rule"]["type"] == "soft",
      "forbid_terms -> soft kural + forbid_patterns accelerator (hibrit: judge genelleştirir)")

rules = store.get_rules(TENANT, project="Acme")
check(len(rules) == 1, f"store'da 1 kural (bulundu: {len(rules)})")
check(store.tenant_by_key("key_demo_123") == TENANT, "api_key -> tenant çözümleme")

print("== 2) ENFORCE (stage 5) ==")
bad = "Yeni Acme: film posteri galerisi ve beIN Sports kanalları!"
good = "Acme bir oynatıcı; kendi M3U/Xtream kaynağını ekleyip izlersin."
rb = enforce(bad, rules=rules, project="Acme", run_soft=False)
rg = enforce(good, rules=rules, project="Acme", run_soft=False)
check(not rb.passed and len(rb.violations) >= 2, f"kötü çıktıda ihlal yakalandı ({len(rb.violations)})")
check(rg.passed, "temiz çıktı geçti")

print("== 3) tenant izolasyonu ==")
other = store.get_rules("t_other", project="Acme")
check(len(other) == 0, "başka tenant kuralları görmüyor (izolasyon)")

print("== 4) EVALUATE (stage 6, held-out ayrımı) ==")
cases = [
    {"name": "fc1", "bucket": "from-corrections",
     "input": "Acme store metni yaz", "output": "Acme oynatıcısına kendi M3U kaynağını ekle."},
    {"name": "ho1", "bucket": "held-out",
     "input": "Acme sürüm notu yaz", "output": "Yeni sürümde film posteri galerisi var!"},
]
rep = run_eval(TENANT, cases, project="Acme", run_soft=False)
print(format_report(rep))
fc = rep["buckets"]["from-corrections"]["compliance"]
ho = rep["buckets"]["held-out"]["compliance"]
check(fc == 1.0, "from-corrections uyum %100 (öğrenilen kural)")
check(ho == 0.0, "held-out uyum %0 (genellemedi -> yakalandı)")
check(any("Held-out" in n or "Overfit" in n for n in rep["notes"]), "overfit/held-out yorumu üretildi")
check(rep["per_rule"], "kural-bazlı ihlal sayacı dolu")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ")
    sys.exit(1)
print("SONUÇ: TÜM TESTLER GEÇTİ")
