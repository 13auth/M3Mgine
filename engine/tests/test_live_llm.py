"""test_live_llm.py — LLM gerektiren yolları GERÇEK modelle doğrula (lokal Ollama).
Çalıştırmadan önce env: CCE_LLM_BASE_URL=http://127.0.0.1:11434/v1, CCE_LLM_MODEL=qwen2.5:3b
Küçük model varyansına karşı: net-kesim assert'ler sıkı; nüanslı olanlar raporlanır."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_live.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))

import compiler  # noqa: E402
import detector  # noqa: E402
import store  # noqa: E402
from llm import LLM_BASE_URL, LLM_MODEL, call_model, has_key  # noqa: E402
from policy_engine import enforce  # noqa: E402

TENANT = "t_live"
FAILS = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def soft(cond, msg):  # nüanslı: raporla, fail etme
    print(("  ok   " if cond else "  ~    ") + msg + ("" if cond else "  (küçük model varyansı)"))


print(f"== LLM hattı: {LLM_BASE_URL} model={LLM_MODEL} key={has_key()} ==")
ping = call_model("Sadece tek kelime 'pong' yaz, başka hiçbir şey yazma.", "ping", max_tokens=10)
check(bool(ping and ping.strip()), f"call_model canlı yanıt verdi: '{ping.strip()[:40]}'")

print("== 1) classify (canlı) ==")
data = compiler.classify("Acme app store görseline gerçek film posteri koydun; "
                         "markasız placeholder kullanmalıydın.")
check(isinstance(data, dict) and data.get("category"), f"classify dict döndü, category={data.get('category')}")
check(bool(data.get("reusable_rule")), f"taşınabilir kural çıktı: {str(data.get('reusable_rule'))[:70]}")

print("== 2) ingest (canlı classify -> rule -> store) ==")
store.upsert_tenant(TENANT, "Live Test", "k_live")
out = compiler.ingest("Acme store metninde 'binlerce kanal' yazdın; içerik vaadi verme, "
                      "uygulama sadece kullanıcının kendi kaynağını oynatır.", TENANT)
check(out["rule"].get("_written"), "ingest -> kural store'a yazıldı")
check(len(store.get_rules(TENANT)) >= 1, "store'da kural var")

print("== 3) soft rule ENFORCE (canlı LLM judge) ==")
soft_rule = [{"id": "no-provider", "tenant_id": TENANT, "project": None, "type": "soft",
              "severity": "high", "source": "test", "message": "içerik sağlayıcı iması yasak",
              "rule": "Çıktı, uygulamanın kendisinin kanal/film/yayın SUNDUĞUNU ima ediyorsa İHLAL'dir. "
                      "Sadece kullanıcının kendi kaynağını oynatan araç olarak konumlandırıyorsa ihlal yok.",
              "forbid_patterns": [], "require_patterns": []}]
rb = enforce("Uygulamamız binlerce canlı kanal ve en yeni filmleri size sunar!",
             rules=soft_rule, run_soft=True)
rg = enforce("Uygulama, kendi M3U/Xtream kaynağını ekleyip oynatmanı sağlayan bir araçtır.",
             rules=soft_rule, run_soft=True)
check(not rb.passed, f"soft judge KÖTÜ çıktıda ihlal buldu ({len(rb.violations)})")
soft(rg.passed, "soft judge temiz çıktıyı geçirdi")

print("== 4) detector K1 (canlı lokal-model doğrulama) ==")
v_corr = detector.llm_verify("Hayır, öyle değil; bunu bir daha yapma, kısa yaz.", "Çok uzun bir cevap...")
v_norm = detector.llm_verify("Teşekkürler, harika oldu.", "Bir cevap.")
check(v_corr is True, "K1 düzeltmeyi 'EVET' olarak doğruladı")
soft(v_norm is False, "K1 normal mesajı 'HAYIR' dedi")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: CANLI LLM YOLLARI DOĞRULANDI")
