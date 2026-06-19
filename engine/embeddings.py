#!/usr/bin/env python3
"""embeddings.py — opsiyonel SEMANTİK retrieval (OpenAI-uyumlu /v1/embeddings).

Soft-rule relevance pre-filter'ını lexical token-overlap'ten embedding-cosine'a
yükseltir. TAMAMEN opsiyonel + graceful: embed hattı yoksa/çökerse None döner,
çağıran lexical fallback'e geçer. Sadece RANKING'i iyileştirir; yüksek/kritik soft
kurallar zaten must_run olduğu için güvenlik kararına etki etmez.

Env: CCE_EMBED_MODEL (vars 'openai/text-embedding-3-small', 1536-dim).
Hat VARSAYILAN olarak llm.py ile aynı (BASE+key), ama AYRILABİLİR: CCE_EMBED_BASE_URL /
CCE_EMBED_API_KEY set'lenirse embedding o hatta gider. Sebep: LLM OpenRouter'a yönlenince
(OpenRouter /embeddings servisi vermez -> 404) embedding ayrı bir sağlayıcıya (OpenAI/Voyage/
lokal gateway) gidebilsin. Set değilse eski davranış birebir korunur (LLM hattına düşer).
Anahtar yalnızca header'da; cache process-içi (metin->vektör), tekrar çağrı yok.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import LLM_BASE_URL, _key, _env  # noqa: E402  (aynı hat + auth + env okuyucu)
from redact import scrub_secrets  # noqa: E402  (tek egress'te scrub)

EMBED_MODEL = _env("CCE_EMBED_MODEL", default="openai/text-embedding-3-small")  # PINLI: birim-ekonomisi bu modele gore (~$0.02/1M token, 1536-dim). CCE_EMBED_DIM=1536 ile HNSW.
# Embedding hattı LLM'den AYRI olabilir (yukarı bkz). Set değilse LLM hattına düşer = eski davranış.
EMBED_BASE_URL = _env("CCE_EMBED_BASE_URL", default="").rstrip("/") or LLM_BASE_URL


def _embed_key() -> str:
    """Embedding sağlayıcı anahtarı: CCE_EMBED_API_KEY öncelik; yoksa LLM anahtarına düşer."""
    return _env("CCE_EMBED_API_KEY") or _key()
_COOLDOWN = 60.0  # geçici hata sonrası bu kadar saniye dene-me, sonra kendini iyileştir
_cache: dict[str, list[float] | None] = {}
_disabled_until = 0.0  # bu zamana kadar embed hattını deneme (latch DEĞİL, cooldown)


def embed(text: str) -> list[float] | None:
    """Metni vektöre çevir; hat yoksa/hata olursa None (çağıran fallback yapar)."""
    global _disabled_until
    clean, _ = scrub_secrets(text or "")   # secret embed sağlayıcısına gitmesin
    key = clean.strip()
    if not key:
        return None
    if key in _cache:
        return _cache[key]
    if time.time() < _disabled_until:      # cooldown'da: sessizce lexical'e bırak
        return None
    payload = {"model": EMBED_MODEL, "input": key[:8000]}
    req = urllib.request.Request(EMBED_BASE_URL + "/embeddings",
                                 data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    k = _embed_key()
    if k:
        req.add_header("Authorization", f"Bearer {k}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
        vec = body["data"][0]["embedding"]
        _cache[key] = vec
        return vec
    except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError):
        _disabled_until = time.time() + _COOLDOWN  # geçici kapat; cooldown sonrası tekrar dener
        return None


def embed_batch(texts: list[str]) -> list:
    """Çoklu metni mümkün olduğunca AZ /embeddings çağrısıyla vektörle (import hızlı yolu).
    Sıra korunur; boş/scrub-sonrası-boş -> None; hat yok/hata -> kalanlar None (lexical fallback).
    Her metin tek tek embed'lemek yerine ~96'lık bloklar (round-trip + maliyet düşer)."""
    global _disabled_until
    out: list = [None] * len(texts)
    idx, batch = [], []
    for i, t in enumerate(texts):
        clean = scrub_secrets(t or "")[0].strip()
        if clean:
            idx.append(i)
            batch.append(clean[:8000])
    if not batch or time.time() < _disabled_until:
        return out
    CHUNK = 96
    for s in range(0, len(batch), CHUNK):
        chunk = batch[s:s + CHUNK]
        payload = {"model": EMBED_MODEL, "input": chunk}
        req = urllib.request.Request(EMBED_BASE_URL + "/embeddings",
                                     data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        k = _embed_key()
        if k:
            req.add_header("Authorization", f"Bearer {k}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))["data"]
            for j, item in enumerate(data):
                pos = item.get("index", j)
                if 0 <= pos < len(chunk):
                    out[idx[s + pos]] = item["embedding"]
        except (urllib.error.URLError, OSError, KeyError, IndexError, TypeError, ValueError):
            _disabled_until = time.time() + _COOLDOWN   # geçici kapat; ne aldıysak elde kalır
            return out
    return out


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def available() -> bool:
    """Embed hattı çalışıyor mu (ucuz bir probe; sonucu cache'lenir)."""
    return embed("probe") is not None
