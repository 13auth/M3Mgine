#!/usr/bin/env python3
"""obsidian.py — Obsidian vault -> 13auth hafıza + knowledge graph içe aktarımı.

Her not: aranabilir bir HAFIZA kaydı + bir KG VARLIĞI (not düğümü).
Her [[wiki-link]]: bir KG KENARI (not --links_to--> hedef-not).  -> graph bedava dolar.
Tüm içerik scrub'lanır (sır sızmaz). LLM gerekmez (enjekte üçlü + doğrudan yazım).
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import embeddings  # noqa: E402
import kg  # noqa: E402
import memory  # noqa: E402  (erasure/tombstone guard — acyclic: memory obsidian'ı import etmez)
import pii  # noqa: E402
import store  # noqa: E402
from redact import scrub_secrets  # noqa: E402

try:
    import yaml  # frontmatter (YAML) parse
except ImportError:
    yaml = None

MAX_CONTENT = 6000          # not başına saklanan içerik tavanı
MAX_FILES = 5000            # tek importta dosya tavanı (DoS)
MAX_LINKS_PER_NOTE = 200
MAX_PROP_LEN = 200

# [[Not]], [[Not|alias]], [[Not#başlık]] -> "Not"
_LINK = re.compile(r"\[\[([^\]\|#]+)")
_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)      # frontmatter bloğunu YAKALA
_TAG = re.compile(r"(?:^|\s)#([A-Za-z0-9_/\-çşğüöıİĞÜŞÖÇ]+)")  # inline #etiket
# frontmatter'dan KG-bağlantısı sayılacak alanlar (entity link'leri)
_LINK_FIELDS = ("connected_nodes", "connections", "related", "links", "linked")


def _scalarize(v):
    """Frontmatter değerini güvenli, kısa, JSON-uyumlu hale getir."""
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return scrub_secrets(v)[0][:MAX_PROP_LEN]
    if isinstance(v, list):
        return [scrub_secrets(str(x))[0][:MAX_PROP_LEN] for x in v[:50]]
    if v is None:
        return None
    return scrub_secrets(str(v))[0][:MAX_PROP_LEN]


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [t.strip() for t in re.split(r"[,\n]", str(v)) if t.strip()]


def parse_note(path: str, text: str) -> dict:
    norm = (path or "").replace("\\", "/")
    title = Path(norm).name
    if title.lower().endswith(".md"):
        title = title[:-3]
    title = title.strip() or path
    folder = norm.rsplit("/", 1)[0] if "/" in norm else ""   # Obsidian klasörü (ağaç gruplama)

    # frontmatter -> props
    props: dict = {}
    fm = _FM.match(text)
    body = text
    if fm:
        body = text[fm.end():]
        if yaml:
            try:
                loaded = yaml.safe_load(fm.group(1))
                if isinstance(loaded, dict):
                    props = {str(k): _scalarize(val) for k, val in list(loaded.items())[:40]}
            except Exception:
                props = {}
    body = body.strip()

    # tags: frontmatter tags + inline #etiket
    tags: list[str] = _as_list(props.pop("tags", None)) if "tags" in props else []
    for t in _TAG.findall(text):
        if t not in tags:
            tags.append(t)
    tags = tags[:50]

    # [[wiki-link]] + connected_nodes vb. -> KG bağlantıları
    links: list[str] = []
    for m in _LINK.findall(text):
        t = m.strip()
        if t and t.lower() != title.lower() and t not in links:
            links.append(t)
    for fld in _LINK_FIELDS:
        if fld in props:
            for t in _as_list(props.get(fld)):
                # "07 Memory/Foo" gibi path link'in son parçasını al
                name = t.replace("\\", "/").rsplit("/", 1)[-1].strip()
                if name and name.lower() != title.lower() and name not in links:
                    links.append(name)
    links = links[:MAX_LINKS_PER_NOTE]

    return {"title": title, "content": body, "links": links, "folder": folder,
            "props": props, "tags": tags}


def import_files(tenant_id: str, files: list[dict], user_id: str = "obsidian") -> dict:
    """files: [{path, text}, ...]  ->  istatistik."""
    notes = [parse_note(f.get("path", ""), f.get("text", "") or "")
             for f in files[:MAX_FILES] if (f.get("text") or "").strip()]
    return import_notes(tenant_id, notes, user_id=user_id)


def import_notes(tenant_id: str, notes: list[dict], user_id: str = "obsidian") -> dict:
    """HIZLI yol: kg.ingest'i (not başına O(N) round-trip + O(N²) sayım) baypas et.
    Tüm entity/memory/edge'leri bellekte topla, batch başına ~3-4 toplu sorguyla yaz."""
    import time
    stats = {"notes": 0, "memories": 0, "edges": 0, "entities": 0, "skipped": 0}

    # mevcut entity haritası (TEK sorgu): norm -> id
    idx: dict[str, str] = {}
    existing = store.list_entities(tenant_id)
    for e in existing:
        if e.get("norm"):
            idx[e["norm"]] = e["id"]
    cap_room = max(0, kg.MAX_KG_ENTITIES - len(existing))
    new_ents: list[dict] = []

    def ent_for(name: str):
        nonlocal cap_room
        nm = scrub_secrets(str(name).strip())[0]
        norm = store._fold(nm)
        if not norm:
            return None
        if norm in idx:
            return idx[norm]
        if cap_room <= 0:
            return None
        eid = "ent-" + hashlib.sha1(f"{tenant_id}|{norm}".encode("utf-8")).hexdigest()[:16]
        idx[norm] = eid
        new_ents.append({"id": eid, "name": nm[:200], "norm": norm, "type": "note"})
        cap_room -= 1
        return eid

    now = time.time()
    mems: list[dict] = []
    edges: list[dict] = []
    seen_edge: set[str] = set()
    seen_content: set[str] = set()   # aynı içeriği birden fazla not olarak yazma (çoğalma önle)

    def add_edge(sid, pred, oid, fact):
        if not sid or not oid:
            return
        eid = "edge-" + hashlib.sha1(f"{tenant_id}|{sid}|{pred}|{oid}".encode("utf-8")).hexdigest()[:16]
        if eid in seen_edge:
            return
        seen_edge.add(eid)
        edges.append({"id": eid, "tenant_id": tenant_id, "subject_id": sid, "predicate": pred,
                      "object_id": oid, "fact": fact[:300], "valid_at": now, "source": "obsidian"})

    for n in notes:
        title = (n.get("title") or "").strip()
        if not title:
            stats["skipped"] += 1
            continue
        sid = ent_for(title)   # not düğümü (izole not da graph'ta görünür)
        content = scrub_secrets(n.get("content") or "")[0][:MAX_CONTENT]
        if content.strip():
            ch = memory._content_hash(content)
            if ch not in seen_content:   # aynı içerik tekrar yazılmaz (graph düğümü/kenarı yine işlenir)
                seen_content.add(ch)
                # id folder+title'dan: aynı başlıklı FARKLI klasör notları ARTIK birbirini EZMEZ (veri kaybı yok)
                mid = "obs-" + hashlib.sha1(f"{tenant_id}|{n.get('folder', '')}|{title}".encode("utf-8")).hexdigest()[:14]
                pi = pii.classify(content)   # AACRP: PII tespit + atıf (regex, LLM gerekmez — bulk hızlı)
                mems.append({"id": mid, "tenant_id": tenant_id, "user_id": user_id, "content": content,
                             "source": "obsidian", "salience": 1.0,
                             "subject_party": pi["subject_party"], "pii_flags": pi["pii_flags"],
                             "metadata": {"title": title, "folder": n.get("folder", ""),
                                          "props": n.get("props", {}), "tags": n.get("tags", [])}})
        for l in n.get("links", []):
            add_edge(sid, "links_to", ent_for(l), f"{title} -> {l}")
        for t in n.get("tags", []):
            add_edge(sid, "tagged", ent_for(f"#{t}"), f"{title} #{t}")
        stats["notes"] += 1

    # SEMANTİK arama için içerikleri batch embed'le (hat yoksa None -> lexical fallback, çökmez).
    # Tek tek değil ~96'lık bloklar: import hızlı kalır, embed maliyeti/round-trip düşer.
    if mems:
        vecs = embeddings.embed_batch([m["content"] for m in mems])
        for m, v in zip(mems, vecs):
            m["embedding"] = v

    # ERASURE GUARD (KVKK Art.17): forget edilen içerik re-import'la DİRİLMESİN — memory.ingest
    # ile aynı tombstone kontrolü (content-hash + semantik). Bu yol eskiden guard'ı baypas ediyordu.
    tombs = store.get_tombstones(tenant_id)
    if tombs and mems:
        tomb_hashes = {t["content_hash"] for t in tombs}
        tomb_embs = [t["embedding"] for t in tombs if t["embedding"]]
        kept = []
        for m in mems:
            if memory._tombstone_blocks(tenant_id, m["content"], m.get("embedding"), tomb_hashes, tomb_embs):
                stats["skipped"] += 1
            else:
                kept.append(m)
        mems = kept

    store.bulk_add_entities(tenant_id, new_ents)   # ~3 toplu yazım
    store.bulk_add_memories(mems)
    store.bulk_add_edges(edges)
    stats["memories"] = len(mems)
    stats["edges"] = len(edges)
    stats["entities"] = len(new_ents)
    return stats


def read_vault(folder: str) -> list[dict]:
    """Bir klasördeki tüm .md dosyalarını {path, text} olarak oku (özyinelemeli)."""
    root = Path(folder)
    out: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        try:
            out.append({"path": str(p.relative_to(root)), "text": p.read_text(encoding="utf-8", errors="ignore")})
        except Exception:
            continue
        if len(out) >= MAX_FILES:
            break
    return out
