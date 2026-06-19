#!/usr/bin/env python3
"""crypto.py — at-rest alan şifrelemesi (memories.content, kg_edges.fact).

Anahtar: CCE_ENCRYPTION_KEY env (Fernet anahtarı; base64, 32 byte).
  - ANAHTAR YOKSA: no-op (düz metin saklanır) -> geriye tam uyumlu.
  - ANAHTAR VARSA: yazarken şifrele, okurken çöz. DB yalnızca 'enc:v1:...' görür.

Tehdit modeli: DB dump / Neon / yedek sızıntısına karşı korur (sadece ciphertext).
NOT zero-knowledge: anahtar sunucu env'inde -> çalışan uygulama (ve sunucuya erişen
operatör) çözebilir; bu, aranabilir hosted memory için pratik en üst seviye.
Anahtarı KAYBETME: anahtar gidince şifreli veri geri gelmez.

Fernet anahtarı üret:  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import hashlib
import hmac
import os

_PREFIX = "enc:v1:"

_RAW_KEY = os.environ.get("CCE_ENCRYPTION_KEY", "").strip()
try:
    from cryptography.fernet import Fernet
    _f = Fernet(_RAW_KEY.encode()) if _RAW_KEY else None
except Exception:
    _f = None

# blind-index anahtarı: at-rest sözlük-saldırısına karşı keyed-hash. CCE_ENCRYPTION_KEY varsa
# ondan türer (gizli); yoksa sabit salt (dev/test'te tutarlı ama gizli değil — şifreleme de kapalı).
_BLIND_KEY = (_RAW_KEY or "cce-blind-default-salt").encode("utf-8")


def blind(token: str) -> str:
    """Term kökünün deterministik keyed-hash'i (blind lexical index). content ŞİFRELİ olduğundan
    lexical aday üretimi düz-metinle yapılamaz; opak köklerle GIN-index üzerinden yapılır.
    Aynı anahtar -> aynı hash; yazım ve sorgu aynı fonksiyonu kullandığından tutarlı."""
    return hmac.new(_BLIND_KEY, (token or "").encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def enabled() -> bool:
    return _f is not None


def encrypt(text):
    if _f is None or not isinstance(text, str) or text == "":
        return text
    if text.startswith(_PREFIX):           # zaten şifreli
        return text
    return _PREFIX + _f.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(text):
    if not isinstance(text, str) or not text.startswith(_PREFIX):
        return text                        # düz metin / eski kayıt
    if _f is None:
        return text                        # anahtar yok -> çözemeyiz (ciphertext döner)
    try:
        return _f.decrypt(text[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return text
