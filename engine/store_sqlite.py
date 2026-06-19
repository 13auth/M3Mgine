#!/usr/bin/env python3
"""store.py — Multi-tenant kalıcı depo (SQLite, sıfır harici bağımlılık).

Ürünün durum katmanı. Her satır tenant_id ile etiketli (izolasyon). Rule dict'i
sistemin tek temsili: enforce, compiler ve API hep aynı şekli kullanır.

Rule dict şekli:
  {id, tenant_id, project, type('hard'|'soft'), severity, source, message,
   rule(soft için niyet metni), forbid_patterns[list], require_patterns[list],
   status('active'|'disabled'), created_at}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import crypto  # at-rest alan şifrelemesi (anahtar yoksa no-op)

DB_PATH = Path(os.environ.get("CCE_DB", Path(__file__).parent / "data" / "cce.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name      TEXT,
    api_key   TEXT UNIQUE,
    plan      TEXT DEFAULT 'free',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
    tenant_id TEXT,
    period    TEXT,          -- 'YYYY-MM'
    ops       REAL DEFAULT 0,
    PRIMARY KEY (tenant_id, period)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT,
    tenant_id  TEXT,
    key_hash   TEXT UNIQUE,  -- düz key saklanmaz; sadece hash
    name       TEXT,
    role       TEXT DEFAULT 'member',
    created_at REAL,
    revoked_at REAL,         -- NULL = aktif
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_apikeys_hash ON api_keys(key_hash);
CREATE TABLE IF NOT EXISTS users (
    email          TEXT PRIMARY KEY,   -- normalize (lower) edilmiş
    tenant_id      TEXT,
    password_hash  TEXT,               -- pbkdf2; düz şifre saklanmaz
    email_verified REAL,               -- NULL = doğrulanmadı; ts = doğrulandı
    approved       REAL,               -- NULL = onay bekliyor; ts = admin onayladı
    created_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,   -- sha256(raw); düz token sadece email linkinde
    tenant_id  TEXT,
    email      TEXT,
    purpose    TEXT,               -- 'verify' | 'reset'
    expires_at REAL,
    used_at    REAL                -- NULL = kullanılmadı
);
CREATE INDEX IF NOT EXISTS idx_authtok_email ON auth_tokens(email);
CREATE TABLE IF NOT EXISTS subscriptions (
    provider_ref TEXT PRIMARY KEY,   -- sağlayıcı subscription/customer id (BİZ checkout'ta eşledik)
    tenant_id    TEXT,
    created_at   REAL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id   TEXT PRIMARY KEY,     -- idempotency / replay engeli
    created_at REAL
);
CREATE TABLE IF NOT EXISTS webhooks (
    id          TEXT,
    tenant_id   TEXT,
    url         TEXT,
    secret_hash TEXT,                -- imza secret'ı SHA-256 (düz değer sadece üretimde döner)
    events      TEXT,                -- CSV: memory.created,kg.updated,...
    active      INTEGER DEFAULT 1,
    last_status TEXT,                -- son teslimat: '200' | 'err: ...'
    last_at     REAL,
    created_at  REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS rules (
    id        TEXT,
    tenant_id TEXT,
    project   TEXT,
    type      TEXT,
    severity  TEXT,
    source    TEXT,
    correction_id TEXT,
    message   TEXT,
    rule      TEXT,
    forbid_patterns TEXT,
    require_patterns TEXT,
    status    TEXT DEFAULT 'active',
    version   INTEGER DEFAULT 1,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS corrections (
    id        TEXT,
    tenant_id TEXT,
    project   TEXT,
    category  TEXT,
    what_was_wrong TEXT,
    correct_behavior TEXT,
    reusable_rule TEXT,
    severity  TEXT,
    tags      TEXT,
    created_at REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS violations (
    tenant_id TEXT,
    rule_id   TEXT,
    bucket    TEXT,
    trace     TEXT,
    evidence  TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS checks (
    tenant_id TEXT,
    rule_id   TEXT,
    passed    INTEGER,
    source    TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS admin_log (
    tenant_id TEXT,
    action    TEXT,
    target    TEXT,
    detail    TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS rule_history (
    tenant_id TEXT,
    rule_id   TEXT,
    version   INTEGER,
    snapshot  TEXT,
    change    TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS memories (
    id        TEXT,
    tenant_id TEXT,
    user_id   TEXT,
    content   TEXT,
    embedding TEXT,          -- JSON float[] (v0; pgvector/Qdrant ölçekte)
    source    TEXT,
    metadata  TEXT,          -- JSON
    salience  REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    valid_at  REAL,
    invalid_at REAL,         -- NULL = hâlâ geçerli (soft-delete; ASLA hard-delete)
    last_accessed_at REAL,
    taxonomy_node_id TEXT,   -- AACRP: routing edildiği kategori düğümü
    taxonomy_path TEXT,      -- okunabilir kategori yolu (ör. "Acme/iOS")
    auto_tags TEXT,          -- JSON: otomatik etiketler
    routing_confidence REAL, -- routing güven skoru
    subject_party TEXT,      -- AACRP attribution: self|third_party|unknown (KVKK)
    pii_flags TEXT,          -- JSON: tespit edilen PII türleri (email/telefon/tckn/iban)
    term_hashes TEXT,        -- JSON str[]: blind lexical index (iki-yönlü hibrit aday; bkz. _lex_index)
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_mem_tenant_user ON memories(tenant_id, user_id);
CREATE TABLE IF NOT EXISTS taxonomy_nodes (
    tenant_id  TEXT,
    id         TEXT,
    parent_id  TEXT,
    slug       TEXT,
    label      TEXT,
    path       TEXT,
    centroid   TEXT,         -- JSON float[] (küme merkezi; opsiyonel)
    member_count INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'active',
    version    INTEGER DEFAULT 1,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS tombstones (
    tenant_id    TEXT,
    content_hash TEXT,     -- sha1(tenant|normalize(content)); tek-yönlü, user'dan bağımsız
    embedding    TEXT,     -- forget edilen içeriğin vektörü (paraphrase'i bloklamak için)
    created_at   REAL,
    PRIMARY KEY (tenant_id, content_hash)
);
CREATE TABLE IF NOT EXISTS handoffs (
    tenant_id  TEXT,
    session_id TEXT,
    summary    TEXT,          -- "kaldığımız yer" özeti (LLM varsa)
    fact_ids   TEXT,          -- JSON: bu oturumdan kalıcılaşan memory id'leri
    metadata   TEXT,          -- JSON
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (tenant_id, session_id)
);
-- Temporal knowledge graph (Graphiti-eşdeğeri çekirdek): düğümler + bi-temporal kenarlar.
CREATE TABLE IF NOT EXISTS kg_entities (
    id         TEXT,
    tenant_id  TEXT,
    name       TEXT,          -- gösterim adı (canonical)
    norm       TEXT,          -- fold'lanmış ad (entity resolution)
    type       TEXT,          -- person/org/place/concept/...
    attributes TEXT,          -- JSON
    embedding  TEXT,          -- JSON float[]
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_kgent_tenant_norm ON kg_entities(tenant_id, norm);
CREATE TABLE IF NOT EXISTS kg_edges (
    id           TEXT,
    tenant_id    TEXT,
    subject_id   TEXT,
    predicate    TEXT,         -- normalize edilmiş ilişki anahtarı
    object_id    TEXT,         -- entity id (literal ise NULL)
    object_value TEXT,         -- literal değer (entity ise NULL)
    fact         TEXT,         -- doğal dil ifade
    embedding    TEXT,         -- JSON float[] (fact)
    valid_at     REAL,         -- DÜNYA: fact ne zaman doğru oldu
    invalid_at   REAL,         -- DÜNYA: ne zaman doğru olmaktan çıktı (NULL = hâlâ doğru)
    created_at   REAL,         -- SİSTEM: ne zaman öğrenildi
    expired_at   REAL,         -- SİSTEM: ne zaman geçersizlendi/süperse (NULL = güncel bilgi)
    source       TEXT,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_kgedge_tenant_subj ON kg_edges(tenant_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_kgedge_tenant_pred ON kg_edges(tenant_id, subject_id, predicate);
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id         TEXT,
    tenant_id  TEXT,
    kind       TEXT,          -- 'memory' | 'kg'
    payload    TEXT,          -- JSON girdi
    status     TEXT DEFAULT 'queued',  -- queued | running | done | error
    result     TEXT,          -- JSON sonuç
    error      TEXT,
    attempts   INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON ingest_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_checks_tenant_rule ON checks(tenant_id, rule_id);
CREATE INDEX IF NOT EXISTS idx_violations_tenant ON violations(tenant_id);
"""

# eski DB'lere eksik kolonları ekle (yeni DB'de _SCHEMA zaten içeriyor)
_MIGRATIONS = [
    "ALTER TABLE rules ADD COLUMN correction_id TEXT",
    "ALTER TABLE rules ADD COLUMN version INTEGER DEFAULT 1",
    "ALTER TABLE rules ADD COLUMN updated_at REAL",
    "ALTER TABLE tenants ADD COLUMN plan TEXT DEFAULT 'free'",
    "ALTER TABLE memories ADD COLUMN term_hashes TEXT",   # iki-yönlü hibrit aday (blind lexical index)
]

_LIST_FIELDS = ("forbid_patterns", "require_patterns")


_SCHEMA_DONE: set[str] = set()  # DB yolu başına bir kez init (#11) — ama path-aware


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: ThreadingHTTPServer altında eşzamanlı yazımlarda 'database is locked'
    # hatasını azaltmak için bekle; WAL ile okuma/yazma çakışmasını düşür.
    c = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c.row_factory = sqlite3.Row
    c.create_function("trfold", 1, _fold)  # Türkçe-güvenli fold SQL'de
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA wal_autocheckpoint=1000")  # WAL sınırlı büyüsün (#23)
    key = str(DB_PATH)
    if key not in _SCHEMA_DONE:  # schema+migration bu DB için bir kez (her bağlantıda değil)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                c.execute(mig)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise  # 'database is locked' vb. gerçek hatayı yutma
        _SCHEMA_DONE.add(key)  # sadece schema+migration başarıyla bitince işaretle
    return c


def _hash_key(api_key: str) -> str:
    """API key'i düz saklama; at-rest hash. Sızıntıda key geri çıkmaz."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _fold(s) -> str:
    """Türkçe-güvenli case fold: İ/I/ı hepsini 'i'ye indir, sonra casefold. SQL lower()
    ve Python .lower() Türkçe-duyarsız olduğundan project eşleşmesi bunu kullanır."""
    s = "" if s is None else str(s)
    return s.replace("İ", "i").replace("I", "i").replace("ı", "i").strip().casefold()


_TOK = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", re.UNICODE)   # memory._TOK ile aynı


def _lex_index(text: str) -> list[str]:
    """Blind lexical index: düz-metinden keyed-hash term kökleri (iki-yönlü hibrit aday).
    Kök = 4-char prefix (len>=4) — memory._term_match çekim-eki davranışının SÜPERSET'i.
    pg ile birebir aynı (aynı fold + prefix + crypto.blind); aynı fonksiyon yazım+sorgu -> tutarlı."""
    roots = set()
    for t in _TOK.findall(text or ""):
        f = _fold(t)
        if len(f) < 3:
            continue
        roots.add(f[:4] if len(f) >= 4 else f)
    return [crypto.blind(r) for r in roots]


def _row_to_rule(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in _LIST_FIELDS:
        d[f] = json.loads(d.get(f) or "[]")
    return d


# ---------------- tenants ----------------
def upsert_tenant(tenant_id: str, name: str, api_key: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO tenants(tenant_id,name,api_key,created_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(tenant_id) DO UPDATE SET name=excluded.name, api_key=excluded.api_key",
                  (tenant_id, name, _hash_key(api_key), time.time()))


def tenant_by_key(api_key: str) -> str | None:
    """Önce çoklu-key tablosu (aktif), sonra bootstrap owner key (tenants.api_key)."""
    if not api_key:
        return None
    h = _hash_key(api_key)
    with _conn() as c:
        r = c.execute("SELECT tenant_id FROM api_keys WHERE key_hash=? AND revoked_at IS NULL",
                      (h,)).fetchone()
        if r:
            return r["tenant_id"]
        r = c.execute("SELECT tenant_id FROM tenants WHERE api_key=?", (h,)).fetchone()
        return r["tenant_id"] if r else None


def add_api_key(tenant_id: str, key_hash: str, name: str, role: str = "member") -> str:
    import uuid
    kid = "key-" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO api_keys(id,tenant_id,key_hash,name,role,created_at,revoked_at) "
                  "VALUES(?,?,?,?,?,?,NULL)", (kid, tenant_id, key_hash, name, role, time.time()))
    return kid


def revoke_api_key(tenant_id: str, key_id: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE api_keys SET revoked_at=? WHERE tenant_id=? AND id=? AND revoked_at IS NULL",
                        (time.time(), tenant_id, key_id))
        return cur.rowcount


def list_api_keys(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id,name,role,created_at,revoked_at FROM api_keys WHERE tenant_id=? "
                         "ORDER BY created_at DESC", (tenant_id,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "role": r["role"],
                 "created_at": r["created_at"], "revoked": r["revoked_at"] is not None} for r in rows]


def add_webhook(tenant_id: str, url: str, secret_hash: str, events: str) -> str:
    import uuid
    wid = "wh-" + uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO webhooks(id,tenant_id,url,secret_hash,events,active,created_at) "
                  "VALUES(?,?,?,?,?,1,?)", (wid, tenant_id, url, secret_hash, events, time.time()))
    return wid


def list_webhooks(tenant_id: str, active_only: bool = False) -> list[dict]:
    q = "SELECT id,url,events,active,last_status,last_at,created_at,secret_hash FROM webhooks WHERE tenant_id=?"
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
        cur = c.execute("DELETE FROM webhooks WHERE tenant_id=? AND id=?", (tenant_id, webhook_id))
        return cur.rowcount


def touch_webhook(tenant_id: str, webhook_id: str, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE webhooks SET last_status=?, last_at=? WHERE tenant_id=? AND id=?",
                  (status[:120], time.time(), tenant_id, webhook_id))


def role_by_key(api_key: str) -> str | None:
    """Çoklu-key tablosundaki rol; bootstrap owner key -> 'owner'."""
    if not api_key:
        return None
    h = _hash_key(api_key)
    with _conn() as c:
        r = c.execute("SELECT role FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (h,)).fetchone()
        if r:
            return r["role"]
        r = c.execute("SELECT 1 FROM tenants WHERE api_key=?", (h,)).fetchone()
        return "owner" if r else None


# ---------------- users (insan email/şifre auth katmanı) ----------------
def create_user(email: str, tenant_id: str, password_hash: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO users(email,tenant_id,password_hash,created_at) VALUES(?,?,?,?)",
                  (email, tenant_id, password_hash, time.time()))


def get_user_by_email(email: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT email,tenant_id,password_hash,email_verified,approved,created_at "
                      "FROM users WHERE email=?", (email,)).fetchone()
        return dict(r) if r else None


def get_user_by_tenant(tenant_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT email,tenant_id,password_hash,email_verified,approved,created_at "
                      "FROM users WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone()
        return dict(r) if r else None


def list_pending_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT email,tenant_id,email_verified,created_at FROM users "
                         "WHERE approved IS NULL ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def set_user_approved(email: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET approved=? WHERE email=?", (time.time(), email)).rowcount


def update_password(email: str, password_hash: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET password_hash=? WHERE email=?",
                         (password_hash, email)).rowcount


def mark_email_verified(email: str) -> int:
    with _conn() as c:
        return c.execute("UPDATE users SET email_verified=? WHERE email=?",
                         (time.time(), email)).rowcount


def create_auth_token(token_hash: str, tenant_id: str, email: str, purpose: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO auth_tokens(token_hash,tenant_id,email,purpose,expires_at,used_at) "
                  "VALUES(?,?,?,?,?,NULL)", (token_hash, tenant_id, email, purpose, expires_at))


def consume_auth_token(token_hash: str, purpose: str) -> dict | None:
    """Atomik: geçerli (kullanılmamış + süresi dolmamış + doğru amaç) ise kullanılmış işaretle ve döndür."""
    now = time.time()
    with _conn() as c:
        cur = c.execute("UPDATE auth_tokens SET used_at=? WHERE token_hash=? AND purpose=? "
                        "AND used_at IS NULL AND expires_at>?", (now, token_hash, purpose, now))
        if cur.rowcount != 1:
            return None
        r = c.execute("SELECT tenant_id,email,purpose FROM auth_tokens WHERE token_hash=?",
                      (token_hash,)).fetchone()
        return dict(r) if r else None


# ---------------- plan / usage (abonelik + metering) ----------------
_VALID_PLANS = {"free", "solo", "starter", "growth", "pro", "enterprise"}  # billing.PLANS ile senkron


def set_plan(tenant_id: str, plan: str) -> None:
    if plan not in _VALID_PLANS:  # write-boundary invariant (audit defense-in-depth)
        raise ValueError(f"unknown plan: {plan}")
    with _conn() as c:
        c.execute("UPDATE tenants SET plan=? WHERE tenant_id=?", (plan, tenant_id))


def link_subscription(tenant_id: str, provider_ref: str) -> None:
    """Checkout'ta sağlayıcı subscription/customer ref'ini tenant'a BİZ eşleriz.
    Webhook tenant'ı buradan çözer (body'deki tenant_id'ye GÜVENMEZ — IDOR önler)."""
    with _conn() as c:
        c.execute("INSERT INTO subscriptions(provider_ref,tenant_id,created_at) VALUES(?,?,?) "
                  "ON CONFLICT(provider_ref) DO UPDATE SET tenant_id=excluded.tenant_id",
                  (provider_ref, tenant_id, time.time()))


def tenant_by_subscription(provider_ref: str) -> str | None:
    if not provider_ref:
        return None
    with _conn() as c:
        r = c.execute("SELECT tenant_id FROM subscriptions WHERE provider_ref=?",
                      (provider_ref,)).fetchone()
        return r["tenant_id"] if r else None


def seen_event(event_id: str) -> bool:
    """Replay/idempotency: True = bu event daha önce işlendi. Atomik INSERT OR IGNORE."""
    if not event_id:
        return False
    with _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO webhook_events(event_id,created_at) VALUES(?,?)",
                        (event_id, time.time()))
        return cur.rowcount == 0


def get_plan(tenant_id: str) -> str:
    with _conn() as c:
        r = c.execute("SELECT plan FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        return r["plan"] if (r and r["plan"]) else "free"


def add_usage(tenant_id: str, period: str, ops: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO usage(tenant_id,period,ops) VALUES(?,?,?) "
                  "ON CONFLICT(tenant_id,period) DO UPDATE SET ops=usage.ops+excluded.ops",
                  (tenant_id, period, ops))


def get_usage(tenant_id: str, period: str) -> float:
    with _conn() as c:
        r = c.execute("SELECT ops FROM usage WHERE tenant_id=? AND period=?",
                      (tenant_id, period)).fetchone()
        return float(r["ops"]) if r else 0.0


def try_consume(tenant_id: str, period: str, cost: float, limit) -> bool:
    """ATOMİK kota rezervasyonu (atomik kota işlemi): BEGIN IMMEDIATE ile yazıcılar serileşir,
    used+cost<=limit ise ops+=cost ve True; aksi halde dokunma ve False. limit None -> sınırsız.
    Check-then-act yarışını ve 'son op her zaman geçer' açığını kapatır."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT ops FROM usage WHERE tenant_id=? AND period=?",
                        (tenant_id, period)).fetchone()
        used = float(row["ops"]) if row else 0.0
        if limit is not None and used + cost > limit:
            return False  # rollback (with-exit commit'ler ama yazma yapmadık)
        c.execute("INSERT INTO usage(tenant_id,period,ops) VALUES(?,?,?) "
                  "ON CONFLICT(tenant_id,period) DO UPDATE SET ops=usage.ops+excluded.ops",
                  (tenant_id, period, cost))
        return True


# ---------------- rules ----------------
def add_rule(rule: dict) -> None:
    r = dict(rule)
    r.setdefault("status", "active")
    now = time.time()
    r.setdefault("created_at", now)
    # type'ı canonical'e indir; tanınmayan -> 'soft' (store asla non-canonical type tutmasın, r4 CRITICAL)
    _t = str(r.get("type") or "soft").strip().lower()
    r["type"] = _t if _t in ("hard", "soft") else "soft"
    # status'u da canonical'e indir (aynı enum-baypas sınıfı: non-canonical status get_rules'tan
    # düşer -> kural sessizce enforce edilmez). tanınmayan -> 'active'.
    _s = str(r.get("status") or "active").strip().lower()
    r["status"] = _s if _s in ("active", "disabled") else "active"
    # severity'i de store'da canonicalize (YAML seed compiler'ı baypas eder).
    # tanınmayan -> 'high' (fail-closed) ki monotonik CASE'in ELSE'ine düşüp Critical kaçmasın.
    _sv = str(r.get("severity") or "medium").strip().lower()
    r["severity"] = _sv if _sv in ("low", "medium", "high", "critical") else "high"
    # project: boş/whitespace -> None (case korunur; karşılaştırma case-insensitive yapılır)
    _p = r.get("project")
    r["project"] = _p.strip() if (isinstance(_p, str) and _p.strip()) else None
    # deterministik pattern içeren kural SOFT olamaz (yoksa pattern ölü kalır)
    if (r.get("forbid_patterns") or r.get("require_patterns")) and r["type"] != "hard":
        r["type"] = "hard"
    # ATOMİK: pre-SELECT yok (lost-update race #9 kapandı). Pattern birleştirme,
    # version++, type-koru ve disabled-yapışkan tamamı tek SQL DO UPDATE içinde.
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # rollback/diff için önceki sürümü arşivle (varsa) — UPSERT mutasyonundan ÖNCE (#8)
        c.execute(
            "INSERT INTO rule_history(tenant_id,rule_id,version,snapshot,change,created_at) "
            "SELECT tenant_id,id,version,json_object('type',type,'severity',severity,'project',project,"
            "'message',message,'rule',rule,'forbid_patterns',forbid_patterns,"
            "'require_patterns',require_patterns,'status',status),'update',? "
            "FROM rules WHERE tenant_id=? AND id=?",
            (now, r["tenant_id"], r["id"]))
        c.execute(
            "INSERT INTO rules(id,tenant_id,project,type,severity,source,correction_id,message,rule,"
            "forbid_patterns,require_patterns,status,version,created_at,updated_at) "
            "VALUES(:id,:tenant_id,:project,:type,:severity,:source,:correction_id,:message,:rule,"
            ":forbid_patterns,:require_patterns,:status,1,:created_at,:updated_at) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET project=excluded.project,"
            # type ASLA hard->soft düşmesin (id zaten type içerir; bu defansif) (#1/#5)
            "type=CASE WHEN rules.type='hard' THEN 'hard' ELSE excluded.type END,"
            # severity ASLA otomatik aşağı düşmez: daha yükseğini koru (monotonik)
            "severity=CASE WHEN "
            "(CASE rules.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 3 END)"
            ">=(CASE excluded.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 3 END)"
            " THEN rules.severity ELSE excluded.severity END,"
            "source=excluded.source,correction_id=excluded.correction_id,"
            "message=excluded.message,rule=excluded.rule,"
            # forbid/require patternleri SQL içinde atomik UNION; bozuk/boş JSON'a karşı json_valid guard
            "forbid_patterns=(SELECT json_group_array(v) FROM (SELECT value v FROM json_each(CASE WHEN json_valid(rules.forbid_patterns) THEN rules.forbid_patterns ELSE '[]' END) "
            "UNION SELECT value v FROM json_each(CASE WHEN json_valid(excluded.forbid_patterns) THEN excluded.forbid_patterns ELSE '[]' END))),"
            "require_patterns=(SELECT json_group_array(v) FROM (SELECT value v FROM json_each(CASE WHEN json_valid(rules.require_patterns) THEN rules.require_patterns ELSE '[]' END) "
            "UNION SELECT value v FROM json_each(CASE WHEN json_valid(excluded.require_patterns) THEN excluded.require_patterns ELSE '[]' END))),"
            # disabled YAPIŞKAN: yeni ingest devre-dışı kuralı sessizce reaktive etmesin (#6/#10)
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
    q = "SELECT * FROM rules WHERE tenant_id=?"
    args: list = [tenant_id]
    if active_only:
        q += " AND status='active'"
    if project:
        # Türkçe-güvenli case-insensitive proje eşleşmesi (İ/ı dahil)
        q += " AND (trfold(project)=trfold(?) OR project IS NULL)"
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
            "FROM rules WHERE tenant_id=? AND status='active' "
            "GROUP BY COALESCE(project,'') ORDER BY rules DESC",
            (tenant_id,)).fetchall()
        return [{"project": r["project"], "rules": r["rules"], "hard": r["hard"] or 0,
                 "soft": r["rules"] - (r["hard"] or 0), "updated_at": r["updated_at"]} for r in rows]


def rule_exists_like(tenant_id: str, project: str | None, message: str,
                     rtype: str | None = None) -> str | None:
    """Aynı tenant+project+TYPE'ta normalize edilmiş aynı mesajlı AKTİF kural var mı?
    TYPE-aware: hard ve soft aynı mesajla AYNI kural DEĞİL (audit critical #1 — hard'ı
    soft'a çevirmeyi önler). active_only: disabled kuralı dedup'a alıp reaktive etme (#6/#10)."""
    norm = " ".join((message or "").lower().split())
    if not norm:
        return None
    pkey = _fold(project)  # Türkçe-güvenli proje fold
    for r in get_rules(tenant_id, project=None, active_only=True):
        if (_fold(r.get("project")) == pkey
                and (rtype is None or r.get("type") == rtype)
                and " ".join((r.get("message") or "").lower().split()) == norm):
            return r["id"]
    return None


def record_admin(tenant_id: str, action: str, target: str = "", detail: str = "") -> None:
    """Hesap verebilirlik: admin işlemleri (sil/devre dışı/purge). PII YAZMA — sadece metadata.
    purge'de bile hayatta kalır (admin_log purge edilmez)."""
    with _conn() as c:
        c.execute("INSERT INTO admin_log(tenant_id,action,target,detail,created_at) VALUES(?,?,?,?,?)",
                  (tenant_id, action, target, detail, time.time()))


def get_admin_log(tenant_id: str, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT action,target,detail,created_at FROM admin_log "
                         "WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                         (tenant_id, max(1, min(limit, 1000)))).fetchall()
        return [dict(r) for r in rows]


def disable_rule(tenant_id: str, rule_id: str) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE rules SET status='disabled' WHERE tenant_id=? AND id=?",
                        (tenant_id, rule_id))
        n = cur.rowcount
    if n:
        record_admin(tenant_id, "disable_rule", rule_id)
    return n


def delete_rule(tenant_id: str, rule_id: str) -> int:
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(  # silmeden önce tombstone snapshot (#8)
            "INSERT INTO rule_history(tenant_id,rule_id,version,snapshot,change,created_at) "
            "SELECT tenant_id,id,version,json_object('type',type,'severity',severity,'project',project,"
            "'message',message,'rule',rule,'forbid_patterns',forbid_patterns,"
            "'require_patterns',require_patterns,'status',status),'delete',? "
            "FROM rules WHERE tenant_id=? AND id=?",
            (time.time(), tenant_id, rule_id))
        cur = c.execute("DELETE FROM rules WHERE tenant_id=? AND id=?", (tenant_id, rule_id))
        n = cur.rowcount
    if n:
        record_admin(tenant_id, "delete_rule", rule_id)
    return n


def get_rule_history(tenant_id: str, rule_id: str, limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT version,change,snapshot,created_at FROM rule_history "
                         "WHERE tenant_id=? AND rule_id=? ORDER BY created_at DESC LIMIT ?",
                         (tenant_id, rule_id, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]


def purge_tenant(tenant_id: str) -> dict:
    """KVKK Art.7 / GDPR Art.17 — tenant'ın TÜM verisini sil (right to erasure).
    admin_log purge EDİLMEZ: silme işleminin kendisi hesap verebilirlik için kalır (PII içermez)."""
    counts = {}
    with _conn() as c:
        for tbl in ("rules", "rule_history", "corrections", "violations", "checks", "memories",
                    "tombstones", "handoffs", "kg_entities", "kg_edges", "taxonomy_nodes", "ingest_jobs",
                    "usage", "webhooks", "api_keys", "users", "auth_tokens", "subscriptions", "tenants"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=?", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "purge_tenant", "", detail=json.dumps(counts))
    return counts


def clear_knowledge(tenant_id: str) -> dict:
    """import --mode replace için: tenant'ın BİLGİ'sini sil (memory/rules/corrections/
    telemetri/handoff). api_keys/tenants/subscriptions/usage'a DOKUNMAZ — aksi halde import
    sırasında çağıranın kendi key'i ve hesabı silinir (lockout).
    tombstones'a da DOKUNMAZ: erasure (KVKK Art.17) YAPIŞKANDIR — replace ile bile forget
    edilen içerik dirilmemeli (audit CRITICAL). Tam silme purge_tenant'ta (hesap kapanışı)."""
    counts = {}
    with _conn() as c:
        for tbl in ("rules", "rule_history", "corrections", "violations", "checks",
                    "memories", "handoffs", "kg_entities", "kg_edges", "taxonomy_nodes"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=?", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "clear_knowledge", "", detail=json.dumps(counts))
    return counts


def clear_imported(tenant_id: str) -> dict:
    """Vault re-import için: tenant'ın HAFIZA + KNOWLEDGE GRAPH'ını sil; kurallar/correction/
    hesap/tombstone'a DOKUNMA (enforce politikan ve erasure kalıcılığı korunur)."""
    counts = {}
    with _conn() as c:
        for tbl in ("memories", "kg_entities", "kg_edges", "taxonomy_nodes"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE tenant_id=?", (tenant_id,))
            counts[tbl] = cur.rowcount
    record_admin(tenant_id, "clear_imported", "", detail=json.dumps(counts))
    return counts


def checkpoint() -> None:
    """WAL'ı ana dosyaya yaz ve WAL/SHM'i küçült (temiz kapanış, #23)."""
    try:
        with _conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def prune_telemetry(days: int = 90) -> dict:
    """Retention: eski checks/violations'ı sil (sınırsız büyümeyi önle)."""
    cutoff = time.time() - days * 86400
    out = {}
    with _conn() as c:
        for tbl in ("checks", "violations"):
            cur = c.execute(f"DELETE FROM {tbl} WHERE created_at < ?", (cutoff,))
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
            "VALUES(:id,:tenant_id,:project,:category,:what_was_wrong,:correct_behavior,"
            ":reusable_rule,:severity,:tags,:created_at) "
            "ON CONFLICT(tenant_id,id) DO NOTHING",
            {
                "id": c0["id"], "tenant_id": c0["tenant_id"], "project": c0.get("project"),
                "category": c0.get("category", ""), "what_was_wrong": c0.get("what_was_wrong", ""),
                "correct_behavior": c0.get("correct_behavior", ""),
                "reusable_rule": c0.get("reusable_rule", ""), "severity": c0.get("severity", "medium"),
                "tags": json.dumps(c0.get("tags", [])), "created_at": c0["created_at"],
            })


# ---------------- violations / measurement ----------------
def record_violation(tenant_id: str, rule_id: str, bucket: str, trace: str, evidence: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO violations(tenant_id,rule_id,bucket,trace,evidence,created_at) "
                  "VALUES(?,?,?,?,?,?)", (tenant_id, rule_id, bucket, trace, evidence, time.time()))


def record_check(tenant_id: str, rule_id: str, passed: bool, source: str = "runtime",
                 at: float | None = None) -> None:
    """Her kural değerlendirmesini (geçti/kaldı) yaz = DENOMINATOR. Oran hesabı için şart.
    'at' verilirse o zaman damgasıyla yazar (test/backfill); yoksa şimdi."""
    with _conn() as c:
        c.execute("INSERT INTO checks(tenant_id,rule_id,passed,source,created_at) VALUES(?,?,?,?,?)",
                  (tenant_id, rule_id, 1 if passed else 0, source,
                   at if at is not None else time.time()))


def record_enforcement(tenant_id: str, evaluated: list[dict], violations: list[dict],
                       source: str = "runtime", trace: str = "") -> None:
    """Runtime enforce sonucunu kalıcılaştır: her değerlendirilen kural için check +
    her ihlal için violation. 'per-rule MEASURE' artık canlı trafikte de var."""
    with _conn() as c:
        ts = time.time()
        for ev in evaluated:
            c.execute("INSERT INTO checks(tenant_id,rule_id,passed,source,created_at) VALUES(?,?,?,?,?)",
                      (tenant_id, ev["rule_id"], 1 if ev["passed"] else 0, source, ts))
        for v in violations:
            c.execute("INSERT INTO violations(tenant_id,rule_id,bucket,trace,evidence,created_at) "
                      "VALUES(?,?,?,?,?,?)", (tenant_id, v["rule_id"], source, trace,
                                              v.get("evidence", ""), ts))


def compliance_by_rule(tenant_id: str, since: float = 0.0) -> list[dict]:
    """Kural başına GERÇEK uyum oranı: checks tablosundan (passed/total). Numerator+denominator."""
    with _conn() as c:
        rows = c.execute(
            "SELECT rule_id, COUNT(*) checks, SUM(passed) passed, "
            "SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) violations "
            "FROM checks WHERE tenant_id=? AND created_at>=? GROUP BY rule_id",
            (tenant_id, since)).fetchall()
        out = []
        for r in rows:
            checks = r["checks"] or 0
            passed = r["passed"] or 0
            out.append({"rule_id": r["rule_id"], "checks": checks, "passed": passed,
                        "violations": r["violations"] or 0,
                        "compliance": (passed / checks) if checks else None})
        return out


def compliance_timeseries(tenant_id: str, days: int = 14, rule_id: str | None = None) -> list[dict]:
    """Günlük per-tenant (veya per-rule) uyum serisi. Araştırmanın 'moat' dediği
    longitudinal kural-bazlı uyum takibi: zamanla iyileşiyor mu / geriliyor mu."""
    cutoff = time.time() - days * 86400
    q = ("SELECT date(created_at,'unixepoch') d, COUNT(*) checks, "
         "SUM(passed) passed FROM checks WHERE tenant_id=? AND created_at>=?")
    args: list = [tenant_id, cutoff]
    if rule_id:
        q += " AND rule_id=?"
        args.append(rule_id)
    q += " GROUP BY d ORDER BY d"
    with _conn() as c:
        out = []
        for r in c.execute(q, args).fetchall():
            checks = r["checks"] or 0
            passed = r["passed"] or 0
            out.append({"date": r["d"], "checks": checks, "passed": passed,
                        "compliance": (passed / checks) if checks else None})
        return out


def rule_health(tenant_id: str, stale_days: int = 7) -> list[dict]:
    """Aktif kuralların SAĞLIK raporu. compliance_by_rule yalnız check'i OLAN kuralı görür;
    bu, rules ile checks'i LEFT JOIN'ler -> HİÇ tetiklenmemiş veya N gündür ATILMAMIŞ ('bayat')
    kuralları da yüzeye çıkarır. 'Bu kural hâlâ iş yapıyor mu / hâlâ geçerli mi?' içgörüsü =
    ölçülebilir uyumun denetim parçası. Sıra: önce bayat, sonra severity, sonra trafik."""
    now = time.time()
    cutoff = now - stale_days * 86400
    _rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    with _conn() as c:
        rows = c.execute(
            "SELECT r.id, r.type, r.severity, r.correction_id, "
            "COUNT(ch.rule_id) checks, COALESCE(SUM(ch.passed),0) passed, "
            "MAX(ch.created_at) last_fired "
            "FROM rules r LEFT JOIN checks ch "
            "ON ch.tenant_id=r.tenant_id AND ch.rule_id=r.id "
            "WHERE r.tenant_id=? AND r.status='active' "
            "GROUP BY r.id, r.type, r.severity, r.correction_id", (tenant_id,)).fetchall()
    out = []
    for r in rows:
        checks = r["checks"] or 0
        passed = r["passed"] or 0
        last_fired = r["last_fired"]
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


# ---------------------------------------------------------------------------
# MEMORIES — genel fact-memory (sadece kural değil, her bilgi). soft-delete.
# ---------------------------------------------------------------------------
def _safe_salience(v) -> float:
    """write-boundary invariant: salience daima sonlu ve [0,5]. NaN/Inf/çöp -> 1.0
    (import edilen bozuk salience ranking'i ve JSON'u bozmasın)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 1.0
    if f != f or f in (float("inf"), float("-inf")):  # NaN / +-Inf
        return 1.0
    return max(0.0, min(f, 5.0))


def _safe_embedding(emb):
    """write-boundary: geçersiz/sonsuz embedding (NaN/Inf, non-list, non-numeric) -> None.
    Postgres backend ile parite: ikisi de aynı saklanan durumu üretir."""
    if not isinstance(emb, (list, tuple)) or not emb:
        return None
    try:
        out = [float(x) for x in emb]
    except (TypeError, ValueError):
        return None
    for x in out:
        if x != x or x in (float("inf"), float("-inf")):
            return None
    return out


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
            "VALUES(:id,:tenant_id,:user_id,:content,:embedding,:source,:metadata,"
            ":salience,0,:created_at,:updated_at,:valid_at,NULL,NULL,:subject_party,:pii_flags,:term_hashes) "
            "ON CONFLICT(tenant_id,id) DO UPDATE SET content=excluded.content,"
            "embedding=excluded.embedding,source=excluded.source,metadata=excluded.metadata,"
            "subject_party=excluded.subject_party,pii_flags=excluded.pii_flags,"
            "term_hashes=excluded.term_hashes,"
            # tekrar görülen fact -> salience +0.5 (max 5), updated_at + valid_at(last-affirmed) tazele.
            # invalid_at'a DOKUNMA: forget edilmiş memory re-ingest'te DİRİLMESİN (audit CRITICAL/erasure).
            "salience=min(memories.salience+0.5, 5.0),updated_at=excluded.updated_at,valid_at=excluded.valid_at",
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
                "term_hashes": json.dumps(_lex_index(m.get("content", ""))),   # blind lexical index
            })


def _row_to_memory(row: sqlite3.Row) -> dict:
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
    """Tenant'ın taksonomisini DEĞİŞTİR (induce çıktısı). nodes: [{id,parent_id,slug,label,path,centroid,member_count}]"""
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM taxonomy_nodes WHERE tenant_id=?", (tenant_id,))
        c.executemany(
            "INSERT INTO taxonomy_nodes(tenant_id,id,parent_id,slug,label,path,centroid,member_count,"
            "status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'active',1,?,?)",
            [(tenant_id, n["id"], n.get("parent_id"), n.get("slug", ""), n.get("label", ""), n.get("path", ""),
              json.dumps(n["centroid"]) if n.get("centroid") else None, n.get("member_count", 0), now, now)
             for n in nodes])


def list_taxonomy(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT id,parent_id,slug,label,path,member_count,status FROM taxonomy_nodes "
                         "WHERE tenant_id=? AND status='active' ORDER BY member_count DESC, label", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]


def bulk_set_routing(tenant_id: str, rows: list[dict]) -> None:
    """Toplu routing yaz: rows [{id,node_id,path,tags,confidence}]."""
    if not rows:
        return
    with _conn() as c:
        c.executemany("UPDATE memories SET taxonomy_node_id=?, taxonomy_path=?, auto_tags=?, routing_confidence=? "
                      "WHERE tenant_id=? AND id=?",
                      [(r.get("node_id"), r.get("path"), json.dumps(r.get("tags") or []),
                        r.get("confidence"), tenant_id, r["id"]) for r in rows])


def get_active_memories(tenant_id: str, user_id: str | None = None, *, exclude_third: bool = False) -> list[dict]:
    q = "SELECT * FROM memories WHERE tenant_id=? AND invalid_at IS NULL"
    args: list = [tenant_id]
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    if exclude_third:   # self-context recall: 3.şahıs PII'si kullanıcının bağlamına SIZMAZ (KVKK)
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    with _conn() as c:
        return [_row_to_memory(r) for r in c.execute(q, args).fetchall()]


def get_memory(tenant_id: str, mem_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM memories WHERE tenant_id=? AND id=?",
                      (tenant_id, mem_id)).fetchone()
        return _row_to_memory(r) if r else None


def invalidate_memory(tenant_id: str, mem_id: str) -> int:
    """forget = CRYPTO-SHRED: invalid_at set + content/embedding NULL'la (KVKK/GDPR Art.17 —
    PII gerçekten silinsin, sadece flag değil). id tombstone olarak kalır (re-ingest engeli)."""
    with _conn() as c:
        cur = c.execute("UPDATE memories SET invalid_at=?, content='', embedding=NULL "
                        "WHERE tenant_id=? AND id=? AND invalid_at IS NULL",
                        (time.time(), tenant_id, mem_id))
        return cur.rowcount


def reinforce_memory(tenant_id: str, mem_id: str) -> int:
    """AKTİF bir memory'yi pekiştir: salience +0.5 (max 5), valid_at/updated_at tazele.
    Forget edilmiş satıra dokunmaz (WHERE invalid_at IS NULL)."""
    now = time.time()
    with _conn() as c:
        cur = c.execute("UPDATE memories SET salience=min(salience+0.5,5.0), updated_at=?, valid_at=? "
                        "WHERE tenant_id=? AND id=? AND invalid_at IS NULL", (now, now, tenant_id, mem_id))
        return cur.rowcount


def touch_memories(tenant_id: str, ids: list[str]) -> None:
    """retrieval sonrası: access_count++ ve last_accessed (reinforcement sinyali)."""
    if not ids:
        return
    now = time.time()
    with _conn() as c:
        c.executemany("UPDATE memories SET access_count=access_count+1, last_accessed_at=? "
                      "WHERE tenant_id=? AND id=?", [(now, tenant_id, i) for i in ids])


def add_tombstone(tenant_id: str, content_hash: str, embedding: list | None) -> None:
    """forget'te içerik-seviyesi mezar taşı: content_hash (user'dan bağımsız) + vektör.
    Re-ingest (exact veya paraphrase) bunu kontrol edip bloklar (erasure kalıcı)."""
    with _conn() as c:
        c.execute("INSERT INTO tombstones(tenant_id,content_hash,embedding,created_at) "
                  "VALUES(?,?,?,?) ON CONFLICT(tenant_id,content_hash) DO UPDATE SET "
                  "embedding=excluded.embedding",
                  (tenant_id, content_hash, json.dumps(embedding) if embedding is not None else None,
                   time.time()))


def get_tombstones(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT content_hash, embedding FROM tombstones WHERE tenant_id=?",
                         (tenant_id,)).fetchall()
        return [{"content_hash": r["content_hash"],
                 "embedding": json.loads(r["embedding"]) if r["embedding"] else None} for r in rows]


def count_memories(tenant_id: str, user_id: str | None = None, active_only: bool = True,
                   *, exclude_third: bool = False) -> int:
    q = "SELECT COUNT(*) n FROM memories WHERE tenant_id=?"
    args: list = [tenant_id]
    if active_only:
        q += " AND invalid_at IS NULL"
    if user_id is not None:
        q += " AND user_id=?"
        args.append(user_id)
    if exclude_third:
        q += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    with _conn() as c:
        return c.execute(q, args).fetchone()["n"]


def count_distinct_users(tenant_id: str) -> int:
    """Genel bakış: son-kullanıcı sayısı (tüm satırları materialize etmeden)."""
    with _conn() as c:
        return c.execute("SELECT COUNT(DISTINCT user_id) n FROM memories WHERE tenant_id=? AND invalid_at IS NULL",
                         (tenant_id,)).fetchone()["n"]


def count_kg(tenant_id: str) -> dict:
    """Genel bakış: knowledge graph boyutu (varlık + güncel kenar)."""
    now = time.time()
    with _conn() as c:
        ents = c.execute("SELECT COUNT(*) n FROM kg_entities WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]
        edges = c.execute("SELECT COUNT(*) n FROM kg_edges WHERE tenant_id=? AND expired_at IS NULL "
                          "AND valid_at<=? AND (invalid_at IS NULL OR invalid_at>?)",
                          (tenant_id, now, now)).fetchone()["n"]
    return {"entities": ents, "edges": edges}


def pii_summary(tenant_id: str) -> dict:
    """Genel bakış / KVKK: aktif kayıtların attribution dağılımı + PII'li kayıt sayısı."""
    out = {"self": 0, "third_party": 0, "unknown": 0, "with_pii": 0}
    with _conn() as c:
        for r in c.execute("SELECT COALESCE(subject_party,'self') sp, COUNT(*) n FROM memories "
                           "WHERE tenant_id=? AND invalid_at IS NULL GROUP BY COALESCE(subject_party,'self')",
                           (tenant_id,)).fetchall():
            out[r["sp"]] = out.get(r["sp"], 0) + r["n"]
        out["with_pii"] = c.execute("SELECT COUNT(*) n FROM memories WHERE tenant_id=? AND invalid_at IS NULL "
                                    "AND pii_flags IS NOT NULL AND pii_flags<>'[]' AND pii_flags<>''",
                                    (tenant_id,)).fetchone()["n"]
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
            "FROM memories WHERE tenant_id=? AND invalid_at IS NULL "
            "GROUP BY user_id ORDER BY n DESC, last_at DESC",
            (tenant_id,)).fetchall()
        return [{"user_id": r["user_id"], "memories": r["n"], "last_at": r["last_at"],
                 "first_at": r["first_at"], "salience": r["sal"] or 0.0} for r in rows]


# ---------------- export / portability ----------------
def get_corrections(tenant_id: str) -> list[dict]:
    """Tenant'ın correction kayıtları (export/audit). tags JSON parse edilir."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM corrections WHERE tenant_id=?", (tenant_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            out.append(d)
        return out


# ---------------- handoffs (session snapshot) ----------------
def add_handoff(tenant_id: str, session_id: str, summary: str,
                fact_ids: list | None = None, metadata: dict | None = None) -> None:
    """Oturum snapshot'ı (tenant başına session_id'ye göre upsert = en güncel hal)."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO handoffs(tenant_id,session_id,summary,fact_ids,metadata,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,session_id) DO UPDATE SET "
            "summary=excluded.summary,fact_ids=excluded.fact_ids,metadata=excluded.metadata,"
            "updated_at=excluded.updated_at",
            (tenant_id, session_id, summary or "", json.dumps(fact_ids or []),
             json.dumps(metadata or {}), now, now))


def get_handoff(tenant_id: str, session_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM handoffs WHERE tenant_id=? AND session_id=?",
                      (tenant_id, session_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["fact_ids"] = json.loads(d.get("fact_ids") or "[]")
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        return d


def list_handoffs(tenant_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT session_id,summary,updated_at FROM handoffs WHERE tenant_id=? "
                         "ORDER BY updated_at DESC", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------- knowledge graph (entities + bi-temporal edges) ----------------
def _row_to_entity(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    d["attributes"] = json.loads(d["attributes"]) if d.get("attributes") else {}
    return d


def add_entity(ent: dict) -> None:
    e = dict(ent)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    with _conn() as c:
        c.execute(
            "INSERT INTO kg_entities(id,tenant_id,name,norm,type,attributes,embedding,created_at,updated_at) "
            "VALUES(:id,:tenant_id,:name,:norm,:type,:attributes,:embedding,:created_at,:updated_at) "
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
        r = c.execute("SELECT * FROM kg_entities WHERE tenant_id=? AND id=?",
                      (tenant_id, ent_id)).fetchone()
        return _row_to_entity(r) if r else None


def get_entity_by_norm(tenant_id: str, norm: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM kg_entities WHERE tenant_id=? AND norm=? LIMIT 1",
                      (tenant_id, norm)).fetchone()
        return _row_to_entity(r) if r else None


def list_entities(tenant_id: str) -> list[dict]:
    with _conn() as c:
        return [_row_to_entity(r) for r in
                c.execute("SELECT * FROM kg_entities WHERE tenant_id=?", (tenant_id,)).fetchall()]


def _row_to_edge(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    d["fact"] = crypto.decrypt(d.get("fact"))
    return d


def add_edge(edge: dict) -> int:
    """Kenar ekle. id çakışırsa DO NOTHING (idempotent). Dönüş: yazılan satır (1) / çakışma (0)
    — çağıran 'added' sayacını GERÇEK yazıma göre tutsun (yalan added:1 önle)."""
    e = dict(edge)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,"
            "embedding,valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(:id,:tenant_id,:subject_id,:predicate,:object_id,:object_value,:fact,"
            ":embedding,:valid_at,:invalid_at,:created_at,:expired_at,:source) "
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
    """Toplu entity ekle (import hızlı yolu). Tek transaction + executemany."""
    if not rows:
        return
    now = time.time()
    with _conn() as c:
        c.executemany(
            "INSERT INTO kg_entities(id,tenant_id,name,norm,type,attributes,embedding,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'{}',NULL,?,?) ON CONFLICT(tenant_id,id) DO NOTHING",
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
               json.dumps(_lex_index(m.get("content", "")))) for m in rows]
    with _conn() as c:
        c.executemany(
            "INSERT INTO memories(id,tenant_id,user_id,content,embedding,source,metadata,salience,"
            "access_count,created_at,updated_at,valid_at,invalid_at,last_accessed_at,subject_party,pii_flags,"
            "term_hashes) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?,?,NULL,NULL,?,?,?) "
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
    with _conn() as c:
        c.executemany(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,embedding,"
            "valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(?,?,?,?,?,NULL,?,NULL,?,NULL,?,NULL,?) ON CONFLICT(tenant_id,id) DO NOTHING", params)


def invalidate_and_add_edge(tenant_id: str, bounds: list, new_edge: dict) -> dict:
    """ATOMİK çelişki-süperse: çelişen kenarları DÜNYA-bitişine çek + yeni kenarı ekle, TEK
    transaction (crash'te kısmi durum yok). bounds=[(edge_id, invalid_at)]. bound_edge güvenlik
    kapıları (ters-aralık yok, sadece daha-erken sınırla, retract'a dokunma) korunur."""
    e = dict(new_edge)
    now = time.time()
    emb = _safe_embedding(e.get("embedding"))
    invalidated = 0
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        for eid, inv in bounds:
            cur = c.execute("UPDATE kg_edges SET invalid_at=? WHERE tenant_id=? AND id=? "
                            "AND expired_at IS NULL AND valid_at<=? AND (invalid_at IS NULL OR invalid_at>?)",
                            (inv, tenant_id, eid, inv, inv))
            invalidated += cur.rowcount
        cur = c.execute(
            "INSERT INTO kg_edges(id,tenant_id,subject_id,predicate,object_id,object_value,fact,"
            "embedding,valid_at,invalid_at,created_at,expired_at,source) "
            "VALUES(:id,:tenant_id,:subject_id,:predicate,:object_id,:object_value,:fact,"
            ":embedding,:valid_at,:invalid_at,:created_at,:expired_at,:source) "
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
    """current_only=True: DÜNYA-zaman-duyarlı 'güncel' = sistemce geçerli (expired_at IS NULL) VE
    `now` anında dünyada doğru (valid_at<=now AND (invalid_at IS NULL OR invalid_at>now)).
    now=None -> şimdi. (audit C: 'güncel' artık valid_at'i yok saymıyor; gelecek-tarihli güncel sayılmaz.)"""
    q = "SELECT * FROM kg_edges WHERE tenant_id=?"
    args: list = [tenant_id]
    if subject_id is not None:
        q += " AND subject_id=?"
        args.append(subject_id)
    if predicate is not None:
        q += " AND predicate=?"
        args.append(predicate)
    if current_only:
        nw = now if now is not None else time.time()
        q += " AND expired_at IS NULL AND valid_at<=? AND (invalid_at IS NULL OR invalid_at>?)"
        args += [nw, nw]
    with _conn() as c:
        return [_row_to_edge(r) for r in c.execute(q, args).fetchall()]


def bound_edge(tenant_id: str, edge_id: str, invalid_at: float) -> int:
    """Çelişki/süperse: kenarın DÜNYA-bitişini `invalid_at`'e çek. Güvenlik kapıları:
      - valid_at<=invalid_at: TERS-aralık üretme (audit A).
      - (invalid_at IS NULL OR invalid_at>?): sadece DAHA ERKEN sınırla (mid-timeline insert),
        var olan bir bitişi ileri uzatma; tarih korunur.
      - expired_at IS NULL: retract edilmiş kenara dokunma."""
    with _conn() as c:
        cur = c.execute("UPDATE kg_edges SET invalid_at=? "
                        "WHERE tenant_id=? AND id=? AND expired_at IS NULL AND valid_at<=? "
                        "AND (invalid_at IS NULL OR invalid_at>?)",
                        (invalid_at, tenant_id, edge_id, invalid_at, invalid_at))
        return cur.rowcount


def expire_edge(tenant_id: str, edge_id: str, expired_at: float | None = None) -> int:
    """SİSTEM retraction (KVKK edge erasure / hatalı bilgi): kenarı tüm okumalardan düşür.
    invalid_at (dünya) değil expired_at (sistem) set eder; bound_edge'den ayrı."""
    with _conn() as c:
        cur = c.execute("UPDATE kg_edges SET expired_at=? WHERE tenant_id=? AND id=? AND expired_at IS NULL",
                        (expired_at if expired_at is not None else time.time(), tenant_id, edge_id))
        return cur.rowcount


def get_edges_asof(tenant_id: str, world_time: float) -> list[dict]:
    """Point-in-time (DÜNYA): world_time anında doğru olan, sistemce geçerli (retract edilmemiş)
    kenarlar. Yarı-açık aralık: valid_at<=T AND (invalid_at IS NULL OR invalid_at>T)."""
    with _conn() as c:
        return [_row_to_edge(r) for r in c.execute(
            "SELECT * FROM kg_edges WHERE tenant_id=? AND expired_at IS NULL AND valid_at<=? "
            "AND (invalid_at IS NULL OR invalid_at>?)",
            (tenant_id, world_time, world_time)).fetchall()]


def count_entities(tenant_id: str) -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM kg_entities WHERE tenant_id=?", (tenant_id,)).fetchone()["n"]


# ---------------- semantic candidate generators (push-down abstraction) ----------------
# memory/kg retrieval bu fonksiyonları çağırır; uygulama tarafı recency/salience/graph
# rerank'ı yapar. Davranış-koruma: aday sayısı (k) >= toplam ise HEPSİNİ döndür (mevcut
# tam-tarama ile BİREBİR aynı sonuç -> tüm audit testleri etkilenmez). Postgres backend
# büyük-N'de DB-tarafı pgvector sıralaması yapar; SQLite (dev) Python cosine ile sıralar.
def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return (dot / (na * nb)) if (na and nb) else 0.0


def vector_enabled() -> bool:
    """Parite: SQLite'ta native vektör push-down yok (candidate_* Python cosine ile çalışır).
    Postgres backend'iyle aynı seam yüzeyi için False döner."""
    return False


def ensure_vector_index(dim: int) -> None:
    """Parite no-op: SQLite'ta HNSW/pgvector yok (Python cosine zaten exact). Seam simetrisi."""
    return None


def drop_vector_index() -> None:
    """Parite no-op (bkz. ensure_vector_index)."""
    return None


def rls_enabled() -> bool:
    """Parite: SQLite'ta RLS yok (dev/test). İzolasyon app-katmanı WHERE ile."""
    return False


def hnsw_active() -> bool:
    """Parite: SQLite'ta HNSW yok (Python cosine exact). Seam simetrisi."""
    return False


def schema_version() -> int:
    """Parite: SQLite tek-şema (migration runner ALTER ile); pg ile aynı sürüm yüzeyi."""
    return 7


def set_tenant(tenant_id):
    """Parite no-op: SQLite'ta per-request RLS GUC yok. Seam simetrisi (api/worker aynı kodu çağırır)."""
    return None


def reset_tenant(token=None) -> None:
    """Parite no-op (bkz. set_tenant)."""
    return None


def candidate_memories(tenant_id: str, user_id, query_vec, k: int, *, exclude_third: bool = False) -> list[dict]:
    mems = get_active_memories(tenant_id, user_id, exclude_third=exclude_third)
    if not query_vec or len(mems) <= k:
        return mems
    mems.sort(key=lambda m: -_cosine(query_vec, m.get("embedding")) if m.get("embedding") else 1.0)
    return mems[:k]


def candidate_memories_lexical(tenant_id: str, user_id, query_hashes, k: int, *,
                               exclude_third: bool = False) -> list[dict]:
    """Blind lexical aday (iki-yönlü hibrit) — pg GIN yolunun oracle eşi: term_hashes ∩ sorgu-hash'leri
    olan kayıtlar, ortak-terim sayısı DESC + salience tiebreak. (SQLite dev/test; O(N) tarama yeterli.)"""
    qh = set(query_hashes or [])
    if not qh:
        return []
    sql = "SELECT * FROM memories WHERE tenant_id=? AND invalid_at IS NULL"
    args: list = [tenant_id]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    if exclude_third:
        sql += " AND (subject_party IS NULL OR subject_party<>'third_party')"
    scored = []
    with _conn() as c:
        for row in c.execute(sql, args).fetchall():
            try:
                th = json.loads(row["term_hashes"]) if row["term_hashes"] else []
            except (TypeError, ValueError):
                th = []
            ov = len(qh.intersection(th))
            if ov:
                scored.append((ov, row["salience"] or 1.0, _row_to_memory(row)))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [m for _, _, m in scored[:k]]


def backfill_lex_index(tenant_id: str | None = None, batch: int = 1000) -> int:
    """Eski kayıtlara (term_hashes IS NULL) blind lexical index'i geriye doldur (pg eşi)."""
    n = 0
    while True:
        q = "SELECT tenant_id,id,content FROM memories WHERE term_hashes IS NULL"
        args: list = []
        if tenant_id is not None:
            q += " AND tenant_id=?"
            args.append(tenant_id)
        q += " LIMIT ?"
        args.append(batch)
        with _conn() as c:
            rows = c.execute(q, args).fetchall()
            for r in rows:
                c.execute("UPDATE memories SET term_hashes=? WHERE tenant_id=? AND id=?",
                          (json.dumps(_lex_index(crypto.decrypt(r["content"]) or "")),
                           r["tenant_id"], r["id"]))
        n += len(rows)
        if len(rows) < batch:
            break
    return n


def candidate_edges(tenant_id: str, query_vec, k: int, now: float | None = None) -> list[dict]:
    edges = get_edges(tenant_id, current_only=True, now=now)
    if not query_vec or len(edges) <= k:
        return edges
    edges.sort(key=lambda e: -_cosine(query_vec, e.get("embedding")) if e.get("embedding") else 1.0)
    return edges[:k]


def candidate_entities(tenant_id: str, query_vec, k: int) -> list[dict]:
    ents = list_entities(tenant_id)
    if not query_vec or len(ents) <= k:
        return ents
    ents.sort(key=lambda e: -_cosine(query_vec, e.get("embedding")) if e.get("embedding") else 1.0)
    return ents[:k]


# ---------------- async ingestion queue (Faz 2) ----------------
_MAX_JOB_ATTEMPTS = 5


def enqueue_job(tenant_id: str, kind: str, payload: dict) -> str:
    import uuid
    jid = "job-" + uuid.uuid4().hex[:16]
    now = time.time()
    with _conn() as c:
        c.execute("INSERT INTO ingest_jobs(id,tenant_id,kind,payload,status,attempts,created_at,updated_at) "
                  "VALUES(?,?,?,?,'queued',0,?,?)",
                  (jid, tenant_id, kind, json.dumps(payload), now, now))
    return jid


def has_pending_job(tenant_id: str, kind: str) -> bool:
    """Aynı tenant+kind için queued/running iş var mı (induce dedup-enqueue: eşzamanlı torn-state önle)."""
    with _conn() as c:
        return c.execute("SELECT 1 FROM ingest_jobs WHERE tenant_id=? AND kind=? "
                         "AND status IN ('queued','running') LIMIT 1", (tenant_id, kind)).fetchone() is not None


def claim_job() -> dict | None:
    """En eski queued işi atomik olarak 'running'a al (SQLite: BEGIN IMMEDIATE ile serialize).
    Postgres backend FOR UPDATE SKIP LOCKED kullanır (çoklu worker)."""
    now = time.time()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM ingest_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not r:
            return None
        c.execute("UPDATE ingest_jobs SET status='running', attempts=attempts+1, updated_at=? "
                  "WHERE tenant_id=? AND id=?", (now, r["tenant_id"], r["id"]))
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        d["attempts"] = (d.get("attempts") or 0) + 1
        return d


def complete_job(tenant_id: str, job_id: str, result: dict) -> None:
    with _conn() as c:
        c.execute("UPDATE ingest_jobs SET status='done', result=?, error=NULL, updated_at=? "
                  "WHERE tenant_id=? AND id=?", (json.dumps(result), time.time(), tenant_id, job_id))


def fail_job(tenant_id: str, job_id: str, error: str, attempts: int) -> None:
    """Geçici hata -> attempts<MAX ise 'queued' (retry); aşılırsa 'error' (dead-letter)."""
    status = "queued" if attempts < _MAX_JOB_ATTEMPTS else "error"
    with _conn() as c:
        c.execute("UPDATE ingest_jobs SET status=?, error=?, updated_at=? WHERE tenant_id=? AND id=?",
                  (status, (error or "")[:500], time.time(), tenant_id, job_id))


def get_job(tenant_id: str, job_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT id,kind,status,result,error,attempts,created_at,updated_at "
                      "FROM ingest_jobs WHERE tenant_id=? AND id=?", (tenant_id, job_id)).fetchone()
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
            "FROM ingest_jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["result"] = json.loads(d["result"]) if d.get("result") else None
            out.append(d)
        return out


def count_jobs(status: str | None = None) -> int:
    q = "SELECT COUNT(*) n FROM ingest_jobs"
    args: list = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    with _conn() as c:
        return c.execute(q, args).fetchone()["n"]


def reclaim_stale_jobs(lease_seconds: float = 300.0) -> int:
    """Worker crash kurtarma: lease_seconds'tan eski 'running' işleri yeniden kuyruğa al
    (attempts>=MAX ise 'error'a -> dead-letter). İdempotent ingest sayesinde güvenli."""
    cutoff = time.time() - lease_seconds
    with _conn() as c:
        cur = c.execute(
            "UPDATE ingest_jobs SET status=CASE WHEN attempts>=? THEN 'error' ELSE 'queued' END, "
            "updated_at=? WHERE status='running' AND updated_at < ?",
            (_MAX_JOB_ATTEMPTS, time.time(), cutoff))
        return cur.rowcount


def seed_from_yaml(yaml_path: str, tenant_id: str) -> int:
    """policy_rules.yaml seed'ini bir tenant'a yükler. Bootstrap için."""
    import yaml  # lazy
    data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    n = 0
    for r in data.get("rules", []):
        r = dict(r)
        r["tenant_id"] = tenant_id
        add_rule(r)
        n += 1
    return n
