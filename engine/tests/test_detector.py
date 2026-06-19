"""test_detector.py — stage 1 otomatik tespit (heuristik key'siz; classify stub)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_detector.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import compiler  # noqa: E402
import detector  # noqa: E402
import store  # noqa: E402

TENANT = "t_det"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# classify'ı stub'la (key gerekmesin)
def stub_classify(raw):
    return {"category": "preference", "project": None,
            "what_was_wrong": "cevap çok uzundu", "correct_behavior": "kısa yaz",
            "reusable_rule": "Yanıtları varsayılan kısa ve aksiyon-odaklı tut",
            "severity": "medium", "tags": ["style"], "forbid_terms": []}


compiler.classify = stub_classify
store.upsert_tenant(TENANT, "Detector Test", "k_det")

print("== 1) heuristik ön-eleme (sıfır token) ==")
s1, sig1 = detector.heuristic_score("Hayır, çok uzun yazdın, kısa olmalı")
s2, sig2 = detector.heuristic_score("Teşekkürler, harika oldu")
s3, sig3 = detector.heuristic_score("bunu yapma, bir daha asla film posteri kullanma")
check(s1 >= 0.5, f"düzeltme mesajı tetikledi (skor={s1}, {sig1})")
check(s2 == 0, f"normal mesaj tetiklemedi (skor={s2})")
check(s3 >= 0.5 and "directive" in sig3, f"yönerge yakalandı ({sig3})")

print("== 2) scan_turn -> compile -> store (key'siz, stub classify) ==")
r = detector.scan_turn("Hayır, çok uzun yazdın; kısa ve net olmalı.",
                       prev_assistant="(çok uzun bir açıklama...)", tenant_id=TENANT, use_llm=False)
check(r["is_correction"], "tur düzeltme olarak işaretlendi")
check(r["ingested"], "düzeltme -> kural store'a yazıldı")
check(len(store.get_rules(TENANT)) == 1, "store'da 1 kural")

print("== 3) normal tur compile etmez ==")
r2 = detector.scan_turn("Süper, aynen böyle devam et.", prev_assistant="...", tenant_id=TENANT, use_llm=False)
check(not r2["is_correction"], "normal tur düzeltme değil")
check(len(store.get_rules(TENANT)) == 1, "store hâlâ 1 kural (yeni eklenmedi)")

print("== 4) scan_transcript (batch / gece cron modu) ==")
convo = [
    {"role": "user", "content": "Acme store metni yaz"},
    {"role": "assistant", "content": "Binlerce kanal ve filmle..."},
    {"role": "user", "content": "Hayır, öyle değil, içerik vaadi verme; kaldır."},
    {"role": "assistant", "content": "Düzelttim."},
    {"role": "user", "content": "tamam teşekkürler"},
]
rep = detector.scan_transcript(convo, TENANT, use_llm=False)
check(rep["corrections_found"] == 1, f"transcript'te 1 düzeltme bulundu ({rep['corrections_found']})")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM TESTLER GEÇTİ")
