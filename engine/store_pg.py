#!/usr/bin/env python3
"""store_pg.py — PostgreSQL backend (üretim substratı). store_sqlite.py'nin birebir
davranış-eşi portu; public API + dict şekilleri AYNI (conformance: aynı testler iki
backend'de de geçer). CCE_STORE_BACKEND=postgres ile store.py buraya yönlenir.

Step B = davranış paritesi: embedding'ler hâlâ JSON-text (SQLite ile aynı), Python cosine.
Step C = pgvector push-down (vector kolon + HNSW + ORDER BY <=>), ayrı eklenecek.

DSN: CCE_DATABASE_URL (yoksa local dev container: 127.0.0.1:5433/cce).
Çeviri notları: ? -> %s, :name -> %(name)s; min() -> least(); json_* -> jsonb_*;
INSERT OR IGNORE -> ON CONFLICT DO NOTHING; trfold = locale-bağımsız Türkçe-fold SQL fn;
try_consume/add_rule/delete_rule = pg_advisory_xact_lock ile serileşir (BEGIN IMMEDIATE eşi).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import re
import time
import uuid

import psycopg
from psycopg.rows import dict_row

import crypto  # at-rest alan şifrelemesi (anahtar yoksa no-op)

DB_PATH = None  # SQLite'a özgü; postgres'te yok (store.DB_PATH referansına karşı güvenli)
_DSN = os.environ.get(
    "CCE_DATABASE_URL",
    "host=127.0.0.1 port=5433 dbname=cce user=postgres password=postgres",
)

_LIST_FIELDS = ("forbid_patterns", "require_patterns")
_VALID_PLANS = {"free", "solo", "starter", "growth", "pro", "enterprise"}

# HNSW push-down durumu: sabit-boyut emb_vec kolonu + index AKTİFSE candidate_* indexli yola geçer.
# _EMB_DIM=0 (env CCE_EMBED_DIM yok) -> cast-path (test/küçük-N; oracle paritesi korunur).
_EMB_VEC_COLS: dict[str, bool] = {}   # f"{_DSN}|{tbl}" -> emb_vec kolonu var mı (process-içi cache)
_EMB_DIM: int = 0                      # aktif sabit emb_vec boyutu (0 = HNSW kapalı)
_VEC_TABLES = ("memories", "kg_edges", "kg_entities", "tombstones")

_TABLES = ("tenants", "usage", "api_keys", "users", "auth_tokens", "subscriptions", "webhook_events", "webhooks",
           "rules", "corrections", "violations", "checks", "admin_log", "rule_history", "memories",
           "tombstones", "handoffs", "kg_entities", "kg_edges", "taxonomy_nodes", "ingest_jobs")

_FUNC_STMTS = [
    # locale-BAĞIMSIZ Türkçe-güvenli fold: İ/I/ı->i + tüm Türkçe büyükleri küçüğe map +
    # ASCII lower. (PG lower() C-locale'de non-ASCII'yi küçültmez; bu yüzden translate ile.)
    "CREATE OR REPLACE FUNCTION trfold(s text) RETURNS text AS "
    "$$ SELECT lower(btrim(translate(coalesce(s,''), "
    "'İIıÇŞĞÜÖ', 'iiiçşğüö'))) $$ "
    "LANGUAGE sql IMMUTABLE",
    # text(JSON) -> jsonb array (bozuk/null/non-array guard); add_rule pattern-merge için.
    "CREATE OR REPLACE FUNCTION _jarr(s text) RETURNS jsonb AS "
    "$$ SELECT CASE WHEN s IS NOT NULL AND pg_input_is_valid(s,'jsonb') "
    "AND jsonb_typeof(s::jsonb)='array' THEN s::jsonb ELSE '[]'::jsonb END $$ "
    "LANGUAGE sql IMMUTABLE",
]

_SCHEMA_STMTS = [
    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at double precision)",
    "CREATE TABLE IF NOT EXISTS tenants (tenant_id TEXT PRIMARY KEY, name TEXT, "
    "api_key TEXT UNIQUE, plan TEXT DEFAULT 'free', created_at double precision)",
    "CREATE TABLE IF NOT EXISTS usage (tenant_id TEXT, period TEXT, ops double precision DEFAULT 0, "
    "PRIMARY KEY (tenant_id, period))",
    "CREATE TABLE IF NOT EXISTS api_keys (id TEXT, tenant_id TEXT, key_hash TEXT UNIQUE, name TEXT, "
    "role TEXT DEFAULT 'member', created_at double precision, revoked_at double precision, "
    "PRIMARY KEY (tenant_id, id))",
    "CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash)",
    "CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, tenant_id TEXT, "
    "password_hash TEXT, email_verified double precision, approved double precision, "
    "created_at double precision)",
    "CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)",
    "CREATE TABLE IF NOT EXISTS auth_tokens (token_hash TEXT PRIMARY KEY, tenant_id TEXT, "
    "email TEXT, purpose TEXT, expires_at double precision, used_at double precision)",
    "CREATE INDEX IF NOT EXISTS idx_authtok_email ON auth_tokens(email)",
    "CREATE TABLE IF NOT EXISTS subscriptions (provider_ref TEXT PRIMARY KEY, tenant_id TEXT, "
    "created_at double precision)",
    "CREATE TABLE IF NOT EXISTS webhook_events (event_id TEXT PRIMARY KEY, created_at double precision)",
    "CREATE TABLE IF NOT EXISTS webhooks (id TEXT, tenant_id TEXT, url TEXT, secret_hash TEXT, "
    "events TEXT, active INTEGER DEFAULT 1, last_status TEXT, last_at double precision, "
    "created_at double precision, PRIMARY KEY (tenant_id, id))",
    "CREATE TABLE IF NOT EXISTS rules (id TEXT, tenant_id TEXT, project TEXT, type TEXT, severity TEXT, "
    "source TEXT, correction_id TEXT, message TEXT, rule TEXT, forbid_patterns TEXT, require_patterns TEXT, "
    "status TEXT DEFAULT 'active', version INTEGER DEFAULT 1, created_at double precision, "
    "updated_at double precision, PRIMARY KEY (tenant_id, id))",
    "CREATE TABLE IF NOT EXISTS corrections (id TEXT, tenant_id TEXT, project TEXT, category TEXT, "
    "what_was_wrong TEXT, correct_behavior TEXT, reusable_rule TEXT, severity TEXT, tags TEXT, "
    "created_at double precision, PRIMARY KEY (tenant_id, id))",
    "CREATE TABLE IF NOT EXISTS violations (tenant_id TEXT, rule_id TEXT, bucket TEXT, trace TEXT, "
    "evidence TEXT, created_at double precision)",
    "CREATE TABLE IF NOT EXISTS checks (tenant_id TEXT, rule_id TEXT, passed INTEGER, source TEXT, "
    "created_at double precision)",
    "CREATE TABLE IF NOT EXISTS admin_log (tenant_id TEXT, action TEXT, target TEXT, detail TEXT, "
    "created_at double precision)",
    "CREATE TABLE IF NOT EXISTS rule_history (tenant_id TEXT, rule_id TEXT, version INTEGER, "
    "snapshot TEXT, change TEXT, created_at double precision)",
    "CREATE TABLE IF NOT EXISTS memories (id TEXT, tenant_id TEXT, user_id TEXT, content TEXT, "
    "embedding TEXT, source TEXT, metadata TEXT, salience double precision DEFAULT 1.0, "
    "access_count INTEGER DEFAULT 0, created_at double precision, updated_at double precision, "
    "valid_at double precision, invalid_at double precision, last_accessed_at double precision, "
    "taxonomy_node_id TEXT, taxonomy_path TEXT, auto_tags TEXT, routing_confidence double precision, "
    "subject_party TEXT, pii_flags TEXT, "
    "PRIMARY KEY (tenant_id, id))",
    "CREATE TABLE IF NOT EXISTS taxonomy_nodes (tenant_id TEXT, id TEXT, parent_id TEXT, slug TEXT, "
    "label TEXT, path TEXT, centroid TEXT, member_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', "
    "version INTEGER DEFAULT 1, created_at double precision, updated_at double precision, "
    "PRIMARY KEY (tenant_id, id))",
    "CREATE INDEX IF NOT EXISTS idx_mem_tenant_user ON memories(tenant_id, user_id)",
    "CREATE TABLE IF NOT EXISTS tombstones (tenant_id TEXT, content_hash TEXT, embedding TEXT, "
    "created_at double precision, PRIMARY KEY (tenant_id, content_hash))",
    "CREATE TABLE IF NOT EXISTS handoffs (tenant_id TEXT, session_id TEXT, summary TEXT, fact_ids TEXT, "
    "metadata TEXT, created_at double precision, updated_at double precision, "
    "PRIMARY KEY (tenant_id, session_id))",
    "CREATE TABLE IF NOT EXISTS kg_entities (id TEXT, tenant_id TEXT, name TEXT, norm TEXT, type TEXT, "
    "attributes TEXT, embedding TEXT, created_at double precision, updated_at double precision, "
    "PRIMARY KEY (tenant_id, id))",
    "CREATE INDEX IF NOT EXISTS idx_kgent_tenant_norm ON kg_entities(tenant_id, norm)",
    "CREATE TABLE IF NOT EXISTS kg_edges (id TEXT, tenant_id TEXT, subject_id TEXT, predicate TEXT, "
    "object_id TEXT, object_value TEXT, fact TEXT, embedding TEXT, valid_at double precision, "
    "invalid_at double precision, created_at double precision, expired_at double precision, source TEXT, "
    "PRIMARY KEY (tenant_id, id))",
    "CREATE INDEX IF NOT EXISTS idx_kgedge_tenant_subj ON kg_edges(tenant_id, subject_id)",
    "CREATE INDEX IF NOT EXISTS idx_kgedge_tenant_pred ON kg_edges(tenant_id, subject_id, predicate)",
    "CREATE TABLE IF NOT EXISTS ingest_jobs (id TEXT, tenant_id TEXT, kind TEXT, payload TEXT, "
    "status TEXT DEFAULT 'queued', result TEXT, error TEXT, attempts INTEGER DEFAULT 0, "
    "created_at double precision, updated_at double precision, PRIMARY KEY (tenant_id, id))",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingest_jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_checks_tenant_rule ON checks(tenant_id, rule_id)",
    "CREATE INDEX IF NOT EXISTS idx_violations_tenant ON violations(tenant_id)",
]

_SCHEMA_DONE: set[str] = set()
_VECTOR_OK = False                # pgvector extension mevcut mu (yoksa push-down -> return-all fallback)
_INIT_LOCK_KEY = 947100847        # session advisory-lock anahtarı (eşzamanlı cold-start DDL serileşir)

# Versiyonlu migration'lar (base = v1). Base sonrası evrimsel şema değişiklikleri buraya:
#   (version, [SQL stmt, ...]). Açık + kayıtlı (schema_migrations) -> üretim DB'sinde
#   ilk-bağlantı CREATE'e güvenmek yerine deterministik, tekrarlanabilir geçiş.
_MIGRATIONS: list[tuple[int, list[str]]] = [
    # users tablosu Neon'da email_verified'sız oluşmuştu -> kolonu ekle (fresh DB'de no-op)
    (2, ["ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified double precision"]),
    # onay kapısı: approved kolonu + MEVCUT kullanıcıları onaylı geç (grandfather; kilitlenme yok)
    (3, ["ALTER TABLE users ADD COLUMN IF NOT EXISTS approved double precision",
         "UPDATE users SET approved = extract(epoch from now()) WHERE approved IS NULL"]),
    # giden (outbound) webhook kayıt tablosu (mevcut DB'de yoksa oluştur)
    (4, ["CREATE TABLE IF NOT EXISTS webhooks (id TEXT, tenant_id TEXT, url TEXT, secret_hash TEXT, "
         "events TEXT, active INTEGER DEFAULT 1, last_status TEXT, last_at double precision, "
         "created_at double precision, PRIMARY KEY (tenant_id, id))"]),
    # AACRP: otonom kategorizasyon — taksonomi tablosu + memories routing kolonları
    (5, ["CREATE TABLE IF NOT EXISTS taxonomy_nodes (tenant_id TEXT, id TEXT, parent_id TEXT, slug TEXT, "
         "label TEXT, path TEXT, centroid TEXT, member_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', "
         "version INTEGER DEFAULT 1, created_at double precision, updated_at double precision, "
         "PRIMARY KEY (tenant_id, id))",
         "ALTER TABLE memories ADD COLUMN IF NOT EXISTS taxonomy_node_id TEXT",
         "ALTER TABLE memories ADD COLUMN IF NOT EXISTS taxonomy_path TEXT",
         "ALTER TABLE memories ADD COLUMN IF NOT EXISTS auto_tags TEXT",
         "ALTER TABLE memories ADD COLUMN IF NOT EXISTS routing_confidence double precision"]),
    # AACRP attribution ekseni: PII tespiti + kim-in-verisi (KVKK cross-contamination)
    (6, ["ALTER TABLE memories ADD COLUMN IF NOT EXISTS subject_party TEXT",
         "ALTER TABLE memories ADD COLUMN IF NOT EXISTS pii_flags TEXT"]),
    # iki-yönlü hibrit aday: blind lexical index (keyed-hash term kökleri) + GIN. Büyük-N'de
    # lexical-only kayıtlar dense-ANN top-k dışında kalsa bile aday havuzuna girer (bkz. _lex_index).
    (7, ["ALTER TABLE memories ADD COLUMN IF NOT EXISTS term_hashes text[]",
         "CREATE INDEX IF NOT EXISTS idx_memories_terms ON memories USING gin (term_hashes)"]),
]


def _ensure_schema() -> None:
    """Şema/migration init. EŞZAMANLI cold-start güvenli: session advisory-lock ile DDL serileşir
    (CREATE ... IF NOT EXISTS Postgres'te session'lar arası atomik DEĞİL -> aksi halde
    'tuple concurrently updated'/UniqueViolation). pgvector'ı self-provision eder;
    yoksa _VECTOR_OK=False -> candidate_* push-down return-all'a düşer (çökmez)."""
    global _VECTOR_OK
    if _DSN in _SCHEMA_DONE:
        return
    with psycopg.connect(_DSN, autocommit=True, row_factory=dict_row) as c:
        c.execute("SELECT pg_advisory_lock(%s)", (_INIT_LOCK_KEY,))
        try:
            try:
                c.execute("CREATE EXTENSION IF NOT EXISTS vector")   # pgvector self-provision
            except psycopg.Error:
                pass  # transient (cold-start) / yetki -> _VECTOR_OK aşağıda GERÇEK varlığa göre
            # create'in başarısına DEĞİL, gerçek extension varlığına bak (Neon cold-start transient
            # hatasında kalıcı False'a düşüp push-down'ı sessizce kapatmayı önler).
            _VECTOR_OK = bool(c.execute(
                "SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone())
            for stmt in _FUNC_STMTS + _SCHEMA_STMTS:   # base (v1) — idempotent
                c.execute(stmt)
            c.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,%s) "
                      "ON CONFLICT(version) DO NOTHING", (time.time(),))
            applied = {r["version"] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
            for ver, stmts in _MIGRATIONS:               # base sonrası, sıralı + bir kez
                if ver in applied:
                    continue
                for s in stmts:
                    c.execute(s)
                c.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(%s,%s) "
                          "ON CONFLICT(version) DO NOTHING", (ver, time.time()))
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (_INIT_LOCK_KEY,))
    _SCHEMA_DONE.add(_DSN)
    _dim = os.environ.get("CCE_EMBED_DIM")
    if _dim and _VECTOR_OK:   # ÜRETİM: sabit boyut bilindiğinde HNSW'yi bir kez kur (cast-seq-scan -> index)
        try:
            ensure_vector_index(int(_dim))
        except Exception:
            pass  # best-effort: başarısızsa cast-path'te kal (çökme yok)


def _has_emb_vec(tbl: str) -> bool:
    """emb_vec (sabit-boyut, HNSW-indexli) kolonu bu tabloda var mı — cache'li."""
    key = f"{_DSN}|{tbl}"
    if key not in _EMB_VEC_COLS:
        try:
            with psycopg.connect(_DSN, row_factory=dict_row) as c:
                r = c.execute("SELECT 1 FROM information_schema.columns "
                              "WHERE table_name=%s AND column_name='emb_vec' LIMIT 1", (tbl,)).fetchone()
            _EMB_VEC_COLS[key] = bool(r)
        except psycopg.Error:
            return False
    return _EMB_VEC_COLS[key]


def _hnsw_ready(tbl: str, query_vec) -> bool:
    """HNSW indexli yol kullanılabilir mi: sabit boyut aktif + sorgu boyutu eşleşiyor + kolon var.
    Boyut uyuşmazsa cast-path'e düşer (yanlış-boyut <=> pgvector'da çöker — onu önle)."""
    return bool(_EMB_DIM) and len(query_vec) == _EMB_DIM and _has_emb_vec(tbl)


def vector_enabled() -> bool:
    """pgvector push-down aktif mi (canlı bayrak). store.py re-export'u modül-düzeyi
    _VECTOR_OK'in BAYAT kopyasını verir; bu fonksiyon referansla re-export edildiğinden
    seam üzerinden GERÇEK durumu döndürür. Şema init'i garanti eder."""
    _ensure_schema()
    return _VECTOR_OK


def schema_version() -> int:
    """Uygulanan en yüksek migration sürümü (ops/doğrulama)."""
    _ensure_schema()
    with psycopg.connect(_DSN, row_factory=dict_row) as c:
        r = c.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()
        return int(r["v"]) if r and r["v"] is not None else 0


_RLS_DSN = os.environ.get("CCE_RLS_DSN", "").strip()   # set => non-owner app rolü + RLS aktif (env-gated)
_tenant_ctx: contextvars.ContextVar = contextvars.ContextVar("cce_tenant", default=None)


def rls_enabled() -> bool:
    """RLS modu açık mı (CCE_RLS_DSN set => app rolü + per-request GUC). Kapalıyken owner DSN = bugünkü davranış."""
    return bool(_RLS_DSN)


def hnsw_active() -> bool:
    """HNSW indexli yol aktif mi (CCE_EMBED_DIM set + ensure_vector_index çalıştı). Status doğrulaması."""
    return bool(_EMB_DIM)


def set_tenant(tenant_id):
    """Request boundary: bu bağlamdaki tenant'ı RLS GUC'u için sabitle (api._auth + worker çağırır).
    RLS kapalıyken etkisiz (yalnız contextvar). Dönen token reset_tenant'a verilir."""
    return _tenant_ctx.set(tenant_id)


def reset_tenant(token=None) -> None:
    """İstek sonu: bağlamı geri al (token varsa) ya da None'a zorla (keep-alive sızıntısı önle)."""
    if token is not None:
        try:
            _tenant_ctx.reset(token)
            return
        except Exception:
            pass
    _tenant_ctx.set(None)


def _owner_conn() -> psycopg.Connection:
    """Owner (RLS-BAYPAS) bağlantı: şema/migration + cross-tenant (worker job claim, reclaim) için.
    RLS modunda da DAİMA owner DSN. Bu yolla okunan satırlar tenant'a göre FİLTRELENMEZ -> yalnız
    gerçekten cross-tenant olan, güvenilir-süreç çağrıları kullanır."""
    _ensure_schema()
    return psycopg.connect(_DSN, row_factory=dict_row)


def _conn() -> psycopg.Connection:
    """Yeni bağlantı (dict_row). `with _conn() as c:` çıkışta commit + close (SQLite eşi).
    RLS modunda (CCE_RLS_DSN) NON-OWNER app rolüyle bağlanır + bu bağlantıya app.cce_tenant GUC'unu
    contextvar'dan yazar -> RLS policy satırları tenant'a kilitler. GUC boş/yoksa current_setting
    NULL -> hiçbir RLS satırı eşleşmez (FAIL-CLOSED: unutulan WHERE sızıntı değil, no-op olur).
    NOT: v0 connection-per-call; üretimde pool eklenecek (PRD)."""
    _ensure_schema()
    if not _RLS_DSN:
        return psycopg.connect(_DSN, row_factory=dict_row)
    c = psycopg.connect(_RLS_DSN, row_factory=dict_row)
    t = _tenant_ctx.get()
    c.execute("SELECT set_config('app.cce_tenant', %s, false)", (t if t is not None else "",))
    return c


def reset_all() -> None:
    """TEST izolasyonu: tüm tabloları temizle (conformance runner her test dosyasından önce çağırır)."""
    _ensure_schema()
    with psycopg.connect(_DSN, autocommit=True) as c:
        c.execute("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE")


def _alock(c, key: str) -> None:
    """Transaction-süreli advisory lock (BEGIN IMMEDIATE eşi): aynı anahtar için yazıcıları serile."""
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


# ---------------- helpers ----------------
def _hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _fold(s) -> str:
    s = "" if s is None else str(s)
    return s.replace("İ", "i").replace("I", "i").replace("ı", "i").strip().casefold()


_TOK = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", re.UNICODE)   # memory._TOK ile aynı


def _lex_index(text: str) -> list[str]:
    """Blind lexical index: düz-metinden keyed-hash term kökleri (iki-yönlü hibrit aday).
    Kök = 4-char prefix (len>=4) — memory._term_match çekim-eki davranışının SÜPERSET'i
    (aday kaybı yok; false-positive'i Python BM25 re-rank zaten eler). content şifreli olduğu
    için DB'de düz-metin FTS yapılamaz; aynı fonksiyon hem yazımda hem sorguda -> tutarlı."""
    roots = set()
    for t in _TOK.findall(text or ""):
        f = _fold(t)
        if len(f) < 3:
            continue
        roots.add(f[:4] if len(f) >= 4 else f)
    return [crypto.blind(r) for r in roots]


def _safe_salience(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1.0
    if f != f or f in (float("inf"), float("-inf")):
        return 1.0
    return max(0.0, min(f, 5.0))


def _safe_embedding(emb):
    """write-boundary: geçersiz/sonsuz embedding'i (NaN/Inf, non-list, non-numeric) DÜŞÜR (None).
    pgvector ::vector cast'i NaN/Inf reddeder -> aksi halde her sonraki <=> sorgusu çöker.
    None embedding lexical/fallback yolunda korunur (NULL satırlar erişilebilir kalır)."""
    if not isinstance(emb, (list, tuple)) or not emb:
        return None
    try:
        out = [float(x) for x in emb]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in out):
        return None
    return out


def _finite_vec(v) -> bool:
    return bool(v) and isinstance(v, (list, tuple)) and all(
        isinstance(x, (int, float)) and math.isfinite(x) for x in v)


def _row_to_rule(row: dict) -> dict:
    d = dict(row)
    for f in _LIST_FIELDS:
        d[f] = json.loads(d.get(f) or "[]")
    return d


def _row_to_memory(row: dict) -> dict:
    d = dict(row)
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else {}
    if "auto_tags" in d:
        d["auto_tags"] = json.loads(d["auto_tags"]) if d.get("auto_tags") else []
    if "pii_flags" in d:
        d["pii_flags"] = json.loads(d["pii_flags"]) if d.get("pii_flags") else []
    d["content"] = crypto.decrypt(d.get("content"))
    return d


# ---------------- taxonomy (AACRP otonom kategorizasyon) ----------------
def replace_taxonomy(tenant_id: str, nodes: list[dict]) -> None:
    """Tenant'ın taksonomisini DEĞİŞTİR (induce çıktısı)."""
    now = time.time()
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM taxonomy_nodes WHERE tenant_id=%s", (tenant_id,))
        cur.executemany(
            "INSERT INTO taxonomy_nodes(tenant_id,id,parent_id,slug,label,path,centroid,member_count,"
            "status,version,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active',1,%s,%s)",
            [(tenant_id, n["id"], n.get("parent_id"), n.get("slug", ""), n.get("label", ""), n.get("path", ""),
              json.dumps(n["centroid"]) if n.get("centroid") else None, n.get("member_count", 0), now, now)
             for n in nodes])


def list_taxonomy(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id,parent_id,slug,label,path,member_count,status FROM taxonomy_nodes "
                         "WHERE tenant_id=%s AND status='active' ORDER BY member_count DESC, label",
                         (tenant_id,)).fetchall()
        return [dict(r) for r in rows]


def bulk_set_routing(tenant_id: str, rows: list[dict]) -> None:
    """Toplu routing yaz: rows [{id,node_id,path,tags,confidence}]."""
    if not rows:
        return
    with _conn() as c, c.cursor() as cur:
        cur.executemany("UPDATE memories SET taxonomy_node_id=%s, taxonomy_path=%s, auto_tags=%s, "
                        "routing_confidence=%s WHERE tenant_id=%s AND id=%s",
                        [(r.get("node_id"), r.get("path"), json.dumps(r.get("tags") or []),
                          r.get("confidence"), tenant_id, r["id"]) for r in rows])


def _row_to_entity(row: dict) -> dict:
    d = dict(row)
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    d["attributes"] = json.loads(d["attributes"]) if d.get("attributes") else {}
    return d


def _row_to_edge(row: dict) -> dict:
    d = dict(row)
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    d["fact"] = crypto.decrypt(d.get("fact"))
    return d


# ---------------- tenants ----------------
def upsert_tenant(tenant_id: str, name: str, api_key: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO tenants(tenant_id,name,api_key,created_at) VALUES(%s,%s,%s,%s) "
                  "ON CONFLICT(tenant_id) DO UPDATE SET name=excluded.name, api_key=excluded.api_key",
                  (tenant_id, name, _hash_key(api_key), time.time()))


def tenant_by_key(api_key: str) -> str | None:
    if not api_key:
        return None
    h = _hash_key(api_key)
    with _conn() as c:
        r = c.execute("SELECT tenant_id FROM api_keys WHERE key_hash=%s AND revoked_at IS NULL",
                      (h,)).fetchone()
        if r:
            return r["tenant_id"]
        r = c.execute("SELECT tenant_id FROM tenants WHERE api_key=%s", (h,)).fetchone()
        return r["tenant_id"] if r else None


def add_api_key(tenant_id: str, key_hash: str, name: str, role: str = "member") -> str:
    kid = "key-" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO api_keys(id,tenant_id,key_hash,name,role,created_at,revoked_at) "
                  "VALUES(%s,%s,%s,%s,%s,%s,NULL)", (kid, tenant_id, key_hash, name, role, time.time()))
    return kid


def revoke_api_key(tenant_id: str, key_id: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE api_keys SET revoked_at=%s WHERE tenant_id=%s AND id=%s AND revoked_at IS NULL",
                        (time.time(), tenant_id, key_id))
        return cur.rowcount


def list_api_keys(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id,name,role,created_at,revoked_at FROM api_keys WHERE tenant_id=%s "
                         "ORDER BY created_at DESC", (tenant_id,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "role": r["role"],
                 "created_at": r["created_at"], "revoked": r["revoked_at"] is not None} for r in rows]


def add_webhook(tenant_id: str, url: str, secret_hash: str, events: str) -> str:
    wid = "wh-" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO webhooks(id,tenant_id,url,secret_hash,events,active,created_at) "
                  "VALUES(%s,%s,%s,%s,%s,1,%s)", (wid, tenant_id, url, secret_hash, events, time.time()))
    return wid


def list_webhooks(tenant_id: str, active_only: bool = False) -> list[dict]:
    q = "SELECT id,url,events,active,last_status,last_at,created_at,secret_hash FROM webhooks WHERE tenant_id=%s"
    if active_only:
        q += " AND active=1"
    q += " ORDER BY created_at DESC"
    with _conn() as c:
        rows = c.execute(q, (tenant_id,)).fetchall()
        return [{"id": r["id"], "url": r["url"], "events": (r["events"] or "").split(",") if r["events"] else [],
                 "active": bool(r["active"]), "last_status": r["last_status"], "last_at": r["last_at"],
                 "created_at": r["created_at"], "secret_hash": r["secret_hash"]} for r in rows]


def delete_webhook(tenant_id: str, webhook_id: str) -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM webhooks WHERE tenant_id=%s AND id=%s", (tenant_id, webhook_id))
        return cur.rowcount


def touch_webhook(tenant_id: str, webhook_id: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE webhooks SET last_status=%s, last_at=%s WHERE tenant_id=%s AND id=%s",
                  (status[:120], time.time(), tenant_id, webhook_id))


def role_by_key(api_key: str) -> str | None:
    if not api_key:
        return None
    h = _hash_key(api_key)
    with _conn() as c:
        r = c.execute("SELECT role FROM api_keys WHERE key_hash=%s AND revoked_at IS NULL", (h,)).fetchone()
        if r:
            return r["role"]
        r = c.execute("SELECT 1 AS x FROM tenants WHERE api_key=%s", (h,)).fetchone()
        return "owner" if r else None


# ---------------- users (insan email/şifre auth katmanı) ----------------
def create_user(email: str, tenant_id: str, password_hash: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO users(email,tenant_id,password_hash,created_at) VALUES(%s,%s,%s,%s)",
                  (email, tenant_id, password_hash, time.time()))


def get_user_by_email(email: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT email,tenant_id,password_hash,email_verified,approved,created_at "
                      "FROM users WHERE email=%s", (email,)).fetchone()
        return dict(r) if r else None


def get_user_by_tenant(tenant_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT email,tenant_id,password_hash,email_verified,approved,created_at "
                      "FROM users WHERE tenant_id=%s LIMIT 1", (tenant_id,)).fetchone()
        return dict(r) if r else None


def list_pending_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT email,tenant_id,email_verified,created_at FROM users "
                         "WHERE approved IS NULL ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def set_user_approved(email: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET approved=%s WHERE email=%s", (time.time(), email)).rowcount


def update_password(email: str, password_hash: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET password_hash=%s WHERE email=%s",
                         (password_hash, email)).rowcount


def mark_email_verified(email: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET email_verified=%s WHERE email=%s",
                         (time.time(), email)).rowcount


def create_auth_token(token_hash: str, tenant_id: str, email: str, purpose: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO auth_tokens(token_hash,tenant_id,email,purpose,expires_at,used_at) "
                  "VALUES(%s,%s,%s,%s,%s,NULL) ON CONFLICT(token_hash) DO UPDATE SET "
                  "tenant_id=excluded.tenant_id, email=excluded.email, purpose=excluded.purpose, "
                  "expires_at=excluded.expires_at, used_at=NULL",
                  (token_hash, tenant_id, email, purpose, expires_at))


def consume_auth_token(token_hash: str, purpose: str) -> dict | None:
    """Atomik: geçerli (kullanılmamış + süresi dolmamış + doğru amaç) ise işaretle + döndür."""
    now = time.time()
    with _conn() as c:
        r = c.execute("UPDATE auth_tokens SET used_at=%s WHERE token_hash=%s AND purpose=%s "
                      "AND used_at IS NULL AND expires_at>%s "
                      "RETURNING tenant_id,email,purpose", (now, token_hash, purpose, now)).fetchone()
        return dict(r) if r else None


# ---------------- plan / usage ----------------
def set_plan(tenant_id: str, plan: str) -> None:
    if plan not in _VALID_PLANS:
        raise ValueError(f"unknown plan: {plan}")
    with _conn() as c:
        c.execute("UPDATE tenants SET plan=%s WHERE tenant_id=%s", (plan, tenant_id))


def link_subscription(tenant_id: str, provider_ref: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO subscriptions(provider_ref,tenant_id,created_at) VALUES(%s,%s,%s) "
                  "ON CONFLICT(provider_ref) DO UPDATE SET tenant_id=excluded.tenant_id",
                  (provider_ref, tenant_id, time.time()))


def tenant_by_subscription(provider_ref: str) -> str | None:
    if not provider_ref:
        return None
    with _conn() as c:
        r = c.execute("SELECT tenant_id FROM subscriptions WHERE provider_ref=%s",
                      (provider_ref,)).fetchone()
        return r["tenant_id"] if r else None


def seen_event(event_id: str) -> bool:
    if not event_id:
        return False
    with _conn() as c:
        cur = c.execute("INSERT INTO webhook_events(event_id,created_at) VALUES(%s,%s) "
                        "ON CONFLICT(event_id) DO NOTHING", (event_id, time.time()))
        return cur.rowcount == 0


def get_plan(tenant_id: str) -> str:
    with _conn() as c:
        r = c.execute("SELECT plan FROM tenants WHERE tenant_id=%s", (tenant_id,)).fetchone()
        return r["plan"] if (r and r["plan"]) else "free"


def add_usage(tenant_id: str, period: str, ops: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO usage(tenant_id,period,ops) VALUES(%s,%s,%s) "
                  "ON CONFLICT(tenant_id,period) DO UPDATE SET ops=usage.ops+excluded.ops",
                  (tenant_id, period, ops))


def get_usage(tenant_id: str, period: str) -> float:
    with _conn() as c:
        r = c.execute("SELECT ops FROM usage WHERE tenant_id=%s AND period=%s",
                      (tenant_id, period)).fetchone()
        return float(r["ops"]) if r else 0.0


def try_consume(tenant_id: str, period: str, cost: float, limit) -> bool:
    """ATOMİK kota: advisory-lock ile aynı (tenant,period) yazıcılarını serile, sonra
    read-check-upsert (SQLite BEGIN IMMEDIATE eşi). limit None -> sınırsız."""
    with _conn() as c:
        _alock(c, "usage|" + tenant_id + "|" + period)
        row = c.execute("SELECT ops FROM usage WHERE tenant_id=%s AND period=%s",
                        (tenant_id, period)).fetchone()
        used = float(row["ops"]) if row else 0.0
        if limit is not None and used + cost > limit:
            return False
        c.execute("INSERT INTO usage(tenant_id,period,ops) VALUES(%s,%s,%s) "
                  "ON CONFLICT(tenant_id,period) DO UPDATE SET ops=usage.ops+excluded.ops",
                  (tenant_id, period, cost))
        return True


# ---------------- rules ----------------
def add_rule(rule: dict) -> None:
    r = dict(rule)
    r.setdefault("status", "active")
    now = time.time()
    r.setdefault("created_at", now)
    _t = str(r.get("type") or "soft").strip().lower()
    r["type"] = _t if _t in ("hard", "soft") else "soft"
    _s = str(r.get("status") or "active").strip().lower()
    r["status"] = _s if _s in ("active", "disabled") else "active"
    _sv = str(r.get("severity") or "medium").strip().lower()
    r["severity"] = _sv if _sv in ("low", "medium", "high", "critical") else "high"
    _p = r.get("project")
    r["project"] = _p.strip() if (isinstance(_p, str) and _p.strip()) else None
    if (r.get("forbid_patterns") or r.get("require_patterns")) and r["type"] != "hard":
        r["type"] = "hard"
    with _conn() as c:
        _alock(c, "rule|" + str(r["tenant_id"]) + "|" + str(r["id"]))
        c.execute(
            "INSERT INTO rule_history(tenant_id,rule_id,version,snapshot,change,created_at) "
            "SELECT tenant_id,id,version,jsonb_build_object('type',type,'severity',severity,"
            "'project',project,'message',message,'rule',rule,'forbid_patterns',forbid_patterns,"
            "'require_patterns',require_patterns,'status',status)::text,'update',%s "
            "FROM rules WHERE tenant_id=%s AND id=%s",
            (now, r["tenant_id"], r["id"]))
        c.execute(
            "INSERT INTO rules(id,tenant_id,project,type,severity,source,correction_id,message,rule,"
            "forbid_patterns,require_patterns,status,version,created_at,updated_at) "
            "VALUES(%(id)s,%(tenant_id)s,%(project)s,%(type)s,%(severity)s,%(source)s,%(correction_id)s,"
            "%(message)s,%(rule)s,%(forbid_patterns)s,%(require_patterns)s,%(status)s,1,%(created_at)s,"
            "%(updated_at)s) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET project=excluded.project,"
            "type=CASE WHEN rules.type='hard' THEN 'hard' ELSE excluded.type END,"
            "severity=CASE WHEN "
            "(CASE rules.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 3 END)"
            ">=(CASE excluded.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 3 END)"
            " THEN rules.severity ELSE excluded.severity END,"
            "source=excluded.source,correction_id=excluded.correction_id,"
            "message=excluded.message,rule=excluded.rule,"
            "forbid_patterns=(SELECT coalesce(jsonb_agg(DISTINCT v),'[]'::jsonb)::text FROM "
            "(SELECT jsonb_array_elements_text(_jarr(rules.forbid_patterns)) v "
            "UNION SELECT jsonb_array_elements_text(_jarr(excluded.forbid_patterns)) v) z),"
            "require_patterns=(SELECT coalesce(jsonb_agg(DISTINCT v),'[]'::jsonb)::text FROM "
            "(SELECT jsonb_array_elements_text(_jarr(rules.require_patterns)) v "
            "UNION SELECT jsonb_array_elements_text(_jarr(excluded.require_patterns)) v) z),"
            "status=CASE WHEN rules.status='disabled' THEN 'disabled' ELSE excluded.status END,"
            "version=rules.version+1,updated_at=excluded.updated_at",
            {
                "id": r["id"], "tenant_id": r["tenant_id"], "project": r.get("project"),
                "type": r.get("type", "soft"), "severity": r.get("severity", "medium"),
                "source": r.get("source", ""), "correction_id": r.get("correction_id"),
                "message": r.get("message", ""), "rule": r.get("rule", ""),
                "forbid_patterns": json.dumps(r.get("forbid_patterns", [])),
                "require_patterns": json.dumps(r.get("require_patterns", [])),
                "status": r["status"], "created_at": r["created_at"], "updated_at": now,
            })


def get_rules(tenant_id: str, project: str | None = None, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM rules WHERE tenant_id=%s"
    args: list = [tenant_id]
    if active_only:
        q += " AND status='active'"
    if project:
        q += " AND (trfold(project)=trfold(%s) OR project IS NULL)"
        args.append(project)
    with _conn() as c:
        return [_row_to_rule(r) for r in c.execute(q, args).fetchall()]


def list_projects(tenant_id: str) -> list[dict]:
    """Tenant'ın projeleri (kural namespace'leri) — distinct project + kural sayıları.
    Boş/NULL project = tüm projelere uygulanan genel kurallar ('' kovası)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT COALESCE(project,'') AS project, COUNT(*) AS rules, "
            "SUM(CASE WHEN type='hard' THEN 1 ELSE 0 END) AS hard, "
            "MAX(updated_at) AS updated_at "
            "FROM rules WHERE tenant_id=%s AND status='active' "
            "GROUP BY COALESCE(project,'') ORDER BY rules DESC",
            (tenant_id,)).fetchall()
        return [{"project": r["project"], "rules": int(r["rules"]), "hard": int(r["hard"] or 0),
                 "soft": int(r["rules"]) - int(r["hard"] or 0), "updated_at": r["updated_at"]}
                for r in rows]


def rule_exists_like(tenant_id: str, project: str | None, message: str,
                     rtype: str | None = None) -> str | None:
    norm = " ".join((message or "").lower().split())
    if not norm:
        return None
    pkey = _fold(project)
    for r in get_rules(tenant_id, project=None, active_only=True):
        if (_fold(r.get("project")) == pkey
                and (rtype is None or r.get("type") == rtype)
                and " ".join((r.get("message") or "").lower().split()) == norm):
            return r["id"]
    return None


def record_admin(tenant_id: str, action: str, target: str = "", detail: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO admin_log(tenant_id,action,target,detail,created_at) VALUES(%s,%s,%s,%s,%s)",
                  (tenant_id, action, target, detail, time.time()))


def get_admin_log(tenant_id: str, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT action,target,detail,created_at FROM admin_log "
                         "WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
                         (tenant_id, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in rows]


def disable_rule(tenant_id: str, rule_id: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE rules SET status='disabled' WHERE tenant_id=%s AND id=%s",
                        (tenant_id, rule_id))
        n = cur.rowcount
    if n:
        record_admin(tenant_id, "disable_rule", rule_id)
    return n


def delete_rule(tenant_id: str, rule_id: str) -> int:
    with _conn() as c:
        _alock(c, "rule|" + str(tenant_id) + "|" + str(rule_id))
        c.execute(
            "INSERT INTO rule_history(tenant_id,rule_id,version,snapshot,change,created_at) "
            "SELECT tenant_id,id,version,jsonb_build_object('type',type,'severity',severity,"
            "'project',project,'message',message,'rule',rule,'forbid_patterns',forbid_patterns,"
            "'require_patterns',require_patterns,'status',status)::text,'delete',%s "
            "FROM rules WHERE tenant_id=%s AND id=%s",
            (time.time(), tenant_id, rule_id))
        cur = c.execute("DELETE FROM rules WHERE tenant_id=%s AND id=%s", (tenant_id, rule_id))
        n = cur.rowcount
    if n:
        record_admin(tenant_id, "delete_rule", rule_id)
    return n


def get_rule_history(tenant_id: str, rule_id: str, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT version,change,snapshot,created_at FROM rule_history "
                         "WHERE tenant_id=%s AND rule_id=%s ORDER BY created_at DESC LIMIT %s",
                         (tenant_id, rule_id, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]


def purge_tenant(tenant_id: str) -> dict:
    counts = {}
    with _conn() as c:
        for tbl in ("rules", "rule_history", "corrections", "violations", "checks", "memories",
                    "tombstones", "handoffs", "kg_entities", "kg_edges", "taxonomy_nodes", "ingest_jobs",
                    "usage", "webhooks", "api_keys", "users", "auth_tokens", "subscriptions", "tenants"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "purge_tenant", "", detail=json.dumps(counts))
    return counts


def clear_knowledge(tenant_id: str) -> dict:
    counts = {}
    with _conn() as c:
        for tbl in ("rules", "rule_history", "corrections", "violations", "checks",
                    "memories", "handoffs", "kg_entities", "kg_edges", "taxonomy_nodes"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "clear_knowledge", "", detail=json.dumps(counts))
    return counts


def clear_imported(tenant_id: str) -> dict:
    """Vault re-import için: tenant'ın HAFIZA + KNOWLEDGE GRAPH'ını sil; kurallar/correction/
    hesap/tombstone'a DOKUNMA (enforce politikan ve erasure kalıcılığı korunur)."""
    counts = {}
    with _conn() as c:
        for tbl in ("memories", "kg_entities", "kg_edges", "taxonomy_nodes"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "clear_imported", "", detail=json.dumps(counts))
    return counts


def checkpoint() -> None:
    pass  # WAL'a özgü; postgres'te no-op


def prune_telemetry(days: int = 90) -> dict:
    cutoff = time.time() - days * 86400
    out = {}
    with _conn() as c:
        for tbl in ("checks", "violations"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE created_at < %s", (cutoff,))
            out[tbl] = cur.rowcount
    return out


# ---------------- corrections ----------------
def add_correction(corr: dict) -> None:
    c0 = dict(corr)
    c0.setdefault("created_at", time.time())
    with _conn() as c:
        c.execute(
            "INSERT INTO corrections(id,tenant_id,project,category,what_was_wrong,"
            "correct_behavior,reusable_rule,severity,tags,created_at) "
            "VALUES(%(id)s,%(tenant_id)s,%(project)s,%(category)s,%(what_was_wrong)s,%(correct_behavior)s,"
            "%(reusable_rule)s,%(severity)s,%(tags)s,%(created_at)s) "
            "ON CONFLICT(tenant_id,id) DO NOTHING",
            {
                "id": c0["id"], "tenant_id": c0["tenant_id"], "project": c0.get("project"),
                "category": c0.get("category", ""), "what_was_wrong": c0.get("what_was_wrong", ""),
                "correct_behavior": c0.get("correct_behavior", ""),
                "reusable_rule": c0.get("reusable_rule", ""), "severity": c0.get("severity", "medium"),
                "tags": json.dumps(c0.get("tags", [])), "created_at": c0["created_at"],
            })


def get_corrections(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM corrections WHERE tenant_id=%s", (tenant_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            out.append(d)
        return out


# ---------------- violations / measurement ----------------
def record_violation(tenant_id: str, rule_id: str, bucket: str, trace: str, evidence: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO violations(tenant_id,rule_id,bucket,trace,evidence,created_at) "
                  "VALUES(%s,%s,%s,%s,%s,%s)", (tenant_id, rule_id, bucket, trace, evidence, time.time()))


def record_check(tenant_id: str, rule_id: str, passed: bool, source: str = "runtime",
                 at: float | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO checks(tenant_id,rule_id,passed,source,created_at) VALUES(%s,%s,%s,%s,%s)",
                  (tenant_id, rule_id, 1 if passed else 0, source,
                   at if at is not None else time.time()))


def record_enforcement(tenant_id: str, evaluated: list[dict], violations: list[dict],
                       source: str = "runtime", trace: str = "") -> None:
    with _conn() as c:
        ts = time.time()
        for ev in evaluated:
            c.execute("INSERT INTO checks(tenant_id,rule_id,passed,source,created_at) VALUES(%s,%s,%s,%s,%s)",
                      (tenant_id, ev["rule_id"], 1 if ev["passed"] else 0, source, ts))
        for v in violations:
            c.execute("INSERT INTO violations(tenant_id,rule_id,bucket,trace,evidence,created_at) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (tenant_id, v["rule_id"], source, trace,
                                                    v.get("evidence", ""), ts))


def compliance_by_rule(tenant_id: str, since: float = 0.0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_id, COUNT(*) AS checks, SUM(passed) AS passed, "
            "SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) AS violations "
            "FROM checks WHERE tenant_id=%s AND created_at>=%s GROUP BY rule_id",
            (tenant_id, since)).fetchall()
        out = []
        for r in rows:
            checks = int(r["checks"] or 0)
            passed = int(r["passed"] or 0)
            out.append({"rule_id": r["rule_id"], "checks": checks, "passed": passed,
                        "violations": int(r["violations"] or 0),
                        "compliance": (passed / checks) if checks else None})
        return out


def compliance_timeseries(tenant_id: str, days: int = 14, rule_id: str | None = None) -> list[dict]:
    cutoff = time.time() - days * 86400
    q = ("SELECT (to_timestamp(created_at) AT TIME ZONE 'UTC')::date::text AS d, COUNT(*) AS checks, "
         "SUM(passed) AS passed FROM checks WHERE tenant_id=%s AND created_at>=%s")
    args: list = [tenant_id, cutoff]
    if rule_id:
        q += " AND rule_id=%s"
        args.append(rule_id)
    q += " GROUP BY d ORDER BY d"
    with _conn() as c:
        out = []
        for r in c.execute(q, args).fetchall():
            checks = int(r["checks"] or 0)
            passed = int(r["passed"] or 0)
            out.append({"date": r["d"], "checks": checks, "passed": passed,
                        "compliance": (passed / checks) if checks else None})
        return out


def rule_health(tenant_id: str, stale_days: int = 7) -> list[dict]:
    """Aktif kuralların sağlık raporu (sqlite ile davranış paritesi). rules LEFT JOIN checks ->
    hiç-tetiklenmemiş + bayat (N gün atışsız) kuralları yüzeye çıkarır."""
    now = time.time()
    cutoff = now - stale_days * 86400
    _rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    with _conn() as c:
        rows = c.execute(
            "SELECT r.id, r.type, r.severity, r.correction_id, "
            "COUNT(ch.rule_id) AS checks, COALESCE(SUM(ch.passed),0) AS passed, "
            "MAX(ch.created_at) AS last_fired "
            "FROM rules r LEFT JOIN checks ch "
            "ON ch.tenant_id=r.tenant_id AND ch.rule_id=r.id "
            "WHERE r.tenant_id=%s AND r.status='active' "
            "GROUP BY r.id, r.type, r.severity, r.correction_id", (tenant_id,)).fetchall()
    out = []
    for r in rows:
        checks = int(r["checks"] or 0)
        passed = int(r["passed"] or 0)
        last_fired = float(r["last_fired"]) if r["last_fired"] is not None else None
        idle_days = int((now - last_fired) // 86400) if last_fired else None
        never = checks == 0
        out.append({
            "rule_id": r["id"], "type": r["type"], "severity": r["severity"],
            "from_correction": bool(r["correction_id"]),
            "checks": checks, "passed": passed, "violations": checks - passed,
            "compliance": (passed / checks) if checks else None,
            "last_fired": last_fired, "idle_days": idle_days, "never_fired": never,
            "stale": never or (last_fired is not None and last_fired < cutoff),
        })
    out.sort(key=lambda x: (0 if x["stale"] else 1, _rank.get(x["severity"], 9), -x["checks"]))
    return out


# ---------------- memories ----------------
def add_memory(mem: dict) -> None:
    m = dict(mem)
    now = time.time()
    m.setdefault("created_at", now)
    m.setdefault("valid_at", now)
    emb = _safe_embedding(m.get("embedding"))
    with _conn() as c:
        c.execute(
            "INSERT INTO memories(id,tenant_id,user_id,content,embedding,source,metadata,"
            "salience,access_count,created_at,updated_at,valid_at,invalid_at,last_accessed_at,"
            "subject_party,pii_flags,term_hashes) "
            "VALUES(%(id)s,%(tenant_id)s,%(user_id)s,%(content)s,%(embedding)s,%(source)s,%(metadata)s,"
            "%(salience)s,0,%(created_at)s,%(updated_at)s,%(valid_at)s,NULL,NULL,"
            "%(subject_party)s,%(pii_flags)s,%(term_hashes)s) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET content=excluded.content,"
            "embedding=excluded.embedding,source=excluded.source,metadata=excluded.metadata,"
            "subject_party=excluded.subject_party,pii_flags=excluded.pii_flags,"
            "term_hashes=excluded.term_hashes,"
            "salience=least(memories.salience+0.5, 5.0),updated_at=excluded.updated_at,"
            "valid_at=excluded.valid_at",
            {
                "id": m["id"], "tenant_id": m["tenant_id"], "user_id": m.get("user_id", "default"),
                "content": crypto.encrypt(m.get("content", "")),
                "embedding": json.dumps(emb) if emb is not None else None,
                "source": m.get("source", "conversation"),
                "metadata": json.dumps(m.get("metadata", {})),
                "salience": _safe_salience(m.get("salience", 1.0)),
                "created_at": m["created_at"], "updated_at": now, "valid_at": m["valid_at"],
                "subject_party": m.get("subject_party"),
                "pii_flags": json.dumps(m["pii_flags"]) if m.get("pii_flags") else None,
                "term_hashes": _lex_index(m.get("content", "")),   # blind lexical index (düz-metinden)
            })


def get_active_memories(tenant_id: str, user_id: str | None = None, *, exclude_third: bool = False) -> list[dict]:
    q = "SELECT * FROM memories WHERE tenant_id=%s AND invalid_at IS NULL"
    args: list = [tenant_id]
    if user_id is not None:
        q += " AND user_id=%s"
        args.append(user_id)
    if exclude_third:   # self-context recall: 3.şahıs PII'si kullanıcının bağlamına SIZMAZ (KVKK)
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    with _conn() as c:
        return [_row_to_memory(r) for r in c.execute(q, args).fetchall()]


def get_memory(tenant_id: str, mem_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM memories WHERE tenant_id=%s AND id=%s",
                      (tenant_id, mem_id)).fetchone()
        return _row_to_memory(r) if r else None


def invalidate_memory(tenant_id: str, mem_id: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE memories SET invalid_at=%s, content='', embedding=NULL "
                        "WHERE tenant_id=%s AND id=%s AND invalid_at IS NULL",
                        (time.time(), tenant_id, mem_id))
        return cur.rowcount


def reinforce_memory(tenant_id: str, mem_id: str) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute("UPDATE memories SET salience=least(salience+0.5,5.0), updated_at=%s, valid_at=%s "
                        "WHERE tenant_id=%s AND id=%s AND invalid_at IS NULL", (now, now, tenant_id, mem_id))
        return cur.rowcount


def touch_memories(tenant_id: str, ids: list[str]) -> None:
    if not ids:
        return
    now = time.time()
    with _conn() as c, c.cursor() as cur:
        cur.executemany("UPDATE memories SET access_count=access_count+1, last_accessed_at=%s "
                        "WHERE tenant_id=%s AND id=%s", [(now, tenant_id, i) for i in ids])


def add_tombstone(tenant_id: str, content_hash: str, embedding: list | None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO tombstones(tenant_id,content_hash,embedding,created_at) "
                  "VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,content_hash) DO UPDATE SET "
                  "embedding=excluded.embedding",
                  (tenant_id, content_hash, json.dumps(embedding) if embedding is not None else None,
                   time.time()))


def get_tombstones(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT content_hash, embedding FROM tombstones WHERE tenant_id=%s",
                         (tenant_id,)).fetchall()
        return [{"content_hash": r["content_hash"],
                 "embedding": json.loads(r["embedding"]) if r["embedding"] else None} for r in rows]


def count_memories(tenant_id: str, user_id: str | None = None, active_only: bool = True,
                   *, exclude_third: bool = False) -> int:
    q = "SELECT COUNT(*) AS n FROM memories WHERE tenant_id=%s"
    args: list = [tenant_id]
    if active_only:
        q += " AND invalid_at IS NULL"
    if user_id is not None:
        q += " AND user_id=%s"
        args.append(user_id)
    if exclude_third:
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    with _conn() as c:
        return int(c.execute(q, args).fetchone()["n"])


def count_distinct_users(tenant_id: str) -> int:
    """Genel bakış: son-kullanıcı sayısı (tüm satırları materialize etmeden)."""
    with _conn() as c:
        return int(c.execute("SELECT COUNT(DISTINCT user_id) AS n FROM memories WHERE tenant_id=%s "
                             "AND invalid_at IS NULL", (tenant_id,)).fetchone()["n"])


def count_kg(tenant_id: str) -> dict:
    """Genel bakış: knowledge graph boyutu (varlık + güncel kenar)."""
    now = time.time()
    with _conn() as c:
        ents = int(c.execute("SELECT COUNT(*) AS n FROM kg_entities WHERE tenant_id=%s", (tenant_id,)).fetchone()["n"])
        edges = int(c.execute("SELECT COUNT(*) AS n FROM kg_edges WHERE tenant_id=%s AND expired_at IS NULL "
                              "AND valid_at<=%s AND (invalid_at IS NULL OR invalid_at>%s)",
                              (tenant_id, now, now)).fetchone()["n"])
    return {"entities": ents, "edges": edges}


def pii_summary(tenant_id: str) -> dict:
    """Genel bakış / KVKK: aktif kayıtların attribution dağılımı + PII'li kayıt sayısı."""
    out = {"self": 0, "third_party": 0, "unknown": 0, "with_pii": 0}
    with _conn() as c:
        for r in c.execute("SELECT COALESCE(subject_party,'self') AS sp, COUNT(*) AS n FROM memories "
                           "WHERE tenant_id=%s AND invalid_at IS NULL GROUP BY COALESCE(subject_party,'self')",
                           (tenant_id,)).fetchall():
            out[r["sp"]] = out.get(r["sp"], 0) + int(r["n"])
        out["with_pii"] = int(c.execute("SELECT COUNT(*) AS n FROM memories WHERE tenant_id=%s AND invalid_at IS NULL "
                                        "AND pii_flags IS NOT NULL AND pii_flags<>'[]' AND pii_flags<>''",
                                        (tenant_id,)).fetchone()["n"])
    return out


def list_users(tenant_id: str) -> list[dict]:
    """Tenant'ın son-kullanıcıları: distinct user_id + aktif memory sayısı + son/ilk aktivite.
    (Users yüzeyi — çok-kullanıcılı hafıza platformu görünümü.) Forget'li satırlar sayılmaz."""
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, COUNT(*) AS n, "
            "MAX(COALESCE(updated_at, valid_at, created_at)) AS last_at, "
            "MIN(COALESCE(created_at, valid_at)) AS first_at, "
            "SUM(salience) AS sal "
            "FROM memories WHERE tenant_id=%s AND invalid_at IS NULL "
            "GROUP BY user_id ORDER BY n DESC, last_at DESC",
            (tenant_id,)).fetchall()
        return [{"user_id": r["user_id"], "memories": int(r["n"]), "last_at": r["last_at"],
                 "first_at": r["first_at"],
                 "salience": float(r["sal"]) if r["sal"] is not None else 0.0} for r in rows]


# ---------------- handoffs ----------------
def add_handoff(tenant_id: str, session_id: str, summary: str,
                fact_ids: list | None = None, metadata: dict | None = None) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO handoffs(tenant_id,session_id,summary,fact_ids,metadata,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,session_id) DO UPDATE SET "
            "summary=excluded.summary,fact_ids=excluded.fact_ids,metadata=excluded.metadata,"
            "updated_at=excluded.updated_at",
            (tenant_id, session_id, summary or "", json.dumps(fact_ids or []),
             json.dumps(metadata or {}), now, now))


def get_handoff(tenant_id: str, session_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM handoffs WHERE tenant_id=%s AND session_id=%s",
                      (tenant_id, session_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["fact_ids"] = json.loads(d.get("fact_ids") or "[]")
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        return d


def list_handoffs(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT session_id,summary,updated_at FROM handoffs WHERE tenant_id=%s "
                         "ORDER BY updated_at DESC", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------- knowledge graph ----------------
def add_entity(ent: dict) -> None:
    e = dict(ent)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    with _conn() as c:
        c.execute(
            "INSERT INTO kg_entities(id,tenant_id,name,norm,type,attributes,embedding,created_at,updated_at) "
            "VALUES(%(id)s,%(tenant_id)s,%(name)s,%(norm)s,%(type)s,%(attributes)s,%(embedding)s,"
            "%(created_at)s,%(updated_at)s) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET name=excluded.name,type=excluded.type,"
            "attributes=excluded.attributes,embedding=COALESCE(excluded.embedding,kg_entities.embedding),"
            "updated_at=excluded.updated_at",
            {"id": e["id"], "tenant_id": e["tenant_id"], "name": e.get("name", ""),
             "norm": e.get("norm", ""), "type": e.get("type", ""),
             "attributes": json.dumps(e.get("attributes", {})),
             "embedding": json.dumps(emb) if emb is not None else None,
             "created_at": e.get("created_at", now), "updated_at": now})


def get_entity(tenant_id: str, ent_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM kg_entities WHERE tenant_id=%s AND id=%s",
                      (tenant_id, ent_id)).fetchone()
        return _row_to_entity(r) if r else None


def get_entity_by_norm(tenant_id: str, norm: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM kg_entities WHERE tenant_id=%s AND norm=%s LIMIT 1",
                      (tenant_id, norm)).fetchone()
        return _row_to_entity(r) if r else None


def list_entities(tenant_id: str) -> list[dict]:
    with _conn() as c:
        return [_row_to_entity(r) for r in
                c.execute("SELECT * FROM kg_entities WHERE tenant_id=%s", (tenant_id,)).fetchall()]


def add_edge(edge: dict) -> int:
    e = dict(edge)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,"
            "embedding,valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(%(id)s,%(tenant_id)s,%(subject_id)s,%(predicate)s,%(object_id)s,%(object_value)s,"
            "%(fact)s,%(embedding)s,%(valid_at)s,%(invalid_at)s,%(created_at)s,%(expired_at)s,%(source)s) "
            "ON CONFLICT(tenant_id,id) DO NOTHING",
            {"id": e["id"], "tenant_id": e["tenant_id"], "subject_id": e.get("subject_id"),
             "predicate": e.get("predicate", ""), "object_id": e.get("object_id"),
             "object_value": e.get("object_value"), "fact": crypto.encrypt(e.get("fact", "")),
             "embedding": json.dumps(emb) if emb is not None else None,
             "valid_at": e.get("valid_at", now), "invalid_at": e.get("invalid_at"),
             "created_at": e.get("created_at", now), "expired_at": e.get("expired_at"),
             "source": e.get("source", "")})
        return cur.rowcount


def bulk_add_entities(tenant_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    now = time.time()
    with _conn() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT INTO kg_entities(id,tenant_id,name,norm,type,attributes,embedding,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,'{}',NULL,%s,%s) ON CONFLICT(tenant_id,id) DO NOTHING",
            [(r["id"], tenant_id, r.get("name", ""), r.get("norm", ""), r.get("type", "note"), now, now) for r in rows])


def bulk_add_memories(rows: list[dict]) -> None:
    if not rows:
        return
    now = time.time()
    params = [(m["id"], m["tenant_id"], m.get("user_id", "default"), crypto.encrypt(m.get("content", "")),
               (lambda e: json.dumps(e) if e is not None else None)(_safe_embedding(m.get("embedding"))),
               m.get("source", "obsidian"), json.dumps(m.get("metadata", {})),
               _safe_salience(m.get("salience", 1.0)), now, now, now,
               m.get("subject_party"), json.dumps(m["pii_flags"]) if m.get("pii_flags") else None,
               _lex_index(m.get("content", ""))) for m in rows]
    with _conn() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT INTO memories(id,tenant_id,user_id,content,embedding,source,metadata,salience,"
            "access_count,created_at,updated_at,valid_at,invalid_at,last_accessed_at,subject_party,pii_flags,"
            "term_hashes) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,NULL,NULL,%s,%s,%s) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET content=excluded.content,embedding=excluded.embedding,"
            "source=excluded.source,metadata=excluded.metadata,updated_at=excluded.updated_at,"
            "valid_at=excluded.valid_at,subject_party=excluded.subject_party,pii_flags=excluded.pii_flags,"
            "term_hashes=excluded.term_hashes", params)


def bulk_add_edges(rows: list[dict]) -> None:
    if not rows:
        return
    now = time.time()
    params = [(e["id"], e["tenant_id"], e.get("subject_id"), e.get("predicate", ""), e.get("object_id"),
               crypto.encrypt(e.get("fact", "")), e.get("valid_at", now), now, e.get("source", "obsidian"))
              for e in rows]
    with _conn() as c, c.cursor() as cur:
        cur.executemany(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,embedding,"
            "valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(%s,%s,%s,%s,%s,NULL,%s,NULL,%s,NULL,%s,NULL,%s) ON CONFLICT(tenant_id,id) DO NOTHING", params)


def invalidate_and_add_edge(tenant_id: str, bounds: list, new_edge: dict) -> dict:
    """ATOMİK çelişki-süperse (tek transaction): çelişen kenarları bound + yeni kenarı ekle.
    bounds=[(edge_id, invalid_at)]. Aynı özne için advisory-lock ile serileşir (out-of-order
    eşzamanlı ingest tutarlılığı). bound guard'ları (ters/forward yok) korunur."""
    e = dict(new_edge)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    invalidated = 0
    with _conn() as c:
        _alock(c, "kgsubj|" + str(tenant_id) + "|" + str(e.get("subject_id")))
        for eid, inv in bounds:
            cur = c.execute("UPDATE kg_edges SET invalid_at=%s WHERE tenant_id=%s AND id=%s "
                            "AND expired_at IS NULL AND valid_at<=%s AND (invalid_at IS NULL OR invalid_at>%s)",
                            (inv, tenant_id, eid, inv, inv))
            invalidated += cur.rowcount
        cur = c.execute(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,"
            "embedding,valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(%(id)s,%(tenant_id)s,%(subject_id)s,%(predicate)s,%(object_id)s,%(object_value)s,"
            "%(fact)s,%(embedding)s,%(valid_at)s,%(invalid_at)s,%(created_at)s,%(expired_at)s,%(source)s) "
            "ON CONFLICT(tenant_id,id) DO NOTHING",
            {"id": e["id"], "tenant_id": e["tenant_id"], "subject_id": e.get("subject_id"),
             "predicate": e.get("predicate", ""), "object_id": e.get("object_id"),
             "object_value": e.get("object_value"), "fact": crypto.encrypt(e.get("fact", "")),
             "embedding": json.dumps(emb) if emb is not None else None,
             "valid_at": e.get("valid_at", now), "invalid_at": e.get("invalid_at"),
             "created_at": e.get("created_at", now), "expired_at": e.get("expired_at"),
             "source": e.get("source", "")})
        added = cur.rowcount
    return {"invalidated": invalidated, "added": added}


def get_edges(tenant_id: str, subject_id: str | None = None, predicate: str | None = None,
              current_only: bool = True, now: float | None = None) -> list[dict]:
    q = "SELECT * FROM kg_edges WHERE tenant_id=%s"
    args: list = [tenant_id]
    if subject_id is not None:
        q += " AND subject_id=%s"
        args.append(subject_id)
    if predicate is not None:
        q += " AND predicate=%s"
        args.append(predicate)
    if current_only:
        nw = now if now is not None else time.time()
        q += " AND expired_at IS NULL AND valid_at<=%s AND (invalid_at IS NULL OR invalid_at>%s)"
        args += [nw, nw]
    with _conn() as c:
        return [_row_to_edge(r) for r in c.execute(q, args).fetchall()]


def bound_edge(tenant_id: str, edge_id: str, invalid_at: float) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE kg_edges SET invalid_at=%s "
                        "WHERE tenant_id=%s AND id=%s AND expired_at IS NULL AND valid_at<=%s "
                        "AND (invalid_at IS NULL OR invalid_at>%s)",
                        (invalid_at, tenant_id, edge_id, invalid_at, invalid_at))
        return cur.rowcount


def expire_edge(tenant_id: str, edge_id: str, expired_at: float | None = None) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE kg_edges SET expired_at=%s WHERE tenant_id=%s AND id=%s AND expired_at IS NULL",
                        (expired_at if expired_at is not None else time.time(), tenant_id, edge_id))
        return cur.rowcount


def get_edges_asof(tenant_id: str, world_time: float) -> list[dict]:
    with _conn() as c:
        return [_row_to_edge(r) for r in c.execute(
            "SELECT * FROM kg_edges WHERE tenant_id=%s AND expired_at IS NULL AND valid_at<=%s "
            "AND (invalid_at IS NULL OR invalid_at>%s)",
            (tenant_id, world_time, world_time)).fetchall()]


def count_entities(tenant_id: str) -> int:
    with _conn() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM kg_entities WHERE tenant_id=%s",
                             (tenant_id,)).fetchone()["n"])


# ---------------- semantic candidate generators (pgvector PUSH-DOWN) ----------------
# Sıralama DB'ye itilir: ORDER BY (embedding::vector) <=> q::vector LIMIT k. Python'a tüm
# satırları yüklemeden, C-seviyesi mesafe. Davranış-koruma: toplam <= k ise HEPSİNİ döndür
# (SQLite ile birebir; conformance bozulmaz). NOT: cast-tabanlı = herhangi-boyut, seq-scan;
# sabit-boyut HNSW index üretimde ensure_vector_index(dim) ile eklenir (aşağıda).
def _vec_literal(emb) -> str:
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def candidate_memories(tenant_id: str, user_id, query_vec, k: int, *, exclude_third: bool = False) -> list[dict]:
    _ensure_schema()  # _VECTOR_OK'i set eder (fallback kararından önce şart)
    # gate post-filter sayısıyla (SQLite ile parite: exclude_third uygulanmış kümeyi say)
    if not _finite_vec(query_vec) or not _VECTOR_OK or count_memories(tenant_id, user_id, active_only=True, exclude_third=exclude_third) <= k:
        return get_active_memories(tenant_id, user_id, exclude_third=exclude_third)
    q = "SELECT * FROM memories WHERE tenant_id=%s AND invalid_at IS NULL"
    args: list = [tenant_id]
    if user_id is not None:
        q += " AND user_id=%s"
        args.append(user_id)
    if exclude_third:   # self-context recall: 3.şahıs PII'si dışla (KVKK)
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    if _hnsw_ready("memories", query_vec):
        # ÜRETİM: indexli emb_vec üzerinden HNSW ANN (approximate, C-hızı, büyük-N). emb_vec'i NULL
        # olan satırlar (embed'siz/boyut-uyuşmaz) index dışı -> total>>k'da zaten top-k'ya girmezdi.
        q += " ORDER BY emb_vec <=> %s::vector LIMIT %s"
        args += [_vec_literal(query_vec), k]
    else:
        # cast-path (test/küçük-N): NULL/boyut-uyuşmaz satırlar -> mesafe 2.0 (en dipte ama DAHİL;
        # oracle gibi); doğru-boyut <=>. (NaN write'ta düşürüldü -> cast güvenli; dim-mismatch çökmez.)
        q += (" ORDER BY CASE WHEN embedding IS NULL OR jsonb_array_length(embedding::jsonb)<>%s "
              "THEN 2.0 ELSE (embedding::vector) <=> %s::vector END LIMIT %s")
        args += [len(query_vec), _vec_literal(query_vec), k]
    with _conn() as c:
        return [_row_to_memory(r) for r in c.execute(q, args).fetchall()]


def candidate_memories_lexical(tenant_id: str, user_id, query_hashes, k: int, *,
                               exclude_third: bool = False) -> list[dict]:
    """Blind lexical aday üretimi (iki-yönlü hibrit): term_hashes && sorgu-hash'leri (GIN-indexli).
    dense-ANN'in büyük-N'de kaçırdığı lexical-only kayıtları aday havuzuna sokar. Sıralama =
    ortak-terim sayısı DESC, tiebreak salience. query_hashes boşsa [] (lexical sinyal yok)."""
    _ensure_schema()
    qh = list(query_hashes or [])
    if not qh:
        return []
    q = ("SELECT *, (SELECT count(*) FROM unnest(term_hashes) th WHERE th = ANY(%s)) AS _ov "
         "FROM memories WHERE tenant_id=%s AND invalid_at IS NULL AND term_hashes && %s")
    args: list = [qh, tenant_id, qh]
    if user_id is not None:
        q += " AND user_id=%s"
        args.append(user_id)
    if exclude_third:
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    q += " ORDER BY _ov DESC, salience DESC LIMIT %s"
    args.append(k)
    with _conn() as c:
        return [_row_to_memory(r) for r in c.execute(q, args).fetchall()]


def backfill_lex_index(tenant_id: str | None = None, batch: int = 1000) -> int:
    """Eski kayıtlara (term_hashes IS NULL) blind lexical index'i geriye doldur: içeriği çöz,
    _lex_index hesapla, UPDATE et. Üretimde değişiklik öncesi yazılan veriye bir kez çalıştırılır
    (yeni yazımlar zaten dolu). Boş içerik -> boş dizi (NULL değil) => sonsuz döngü yok."""
    _ensure_schema()
    n = 0
    while True:
        q = "SELECT tenant_id,id,content FROM memories WHERE term_hashes IS NULL"
        args: list = []
        if tenant_id is not None:
            q += " AND tenant_id=%s"
            args.append(tenant_id)
        q += " LIMIT %s"
        args.append(batch)
        with _conn() as c:
            rows = c.execute(q, args).fetchall()
            for r in rows:
                c.execute("UPDATE memories SET term_hashes=%s WHERE tenant_id=%s AND id=%s",
                          (_lex_index(crypto.decrypt(r["content"]) or ""), r["tenant_id"], r["id"]))
        n += len(rows)
        if len(rows) < batch:
            break
    return n


def _count_edges_current(tenant_id: str, now: float) -> int:
    with _conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) AS n FROM kg_edges WHERE tenant_id=%s AND expired_at IS NULL "
            "AND valid_at<=%s AND (invalid_at IS NULL OR invalid_at>%s)",
            (tenant_id, now, now)).fetchone()["n"])


def candidate_edges(tenant_id: str, query_vec, k: int, now: float | None = None) -> list[dict]:
    _ensure_schema()
    nw = now if now is not None else time.time()
    if not _finite_vec(query_vec) or not _VECTOR_OK or _count_edges_current(tenant_id, nw) <= k:
        return get_edges(tenant_id, current_only=True, now=nw)
    base = ("SELECT * FROM kg_edges WHERE tenant_id=%s AND expired_at IS NULL AND valid_at<=%s "
            "AND (invalid_at IS NULL OR invalid_at>%s) ")
    if _hnsw_ready("kg_edges", query_vec):
        q = base + "ORDER BY emb_vec <=> %s::vector LIMIT %s"
        args = (tenant_id, nw, nw, _vec_literal(query_vec), k)
    else:
        q = base + ("ORDER BY CASE WHEN embedding IS NULL OR jsonb_array_length(embedding::jsonb)<>%s "
                    "THEN 2.0 ELSE (embedding::vector) <=> %s::vector END LIMIT %s")
        args = (tenant_id, nw, nw, len(query_vec), _vec_literal(query_vec), k)
    with _conn() as c:
        return [_row_to_edge(r) for r in c.execute(q, args).fetchall()]


def candidate_entities(tenant_id: str, query_vec, k: int) -> list[dict]:
    _ensure_schema()
    if not _finite_vec(query_vec) or not _VECTOR_OK or count_entities(tenant_id) <= k:
        return list_entities(tenant_id)
    if _hnsw_ready("kg_entities", query_vec):
        q = "SELECT * FROM kg_entities WHERE tenant_id=%s ORDER BY emb_vec <=> %s::vector LIMIT %s"
        args = (tenant_id, _vec_literal(query_vec), k)
    else:
        q = ("SELECT * FROM kg_entities WHERE tenant_id=%s "
             "ORDER BY CASE WHEN embedding IS NULL OR jsonb_array_length(embedding::jsonb)<>%s "
             "THEN 2.0 ELSE (embedding::vector) <=> %s::vector END LIMIT %s")
        args = (tenant_id, len(query_vec), _vec_literal(query_vec), k)
    with _conn() as c:
        return [_row_to_entity(r) for r in c.execute(q, args).fetchall()]


def ensure_vector_index(dim: int) -> None:
    """ÜRETİM optimizasyonu: sabit-boyut emb_vec kolonu + HNSW index + backfill + SENKRON TRIGGER.
    Üretimde embedding boyutu (örn. 1536) sabitlenince bir kez çağrılır (env CCE_EMBED_DIM ile
    boot'ta otomatik); cast-seq-scan -> indexli HNSW ANN'e geçer. Trigger ŞART: yoksa index yalnızca
    backfill anını görür, sonraki INSERT'lerde emb_vec NULL kalıp ANN dışı düşerdi (sessiz recall kaybı).
    Yerel cast-path testlerinde stub boyutları değişken olduğu için ÇAĞRILMAZ (env yok -> _EMB_DIM=0)."""
    global _EMB_DIM
    d = int(dim)
    with psycopg.connect(_DSN, autocommit=True) as c:
        # embedding (text-JSON) -> emb_vec senkronu: doğru boyutsa cast, değilse NULL (çökme yok)
        c.execute("CREATE OR REPLACE FUNCTION cce_sync_emb_vec() RETURNS trigger AS $f$ BEGIN "
                  "IF NEW.embedding IS NOT NULL AND jsonb_array_length(NEW.embedding::jsonb)=" + str(d) + " "
                  "THEN NEW.emb_vec := NEW.embedding::vector; ELSE NEW.emb_vec := NULL; END IF; "
                  "RETURN NEW; END; $f$ LANGUAGE plpgsql")
        for tbl in _VEC_TABLES:
            c.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS emb_vec vector({d})")
            c.execute(f"UPDATE {tbl} SET emb_vec = embedding::vector "
                      f"WHERE embedding IS NOT NULL AND emb_vec IS NULL "
                      f"AND jsonb_array_length(embedding::jsonb)={d}")
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_hnsw ON {tbl} "
                      f"USING hnsw (emb_vec vector_cosine_ops)")
            c.execute(f"ANALYZE {tbl}")   # backfill sonrası bayat istatistik -> planner HNSW yerine
            #                               btree+sort seçerdi; ANALYZE anında HNSW'yi devreye alır
            c.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_emb_vec ON {tbl}")
            c.execute(f"CREATE TRIGGER trg_{tbl}_emb_vec BEFORE INSERT OR UPDATE OF embedding "
                      f"ON {tbl} FOR EACH ROW EXECUTE FUNCTION cce_sync_emb_vec()")
            _EMB_VEC_COLS[f"{_DSN}|{tbl}"] = True
    _EMB_DIM = d


def drop_vector_index() -> None:
    """TEST teardown: emb_vec kolonu/trigger/index/fonksiyonu kaldır + cache sıfırla. HNSW testi
    şemayı (TRUNCATE kolonu korur) sonraki cast-path testlerine SIZDIRMASIN."""
    global _EMB_DIM
    with psycopg.connect(_DSN, autocommit=True) as c:
        for tbl in _VEC_TABLES:
            c.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_emb_vec ON {tbl}")
            c.execute(f"DROP INDEX IF EXISTS idx_{tbl}_hnsw")
            c.execute(f"ALTER TABLE {tbl} DROP COLUMN IF EXISTS emb_vec")
            _EMB_VEC_COLS.pop(f"{_DSN}|{tbl}", None)
        c.execute("DROP FUNCTION IF EXISTS cce_sync_emb_vec()")
    _EMB_DIM = 0


_RLS_DATA_TABLES = ("rules", "rule_history", "corrections", "violations", "checks", "memories",
                    "tombstones", "handoffs", "kg_entities", "kg_edges", "usage", "taxonomy_nodes")


def apply_rls(app_role: str = "cce_app", app_password: str | None = None) -> str:
    """ÜRETİM (DB-katmanı tenant izolasyonu, defense-in-depth): DATA tablolarında RLS + policy
    (tenant_id = current_setting('app.cce_tenant')). Owner(postgres) RLS'i baypas eder; ÜRETİMDE
    uygulama `app_role` (non-owner) ile bağlanıp HER İSTEKTE `SET app.cce_tenant=<tenant>` yapar ->
    unutulan bir WHERE tenant_id, sızıntı yerine fail-closed NO-OP olur (CISO-onaylı izolasyon,
    app WHERE filtresinin ALTINDA). Auth tabloları (tenants/api_keys/webhook_events/subscriptions)
    RLS'siz bırakılır: auth çözümü (key->tenant) cross-tenant okuma gerektirir.
    GUC ayarlanmazsa current_setting(...,true)=NULL -> hiçbir satır eşleşmez (fail-closed)."""
    import secrets
    from psycopg import sql
    if not app_role.replace("_", "").isalnum():   # identifier güvenliği (f-string'de kullanılıyor)
        raise ValueError("invalid app_role")
    if not app_password:   # Neon vb. parola-karmaşıklığı zorunlu: güçlü üret (upper+lower+digit+special)
        app_password = "Aa1!" + secrets.token_urlsafe(24)
    _ensure_schema()
    with psycopg.connect(_DSN, autocommit=True) as c:
        exists = c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app_role,)).fetchone()
        verb = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} LOGIN PASSWORD {}"
        # utility komutu bind-param almaz -> sql.Literal/Identifier ile güvenli inline
        c.execute(sql.SQL(verb).format(sql.Identifier(app_role), sql.Literal(app_password)))
        c.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")
        c.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {app_role}")
        c.execute(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {app_role}")
        for t in _RLS_DATA_TABLES:
            c.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
            c.execute(f"DROP POLICY IF EXISTS p_tenant ON {t}")
            c.execute(f"CREATE POLICY p_tenant ON {t} "
                      "USING (tenant_id = current_setting('app.cce_tenant', true)) "
                      "WITH CHECK (tenant_id = current_setting('app.cce_tenant', true))")
    return app_password   # deployer üretim app DSN'i için yakalar (üretmişsek)


# ---------------- async ingestion queue (Faz 2) ----------------
_MAX_JOB_ATTEMPTS = 5


def enqueue_job(tenant_id: str, kind: str, payload: dict) -> str:
    jid = "job-" + uuid.uuid4().hex[:16]
    now = time.time()
    with _conn() as c:
        c.execute("INSERT INTO ingest_jobs(id,tenant_id,kind,payload,status,attempts,created_at,updated_at) "
                  "VALUES(%s,%s,%s,%s,'queued',0,%s,%s)",
                  (jid, tenant_id, kind, json.dumps(payload), now, now))
    return jid


def has_pending_job(tenant_id: str, kind: str) -> bool:
    """Aynı tenant+kind için queued/running iş var mı (induce dedup-enqueue: eşzamanlı torn-state önle)."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM ingest_jobs WHERE tenant_id=%s AND kind=%s "
                         "AND status IN ('queued','running') LIMIT 1", (tenant_id, kind)).fetchone() is not None


def claim_job() -> dict | None:
    """En eski queued işi FOR UPDATE SKIP LOCKED ile atomik al (çoklu worker güvenli;
    iki worker aynı işi almaz). 'running'a çeker, attempts++. CROSS-TENANT (owner)."""
    with _owner_conn() as c:
        r = c.execute(
            "UPDATE ingest_jobs SET status='running', attempts=attempts+1, updated_at=%s "
            "WHERE (tenant_id,id) = (SELECT tenant_id,id FROM ingest_jobs WHERE status='queued' "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            (time.time(),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        return d


def complete_job(tenant_id: str, job_id: str, result: dict) -> None:
    with _conn() as c:
        c.execute("UPDATE ingest_jobs SET status='done', result=%s, error=NULL, updated_at=%s "
                  "WHERE tenant_id=%s AND id=%s", (json.dumps(result), time.time(), tenant_id, job_id))


def fail_job(tenant_id: str, job_id: str, error: str, attempts: int) -> None:
    status = "queued" if attempts < _MAX_JOB_ATTEMPTS else "error"
    with _conn() as c:
        c.execute("UPDATE ingest_jobs SET status=%s, error=%s, updated_at=%s WHERE tenant_id=%s AND id=%s",
                  (status, (error or "")[:500], time.time(), tenant_id, job_id))


def get_job(tenant_id: str, job_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT id,kind,status,result,error,attempts,created_at,updated_at "
                      "FROM ingest_jobs WHERE tenant_id=%s AND id=%s", (tenant_id, job_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d


def list_jobs(tenant_id: str, limit: int = 50) -> list[dict]:
    """Tenant'ın son ingest işleri (Batches yüzeyi) — en yeni önce."""
    limit = max(1, min(int(limit), 200))
    with _conn() as c:
        rows = c.execute(
            "SELECT id,kind,status,result,error,attempts,created_at,updated_at "
            "FROM ingest_jobs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["result"] = json.loads(d["result"]) if d.get("result") else None
            out.append(d)
        return out


def count_jobs(status: str | None = None) -> int:
    q = "SELECT COUNT(*) AS n FROM ingest_jobs"
    args: list = []
    if status:
        q += " WHERE status=%s"
        args.append(status)
    with _conn() as c:
        return int(c.execute(q, args).fetchone()["n"])


def reclaim_stale_jobs(lease_seconds: float = 300.0) -> int:
    """Worker crash kurtarma: lease_seconds'tan eski 'running' işleri yeniden kuyruğa al
    (attempts>=MAX ise 'error' -> dead-letter). İdempotent ingest sayesinde güvenli. CROSS-TENANT (owner)."""
    cutoff = time.time() - lease_seconds
    with _owner_conn() as c:
        cur = c.execute(
            "UPDATE ingest_jobs SET status=CASE WHEN attempts>=%s THEN 'error' ELSE 'queued' END, "
            "updated_at=%s WHERE status='running' AND updated_at < %s",
            (_MAX_JOB_ATTEMPTS, time.time(), cutoff))
        return cur.rowcount


def seed_from_yaml(yaml_path: str, tenant_id: str) -> int:
    import yaml
    from pathlib import Path
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    n = 0
    for r in data.get("rules", []):
        r = dict(r)
        r["tenant_id"] = tenant_id
        add_rule(r)
        n += 1
    return n
