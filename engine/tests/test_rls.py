"""test_rls.py — DB-katmanı tenant izolasyonu (RLS) GERÇEKTEN fail-closed mu (postgres-only).

apply_rls() ile non-owner app rolü + policy kurulur; app rolüyle bağlanıp app.cce_tenant GUC'u
değiştirilerek KANITLANIR: (1) bağlam=A -> yalnız A, (2) bağlam=B -> A görünmez, (3) bağlam yok
-> hiçbir satır (fail-closed), (4) açık cross-tenant WHERE bile sızdırmaz, (5) yanlış bağlamda
INSERT (WITH CHECK) reddedilir. Owner RLS'i baypas eder -> conformance (owner) etkilenmez.

Teardown: policy/RLS kapat + rol düşür (şemayı diğer testlere bırakma). SQLite'ta RLS yok -> SKIP.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402

if store.BACKEND != "store_pg":
    print("test_rls.py: SKIP (postgres-only)")
    sys.exit(0)
if store.rls_enabled():
    print("test_rls.py: SKIP (CCE_RLS_DSN zaten set — test kendi rolünü kurar)")
    sys.exit(0)

import psycopg  # noqa: E402

import store_pg  # noqa: E402

OWNER = store_pg._DSN
APP_PW = "CceAppTest123x"


def _app_dsn(pw):
    parts = dict(kv.split("=", 1) for kv in OWNER.split())
    parts["user"] = "cce_app"
    parts["password"] = pw
    return " ".join(f"{k}={v}" for k, v in parts.items())


def fail(msg):
    print("test_rls.py: FAIL -", msg)
    sys.exit(1)


def app_ids(app_dsn, guc, where=""):
    with psycopg.connect(app_dsn) as c:
        c.execute("SELECT set_config('app.cce_tenant', %s, false)", (guc,))
        return {r[0] for r in c.execute("SELECT id FROM memories " + where).fetchall()}


try:
    store.reset_all()
    store.upsert_tenant("A", "A", "ka")
    store.upsert_tenant("B", "B", "kb")
    store.add_memory({"id": "a1", "tenant_id": "A", "user_id": "default", "content": "alfa kayit"})
    store.add_memory({"id": "b1", "tenant_id": "B", "user_id": "default", "content": "beta kayit"})

    store.apply_rls(app_password=APP_PW)   # non-owner rol + policy + RLS enable
    APP = _app_dsn(APP_PW)

    if app_ids(APP, "A") != {"a1"}:
        fail("baglam=A yalniz A'yi gormeli")
    if app_ids(APP, "B") != {"b1"}:
        fail("baglam=B yalniz B'yi gormeli (A sizdi)")
    if app_ids(APP, "") != set():
        fail("baglam YOK -> hicbir satir olmamali (fail-closed)")
    # açık cross-tenant WHERE bile policy'yi aşamaz
    if app_ids(APP, "B", "WHERE tenant_id='A'") != set():
        fail("acik cross-tenant WHERE sizdirdi (RLS policy asildi)")

    # WITH CHECK: yanlış bağlamda INSERT reddedilmeli
    blocked = False
    try:
        with psycopg.connect(APP) as c:
            c.execute("SELECT set_config('app.cce_tenant', %s, false)", ("B",))
            c.execute("INSERT INTO memories(id,tenant_id,user_id,content) VALUES('x1','A','default','sizdir')")
    except psycopg.Error:
        blocked = True
    if not blocked:
        fail("WITH CHECK yanlis-tenant INSERT'i engellemedi")

    # --- PLUMBING: store._conn (operatörün yolu) RLS modunda set_tenant bağlamını uyguluyor mu ---
    store_pg._RLS_DSN = APP   # RLS modunu aç (monkeypatch; üretimde CCE_RLS_DSN env)
    try:
        tokA = store.set_tenant("A")
        only_a = {m["id"] for m in store.get_active_memories("A", "default")}
        store.reset_tenant(tokA)
        store.set_tenant("B")
        a_via_b = {m["id"] for m in store.get_active_memories("A", "default")}  # bağlam B, sorgu A -> RLS keser
        store.set_tenant(None)
        none_ctx = {m["id"] for m in store.get_active_memories("A", "default")}
    finally:
        store_pg._RLS_DSN = ""   # RLS modunu kapat (teardown owner ile)
        store.reset_tenant()
    if only_a != {"a1"}:
        fail(f"plumbing baglam=A yanlis (set_tenant uygulanmadi?): {only_a}")
    if a_via_b != set():
        fail(f"plumbing: baglam=B iken A sorgusu sizdi (fail-closed degil): {a_via_b}")
    if none_ctx != set():
        fail(f"plumbing: baglam yok -> bos olmali: {none_ctx}")

    print("test_rls.py: PASS")
finally:
    try:
        with psycopg.connect(OWNER, autocommit=True) as c:
            for t in store_pg._RLS_DATA_TABLES:
                c.execute(f"DROP POLICY IF EXISTS p_tenant ON {t}")
                c.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
            try:
                c.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM cce_app")
                c.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM cce_app")
                c.execute("REVOKE ALL ON SCHEMA public FROM cce_app")
                c.execute("DROP ROLE IF EXISTS cce_app")
            except psycopg.Error:
                pass
    except Exception:
        pass
