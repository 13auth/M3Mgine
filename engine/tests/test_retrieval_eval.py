"""test_retrieval_eval.py — retrieval kalite regresyon kapısı (sparse-mod recall@k).
BM25 + TR prefix-stem çekim-eki eşleşmesini kilitler; recall@5 zemininin altına düşerse CI kırılır."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_reteval.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

embeddings.embed = lambda *a, **k: None   # SPARSE-mod: lexical/BM25 sinyalini ölç (key gerekmez)

T = "t_reteval"
MEMS = [
    ("m1", "Aylık raporlar ayın son iş günü hazırlanır"),
    ("m2", "Postgres veritabanı yedekleri her gün otomatik alınıyor"),
    ("m3", "Kullanıcı arayüzde koyu tema tercih ediyor"),
    ("m4", "Ödeme altyapısı olarak iyzico kullanılıyor"),
    ("m5", "Haftalık retro toplantısı ekip moralini ölçüyor"),
    ("m6", "Veritabanı göçü için versiyonlu migration kullanılıyor"),
]
for mid, c in MEMS:
    store.add_memory({"id": mid, "tenant_id": T, "user_id": "default", "content": c, "embedding": None})

# çekim-eki (rapor/raporlar, tema/teması) dahil — BM25 prefix-stem bunları yakalamalı
GOLD = [
    ("rapor ne zaman hazırlanıyor", "m1"),
    ("arayüz teması", "m3"),
    ("yedekleme sıklığı", "m2"),
    ("hangi ödeme sağlayıcısı", "m4"),
    ("migration nasıl yapılıyor", "m6"),
]
hit5 = 0
for q, gold in GOLD:
    ids = [r["id"] for r in memory.search(T, q, top_k=5, touch=False)]
    if gold in ids[:5]:
        hit5 += 1
r5 = hit5 / len(GOLD)
assert r5 >= 0.8, f"retrieval recall@5 regresyonu: {r5:.0%} (<80%)"
# çekim-eki vakası özellikle tutmalı (Jaccard kaçırıyordu)
assert "m1" in [r["id"] for r in memory.search(T, "rapor ne zaman hazırlanıyor", top_k=5, touch=False)], "çekim-eki eşleşmesi bozuldu"
print(f"test_retrieval_eval.py: PASS (recall@5={r5:.0%})")
