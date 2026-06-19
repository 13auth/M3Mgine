#!/usr/bin/env python3
"""payments.py — checkout BAŞLATMA (ödeme sağlayıcı soyutlaması).

webhooks.py = sağlayıcıdan BİZE gelen abonelik olaylarını plana çevirir (ALICI taraf).
Bu modül ters yön: (tenant, plan) -> sağlayıcıda checkout oturumu -> {url, ref}.
Dönen ref ÇAĞIRAN tarafından store.link_subscription(tenant, ref) ile eşlenir; sağlayıcı
sonra o ref ile webhook gönderir, webhooks.handle tenant'ı SUNUCU-TARAFI eşlemeden çözer (IDOR yok).

Sağlayıcı seçimi: CCE_PAYMENT_PROVIDER
  stub   (varsayılan) -> dev/test; GERÇEK TAHSİLAT YOK, deterministik sahte url+ref.
  iyzico              -> TR/₺ canlı; CCE_IYZICO_* gerekir (HTTP entegrasyonu sandbox creds ile tamamlanır).
"""
from __future__ import annotations

import hashlib
import os

import billing

PROVIDER = os.environ.get("CCE_PAYMENT_PROVIDER", "stub").strip().lower()


class PaymentError(Exception):
    """create_checkout hatası; mesaj API'de error kodu olarak döner."""


def _stub_checkout(tenant_id: str, plan: str) -> dict:
    """Lokal/test akışı: GERÇEK TAHSİLAT YOK. ref deterministik -> tekrar checkout idempotent eşleme."""
    ref = "stub_" + hashlib.sha256(f"{tenant_id}:{plan}".encode("utf-8")).hexdigest()[:24]
    base = os.environ.get("CCE_CHECKOUT_RETURN_URL", "https://app.13auth.com/billing/done")
    sep = "&" if "?" in base else "?"
    return {"url": f"{base}{sep}provider=stub&plan={plan}&ref={ref}", "ref": ref}


def _iyzico_auth(uri_path: str, body_str: str, api_key: str, secret: str) -> tuple[str, str]:
    """iyzico IYZWSv2 imzası: payload = randomKey + uriPath + body; sig = HMAC-SHA256(secret, payload) hex;
    Authorization = 'IYZWSv2 ' + base64('apiKey:..&randomKey:..&signature:..'). (random=secrets, resume-safe değil-test)."""
    import base64
    import hashlib
    import hmac as _hmac
    import secrets
    rnd = secrets.token_hex(8)
    payload = rnd + uri_path + body_str
    sig = _hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    params = f"apiKey:{api_key}&randomKey:{rnd}&signature:{sig}"
    return "IYZWSv2 " + base64.b64encode(params.encode("utf-8")).decode("ascii"), rnd


# plan -> iyzico pricingPlanReferenceCode (panelde oluşturulur; env'de eşlenir): CCE_IYZICO_PLAN_<PLAN>
def _plan_ref(plan: str) -> str:
    return os.environ.get("CCE_IYZICO_PLAN_" + plan.upper(), "").strip()


def _iyzico_checkout(tenant_id: str, plan: str, customer: dict | None = None) -> dict:
    """TR/₺ canlı — iyzico Abonelik Checkout Form (env-gated). [UNVERIFIED: sandbox creds ile doğrulanacak]

    Gerekli env: CCE_IYZICO_API_KEY, CCE_IYZICO_SECRET, CCE_IYZICO_BASE (sandbox: https://sandbox-api.iyzipay.com),
                 CCE_IYZICO_PLAN_SOLO/STARTER/GROWTH/PRO (panelde açılan pricingPlanReferenceCode'lar),
                 CCE_CHECKOUT_RETURN_URL (callbackUrl).
    Akış: POST {base}/v2/subscription/checkoutform/initialize (IYZWSv2 imzalı) -> {token, checkoutFormContent/paymentPageUrl};
          ref = subscriptionReferenceCode (webhook bununla gelir, store.link_subscription'a yazılır).
    TODO(canlı): iyzico abonelik 'customer' (ad/soyad/email/adres/TCKN/gsm) ZORUNLU alanlarını frontend'den topla
                 ve aşağıdaki body'ye ekle; yanıt parse + hata haritalama; sandbox round-trip testi."""
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request
    api = os.environ.get("CCE_IYZICO_API_KEY", "").strip()
    secret = os.environ.get("CCE_IYZICO_SECRET", "").strip()
    base = os.environ.get("CCE_IYZICO_BASE", "https://sandbox-api.iyzipay.com").rstrip("/")
    if not api or not secret:
        raise PaymentError("iyzico_not_configured")
    _host = urllib.parse.urlparse(base).hostname or ""
    if not (_host == "iyzipay.com" or _host.endswith(".iyzipay.com")):
        raise PaymentError("iyzico_base_invalid")   # imzalı isteği yalnız iyzipay host'una gönder (SSRF guard)
    ref_plan = _plan_ref(plan)
    if not ref_plan:
        raise PaymentError("iyzico_plan_ref_missing")   # CCE_IYZICO_PLAN_<PLAN> env yok
    uri = "/v2/subscription/checkoutform/initialize"
    body = {
        "locale": "tr",
        "conversationId": tenant_id,
        "callbackUrl": os.environ.get("CCE_CHECKOUT_RETURN_URL", "https://app.13auth.com/billing/done"),
        "pricingPlanReferenceCode": ref_plan,
        "subscriptionInitialStatus": "ACTIVE",
        "customer": _iyzico_customer(customer),   # zorunlu fatura alanları (frontend'den)
    }
    body_str = _json.dumps(body)
    auth, rnd = _iyzico_auth(uri, body_str, api, secret)
    req = urllib.request.Request(base + uri, data=body_str.encode("utf-8"), method="POST", headers={
        "Authorization": auth, "x-iyzi-rnd": rnd, "Content-Type": "application/json", "Accept": "application/json",
    })
    # imzalı Authorization başka host'a taşınmasın diye 30x yönlendirmeleri KAPAT
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:                 # noqa: S310 (host iyzipay.com doğrulandı + redirect kapalı)
            data = _json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise PaymentError("payment_provider_error")            # sağlayıcı hata gövdesini istemciye sızdırma
    if data.get("status") != "success":
        raise PaymentError("payment_provider_error")            # ham errorMessage yansıtma (LOW fix)
    url = data.get("paymentPageUrl") or data.get("checkoutFormContent")
    ref = data.get("subscriptionReferenceCode") or data.get("token")
    if not url or not ref:
        raise PaymentError("iyzico_bad_response")
    return {"url": url, "ref": ref}


def _iyzico_customer(c: dict | None) -> dict:
    """Flat müşteri girdisini iyzico abonelik 'customer' (+billing/shipping address) şekline çevir; eksikse hata."""
    c = c or {}
    req = ["name", "surname", "email", "gsmNumber", "identityNumber", "city", "address"]
    missing = [k for k in req if not str(c.get(k) or "").strip()]
    if missing:
        raise PaymentError("customer_fields_missing:" + ",".join(missing))
    addr = {
        "contactName": f"{c['name']} {c['surname']}".strip(),
        "city": c["city"], "country": c.get("country") or "Turkey", "address": c["address"],
    }
    return {
        "name": c["name"], "surname": c["surname"], "email": c["email"],
        "gsmNumber": c["gsmNumber"], "identityNumber": c["identityNumber"],
        "billingAddress": addr, "shippingAddress": addr,
    }


def create_checkout(tenant_id: str, plan: str, customer: dict | None = None) -> dict:
    """(tenant, plan[, customer]) için checkout başlat -> {url, ref}. free/enterprise self-serve değil.
    iyzico aboneliği müşteri fatura alanlarını (customer) ZORUNLU ister; stub yok sayar."""
    if plan not in billing.PLANS or plan in ("free", "enterprise"):
        raise PaymentError("invalid_plan")
    if PROVIDER == "stub":
        return _stub_checkout(tenant_id, plan)
    if PROVIDER == "iyzico":
        return _iyzico_checkout(tenant_id, plan, customer)
    raise PaymentError("unknown_provider")
