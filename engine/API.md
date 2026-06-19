# CCE — HTTP API Reference

Base: `http://<host>:8770` (üretimde TLS proxy arkası). Auth: `Authorization: Bearer <api_key>`
(tenant key'den çözülür; webhook hariç). Yanıtlar JSON. Hatalar: `{"error": "...", ...}`.

## Auth & roller
- Her istek (health/dashboard/webhook hariç) Bearer key ister → 401 yoksa.
- Roller: **owner** (bootstrap key + owner-rollü key'ler), **member**. Yıkıcı/admin işlemler owner-only → 403.
- Rate limit: per-tenant `CCE_RATE_RPM`/dk → 429.

## Genel
| Method | Path | Auth | Açıklama |
|---|---|---|---|
| GET | `/health` | yok | `{status:"ok"}` (liveness) |
| GET | `/dashboard` `/` | yok (shell) | HTML dashboard (veri authed XHR) |
| GET | `/v1/status` | key | `{llm_available, llm_key, rate_rpm}` |
| GET | `/v1/usage` | key | plan + bu dönem kullanım/kota |

## Fact-memory
| Method | Path | Auth | Gövde / Açıklama |
|---|---|---|---|
| POST | `/v1/memories` | key (metered) | `{text\|facts, user_id?, source?}` → `{added,reinforced,skipped,blocked,ids}`. `text` LLM-extraction ister (yoksa 503); `facts` doğrudan. |
| POST | `/v1/memories/search` | key (metered) | `{query, user_id?, top_k?}` → `{results:[{id,content,score,...}]}` (hybrid) |
| GET | `/v1/memories?user_id=` | key (gated) | aktif memory listesi |
| DELETE | `/v1/memories/{id}` | key | forget = crypto-shred + tombstone → `{forgotten}` |

## Correction → enforce → measure
| Method | Path | Auth | Açıklama |
|---|---|---|---|
| POST | `/v1/correct` | key (metered) | `{text, project?}` → correction'dan kural derle (LLM) |
| POST | `/v1/check` | key (metered) | `{project, output, run_soft?, fail_open?}` → `{passed, allow, violations[], unevaluated[], deferred[]}`. `allow`=fail-closed karar. |
| GET | `/v1/rules?project=` | key | kural listesi |
| DELETE | `/v1/rules/{id}` | **owner** | kural sil |
| GET | `/v1/compliance?since=` | key (gated) | per-rule canlı uyum oranı |
| GET | `/v1/compliance/trend?days=&rule=` | key (gated) | günlük uyum trendi |

## Knowledge Graph (temporal)
| Method | Path | Auth | Gövde / Açıklama |
|---|---|---|---|
| POST | `/v1/kg` | key (metered) | `{text\|triples, source?, valid_at?}` → `{added, invalidated, entities_created, triples}`. Metinden varlık+ilişki çıkarır (LLM; `text` için), bilgi grafiğine yazar. **Çelişen yeni bilgi eski kenarı bi-temporal geçersizler.** `triples` doğrudan (LLM'siz). |
| POST | `/v1/kg/search` | key (metered) | `{query, as_of?, top_k?}` → `{results:[{fact, subject, predicate, object, score, valid_at}]}`. Hybrid (semantik + graph komşuluk). `as_of` = **DÜNYA point-in-time** ("T anında ne doğruydu"). |
| GET | `/v1/kg/neighbors?entity=` | key (gated) | bir varlığın güncel kenarları (graph gezinme). |

## Context aktarımı (A: pack, B: export/import, C: handoff)
| Method | Path | Auth | Gövde / Açıklama |
|---|---|---|---|
| POST | `/v1/context` | key (metered) | `{query?, project?, user_id?, token_budget?, max_memories?, max_rules?, render?}` → `{pack, rendered?}`. İlgili hafıza+aktif kuralları token-bütçeli, taşınabilir pakete derler (her LLM'e enjekte edilir). `query` yoksa salience'a göre. |
| GET | `/v1/export` | **owner** (gated) | tenant'ın tüm bilgisini taşınabilir bundle olarak indir. **Secret YOK** (api_keys/abonelik hariç); içerik scrub'lı. |
| POST | `/v1/import` | **owner** (metered) | `{bundle, mode?}` (`merge`\|`replace`) → `{mode, memories, rules, corrections, tombstones, cleared?}`. **Erasure-güvenli**: tombstone önce, forget edilen DİRİLMEZ. `replace` bilgiyi temizler ama tombstone'ları/api_key'i korur. |
| POST | `/v1/handoff` | key (metered) | `{session_id, text?\|facts?, summary?, user_id?}` → `{session_id, summary, facts_added, fact_ids, llm_used}`. Oturumu kalıcı fact + özete indirir. |
| GET | `/v1/handoff/{session_id}?query=&budget=` | key (gated) | resume: `{found, summary, fact_ids, pack}` (özet + Context Pack). |
| GET | `/v1/handoffs` | key | oturum snapshot listesi (metadata). |

## Hesap (accounts)
| Method | Path | Auth | Açıklama |
|---|---|---|---|
| GET | `/v1/keys` | key | key listesi (metadata; raw yok) |
| POST | `/v1/keys` | **owner** | `{name, role}` → `{id, api_key, ...}` (raw SADECE burada) |
| DELETE | `/v1/keys/{id}` | **owner** | key iptal |
| DELETE | `/v1/data` | **owner** | KVKK Art.17: tenant'ın TÜM verisini sil |

## Billing webhook (sağlayıcı → biz)
| Method | Path | Auth | Açıklama |
|---|---|---|---|
| POST | `/v1/webhooks/billing` | **HMAC** | Bearer yok. Header `X-Signature` = HMAC-SHA256(`{ts}.`+body, secret), `X-Timestamp`. Gövde `{id, type, subscription_id, plan}`. tenant subscription-mapping'den çözülür. |

## Metering (memory-op)
add=1 · search=0.2 · check=1 · correct=5. Kota atomik **pre-debit** (reserve); aşımda **429**. Plan/kota: `GET /v1/usage`.

## Örnek
```bash
# memory
curl -XPOST $B/v1/memories -H "Authorization: Bearer $K" -H 'Content-Type: application/json' \
  -d '{"facts":["User prefers Python"],"user_id":"u1"}'
curl -XPOST $B/v1/memories/search -H "Authorization: Bearer $K" -d '{"query":"language?","user_id":"u1"}'
# enforce
curl -XPOST $B/v1/check -H "Authorization: Bearer $K" -d '{"project":"Acme","output":"binlerce kanal"}'
# SDK
python -c "from client import CCEClient; c=CCEClient('$B','$K'); print(c.allowed('binlerce kanal',project='Acme'))"
```

## Hata kodları
`400` geçersiz girdi · `401` auth yok/iptal · `403` owner gerekli · `413` çok büyük · `429` kota/rate · `502` LLM/derleme hatası · `503` LLM hattı yok.
