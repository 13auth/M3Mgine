"""test_obsidian_dedup.py — obsidian aynı-içerik çoğalma + aynı-başlık-farklı-klasör ezme + has_pending_job."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_obsdedup.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import obsidian  # noqa: E402
import store  # noqa: E402

T = "t_od"


def note(title, folder, content):
    return {"title": title, "folder": folder, "content": content, "links": [], "props": {}, "tags": []}


# 1) AYNI içerik, farklı başlık/klasör -> TEK memory (çoğalma önlenir)
obsidian.import_notes(T, [note("A", "f1", "Tedarikci domates fiyati artti"),
                          note("B", "f2", "Tedarikci domates fiyati artti")])
assert store.count_memories(T) == 1, f"icerik-dedup bozuk: {store.count_memories(T)}"

# 2) AYNI başlık, FARKLI klasör, FARKLI içerik -> İKİ memory (eskiden id title'dan -> ezme/veri kaybı)
store.purge_tenant(T)
obsidian.import_notes(T, [note("Index", "01 Daily", "gunluk notlar"),
                          note("Index", "02 People", "kisi notlari")])
assert store.count_memories(T) == 2, f"ayni-baslik-farkli-klasor ezme: {store.count_memories(T)}"

# 3) has_pending_job (induce dedup-enqueue)
store.purge_tenant(T)
assert store.has_pending_job(T, "induce") is False
store.enqueue_job(T, "induce", {})
assert store.has_pending_job(T, "induce") is True

print("test_obsidian_dedup.py: PASS")
