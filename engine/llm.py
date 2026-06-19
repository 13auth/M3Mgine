#!/usr/bin/env python3
"""llm.py — Tek LLM giriş noktası (env-driven).

Ürünün TÜM model çağrıları buradan geçer (tek hat disiplini). Kütüphane
olduğu için hata durumunda sys.exit DEĞİL, LLMError fırlatır — çağıran yakalar.

Env (CCE_*):
  CCE_LLM_BASE_URL   (vars http://127.0.0.1:8642/v1) 
  CCE_LLM_MODEL      (vars openai/gpt-4o-mini; PINLI — fiyatlandirma buna gore)
  CCE_LLM_API_KEY    (yoksa API_SERVER_KEY, o da yoksa ANTHROPIC_API_KEY)
Anahtar SADECE Authorization header'ında; koda/log'a/hataya yazılmaz.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _env(*names: str, default: str = "") -> str:
    """İlk dolu env değerini döndür."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


LLM_BASE_URL = _env("CCE_LLM_BASE_URL",
                    default="http://127.0.0.1:8642/v1").rstrip("/")
LLM_MODEL = _env("CCE_LLM_MODEL", default="openai/gpt-4o-mini")  # PINLI: birim-ekonomisi bu modele gore; degistirmeden once billing.py gozden gecir.


class LLMError(RuntimeError):
    """Model hattı erişilemediğinde / beklenmeyen yanıtta."""


def _key() -> str:
    return _env("CCE_LLM_API_KEY", "API_SERVER_KEY", "ANTHROPIC_API_KEY")


def has_key() -> bool:
    return bool(_key())


def _is_local_endpoint() -> bool:
    host = (urlparse(LLM_BASE_URL).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def llm_available() -> bool:
    """LLM çağrısı denenebilir mi? Key varsa YA DA endpoint lokal (keyless, ör. Ollama).
    'has_key' yanlış kapıydı: lokal model key istemez ama tamamen çalışır."""
    return has_key() or _is_local_endpoint()


def call_model(system: str, user: str, max_tokens: int = 800,
               temperature: float = 0.0, response_format: dict | None = None,
               model: str | None = None) -> str:
    """Tek model çağrısı. response_format ör. {"type":"json_object"} -> sağlayıcı JSON
    garantili döndürür (destekleyen modellerde format-fail ~0). Desteklemeyen model
    parametreyi yok sayar/4xx verir; çağıran graceful olmalı. model=None -> pinli LLM_MODEL."""
    payload = {
        "model": model or LLM_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if response_format is not None:
        payload["response_format"] = response_format
    req = urllib.request.Request(
        LLM_BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    key = _key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = " (401: CCE_LLM_API_KEY / API_SERVER_KEY ortamda mi?)" if e.code == 401 else ""
        raise LLMError(f"HTTP {e.code} (base={LLM_BASE_URL}, model={LLM_MODEL}){hint}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"baglanti: {e.reason} (base={LLM_BASE_URL})") from None
    except OSError as e:  # socket.timeout/TimeoutError (read-timeout URLError'a sarılmaz) -> LLMError
        raise LLMError(f"baglanti/timeout: {e} (base={LLM_BASE_URL})") from None
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"beklenmeyen yanit: {json.dumps(body)[:300]}") from None
