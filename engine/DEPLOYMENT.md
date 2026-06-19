# CCE — Deployment & Operations

Self-host kurulum, ortam, güvenlik, ölçek.

## Hızlı başlangıç (Docker)
```bash
cp .env.example .env   # değişkenleri doldur (aşağıda)
docker compose up -d   # 127.0.0.1:8770, /data volume
# sağlık:
docker compose exec cce python /app/engine/cli.py doctor
```
Çıplak (Docker'sız): `pip install -r engine/requirements.txt && python engine/api.py`.

## Ortam değişkenleri
| Değişken | Vars | Açıklama |
|---|---|---|
| `CCE_DB` | engine/data/cce.db | SQLite yolu (v0; ölçekte Postgres) |
| `CCE_HOST` / `CCE_PORT` | 127.0.0.1 / 8770 | bind (üretimde proxy arkası) |
| `CCE_MAX_BODY` / `CCE_MAX_TEXT` / `CCE_MAX_FACTS` | 256K / 20K / 100 | DoS tavanları |
| `CCE_RATE_RPM` | 120 | per-tenant dakikada istek |
| `CCE_JUDGE_VOTES` | 1 | soft-rule judge paneli (tek/çok) |
| `CCE_WEBHOOK_SECRET` | — | ödeme webhook HMAC secret (yoksa webhook reddeder) |
| `CCE_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | OpenRouter / **openai/gpt-4o-mini** (varsayılan) / — | LLM hattı (BYO-key) |
| `CCE_EMBED_MODEL` / `CCE_EMBED_DIM` | openai/text-embedding-3-small / **1536** | embedding modeli + HNSW boyutu (CCE_EMBED_DIM set edilince HNSW açılır) |

Anahtarlar **asla** imaja/log'a yazılmaz; sadece env + Authorization header.

## Güvenlik (production)
- **TLS:** sunucu düz HTTP. **TLS sonlandıran reverse proxy** (nginx/Caddy/LB) ARKASINDA çalıştır; `8770`'i doğrudan internete AÇMA. `CCE_HOST` loopback değilse başlangıçta uyarı verir.
- **Auth:** Bearer API key (SHA-256 hash saklanır). Çoklu key + RBAC (owner/member). Yıkıcı işlemler owner-only.
- **Webhook:** `/v1/webhooks/billing` HMAC imzalı-timestamp + freshness + event dedup + IP rate-limit.
- **Secrets:** Infisical/Doppler önerilir; `CCE_WEBHOOK_SECRET` ve LLM key'i orada tut.
- **İzolasyon:** her sorgu `tenant_id` ile filtreli (DB-enforced). ReDoS guard + body/text/facts tavanları.

## KVKK / veri
- **Erasure:** `DELETE /v1/memories/{id}` ve `DELETE /v1/data` (purge) → crypto-shred (içerik+embedding NULL) + tombstone (re-ingest engeli) + admin-log.
- **Yerellik:** TR müşteri/devlet için AWS İstanbul Local Zone veya TTBulut/Turkcell'de barındır; VERBIS + Türkçe DPA + SS-2/SS-3.

## Yedekleme & ops
- SQLite: WAL açık (`wal_autocheckpoint`), `PRAGMA wal_checkpoint(TRUNCATE)` temiz kapanışta. Düzenli `*.db` snapshot + offsite kopya.
- Retention: `cce prune --days 90` (eski telemetri).
- Sağlık: `cce doctor` (DB/LLM/embed/webhook/plan); `GET /health` (liveness).
- Gözlemlenebilirlik: Axiom/Sentry/BetterUptime.

## Ölçek (v0 → üretim)
- v0 tek-node SQLite + brute-force cosine = ~yüz bin memory'ye dek yeterli.
- Ölçekte: **Postgres + pgvector** (metadata+vektör) veya **Qdrant** (tiered multitenancy); embedding'i Voyage→BGE-M3 self-host (>$500/ay). Çok-process'te rate-limit'i Redis'e taşı (in-process limiter tek-process).

## Abonelik
`cce plan --tenant X --set growth` veya ödeme sağlayıcı (Polar/iyzico) → `POST /v1/webhooks/billing`. Checkout'ta `cce link-subscription --tenant X --ref <provider_sub_id>` ile eşle (webhook tenant'ı buradan çözer).
