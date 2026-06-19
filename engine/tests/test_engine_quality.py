"""test_engine_quality.py — GERÇEK retrieval kalite kapısı + baseline (eski toy 6-sorgu/%100'ün yerine).

25+ TR bellek, 35 sorgu (çekim-eki / paraphrase / synonym / typo / akronim / çok-gold / ellipsis),
held-out split. Metrikler: recall@1/5/10 + MRR + NDCG@10. ABLATION: sparse-only (lexical/BM25) vs
hybrid (dense+lexical+RRF) — RRF/dense'in gerçekten kazandırdığını ölçer. Embedding anahtarı yoksa
hybrid SKIP (sparse her zaman CI-gate). Çalıştır: python tests/test_engine_quality.py
"""
import math
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.gettempdir()) / "cce_test_engine_quality.db"
if _tmp.exists():
    _tmp.unlink()
os.environ["CCE_DB"] = str(_tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# embedding/LLM anahtarını engine/.env'den yükle (DB anahtarlarını ASLA -> temp sqlite korunur).
# .env yoksa (CI) sessiz geç -> hybrid SKIP, sparse gate çalışır.
_envf = Path(__file__).resolve().parent.parent / ".env"
if _envf.exists():
    for _ln in _envf.read_text(encoding="utf-8").splitlines():
        _ln = _ln.strip()
        if not _ln or _ln.startswith("#") or "=" not in _ln:
            continue
        _k, _v = _ln.split("=", 1)
        _k = _k.strip()
        if _k in ("CCE_DB", "CCE_STORE_BACKEND", "CCE_RLS_DSN") or _k in os.environ:
            continue
        os.environ[_k] = _v.strip()

import embeddings  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

_REAL_EMBED = embeddings.embed   # import anında gerçek (varsa)


def _embed_available() -> bool:
    try:
        return bool(_REAL_EMBED("embedding anahtar kontrol"))
    except Exception:
        return False


# ---- veri seti (id, içerik) ----
MEMS = [
    ("p1", "İade talepleri 14 gün içinde kabul edilir"),
    ("p2", "Faturalar her ayın 1'inde kesilir"),
    ("p3", "Ürün garanti süresi 24 aydır"),
    ("p4", "Kargo bedeli 500 TL üzeri siparişlerde ücretsizdir"),
    ("f1", "Ödeme altyapısı olarak iyzico kullanılıyor"),
    ("f2", "Postgres veritabanı yedekleri her gün otomatik alınır"),
    ("f3", "Mobil uygulama hem iOS hem Android platformunda mevcut"),
    ("f4", "API anahtarı yönetim konsolundan üretilir"),
    ("f5", "Veritabanı göçü versiyonlu migration ile yapılır"),
    ("u1", "Kullanıcı arayüzde koyu tema tercih ediyor"),
    ("u2", "Müşteri bildirimleri e-posta ile almak istiyor"),
    ("u3", "Kullanıcı haftalık özet raporu istiyor"),
    ("e1", "Proje yöneticisi Ayşe Yılmaz"),
    ("e2", "Destek ekibi lideri Mehmet Demir"),
    ("e3", "Şirket merkezi İstanbul'da bulunuyor"),
    ("s1", "Aylık raporlar ayın son iş günü hazırlanır"),
    ("s2", "Haftalık retro toplantısı Cuma günleri yapılır"),
    ("s3", "Sistem bakım penceresi gece 02:00 ile 04:00 arasıdır"),
    ("t1", "Web arayüzü Next.js ile geliştirildi"),
    ("t2", "Motor Python ile yazıldı ve Render üzerinde çalışıyor"),
    ("t3", "Önbellekleme için Redis kullanılıyor"),
    ("t4", "Kimlik doğrulama JWT token ile yapılır"),
    ("m1", "Yeni çalışanlar ilk hafta oryantasyon eğitimi alır"),
    ("m2", "Toplantı notları Notion üzerinde tutulur"),
    ("m3", "Acil durumlar için nöbetçi mühendis bulunur"),
    ("m4", "Şifreler pbkdf2 ile saklanır, asla düz metin değil"),
    # --- distractor bellekler (korpusu büyüt -> top-k ayırt edici olsun; bazıları sorgularla kelime paylaşır = zorlaştırır) ---
    ("d1", "Şirket logosu mavi ve beyaz renklerden oluşur"),
    ("d2", "Ofis kahve makinesi her sabah temizlenir"),
    ("d3", "Yıllık izin hakkı 14 gündür ve devredilebilir"),
    ("d4", "Toplantı odaları takvim üzerinden rezerve edilir"),
    ("d5", "Sunucu logları 30 gün boyunca saklanır"),
    ("d6", "Müşteri memnuniyet anketi çeyrekte bir yapılır"),
    ("d7", "Geliştirme ortamı Docker ile ayağa kaldırılır"),
    ("d8", "Kod incelemesi en az bir onay gerektirir"),
    ("d9", "Tasarım dosyaları Figma üzerinde tutulur"),
    ("d10", "Faturalar PDF olarak e-posta ile gönderilir"),
    ("d11", "Sözleşmeler hukuk ekibi tarafından onaylanır"),
    ("d12", "Reklam bütçesi aylık olarak gözden geçirilir"),
    ("d13", "Sunum şablonları marka kılavuzuna uygun olmalı"),
    ("d14", "Test ortamında gerçek müşteri verisi kullanılmaz"),
    ("d15", "Hata bildirimleri öncelik etiketiyle sınıflandırılır"),
    ("d16", "Yıllık şirket pikniği yaz aylarında düzenlenir"),
    ("d17", "Çalışanlara uzaktan çalışma imkanı sunulur"),
    ("d18", "Domain kayıtları her yıl otomatik yenilenir"),
    ("d19", "Sosyal medya paylaşımları pazarlama ekibince yapılır"),
    ("d20", "Müşteri sözleşmesi KVKK aydınlatma metni içerir"),
    ("d21", "Stok seviyeleri günlük olarak güncellenir"),
    ("d22", "Yeni özellikler önce beta kullanıcılara açılır"),
    ("d23", "Sunucu CPU kullanımı izleme paneline yansır"),
    ("d24", "Eğitim videoları yardım merkezinde yayınlanır"),
    ("d25", "İş başvuruları kariyer portalından alınır"),
    ("d26", "Marka rengi olarak lime yeşili kullanılır"),
    ("d27", "Ödeme başarısız olursa müşteriye bildirim gider"),
    ("d28", "Veri merkezi Frankfurt bölgesinde konumlanır"),
    ("d29", "Günlük aktif kullanıcı sayısı panoda gösterilir"),
    ("d30", "Abonelik iptalleri ay sonunda geçerli olur"),
    ("d31", "Geri bildirim formu her sayfanın altında bulunur"),
    ("d32", "Sunucu sertifikaları Let's Encrypt ile yenilenir"),
    ("d33", "Çalışan maaşları her ayın 5'inde yatırılır"),
    ("d34", "Ürün yol haritası üç ayda bir güncellenir"),
    ("d35", "Telefon desteği hafta içi 09-18 arası verilir"),
    ("d36", "Yedek anahtarlar kasada fiziksel olarak saklanır"),
    ("d37", "Blog yazıları SEO kontrolünden geçer"),
    ("d38", "Sunucu maliyetleri bulut sağlayıcı faturasında izlenir"),
    ("d39", "Yeni sürümler önce stage ortamında denenir"),
    ("d40", "Müşteri görüşmeleri CRM sistemine kaydedilir"),
]

# (sorgu, gold-id'ler, split). tune=tuning'e açık, heldout=sadece raporlama (overfit kontrol)
GOLD = [
    ("iade süresi ne kadar", {"p1"}, "tune"),
    ("ürün garantisi kaç ay", {"p3"}, "tune"),
    ("hangi ödeme sağlayıcısı kullanılıyor", {"f1"}, "tune"),
    ("kargo ne zaman ücretsiz olur", {"p4"}, "tune"),
    ("raporlar ne zaman hazırlanıyor", {"s1"}, "tune"),          # çekim-eki rapor/raporlar
    ("faturalar ne zaman kesiliyor", {"p2"}, "tune"),
    ("yedekler ne sıklıkla alınıyor", {"f2"}, "tune"),           # yedek/yedekler
    ("retro toplantıları hangi gün", {"s2"}, "tune"),
    ("kullanıcı hangi temayı tercih ediyor", {"u1"}, "tune"),    # paraphrase
    ("müşteri bildirimleri nasıl almak istiyor", {"u2"}, "tune"),
    ("veritabanı yedekleme sıklığı nedir", {"f2"}, "tune"),      # paraphrase -> f2
    ("projeden kim sorumlu", {"e1"}, "tune"),                    # paraphrase -> yönetici
    ("şirket nerede bulunuyor", {"e3"}, "tune"),
    ("destek ekibinin lideri kim", {"e2"}, "tune"),
    ("bakım hangi saatlerde", {"s3"}, "tune"),
    ("önbellek için hangi teknoloji", {"t3"}, "tune"),           # synonym önbellek/önbellekleme
    ("API anahtarı nasıl üretilir", {"f4"}, "tune"),
    ("kimlik doğrulama nasıl yapılıyor", {"t4"}, "tune"),        # JWT
    ("mobil uygulama hangi platformlarda", {"f3"}, "tune"),
    ("motor hangi programlama dilinde", {"t2"}, "tune"),         # paraphrase -> Python
    ("web arayüzü hangi teknoloji ile yapıldı", {"t1"}, "tune"),
    ("yeni başlayanlar ilk hafta ne yapar", {"m1"}, "tune"),     # ellipsis -> oryantasyon
    ("toplantı notları nerede tutuluyor", {"m2"}, "tune"),
    ("acil durumda kime ulaşılır", {"m3"}, "tune"),              # paraphrase -> nöbetçi
    ("şifreler nasıl saklanıyor", {"m4"}, "tune"),
    # --- held-out (tuning'e kapalı; gerçek genelleme) ---
    ("migration nasıl yapılıyor", {"f5"}, "heldout"),
    ("garanti", {"p3"}, "heldout"),                              # tek kelime
    ("rapor", {"s1", "u3"}, "heldout"),                          # çok-gold (iki kayıt rapor içerir)
    ("veritabanı", {"f2", "f5"}, "heldout"),                     # çok-gold
    ("iyzco ile mi ödeme alınıyor", {"f1"}, "heldout"),         # TYPO iyzco->iyzico (sparse zorlu)
    ("haftalik ozet raporu", {"u3"}, "heldout"),                # diakritiksiz
    ("İstanbul'da mı", {"e3"}, "heldout"),
    ("gece bakım var mı", {"s3"}, "heldout"),                    # ellipsis
    ("koyu mod tercihi", {"u1"}, "heldout"),                    # synonym koyu tema/koyu mod
    ("nöbetçi mühendis", {"m3"}, "heldout"),
]


def _seed(tenant: str, embed_fn):
    for mid, content in MEMS:
        emb = embed_fn(content)
        store.add_memory({"id": f"{tenant}_{mid}", "tenant_id": tenant, "user_id": "default",
                          "content": content, "embedding": emb})


def _ndcg(ranked_ids, gold, k=10):
    dcg = 0.0
    for i, rid in enumerate(ranked_ids[:k]):
        if rid in gold:
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / ideal if ideal else 0.0


def run_eval(label: str, embed_fn, split=None) -> dict:
    embeddings.embed = embed_fn
    tenant = "t_eq_" + label
    store.upsert_tenant(tenant, label, "k_" + label)
    _seed(tenant, embed_fn)
    rows = [g for g in GOLD if split is None or g[2] == split]
    r1 = r5 = r10 = mrr = ndcg = 0.0
    for q, gold, _sp in rows:
        gold_t = {f"{tenant}_{g}" for g in gold}
        ranked = [r["id"] for r in memory.search(tenant, q, top_k=10, touch=False)]
        r1 += 1.0 if any(g in ranked[:1] for g in gold_t) else 0.0
        r5 += len(gold_t & set(ranked[:5])) / len(gold_t)
        r10 += len(gold_t & set(ranked[:10])) / len(gold_t)
        rank = next((i + 1 for i, rid in enumerate(ranked) if rid in gold_t), 0)
        mrr += 1.0 / rank if rank else 0.0
        ndcg += _ndcg(ranked, gold_t)
    n = len(rows)
    return {"n": n, "recall@1": r1 / n, "recall@5": r5 / n, "recall@10": r10 / n,
            "mrr": mrr / n, "ndcg@10": ndcg / n}


def _fmt(m):
    return (f"n={m['n']:>2}  R@1={m['recall@1']:.2f}  R@5={m['recall@5']:.2f}  "
            f"R@10={m['recall@10']:.2f}  MRR={m['mrr']:.2f}  NDCG@10={m['ndcg@10']:.2f}")


def main() -> int:
    # ---- SPARSE (her zaman; CI-gate) ----
    sparse = run_eval("sparse", lambda *a, **k: None)
    sparse_ho = run_eval("sparse_ho", lambda *a, **k: None, split="heldout")
    print("== ABLATION ==")
    print(f"SPARSE (lexical/BM25)      {_fmt(sparse)}")
    print(f"SPARSE held-out           {_fmt(sparse_ho)}")

    # ---- HYBRID (embedding anahtarı varsa) ----
    if _embed_available():
        hybrid = run_eval("hybrid", _REAL_EMBED)
        hybrid_ho = run_eval("hybrid_ho", _REAL_EMBED, split="heldout")
        print(f"HYBRID (dense+lex+RRF)     {_fmt(hybrid)}")
        print(f"HYBRID held-out           {_fmt(hybrid_ho)}")
        print(f"\nDENSE KATKISI (hybrid - sparse): R@5 {hybrid['recall@5'] - sparse['recall@5']:+.2f}, "
              f"NDCG@10 {hybrid['ndcg@10'] - sparse['ndcg@10']:+.2f}")
    else:
        print("HYBRID: SKIP (embedding anahtarı yok — sadece sparse gate)")

    # ---- CI GATE (sparse zemini; baseline'ı kilitler, regresyonda kırılır) ----
    # Baseline (66 bellek, 35 sorgu): R@1=0.71, R@5=1.00, MRR=0.85. Ayırt edici metrik = R@1/MRR
    # (R@5 küçük korpusta doygun). Gate = baseline - küçük marj.
    GATE_R1, GATE_MRR = 0.65, 0.80
    embeddings.embed = _REAL_EMBED   # diğer testlere bırakma
    if sparse["recall@1"] < GATE_R1 or sparse["mrr"] < GATE_MRR:
        print(f"\ntest_engine_quality.py: FAIL — sparse R@1={sparse['recall@1']:.2f} (gate {GATE_R1}) "
              f"MRR={sparse['mrr']:.2f} (gate {GATE_MRR})")
        return 1
    print(f"\ntest_engine_quality.py: PASS (sparse R@1={sparse['recall@1']:.2f}, MRR={sparse['mrr']:.2f}, "
          f"NDCG@10={sparse['ndcg@10']:.2f}; hybrid eval için CCE_LLM_API_KEY gerekir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
