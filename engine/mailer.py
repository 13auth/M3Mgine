#!/usr/bin/env python3
"""mailer.py — transactional email (Resend HTTP API, stdlib-only).

RESEND_API_KEY yoksa no-op (False döner) — flow çökmez, sadece mail gitmez.
RESEND_FROM ile gönderen ayarlanır (domain doğrulanınca noreply@13auth.com).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def enabled() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def send_email(to: str, subject: str, html: str) -> bool:
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return False
    frm = os.environ.get("RESEND_FROM", "13auth <onboarding@resend.dev>")
    data = json.dumps({"from": frm, "to": [to], "subject": subject, "html": html}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=data, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _wrap(title: str, body_html: str, cta_text: str, cta_url: str) -> str:
    return f"""<div style="font-family:system-ui,sans-serif;background:#08090a;color:#e9ebed;padding:32px">
  <div style="max-width:480px;margin:0 auto;background:#0c0e10;border:1px solid #1c2024;border-radius:12px;padding:28px">
    <div style="font-weight:700;font-size:18px;color:#a3e635">13auth</div>
    <h2 style="font-size:18px;margin:18px 0 8px">{title}</h2>
    <div style="color:#9aa1a7;font-size:14px;line-height:1.6">{body_html}</div>
    <a href="{cta_url}" style="display:inline-block;margin-top:20px;background:#a3e635;color:#0a0c08;
       font-weight:600;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:14px">{cta_text}</a>
    <p style="color:#646a70;font-size:12px;margin-top:20px">Bu isteği sen yapmadıysan görmezden gel.</p>
  </div>
</div>"""


def send_verification(to: str, url: str) -> bool:
    return send_email(to, "13auth — e-postanı doğrula",
                      _wrap("E-postanı doğrula",
                            "Hesabını etkinleştirmek için aşağıdaki butona tıkla. Link 24 saat geçerli.",
                            "E-postamı doğrula", url))


def send_password_reset(to: str, url: str) -> bool:
    return send_email(to, "13auth — şifre sıfırlama",
                      _wrap("Şifreni sıfırla",
                            "Yeni şifre belirlemek için aşağıdaki butona tıkla. Link 1 saat geçerli.",
                            "Şifremi sıfırla", url))


def send_admin_approval(to: str, pending_email: str, url: str) -> bool:
    return send_email(to, f"13auth — yeni kayıt onayı: {pending_email}",
                      _wrap("Yeni kayıt onay bekliyor",
                            f"<b>{pending_email}</b> hesap açtı ve girişin için onayını bekliyor. "
                            "Onaylamak için tıkla (ya da konsoldaki Admin sayfasından yönet).",
                            "Bu kullanıcıyı onayla", url))
