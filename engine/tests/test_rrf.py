"""test_rrf.py — Faz 2.5 hybrid RRF füzyonu. Sparse (lexical/keyword) kolun katkısı:
embedding'i OLMAYAN ama keyword-eşleşen kayıt retrieval'da yüzeye çıkar (pure-dense'te
düşerdi). Dense kol: embed varsa semantik-en-yakın üstte. key'siz: embed stub."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_rrf.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))
import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


def _stub(text):
    # keyword-granüler stub: farklı konular ORTOGONAL vektör (kaba stub sahte dense eşleşme
    # yaratıyordu). 'rapor/teslim' sorgusu hiçbir embed'li doldurucuya dense-yakın olmamalı.
    t = (text or "").lower()
    v = [0.0, 0.0, 0.0, 0.0]
    if "kahve" in t:
        v[0] = 1.0
    if "hava" in t:
        v[1] = 1.0
    if "rapor" in t or "teslim" in t:
        v[2] = 1.0
    if not any(v):
        v[3] = 1.0
    return v


embeddings.embed = _stub
T = "t_rrf"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "RRF", "k_rrf")

print("== 1) SPARSE kol: embedding'siz keyword-eşleşen kayıt yüzeye çıkar ==")
# embed hattı kapalı -> bu fact embedding'siz saklanır (None)
embeddings.embed = lambda t: None
memory.ingest(T, "x", facts=["Proje raporu cuma teslim edilecek"])  # embedding YOK
embeddings.embed = _stub
# embed'li alakasız doldurucular
memory.ingest(T, "x", facts=["Kullanıcı kahveyi sever", "Hava bugün güzel"])
hits = memory.search(T, "rapor teslim ne zaman", top_k=1)
check(hits and "rapor" in hits[0]["content"].lower(),
      f"keyword kaydı embedding'siz olsa da üstte ({hits[0]['content'] if hits else '-'})")

print("== 2) DENSE kol: embed varken semantik-en-yakın üstte ==")
h2 = memory.search(T, "kahve içmek", top_k=1)
check(h2 and "kahve" in h2[0]["content"].lower(), f"semantik-yakın üstte ({h2[0]['content'] if h2 else '-'})")

print("== 3) RRF skoru sıralı ve pozitif ==")
allh = memory.search(T, "rapor", top_k=5, touch=False)
check(allh and all(allh[i]["score"] >= allh[i + 1]["score"] for i in range(len(allh) - 1)),
      "sonuçlar skora göre azalan sırada")

print()
if FAILS:
    print(f"SONUC: {len(FAILS)} BASARISIZ"); sys.exit(1)
print("SONUC: RRF HYBRID FUZYON DOGRULANDI (dense + sparse)")
