"""test_pgvector_ann.py — PUSH-DOWN doğrulaması: store.candidate_* (büyük-N, ANN yolu)
gerçekten en-yakın-k'yi döndürüyor mu? Bağımsız Python cosine top-k ile karşılaştırır.
Postgres'te pgvector ORDER BY <=> sıralamasını, SQLite'ta Python sort'u kanıtlar (her iki
backend de doğru olmalı). conformance küçük-N (fallback) yolunu sürer; bu test ANN yolunu."""
import math
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_ann.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))
import store  # noqa: E402

DIM = 8
N = 40       # > k -> ANN/push-down yolu (küçük-N fallback değil)
K = 6
T = "t_ann"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


def vec(i):
    return [math.sin(i * 0.7 + j) + 0.01 * j for j in range(DIM)]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return (dot / (na * nb)) if (na and nb) else 0.0


def topk_ids(qv, items):
    return {i for i, _ in sorted(items, key=lambda kv: -cosine(qv, kv[1]))[:K]}


print(f"== backend = {store.BACKEND}, N={N}, K={K} ==")
store.upsert_tenant(T, "ANN", "k_ann")

# --- memories ---
mem_emb = {}
for i in range(N):
    e = vec(i)
    mem_emb[f"m{i}"] = e
    store.add_memory({"id": f"m{i}", "tenant_id": T, "content": f"c{i}", "embedding": e})
q = vec(3)
got = {m["id"] for m in store.candidate_memories(T, "default", q, K)}
exp = topk_ids(q, list(mem_emb.items()))
check(len(got) == K, f"candidate_memories K aday döndü ({len(got)})")
check(got == exp, f"candidate_memories ANN top-{K} = Python cosine top-{K} (got={sorted(got)} exp={sorted(exp)})")
check("m3" in got, "tam eşleşen (m3) en yakınlarda")

# --- entities ---
ent_emb = {}
for i in range(N):
    e = vec(i + 100)
    ent_emb[f"e{i}"] = e
    store.add_entity({"id": f"e{i}", "tenant_id": T, "name": f"E{i}", "norm": f"e{i}", "embedding": e})
qe = vec(105)
gote = {x["id"] for x in store.candidate_entities(T, qe, K)}
expe = topk_ids(qe, list(ent_emb.items()))
check(gote == expe, f"candidate_entities ANN top-{K} = Python top-{K}")

# --- edges (current) ---
edge_emb = {}
for i in range(N):
    e = vec(i + 200)
    edge_emb[f"g{i}"] = e
    store.add_edge({"id": f"g{i}", "tenant_id": T, "subject_id": "s", "predicate": "p",
                    "object_value": f"o{i}", "fact": f"f{i}", "embedding": e, "valid_at": 1000.0})
qg = vec(207)
gotg = {x["id"] for x in store.candidate_edges(T, qg, K)}
expg = topk_ids(qg, list(edge_emb.items()))
check(gotg == expg, f"candidate_edges ANN top-{K} = Python top-{K}")

print("== 4) audit-fix: NULL-embedding satirlar candidate'ta DAHIL (oracle paritesi, total>k) ==")
store.upsert_tenant("t_ann2", "A2", "k2")
for i in range(3):
    store.add_memory({"id": f"e{i}", "tenant_id": "t_ann2", "content": f"e{i}", "embedding": vec(i)})
for i in range(5):
    store.add_memory({"id": f"n{i}", "tenant_id": "t_ann2", "content": f"n{i}", "embedding": None})
r4 = store.candidate_memories("t_ann2", "default", vec(1), 6)
ids4 = {m["id"] for m in r4}
check(len(r4) == 6, f"k=6 aday dondu ({len(r4)})")
check(all(f"e{i}" in ids4 for i in range(3)), "tum embedded satirlar dahil")
check(any(x.startswith("n") for x in ids4), "NULL-embedding satirlar DA dahil (erisilebilir kaldi)")

print("== 5) audit-fix: boyut-uyusmaz satir candidate'i COKERTMEZ (oracle paritesi) ==")
store.upsert_tenant("t_ann3", "A3", "k3")
for i in range(6):
    store.add_memory({"id": f"d{i}", "tenant_id": "t_ann3", "content": f"d{i}", "embedding": vec(i)})
store.add_memory({"id": "odd", "tenant_id": "t_ann3", "content": "odd", "embedding": [1.0, 2.0, 3.0]})
try:
    r5 = store.candidate_memories("t_ann3", "default", vec(2), 4)
    check(len(r5) == 4 and "d2" in {m["id"] for m in r5}, f"dim-mismatch'te cokme yok ({[m['id'] for m in r5]})")
except Exception as e:
    check(False, f"dim-mismatch COKERTTI: {e}")

print("== 6) audit-fix: NaN embedding write'ta dusuruldu (cast cokmez) ==")
store.upsert_tenant("t_ann4", "A4", "k4")
for i in range(6):
    store.add_memory({"id": f"f{i}", "tenant_id": "t_ann4", "content": f"f{i}", "embedding": vec(i)})
store.add_memory({"id": "nanrow", "tenant_id": "t_ann4", "content": "nan",
                  "embedding": [float("nan")] + [1.0] * (DIM - 1)})
check((store.get_memory("t_ann4", "nanrow") or {}).get("embedding") is None,
      "NaN embedding None'a dusuruldu (saklanmadi)")
try:
    r6 = store.candidate_memories("t_ann4", "default", vec(1), 4)
    check(len(r6) == 4, "NaN satir var ama candidate cokmedi")
except Exception as e:
    check(False, f"NaN COKERTTI: {e}")

print()
if FAILS:
    print(f"SONUC: {len(FAILS)} BASARISIZ"); sys.exit(1)
print("SONUC: PUSH-DOWN ANN DOGRULANDI (top-k = Python cosine ile ayni)")
