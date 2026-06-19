#!/usr/bin/env python3
"""cli.py — Correction-Compliance Engine birleşik komut satırı.

  cce init-tenant --id t_x --name "Stüdyo X" --key SECRET
  cce seed        --tenant t_x --yaml policy_rules.yaml
  cce correct     --tenant t_x "AI şunu yanlış yaptı, şöyle olmalı"
  cce check       --tenant t_x --project Acme "aday çıktı"
  cce eval        --tenant t_x --project Acme --dir "C:/.../07 Memory/Evals"
  cce rules       --tenant t_x [--project Acme]
  cce serve
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import accounts  # noqa: E402
import billing  # noqa: E402
import compiler  # noqa: E402
import context as ctx  # noqa: E402
import handoff  # noqa: E402
import kg  # noqa: E402
import memory  # noqa: E402
import portability  # noqa: E402
import store  # noqa: E402
from llm import LLMError, llm_available  # noqa: E402
from policy_engine import enforce  # noqa: E402
from evaluator import (format_regression, format_report, load_cases,  # noqa: E402
                       load_report, regression, run_eval, save_report)


def _init_tenant(a):
    store.upsert_tenant(a.id, a.name, a.key)
    print(f"[OK] tenant: {a.id} ({a.name})  api_key set")
    return 0


def _seed(a):
    n = store.seed_from_yaml(a.yaml, a.tenant)
    print(f"[OK] {n} kural seed edildi -> {a.tenant}")
    return 0


def _correct(a):
    text = " ".join(a.text).strip()
    if not text:
        sys.exit('Kullanım: cce correct --tenant t_x "<düzeltme>"')
    if not llm_available():
        print("[!] LLM hattı yok (uzak için CCE_LLM_API_KEY/API_SERVER_KEY, "
              "veya lokal CCE_LLM_BASE_URL ör. Ollama). classify yapılamaz.", file=sys.stderr)
        return 3
    try:
        res = compiler.ingest(text, a.tenant)
    except (LLMError, ValueError) as e:
        print(f"[HATA] {e}", file=sys.stderr)
        return 2
    r = res["rule"]
    print(f"[OK] correction -> rule")
    print(f"     kategori : {res['correction']['category']}  proje: {res['correction'].get('project')}")
    print(f"     kural    : {r['message']}")
    print(f"     tip      : {r['type']}  ({'yazıldı' if r.get('_written') else 'skill-fix: yazılmadı'})")
    return 0


def _check(a):
    output = " ".join(a.text).strip()
    if not output:
        sys.exit('Kullanım: cce check --tenant t_x "<çıktı>"')
    rules = store.get_rules(a.tenant, a.project)
    run_soft = (not a.hard_only) and llm_available()
    res = enforce(output, rules=rules, project=a.project, run_soft=run_soft)
    store.record_enforcement(a.tenant, res.evaluated,
                             [v.as_dict() for v in res.violations], source="cli")  # per-rule MEASURE
    allow = res.safe(fail_open=a.fail_open)  # FAIL-CLOSED: api/gate/SDK ile aynı karar
    if a.json:
        print(json.dumps({"passed": res.passed, "allow": allow,
                          "violations": [v.as_dict() for v in res.violations],
                          "unevaluated": res.unevaluated, "deferred": res.deferred},
                         ensure_ascii=False, indent=2))
    else:
        print(f"== CHECK (tenant={a.tenant}, {res.checked} kural) | allow={allow} ==")
        for v in res.violations:
            print(f"  [İHLAL/{v.type}/{v.severity}] {v.rule_id}: {v.evidence}")
        for u in res.unevaluated:
            print(f"  [DEĞERLENDİRİLEMEDİ/{u['severity']}] {u['rule_id']}: {u['reason']}")
        if allow:
            print("  [GEÇTİ]")
    return 0 if allow else 1   # CI/runtime kapısı: fail-closed


def _eval(a):
    root = Path(a.dir)
    cases = (load_cases(root / "from-corrections", "from-corrections")
             + load_cases(root / "held-out", "held-out"))
    if not cases:
        sys.exit(f"Trap bulunamadı: {root}/(from-corrections|held-out)")
    rep = run_eval(a.tenant, cases, project=a.project, record=not a.no_record)
    print(format_report(rep))
    if a.save:
        save_report(rep, a.save)
        print(f"[OK] rapor kaydedildi: {a.save}")
    if a.baseline:
        reg = regression(load_report(a.baseline), rep)
        print(format_regression(reg))
        return 0 if reg["gate_pass"] else 1
    return 0


def _rules(a):
    rs = store.get_rules(a.tenant, a.project)
    print(f"== {len(rs)} kural (tenant={a.tenant}) ==")
    for r in rs:
        prov = f" <- {r.get('correction_id')}" if r.get("correction_id") else ""
        print(f"  [{r['type']}/{r['severity']}] {r['id']}  (v{r.get('version', 1)}, {r.get('project')}){prov}")
        print(f"      {r['message'][:100]}")
    return 0


def _delete_rule(a):
    n = store.delete_rule(a.tenant, a.rule_id)
    print(f"[OK] silindi: {n} kural" if n else "[--] kural bulunamadı")
    return 0 if n else 1


def _disable_rule(a):
    n = store.disable_rule(a.tenant, a.rule_id)
    print(f"[OK] devre dışı: {n} kural" if n else "[--] kural bulunamadı")
    return 0 if n else 1


def _forget(a):
    # KVKK Art.7 / GDPR Art.17 — tenant'ın tüm verisini sil
    counts = store.purge_tenant(a.tenant)
    print(f"[OK] tenant verisi silindi: {counts}")
    return 0


def _key_create(a):
    k = accounts.create_key(a.tenant, name=a.name, role=a.role)
    print(f"[OK] key oluşturuldu: id={k['id']} role={k['role']}")
    print(f"     API KEY (SADECE ŞİMDİ GÖRÜNÜR, sakla): {k['api_key']}")
    return 0


def _key_revoke(a):
    n = store.revoke_api_key(a.tenant, a.id)
    print(f"[OK] iptal edildi" if n else "[--] key bulunamadı/zaten iptal")
    return 0 if n else 1


def _keys(a):
    ks = accounts.list_keys(a.tenant)
    print(f"== {len(ks)} key (tenant={a.tenant}) ==")
    for k in ks:
        print(f"  {k['id']}  {k['role']:<7} {'[İPTAL]' if k['revoked'] else '[aktif]'}  {k['name']}")
    return 0


def _plan(a):
    if a.set:
        if a.set not in billing.PLANS:
            print(f"[HATA] geçersiz plan. Seçenekler: {', '.join(billing.PLANS)}"); return 2
        store.set_plan(a.tenant, a.set)
        print(f"[OK] plan = {a.set}")
    q = billing.quota(a.tenant)
    lim = q["limit"] if q["limit"] is not None else "∞"
    print(f"== plan={q['plan']} | dönem={q['period']} | kullanım={q['used']}/{lim} op ==")
    return 0


def _usage(a):
    q = billing.quota(a.tenant)
    lim = q["limit"] if q["limit"] is not None else "sınırsız"
    print(f"tenant={a.tenant} plan={q['plan']} dönem={q['period']}")
    print(f"  kullanım: {q['used']} / {lim} op  (kalan: {q['remaining']})  izin: {q['allowed']}")
    return 0


def _doctor(a):
    import embeddings as _emb  # noqa: E402
    import llm as _llm  # noqa: E402
    import webhooks as _wh  # noqa: E402
    print("== cce doctor (ortam sağlık kontrolü) ==")
    try:
        store.upsert_tenant("__doctor__", "probe", "__dk__"); store.purge_tenant("__doctor__")
        print("  [OK]   DB yazılabilir")
    except Exception as e:
        print(f"  [HATA] DB: {e}")
    print(f"  [{'OK' if _llm.llm_available() else 'WARN'}] LLM: base={_llm.LLM_BASE_URL} model={_llm.LLM_MODEL} "
          f"key={'var' if _llm.has_key() else 'yok'} available={_llm.llm_available()}")
    es = _emb.available()
    print(f"  [{'OK' if es else 'WARN'}] embed: model={_emb.EMBED_MODEL} -> {'semantik retrieval' if es else 'lexical fallback'}")
    print(f"  [{'OK' if _wh.WEBHOOK_SECRET else 'WARN'}] webhook secret: {'set' if _wh.WEBHOOK_SECRET else 'YOK -> webhook reddeder'}")
    print(f"  [OK]   planlar: {', '.join(billing.PLANS)}")
    return 0


def _demo(a):
    store.upsert_tenant(a.tenant, "Demo", a.key)
    store.set_plan(a.tenant, "growth")
    n = store.seed_from_yaml(str(Path(__file__).parent / "policy_rules.yaml"), a.tenant)
    facts = ["Kullanıcı Türkçe yanıt tercih eder", "Kullanıcı Acme projesinde çalışıyor",
             "Kullanıcı kısa ve net cevap ister"]
    r = memory.ingest(a.tenant, "demo", facts=facts)
    print(f"[OK] demo tenant '{a.tenant}': {n} kural, {r['added']} memory, plan=growth, key={a.key}")
    print(f"     dene: cce recall --tenant {a.tenant} \"ne tercih eder\"")
    print(f"           cce check --tenant {a.tenant} --project Acme --hard-only \"binlerce kanal\"")
    return 0


def _link_sub(a):
    store.link_subscription(a.tenant, a.ref)
    print(f"[OK] subscription '{a.ref}' -> {a.tenant} (webhook bu eşlemeden tenant çözer)")
    return 0


def _audit_log(a):
    rows = store.get_admin_log(a.tenant)
    print(f"== admin işlem log ({len(rows)}) tenant={a.tenant} ==")
    for r in rows:
        print(f"  {r['action']:<14} {r['target']}  {r['detail'][:60]}")
    return 0


def _prune(a):
    out = store.prune_telemetry(a.days)
    print(f"[OK] retention ({a.days}g): silinen {out}")
    return 0


def _remember(a):
    text = " ".join(a.text).strip()
    if not text:
        sys.exit('Kullanım: cce remember --tenant t "<metin>"')
    if not llm_available():
        print("[!] LLM hattı yok — extraction yapılamaz (key ile çalıştır).", file=sys.stderr)
        return 3
    try:
        r = memory.ingest(a.tenant, text, user_id=a.user)
    except (LLMError, ValueError) as e:
        print(f"[HATA] {e}", file=sys.stderr); return 2
    print(f"[OK] +{r['added']} yeni, {r['reinforced']} pekiştirildi, {r['skipped']} atlandı")
    for f in r["facts"]:
        print(f"     - {f}")
    return 0


def _recall(a):
    query = " ".join(a.text).strip()
    if not query:
        sys.exit('Kullanım: cce recall --tenant t "<sorgu>"')
    hits = memory.search(a.tenant, query, user_id=a.user, top_k=a.top_k)
    print(f"== recall (tenant={a.tenant}, user={a.user}) ==")
    for h in hits:
        print(f"  [{h['score']:.2f}] {h['content']}")
    if not hits:
        print("  (hafıza boş)")
    return 0


def _memories(a):
    ms = store.get_active_memories(a.tenant, a.user)
    print(f"== {len(ms)} aktif memory (tenant={a.tenant}) ==")
    for m in ms:
        print(f"  [{m.get('salience', 1):.1f}|{m.get('access_count', 0)}x] {m['content'][:90]}")
    return 0


def _compliance(a):
    rows = store.rule_health(a.tenant, stale_days=a.stale_days)
    if not rows:
        print("(aktif kural yok)")
        return 0
    stale = [r for r in rows if r["stale"]]
    print(f"== per-rule sağlık (tenant={a.tenant}, {len(rows)} aktif kural, {len(stale)} bayat) ==")
    for r in rows:
        comp = f"{r['compliance']:.0%}" if r["compliance"] is not None else "  -"
        if r["never_fired"]:
            flag = "  ! hiç tetiklenmedi"
        elif r["stale"]:
            flag = f"  ! {r['idle_days']}g atış yok"
        else:
            flag = ""
        print(f"  {comp:>4}  [{r['severity']}] {r['rule_id']}  "
              f"({r['passed']}/{r['checks']} geçti, {r['violations']} ihlal){flag}")
    return 0


def _context(a):
    query = " ".join(a.query).strip() if a.query else None
    pack = ctx.build_pack(a.tenant, query=query, project=a.project, user_id=a.user,
                          token_budget=a.budget)
    if a.render:
        print(ctx.render_pack(pack))
    else:
        c = pack["counts"]
        print(f"== context pack (tenant={a.tenant}) ~{pack['token_estimate']} tok "
              f"{'[KIRPILDI]' if pack['truncated'] else ''} ==")
        print(f"  hafıza: {c['memories']}/{c['memories_total']}  kural: {c['rules']}/{c['rules_total']}")
        print(ctx.render_pack(pack))
    return 0


def _export(a):
    bundle = portability.export_tenant(a.tenant)
    data = json.dumps(bundle, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(data, encoding="utf-8")
        print(f"[OK] export -> {a.out}  {bundle['counts']}")
    else:
        print(data)
    return 0


def _import(a):
    try:
        bundle = json.loads(Path(a.infile).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[HATA] bundle okunamadı: {e}", file=sys.stderr); return 2
    try:
        res = portability.import_tenant(a.tenant, bundle, mode=a.mode)
    except ValueError as e:
        print(f"[HATA] {e}", file=sys.stderr); return 2
    print(f"[OK] import ({a.mode}) -> {a.tenant}")
    print(f"     memory: +{res['memories']['added']} (bloklu {res['memories']['blocked']}), "
          f"kural: +{res['rules']}, correction: +{res['corrections']}, tombstone: +{res['tombstones']}")
    return 0


def _handoff(a):
    text = " ".join(a.text).strip() if a.text else None
    try:
        res = handoff.snapshot(a.tenant, a.session, text=text, summary=a.summary, user_id=a.user)
    except (LLMError, ValueError) as e:
        print(f"[HATA] {e}", file=sys.stderr); return 2
    print(f"[OK] handoff '{a.session}': +{res['facts_added']} fact "
          f"({res['facts_reinforced']} pekiştirildi), özet={'var' if res['summary'] else 'yok'}")
    if res["summary"]:
        print("--- özet ---"); print(res["summary"])
    return 0


def _resume(a):
    query = " ".join(a.query).strip() if a.query else None
    res = handoff.resume(a.tenant, a.session, query=query, token_budget=a.budget)
    if not res.get("found"):
        print(f"[--] handoff yok: {a.session}"); return 1
    print(handoff.render_resume(res))
    return 0


def _kg_add(a):
    text = " ".join(a.text).strip()
    if not text:
        sys.exit('Kullanım: cce kg-add --tenant t "<metin>"')
    if not llm_available():
        print("[!] LLM hattı yok — varlık/ilişki çıkarımı yapılamaz (key ile çalıştır).",
              file=sys.stderr)
        return 3
    try:
        r = kg.ingest(a.tenant, text=text)
    except (LLMError, ValueError) as e:
        print(f"[HATA] {e}", file=sys.stderr); return 2
    print(f"[OK] kg: +{r['added']} kenar, {r['invalidated']} geçersizlendi, "
          f"+{r['entities_created']} varlık ({r['triples']} üçlü)")
    return 0


def _kg_search(a):
    query = " ".join(a.query).strip()
    if not query:
        sys.exit('Kullanım: cce kg-search --tenant t "<sorgu>"')
    hits = kg.search(a.tenant, query, as_of=a.as_of, top_k=a.top_k)
    label = f" @as_of={a.as_of}" if a.as_of is not None else ""
    print(f"== kg-search (tenant={a.tenant}){label} ==")
    for h in hits:
        print(f"  [{h['score']:.2f}] {h['fact']}  ({h['subject']} -{h['predicate']}-> {h['object']})")
    if not hits:
        print("  (grafik boş / eşleşme yok)")
    return 0


def _kg_neighbors(a):
    nb = kg.neighbors(a.tenant, a.entity)
    print(f"== {a.entity} komşuları (tenant={a.tenant}) ==")
    for n in nb:
        print(f"  -{n['predicate']}-> {n['object']}   ({n['fact']})")
    if not nb:
        print("  (varlık yok / kenar yok)")
    return 0


def _serve(a):
    import api
    api.main()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="cce", description="Correction-Compliance Engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-tenant"); p.add_argument("--id", required=True)
    p.add_argument("--name", default=""); p.add_argument("--key", required=True)
    p.set_defaults(func=_init_tenant)

    p = sub.add_parser("seed"); p.add_argument("--tenant", required=True)
    p.add_argument("--yaml", required=True); p.set_defaults(func=_seed)

    p = sub.add_parser("correct"); p.add_argument("--tenant", required=True)
    p.add_argument("text", nargs="*"); p.set_defaults(func=_correct)

    p = sub.add_parser("check"); p.add_argument("--tenant", required=True)
    p.add_argument("--project", default=None); p.add_argument("--hard-only", action="store_true")
    p.add_argument("--fail-open", dest="fail_open", action="store_true")
    p.add_argument("--json", action="store_true"); p.add_argument("text", nargs="*")
    p.set_defaults(func=_check)

    p = sub.add_parser("eval"); p.add_argument("--tenant", required=True)
    p.add_argument("--project", default=None); p.add_argument("--dir", required=True)
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--save", default=None, help="raporu JSON'a kaydet (baseline için)")
    p.add_argument("--baseline", default=None, help="baseline JSON ile kıyasla (regresyon kapısı)")
    p.set_defaults(func=_eval)

    p = sub.add_parser("rules"); p.add_argument("--tenant", required=True)
    p.add_argument("--project", default=None); p.set_defaults(func=_rules)

    p = sub.add_parser("delete-rule"); p.add_argument("--tenant", required=True)
    p.add_argument("--rule-id", dest="rule_id", required=True); p.set_defaults(func=_delete_rule)

    p = sub.add_parser("disable-rule"); p.add_argument("--tenant", required=True)
    p.add_argument("--rule-id", dest="rule_id", required=True); p.set_defaults(func=_disable_rule)

    p = sub.add_parser("forget", help="KVKK/GDPR: tenant'ın tüm verisini sil")
    p.add_argument("--tenant", required=True); p.set_defaults(func=_forget)

    p = sub.add_parser("remember", help="metinden fact çıkar ve hafızaya yaz")
    p.add_argument("--tenant", required=True); p.add_argument("--user", default="default")
    p.add_argument("text", nargs="*"); p.set_defaults(func=_remember)

    p = sub.add_parser("recall", help="hafızadan hybrid getir")
    p.add_argument("--tenant", required=True); p.add_argument("--user", default="default")
    p.add_argument("--top-k", dest="top_k", type=int, default=5); p.add_argument("text", nargs="*")
    p.set_defaults(func=_recall)

    p = sub.add_parser("memories", help="aktif memory listesi")
    p.add_argument("--tenant", required=True); p.add_argument("--user", default="default")
    p.set_defaults(func=_memories)

    p = sub.add_parser("compliance", help="per-rule sağlık + uyum (bayat/hiç-tetiklenmemiş kural tespiti)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--stale-days", dest="stale_days", type=int, default=7)
    p.set_defaults(func=_compliance)

    p = sub.add_parser("key-create", help="yeni API key üret (raw bir kez görünür)")
    p.add_argument("--tenant", required=True); p.add_argument("--name", default="key")
    p.add_argument("--role", default="member"); p.set_defaults(func=_key_create)

    p = sub.add_parser("key-revoke", help="API key iptal et")
    p.add_argument("--tenant", required=True); p.add_argument("--id", required=True)
    p.set_defaults(func=_key_revoke)

    p = sub.add_parser("keys", help="API key listesi")
    p.add_argument("--tenant", required=True); p.set_defaults(func=_keys)

    p = sub.add_parser("plan", help="planı göster/değiştir")
    p.add_argument("--tenant", required=True); p.add_argument("--set", default=None)
    p.set_defaults(func=_plan)

    p = sub.add_parser("usage", help="bu dönem kullanım/kota")
    p.add_argument("--tenant", required=True); p.set_defaults(func=_usage)

    p = sub.add_parser("doctor", help="ortam sağlık kontrolü (DB/LLM/embed/webhook/plan)")
    p.set_defaults(func=_doctor)

    p = sub.add_parser("demo", help="tek komutla deneme tenant'ı (kural+memory+plan)")
    p.add_argument("--tenant", default="demo"); p.add_argument("--key", default="demo_key")
    p.set_defaults(func=_demo)

    p = sub.add_parser("link-subscription", help="ödeme sağlayıcı ref'ini tenant'a eşle (checkout)")
    p.add_argument("--tenant", required=True); p.add_argument("--ref", required=True)
    p.set_defaults(func=_link_sub)

    p = sub.add_parser("audit-log", help="admin işlem geçmişi (sil/purge)")
    p.add_argument("--tenant", required=True); p.set_defaults(func=_audit_log)

    p = sub.add_parser("prune", help="retention: eski telemetriyi sil")
    p.add_argument("--tenant", required=True)  # imzayı tutması için; prune global
    p.add_argument("--days", type=int, default=90); p.set_defaults(func=_prune)

    p = sub.add_parser("context", help="Context Pack üret (hafıza+kural -> taşınabilir paket)")
    p.add_argument("--tenant", required=True); p.add_argument("--project", default=None)
    p.add_argument("--user", default="default"); p.add_argument("--budget", type=int, default=2000)
    p.add_argument("--render", action="store_true"); p.add_argument("query", nargs="*")
    p.set_defaults(func=_context)

    p = sub.add_parser("export", help="tenant bilgisini taşınabilir bundle'a çıkar (secret YOK)")
    p.add_argument("--tenant", required=True); p.add_argument("--out", default=None)
    p.set_defaults(func=_export)

    p = sub.add_parser("import", help="bundle'ı tenant'a yükle (erasure-güvenli)")
    p.add_argument("--tenant", required=True); p.add_argument("--in", dest="infile", required=True)
    p.add_argument("--mode", default="merge", choices=["merge", "replace"])
    p.set_defaults(func=_import)

    p = sub.add_parser("handoff", help="oturum snapshot al (kalıcı fact + özet)")
    p.add_argument("--tenant", required=True); p.add_argument("--session", required=True)
    p.add_argument("--user", default="default"); p.add_argument("--summary", default=None)
    p.add_argument("text", nargs="*"); p.set_defaults(func=_handoff)

    p = sub.add_parser("resume", help="oturumu geri yükle (özet + context pack)")
    p.add_argument("--tenant", required=True); p.add_argument("--session", required=True)
    p.add_argument("--budget", type=int, default=2000); p.add_argument("query", nargs="*")
    p.set_defaults(func=_resume)

    p = sub.add_parser("kg-add", help="metinden varlık+ilişki çıkar -> temporal knowledge graph")
    p.add_argument("--tenant", required=True); p.add_argument("text", nargs="*")
    p.set_defaults(func=_kg_add)

    p = sub.add_parser("kg-search", help="knowledge graph hybrid retrieval (+ --as-of point-in-time)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--as-of", dest="as_of", type=float, default=None)
    p.add_argument("--top-k", dest="top_k", type=int, default=5); p.add_argument("query", nargs="*")
    p.set_defaults(func=_kg_search)

    p = sub.add_parser("kg-neighbors", help="bir varlığın güncel kenarları (graph gezinme)")
    p.add_argument("--tenant", required=True); p.add_argument("--entity", required=True)
    p.set_defaults(func=_kg_neighbors)

    p = sub.add_parser("serve"); p.set_defaults(func=_serve)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
