# M3Mgine

**Open-source memory + enforcement engine for AI agents.** — by [13auth](https://13auth.com)

[![CI](https://github.com/13auth/M3Mgine/actions/workflows/ci.yml/badge.svg)](https://github.com/13auth/M3Mgine/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![GitHub stars](https://img.shields.io/github/stars/13auth/M3Mgine?style=flat)](https://github.com/13auth/M3Mgine/stargazers)
![status](https://img.shields.io/badge/status-v0%20%C2%B7%20early%20access-orange)

Most memory layers **store and recall**. They don't stop your agent from repeating a
mistake. M3Mgine adds the missing piece: when you **correct** an agent, that correction
becomes a **durable rule** that is **enforced on every future output — before it reaches
the user.** Plus a processed (not dumped) memory, a temporal knowledge graph, and
provenance-tagged answers.

```
correction ──▶ compiled rule ──▶ stored ──▶ enforced on the NEXT output (before it ships)
```

---

## Why

An agent says something it shouldn't. You correct it.

- **Most setups today:** the correction is saved as a "memory" and you *hope* retrieval
  surfaces it next time. It often doesn't.
- **With M3Mgine:** the correction is compiled into a rule and **enforced at runtime**.
  The bad output is blocked *before* the user sees it — deterministically for hard rules,
  semantically (LLM-judge) for nuanced ones. Every decision leaves an audit trail.

This "correction → rule → pre-output enforcement" loop is what separates a memory store
from a control layer.

---

## What you get

- **Processed memory** — extracts atomic facts from raw text, deduplicates, tracks
  **bi-temporal** validity, and retrieves with a hybrid (dense + lexical / RRF) ranker.
  No dumping the whole transcript and hoping.
- **Enforcement** — `correction → rule → enforce`. **Hard rules** (regex, deterministic,
  zero-LLM, every request) + **soft rules** (semantic LLM-judge for tone/intent). Output
  is checked **before** it ships; violations are **fail-closed** (blocked, never silently
  passed).
- **Grounded answers + provenance** — combine live context with the tenant's situation;
  every claim is tagged `fact / prior / forecast` with a source, and the full decision is
  exported as an audit record.
- **Temporal knowledge graph** — entities + relations, not flat facts; contradictory
  information automatically invalidates the old edge while preserving history
  (point-in-time queries supported).
- **Real erasure** — crypto-shred + semantic tombstone (GDPR/KVKK Art. 17); deleted
  content can't be resurrected via re-import or paraphrase.
- **Multi-tenant, two backends, one API** — **SQLite** for dev, **PostgreSQL + pgvector**
  for production. Same API, same tests.

Bring your own model: any OpenAI-compatible endpoint (hosted or local).

---

## Quickstart (60 seconds)

No external services required — v0 runs on SQLite with an optional, env-driven LLM line.

```bash
git clone https://github.com/13auth/M3Mgine.git
cd M3Mgine/engine
pip install -r requirements.txt        # core is stdlib; only dep is PyYAML

python cli.py demo                      # one command: sets up a demo tenant + rules + memory
```

### The core loop — block a bad output before it ships

```bash
# Deterministic hard rule — works with zero LLM:
python cli.py check --tenant demo --project demo "guaranteed 100% returns, you can't lose"
#   -> allow=False  (hard violation)  exit 1

# Teach a rule from a single plain-language correction (needs an LLM line, see Config):
python cli.py correct --tenant demo "Never promise guaranteed returns; always state risk."

# Now a *new, unseen* output is judged against that rule:
python cli.py check --tenant demo "This fund will definitely double your money."
#   -> BLOCKED   (semantic: implies a guaranteed return)
python cli.py check --tenant demo "This fund did well historically, but all investing carries risk."
#   -> OK
```

### Memory

```bash
python cli.py remember --tenant demo "The user prefers Python"
python cli.py recall   --tenant demo "what language do they like"
```

### Temporal knowledge graph

```bash
python cli.py kg-add    --tenant demo "Ali works at Acme as a designer"
python cli.py kg-add    --tenant demo "Ali now works at ExampleCo"     # old edge auto-invalidated
python cli.py kg-search --tenant demo "where does Ali work"            # -> current: ExampleCo
python cli.py kg-search --tenant demo --as-of 1718500000 "where does Ali work"   # point-in-time
```

### Portability

```bash
python cli.py context --tenant demo --render "what does the user prefer"  # token-budgeted context pack
python cli.py export  --tenant demo --out demo.bundle.json                # move a tenant (no secrets, erasure-safe)
python cli.py handoff --tenant demo --session s1 --summary "where I left off"
python cli.py resume  --tenant demo --session s1
```

Install as a package: `pip install .` → `cce` CLI. HTTP API + live dashboard:
`python cli.py serve` → `http://127.0.0.1:8642/dashboard`.

---

## How it works

```
        ┌─────────────┐   correction   ┌──────────────┐   compile   ┌──────────┐
 user ─▶│  your agent │ ─────────────▶ │  M3Mgine     │ ──────────▶ │  rule    │
        └─────────────┘                │  (compiler)  │             │  store   │
              │ draft output           └──────────────┘             └────┬─────┘
              ▼                                                          │
        ┌──────────────────────────────────────────────────────────────┘
        ▼
  ┌───────────────┐   pass ✓   ┌──────────────┐
  │  ENFORCE gate │ ─────────▶ │  user sees it │
  │  hard + soft  │            └──────────────┘
  └──────┬────────┘   block ✗ (with reason → audit log) ──▶ regenerate / drop
```

1. **Detect** — capture the correction (secrets scrubbed first).
2. **Compile** — classify it and distil a *generalizable* rule (not a one-off summary).
3. **Store** — multi-tenant, deduplicated, bi-temporal.
4. **Enforce** — every candidate output passes the gate: hard regex (always, zero-LLM) +
   soft LLM-judge (nuance). Fail-closed.
5. **Measure** — per-rule compliance, with a held-out split.

---

## Usage from code

**Python SDK** ([`engine/client.py`](engine/client.py)):

```python
from client import CCEClient

cce = CCEClient("http://127.0.0.1:8642", "sk_...")

if not cce.allowed(model_output, project="demo"):   # fail-closed gate
    model_output = regenerate()

cce.correct("Never promise guaranteed returns; always state risk.", project="demo")
cce.remember("The user prefers Python")
print(cce.recall("what language do they like"))
```

**MCP** — any MCP-speaking agent (Claude Code, Cursor, your own) gets the tools
(`enforce_check`, `remember`, `recall`, `add_correction`, `kg_*`, `context_pack`) with one
config line. **HTTP API** — see [`engine/API.md`](engine/API.md).

---

## Configuration

Copy the template and fill it in — `.env` is never committed.

```bash
cp .env.example .env
```

| Variable | What it does |
|---|---|
| `CCE_STORE_BACKEND` | `sqlite` (default) or `postgres` |
| `CCE_DATABASE_URL` | Postgres DSN (when backend is `postgres`) |
| `CCE_LLM_BASE_URL` / `CCE_LLM_MODEL` / `CCE_LLM_API_KEY` | OpenAI-compatible LLM line (for soft enforce + extraction). If empty, soft rules are skipped fail-closed; hard rules still run. A local endpoint (e.g. Ollama) needs no key. |
| `CCE_EMBED_MODEL` / `CCE_EMBED_BASE_URL` / `CCE_EMBED_API_KEY` | Embedding line (optional; falls back to lexical retrieval if unset). Can point to a different provider than the LLM line. |
| `CCE_WEBHOOK_SECRET` | HMAC secret for billing webhooks |
| `ADMIN_EMAILS` | comma-separated admin emails |

Full list: [`.env.example`](.env.example).

---

## Architecture

- **Engine** — `~25` Python modules, core is stdlib (single dependency: PyYAML).
- **Storage** — pluggable: SQLite (dev) and PostgreSQL + pgvector (prod, self-provisioning).
  A **conformance gate** runs the *same* suite on both backends to guarantee behavioral parity.
- **Retrieval** — two-way hybrid: dense (pgvector) + blind keyed-hash lexical index, fused
  with RRF (recency + salience aware). HNSW index activates via `CCE_EMBED_DIM` at scale.
- **Surfaces** — CLI (`cli.py`), HTTP API (`api.py`), Python SDK (`client.py`), MCP server
  (`mcp_server.py`). All gate surfaces converge on one fail-closed decision.

---

## Verify

```bash
cd engine
python tests/run_ci.py          # all offline suites + an end-to-end run (spins up its own server)
python tests/run_conformance.py # same suites on SQLite AND PostgreSQL (behavioral parity)
```

Exit code 0 = green. `test_live_llm` runs only if a real LLM line is reachable, otherwise **SKIP**.

---

## Security posture

- All gate surfaces (API / SDK / CLI / MCP) converge on **one fail-closed decision**.
- Secrets are scrubbed **before** storage and **before** any LLM call ([`engine/redact.py`](engine/redact.py)).
- Webhooks: HMAC-signed + timestamp freshness + event dedup; tenant resolved from the
  subscription mapping, **never** from the request body (IDOR-proof).
- Erasure: crypto-shred + content + semantic tombstone; stays fail-closed even if the
  embedding line is down (paraphrases can't leak).
- Tenant isolation: app-layer `WHERE tenant_id` (default) + optional DB-layer **RLS**
  (`CCE_RLS_DSN`) — a forgotten `WHERE` becomes a fail-closed no-op, not a leak.

---

## Status

**v0, early access.** The engine runs and is covered by an offline + e2e + dual-backend
conformance test suite. It's not yet hardened for high-scale production traffic — see
[`engine/DEPLOYMENT.md`](engine/DEPLOYMENT.md) for the self-host guide and known limits.

The hosted version (managed, multi-tenant, audit export) is in early access at
[13auth.com](https://13auth.com) — **free for now**.

---

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `python engine/tests/run_ci.py`
before opening one (it must stay green), and don't commit secrets. Contributions are
accepted under [Apache-2.0](LICENSE) + a lightweight [CLA](CLA.md).

## License

[Apache-2.0](LICENSE).
