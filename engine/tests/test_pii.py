"""test_pii.py — PII tespit + attribution (proximity, fail-closed, akrabalık rolleri). Audit sızıntı senaryoları."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pii  # noqa: E402


def sp(t):
    return pii.classify(t)["subject_party"]


# 3.şahıs olması GEREKENLER (audit'in canlı doğruladığı sızıntı senaryoları + akrabalık rolleri)
LEAK = [
    "Ali'nin numarasi 0532 123 45 67 benim rehbere kayitli",
    "Kendi rehberimde Mehmet 0533 222 11 00",
    "Benim kardesimin TCKN 10000000146",
    "Musterinin telefonu benim telefonum gibi 05321234567",
    "Musterinin email adresi ahmet@firma.com",
    "kuzenim icin kaydettim, numaram 0532 111 22 33",   # akrabalık rol fix (eskiden self sızıyordu)
    "komsumun numarasi 0535 444 55 66",
    "ortagimin telefonu 0536 777 88 99",
]
for t in LEAK:
    assert sp(t) == "third_party", f"third_party bekleniyordu: {t!r} -> {sp(t)}"

# kullanıcının KENDİ bilgisi / PII-yok -> self
SELF = [
    "Benim numaram 0533 111 22 33",
    "Python ogrenmek istiyorum",
    "Turkiye nin baskenti Ankara",
    "Kendi e-postam test@example.com",
]
for t in SELF:
    assert sp(t) == "self", f"self bekleniyordu: {t!r} -> {sp(t)}"

# detect flag'leri + checksum
assert "tckn" in pii.detect("TCKN 10000000146")["flags"]
assert "telefon" in pii.detect("ara 0532 145 67 89")["flags"]
assert pii.detect("sadece duz metin")["flags"] == []
assert pii.detect("gecersiz tckn 12345678901")["flags"] == [] or "tckn" not in pii.detect("gecersiz tckn 12345678901")["flags"]

print("test_pii.py: PASS")
