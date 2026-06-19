# Katkı (Contributing)

13auth / M3Mgine'e katkı için teşekkürler. Kısa kurallar:

## Başlamadan
- Büyük bir değişiklik düşünüyorsan önce bir **issue** aç, yaklaşımı konuşalım.
- Küçük düzeltmeler (bug, doküman, test) için doğrudan PR açabilirsin.

## Geliştirme
```bash
cd engine
pip install -r requirements.txt
python tests/run_ci.py     # tüm doğrulama yeşil olmalı (offline + e2e)
```
- Mevcut kod stiline uy (yorum yoğunluğu, isimlendirme, idiom).
- Davranış değiştiren her PR **test** ekler/günceller; `run_ci.py` yeşil kalmalı.
- Sırları (anahtar/token/parola) **asla** commit etme — `.env` gitignore'lu; `.env.example` şablonunu kullan.

## Lisans & CLA
- Tüm katkılar [Apache-2.0](LICENSE) altında kabul edilir.
- İlk PR'ında [CLA](CLA.md)'yı kabul ettiğini belirt:
  > I have read the CLA and I agree to its terms for this and all my future contributions to this Project.

## PR kontrol listesi
- [ ] `python engine/tests/run_ci.py` yeşil
- [ ] Yeni davranış için test var
- [ ] Sır/PII/kişisel-yol eklenmedi
- [ ] CLA kabulü (ilk PR)
