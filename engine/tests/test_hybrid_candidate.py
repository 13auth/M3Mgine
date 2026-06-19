"""test_hybrid_candidate.py — iki-yönlü hibrit aday (blind lexical index ∪ dense ANN).

content ŞİFRELİ olduğundan lexical aday DB'de düz-metinle değil keyed-hash term kökleriyle (GIN/pg,
O(N)/sqlite oracle) üretilir. Büyük-N'de dense top-k'nın kaçırdığı lexical-only kayıt recall'a girer.
Dual-backend: aynı davranış HEM sqlite HEM postgres.
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_hybrid.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

if store.BACKEND == "store_pg":
    store.reset_all()


def add(tenant, mid, content, emb=None, party=None):
    store.add_memory({"id": mid, "tenant_id": tenant, "user_id": "default",
                      "content": content, "embedding": emb, "subject_party": party})


# ---- 1) candidate_memories_lexical: terim eşleşeni getirir, ilgisizi getirmez ----
T = "t_hyb"
store.upsert_tenant(T, "HYB", "k_hyb")
add(T, "a", "domates fiyati artti")                  # ortak: domates(1)
add(T, "b", "kullanici arayuzu tasarim toplantisi")  # ilgisiz
add(T, "c", "domates ve biber tedarik plani")        # ortak: domates+tedarik(2)
qh = store._lex_index("domates tedarik")
got = {m["id"] for m in store.candidate_memories_lexical(T, "default", qh, 10)}
assert got == {"a", "c"}, f"lexical eşleşme yanlis: {got}"

# overlap-rank: c(2 ortak) a'dan(1) önce
ranked = [m["id"] for m in store.candidate_memories_lexical(T, "default", qh, 10)]
assert ranked[0] == "c", f"overlap-rank yanlis: {ranked}"

# ---- 2) çekim-eki (4-prefix) süperset: 'rapor' sorgusu 'raporlar' dökümanini surface eder ----
add(T, "r", "aylik raporlar hazirlandi")
got2 = {m["id"] for m in store.candidate_memories_lexical(T, "default", store._lex_index("rapor"), 10)}
assert "r" in got2, f"prefix-stem eşleşme kacti: {got2}"

# ---- 3) exclude_third: 3.şahıs PII kaydı self-context lexical aday'a SIZMAZ (KVKK) ----
add(T, "t", "musteri telefonu kaydi", party="third_party")
qh3 = store._lex_index("musteri telefonu")
incl = {m["id"] for m in store.candidate_memories_lexical(T, "default", qh3, 10, exclude_third=False)}
excl = {m["id"] for m in store.candidate_memories_lexical(T, "default", qh3, 10, exclude_third=True)}
assert "t" in incl and "t" not in excl, f"exclude_third bozuk: incl={incl} excl={excl}"

# ---- 4) SEARCH entegrasyonu: dense top-k'nın kaçırdığı lexical-only kayıt recall'a girer ----
# embed hattını sabitle + aday tavanını 1'e indir -> dense yalniz EN YAKIN'i alir; lexical kurtarir.
QV = [1.0, 0.0, 0.0]
embeddings.embed = lambda *a, **k: QV          # sorgu (ve write) vektörü sabit-yön
memory.CANDIDATE_K = 1
T2 = "t_hyb2"
store.upsert_tenant(T2, "HYB2", "k_hyb2")
add(T2, "near", "alfa beta gama", emb=[1.0, 0.0, 0.0])   # embed YAKIN, terim ilgisiz
add(T2, "lex", "zeta hedefkelime omega", emb=[0.0, 0.0, 1.0])  # embed UZAK, terim eşleşir
res_ids = {r["id"] for r in memory.search(T2, "hedefkelime", top_k=5, touch=False)}
assert "lex" in res_ids, f"lexical-only kayit dense-miss'te kurtarilamadi: {res_ids}"
# kontrol: dense tek-aday yolu (lexical olmadan) 'lex'i ALMAZDI -> union'in katkisi kanitli
dense_only = {m["id"] for m in store.candidate_memories(T2, "default", QV, 1)}
assert dense_only == {"near"}, f"dense tek-aday beklenmedik: {dense_only}"

# ---- 5) backfill: eski (term_hashes NULL) kayitlar geriye doldurulur ----
add(T, "old", "envanter sayim raporu hazir")
ph = "%s" if store.BACKEND == "store_pg" else "?"
with store._conn() as c:   # eski-kayit simulasyonu: term_hashes -> NULL
    c.execute(f"UPDATE memories SET term_hashes=NULL WHERE tenant_id={ph} AND id='old'", (T,))
pre = {m["id"] for m in store.candidate_memories_lexical(T, "default", store._lex_index("envanter"), 10)}
assert "old" not in pre, f"NULL term_hashes lexical'de gorunmemeli: {pre}"
filled = store.backfill_lex_index(T)
assert filled >= 1, f"backfill kayit doldurmadi: {filled}"
post = {m["id"] for m in store.candidate_memories_lexical(T, "default", store._lex_index("envanter"), 10)}
assert "old" in post, f"backfill sonrasi bulunmali: {post}"

print("test_hybrid_candidate.py: PASS")
