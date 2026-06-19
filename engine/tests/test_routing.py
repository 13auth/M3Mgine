"""test_routing.py — classify_llm/classify_batch longest-match + DIGER (LLM mock, key'siz)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import routing  # noqa: E402

cats = ["Sağlık", "Sağlık Sigortası", "Finans"]

# prefix-label tuzağı: LLM doğru 'Sağlık Sigortası' döner -> EN UZUN eşleşme seçilmeli, 'Sağlık' değil
routing.call_model = lambda s, u, **k: "Sağlık Sigortası"
assert routing.classify_llm("x", cats)["label"] == "Sağlık Sigortası"

routing.call_model = lambda s, u, **k: "Finans"
assert routing.classify_llm("x", cats)["label"] == "Finans"

routing.call_model = lambda s, u, **k: "alakasiz bir sey"
assert routing.classify_llm("x", cats)["label"] == "DIGER"

# batch: sıra korunur + longest-match + bilinmeyen -> DIGER
routing.call_model = lambda s, u, **k: json.dumps([{"n": 1, "k": "Sağlık Sigortası"}, {"n": 2, "k": "yok"}])
r = routing.classify_batch(["a", "b"], cats)
assert r == ["Sağlık Sigortası", "DIGER"], r

# parse hatası -> hepsi DIGER (çökmez)
routing.call_model = lambda s, u, **k: "JSON degil"
assert routing.classify_batch(["a", "b"], cats) == ["DIGER", "DIGER"]

print("test_routing.py: PASS")
