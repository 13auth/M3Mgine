"""test_hardening.py — audit bulgularının düzeltmelerini doğrular (key'siz, LLM çağırmaz)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_harden.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)

ENG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENG))
sys.path.insert(0, str(ENG / "integrations"))

import compiler  # noqa: E402
import store  # noqa: E402
import runtime_gate  # noqa: E402
from policy_engine import enforce  # noqa: E402
from redact import scrub_secrets  # noqa: E402

TENANT = "t_h"
FAILS = []


def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        FAILS.append(m)


store.upsert_tenant(TENANT, "Harden", "k_h")

print("== soft rule fail-CLOSED (LLM yokken yüksek-önemli soft -> blokla) ==")
store.add_rule({"id": "soft_high", "tenant_id": TENANT, "project": None, "type": "soft",
                "severity": "high", "source": "t", "message": "m", "rule": "asla X yapma",
                "forbid_patterns": [], "require_patterns": []})
rules = store.get_rules(TENANT)
res = enforce("herhangi bir çıktı", rules=rules, run_soft=False)  # soft değerlendirilemedi
check(res.passed, "ihlal yok (passed True)")
check(not res.safe(fail_open=False), "ama safe()=False (değerlendirilemeyen yüksek soft)")
check(res.safe(fail_open=True), "fail_open=True ise safe True")
g = runtime_gate.gate("herhangi bir çıktı", TENANT, run_soft=False, record=False)
check(g["allow"] is False and g["blocked_reason"] == "unevaluated_high_severity",
      f"gate FAIL-CLOSED blokladı ({g.get('blocked_reason')})")

print("== #2: YÜKSEK soft kural ASLA deferred'a düşmez (fail-closed baypası yok) ==")
high_only = [r for r in rules if r["id"] == "soft_high"]
res_h = enforce("alakasiz metin zzz", rules=high_only, run_soft=True, soft_relevance_min=2.0)
check(not res_h.deferred, "yüksek soft kural deferred'a düşmedi (must_run)")

print("== MEDIUM soft kural relevans/bütçe ile ERTELENİR, bloklamaz ==")
med = [{"id": "soft_med", "tenant_id": TENANT, "project": None, "type": "soft",
        "severity": "medium", "source": "t", "message": "m2", "rule": "bir şey",
        "forbid_patterns": [], "require_patterns": []}]
res2 = enforce("alakasiz metin zzz", rules=med, run_soft=True, soft_relevance_min=2.0)
check(res2.deferred and not res2.unevaluated, "medium kural deferred'a düştü (LLM çağrılmadı)")
check(res2.safe(fail_open=False), "deferred medium safe()'i bozmaz")

print("== #1: dedup HARD kuralı SOFT'a ÇEVİRMEZ (type-aware) ==")
hard_cls = {"category": "project", "project": "KX", "what_was_wrong": "w",
            "correct_behavior": "c", "reusable_rule": "Aynı kural metni", "severity": "high",
            "tags": [], "forbid_terms": ["yasakkelime"]}
soft_cls = dict(hard_cls, forbid_terms=[])  # AYNI reusable_rule ama forbid yok -> soft
compiler.ingest("h", TENANT, classification=hard_cls)
compiler.ingest("s", TENANT, classification=soft_cls)
kx = [r for r in store.get_rules(TENANT) if r.get("project") == "KX"]
hard_kx = [r for r in kx if r["type"] == "hard"]
check(hard_kx and "yasakkelime" in str(hard_kx[0]["forbid_patterns"]),
      f"HARD kural korundu (type flip yok) — {len(kx)} kural, hard={len(hard_kx)}")
rh = enforce("içinde yasakkelime geçen çıktı", rules=hard_kx, run_soft=False)
check(not rh.passed, "hard kural hâlâ deterministik yakalıyor (demote olmadı)")

print("== #6: disabled kural re-ingest ile reaktive OLMAZ ==")
did = hard_kx[0]["id"]
store.disable_rule(TENANT, did)
compiler.ingest("h", TENANT, classification=hard_cls)  # aynı -> reaktive etmemeli
still = [r for r in store.get_rules(TENANT, active_only=False) if r["id"] == did]
check(still and still[0]["status"] == "disabled", "disabled yapışkan kaldı (sessiz reaktivasyon yok)")

print("== DEDUP: aynı correction iki kez -> tek kural, version++ ==")
cls = {"category": "preference", "project": None, "what_was_wrong": "uzun yazdı",
       "correct_behavior": "kısa", "reusable_rule": "Yanıtları kısa tut", "severity": "medium",
       "tags": [], "forbid_terms": []}
compiler.ingest("x", TENANT, classification=cls)
compiler.ingest("x", TENANT, classification=cls)  # tekrar
pref_rules = [r for r in store.get_rules(TENANT) if r["message"] == "Yanıtları kısa tut"]
check(len(pref_rules) == 1, f"tek kural (kopya yok) — bulundu {len(pref_rules)}")
check(pref_rules and pref_rules[0]["version"] >= 2, f"version arttı (v{pref_rules[0]['version'] if pref_rules else '?'})")
check(pref_rules and pref_rules[0].get("correction_id"), "provenance: correction_id bağlı")

print("== MEASURE: gerçek per-rule oran (checks denominator) ==")
store.record_enforcement(TENANT, [{"rule_id": "rX", "passed": True}, {"rule_id": "rX", "passed": False}],
                         [{"rule_id": "rX", "evidence": "e"}], source="runtime")
comp = {c["rule_id"]: c for c in store.compliance_by_rule(TENANT)}
check("rX" in comp and comp["rX"]["checks"] == 2, "checks=2 (denominator var)")
check(comp.get("rX", {}).get("compliance") == 0.5, f"oran=0.5 (rate hesaplanıyor: {comp.get('rX',{}).get('compliance')})")

print("== Secret scrub enforce yolunda da (paylaşılan redact) ==")
red, found = scrub_secrets("anahtar password: hunter2secret123 burada")
check(found and "hunter2secret123" not in red, "enforce judge'a giden metin scrub'lanır")

print("== severity casing 'Critical' -> normalize + fail-closed bloklar ==")
cls_crit = {"category": "preference", "project": None, "what_was_wrong": "w", "correct_behavior": "c",
            "reusable_rule": "Kritik soft kural metni", "severity": "Critical", "tags": [], "forbid_terms": []}
compiler.ingest("c1", TENANT, classification=cls_crit)
crit = [r for r in store.get_rules(TENANT) if r["message"] == "Kritik soft kural metni"]
check(crit and crit[0]["severity"] == "critical", f"'Critical'->'critical' normalize ({crit[0]['severity'] if crit else '?'})")
res_c = enforce("alakasiz", rules=crit, run_soft=False)
check(not res_c.safe(fail_open=False), "kritik soft LLM yokken fail-closed BLOKLAR (casing baypası yok)")

print("== defansif: ham 'HIGH' (büyük harf) rule dict deferred'a düşmez ==")
raw_high = [{"id": "rh", "tenant_id": TENANT, "project": None, "type": "soft", "severity": "HIGH",
             "source": "t", "message": "m", "rule": "x", "forbid_patterns": [], "require_patterns": []}]
res_rh = enforce("alakasiz zzz", rules=raw_high, run_soft=True, soft_relevance_min=2.0)
check(not res_rh.deferred, "'HIGH' soft kural must_run'a alındı (deferred değil)")

print("== severity monotonik (high, low ile re-ingest'te düşmez) ==")
hi = {"category": "project", "project": "MX", "what_was_wrong": "w", "correct_behavior": "c",
      "reusable_rule": "Mono kural", "severity": "high", "tags": [], "forbid_terms": ["zzyy"]}
compiler.ingest("h", TENANT, classification=hi)
compiler.ingest("l", TENANT, classification=dict(hi, severity="low"))
mono = [r for r in store.get_rules(TENANT) if r.get("project") == "MX"]
check(mono and mono[0]["severity"] == "high", f"severity high kaldı: {mono[0]['severity'] if mono else '?'}")

print("== r4 CRITICAL: non-canonical type 'Soft'/'SEMANTIC' sessizce DÜŞMEZ ==")
bad_type = [{"id": "bt", "tenant_id": TENANT, "project": None, "type": "Soft", "severity": "critical",
             "source": "t", "message": "m", "rule": "asla içerik ima etme", "forbid_patterns": [], "require_patterns": []}]
res_bt = enforce("binlerce kanal", rules=bad_type, run_soft=False)
check(res_bt.unevaluated and not res_bt.safe(fail_open=False),
      "type='Soft' kural unevaluated(blocking) + safe False (sessiz allow yok)")
store.add_rule({"id": "bt2", "tenant_id": TENANT, "project": None, "type": "SEMANTIC", "severity": "high",
                "source": "t", "message": "m2", "rule": "x", "forbid_patterns": [], "require_patterns": []})
got = [r for r in store.get_rules(TENANT) if r["id"] == "bt2"]
check(got and got[0]["type"] == "soft", f"store type'ı canonical'e indirdi: {got[0]['type'] if got else '?'}")

print("== project case-insensitive (Acme yazıldı, Acme sorgulandı) ==")
store.add_rule({"id": "pj", "tenant_id": TENANT, "project": "Acme", "type": "hard", "severity": "high",
                "source": "t", "message": "pm", "rule": "x", "forbid_patterns": ["gizlikelime"], "require_patterns": []})
check(any(r["id"] == "pj" for r in store.get_rules(TENANT, project="Acme")),
      "Acme kuralı Acme sorgusunda bulundu (sessiz drop yok)")

print("== patternsiz HARD kural fail-CLOSED (unevaluated, safe False) ==")
ph = [{"id": "ph", "tenant_id": TENANT, "project": None, "type": "hard", "severity": "high",
       "source": "t", "message": "m", "rule": "x", "forbid_patterns": [], "require_patterns": []}]
res_ph = enforce("herhangi", rules=ph, run_soft=False)
check(res_ph.unevaluated and not res_ph.safe(fail_open=False),
      "patternsiz hard kural unevaluated+blocking (fail-open değil)")

print("== severity store'da canonicalize (CRITICAL -> critical) ==")
store.add_rule({"id": "sv", "tenant_id": TENANT, "project": None, "type": "soft", "severity": "CRITICAL",
                "source": "t", "message": "svm", "rule": "x", "forbid_patterns": [], "require_patterns": []})
got_sv = [r for r in store.get_rules(TENANT) if r["id"] == "sv"]
check(got_sv and got_sv[0]["severity"] == "critical", f"store severity canonicalize: {got_sv[0]['severity'] if got_sv else '?'}")

print("== Türkçe İ/ı project (İÇERİK yazıldı, içerik sorgulandı) ==")
store.add_rule({"id": "tr", "tenant_id": TENANT, "project": "İÇERİK", "type": "hard", "severity": "high",
                "source": "t", "message": "trm", "rule": "x", "forbid_patterns": ["SECRET_DATA"], "require_patterns": []})
check(any(r["id"] == "tr" for r in store.get_rules(TENANT, project="içerik")),
      "İÇERİK kuralı 'içerik' sorgusunda bulundu (Türkçe-güvenli fold)")

print("== her-zaman-eşleşen require ('^') fail-CLOSED ==")
am = [{"id": "am", "tenant_id": TENANT, "project": None, "type": "hard", "severity": "critical",
       "source": "t", "message": "m", "rule": "x", "forbid_patterns": [], "require_patterns": ["^"]}]
res_am = enforce("herhangi bir çıktı", rules=am, run_soft=False)
check(res_am.unevaluated and not res_am.safe(fail_open=False),
      "require='^' kuralı unevaluated+blocking (her çıktıyı geçirmiyor)")

print("== LLM read-timeout -> LLMError (enforce çökmez) ==")
import llm as _llm  # noqa: E402
_orig = _llm.urllib.request.urlopen
_llm.urllib.request.urlopen = (lambda *a, **k: (_ for _ in ()).throw(TimeoutError("read timed out")))
try:
    raised = False
    try:
        _llm.call_model("s", "u")
    except _llm.LLMError:
        raised = True
    check(raised, "timeout LLMError'a dönüştü (ham TimeoutError değil)")
finally:
    _llm.urllib.request.urlopen = _orig

print("== KVKK Art.17: tenant verisini sil ==")
counts = store.purge_tenant(TENANT)
check(len(store.get_rules(TENANT)) == 0, "purge sonrası kural yok")
check(store.tenant_by_key("k_h") is None, "tenant silindi (key çözülmüyor)")

print()
if FAILS:
    print(f"SONUÇ: {len(FAILS)} BAŞARISIZ"); sys.exit(1)
print("SONUÇ: TÜM HARDENING TESTLERİ GEÇTİ")
