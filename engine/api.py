#!/usr/bin/env python3
"""api.py — Correction-Compliance Engine HTTP API (stdlib, sıfır harici dep).

OpenAI-tarzı, multi-tenant. tenant kimliği SADECE Bearer API key'inden çözülür
(asla request body'sinden — güvenlik). Endpoints:

  GET  /health                         -> {status}
  POST /v1/check    {project, output}  -> enforce sonucu (stage 5)
  POST /v1/correct  {project, text}    -> correction -> compiled rule (stage 1-2)
  GET  /v1/rules?project=              -> tenant kuralları

Başlat:  python api.py            (vars 127.0.0.1:8770)
         CCE_HOST / CCE_PORT ile değiştir.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
import accounts  # noqa: E402
import billing  # noqa: E402
import compiler  # noqa: E402
import context as ctx  # noqa: E402
import embeddings  # noqa: E402
import dashboard  # noqa: E402
import handoff  # noqa: E402
import auth  # noqa: E402
import kg  # noqa: E402
import memory  # noqa: E402
import obsidian  # noqa: E402
import outbound  # noqa: E402
import payments  # noqa: E402
import metrics  # noqa: E402
import portability  # noqa: E402
import routing  # noqa: E402
import store  # noqa: E402
import taxonomy  # noqa: E402
import webhooks  # noqa: E402

LOG_ON = os.environ.get("CCE_LOG", "") not in ("", "0", "false")
from llm import LLMError, LLM_MODEL, has_key, llm_available  # noqa: E402
from policy_engine import enforce  # noqa: E402

HOST = os.environ.get("CCE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCE_PORT") or os.environ.get("PORT", "8770"))  # PORT: Render/Fly/Heroku enjekte eder
MAX_BODY = int(os.environ.get("CCE_MAX_BODY", str(256 * 1024)))   # DoS: gövde tavanı
MAX_TEXT = int(os.environ.get("CCE_MAX_TEXT", "20000"))           # LLM maliyet/latency tavanı
MAX_FACTS = int(os.environ.get("CCE_MAX_FACTS", "100"))           # ingest facts[] tavanı


def _valid_user(uid) -> bool:
    return isinstance(uid, str) and 0 < len(uid) <= 128
RATE_RPM = int(os.environ.get("CCE_RATE_RPM", "120"))             # per-tenant dakikada istek

_RATE: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()


def _rate_ok(tenant: str) -> bool:
    """Basit per-tenant sliding-window (in-process). Bir tenant servisi boğamasın."""
    now = time.time()
    with _RATE_LOCK:
        q = _RATE.setdefault(tenant, [])
        q[:] = [t for t in q if t >= now - 60]
        if len(q) >= RATE_RPM:
            return False
        q.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # sessiz; log'a payload/secret sızdırma
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._observe(code)

    def _send_text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._observe(code)

    def _observe(self, code: int):
        try:
            path = urlparse(self.path).path
            metrics.record(self.command, path, code)
            if LOG_ON:  # yapılandırılmış erişim log'u — PII/payload YOK
                print(json.dumps({"m": self.command, "p": path, "s": code}), file=sys.stderr)
        except Exception:
            pass

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _quota_gate(self, tenant: str) -> bool:
        """Read-only gate: kota aşıldıysa 402 (Payment Required) + yükseltme ipucu, True dön (metering YOK)."""
        q = billing.quota(tenant)
        if not q["allowed"]:
            self._send(402, {"error": "quota_exceeded", "upgrade": billing.upgrade_hint(q["plan"]), **q})
            return True
        return False

    def _handle_webhook(self):
        """Ödeme sağlayıcı webhook'u: Bearer YOK; IP rate-limit + HMAC(imzalı-ts) doğrula."""
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or self.client_address[0])
        if not _rate_ok("wh:" + ip):  # imza-deneme/DoS yüzeyi
            return self._send(429, {"error": "rate_limited"})
        sig = self.headers.get("X-Signature", "")
        ts = self.headers.get("X-Timestamp", "")
        if not sig:
            return self._send(401, {"error": "missing_signature"})
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n < 0 or n > MAX_BODY:
            return self._send(413, {"error": "payload_too_large"})
        raw = self.rfile.read(n) if n else b""
        if not webhooks.verify(raw, sig, ts):
            return self._send(401, {"error": "invalid_signature"})
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "invalid_json"})
        if not isinstance(event, dict):  # non-object webhook gövdesi -> 400 (crash önle)
            return self._send(400, {"error": "body_must_be_object"})
        return self._send(200, webhooks.handle(event))

    def _handle_auth(self, path):
        """İnsan signup/login: Bearer YOK (giriş öncesi). IP rate-limit (brute-force/DoS)."""
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or self.client_address[0])
        if not _rate_ok("auth:" + ip):
            return self._send(429, {"error": "rate_limited"})
        try:
            body = self._body()
        except ValueError as e:
            if str(e) == "body_too_large":
                return self._send(413, {"error": "payload_too_large"})
            return self._send(400, {"error": "invalid_json"})
        except Exception:
            return self._send(400, {"error": "invalid_json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body_must_be_object"})
        email = str(body.get("email") or "")
        password = str(body.get("password") or "")
        token = str(body.get("token") or "")
        try:
            if path.endswith("/signup"):
                res = auth.signup(email, password)
            elif path.endswith("/login"):
                res = auth.login(email, password)
            elif path.endswith("/request-reset"):
                auth.request_password_reset(email); res = {"ok": True}   # her zaman 200 (enumeration yok)
            elif path.endswith("/resend-verification"):
                auth.send_verification_email(email); res = {"ok": True}
            elif path.endswith("/reset"):
                res = auth.reset_password(token, password)
            elif path.endswith("/verify"):
                res = auth.verify_email(token)
            elif path.endswith("/approve"):   # admin'e mail'lenen onay-linki (capability token)
                res = auth.approve_via_token(token)
            else:
                return self._send(404, {"error": "not_found"})
            return self._send(200, res)
        except auth.AuthError as e:
            return self._send(e.status, {"error": e.code})
        except Exception:
            return self._send(500, {"error": "auth_failed"})

    def _handle_oauth_bridge(self):
        """OAuth köprüsü: frontend (Vercel) sağlayıcının doğruladığı email'i paylaşılan
        secret ile gönderir → tenant+key. Secret yoksa kapalı (503). Açık uç DEĞİL."""
        import hmac
        secret = os.environ.get("OAUTH_BRIDGE_SECRET")
        if not secret:
            return self._send(503, {"error": "oauth_disabled"})
        if not hmac.compare_digest(self.headers.get("X-OAuth-Bridge", ""), secret):
            return self._send(401, {"error": "unauthorized"})
        ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0])
        if not _rate_ok("oauth:" + ip):
            return self._send(429, {"error": "rate_limited"})
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "invalid_json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "body_must_be_object"})
        try:
            return self._send(200, auth.oauth_upsert(str(body.get("email") or "")))
        except auth.AuthError as e:
            return self._send(e.status, {"error": e.code})
        except Exception:
            return self._send(500, {"error": "auth_failed"})

    def _reserve(self, tenant: str, op_type: str, n: int = 1) -> bool:
        """ATOMİK pre-debit (atomik kota işlemi): kota yoksa 402 (Payment Required) + yükseltme ipucu + False; varsa düş + True."""
        if not billing.reserve(tenant, op_type, n):
            q = billing.quota(tenant)
            self._send(402, {"error": "quota_exceeded", "upgrade": billing.upgrade_hint(q["plan"]), **q})
            return False
        return True

    def _storage_gate(self, tenant: str) -> bool:
        """STORAGE (stok) tavanı: aktif hatıra tavanı dolduysa yeni add'i 402 ile reddet.
        op kotasından AYRI — storage sürekli tahakkuk eden Neon maliyeti (hoarder kuyruğu kapatılır)."""
        sq = billing.storage_quota(tenant)
        if not sq["allowed"]:
            self._send(402, {"error": "storage_limit_reached", "storage": sq,
                             "detail": "aktif hatıra tavanı doldu; eski kayıtları silin veya planı yükseltin"})
            return False
        return True

    def _role(self) -> str | None:
        h = self.headers.get("Authorization", "")
        return store.role_by_key(h[7:].strip()) if h.startswith("Bearer ") else None

    def _require_owner(self) -> bool:
        """Yıkıcı/admin işlemler owner rolü ister (RBAC). Değilse 403 + False."""
        if self._role() != "owner":
            self._send(403, {"error": "owner_role_required"})
            return False
        return True

    def handle_one_request(self):
        # RLS: her isteğin sonunda tenant bağlamını temizle (keep-alive'da bir sonraki isteğe sızmasın).
        try:
            super().handle_one_request()
        finally:
            store.reset_tenant()

    def _auth(self) -> str | None:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return None
        tenant = store.tenant_by_key(h[7:].strip())
        if tenant:
            store.set_tenant(tenant)   # RLS GUC bağlamı (RLS kapalıyken etkisiz; bkz. store_pg._conn)
        return tenant

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n < 0:
            raise ValueError("invalid_length")
        if n > MAX_BODY:                      # DoS: tavanı aşan gövdeyi okumadan reddet
            raise ValueError("body_too_large")
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---- routes ----
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"status": "ok"})  # config sızdırma (#22): sadece liveness
        if u.path in ("/", "/dashboard"):
            return self._send_html(dashboard.HTML)  # statik shell (secret yok); veri authed XHR
        tenant = self._auth()
        if not tenant:
            return self._send(401, {"error": "invalid_or_missing_api_key"})
        if not _rate_ok(tenant):
            return self._send(429, {"error": "rate_limited", "limit_rpm": RATE_RPM})
        if u.path == "/v1/status":  # config bilgisi sadece authed (#17/#22)
            st = {"service": "cce", "llm_available": llm_available(),
                  "llm_key": has_key(), "rate_rpm": RATE_RPM,
                  "llm_model": LLM_MODEL, "embed_model": embeddings.EMBED_MODEL,
                  "embed_dim": os.environ.get("CCE_EMBED_DIM") or None,   # HNSW boyutu (set ise aktif)
                  "hnsw_active": store.hnsw_active(),      # canlı aktivasyon doğrulaması
                  "rls_enabled": store.rls_enabled(),
                  "schema_version": store.schema_version(),
                  "is_admin": auth.is_admin_tenant(tenant)}
            if (parse_qs(u.query).get("probe") or [""])[0] == "1":  # embed hattını canlı dene (teşhis)
                st["embed_available"] = embeddings.available()
            return self._send(200, st)
        if u.path == "/v1/admin/pending":  # admin: onay bekleyen kayıtlar
            try:
                return self._send(200, {"pending": auth.admin_list_pending(tenant)})
            except auth.AuthError as e:
                return self._send(e.status, {"error": e.code})
        if u.path == "/metrics":  # Prometheus (key-gated; aggregate, PII yok)
            return self._send_text(200, metrics.prometheus())
        if u.path == "/v1/usage":  # plan + bu dönem kullanım/kota
            return self._send(200, billing.quota(tenant))
        if u.path == "/v1/billing/invoice":  # dönem fatura/kullanım-özeti (matrah+KDV+toplam; ?period=YYYY-MM)
            period = (parse_qs(u.query).get("period", [None])[0] or None)
            return self._send(200, billing.build_invoice(tenant, period))
        if u.path == "/v1/overview":  # Genel bakış: hafıza/kategori/kullanıcı/graph/PII özeti (tek çağrı)
            tax = store.list_taxonomy(tenant)
            return self._send(200, {
                "memories": store.count_memories(tenant),
                "categories": len(tax),
                "end_users": store.count_distinct_users(tenant),
                "kg": store.count_kg(tenant),
                "pii": store.pii_summary(tenant),
                "top_categories": [{"label": n["label"], "members": n["member_count"]} for n in tax[:6]],
            })
        if u.path == "/v1/keys":  # key listesi (metadata; raw yok)
            return self._send(200, {"keys": accounts.list_keys(tenant)})
        if u.path == "/v1/rules":
            project = (parse_qs(u.query).get("project") or [None])[0]
            return self._send(200, {"rules": store.get_rules(tenant, project)})
        if u.path == "/v1/projects":  # kural namespace'leri (distinct project + sayılar)
            return self._send(200, {"projects": store.list_projects(tenant)})
        if u.path == "/v1/taxonomy":  # AACRP: otomatik kategori ağacı
            return self._send(200, {"nodes": store.list_taxonomy(tenant)})
        if u.path == "/v1/webhooks":  # giden webhook kayıtları (secret DÖNMEZ)
            hooks = [{k: v for k, v in h.items() if k != "secret_hash"}
                     for h in store.list_webhooks(tenant)]
            return self._send(200, {"webhooks": hooks, "events": list(outbound.EVENTS)})
        if u.path == "/v1/memories":  # pahalı: tüm aktif memory -> over-quota'da gate (metering yok)
            if self._quota_gate(tenant):
                return
            uid = (parse_qs(u.query).get("user_id") or [None])[0]
            return self._send(200, {"memories": store.get_active_memories(tenant, uid)})
        if u.path == "/v1/users":  # son-kullanıcı listesi (distinct user_id + sayı) -> GROUP BY -> gate
            if self._quota_gate(tenant):
                return
            return self._send(200, {"users": store.list_users(tenant)})
        if u.path == "/v1/compliance":  # pahalı GROUP BY -> gate
            if self._quota_gate(tenant):
                return
            since = float((parse_qs(u.query).get("since") or ["0"])[0] or 0)
            return self._send(200, {"compliance": store.compliance_by_rule(tenant, since)})
        if u.path == "/v1/compliance/trend":  # pahalı aggregation -> gate
            if self._quota_gate(tenant):
                return
            qs = parse_qs(u.query)
            days = int((qs.get("days") or ["14"])[0] or 14)
            rule = (qs.get("rule") or [None])[0]
            return self._send(200, {"trend": store.compliance_timeseries(tenant, days, rule)})
        if u.path == "/v1/export":  # tüm bilgi egress'i -> owner-only + pahalı -> gate
            if not self._require_owner():
                return
            if self._quota_gate(tenant):
                return
            return self._send(200, portability.export_tenant(tenant))
        if u.path == "/v1/handoffs":  # oturum snapshot listesi (metadata)
            return self._send(200, {"handoffs": store.list_handoffs(tenant)})
        if u.path == "/v1/kg/graph":  # tüm grafiği görselleştirme için dök (cap'li)
            if self._quota_gate(tenant):
                return
            NODE_CAP, EDGE_CAP = 600, 2000
            ents = store.list_entities(tenant)
            edges = store.get_edges(tenant, current_only=True)
            nodes = [{"id": e["id"], "name": e.get("name") or "", "type": e.get("type") or ""}
                     for e in ents[:NODE_CAP]]
            nodeset = {n["id"] for n in nodes}
            out_edges = []
            for e in edges:
                oid = e.get("object_id")
                if oid and e.get("subject_id") in nodeset and oid in nodeset:
                    out_edges.append({"source": e["subject_id"], "target": oid,
                                      "predicate": e.get("predicate") or ""})
                    if len(out_edges) >= EDGE_CAP:
                        break
            return self._send(200, {"nodes": nodes, "edges": out_edges,
                                    "stats": {"entities": len(ents),
                                              "edges": sum(1 for x in edges if x.get("object_id"))}})

        if u.path == "/v1/kg/neighbors":  # bir varlığın güncel kenarları (graph gezinme)
            if self._quota_gate(tenant):
                return
            ent = (parse_qs(u.query).get("entity") or [""])[0]
            if not ent:
                return self._send(400, {"error": "entity_required"})
            return self._send(200, {"neighbors": kg.neighbors(tenant, ent)})
        if u.path == "/v1/jobs":  # son ingest işleri (Batches yüzeyi)
            limit = int((parse_qs(u.query).get("limit") or ["50"])[0] or 50)
            return self._send(200, {"jobs": store.list_jobs(tenant, limit)})
        if u.path.startswith("/v1/jobs/"):  # async ingest job durumu/sonucu
            job = store.get_job(tenant, u.path[len("/v1/jobs/"):])
            return self._send(200 if job else 404, job or {"error": "not_found"})
        if u.path.startswith("/v1/handoff/"):  # resume: özet + context pack
            if self._quota_gate(tenant):
                return
            sid = u.path[len("/v1/handoff/"):]
            qs = parse_qs(u.query)
            query = (qs.get("query") or [None])[0]
            budget = int((qs.get("budget") or ["2000"])[0] or 2000)
            res = handoff.resume(tenant, sid, query=query, token_budget=budget)
            return self._send(200 if res.get("found") else 404, res)
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/v1/webhooks/billing":   # sağlayıcı->biz: Bearer yok, HMAC doğrulanır
            return self._handle_webhook()
        if u.path in ("/v1/auth/signup", "/v1/auth/login", "/v1/auth/request-reset",
                      "/v1/auth/reset", "/v1/auth/verify", "/v1/auth/resend-verification",
                      "/v1/auth/approve"):
            return self._handle_auth(u.path)   # insan auth akışı: Bearer YOK
        if u.path == "/v1/auth/oauth":   # OAuth köprüsü: Bearer YOK, paylaşılan secret ile
            return self._handle_oauth_bridge()
        tenant = self._auth()
        if not tenant:
            return self._send(401, {"error": "invalid_or_missing_api_key"})
        if not _rate_ok(tenant):
            return self._send(429, {"error": "rate_limited", "limit_rpm": RATE_RPM})
        try:
            body = self._body()
        except ValueError as e:
            if str(e) == "body_too_large":
                return self._send(413, {"error": "payload_too_large", "max_bytes": MAX_BODY})
            return self._send(400, {"error": "invalid_json"})
        except Exception:
            return self._send(400, {"error": "invalid_json"})
        if not isinstance(body, dict):  # valid-JSON ama obje değil ([..]/5/null) -> 400 (crash önle)
            return self._send(400, {"error": "body_must_be_object"})

        if u.path == "/v1/admin/approve":  # admin: bir kullanıcıyı onayla
            try:
                return self._send(200, auth.admin_approve(tenant, str(body.get("email") or "")))
            except auth.AuthError as e:
                return self._send(e.status, {"error": e.code})

        if u.path == "/v1/keys":  # yeni key üret -> owner-only; raw SADECE yanıtta bir kez
            if not self._require_owner():
                return
            role = body.get("role", "member")
            if role not in accounts.ROLES:
                return self._send(400, {"error": "invalid_role", "roles": list(accounts.ROLES)})
            k = accounts.create_key(tenant, name=(str(body.get("name") or "key"))[:128], role=role)
            return self._send(200, k)

        if u.path == "/v1/webhooks":  # giden webhook kaydı -> owner-only; secret SADECE yanıtta bir kez
            if not self._require_owner():
                return
            url = str(body.get("url") or "").strip()[:512]
            if not outbound.safe_url(url):
                return self._send(400, {"error": "invalid_or_unsafe_url",
                                        "detail": "http(s) olmalı; özel/loopback/link-local host reddedilir"})
            events = body.get("events") or []
            if not isinstance(events, list):
                return self._send(400, {"error": "events_must_be_list"})
            return self._send(200, outbound.create(tenant, url, events))

        if u.path.startswith("/v1/webhooks/") and u.path.endswith("/test"):  # tek webhook'a ping -> owner
            if not self._require_owner():
                return
            wid = u.path[len("/v1/webhooks/"):-len("/test")]
            return self._send(200, outbound.test(tenant, wid))

        if u.path == "/v1/billing/checkout":  # owner: bir plan için ödeme checkout'u başlat
            if not self._require_owner():
                return
            plan = str(body.get("plan") or "").strip()
            cust = body.get("customer") if isinstance(body.get("customer"), dict) else None
            try:
                co = payments.create_checkout(tenant, plan, cust)
            except payments.PaymentError as e:
                return self._send(400, {"error": str(e)})
            store.link_subscription(tenant, co["ref"])   # webhook tenant'ı bu ref'ten çözer (IDOR yok)
            store.record_admin(tenant, "billing_checkout", plan, detail=f"ref={co['ref']}")
            return self._send(200, {"checkout_url": co["url"], "ref": co["ref"], "plan": plan})

        if u.path == "/v1/check":
            output = (body.get("output") or "").strip()
            if not output:
                return self._send(400, {"error": "output_required"})
            if len(output) > MAX_TEXT:
                return self._send(413, {"error": "output_too_long", "max_chars": MAX_TEXT})
            project = body.get("project")
            fail_open = bool(body.get("fail_open", False))
            if not self._reserve(tenant, "check"):
                return
            run_soft = bool(body.get("run_soft", True)) and llm_available()
            rules = store.get_rules(tenant, project)
            res = enforce(output, rules=rules, project=project, run_soft=run_soft)
            store.record_enforcement(tenant, res.evaluated,
                                     [v.as_dict() for v in res.violations], source="api")
            return self._send(200, {
                "passed": res.passed,
                "allow": res.safe(fail_open=fail_open),   # fail-closed karar
                "checked": res.checked,
                "violations": [v.as_dict() for v in res.violations],
                "unevaluated": res.unevaluated,
                "deferred": res.deferred,
            })

        if u.path == "/v1/route/preview":  # AACRP K2: metni kategoriye ata (LLM-destekli routing önizleme)
            text = (body.get("text") or "").strip()
            labels = body.get("labels") or []
            if not text or not isinstance(labels, list) or not labels:
                return self._send(400, {"error": "text_and_labels_required"})
            if len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            if not llm_available():
                return self._send(503, {"error": "llm_unavailable"})
            if not self._reserve(tenant, "check"):
                return
            try:
                return self._send(200, routing.classify_llm(text, labels))
            except LLMError as e:
                return self._send(502, {"error": "route_failed", "detail": str(e)})
        if u.path == "/v1/taxonomy/induce":  # AACRP: otomatik kategorizasyon -> ASYNC (uzun sürer, worker işler)
            if not self._require_owner():
                return
            if not llm_available():
                return self._send(503, {"error": "llm_unavailable"})
            if store.has_pending_job(tenant, "induce"):   # eşzamanlı induce torn-state önle (dedup-enqueue)
                return self._send(200, {"status": "already_running"})
            # induce O(N) LLM yakar -> kayıt-başı 0.5 kredi (aktif hatıra tahminiyle) ENQUEUE'da rezerve et.
            # Böylece büyük vault induce'u kotaya çarpar (üst tier'a iter) + para-kaybettiren kaçak kapanır.
            if not self._reserve(tenant, "induce", store.count_memories(tenant)):
                return
            jid = store.enqueue_job(tenant, "induce", {})
            return self._send(202, {"job_id": jid, "status": "queued"})
        if u.path == "/v1/data/forget-subject":  # KVKK toplu silme (ör. tüm 3.şahıs verisi) -> owner
            if not self._require_owner():
                return
            party = str(body.get("subject_party") or "").strip()
            if party not in ("third_party", "unknown", "self"):
                return self._send(400, {"error": "invalid_subject_party"})
            return self._send(200, {"forgotten": memory.forget_by_subject(tenant, party)})
        if u.path == "/v1/import/clear":  # vault re-import öncesi: hafıza+KG temizle (owner-only)
            if not self._require_owner():
                return
            return self._send(200, {"cleared": store.clear_imported(tenant)})
        if u.path == "/v1/admin/backfill-lex":  # eski (term_hashes NULL) kayıtlara blind lexical index'i doldur (owner)
            if not self._require_owner():
                return
            return self._send(200, {"backfilled": store.backfill_lex_index(tenant)})
        if u.path == "/v1/import/obsidian":  # Obsidian vault -> hafıza + KG (owner-only)
            if not self._require_owner():
                return
            files = body.get("files")
            if not isinstance(files, list) or not files:
                return self._send(400, {"error": "files_required"})
            if len(files) > obsidian.MAX_FILES:
                return self._send(413, {"error": "too_many_files", "max": obsidian.MAX_FILES})
            if not self._storage_gate(tenant):   # storage tavanı dolu -> 402
                return
            if not self._reserve(tenant, "memory_add", len(files)):  # her not ~1 op
                return
            try:
                stats = obsidian.import_files(tenant, files, user_id=str(body.get("user_id") or "obsidian"))
            except Exception as e:
                return self._send(502, {"error": "import_failed", "detail": str(e)[:200]})
            return self._send(200, stats)

        if u.path == "/v1/memories":  # fact-memory ingest
            text = (body.get("text") or "").strip()
            facts = body.get("facts")
            user_id = body.get("user_id", "default")
            if not _valid_user(user_id):
                return self._send(400, {"error": "invalid_user_id"})
            if len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            if facts is not None:
                if not isinstance(facts, list):
                    return self._send(400, {"error": "facts_must_be_list"})
                if len(facts) > MAX_FACTS:
                    return self._send(413, {"error": "too_many_facts", "max": MAX_FACTS})
                if any(not isinstance(f, str) or len(f) > MAX_TEXT for f in facts):
                    return self._send(413, {"error": "fact_too_long_or_not_string", "max_chars": MAX_TEXT})
            if not text and not facts:
                return self._send(400, {"error": "text_or_facts_required"})
            if facts is None and not llm_available():
                return self._send(503, {"error": "llm_unavailable",
                                        "detail": "extraction icin LLM yok; ya 'facts' gonder ya hatti ayarla"})
            if not self._storage_gate(tenant):   # storage tavanı dolu -> 402 (op kotasından ayrı eksen)
                return
            if not self._reserve(tenant, "memory_add", 1):  # işlemi (extraction dahil) rezerve et
                return
            try:
                res = memory.ingest(tenant, text, user_id=user_id, facts=facts,
                                    source=body.get("source", "api"))
            except (LLMError, ValueError) as e:
                return self._send(502, {"error": "ingest_failed", "detail": str(e)})
            billing.meter(tenant, "memory_add", max(0, res["added"] - 1))  # SADECE yeni fact'ler (reinforce COGS~0, ücretsiz)
            if facts is None and text:   # ham metin -> LLM extraction çalıştı; pahalı op'u ayrı metrele
                billing.meter(tenant, "llm_extract", 1)
            if res["added"]:                          # giden webhook: yeni hafıza (best-effort, bloklamaz)
                outbound.fire(tenant, "memory.created", {"added": res["added"], "user_id": user_id, "ids": res["ids"]})
            return self._send(200, {"added": res["added"], "reinforced": res["reinforced"],
                                    "skipped": res["skipped"], "blocked": res.get("blocked", 0),
                                    "ids": res["ids"]})

        if u.path == "/v1/memories/search":  # hybrid retrieval
            query = (body.get("query") or "").strip()
            user_id = body.get("user_id", "default")
            if not _valid_user(user_id):
                return self._send(400, {"error": "invalid_user_id"})
            if not query:
                return self._send(400, {"error": "query_required"})
            if len(query) > MAX_TEXT:
                return self._send(413, {"error": "query_too_long", "max_chars": MAX_TEXT})
            if not self._reserve(tenant, "memory_search"):
                return
            top_k = max(0, min(int(body.get("top_k", 5) or 0), 100))   # clamp
            hits = memory.search(tenant, query, user_id=user_id, top_k=top_k)
            return self._send(200, {"results": hits})

        if u.path == "/v1/correct":
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "text_required"})
            if len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            if not llm_available():
                return self._send(503, {"error": "llm_unavailable",
                                        "detail": "classify icin LLM hatti yok (key veya lokal CCE_LLM_BASE_URL)"})
            if not self._reserve(tenant, "correct"):
                return
            try:
                result = compiler.ingest(text, tenant)
            except (LLMError, ValueError) as e:
                return self._send(502, {"error": "compile_failed", "detail": str(e)})
            return self._send(200, {"correction": result["correction"], "rule": result["rule"]})

        if u.path == "/v1/context":  # Context Pack üret (hafıza+kural -> taşınabilir paket)
            query = body.get("query")
            if query is not None and (not isinstance(query, str) or len(query) > MAX_TEXT):
                return self._send(400, {"error": "invalid_query"})
            user_id = body.get("user_id", "default")
            if not _valid_user(user_id):
                return self._send(400, {"error": "invalid_user_id"})
            project = body.get("project")
            if not self._reserve(tenant, "memory_search"):
                return
            pack = ctx.build_pack(
                tenant, query=query, project=project, user_id=user_id,
                token_budget=int(body.get("token_budget", 2000) or 2000),
                max_memories=int(body.get("max_memories", 20) or 20),
                max_rules=int(body.get("max_rules", 30) or 30))
            out = {"pack": pack}
            if body.get("render"):
                out["rendered"] = ctx.render_pack(pack)
            return self._send(200, out)

        if u.path == "/v1/import":  # bundle yükle -> owner-only (tüm bilgiyi değiştirir)
            if not self._require_owner():
                return
            bundle = body.get("bundle")
            if not isinstance(bundle, dict):
                return self._send(400, {"error": "bundle_required"})
            mode = body.get("mode", "merge")
            if not self._reserve(tenant, "memory_add", 1):
                return
            try:
                res = portability.import_tenant(tenant, bundle, mode=mode)
            except ValueError as e:
                return self._send(400, {"error": "import_failed", "detail": str(e)})
            billing.meter(tenant, "memory_add", max(0, res["memories"]["added"] - 1))
            return self._send(200, res)

        if u.path == "/v1/handoff":  # oturum snapshot al
            session_id = (body.get("session_id") or "").strip()
            if not session_id or len(session_id) > 128:
                return self._send(400, {"error": "session_id_required"})
            user_id = body.get("user_id", "default")
            if not _valid_user(user_id):
                return self._send(400, {"error": "invalid_user_id"})
            text = body.get("text") or ""
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            facts = body.get("facts")
            if facts is not None:
                if not isinstance(facts, list):
                    return self._send(400, {"error": "facts_must_be_list"})
                if len(facts) > MAX_FACTS:
                    return self._send(413, {"error": "too_many_facts", "max": MAX_FACTS})
                if any(not isinstance(f, str) or len(f) > MAX_TEXT for f in facts):
                    return self._send(413, {"error": "fact_too_long_or_not_string"})
            summary = body.get("summary")
            if summary is not None and (not isinstance(summary, str) or len(summary) > MAX_TEXT):
                return self._send(400, {"error": "invalid_summary"})
            if not self._reserve(tenant, "memory_add", 1):
                return
            try:
                res = handoff.snapshot(tenant, session_id, text=text or None, facts=facts,
                                       summary=summary, user_id=user_id)
            except (LLMError, ValueError) as e:
                return self._send(502, {"error": "handoff_failed", "detail": str(e)})
            # ingest ile parity: rezerve edilen 1 op dışında kalan YENİ fact'leri metele (reinforce ücretsiz)
            billing.meter(tenant, "memory_add", max(0, res["facts_added"] - 1))
            return self._send(200, res)

        if u.path == "/v1/kg":  # knowledge graph ingest (varlık+ilişki -> temporal grafik)
            text = body.get("text") or ""
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            triples = body.get("triples")
            if triples is not None:
                if not isinstance(triples, list):
                    return self._send(400, {"error": "triples_must_be_list"})
                if len(triples) > MAX_FACTS:
                    return self._send(413, {"error": "too_many_triples", "max": MAX_FACTS})
            if not text and not triples:
                return self._send(400, {"error": "text_or_triples_required"})
            if triples is None and not llm_available():
                return self._send(503, {"error": "llm_unavailable",
                                        "detail": "extraction icin LLM yok; ya 'triples' gonder ya hatti ayarla"})
            valid_at = body.get("valid_at")
            if valid_at is not None:
                try:
                    valid_at = float(valid_at)
                except (TypeError, ValueError):
                    return self._send(400, {"error": "invalid_valid_at"})
            if not self._reserve(tenant, "memory_add", 1):
                return
            try:
                res = kg.ingest(tenant, text=text, triples=triples,
                                source=str(body.get("source", "api"))[:64], valid_at=valid_at)
            except (LLMError, ValueError) as e:
                return self._send(502, {"error": "kg_ingest_failed", "detail": str(e)})
            billing.meter(tenant, "memory_add", max(0, res["added"] - 1))  # ekstra kenarlar
            if triples is None and text:   # ham metin -> LLM extraction çalıştı (memory ucuyla parity; KAÇAK kapandı)
                billing.meter(tenant, "llm_extract", 1)
            return self._send(200, res)

        if u.path == "/v1/kg/search":  # knowledge graph hybrid retrieval (+ point-in-time)
            query = (body.get("query") or "").strip()
            if not query:
                return self._send(400, {"error": "query_required"})
            if len(query) > MAX_TEXT:
                return self._send(413, {"error": "query_too_long", "max_chars": MAX_TEXT})
            as_of = body.get("as_of")
            if as_of is not None:
                try:
                    as_of = float(as_of)
                except (TypeError, ValueError):
                    return self._send(400, {"error": "invalid_as_of"})
            if not self._reserve(tenant, "memory_search"):
                return
            top_k = max(0, min(int(body.get("top_k", 5) or 0), 100))
            return self._send(200, {"results": kg.search(tenant, query, as_of=as_of, top_k=top_k)})

        if u.path == "/v1/ingest":   # ASYNC ingestion: kuyruğa al, hemen job_id dön (worker işler)
            kind = body.get("kind")
            if kind not in ("memory", "kg"):
                return self._send(400, {"error": "kind_must_be_memory_or_kg"})
            text = body.get("text") or ""
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                return self._send(413, {"error": "text_too_long", "max_chars": MAX_TEXT})
            items = body.get("facts") if kind == "memory" else body.get("triples")
            if items is not None:
                if not isinstance(items, list):
                    return self._send(400, {"error": "items_must_be_list"})
                if len(items) > MAX_FACTS:
                    return self._send(413, {"error": "too_many_items", "max": MAX_FACTS})
            user_id = body.get("user_id", "default")
            if not _valid_user(user_id):
                return self._send(400, {"error": "invalid_user_id"})
            if not text and not items:
                return self._send(400, {"error": "text_or_items_required"})
            if kind == "memory" and not self._storage_gate(tenant):   # storage tavanı dolu -> 402
                return
            if not self._reserve(tenant, "memory_add", 1):   # reserve-at-enqueue (kota öne alınır)
                return
            payload = {"text": text, "user_id": user_id, "source": str(body.get("source", "async"))[:64]}
            if kind == "memory":
                payload["facts"] = items
            else:
                payload["triples"] = items
                payload["valid_at"] = body.get("valid_at")
            jid = store.enqueue_job(tenant, kind, payload)
            return self._send(202, {"job_id": jid, "status": "queued"})

        return self._send(404, {"error": "not_found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        tenant = self._auth()
        if not tenant:
            return self._send(401, {"error": "invalid_or_missing_api_key"})
        if not _rate_ok(tenant):
            return self._send(429, {"error": "rate_limited", "limit_rpm": RATE_RPM})
        # KVKK Art.7 / GDPR Art.17 — right to erasure
        if u.path == "/v1/data":  # tenant'ın TÜM verisini sil — owner-only (RBAC)
            if not self._require_owner():
                return
            counts = store.purge_tenant(tenant)
            return self._send(200, {"purged": counts})
        if u.path.startswith("/v1/rules/"):  # org policy -> owner-only
            if not self._require_owner():
                return
            rule_id = u.path[len("/v1/rules/"):]
            n = store.delete_rule(tenant, rule_id)
            return self._send(200 if n else 404, {"deleted": n})
        if u.path.startswith("/v1/keys/"):  # key iptal -> owner-only
            if not self._require_owner():
                return
            key_id = u.path[len("/v1/keys/"):]
            n = store.revoke_api_key(tenant, key_id)
            return self._send(200 if n else 404, {"revoked": n})
        if u.path.startswith("/v1/webhooks/"):  # webhook sil -> owner-only
            if not self._require_owner():
                return
            wid = u.path[len("/v1/webhooks/"):]
            n = store.delete_webhook(tenant, wid)
            return self._send(200 if n else 404, {"deleted": n})
        if u.path.startswith("/v1/memories/"):  # forget -> her tenant key'i (kullanıcı kendi hafızası)
            mem_id = u.path[len("/v1/memories/"):]
            n = memory.forget(tenant, mem_id)
            return self._send(200 if n else 404, {"forgotten": n})
        return self._send(404, {"error": "not_found"})


def main():
    import signal
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    except (ValueError, AttributeError):
        pass  # ana thread değilse / platform desteklemiyorsa
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"[UYARI] {HOST} loopback değil — TLS sonlandıran reverse proxy ARKASINDA çalıştır (#24).")
    # CCE_INLINE_WORKER=1 -> async ingest kuyruğunu AYNI process'te bir daemon thread drenajla
    # (free-tier/tek-kutu deploy: ayrı ücretli worker'a gerek kalmaz). Üretim ölçeğinde ayrı worker tercih edilir.
    if os.environ.get("CCE_INLINE_WORKER", "").strip() in ("1", "true", "yes"):
        import threading
        import worker as _worker
        threading.Thread(target=_worker.run, kwargs={"max_idle_polls": 0}, daemon=True).start()
        print("[inline-worker] async ingest worker bu process'te başladı (daemon thread)")
    print(f"CCE API {HOST}:{PORT} (llm_available={llm_available()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        store.checkpoint()  # temiz kapanış: WAL'ı düzleştir (#23)


if __name__ == "__main__":
    main()
