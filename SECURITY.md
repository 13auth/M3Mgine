# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Instead, report privately via either:

- **GitHub:** the repository's **Security** tab → **Report a vulnerability** (private advisory), or
- **Email:** security@13auth.com

Include a description, steps to reproduce, and potential impact. We aim to acknowledge
reports within a few business days and will keep you updated on the fix.

## Supported versions

| Version | Status |
|---------|--------|
| `v0.x`  | Early access — fixes land on `main` |

## Notes

- **Secrets** are scrubbed before storage and before any LLM call (`engine/redact.py`);
  never commit a `.env` or real credentials.
- **Erasure** is crypto-shred + semantic tombstone — deleted content cannot be
  resurrected via re-import or paraphrase.
- **Tenant isolation** is app-layer `WHERE tenant_id` by default, with optional DB-layer
  RLS; a forgotten filter fails closed rather than leaking.
