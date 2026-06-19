"""test_taxonomy.py — induce duplicate-PK güvenliği + cold-start (LLM mock, key'siz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_taxonomy.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import routing  # noqa: E402
import store  # noqa: E402
import taxonomy  # noqa: E402

T = "t_tax"

# cold-start: MIN altı -> ok False (induce etme)
assert taxonomy.induce(T)["ok"] is False

for i in range(12):
    store.add_memory({"id": f"m{i}", "tenant_id": T, "user_id": "u", "content": f"icerik {i}", "embedding": None})

# DUPLICATE-PK TUZAĞI: iki label slug[:40]'ta AYNI ama label FARKLI.
# Eskiden node id slug[:40]'tan türetiliyordu -> aynı id -> replace_taxonomy IntegrityError -> TÜM induce çökerdi.
# Fix: id LABEL'dan türetilir + id-dedup. Bu test regresyonu yakalar.
long_a = "A" * 45 + "X"
long_b = "A" * 45 + "Y"
taxonomy.propose_taxonomy = lambda samples, want="6-10": [
    {"slug": long_a.lower(), "label": long_a},
    {"slug": long_b.lower(), "label": long_b},
    {"slug": "finans", "label": "Finans"},
]
routing.classify_batch = lambda txts, labs: ["Finans"] * len(txts)

res = taxonomy.induce(T)
assert res["ok"] is True, res                 # ÇÖKMEDİ
assert res["total"] == 12, res
labels = {n["label"] for n in store.list_taxonomy(T)}
assert "Finans" in labels                     # routing çalıştı
print("test_taxonomy.py: PASS")
