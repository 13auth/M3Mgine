"""test_obsidian_erasure.py — forget edilen içerik obsidian re-import'la DİRİLMEZ (KVKK Art.17)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_obs.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory  # noqa: E402
import obsidian  # noqa: E402
import store  # noqa: E402

T = "t_obs"
content = "Musteri Ayse Hanim numarasi 0532 145 67 89 aramak istemiyor"
note = lambda title: {"title": title, "content": content, "links": [], "folder": "", "props": {}, "tags": []}

# 1) import -> 1 kayıt (3.şahıs + telefon)
obsidian.import_notes(T, [note("NotA")])
mems = store.get_active_memories(T)
assert len(mems) == 1, mems
assert mems[0].get("subject_party") == "third_party", mems[0].get("subject_party")

# 2) forget (tombstone + crypto-shred)
memory.forget(T, mems[0]["id"])
assert len(store.get_active_memories(T)) == 0

# 3) AYNI içeriği FARKLI başlıkla re-import -> erasure guard bloklamalı (DİRİLMEMELİ)
obsidian.import_notes(T, [note("NotB-farkli-baslik")])
active = store.get_active_memories(T)
assert len(active) == 0, f"forget edilen 3.şahıs içeriği re-import'la dirildi: {active}"

print("test_obsidian_erasure.py: PASS")
