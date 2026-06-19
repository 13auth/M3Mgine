"""test_embeddings.py — semantik retrieval: cosine + graceful fallback + semantik path."""
import sys
from pathlib import Path

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import embeddings as emb  # noqa: E402
import policy_engine as pe  # noqa: E402

FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


print("== cosine matematiği ==")
check(abs(emb.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9, "identik vektör -> 1.0")
check(abs(emb.cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9, "ortogonal -> 0.0")
check(emb.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0, "boyut uyumsuz -> 0.0")
check(emb.cosine([], [1.0]) == 0.0, "boş -> 0.0")

print("== embed hattı yokken graceful fallback (None) ==")
import time as _t  # noqa: E402
emb._cache.clear()
emb._disabled_until = _t.time() + 1000  # cooldown'da
check(emb.embed("herhangi") is None, "embed kapalı -> None (çökme yok)")
r_lex = pe._relevance("acme binlerce kanal var", {"rule": "binlerce kanal yasak", "message": ""})
check(0.0 <= r_lex <= 1.0 and r_lex > 0, f"embed yokken lexical relevance çalıştı ({r_lex:.2f})")

print("== embed hattı varken SEMANTİK path (stub vektörler) ==")
emb._disabled_until = 0.0
emb._cache.clear()
_orig = emb.embed
emb.embed = lambda t: [1.0, 0.0] if "kanal" in t else [0.0, 1.0]
try:
    r_sem_hi = pe._relevance("binlerce kanal var", {"rule": "kanal yasak", "message": ""})
    r_sem_lo = pe._relevance("tamamen alakasiz metin", {"rule": "kanal yasak", "message": ""})
    check(abs(r_sem_hi - 1.0) < 1e-9, f"ilgili -> semantik cosine yüksek ({r_sem_hi:.2f})")
    check(abs(r_sem_lo) < 1e-9, f"alakasiz -> semantik cosine düşük ({r_sem_lo:.2f})")
finally:
    emb.embed = _orig

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM EMBEDDING TESTLERİ GEÇTİ")
