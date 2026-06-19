#!/usr/bin/env python3
"""pii.py — KVKK/PII tespiti + ATIF (AACRP attribution ekseni).

  detect(text)            -> {flags:[email,telefon,tckn,iban], strong:bool}
  attribute(text, strong) -> subject_party: self | third_party
  classify(text)          -> {subject_party, pii_flags}

ATIF — PROXIMITY-AWARE + FAIL-CLOSED (güvenlik denetiminden geçirildi):
  Güçlü-PII (telefon/TCKN/IBAN) için subject_party='self' SADECE şu durumda:
  PII token'ına YAKIN (~45 karakter) bir İYELİK-self işareti (numaram/telefonum/ibanim...)
  var VE yakında 3.şahıs/başkası işareti YOK. Aksi halde 'third_party' (fail-closed).
  Genel "ben/benim/kendi" tek başına güçlü-PII'yi self yapMAZ (3.şahsın verisini
  kullanıcının profiline karıştıran ana sızıntı buydu).
  3.şahıs işaretleri Türkçe ekleri de kapsar (müşterinin/danışanın -> \\w* ile).
ReDoS guard: tüm regex'ler ilk 4096 karakterde çalışır (PII baştadır).
redact.py (secret scrub) AYRI katman; korunur.
"""
from __future__ import annotations

import re

_MAX = 4096  # ReDoS/DoS: PII metnin başında olur; regex'i ilk 4KB ile sınırla

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE_TR = re.compile(r"(?<!\d)(?:\+?90[\s-]?)?0?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
_TCKN = re.compile(r"(?<!\d)\d{11}(?!\d)")
_IBAN_TR = re.compile(r"\bTR\d{2}\s?(?:\d{4}\s?){5}\d{2}\b", re.I)

# İYELİK-self: kullanıcının KENDİ verisini işaret eden iyelik ekleri (sadece bunlar self lisanslar)
_SELF_POSS = re.compile(
    r"(numaram|telefonum|cep\s?numaram|mailim|mail\s?adresim|e-?postam|hesabım|hesabim|"
    r"iban'?ım|iban'?im|ibanım|ibanim|tckn'?im|kimlik\s?numaram|adresim|kart\s?numaram)", re.I)

# 3.ŞAHIS / BAŞKASI: roller (Türkçe ekleriyle: müşterinin/danışanın -> \w*) + başka-kişi iyelikleri.
# Bu sınıf güçlü-PII'de self'i EZER (eşimin/kardeşimin TCKN'si = 3.şahıs verisi).
_THIRD = re.compile(
    r"(müşteri|musteri|danışan|danisan|hasta|çalışan|calisan|tedarikçi|tedarikci|müvekkil|muvekkil|"
    r"kişi|kisi|abone|kullanıcı|kullanici|"
    # akrabalık / ilişki rolleri (3.şahıs verisi — fail-closed için GENİŞ tutulur)
    r"eşim|esim|annem|babam|anne|baba|kardeşim|kardesim|abim|ablam|oğlum|oglum|kızım|kizim|"
    r"kuzenim|kuzen|yeğenim|yegenim|yeğen|yegen|komşum|komsum|komşu|komsu|arkadaşım|arkadasim|arkadaş|arkadas|"
    r"sevgilim|sevgili|nişanlım|nisanlim|nişanlı|nisanli|ortağım|ortagim|ortak|"
    r"amcam|amca|dayım|dayim|dayı|dayi|halam|hala|teyzem|teyze|dedem|dede|babaannem|babaanne|anneannem|anneanne|"
    r"gelin|damat|kayın|kayin|kayınvalide|kayinvalide|kayınpeder|kayinpeder|"
    r"avukatım|avukatim|avukat|asistanım|asistanim|asistan|sekreterim|sekreter|danışmanım|danismanim|"
    r"patronum|patron|müdürüm|mudurum|müdür|mudur|hoca|hocam)\w*",
    re.I)
# isim+genitif kabası ("Ali'nin", "Mehmet'in") — 3.şahıs sinyali (özel ad + 'nin/'nın eki)
_NAME_GEN = re.compile(r"\b[A-ZÇĞİÖŞÜ][a-zçğıöşü]+['’]?(?:nin|nın|nun|nün|in|ın|un|ün)\b")


def _valid_tckn(n: str) -> bool:
    if len(n) != 11 or n[0] == "0" or len(set(n)) == 1:
        return False
    d = [int(x) for x in n]
    if ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10 != d[9]:
        return False
    return sum(d[0:10]) % 10 == d[10]


def _valid_iban_tr(s: str) -> bool:
    raw = re.sub(r"\s", "", s).upper()
    if not re.fullmatch(r"TR\d{24}", raw):
        return False
    rearr = raw[4:] + raw[:4]
    try:
        return int("".join(str(int(c, 36)) for c in rearr)) % 97 == 1
    except ValueError:
        return False


def _strong_spans(t: str) -> list:
    """Güçlü-PII (telefon/geçerli-TCKN/geçerli-IBAN) konumları (proximity için)."""
    spans = []
    spans += [m.span() for m in _PHONE_TR.finditer(t)]
    spans += [m.span() for m in _TCKN.finditer(t) if _valid_tckn(m.group())]
    spans += [m.span() for m in _IBAN_TR.finditer(t) if _valid_iban_tr(m.group())]
    return spans


def detect(text: str) -> dict:
    """PII türleri. email=zayıf; telefon/tckn/iban=güçlü. İlk 4KB (ReDoS guard)."""
    t = (text or "")[:_MAX]
    flags = []
    if _EMAIL.search(t):
        flags.append("email")
    if _PHONE_TR.search(t):
        flags.append("telefon")
    if any(_valid_tckn(m) for m in _TCKN.findall(t)):
        flags.append("tckn")
    if any(_valid_iban_tr(m) for m in _IBAN_TR.findall(t)):
        flags.append("iban")
    return {"flags": flags, "strong": bool({"telefon", "tckn", "iban"} & set(flags))}


def _third_near(win: str) -> bool:
    return bool(_THIRD.search(win) or _NAME_GEN.search(win))


def _win(t: str, s: int, e: int) -> str:
    return t[max(0, s - 45):min(len(t), e + 45)]


def attribute(text: str, strong: bool) -> str:
    """subject_party. Atıf YALNIZ gerçek PII'nin YAKININDA yapılır (PII-yok not -> self).
    Güçlü-PII'de PROXIMITY + FAIL-CLOSED."""
    t = (text or "")[:_MAX]
    if strong:
        spans = _strong_spans(t)
        # 1) güçlü-PII'nin YAKININDA 3.şahıs/isim işareti -> third_party
        for s, e in spans:
            if _third_near(_win(t, s, e)):
                return "third_party"
        # 2) YAKINDA iyelik-self (ve hiç third yok) -> self
        for s, e in spans:
            if _SELF_POSS.search(_win(t, s, e)):
                return "self"
        # 3) belirsiz güçlü-PII -> fail-closed
        return "third_party"
    # güçlü-PII yok: yalnız email (zayıf PII) varsa atıf yap; YOKSA kullanıcının kendi notu (self)
    em = list(_EMAIL.finditer(t))
    if not em:
        return "self"   # PII yok -> başkasının kişisel verisi yok -> kullanıcının bilgisi
    for m in em:
        if _third_near(_win(t, *m.span())):
            return "third_party"
    return "self"


def classify(text: str) -> dict:
    d = detect(text)
    return {"subject_party": attribute(text, d["strong"]), "pii_flags": d["flags"]}
