"""Texts with versions ("Texte mit Fassungen", 3 Nov 2026): laws, regulations, directives, guidelines, standards, terms of
service and AI-provider policies kept as datasets whose versions are the text's versions (Fassungen), each with its
validity period.

A dataset is a text when `dataset.act_type` is set (law | regulation | directive | ordinance | guideline | standard | terms
| policy | other); it also carries the official `identifier` (ELI, CELEX, SR number, URL), `jurisdiction` and `issuer`.
Each `dataset_version` of a text has valid_from, valid_to (last day, inclusive; NULL = still valid), version_label,
published_at and source_url. Ingest only stores and describes (Falk, 3 Nov 2026): the files stay as delivered (PDF, HTML,
XHTML, XML – documents, never tables); no splitting into articles, no diffs, no time series. The local model describes a
text from its metadata, the beginning of the text (title, preamble) and its headings only – never the whole text.

    ACT_TYPES · is_text(d) · check_meta(…) · set_version(store, ref, n, …) · apply_version(store, did, n, …)
    versions(store, d, files=True) · version_at(store, ref, date) · in_force(store, d)
    text_inputs(store, d) · prompt(store, d, vocab)                         description input (capped)
    convert(store, ref, spec)                                                turn a stored dataset into a text
"""
from __future__ import annotations

import html.parser
import json
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from fundus.store import Store

ACT_TYPES = ("law", "regulation", "directive", "ordinance", "guideline", "standard", "terms", "policy", "other")
LABELS = {"law": "Gesetz", "regulation": "Verordnung (EU-Verordnung)", "directive": "Richtlinie (EU-Richtlinie)",
          "ordinance": "Verordnung (Ausführungsrecht)", "guideline": "Leitlinie", "standard": "Norm / Standard",
          "terms": "Nutzungsbedingungen", "policy": "Policy eines Anbieters", "other": "anderer Text"}
VERSION_FIELDS = ("valid_from", "valid_to", "version_label", "published_at", "source_url")
TEXT_FIELDS = ("identifier", "jurisdiction", "act_type", "issuer")
ISO_DAY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
# what the local model sees of a text: the beginning (title, preamble) and the headings – capped, never the whole text
PREAMBLE_CHARS = 6000
TOC_LINES = 250
TOC_CHARS = 8000
READ_MAX = 12 * 1024 ** 2          # bytes of an HTML/XML/Markdown file read for the headings
PDF_PAGES = 400                    # pages of a PDF read for the headings (pdftotext)
FIRST_PROVISION = re.compile(r"^(art(icle|ikel|\.)?|§)\s*1\b", re.I)
MAIN_RANK = {"xhtml": 0, "html": 0, "htm": 0, "xml": 1, "md": 2, "txt": 2, "pdf": 3}
HEADING = re.compile(r"^(art(icle|ikel|\.)|chapter|kapitel|chapitre|title|titel|titre|section|abschnitt|annex|anhang|annexe|"
                     r"part|teil|partie|book|buch|§|preamble|präambel|schedule|appendix|anlage)\b\s*[\dIVXLCDM§A-Z]?", re.I)


def is_text(d) -> bool:
    return bool(d["act_type"]) if "act_type" in d.keys() else False


def _day(v, field: str) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()[:10]
    if not ISO_DAY.match(s):
        raise ValueError(f"{field}: ISO date, e.g. 2026-07-27")
    return s


def check_meta(act_type=None, valid_from=None, valid_to=None, published_at=None, need_act_type: bool = False,
               need_valid_from: bool = True) -> dict:
    """Validate text metadata before anything is stored; returns the normalised dates."""
    if need_act_type and not act_type:
        raise ValueError(f"a text needs act_type: {' | '.join(ACT_TYPES)}")
    if act_type and act_type not in ACT_TYPES:
        raise ValueError(f"act_type: {' | '.join(ACT_TYPES)}")
    vf, vt, pa = _day(valid_from, "valid_from"), _day(valid_to, "valid_to"), _day(published_at, "published_at")
    if need_valid_from and not vf:
        raise ValueError("a version of a text needs valid_from (ISO date from which it applies)")
    if vf and vt and vt < vf:
        raise ValueError("valid_to lies before valid_from")
    return {"valid_from": vf, "valid_to": vt, "published_at": pa}


def _prev_day(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def apply_version(store: Store, did: int, n: int, valid_from=None, valid_to=None, version_label=None, published_at=None,
                  source_url=None, close: bool = True) -> list[str]:
    """Write a version's validity (inside the caller's transaction). close: a version without valid_to ends the day before
    the next later version starts; earlier versions still open (valid_to empty) end the day before this one starts.
    Explicit valid_to values are never overwritten. Returns notes on closed versions."""
    m = check_meta(valid_from=valid_from, valid_to=valid_to, published_at=published_at, need_valid_from=False)
    vals = {"valid_from": m["valid_from"], "valid_to": m["valid_to"], "published_at": m["published_at"],
            "version_label": (version_label or "").strip() or None, "source_url": (source_url or "").strip() or None}
    vals = {k: v for k, v in vals.items() if v is not None}
    if vals:
        store.db.execute(f"UPDATE dataset_version SET {', '.join(f'{k} = ?' for k in vals)} WHERE dataset_id = ? AND n = ?",
                         (*vals.values(), did, n))
    notes = []
    cur = store.db.execute("SELECT valid_from, valid_to FROM dataset_version WHERE dataset_id = ? AND n = ?", (did, n)).fetchone()
    vf = cur["valid_from"] if cur else None
    if not (close and vf):
        return notes
    for r in store.db.execute("SELECT n, valid_from FROM dataset_version WHERE dataset_id = ? AND n != ? AND valid_to IS NULL "
                              "AND valid_from IS NOT NULL AND valid_from < ? ORDER BY n", (did, n, vf)).fetchall():
        store.db.execute("UPDATE dataset_version SET valid_to = ? WHERE dataset_id = ? AND n = ?", (_prev_day(vf), did, r["n"]))
        notes.append(f"version {r['n']} now valid until {_prev_day(vf)}")
    if not cur["valid_to"]:
        nxt = store.db.execute("SELECT min(valid_from) FROM dataset_version WHERE dataset_id = ? AND n != ? AND valid_from > ?",
                               (did, n, vf)).fetchone()[0]
        if nxt:
            store.db.execute("UPDATE dataset_version SET valid_to = ? WHERE dataset_id = ? AND n = ?", (_prev_day(nxt), did, n))
            notes.append(f"version {n} valid until {_prev_day(nxt)} (a later version starts {nxt})")
    return notes


def set_version(store: Store, ref, n: int, changes: dict, actor: str = "cli") -> dict:
    """Change a version's validity or label (valid_from, valid_to, version_label, published_at, source_url; '' or None
    clears a field). No automatic closing here – what is given is what is stored."""
    from fundus import datasets as ds
    d = ds._row(store, ref)
    bad = set(changes) - set(VERSION_FIELDS)
    if bad:
        raise ValueError(f"cannot change {sorted(bad)} of a version (fields: {', '.join(VERSION_FIELDS)})")
    old = store.db.execute("SELECT * FROM dataset_version WHERE dataset_id = ? AND n = ?", (d["id"], int(n))).fetchone()
    if old is None:
        raise LookupError(f"dataset:{d['id']} has no version {n}")
    ch = {}
    for k, v in changes.items():
        v = None if v in (None, "") else str(v).strip()
        if k in ("valid_from", "valid_to", "published_at"):
            v = _day(v, k)
        ch[k] = v
    vf = ch.get("valid_from", old["valid_from"])
    vt = ch.get("valid_to", old["valid_to"])
    if vf and vt and vt < vf:
        raise ValueError("valid_to lies before valid_from")
    ch = {k: v for k, v in ch.items() if old[k] != v}
    if ch:
        with store.batch(actor):
            store.db.execute(f"UPDATE dataset_version SET {', '.join(f'{k} = ?' for k in ch)} WHERE dataset_id = ? AND n = ?",
                             (*ch.values(), d["id"], int(n)))
            store.db.execute("UPDATE dataset SET updated_at = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["id"]))
            store.log("dataset", d["id"], "update", {f"version {n}": {k: {"old": old[k], "new": v} for k, v in ch.items()}})
    return {"dataset": f"dataset:{d['id']}", "version": int(n), "changed": sorted(ch),
            "versions": versions(store, ds._row(store, d["id"]), files=False)}


def versions(store: Store, d, files: bool = True, today: str | None = None) -> list[dict]:
    """The versions of a dataset, newest first, with validity; for a text `in_force` marks the one valid today and files
    (documents of that version with download links) are listed per version."""
    from fundus import datasets as ds
    rows = [dict(r) for r in store.db.execute(
        "SELECT n, note, created_at, created_by, valid_from, valid_to, version_label, published_at, source_url "
        "FROM dataset_version WHERE dataset_id = ? ORDER BY n DESC", (d["id"],))]
    if not is_text(d):
        return [{k: v for k, v in r.items() if v is not None or k in ("note", "created_by")} for r in rows]
    now = _at(store, d["id"], today or date.today().isoformat())
    for r in rows:
        r["in_force"] = now is not None and r["n"] == now
        if files:
            r["files"] = [{"id": f["id"], "file": f["name"], "format": f["ext"], "bytes": f["bytes"],
                           "path": str(ds.file_path(store, f["sha"], f["ext"])), "download_url": ds.url(f["sha"], f["ext"], f["name"])}
                          for f in store.db.execute("SELECT * FROM dataset_file WHERE dataset_id = ? AND version = ? ORDER BY id",
                                                    (d["id"], r["n"]))]
    return rows


def _at(store: Store, did: int, day: str) -> int | None:
    """Number of the version valid on `day`: valid_from ≤ day ≤ valid_to (open end); of several, the latest start, then
    the highest number (a consolidated version added after the official one with the same start date wins)."""
    r = store.db.execute("SELECT n FROM dataset_version WHERE dataset_id = ? AND valid_from IS NOT NULL AND valid_from <= ? "
                         "AND (valid_to IS NULL OR valid_to >= ?) ORDER BY valid_from DESC, n DESC LIMIT 1",
                         (did, day, day)).fetchone()
    return r[0] if r else None


def version_at(store: Store, ref, day: str, include_internal: bool = True, project: str | None = None) -> dict:
    """Which version of a text was valid at a date (from the stored validity only – the text is not read)."""
    from fundus import datasets as ds
    d = ds._row(store, ref)
    if not ds._visible(d, include_internal, project):
        raise PermissionError(f"dataset {ref} is internal; pass include_internal=true or its project")
    day = _day(day, "date")
    if not day:
        raise ValueError("date: ISO date, e.g. 2025-03-01")
    n = _at(store, d["id"], day)
    out = {"dataset": f"dataset:{d['id']}", "title": d["title"], "identifier": d["identifier"], "date": day}
    if n is None:
        first = store.db.execute("SELECT min(valid_from) FROM dataset_version WHERE dataset_id = ?", (d["id"],)).fetchone()[0]
        out["version"] = None
        out["why"] = ("no version of this dataset has a validity (not a text with versions?)" if not first else
                      f"before the first version (valid from {first})" if day < first else "no version covers this date (gap)")
        return out
    v = next(x for x in versions(store, d) if x["n"] == n)
    out.update({"version": n, **{k: v.get(k) for k in VERSION_FIELDS}, "current_version": d["current_version"],
                "in_force_today": v["in_force"], "files": v.get("files", [])})
    return out


def in_force(store: Store, d) -> dict | None:
    """The version valid today (n, label, valid_from, valid_to) – for the catalog and the Steckbrief."""
    n = _at(store, d["id"], date.today().isoformat())
    if n is None:
        return None
    r = store.db.execute("SELECT n, version_label, valid_from, valid_to FROM dataset_version WHERE dataset_id = ? AND n = ?",
                         (d["id"], n)).fetchone()
    return {k: r[k] for k in r.keys() if r[k] is not None}


# ------------------------------------------------------------------ description input (beginning + headings only)
class _Lines(html.parser.HTMLParser):
    """HTML/XHTML/XML → text lines (a line break at every block element); scripts and styles dropped."""
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "title", "section", "article",
             "table", "dt", "dd", "blockquote", "pre", "hr", "header", "footer", "caption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag.split(":")[-1] in self.BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag.split(":")[-1] in self.BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def _lines_of(path: Path, ext: str) -> list[str]:
    if ext == "pdf":
        try:
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", "-l", str(PDF_PAGES), str(path), "-"], capture_output=True, timeout=180)
            raw = out.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
    else:
        with open(path, "rb") as f:
            raw = f.read(READ_MAX).decode("utf-8", "replace")
        if ext in ("xhtml", "html", "htm", "xml"):
            p = _Lines()
            p.feed(raw)
            p.close()
            raw = "".join(p.buf)
    return [" ".join(x.split()) for x in raw.splitlines() if x.strip()]


def _headings(lines: list[str]) -> list[str]:
    """Lines that look like headings (Article 5, CHAPTER III, Anhang I, § 3 …), joined with a short title line after
    them; capped at TOC_LINES lines / TOC_CHARS characters."""
    out, total = [], 0
    for i, ln in enumerate(lines):
        if len(ln) > 120 or not HEADING.match(ln) or ln.endswith((":", ";", ",")) or (len(ln) > 40 and ln.endswith(".")):
            continue                          # a sentence that starts with "Chapter …", not a heading
        h = ln
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (nxt and len(nxt) <= 100 and not HEADING.match(nxt) and not nxt.endswith((".", ";", ":")) and len(ln) < 40
                and not nxt.startswith(("►", "▼", "◄", "▲"))):          # EUR-Lex amendment markers are no titles
            h = f"{ln} – {nxt}"
        if out and out[-1] == h:
            continue
        if len(out) >= TOC_LINES or total + len(h) > TOC_CHARS:
            out.append("… (more headings)")
            break
        out.append(h)
        total += len(h) + 1
    return out


def main_document(store: Store, d, version: int | None = None):
    """The document of a version the description is built from: HTML/XHTML before XML, Markdown/text, PDF; English first."""
    rows = store.db.execute("SELECT * FROM dataset_file WHERE dataset_id = ? AND version = ? AND role = 'document'",
                            (d["id"], version or d["current_version"])).fetchall()
    rows = [r for r in rows if r["ext"] in MAIN_RANK and not re.match(r"(?i)readme", r["name"])]
    if not rows:
        return None
    en = lambda r: 0 if re.search(r"(?i)(^|[_\-.])(en|eng|english)([_\-.]|$)", r["name"]) else 1
    return sorted(rows, key=lambda r: (MAIN_RANK[r["ext"]], en(r), -(r["bytes"] or 0)))[0]


def text_inputs(store: Store, d) -> dict:
    """{file, preamble, headings} for the description: the beginning of the main document of the current version (title,
    preamble – up to the first article/§ 1, at most PREAMBLE_CHARS characters) and its headings. Never more."""
    from fundus import datasets as ds
    f = main_document(store, d)
    if f is None:
        return {"file": None, "preamble": "", "headings": []}
    p = ds.file_path(store, f["sha"], f["ext"])
    lines = _lines_of(p, f["ext"]) if p.is_file() else []
    pre, n = [], 0
    for ln in lines:
        if n >= PREAMBLE_CHARS or FIRST_PROVISION.match(ln):      # the beginning ends where the provisions start
            break
        pre.append(ln[:PREAMBLE_CHARS - n])
        n += len(ln) + 1
    return {"file": f["name"], "preamble": "\n".join(pre)[:PREAMBLE_CHARS], "headings": _headings(lines)}


PROMPT = """You describe a legal or policy text (law, regulation, directive, guideline, standard, terms of service or an
AI provider's policy) for a research archive so that it can be found later. You see ONLY the metadata, the beginning of
the text (title, preamble) and its headings – not the provisions themselves. Describe what the text is, who issued it,
what it regulates and for whom, and how it is structured; do not state what individual provisions say beyond their
headings, never invent dates, numbers or obligations.

Given by the owner (trust it): {given}

Versions of the text (Fassungen, newest first): {versions}

Beginning of the text ({file}):
{preamble}

Headings (table of contents):
{headings}

Answer with one JSON object:
{{"title": "short title if none is given, else repeat it",
 "summary_en": "3-5 sentences: what the text is and regulates, issuer, scope and addressees, structure (chapters, annexes), the versions held",
 "summary_de": "the same in German", "keywords": ["8-15 short English keywords"],
 "geo": "jurisdiction, e.g. 'EU' or 'CH', or null",
 "industry": [], "topic": [], "region": []}}
For industry, topic and region use ONLY these values (zero to three each):
{vocab}"""


def prompt(store: Store, d, vocab: str) -> tuple[str, dict]:
    """The description prompt of a text and what went into it (for the annotation)."""
    inp = text_inputs(store, d)
    given = {k: d[k] for k in ("title", "description", "identifier", "jurisdiction", "act_type", "issuer", "source_title",
                               "publisher", "published", "url", "project") if d[k]}
    vs = [{k: v[k] for k in ("n", "version_label", "valid_from", "valid_to", "published_at") if v.get(k)}
          for v in versions(store, d, files=False)][:20]
    text = PROMPT.format(given=json.dumps(given, ensure_ascii=False), versions=json.dumps(vs, ensure_ascii=False),
                         file=inp["file"] or "no readable document", preamble=inp["preamble"] or "(none)",
                         headings="\n".join(inp["headings"]) or "(none found)", vocab=vocab)
    return text, {"file": inp["file"], "preamble_chars": len(inp["preamble"]), "headings": len(inp["headings"])}


# ------------------------------------------------------------------ conversion of an existing dataset
def convert(store: Store, ref, spec: dict, actor: str = "cli", dry_run: bool = False) -> dict:
    """Turn a stored dataset into a text with versions (one-off, e.g. dataset:214 EU AI Act). spec:
    {"identifier", "jurisdiction", "act_type", "issuer", "title"?, "base": folder for relative paths,
     "tables_as_documents": true (tables of the dataset become plain documents – a text has no tables),
     "versions": [{"version": 1 (an existing version) | omitted (a new version), "files": [paths; for an existing version
     the files are added to it], "valid_from", "valid_to"?, "version_label", "published_at"?, "source_url"?, "note"?}]}
    Existing versions first, then the new ones in the given order (each a new version via datasets.add)."""
    from fundus import datasets as ds
    d = ds._row(store, ref)
    if is_text(d):
        raise ValueError(f"dataset:{d['id']} is already a text with versions")
    if (d["holding"] or "stored") == "reference":
        raise ValueError("a reference dataset cannot be converted (nothing stored)")
    check_meta(act_type=spec.get("act_type"), need_act_type=True, need_valid_from=False)
    base = Path(spec.get("base") or ".").expanduser()
    vers = spec.get("versions") or []
    if not vers:
        raise ValueError("spec.versions: at least one version")
    have = {r[0] for r in store.db.execute("SELECT n FROM dataset_version WHERE dataset_id = ?", (d["id"],))}
    plan = []
    for v in vers:
        check_meta(valid_from=v.get("valid_from"), valid_to=v.get("valid_to"), published_at=v.get("published_at"))
        files = [Path(f) if Path(f).is_absolute() else base / f for f in v.get("files") or []]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise FileNotFoundError(f"not found: {', '.join(missing)}")
        if v.get("version") is not None and int(v["version"]) not in have:
            raise LookupError(f"dataset:{d['id']} has no version {v['version']}")
        if v.get("version") is None and not files:
            raise ValueError(f"new version {v.get('version_label') or v.get('valid_from')}: files needed")
        plan.append({**v, "files": files})
    plan.sort(key=lambda v: (v.get("version") is None, int(v.get("version") or 0)))
    if dry_run:
        return {"dataset": f"dataset:{d['id']}", "dry_run": True,
                "plan": [{"version": v.get("version") or "new", "files": [f.name for f in v["files"]],
                          **{k: v.get(k) for k in VERSION_FIELDS if v.get(k)}} for v in plan]}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with store.batch(actor):
        ch = {k: spec[k] for k in (*TEXT_FIELDS, "title") if spec.get(k)}
        if not d["frequency"]:
            ch["frequency"] = "irregular"         # new consolidated versions appear irregularly: tracked (due/update_targets)
        store.db.execute(f"UPDATE dataset SET {', '.join(f'{k} = ?' for k in ch)}, status = 'new', updated_at = ? WHERE id = ?",
                         (*ch.values(), now, d["id"]))
        moved = 0
        if spec.get("tables_as_documents"):
            moved = store.db.execute("UPDATE dataset_file SET role = 'document' WHERE dataset_id = ? AND role = 'table'",
                                     (d["id"],)).rowcount
        store.log("dataset", d["id"], "update", {"converted_to_text": ch, "tables_as_documents": moved})
    notes, added = [], []
    for v in plan:
        if v.get("version") is not None:
            n = int(v["version"])
            with store.batch(actor):
                present = {r[0] for r in store.db.execute("SELECT sha FROM dataset_file WHERE dataset_id = ? AND version = ?", (d["id"], n))}
                for f in v["files"]:
                    sha, ext = ds._sha_file(f), f.suffix.lower().lstrip(".")
                    if sha in present:
                        continue
                    ds._store_file(store, f, sha, ext)
                    store.db.execute("INSERT INTO dataset_file(dataset_id, version, role, name, sha, ext, bytes, source_path) "
                                     "VALUES (?,?,'document',?,?,?,?,?)", (d["id"], n, f.name, sha, ext, f.stat().st_size, str(f)))
                    present.add(sha)
                    added.append(f"v{n}: {f.name}")
                if v.get("note"):
                    store.db.execute("UPDATE dataset_version SET note = ? WHERE dataset_id = ? AND n = ?", (v["note"], d["id"], n))
                notes += apply_version(store, d["id"], n, **{k: v.get(k) for k in VERSION_FIELDS})
                store.log("dataset", d["id"], "version", {"version": n, "text_version": {k: v.get(k) for k in VERSION_FIELDS if v.get(k)}})
        else:
            res = ds.add(store, v["files"], dataset=d["id"], note=v.get("note"), actor=actor, text=True,
                         **{k: v.get(k) for k in VERSION_FIELDS})
            notes += res.get("closed") or []
            added += [f"v{res['version']}: {x}" for x in res["documents"]]
    d2 = ds._row(store, d["id"])
    return {"dataset": f"dataset:{d['id']}", "converted": True, "fields": {k: d2[k] for k in (*TEXT_FIELDS, "title", "frequency")},
            "tables_as_documents": moved, "files_added": added, "closed": notes,
            "versions": versions(store, d2, files=False), "in_force": in_force(store, d2),
            "description": "queued: the local model describes the text from metadata, preamble and headings"}
