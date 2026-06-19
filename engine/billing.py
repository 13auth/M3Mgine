#!/usr/bin/env python3
"""billing.py — abonelik planları + usage metering + kota enforcement (Faz 2).

Ürünü "araç"tan "abonelik SaaS"ına çevirir. Ödeme SAĞLAYICISI (Polar/iyzico)
ayrı bir webhook entegrasyonu; buradaki mantık (plan, metering, kota) bizim ve
tamamen lokal/test edilebilir. Metering birimi: memory-operation.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store  # noqa: E402

# WRITE-AXIS MODEL (kesin tasarım, 2026-06): TEK faturalanan eksen = yazma-kredisi ('op'). SADECE YAZMA
# sayılır; OKUMA bedava/cömert (search=0.2 cüzi, vitrin "bedava", RPM ile frenli); SAKLAMA cömert fair-use
# (yazma limitinin ~2.5-3x'i, "pratikte sınırsız" pazarla). reinforce/delete = BEDAVA. Pitch: "Krediyle yaz,
# bedavaya oku, cömert sakla — ham metin pahalı, hazır fact ucuz."
# Free KÜÇÜK (tadımlık, hard-stop) + 14-gün Pro-trial. 'mem' = fair-use saklama tavanı.
# KURAL: storage tavanı = yazma*~2.5-3x (Indie 2.5x, Pro/Business 3x).
PLANS: dict[str, dict] = {
    "free":       {"label": "Free",       "ops": 2_000,   "mem": 1_000,     "price_try": 0},
    "solo":       {"label": "Kişisel",    "ops": 25_000,  "mem": 60_000,    "price_try": 349},
    "starter":    {"label": "Indie",      "ops": 90_000,  "mem": 225_000,   "price_try": 990},
    "growth":     {"label": "Pro",        "ops": 300_000, "mem": 900_000,   "price_try": 3990},
    "pro":        {"label": "Business",   "ops": 800_000, "mem": 2_400_000, "price_try": 12990},
    "enterprise": {"label": "Enterprise", "ops": None,    "mem": None,      "price_try": None},
}

# metering: op türü -> kredi maliyeti. add per-FACT (worker added). multi-fact kaçağı kapalı.
# llm_extract = ham metin->fact LLM çağrısı; pahalı ($0.000435 ~ 36x base) -> AĞIR (40) ki bu yolu kullanan
# kendi maliyetini ödesin. memory_reinforce=0 (COGS~0, salience++ SQL UPDATE; müşteriyi over-charge etme).
# induce = taxonomy auto-kategorizasyon; O(N) LLM -> kayıt-başı 0.5 kredi (en ucuz tier'da bile break-even
# üstü; büyük vault induce'u doğal olarak üst tier'a iter). search=0.2 (cüzi, vitrin "bedava").
OP_COST: dict[str, float] = {
    "memory_add": 1.0,
    "memory_reinforce": 0.0,
    "memory_search": 0.2,
    "check": 1.0,
    "correct": 5.0,
    "llm_extract": 40.0,
    "induce": 0.5,
}

# limit-aşımı birim fiyatları (maliyet ÜSTÜ, KDV hariç TL). Aşımda hard-block YERİNE otomatik top-up
# (opt-in, ödeme onaylı) önerilir; free hard-stop. storage caydırıcı (kâr merkezi değil; asıl koruma cap).
OVERAGE: dict[str, float] = {"op_per_1k_try": 12.0, "storage_per_gb_month_try": 50.0}

# FX: maliyet USD, gelir TL. breakeven_kur(plan) = o planın max-kullanıcı (full op + full storage) sıfır-marj kuru.
CALIB_KUR = 46.32
_USD_ADD = 0.000012         # 1 op (fact-store/embed) base COGS; extraction llm_extract ile ayrı
_USD_STORE_MO = 0.0000105   # 1 saklanan hatıra/ay COGS (USD)


def breakeven_kur(plan: str) -> float | None:
    """O planın MAX-kullanıcı (full op + full storage) net=0 olduğu USD/TRY kuru. Üstüne çıkınca
    worst-case kullanıcı zarar ettirir -> fiyat revizyonu/kur-endeksleme sinyali. None=sınırsız/ücretsiz."""
    p = PLANS.get(plan, PLANS["free"])
    ops, mem, price = p.get("ops"), p.get("mem"), p.get("price_try")
    if not ops or not mem or not price:
        return None
    matrah = price / 1.20
    iyzico = price * 0.0399 + 0.25
    cogs_usd = ops * _USD_ADD + mem * _USD_STORE_MO
    return round((matrah - iyzico) / cogs_usd, 1) if cogs_usd else None


def current_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def plan_limit(plan: str):
    return PLANS.get(plan, PLANS["free"])["ops"]


def storage_limit(plan: str):
    """Plana göre AKTİF hatıra (storage) tavanı. None -> sınırsız (enterprise)."""
    return PLANS.get(plan, PLANS["free"]).get("mem")


def storage_quota(tenant_id: str) -> dict:
    """Storage (stok) kotası: o an aktif hatıra sayısı vs tavan. op kotasından AYRI eksen —
    op tek-seferlik yanar, storage sürekli tahakkuk eder (Neon). Tavan dolunca yeni add 402."""
    plan = store.get_plan(tenant_id)
    used = store.count_memories(tenant_id)          # tüm aktif hatıra (3.şahıs dahil; hepsi yer kaplar)
    limit = storage_limit(plan)
    return {
        "used": used, "limit": limit,
        "allowed": (limit is None or used < limit),
        "remaining": (None if limit is None else max(0, limit - used)),
    }


def storage_ok(tenant_id: str) -> bool:
    """Yeni add kabul edilebilir mi (storage tavanı dolmadı mı)."""
    return storage_quota(tenant_id)["allowed"]


def quota(tenant_id: str) -> dict:
    plan = store.get_plan(tenant_id)
    used = store.get_usage(tenant_id, current_period())
    limit = plan_limit(plan)
    return {
        "plan": plan, "period": current_period(), "used": round(used, 2), "limit": limit,
        "allowed": (limit is None or used < limit),
        "remaining": (None if limit is None else max(0.0, round(limit - used, 2))),
        "price_try": PLANS.get(plan, PLANS["free"]).get("price_try"),
        "storage": storage_quota(tenant_id),   # stok ekseni (aktif hatıra/tavan)
    }


def meter(tenant_id: str, op_type: str, n: int = 1) -> float:
    cost = OP_COST.get(op_type, 1.0) * max(0, n)
    if cost:
        store.add_usage(tenant_id, current_period(), cost)
    return cost


def reserve(tenant_id: str, op_type: str, n: int = 1) -> bool:
    """ATOMİK pre-debit: kota varsa cost'u şimdi düş, True. Yoksa False (429).
    check-then-act yarışını ve over-quota tek-op sızıntısını kapatır (atomik kota işlemi)."""
    cost = OP_COST.get(op_type, 1.0) * max(0, n)
    plan = store.get_plan(tenant_id)
    return store.try_consume(tenant_id, current_period(), cost, plan_limit(plan))


def charge(tenant_id: str, op_type: str, n: int = 1) -> tuple[bool, dict]:
    """(geri-uyum) atomik reserve + quota döndür. Aşımda metering yok."""
    ok = reserve(tenant_id, op_type, n)
    return ok, quota(tenant_id)


KDV_RATE = 0.20   # TR KDV; plan price_try KDV-DAHİL (matrah=price/1.20), overage KDV-HARİÇ


def build_invoice(tenant_id: str, period: str | None = None) -> dict:
    """Dönem fatura/kullanım-özeti (matrah + KDV + genel toplam). Hesaplama-anında, ÖN İZLEME
    niteliğinde — kalıcı/immutable e-fatura kaydı + GİB/e-arşiv entegrasyonu ayrı adım (bkz roadmap).
    Plan fiyatı KDV-DAHİL gösterilir (matrah=price/1.20); op-aşımı (overage) KDV-HARİÇ birim (OVERAGE).
    Storage hard-cap (faturalanmaz). free hard-stop -> overage olmaz."""
    period = period or current_period()
    plan = store.get_plan(tenant_id)
    p = PLANS.get(plan, PLANS["free"])
    price = p.get("price_try") or 0                      # KDV dahil görünen plan fiyatı
    ops_limit = p.get("ops")
    used = store.get_usage(tenant_id, period)
    over_ops = 0.0 if ops_limit is None else max(0.0, used - ops_limit)
    overage_net = round((over_ops / 1000.0) * OVERAGE["op_per_1k_try"], 2)   # KDV hariç
    base_net = round(price / (1.0 + KDV_RATE), 2)        # plan fiyatının matrahı
    matrah = round(base_net + overage_net, 2)
    kdv = round(matrah * KDV_RATE, 2)
    total = round(matrah + kdv, 2)
    return {
        "tenant": tenant_id, "period": period, "currency": "TRY",
        "plan": plan, "plan_label": p.get("label"),
        "base_price_try": price,                          # KDV dahil
        "ops_used": round(used, 2), "ops_included": ops_limit, "ops_overage": round(over_ops, 2),
        "overage_net_try": overage_net,
        "storage_used": store.count_memories(tenant_id), "storage_included": p.get("mem"),
        "matrah_try": matrah, "kdv_rate": KDV_RATE, "kdv_try": kdv, "total_try": total,
        "preview": True,   # kalıcı e-fatura değil; tahakkuk önizlemesi
    }


def upgrade_hint(plan: str) -> dict:
    """Kota aşımı (402) yanıtına eklenir: bir üst plan + op-aşımı (top-up) birim fiyatı + checkout yolu.
    Top-up GERÇEK tahsilatı ödeme entegrasyonuna bağlıdır (opt-in, ödeme onaylı); bu yapısal öneridir."""
    order = list(PLANS.keys())
    i = order.index(plan) if plan in order else 0
    nxt = order[i + 1] if i + 1 < len(order) else None
    hint = {
        "current_plan": plan,
        "overage_per_1k_try": OVERAGE["op_per_1k_try"],   # KDV hariç; top-up (opt-in)
        "checkout_path": "/v1/billing/checkout",
    }
    if nxt:
        np = PLANS[nxt]
        hint.update(suggested_plan=nxt, suggested_plan_label=np.get("label"),
                    suggested_price_try=np.get("price_try"), suggested_ops=np.get("ops"))
    return hint
