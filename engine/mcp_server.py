#!/usr/bin/env python3
"""mcp_server.py — 13auth MCP sunucusu (Claude Code / Cursor / Codex / Cline / Windsurf).

İki taşıma da TEK koddan:
  stdio  : `python mcp_server.py`            -> lokal; key CCE_API_KEY env'inden
  http   : `python mcp_server.py --http`     -> remote; key her istekte Authorization header'ından
                                                 (çok-kiracılı; yoksa CCE_API_KEY fallback)

Araçlar mevcut REST API'yi (client.py / CCEClient) sarar. Tek harici bağımlılık: `mcp`.
Kur:  pip install mcp     (ya da: pip install '.[mcp]')
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from client import CCEClient, CCEError  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("HATA: 'mcp' paketi gerekli -> pip install mcp\n")
    sys.exit(1)

# API tabanı (üretim Render API'si; ileride api.13auth.com'a yönlendirilebilir)
BASE = os.environ.get("CCE_BASE", "http://localhost:8000").rstrip("/")

mcp = FastMCP("13auth", instructions=(
    "13auth: AI ajanları için hosted hafıza + uyum-zorlama. "
    "Kullanıcı bilgisini kalıcı kılmak için 'remember', geçmişi getirmek için 'recall', "
    "bir çıktıyı marka/politika kurallarına karşı denetlemek için 'enforce_check' kullan. "
    "Bilgi grafiği için kg_search/kg_add."
))


def _key() -> str:
    """HTTP modunda istek-başı Authorization header; yoksa env (stdio / tek-kiracı)."""
    try:
        from mcp.server.fastmcp.server import get_http_headers
        h = get_http_headers() or {}
        auth = h.get("authorization") or h.get("x-api-key") or ""
        if auth:
            return auth.removeprefix("Bearer ").removeprefix("bearer ").strip()
    except Exception:
        pass
    return os.environ.get("CCE_API_KEY", "")


def _client() -> CCEClient:
    key = _key()
    if not key:
        raise CCEError("API key yok: CCE_API_KEY env'ini ayarla (stdio) ya da Authorization header gönder (http).")
    return CCEClient(BASE, key)


@mcp.tool()
def enforce_check(output: str, project: str | None = None) -> dict:
    """Bir AI çıktısını tenant'ın kurallarına karşı denetle (marka/politika ihlali var mı).
    Dönüş: allow (geçer mi), violations (ihlaller). Üretmeden ÖNCE kendi çıktını buradan geçir."""
    return _client().check(output, project=project)


@mcp.tool()
def remember(text: str | None = None, facts: list[str] | None = None, user_id: str = "default") -> dict:
    """Kalıcı hafızaya bilgi yaz. Serbest metin ('text') ya da hazır maddeler ('facts')."""
    return _client().remember(text=text, facts=facts, user_id=user_id)


@mcp.tool()
def recall(query: str, user_id: str = "default", top_k: int = 5) -> list[dict]:
    """Hafızadan ilgili bilgileri getir (hibrit arama)."""
    return _client().recall(query, user_id=user_id, top_k=top_k)


@mcp.tool()
def add_correction(text: str, project: str | None = None) -> dict:
    """Bir düzeltmeyi kalıcı, zorlanabilir bir kurala derle (enforce-loop). Örn: 'asla X deme'."""
    return _client().correct(text, project=project)


@mcp.tool()
def kg_add(text: str) -> dict:
    """Metinden varlık+ilişki çıkarıp temporal knowledge graph'a ekle."""
    return _client().kg_add(text=text)


@mcp.tool()
def kg_search(query: str, top_k: int = 5) -> list[dict]:
    """Knowledge graph'ta ara (varlık/ilişki, güncel doğru)."""
    return _client().kg_search(query, top_k=top_k)


@mcp.tool()
def context_pack(query: str | None = None, project: str | None = None) -> dict:
    """İlgili hafıza + aktif kuralları taşınabilir bir 'context pack'e derle (prompt'a enjekte edilir)."""
    return _client().context(query=query, project=project)


def main() -> None:
    if "--http" in sys.argv:
        host = os.environ.get("CCE_MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("PORT") or os.environ.get("CCE_MCP_PORT", "8765"))
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
