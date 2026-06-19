"""test_pgvector_hnsw.py — HNSW push-down GERÇEKTEN ateşleniyor mu (postgres-only).

ensure_vector_index(dim) sonrası candidate_* indexli emb_vec yoluna geçmeli; trigger sayesinde
index'ten SONRA eklenen satırlar da emb_vec'i otomatik dolup ANN'de bulunmalı; NULL-embedding
satır ANN'i çökertmemeli. SQLite'ta (cast/python yol) anlamsız -> SKIP.

Teardown emb_vec kolonu/trigger'ı düşürür: TRUNCATE kolonu koruduğundan, bırakılırsa sonraki
cast-path testleri (değişken stub boyutu) bozulurdu.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402

if store.BACKEND != "store_pg":
    print("test_pgvector_hnsw.py: SKIP (postgres-only)")
    sys.exit(0)

if not store.vector_enabled():
    print("test_pgvector_hnsw.py: SKIP (pgvector yok)")
    sys.exit(0)

DIM = 24          # N(20) < DIM -> i%DIM=i: her vektör TEKİL (tie yok, top-1 belirgin)
T = "t_hnsw"


def vec(i):
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    v[(i + 1) % DIM] = 0.25   # iyi-ayrışmış birim-benzeri vektörler (HNSW top-1 recall ~1.0)
    return v


def fail(msg):
    print("test_pgvector_hnsw.py: FAIL -", msg)
    sys.exit(1)


try:
    store.reset_all()
    store.upsert_tenant(T, "HNSW", "k_hnsw")

    # index'ten ÖNCE ekle: satırlar yalnız embedding (text-JSON) taşır
    N = 20
    for i in range(N):
        store.add_memory({"id": f"m{i}", "tenant_id": T, "content": f"m{i}", "embedding": vec(i)})

    # sabit-boyut emb_vec kolonu + HNSW index + backfill + trigger
    store.ensure_vector_index(DIM)
    if store._has_emb_vec("memories") is not True:
        fail("emb_vec kolonu kurulmadi")

    # total(20) > k -> ANN yolu aktif; tam k satir donmeli
    K = 5
    got = store.candidate_memories(T, "default", vec(3), K)
    if len(got) != K:
        fail(f"HNSW K aday donmedi: {len(got)}")
    if got[0]["id"] != "m3":
        fail(f"HNSW top-1 yanlis (beklenen m3): {got[0]['id']}")

    # index'ten SONRA ekle -> trigger emb_vec'i doldurmali -> ANN'de bulunabilmeli (sessiz kayip yok)
    store.add_memory({"id": "m_new", "tenant_id": T, "content": "m_new", "embedding": vec(3)})
    ids2 = {m["id"] for m in store.candidate_memories(T, "default", vec(3), 3)}
    if "m_new" not in ids2 and "m3" not in ids2:
        fail(f"trigger sonrasi ANN'de bulunamadi: {ids2}")

    # NULL-embedding satir: index disi (parite notu) ama ANN'i COKERTMEMELI
    store.add_memory({"id": "m_null", "tenant_id": T, "content": "m_null", "embedding": None})
    if len(store.candidate_memories(T, "default", vec(3), K)) != K:
        fail("NULL-embedding satir ANN'i bozdu")

    # boyut-uyusmaz sorgu -> cast-path'e dusmeli (cokmemeli)
    if len(store.candidate_memories(T, "default", [0.1, 0.2, 0.3], K)) != K:
        fail("boyut-uyusmaz sorgu fallback bozuk")

    # --- PLANNER KANITI: olcekte HNSW index'i GERCEKTEN seciliyor mu ---
    # (kucuk-N'de btree+sort dogru; ANN degeri olcekte. ensure_vector_index ANALYZE yapar ->
    #  planner bayat istatistik yuzunden btree'de kalmamali.)
    import json as _json  # noqa: E402

    import psycopg  # noqa: E402
    import store_pg  # noqa: E402
    store.reset_all()
    store.upsert_tenant(T, "HNSW", "k_hnsw")
    with psycopg.connect(store_pg._DSN) as c:
        for i in range(2000):
            c.execute("INSERT INTO memories(id,tenant_id,user_id,content,embedding,salience,"
                      "created_at,updated_at,valid_at) VALUES(%s,%s,'default',%s,%s,1.0,0,0,0)",
                      (f"s{i}", T, f"s{i}", _json.dumps(vec(i))))
    store.ensure_vector_index(DIM)
    with psycopg.connect(store_pg._DSN) as c:
        plan = "\n".join(str(r[0]) for r in c.execute(
            "EXPLAIN SELECT * FROM memories WHERE tenant_id=%s ORDER BY emb_vec <=> %s::vector LIMIT 5",
            (T, store_pg._vec_literal(vec(50)))).fetchall())
    if "idx_memories_hnsw" not in plan:
        fail("planner olcekte HNSW indexini SECMEDI:\n" + plan)

    print("test_pgvector_hnsw.py: PASS")
finally:
    try:
        store.drop_vector_index()
    except Exception:
        pass
