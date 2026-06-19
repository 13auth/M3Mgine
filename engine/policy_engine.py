#!/usr/bin/env python3
"""policy_engine.py — Correction-Compliance Engine v0 (stage 5 ENFORCE).

Kimsenin ürünleştirmediği parça: correction'dan türeyen kuralı, bir AI
çıktısı üretilmeden ÖNCE runtime'da dayat. "Fact olarak hatırla + umut et"
değil; gatekeeper.

- hard kural: regex forbid/require, deterministik, sıfır LLM, %100 trafikte.
- soft kural: semantik; LLM judge kuralın niyetine göre İHLAL var/yok der.
  LLM hattı erişilemezse soft kural ATLANIR (engine çökmez) ve raporlanır.

API:
  enforce(output, project=None, run_soft=True) -> EnforceResult
CLI:
  python policy_engine.py check --project Acme "aday çıktı metni"
  exit 0 = temiz | 1 = ihlal var | 2 = motor hatası
  (yani CI/runtime kapısı olarak doğrudan kullanılır)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import LLMError, call_model, has_key  # noqa: E402
from redact import scrub_secrets  # noqa: E402

JUDGE_VOTES = max(1, int(os.environ.get("CCE_JUDGE_VOTES", "1")))  # panel: tek/çok judge
JUDGE_RETRIES = max(0, int(os.environ.get("CCE_JUDGE_RETRIES", "1")))  # format-fail'de katı uyarıyla yeniden dene
_SEV_SET = {"low", "medium", "high", "critical"}


def _sev(rule: dict) -> str:
    """Severity'i canonical'e indir; TANINMAYAN -> 'critical' (fail-closed). Güvenlik:
    'High'/'Critical'/typo gatekeeper'ı baypas etmesin."""
    s = str(rule.get("severity") or "").strip().lower()
    return s if s in _SEV_SET else "critical"


def _norm_type(rule: dict) -> str:
    """Type'ı canonical'e indir; TANINMAYAN -> 'soft' (fail-closed). Güvenlik:
    'Soft'/'SEMANTIC'/'' enforce'ta ne hard ne soft listesine girmeyip sessizce
    DÜŞMESİN; soft sayılır -> değerlendirilemezse unevaluated(blocking) olur."""
    t = str(rule.get("type") or "soft").strip().lower()
    return t if t in ("hard", "soft") else "soft"

try:
    import yaml
except ImportError:
    sys.exit("PyYAML gerekli: pip install pyyaml")

RULES_PATH = Path(os.environ.get("POLICY_RULES", Path(__file__).parent / "policy_rules.yaml"))

# FP-disiplin prompt: eval_enforce.py ile DOĞRULANDI (45 vaka × 5 tekrar = 225 değerlendirme,
# held-out dahil): eski kısa prompt %95 (FP %7) -> bu %100 (FP %0, FN %0, format-fail %0),
# gemini-2.5-flash-lite. Asıl açık tırnak/olumsuzlama/meta tetik kelimelerde FAZLA bloklamaydı.
JUDGE_SYS = (
    "Sen tarafsız ve titiz bir politika denetçisisin. KURAL'ın, AI ÇIKTISI tarafından "
    "ihlal edilip edilmediğine karar ver.\n\n"
    "TEMEL AYRIM — şunlar İHLAL DEĞİLDİR:\n"
    "- Yasak ifadeyi TIRNAK içinde/alıntılayarak ama onaylamadan anmak.\n"
    "- Yasak şeyi OLUMSUZLAMAK veya REDDETMEK ('garanti veremem', 'bunu söyleyemem').\n"
    "- Yasak konuyu META olarak tartışmak ('... demek istemem ama').\n"
    "Yalnızca çıktının KENDİSİ kuralı fiilen çiğneyen bir iddia/eylem içeriyorsa İHLAL'dir.\n"
    "'İma' da ihlaldir: dolaylı ama gerçek bir iddia varsa yakala. Kanıt yoksa violation=false.\n\n"
    "ÇIKTI BİÇİMİ: SADECE tek satır JSON, başka metin/markdown YOK:\n"
    '{"violation": true|false, "evidence": "kısa somut kanıt"}\n\n'
    "Örnekler:\n"
    "KURAL: Çıktı kâr garantisi ima ediyorsa ihlaldir.\n"
    "ÇIKTI: Sana kâr garantisi veremem; her yatırım risklidir.\n"
    '{"violation": false, "evidence": "garanti reddediliyor"}\n\n'
    "KURAL: Çıktı kâr garantisi ima ediyorsa ihlaldir.\n"
    "ÇIKTI: Resmi olarak garanti diyemem ama bu coin kesin 10x yapar.\n"
    '{"violation": true, "evidence": "\'kesin 10x\' örtük garanti"}\n\n'
    "KURAL: Çıktı 'en iyi/rakipsiz' gibi abartı içeriyorsa ihlaldir.\n"
    "ÇIKTI: 'Piyasanın en iyisi' demek istemem ama işimizi iyi yapıyoruz.\n"
    '{"violation": false, "evidence": "abartı reddediliyor, alıntı"}\n\n'
    "KURAL: Çıktı kullanıcıya hakaret içeriyorsa ihlaldir.\n"
    "ÇIKTI: 'Bu soru aptalca' diye düşünebilirsin ama hiç de değil.\n"
    '{"violation": false, "evidence": "alıntı; kullanıcıya hakaret yok"}'
)


@dataclass
class Violation:
    rule_id: str
    type: str
    severity: str
    evidence: str
    message: str

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "type": self.type, "severity": self.severity,
                "evidence": self.evidence, "message": self.message.strip()}


@dataclass
class EnforceResult:
    violations: list[Violation] = field(default_factory=list)
    evaluated: list[dict] = field(default_factory=list)     # {rule_id, passed} fiilen çalışan her kural
    unevaluated: list[dict] = field(default_factory=list)   # {rule_id, severity, reason} ÇALIŞMASI gereken ama olamayan soft (LLM yok/disabled)
    deferred: list[str] = field(default_factory=list)       # relevans/bütçe ile bilerek atlanan soft
    checked: int = 0

    @property
    def passed(self) -> bool:
        """Sadece bulunan ihlallere bakar (geriye dönük anlam)."""
        return not self.violations

    @property
    def skipped_soft(self) -> list[str]:
        """Geriye dönük görüntü: hem değerlendirilemeyen hem ertelenen soft kurallar."""
        return [f"{u['rule_id']} ({u['reason']})" for u in self.unevaluated] + list(self.deferred)

    def blocking_unevaluated(self, severities=("high", "critical")) -> list[dict]:
        return [u for u in self.unevaluated if u.get("severity") in severities]

    def safe(self, fail_open: bool = False, severities=("high", "critical")) -> bool:
        """FAIL-CLOSED karar: ihlal varsa VEYA yüksek-önemli bir soft kural
        değerlendirilemediyse (LLM çöktü) güvenli değildir. Güvenlik tasarımı."""
        if self.violations:
            return False
        if not fail_open and self.blocking_unevaluated(severities):
            return False
        return True


def load_rules(project: str | None = None) -> list[dict]:
    if not RULES_PATH.exists():
        sys.exit(f"Kural dosyası yok: {RULES_PATH}")
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8")) or {}
    rules = data.get("rules", [])
    if project:
        # Türkçe-güvenli fold (store._fold ile aynı)
        def _fold(s):
            s = "" if s is None else str(s)
            return s.replace("İ", "i").replace("I", "i").replace("ı", "i").strip().casefold()
        pk = _fold(project)
        rules = [r for r in rules if r.get("project") is None or _fold(r.get("project")) == pk]
    return rules


MAX_SCAN = 10_000  # ReDoS/aşırı-tarama azaltma: regex'i ilk N karaktere uygula (#3: 50k->10k)
# catastrophic backtracking sezgisi: niceleyici-grup içinde +/* VEYA alternation (|),
# ardından + veya *  -> (a+)+ , (.*)* , (a|a)+ , (ab|a)+ , (a|aa)+ hepsini yakalar (#3).
# NOT: heuristik tam değildir; üretimde gerçek timeout için 'regex' (timeout=) önerilir.
_REDOS = re.compile(r"\([^)]*[+*|][^)]*\)\s*[+*]")


def _pattern_ok(pattern: str) -> bool:
    """Desen güvenli ve derlenebilir mi? Bozuk veya ReDoS-riskli ise reddet."""
    if _REDOS.search(pattern):
        return False
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _safe_search(pattern: str, text: str):
    try:
        return re.search(pattern, text[:MAX_SCAN], re.IGNORECASE | re.UNICODE)
    except re.error:
        return None


def _always_satisfied(pattern: str) -> bool:
    """require için: boş string'i bile eşleştiren desen ('', '^', '.*') DAİMA sağlanır ->
    anlamsız (her çıktıyı geçirir). Böyle bir require kuralı 'enforce edilemez' sayılır."""
    try:
        return re.search(pattern, "") is not None
    except re.error:
        return False


def _check_hard(output: str, rule: dict) -> list[Violation]:
    out = []
    for pat in rule.get("forbid_patterns", []):
        if not _pattern_ok(pat):
            continue  # geçersiz/ReDoS-riskli desen: atla (yanlış yakalamadansa hiç)
        m = _safe_search(pat, output)
        if m:
            out.append(Violation(rule["id"], "hard", _sev(rule),
                                 evidence=f"yasak ifade: '{m.group(0)}'",
                                 message=rule.get("message", "")))
    for pat in rule.get("require_patterns", []):
        if not _pattern_ok(pat):
            continue
        if not _safe_search(pat, output):
            out.append(Violation(rule["id"], "hard", _sev(rule),
                                 evidence=f"zorunlu ifade eksik: /{pat}/",
                                 message=rule.get("message", "")))
    return out


_TOK = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", re.UNICODE)


def _relevance(output: str, rule: dict) -> float:
    """Soft kuralı output'a karşı çalıştırmaya değer mi? Önce SEMANTİK (embedding-cosine);
    embed hattı yoksa lexical token-overlap fallback. Sadece pre-filter RANKING'i; yüksek/
    kritik kurallar zaten must_run, o yüzden bu skor güvenlik kararına etki etmez."""
    try:
        import embeddings as _emb  # lazy: embed yoksa cache'leyip sessizce lexical'e döner
        ov = _emb.embed(output)
        if ov is not None:
            rv = _emb.embed((rule.get("rule", "") + " " + rule.get("message", "")).strip())
            if rv is not None:
                return max(0.0, _emb.cosine(ov, rv))
    except Exception:
        pass
    rt = (rule.get("rule", "") + " " + rule.get("message", "")).lower()
    rtok = {t for t in _TOK.findall(rt) if len(t) > 3}
    if not rtok:
        return 1.0  # boş kural -> elenmesin, judge karar versin
    otok = {t for t in _TOK.findall(output.lower()) if len(t) > 3}
    return len(rtok & otok) / len(rtok)


def _judge_once(output: str, rule: dict) -> tuple[bool, str]:
    # secret/PII LLM'e gitmesin (audit bulgusu): output'u scrub'la
    safe_output, _ = scrub_secrets(output)
    base_user = f"KURAL:\n{rule.get('rule', rule.get('message', ''))}\n\nAI ÇIKTISI:\n{safe_output}"
    user, last_raw = base_user, ""
    # JUDGE_RETRIES kez biçim hatasında KATI uyarıyla yeniden dene; iyi modelde format-fail ~0
    # ama ucuz sigorta (eval_enforce ile doğrulandı). Tükenirse LLMError -> enforce'ta
    # unevaluated(blocking) = fail-closed (sessiz fail-open DEĞİL).
    for _ in range(JUDGE_RETRIES + 1):
        last_raw = call_model(JUDGE_SYS, user, max_tokens=300)
        m = re.search(r"\{.*\}", last_raw, re.DOTALL)
        if m:
            try:
                verdict = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                verdict = None
            if isinstance(verdict, dict) and "violation" in verdict:
                return bool(verdict.get("violation")), verdict.get("evidence", "(judge gerekçe vermedi)")
        user = base_user + ('\n\n[UYARI] Önceki yanıt geçerli JSON değildi. SADECE şunu '
                            'döndür: {"violation": true|false, "evidence": "..."}')
    raise LLMError(f"judge JSON döndürmedi ({JUDGE_RETRIES + 1} deneme): {last_raw[:200]}")


def _check_soft(output: str, rule: dict) -> Violation | None:
    """Tek veya panel (CCE_JUDGE_VOTES) judge; panelde çoğunluk oyu ile karar."""
    votes, evidences = 0, []
    for _ in range(JUDGE_VOTES):
        viol, ev = _judge_once(output, rule)
        if viol:
            votes += 1
            evidences.append(ev)
    if votes * 2 >= JUDGE_VOTES:  # çoğunluk VEYA beraberlik -> fail-closed
        return Violation(rule["id"], "soft", _sev(rule),
                         evidence=(evidences[0] if evidences else "(judge)") +
                                  (f" [{votes}/{JUDGE_VOTES} oy]" if JUDGE_VOTES > 1 else ""),
                         message=rule.get("message", ""))
    return None


def enforce(output: str, rules: list[dict] | None = None, project: str | None = None,
            run_soft: bool = True, max_soft_rules: int = 8,
            soft_relevance_min: float = 0.0, max_blocking_rules: int = 50) -> EnforceResult:
    """Hard kuralların hepsi çalışır (ucuz). Soft kurallar ölçekte pahalı (kural başına
    1 LLM judge) -> relevansa göre top-K seç, gerisini atla. max_soft_rules=0 => sınırsız."""
    if rules is None:
        rules = load_rules(project)
    res = EnforceResult()
    # type fail-closed normalize: hiçbir kural iki listeden de düşmesin (r4 CRITICAL)
    hard = [r for r in rules if _norm_type(r) == "hard"]
    soft = [r for r in rules if _norm_type(r) == "soft"]

    for rule in hard:
        res.checked += 1
        # patternsiz / geçersiz / her-zaman-eşleşen HARD kural fail-OPEN olmasın:
        # anlamlı bir şey yoksa unevaluated(blocking) -> safe() yüksek/kritikte bloklar
        usable_forbid = [p for p in rule.get("forbid_patterns", []) if _pattern_ok(p)]
        usable_require = [p for p in rule.get("require_patterns", [])
                          if _pattern_ok(p) and not _always_satisfied(p)]
        if not usable_forbid and not usable_require:
            res.unevaluated.append({"rule_id": rule["id"], "severity": _sev(rule),
                                    "reason": "hard_no_meaningful_patterns"})
            continue
        vs = _check_hard(output, rule)
        res.violations.extend(vs)
        res.evaluated.append({"rule_id": rule["id"], "passed": not vs})

    if not run_soft:
        # soft kapalı: bunlar DEĞERLENDİRİLEMEDİ -> unevaluated (fail-closed bunlara bakar)
        for r in soft:
            res.unevaluated.append({"rule_id": r["id"], "severity": _sev(r),
                                    "reason": "soft_disabled"})
        return res

    # soft pre-filter: YÜKSEK/KRİTİK soft kurallar ASLA sessizce atlanmaz.
    BLOCKING = ("high", "critical")
    must_run_all = [r for r in soft if _sev(r) in BLOCKING]
    budgetable = [r for r in soft if _sev(r) not in BLOCKING]
    selected = []
    # must_run da sınırsız LLM maliyeti olmasın (#3): relevansa göre sırala, bütçe üstü
    # ERTELENMEZ -> unevaluated(blocking) olur ki safe()/fail-closed yine BLOKLASIN.
    must_scored = sorted(((_relevance(output, r), r) for r in must_run_all), key=lambda x: -x[0])
    for i, (_score, r) in enumerate(must_scored):
        if max_blocking_rules and i >= max_blocking_rules:
            res.unevaluated.append({"rule_id": r["id"], "severity": _sev(r),
                                    "reason": "blocking_budget_exceeded"})
        else:
            selected.append(r)
    scored = sorted(((_relevance(output, r), r) for r in budgetable), key=lambda x: -x[0])
    for score, r in scored:
        if score < soft_relevance_min or (max_soft_rules and len(selected) >= max_soft_rules):
            res.deferred.append(r["id"])
        else:
            selected.append(r)

    for rule in selected:
        res.checked += 1
        try:
            v = _check_soft(output, rule)
            res.evaluated.append({"rule_id": rule["id"], "passed": v is None})
            if v:
                res.violations.append(v)
        except (LLMError, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            # beklenen judge/LLM hataları -> DEĞERLENDİRİLEMEDİ (fail-open DEĞİL): unevaluated(blocking)
            # ki safe() yine bloklasın; api 500 ile patlamasın. Beklenmeyen hata türü propagate olur
            # (gerçek programlama bug'ı maskelenmesin). runtime gate yine fail-closed yakalar.
            reason = ("llm_error: " if isinstance(e, LLMError) else "judge_error: ") + str(e)
            res.unevaluated.append({"rule_id": rule["id"], "severity": _sev(rule), "reason": reason})
    return res


def _cmd_check(args) -> int:
    output = " ".join(args.text).strip()
    if not output:
        sys.exit('Kullanım: policy_engine.py check --project Acme "<çıktı>"')
    run_soft = not args.hard_only
    res = enforce(output, project=args.project, run_soft=run_soft)
    allow = res.safe(fail_open=args.fail_open)  # FAIL-CLOSED (api/gate/SDK ile aynı karar)

    if args.json:
        print(json.dumps({"passed": res.passed, "allow": allow, "checked": res.checked,
                          "violations": [v.as_dict() for v in res.violations],
                          "unevaluated": res.unevaluated, "deferred": res.deferred},
                         ensure_ascii=False, indent=2))
    else:
        print(f"== POLICY CHECK (project={args.project or 'ALL'}, {res.checked} kural) | allow={allow} ==")
        for v in res.violations:
            print(f"  [İHLAL/{v.type}/{v.severity}] {v.rule_id}")
            print(f"      kanıt : {v.evidence}")
        for u in res.unevaluated:
            print(f"  [DEĞERLENDİRİLEMEDİ/{u['severity']}] {u['rule_id']}: {u['reason']}")
        if allow:
            print("  [GEÇTİ]")
    return 0 if allow else 1   # fail-closed exit kodu


def main():
    ap = argparse.ArgumentParser(description="Correction-Compliance enforce motoru")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="bir çıktıyı kurallara karşı denetle")
    c.add_argument("text", nargs="*")
    c.add_argument("--project", default=None)
    c.add_argument("--hard-only", action="store_true", help="soft (LLM) kuralları atla")
    c.add_argument("--fail-open", dest="fail_open", action="store_true")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=_cmd_check)
    args = ap.parse_args()
    try:
        sys.exit(args.func(args))
    except SystemExit:
        raise
    except Exception as e:  # motor hatası -> exit 2 (gate "fail-closed" davranabilir)
        print(f"[MOTOR HATASI] {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
