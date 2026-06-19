#!/usr/bin/env python3
"""auth.py — insan email/şifre kimlik katmanı (konsol girişi).

Bir kullanıcı = bir tenant (org). signup tenant + owner key üretir; login şifreyi
doğrulayıp tenant için TAZE bir owner 'console' key mintler (eski console key'leri
iptal eder → birikme yok). Düz key SADECE yanıtta bir kez döner.

Şifre: pbkdf2_hmac(sha256). Düz şifre ASLA saklanmaz/loglanmaz. Key'ler store'da
yalnızca hash'li tutulur ([[accounts]] ile aynı model).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import accounts  # noqa: E402
import mailer  # noqa: E402
import store  # noqa: E402

_PBKDF2_ITERS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
APP_URL = os.environ.get("APP_URL", "https://app.13auth.com").rstrip("/")
_VERIFY_TTL = 24 * 3600
_RESET_TTL = 3600
_APPROVE_TTL = 14 * 24 * 3600
# Admin e-postaları (virgülle): otomatik onaylı + başkalarını onaylayabilir. Kilitlenme önler.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(email: str) -> bool:
    return _norm_email(email) in ADMIN_EMAILS


def is_admin_tenant(tenant_id: str) -> bool:
    u = store.get_user_by_tenant(tenant_id)
    return bool(u and is_admin(u["email"]))


def _is_approved(user: dict) -> bool:
    return user.get("approved") is not None or is_admin(user.get("email", ""))


class AuthError(Exception):
    """status: HTTP kodu; code: makine-okur sebep."""
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def _validate(email: str, password: str) -> str:
    e = _norm_email(email)
    if not _EMAIL_RE.match(e):
        raise AuthError(400, "invalid_email")
    if not password or len(password) < MIN_PASSWORD:
        raise AuthError(400, "weak_password")
    return e


def signup(email: str, password: str) -> dict:
    e = _validate(email, password)
    if store.get_user_by_email(e):
        raise AuthError(409, "email_exists")
    tenant_id = "t_" + secrets.token_hex(8)
    # bootstrap key kullanılmaz (asla dönmez); gerçek erişim api_keys üzerinden
    store.upsert_tenant(tenant_id, e, "boot_" + secrets.token_urlsafe(32))
    store.create_user(e, tenant_id, _hash_password(password))
    if is_admin(e):  # admin otomatik onaylı -> anında giriş
        store.set_user_approved(e)
        try: send_verification_email(e)
        except Exception: pass
        return {"tenant_id": tenant_id, "email": e, "api_key": _mint_console_session(tenant_id)}
    # normal kullanıcı: ONAY BEKLER, oturum anahtarı VERİLMEZ
    try: _notify_admins_new_signup(e)
    except Exception: pass
    return {"status": "pending", "email": e}


def _mint_console_session(tenant_id: str) -> str:
    """Eski console oturum key'lerini iptal et (birikme önlenir), taze owner key mint et."""
    for k in store.list_api_keys(tenant_id):
        if k.get("name") == "console" and not k.get("revoked"):
            store.revoke_api_key(tenant_id, k["id"])
    return accounts.create_key(tenant_id, name="console", role="owner")["api_key"]


def login(email: str, password: str) -> dict:
    e = _norm_email(email)
    user = store.get_user_by_email(e)
    if not user or not _verify_password(password, user.get("password_hash", "")):
        raise AuthError(401, "invalid_credentials")
    if not _is_approved(user):
        raise AuthError(403, "pending_approval")
    tenant_id = user["tenant_id"]
    return {"tenant_id": tenant_id, "email": e, "api_key": _mint_console_session(tenant_id)}


def oauth_upsert(email: str) -> dict:
    """OAuth (Google/GitHub) köprüsü: sağlayıcının DOĞRULADIĞI email ile hesap aç/getir.
    Şifre yok (password_hash='oauth' -> _verify_password False; şifreyle girilemez).
    Email ile hesap eşleşmesi: aynı email password VEYA OAuth ile aynı tenant'a düşer."""
    e = _norm_email(email)
    if not _EMAIL_RE.match(e):
        raise AuthError(400, "invalid_email")
    user = store.get_user_by_email(e)
    if not user:
        tenant_id = "t_" + secrets.token_hex(8)
        store.upsert_tenant(tenant_id, e, "boot_" + secrets.token_urlsafe(32))
        store.create_user(e, tenant_id, "oauth")   # şifresiz; pbkdf2 formatı değil -> doğrulanamaz
        store.mark_email_verified(e)   # OAuth sağlayıcı email'i zaten doğruladı
        if is_admin(e):
            store.set_user_approved(e)
        else:
            try: _notify_admins_new_signup(e)
            except Exception: pass
        user = store.get_user_by_email(e)
    if not _is_approved(user):
        return {"status": "pending", "email": e}   # callback oturum açmaz, bekleme ekranına yollar
    return {"tenant_id": user["tenant_id"], "email": e, "api_key": _mint_console_session(user["tenant_id"])}


# ---------------- e-posta doğrulama + şifre sıfırlama (token + mail) ----------------
def _issue_token(tenant_id: str, email: str, purpose: str, ttl: float) -> str:
    raw = secrets.token_urlsafe(32)
    store.create_auth_token(store._hash_key(raw), tenant_id, email, purpose, time.time() + ttl)
    return raw


def send_verification_email(email: str) -> bool:
    e = _norm_email(email)
    user = store.get_user_by_email(e)
    if not user:
        return False
    raw = _issue_token(user["tenant_id"], e, "verify", _VERIFY_TTL)
    return mailer.send_verification(e, f"{APP_URL}/verify?token={raw}")


def verify_email(token: str) -> dict:
    rec = store.consume_auth_token(store._hash_key(token or ""), "verify")
    if not rec:
        raise AuthError(400, "invalid_token")
    store.mark_email_verified(rec["email"])
    return {"email": rec["email"], "verified": True}


def request_password_reset(email: str) -> None:
    """Email var olsa da olmasa da SESSİZ başarı döner (account-enumeration sızdırmaz)."""
    e = _norm_email(email)
    user = store.get_user_by_email(e)
    if user:
        raw = _issue_token(user["tenant_id"], e, "reset", _RESET_TTL)
        mailer.send_password_reset(e, f"{APP_URL}/reset?token={raw}")


def reset_password(token: str, new_password: str) -> dict:
    if not new_password or len(new_password) < MIN_PASSWORD:
        raise AuthError(400, "weak_password")
    rec = store.consume_auth_token(store._hash_key(token or ""), "reset")
    if not rec:
        raise AuthError(400, "invalid_token")
    store.update_password(rec["email"], _hash_password(new_password))
    return {"email": rec["email"], "reset": True}


# ---------------- onay kapısı (admin) ----------------
def _notify_admins_new_signup(pending_email: str) -> None:
    user = store.get_user_by_email(pending_email)
    if not user:
        return
    for admin in ADMIN_EMAILS:
        raw = _issue_token(user["tenant_id"], pending_email, "approve", _APPROVE_TTL)
        try:
            mailer.send_admin_approval(admin, pending_email, f"{APP_URL}/approve?token={raw}")
        except Exception:
            pass


def approve_via_token(token: str) -> dict:
    """E-posta linkindeki token ile onay (capability; admin'e mail edildi, tek-kullanım, süreli)."""
    rec = store.consume_auth_token(store._hash_key(token or ""), "approve")
    if not rec:
        raise AuthError(400, "invalid_token")
    store.set_user_approved(rec["email"])
    return {"email": rec["email"], "approved": True}


def admin_list_pending(tenant_id: str) -> list[dict]:
    if not is_admin_tenant(tenant_id):
        raise AuthError(403, "not_admin")
    return store.list_pending_users()


def admin_approve(tenant_id: str, email: str) -> dict:
    if not is_admin_tenant(tenant_id):
        raise AuthError(403, "not_admin")
    e = _norm_email(email)
    if not store.get_user_by_email(e):
        raise AuthError(404, "user_not_found")
    store.set_user_approved(e)
    return {"email": e, "approved": True}
