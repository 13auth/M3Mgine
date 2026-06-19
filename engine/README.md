# Correction-Compliance Engine (CCE) — v0

> AI ajanlarının aynı **marka/policy/uyum** hatasını tekrarlamasını, correction'lardan
> derlenen kuralları **runtime'da dayatarak** durduran ve **kural bazında ölçen** katman.
> Emtia memory değil; kimsenin ürünleştirmediği parça: TRACE-tarzı
> *düzeltme → atomic kural → zorlama → ölçüm* döngüsü.

Tasarım döngüsü: düzeltme → atomic kural → enforce → ölçüm.

## Neden

Düzeltmeyi yalnızca "fact" olarak saklamak düşük uyum verir. Düzeltmeyi atomic
kurala derleyip çıktı üretilmeden önce dayatmak → ihlal %100→%2 (TRACE, arXiv:2606.13174).
Fark tek aşama: **ENFORCE**. CCE o aşamayı ürünleştirir.

## 6 aşamalı döngü → modüller

| Aşama | Ne | Dosya |
|---|---|---|
| 1 DETECT | düzeltmeyi yakala (+secret temizle) | `compiler.py` (`scrub_secrets`) |
| 2 EXTRACT/COMPILE | sınıflandır → **atomic kural** | `compiler.py` (`classify`, `compile_to_rule`) |
| 3 STORE | multi-tenant kalıcı depo | `store.py` (SQLite) |
| 4 RETRIEVE | scope'lu getirme + soft pre-filter: **semantik (embedding-cosine)**, embed yoksa lexical fallback | `store.get_rules`, `embeddings.py`, `policy_engine._relevance` |
| 5 **ENFORCE** | çıktıyı kurallara karşı **dayat** (gatekeeper) | `policy_engine.py` (`enforce`) |
| 6 MEASURE | kural-bazlı uyum, **held-out ayrımı** | `evaluator.py` (`run_eval`) |

Hat: tüm LLM çağrıları `llm.py` (env-driven LLM hattı). Servis: `api.py`. CLI: `cli.py`.

## Fact-Memory (Faz 1) — genel hafıza
Sadece kural değil, **her bilgiyi** sakla/getir/güncelle (`memory.py` + `store` memories tablosu):
- **ingest**: metinden atomik fact'ler çıkar (LLM) → scrub → embed → dedup (exact + semantik cosine≥0.95) → upsert. Tekrar görülen fact salience'i artırır (reinforcement).
- **search**: hybrid skor = 0.7·semantik(embed/lexical) + 0.2·recency(exp decay) + 0.1·salience; top-k; retrieval access_count'u artırır.
- **erasure (KVKK/GDPR Art.17)**: `forget()` = **crypto-shred** (content+embedding NULL) + **tombstone** (content-level sha1 + saklanan embedding) → re-ingestّi hem **cross-user** hem **paraphrase** olarak bloklar + admin-log. (Tradeoff: paraphrase bloklamak için forget edilen vektör tutulur.)
- CLI: `cce remember/recall/memories`. SDK: `remember/recall/memories/forget_memory`.
- v0 SQLite + brute-force cosine; ölçekte pgvector/Qdrant .

## Kural tipleri
- **hard** — regex forbid/require. Deterministik, **sıfır LLM, %100 trafikte**. (örn. yasak marka/lig.)
- **soft** — semantik. LLM judge kuralın *niyetini* denetler. LLM yoksa **atlanır, motor çökmez**.

## Kurulum
```bash
pip install -r requirements.txt           # tek dep: PyYAML (çekirdek stdlib)
```
Env (LLM hattı; soft kural + correct + agent-eval için):
```
CCE_LLM_BASE_URL  (vars http://127.0.0.1:8642/v1)  
CCE_LLM_MODEL     (vars gpt-4o-mini)                    
CCE_LLM_API_KEY   (yoksa API_SERVER_KEY / ANTHROPIC_API_KEY)
CCE_DB            (vars engine/data/cce.db)
```
Anahtar yalnızca Authorization header'ında; koda/log'a/hataya **asla** yazılmaz.
Endpoint **lokal (127.0.0.1)** ise key gerekmez (`llm_available()` lokal hattı tanır).

### Hızlı başlangıç — lokal, key'siz (Ollama ile doğrulandı)
```bash
ollama serve & ; ollama pull qwen2.5:3b
set CCE_LLM_BASE_URL=http://127.0.0.1:11434/v1
set CCE_LLM_MODEL=qwen2.5:3b
python cli.py init-tenant --id s1 --name "Stüdyo" --key k1
python cli.py correct --tenant s1 "AI şunu yanlış yaptı; şöyle olmalı"   # canlı classify -> kural
python cli.py check   --tenant s1 "aday çıktı"                            # soft+hard enforce
```
**Model kalitesi notu:** detector K1 (ikili evet/hayır) küçük lokal modelle iyi çalışır;
nüanslı `classify`/soft-judge için güçlü model önerilir (GPT-4 sınıfı ya da 14B+
lokal). `CCE_LLM_MODEL` ile değiştir, kod aynı kalır.

## CLI
```bash
python cli.py init-tenant --id t_x --name "Studio X" --key SECRET
python cli.py seed        --tenant t_x --yaml policy_rules.yaml
python cli.py correct     --tenant t_x "AI şunu yanlış yaptı, şöyle olmalı"   # LLM gerekir
python cli.py check       --tenant t_x --project Acme "aday çıktı"          # exit 1 = ihlal
python cli.py eval        --tenant t_x --project Acme --dir ".../07 Memory/Evals" --save run.json
python cli.py eval        --tenant t_x --project Acme --dir "..." --baseline run.json   # regresyon kapısı
python cli.py rules       --tenant t_x --project Acme
python cli.py delete-rule --tenant t_x --rule-id <id>     # kural sil
python cli.py disable-rule --tenant t_x --rule-id <id>    # kuralı devre dışı
python cli.py forget      --tenant t_x                    # KVKK/GDPR: tüm tenant verisi
python cli.py compliance  --tenant t_x                    # per-rule canlı uyum oranı
python cli.py audit-log   --tenant t_x                    # admin işlem geçmişi (sil/purge)
python cli.py prune       --tenant t_x --days 90          # retention: eski telemetri
python cli.py serve
python cli.py doctor                                      # ortam sağlık kontrolü
python cli.py demo --tenant demo --key demo_key           # tek komutla deneme tenant'ı
python cli.py key-create/keys/key-revoke · plan · usage · link-subscription · audit-log · prune
```
Deploy & API: [DEPLOYMENT.md](DEPLOYMENT.md) · [API.md](API.md) · `../.env.example`.
`check` exit kodu: 0 temiz · 1 ihlal · 2 motor hatası → **CI/runtime kapısı** olarak kullanılır.
`eval --baseline` exit 1 = gerileme (CI-for-rules: kural-bazlı regresyon yakalama).

### Python SDK (`client.py`)
```python
from client import CCEClient
cce = CCEClient("http://127.0.0.1:8770", "sk_...")
if not cce.allowed(model_output, project="Acme"):   # fail-closed kapı
    model_output = regenerate()
cce.correct("AI şunu yanlış yaptı, şöyle olmalı", project="Acme")
cce.compliance()   # per-rule canlı oran
```

### Self-host (Docker)
```bash
docker compose up -d        # 127.0.0.1:8770, /data volume, LLM hattı env'den
# üretimde: ÖNÜNE TLS reverse proxy (nginx/Caddy); 8770'i doğrudan açma
```

### Runtime enforce gate (deployment)
`integrations/runtime_gate.py` — Ajan cevabı kullanıcıya vermeden çağırır:
```bash
echo "<aday cevap>" | python integrations/runtime_gate.py --tenant t_x --project Acme
# stdout: {"allow":bool,"violations":[...]}  exit 0 allow / 1 block (fail-closed)
```

## HTTP API (`python api.py`, vars 127.0.0.1:8770)
tenant = Bearer API key'inden çözülür (body'den ASLA — güvenlik). Gövde tavanı 256KB (413).
```
GET    /health                                                -> {status}   (auth yok)
GET    /metrics                                               -> Prometheus (key-gated, aggregate)
GET    /dashboard  (veya /)                                   -> HTML compliance dashboard (shell statik; veri authed XHR)
POST   /v1/memories          {text|facts, user_id?, source?}  -> fact-memory ingest {added, reinforced, ids}
POST   /v1/memories/search   {query, user_id?, top_k?}        -> hybrid retrieval {results[]}
GET    /v1/memories?user_id=                                  -> aktif memory listesi
DELETE /v1/memories/{id}                                      -> forget (soft-delete)
POST   /v1/check    {project, output, run_soft?, fail_open?} -> {passed, allow, violations[], unevaluated[], deferred[]}
POST   /v1/correct  {project?, text}                          -> {correction, rule}   (LLM yoksa 503)
GET    /v1/usage                                              -> plan + bu dönem kullanım/kota
GET    /v1/keys  ·  POST /v1/keys {name,role}  ·  DELETE /v1/keys/{id}   -> API key yönetimi (POST/DELETE owner-only)
GET    /v1/rules?project=                                      -> {rules[]}
GET    /v1/compliance?since=                                    -> {compliance[]}       (per-rule canlı oran)
DELETE /v1/rules/{id}                                          -> {deleted}            (kural sil)
DELETE /v1/data                                                -> {purged}             (KVKK Art.17: tüm tenant verisi)
```
`allow` = FAIL-CLOSED karar: ihlal VEYA değerlendirilemeyen yüksek-önemli soft kural -> allow=false.

## Multi-tenancy & güvenlik
- Her satır `tenant_id` ile etiketli; `get_rules` tenant dışına sızdırmaz (test: izolasyon ✓).
- tenant kimliği **Bearer key'inden** çözülür (body'den asla); API key'ler **at-rest SHA-256 hash** (düz saklanmaz).
- secret filtresi HEM ingest HEM enforce yolunda (`redact.py` paylaşılan).
- **KVKK/GDPR Art.17 silme:** `cce forget` / `DELETE /v1/data` tüm tenant verisini siler.

## Güvenlik sertleştirme
Çok turlu güvenlik incelemesinden geçti; tüm kritik ve yüksek bulgular kapatıldı.
Tüm critical+high düzeltildi ve test edildi. Tekrar eden ana sınıf: **non-canonical enum baypası**
— severity/type/status casing-typo değeri kuralı sessizce enforce dışı bırakıyordu; üçü de artık
tek noktada fail-closed normalize ediliyor (`_norm_sev`/`_sev`→critical, `_norm_type`→soft,
store status/type/severity canonicalize). Öne çıkan ilk-tur düzeltmeleri:
- **FAIL-CLOSED enforce:** LLM çökünce yüksek-önemli soft kural sessizce geçmez → gate bloklar (`res.safe()`).
- **Dedup + provenance:** kural id deterministik içerik-hash → re-ingest in-place günceller (kopya birikmez), `version++`, `correction_id` bağı, pattern birleştirme.
- **Canlı ölçüm:** runtime enforce artık `checks`(denominator)+`violations` yazar → `compliance_by_rule` gerçek per-rule ORAN.
- **DoS:** gövde 256KB tavanı (413), metin 20K tavanı, SQLite WAL+busy_timeout.
- **ReDoS:** iç-içe niceleyici tespiti + `re.error` guard + 50K tarama sınırı.
- **Secret egress:** enforce judge'a giden output scrub'lanır.
- **Judge paneli:** `CCE_JUDGE_VOTES` ile çoklu-judge çoğunluk oyu (vars 1).

## Durum (dürüst)
- **Key'siz test edildi (`tests/` 3 paket GEÇTİ):** store + multi-tenant izolasyon, enforce hard kurallar, evaluator + held-out ayrımı/overfit yorumu, **regresyon kapısı (CI-for-rules)**, stage-1 detector heuristik + scan_transcript, tüm CLI, HTTP API (health/rules/check/401/503), runtime enforce gate.
- **CANLI LLM doğrulandı (lokal Ollama qwen2.5:3b, gateway key'i OLMADAN — `tests/test_live_llm.py`):** `classify`, `ingest` (correction→rule), soft-kural LLM judge, detector K1 lokal-model doğrulama. Bütünleşik CLI demosu (correct→rule→check→gate) uçtan uca çalıştı.
- **Tek kalan = kalite, kod değil:** nüanslı classify, 3B lokal modelde kabaca çalışır; üretim için güçlü model (GPT-4 sınıfı / 14B+) önerilir — sadece `CCE_LLM_MODEL` değişir.

## Faz 2 — SaaS kabuğu (abonelik/hesap)
- **billing.py** — planlar (free/starter/growth/pro/enterprise) + memory-op metering (add=1, search=0.2, check=1, correct=5) + **atomik kota reserve** (`store.try_consume`, BEGIN IMMEDIATE → TOCTOU yok, cost-aware: kalan op'a sığmayan iş reddedilir). Aşımda 429. `cce plan/usage`, `GET /v1/usage`.
- **accounts.py** — org başına çoklu, isimli, rollü (owner/member), iptal-edilebilir API key (raw bir kez döner, hash saklanır). `tenant_by_key` önce aktif api_keys sonra bootstrap key. `cce key-create/key-revoke/keys`, `GET/POST /v1/keys`.
- **RBAC** — yıkıcı/admin işlemler owner-only (purge, rule-delete, key-create/revoke → 403); member memory/check/correct/forget yapabilir.
- Dashboard'da plan/kullanım rozeti.
- Kalan (infra/dış): Postgres+pgvector migrasyon, Polar/iyzico ödeme webhook, yönetilen deployment (roadmap).

## Üretim dağıtımı & bilinen sınırlar
- **Paket:** kök `pyproject.toml` → `pip install -e .` ile `cce` komutu (veya `python engine/cli.py`). Modüller dir'i sys.path'e ekleyerek düz import kullanır (kurulu/çıplak çalışır).
- **TLS/CORS:** sunucu düz HTTP; **TLS sonlandıran reverse proxy ARKASINDA** çalıştır (nginx/Caddy/LB). `HOST` loopback değilse başlangıçta uyarı verir. 0.0.0.0'a TLS'siz bağlama.
- **ReDoS:** heuristik + 10K tarama sınırı catastrophic backtracking'i azaltır ama stdlib `re`'de gerçek timeout yok — yüksek-riskli üretimde `regex` (timeout=) veya ayrı-process deadline önerilir.
- **llm_available:** "yapılandırılmış" demek, "erişilebilir" değil; gerçek garanti enforce katmanının LLMError→unevaluated→fail-closed yoludur.
- **Ölçüm denominator:** `checks` yalnızca *değerlendirilen* kuralları sayar; unevaluated/deferred sayılmaz (oran iyimser olabilir). Hiç check görmemiş kural `compliance`'ta görünmez.
- **Multi-process:** rate limit ve schema-once in-process; çok-process dağıtımda paylaşımlı limiter (Redis) + tek init gerekir.

## Sıradaki (roadmap — v0 sonrası)
- Retrieve stage 4 ölçekte: multi-signal (semantic+BM25), kural çoğalınca.
- Storage adapter: pgvector/Qdrant (büyük tenant).
- Per-rule zaman serisi dashboard + değişiklik-atıf (hangi prompt/model değişikliği gerilemeye yol açtı).
- Packaging (pip installable `cce`), Docker/Helm self-host dağıtımı.
```
```
