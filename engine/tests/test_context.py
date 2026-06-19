"""test_context.py — Context Pack üretimi (A). key'siz: embed stub."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_context.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import context  # noqa: E402
import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


def _stub_embed(text):
    t = (text or "").lower()
    if "kahve" in t:
        return [1.0, 0.0, 0.0]
    if "istanbul" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


embeddings.embed = _stub_embed

T = "t_ctx"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "Ctx", "k_ctx")
# kurallar: 1 hard (high), 1 soft (critical), 1 düşük (low)
store.add_rule({"id": "r-hard", "tenant_id": T, "project": "Acme", "type": "hard",
                "severity": "high", "message": "binlerce kanal yasak",
                "forbid_patterns": ["binlerce kanal"]})
store.add_rule({"id": "r-soft", "tenant_id": T, "project": "Acme", "type": "soft",
                "severity": "critical", "message": "icerik saglayici imasi yasak",
                "rule": "uygulama kanal sunmaz"})
store.add_rule({"id": "r-low", "tenant_id": T, "project": "Acme", "type": "soft",
                "severity": "low", "message": "ufak stil notu"})
memory.ingest(T, "x", facts=["Kullanıcı kahveyi sever", "Kullanıcı İstanbul'da yaşıyor"])

print("== 1) build_pack: hafıza + kural derlenir ==")
p = context.build_pack(T, query="kahve", project="Acme", token_budget=2000)
check(p["counts"]["rules"] >= 1 and p["counts"]["memories"] >= 1, f"pack dolu ({p['counts']})")
check(any("kahve" in m["content"].lower() for m in p["memories"]), "sorguya ilgili memory geldi")
check(p.get("hash") and p.get("pack_version") == 1, "pack hash + version var")

print("== 2) kural sıralaması: önem (critical/hard) üste ==")
ids = [r["id"] for r in p["rules"]]
check(ids and ids[0] in ("r-soft", "r-hard"), f"yüksek-önem kural önde ({ids})")
check(ids[-1] == "r-low" if "r-low" in ids else True, "düşük-önem kural sonda")

print("== 3) render: LLM'e enjekte edilebilir metin ==")
txt = context.render_pack(p)
check("Context Pack" in txt and "kurallar" in txt.lower(), "render başlıkları var")
check("kahve" in txt.lower(), "render memory içeriyor")

print("== 4) token bütçesi -> truncation ==")
small = context.build_pack(T, query="kahve", project="Acme", token_budget=3)
check(small["truncated"], "küçük bütçe truncated=True")
check(small["counts"]["rules"] <= p["counts"]["rules"], "küçük bütçede daha az/eşit kural")

print("== 5) egress scrub: kural mesajı VE pattern'lerdeki secret temizlenir ==")
store.add_rule({"id": "r-secret", "tenant_id": T, "project": "SEC", "type": "hard",
                "severity": "high", "message": "kullan api_key=SUPERGIZLI123 sakın",
                "forbid_patterns": ["token=PATSECRET99999999999"]})
ps = context.build_pack(T, project="SEC", token_budget=2000)
joined = context.render_pack(ps)
check("SUPERGIZLI123" not in joined, "mesajdaki secret render'da yok")
check("PATSECRET99999999999" not in joined, "pattern'deki secret render'da yok")
check("[REDACTED_SECRET]" in joined, "secret redact edildi")

print("== 6) tenant izolasyonu ==")
piso = context.build_pack("t_other", token_budget=2000)
check(piso["counts"]["memories"] == 0 and piso["counts"]["rules"] == 0, "başka tenant boş pack")

print("== 7) sorgusuz pack: salience'a göre hafıza ==")
pq = context.build_pack(T, project="Acme", token_budget=2000)
check(pq["counts"]["memories"] >= 1, "sorgusuz da hafıza geldi")

print("== 8) convergence: pack echo (query/project) egress scrub ==")
pe = context.build_pack(T, query="parolam token=QLEAK1234567890ABCDEF nedir",
                        project="token=PLEAK1234567890ABCD", token_budget=2000)
blob = str(pe) + context.render_pack(pe)
check("QLEAK" not in blob, "query echo'su scrub'landı (pack + render)")
check("PLEAK" not in blob, "project echo'su scrub'landı (render '_proje:' dahil)")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM CONTEXT-PACK TESTLERİ GEÇTİ")
