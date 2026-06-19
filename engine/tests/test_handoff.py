"""test_handoff.py — Session handoff (C). key'siz: embed stub, LLM yok (graceful)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_handoff.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import embeddings  # noqa: E402
import handoff  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402


def _stub_embed(text):
    t = (text or "").lower()
    if "kahve" in t:
        return [1.0, 0.0, 0.0]
    return [0.0, 0.0, 1.0]


embeddings.embed = _stub_embed

T = "t_ho"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(T, "Ho", "k_ho")

print("== 1) snapshot (facts verilir, LLM yok) -> kalıcı fact + handoff kaydı ==")
res = handoff.snapshot(T, "sess-1", facts=["Kullanıcı kahveyi sever", "Proje Acme"],
                       summary="DB şeması bitti, sırada API var")
check(res["facts_added"] == 2, f"2 fact kalıcılaştı ({res['facts_added']})")
check(res["summary"].startswith("DB şeması"), "verilen özet saklandı")
check(store.count_memories(T) == 2, "fact'ler hafızaya yazıldı (recall edilebilir)")
h = store.get_handoff(T, "sess-1")
check(h is not None and len(h["fact_ids"]) == 2, "handoff kaydı fact_ids ile var")

print("== 2) LLM yokken özet verilmezse boş kalır (sahte özet yok) ==")
res2 = handoff.snapshot(T, "sess-2", facts=["Ek bir fact"])
check(res2["summary"] == "" and res2["llm_used"] is False, "LLM yok -> özet boş, uydurma yok")

print("== 3) resume -> özet + context pack (fact'ler pakette) ==")
r = handoff.resume(T, "sess-1", query="kahve")
check(r["found"] and r["summary"].startswith("DB şeması"), "resume özeti döndü")
check(r["pack"]["counts"]["memories"] >= 1, "resume pack'inde hafıza var")
txt = handoff.render_resume(r)
check("Kaldığımız yer" in txt and "Context Pack" in txt, "render: özet + pack birleşti")

print("== 4) olmayan oturum -> found False ==")
check(handoff.resume(T, "yok")["found"] is False, "olmayan handoff found=False")

print("== 5) tenant izolasyonu ==")
check(handoff.resume("t_other", "sess-1")["found"] is False, "başka tenant handoff görmüyor")

print("== 6) session_id zorunlu ==")
try:
    handoff.snapshot(T, "", facts=["x"])
    check(False, "boş session_id kabul edilmemeliydi")
except ValueError:
    check(True, "boş session_id reddedildi")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM HANDOFF TESTLERİ GEÇTİ")
