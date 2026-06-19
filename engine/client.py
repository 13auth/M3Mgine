#!/usr/bin/env python3
"""client.py — CCE Python SDK (ince istemci, stdlib urllib; harici bağımlılık yok).

Entegrasyon: bir AI ajanı çıktıyı kullanıcıya vermeden CCEClient.allowed(...) ile
geçirir; correct(...) ile düzeltme besler. tenant = API key'inden çözülür.

    from client import CCEClient
    cce = CCEClient("http://127.0.0.1:8770", "sk_...")
    if not cce.allowed(model_output, project="Acme"):
        model_output = regenerate()      # fail-closed gate
    cce.correct("AI şunu yanlış yaptı, şöyle olmalı", project="Acme")
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class CCEError(RuntimeError):
    pass


class CCEClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
            except Exception:
                detail = {"error": e.reason}
            raise CCEError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise CCEError(f"baglanti: {e.reason} ({self.base})") from None

    # ---- enforce (stage 5) ----
    def check(self, output: str, project: str | None = None,
              run_soft: bool = True, fail_open: bool = False) -> dict:
        return self._req("POST", "/v1/check", {"output": output, "project": project,
                                               "run_soft": run_soft, "fail_open": fail_open})

    def allowed(self, output: str, project: str | None = None, fail_open: bool = False) -> bool:
        """Fail-closed kapı kararı: çıktı yayına gidebilir mi?"""
        return bool(self.check(output, project=project, fail_open=fail_open).get("allow"))

    # ---- correction (stage 1-2) ----
    def correct(self, text: str, project: str | None = None) -> dict:
        return self._req("POST", "/v1/correct", {"text": text, "project": project})

    # ---- fact-memory ----
    def remember(self, text: str | None = None, facts: list | None = None,
                 user_id: str = "default", source: str = "api") -> dict:
        return self._req("POST", "/v1/memories",
                         {"text": text or "", "facts": facts, "user_id": user_id, "source": source})

    def recall(self, query: str, user_id: str = "default", top_k: int = 5) -> list[dict]:
        return self._req("POST", "/v1/memories/search",
                         {"query": query, "user_id": user_id, "top_k": top_k}).get("results", [])

    def memories(self, user_id: str | None = None) -> list[dict]:
        q = f"?user_id={urllib.parse.quote(user_id)}" if user_id else ""
        return self._req("GET", "/v1/memories" + q).get("memories", [])

    def forget_memory(self, mem_id: str) -> dict:
        return self._req("DELETE", f"/v1/memories/{mem_id}")

    # ---- context aktarımı (A: pack, B: export/import, C: handoff) ----
    def context(self, query: str | None = None, project: str | None = None,
                user_id: str = "default", token_budget: int = 2000, render: bool = False) -> dict:
        """Context Pack: ilgili hafıza+kuralları token-bütçeli, taşınabilir pakete derle."""
        return self._req("POST", "/v1/context", {"query": query, "project": project,
                         "user_id": user_id, "token_budget": token_budget, "render": render})

    def export(self) -> dict:
        """Tenant'ın tüm bilgisini taşınabilir bundle olarak indir (owner; secret YOK)."""
        return self._req("GET", "/v1/export")

    def import_bundle(self, bundle: dict, mode: str = "merge") -> dict:
        """Bundle'ı bu tenant'a yükle (owner; erasure-güvenli). mode: merge|replace."""
        return self._req("POST", "/v1/import", {"bundle": bundle, "mode": mode})

    def handoff(self, session_id: str, text: str | None = None, facts: list | None = None,
                summary: str | None = None, user_id: str = "default") -> dict:
        """Oturum snapshot'ı: kalıcı fact + 'kaldığımız yer' özeti."""
        return self._req("POST", "/v1/handoff", {"session_id": session_id, "text": text,
                         "facts": facts, "summary": summary, "user_id": user_id})

    def resume(self, session_id: str, query: str | None = None, token_budget: int = 2000) -> dict:
        """Oturumu geri yükle: özet + Context Pack."""
        qs = {"budget": token_budget}
        if query:
            qs["query"] = query
        path = f"/v1/handoff/{urllib.parse.quote(session_id)}?" + urllib.parse.urlencode(qs)
        return self._req("GET", path)

    def handoffs(self) -> list[dict]:
        return self._req("GET", "/v1/handoffs").get("handoffs", [])

    # ---- knowledge graph (temporal) ----
    def kg_add(self, text: str | None = None, triples: list | None = None,
               source: str = "api", valid_at: float | None = None) -> dict:
        """Metinden (LLM) veya enjekte üçlülerden varlık+ilişki çıkar -> temporal grafik.
        Çelişen yeni bilgi eski kenarı otomatik geçersizler (bi-temporal)."""
        return self._req("POST", "/v1/kg", {"text": text, "triples": triples,
                         "source": source, "valid_at": valid_at})

    def kg_search(self, query: str, as_of: float | None = None, top_k: int = 5) -> list[dict]:
        """Knowledge graph hybrid retrieval. as_of verilirse DÜNYA point-in-time (o an doğru olanlar)."""
        return self._req("POST", "/v1/kg/search",
                         {"query": query, "as_of": as_of, "top_k": top_k}).get("results", [])

    def kg_neighbors(self, entity: str) -> list[dict]:
        return self._req("GET", "/v1/kg/neighbors?entity=" + urllib.parse.quote(entity)).get("neighbors", [])

    # ---- async ingestion (Faz 2) ----
    def ingest_async(self, kind: str, text: str | None = None, facts: list | None = None,
                     triples: list | None = None, user_id: str = "default",
                     source: str = "async", valid_at: float | None = None) -> dict:
        """Ağır ingest'i kuyruğa al, hemen {job_id, status} dön (worker arka planda işler)."""
        body = {"kind": kind, "text": text, "user_id": user_id, "source": source}
        if kind == "memory":
            body["facts"] = facts
        else:
            body["triples"] = triples
            body["valid_at"] = valid_at
        return self._req("POST", "/v1/ingest", body)

    def job(self, job_id: str) -> dict:
        """Async ingest job'ının durumu/sonucu (queued|running|done|error)."""
        return self._req("GET", "/v1/jobs/" + urllib.parse.quote(job_id))

    # ---- rules / measure ----
    def rules(self, project: str | None = None) -> list[dict]:
        q = f"?project={urllib.parse.quote(project)}" if project else ""
        return self._req("GET", "/v1/rules" + q).get("rules", [])

    def compliance(self, since: float = 0.0) -> list[dict]:
        return self._req("GET", f"/v1/compliance?since={since}").get("compliance", [])

    def status(self) -> dict:
        return self._req("GET", "/v1/status")

    def usage(self) -> dict:
        """plan + bu dönem kullanım/kota."""
        return self._req("GET", "/v1/usage")

    # ---- admin ----
    def delete_rule(self, rule_id: str) -> dict:
        return self._req("DELETE", f"/v1/rules/{rule_id}")

    def forget(self) -> dict:
        """KVKK/GDPR Art.17 — tüm tenant verisini sil."""
        return self._req("DELETE", "/v1/data")
