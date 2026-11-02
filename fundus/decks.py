"""Ordered decks (Reihenfolgen): the slide order of a deck as the session built it.

Folder decks (numbered copies in a deck folder, `exhibit_deck`) only know the order when every slide is copied with a
prefix. Deck scripts often build the PDF straight from the exhibit folders (Acme deck_v4.py: PARTS → PDF), so the order
lives in the script and the PDF. Two ways bring it into Fundus:

- manifest: the session passes the final sequence (MCP fundus_exhibit_deck, CLI `fundus exhibits deck set`, from a deck
  script `mck.fundus_deck(seq, name, pdf=…)`); entries are PNG paths (registered first if new) or exhibit refs.
- pdf: `fundus exhibits deck pdf <file>` and the nightly `exhibits sync` read deck PDFs in the project folders, render the
  pages small and match each page against the stored versions of the project (colour thumbnails, mean absolute
  difference). A PDF counts as a deck when most of its pages match; a manifest naming the same PDF wins.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from fundus import exhibits as ex
from fundus.store import Store, slug as _slug

THUMB = (64, 36)               # colour thumbnail for page matching (16:9)
FINE = (160, 90)               # second look for close calls (section slides that differ only in their number)
FINE_RATIO = 1.3               # second look: on the pixels where two candidates differ, the page must be this much closer
FINE_DIFF = 0.15               # a pixel "differs" between two candidates above this (0..1, any channel)
FINE_MIN_PIXELS = 12           # fewer differing pixels: the two candidates cannot be told apart
FINE_TOP = 6                   # slides compared in the second look
ACCEPT_EXACT = 0.005           # mean abs difference (0..1): practically the same image
ACCEPT_MAX = 0.025             # above this a page is no stored slide
ACCEPT_RATIO = 1.4             # otherwise the best match must beat the best other slide by this factor
VERSION_TIE = 0.001            # versions within this distance of the best are indistinguishable at thumbnail size
DECK_SHARE = 0.6               # a PDF is a deck when at least this share of its pages (and >= 3) match
PDF_SKIP_DIR = re.compile(r"^(recherche|research|sources?|quellen|literatur|literature|papers?|whitepaper|"
                          r"\..*|node_modules|venv|__pycache__)$", re.I)


# ------------------------------------------------------------------ manifests
def _sha_file(path: Path) -> str:
    return ex.sha256(path.read_bytes())


def _entry(store: Store, project: str, item, register: bool) -> tuple[int, int] | None:
    """(exhibit id, version n) for one manifest entry: a PNG path (matched by content, registered first when new) or an
    exhibit ref (id, project/slug, project/set/slug: its current version)."""
    s = str(item).strip()
    p = Path(os.path.expanduser(s))
    if s.lower().endswith(".png") and not p.is_absolute():
        root = ex.project_roots_by_slug(store).get(project)
        p = (root / s) if root else p
    if s.lower().endswith(".png"):
        p = Path(os.path.realpath(p))
        if not p.is_file():
            return None
        sha = _sha_file(p)
        row = store.db.execute("SELECT v.exhibit_id, v.n FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
                               "WHERE e.project = ? AND v.png_sha = ? ORDER BY v.created_at DESC, v.n DESC LIMIT 1",
                               (project, sha)).fetchone()
        if row is None and register:
            html = next((h for h in (p.with_suffix(".html"), p.with_suffix(".svg")) if h.is_file()), None)
            r = ex.register(store, html, p, project=project, actor="exhibits")
            return r["exhibit_id"], r["version"]
        return (row[0], row[1]) if row else None
    try:
        r = ex._resolve(store, s, include_internal=True, project=project)
    except (LookupError, ValueError):
        return None
    return r["id"], r["current_version"]


def _majority_set(store: Store, eids: list[int]) -> int | None:
    if not eids:
        return None
    marks = ",".join("?" * len(set(eids)))
    sets = dict(store.db.execute(f"SELECT id, set_id FROM exhibit WHERE id IN ({marks})", list(set(eids))).fetchall())
    c = Counter(sets[e] for e in eids if e in sets)
    return c.most_common(1)[0][0] if c else None


def _save(store: Store, project: str, name: str, items: list[tuple[int, int, float | None]], source: str,
          title: str | None = None, set_id: int | None = None, pdf: Path | None = None, pdf_sha: str | None = None,
          pages: int | None = None, actor: str = "exhibits") -> dict:
    """Create or replace one ordered deck (items: (exhibit id, version, match distance) in slide order)."""
    from fundus import locks
    set_id = set_id or _majority_set(store, [e for e, _, _ in items])
    old = store.db.execute("SELECT * FROM exhibit_sequence WHERE project = ? AND name = ?", (project, name)).fetchone()
    if old is not None and old["set_id"]:
        locks.guard_set(store, old["set_id"], f"changing the deck order '{name}'")
    if set_id:
        locks.guard_set(store, set_id, f"a new deck order '{name}'")
    with store.batch(actor):
        if old is None:
            sid = store.db.execute(
                "INSERT INTO exhibit_sequence(project, name, title, set_id, source, pdf_path, pdf_sha, pages, matched) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (project, name, title or name, set_id, source, str(pdf) if pdf else None,
                                               pdf_sha, pages, len(items))).lastrowid
        else:
            sid = old["id"]
            store.db.execute("UPDATE exhibit_sequence SET title = ?, set_id = ?, source = ?, pdf_path = coalesce(?, pdf_path), "
                             "pdf_sha = coalesce(?, pdf_sha), pages = ?, matched = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                             (title or old["title"], set_id, source, str(pdf) if pdf else None, pdf_sha, pages, len(items), sid))
            store.db.execute("DELETE FROM exhibit_sequence_item WHERE sequence_id = ?", (sid,))
        store.db.executemany("INSERT INTO exhibit_sequence_item(sequence_id, position, exhibit_id, version, score) VALUES (?,?,?,?,?)",
                             [(sid, i + 1, e, n, d) for i, (e, n, d) in enumerate(items)])
        store.log("exhibit_sequence", sid, "insert" if old is None else "update",
                  {"project": project, "name": name, "source": source, "slides": len(items), **({"pdf": str(pdf)} if pdf else {})})
    return get_sequence(store, sid)


def set_sequence(store: Store, project: str, name: str, slides: list, title: str | None = None, set_ref=None,
                 pdf: str | None = None, register: bool = True, actor: str = "exhibits") -> dict:
    """Store the order of a deck as the session built it. slides: PNG paths (absolute or project-relative; new renders are
    registered first) or exhibit refs, in slide order. name: the deck's name ('deck-v4'); title: shown in the portal;
    set_ref: slide set (default: the set most slides belong to); pdf: the PDF built from this sequence (the nightly PDF
    scan then leaves it alone). Returns the deck with its slides and the entries that matched nothing."""
    if project in (None, "", "auto"):   # from the first PNG path (~/Projects/<Project>/…)
        first = next((str(x) for x in slides or [] if str(x).lower().endswith(".png") and os.path.isabs(os.path.expanduser(str(x)))), None)
        project = ex.detect_project(os.path.expanduser(first))[0] if first else ""
    project = _slug(project or "")
    if not project:
        raise ValueError("give the project (or absolute PNG paths under ~/Projects/<project>/)")
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("give the deck a name (e.g. 'deck-v4')")
    if not slides:
        raise ValueError("slides must list the deck's PNG paths or exhibit refs in order")
    items, missing = [], []
    for s in slides:
        hit = _entry(store, project, s, register)
        if hit is None:
            missing.append(str(s))
        else:
            items.append((hit[0], hit[1], None))
    if not items:
        raise LookupError(f"none of the {len(slides)} slides is known in project '{project}'")
    set_id = ex.resolve_set(store, set_ref, project)["id"] if set_ref not in (None, "") else None
    pp = Path(os.path.realpath(os.path.expanduser(pdf))) if pdf else None
    psha = _sha_file(pp) if pp and pp.is_file() else None
    out = _save(store, project, name, items, "manifest", title=title, set_id=set_id, pdf=pp, pdf_sha=psha, actor=actor)
    if psha:
        _seen(store, psha, pp, {"manifest": out["id"]})
    out["missing"] = missing
    return out


def get_sequence(store: Store, ref, project: str | None = None) -> dict:
    s = str(ref)
    if s.isdigit():
        row = store.db.execute("SELECT * FROM exhibit_sequence WHERE id = ?", (int(s),)).fetchone()
    else:
        if "/" in s:
            project, s = s.split("/", 1)
        row = store.db.execute("SELECT * FROM exhibit_sequence WHERE project = ? AND name = ?", (_slug(project or ""), s)).fetchone()
    if row is None:
        raise LookupError(f"no ordered deck '{ref}'")
    out = _brief(row)
    out["slides"] = [{"position": r["position"], "id": r["exhibit_id"], "slug": r["slug"], "version": r["version"],
                      **({"score": round(r["score"], 4)} if r["score"] is not None else {})}
                     for r in store.db.execute("SELECT i.*, e.slug FROM exhibit_sequence_item i JOIN exhibit e ON e.id = i.exhibit_id "
                                               "WHERE i.sequence_id = ? ORDER BY i.position", (row["id"],))]
    return out


def _brief(row) -> dict:
    return {k: row[k] for k in ("id", "project", "name", "title", "set_id", "source", "pdf_path", "pages", "matched",
                                "created_at", "updated_at") if row[k] is not None}


def list_sequences(store: Store, project: str | None = None) -> list[dict]:
    rows = store.db.execute("SELECT * FROM exhibit_sequence" + (" WHERE project = ?" if project else "") +
                            " ORDER BY project, updated_at DESC, id DESC", ([_slug(project)] if project else [])).fetchall()
    return [_brief(r) for r in rows]


def delete_sequence(store: Store, ref, project: str | None = None, actor: str = "exhibits") -> dict:
    from fundus import locks
    seq = get_sequence(store, ref, project)
    if seq.get("set_id"):
        locks.guard_set(store, seq["set_id"], f"deleting the deck order '{seq['name']}'")
    with store.batch(actor):
        store.db.execute("DELETE FROM exhibit_sequence WHERE id = ?", (seq["id"],))
        store.log("exhibit_sequence", seq["id"], "delete", {"project": seq["project"], "name": seq["name"]})
    return {"deleted": seq["id"], "name": seq["name"]}


def gallery_sequences(store: Store, project: str, set_id: int | None = None) -> list[dict]:
    """Ordered decks of a project for the portal (newest first), each with its slides [{id, position, version}]."""
    out = []
    for r in store.db.execute("SELECT * FROM exhibit_sequence WHERE project = ?" + (" AND set_id = ?" if set_id else "") +
                              " ORDER BY updated_at DESC, id DESC", (project, *([set_id] if set_id else []))):
        slides = []
        for i in store.db.execute(
                "SELECT i.exhibit_id, i.position, i.version, v.png_sha, v.html_sha, v.html_ext, e.visibility FROM exhibit_sequence_item i "
                "JOIN exhibit e ON e.id = i.exhibit_id LEFT JOIN exhibit_version v ON v.exhibit_id = i.exhibit_id AND v.n = i.version "
                "WHERE i.sequence_id = ? ORDER BY i.position", (r["id"],)):
            it = {"id": i["exhibit_id"], "position": i["position"], "version": i["version"]}
            if i["png_sha"]:   # the version that is in this deck (older decks show their older slides)
                internal = i["visibility"] == "internal"
                it["preview"] = ex.url(i["png_sha"], "preview.png", project, internal)
                it["png"] = ex.url(i["png_sha"], "png", project, internal)
                if i["html_sha"]:
                    it["html"] = ex.url(i["html_sha"], i["html_ext"] or "html", project, internal)
            slides.append(it)
        out.append({**_brief(r), "slides": slides})
    return out


# ------------------------------------------------------------------ PDF decks
def _canvas(im):
    """A slide as it sits on a 16:9 page (non-16:9 slides centred on white, as deck scripts do)."""
    from PIL import Image
    im = im.convert("RGB")
    W, H = 1600, 900
    s = min(W / im.width, H / im.height)
    im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.BILINEAR)
    c = Image.new("RGB", (W, H), "white")
    c.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    return c


def _thumb(im, size=THUMB) -> np.ndarray:
    from PIL import Image
    return np.asarray(_canvas(im).resize(size, Image.BOX), dtype=np.float32) / 255


def _candidates(store: Store, project: str) -> tuple[list[tuple], np.ndarray | None]:
    from PIL import Image
    cand, vecs = [], []
    for eid, n, sha, created in store.db.execute(
            "SELECT v.exhibit_id, v.n, v.png_sha, v.created_at FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
            "WHERE e.project = ?", (project,)):
        f = ex.file_path(store, sha, "preview.png")
        if not f.is_file():
            f = ex.file_path(store, sha, "png")
        if not f.is_file():
            continue
        try:
            with Image.open(f) as im:
                vecs.append(_thumb(im))
        except OSError:
            continue
        cand.append((eid, n, created, f))
    return cand, (np.stack(vecs) if vecs else None)


def _fine(cand_row, memo: dict) -> np.ndarray:
    from PIL import Image
    f = cand_row[3]
    if f not in memo:
        with Image.open(f) as im:
            memo[f] = _thumb(im, FINE)
    return memo[f]


def pdf_pages(pdf: Path) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per page (coarse thumbnail, fine thumbnail)."""
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["pdftoppm", "-png", "-scale-to", "480", str(pdf), f"{td}/p"], check=True, capture_output=True, timeout=300)
        out = []
        for p in sorted(Path(td).glob("p-*.png"), key=lambda q: int(q.stem.rsplit("-", 1)[1])):
            with Image.open(p) as im:
                c = _canvas(im)
            out.append((np.asarray(c.resize(THUMB, Image.BOX), dtype=np.float32) / 255,
                        np.asarray(c.resize(FINE, Image.BOX), dtype=np.float32) / 255))
        return out


def match_pages(store: Store, project: str, pages: list, made: str | None = None, cache=None) -> list[dict | None]:
    """Per page the matching stored slide {id, version, score} or None. Close calls (near-identical slides such as
    numbered section dividers) get a second look at higher resolution. Between versions too alike to tell apart, the
    newest one made before the PDF wins."""
    cand, M = cache if cache else _candidates(store, project)
    if M is None:
        return [None] * len(pages)
    out, memo = [], {}
    for v, fine in pages:
        d = np.abs(M - v).mean(axis=(1, 2, 3))
        order = np.argsort(d)
        best = int(order[0])
        eid = cand[best][0]
        other = next((float(d[j]) for j in order if cand[j][0] != eid), 1.0)
        b = float(d[best])
        if b > ACCEPT_MAX:
            out.append(None)
            continue
        if not (b <= ACCEPT_EXACT or other >= b * ACCEPT_RATIO):
            top, seen = [], set()   # best version of each of the closest slides, compared at FINE size
            for j in order:
                if cand[j][0] not in seen:
                    seen.add(cand[j][0])
                    top.append(int(j))
                if len(top) == FINE_TOP:
                    break
            fd = sorted((float(np.abs(_fine(cand[j], memo) - fine).mean()), j) for j in top)
            win = fd[0][1]
            if not all(_beats(fine, _fine(cand[win], memo), _fine(cand[j], memo)) for _, j in fd[1:]):
                out.append(None)
                continue
            best, eid = win, cand[win][0]
            b = float(d[best])
        alike = [j for j in order if cand[j][0] == eid and d[j] <= b + VERSION_TIE]
        before = [j for j in alike if not made or (cand[j][2] or "") <= made]
        pick = max(before or alike, key=lambda j: (cand[j][2] or "", cand[j][1]))
        out.append({"id": eid, "version": cand[pick][1], "score": b})
    return out


def _beats(page: np.ndarray, a: np.ndarray, b: np.ndarray) -> bool:
    """On the pixels where candidates a and b differ, is the page clearly closer to a?"""
    mask = np.abs(a - b).max(axis=2) > FINE_DIFF
    if mask.sum() < FINE_MIN_PIXELS:
        return False
    da = float(np.abs(page - a)[mask].mean())
    db = float(np.abs(page - b)[mask].mean())
    return db >= da * FINE_RATIO


def _seen(store: Store, sha: str | None, path: Path, result: dict) -> None:
    if sha:
        store.db.execute("INSERT INTO exhibit_pdf_seen(pdf_sha, path, result) VALUES (?,?,?) ON CONFLICT(pdf_sha) DO UPDATE SET "
                         "path = excluded.path, result = excluded.result, checked_at = CURRENT_TIMESTAMP",
                         (sha, str(path), json.dumps(result, ensure_ascii=False)))
        store.db.commit()


def _pdf_title(pdf: Path) -> str | None:
    try:
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^Title:\s*(.+)$", info, re.M)
    return m.group(1).strip() if m and m.group(1).strip() else None


def import_pdf(store: Store, pdf, project: str | None = None, name: str | None = None, force: bool = False,
               cache=None, actor: str = "importer:exhibits") -> dict:
    """Read a deck PDF: match every page against the project's stored slides and store the order as deck `name`
    (default: file name without _web). Not a deck (too few pages match) → nothing stored. A manifest that names this
    PDF wins; an unchanged PDF is skipped unless force."""
    pdf = Path(os.path.realpath(os.path.expanduser(str(pdf))))
    if not pdf.is_file():
        raise ValueError(f"PDF not found: {pdf}")
    project = _slug(project) if project else ex.detect_project(pdf)[0]
    sha = _sha_file(pdf)
    if not force and store.db.execute("SELECT 1 FROM exhibit_pdf_seen WHERE pdf_sha = ?", (sha,)).fetchone():
        return {"pdf": str(pdf), "status": "unchanged"}
    name = name or re.sub(r"[_-]web$", "", pdf.stem)
    man = store.db.execute("SELECT id FROM exhibit_sequence WHERE project = ? AND source = 'manifest' AND (pdf_path = ? OR "
                           "pdf_path = ? OR name = ?)", (project, str(pdf), str(pdf.with_name(name + pdf.suffix)), name)).fetchone()
    if man and not force:
        _seen(store, sha, pdf, {"manifest": man[0]})
        return {"pdf": str(pdf), "status": "manifest", "deck": man[0]}
    made = ex._ts(pdf.stat().st_mtime)
    pages = pdf_pages(pdf)
    hits = match_pages(store, project, pages, made=made, cache=cache)
    matched = [h for h in hits if h]
    res = {"pdf": str(pdf), "project": project, "pages": len(pages), "matched": len(matched)}
    if len(matched) < 3 or len(matched) < DECK_SHARE * len(pages):
        _seen(store, sha, pdf, {**res, "status": "not_a_deck"})
        return {**res, "status": "not_a_deck"}
    from fundus import locks
    try:
        seq = _save(store, project, name, [(h["id"], h["version"], h["score"]) for h in matched], "pdf",
                    title=_pdf_title(pdf) or name, pdf=pdf, pdf_sha=sha, pages=len(pages), actor=actor)
    except locks.Locked as e:
        return {**res, "status": "locked", "note": str(e)}
    _seen(store, sha, pdf, {**res, "status": "deck", "deck": seq["id"]})
    return {**res, "status": "deck", "deck": seq["id"], "name": seq["name"],
            "unmatched_pages": [i + 1 for i, h in enumerate(hits) if not h]}


def sync_pdfs(store: Store, actor: str = "importer:exhibits") -> dict:
    """Nightly: read new or changed deck PDFs in every project folder with renders (the _web copy of a deck is skipped
    when the full PDF lies next to it)."""
    out = {}
    roots = ex.project_roots_by_slug(store)
    for project, root in sorted(roots.items()):
        pdfs = []
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not PDF_SKIP_DIR.match(d)]
            names = set(files)
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                if re.search(r"[_-]web\.pdf$", f, re.I) and re.sub(r"[_-]web(\.pdf)$", r"\1", f, flags=re.I) in names:
                    continue
                pdfs.append(Path(dirpath) / f)
        cache = None
        res = Counter()
        for p in sorted(pdfs, key=lambda q: q.stat().st_mtime):
            try:
                if store.db.execute("SELECT 1 FROM exhibit_pdf_seen WHERE pdf_sha = ?", (_sha_file(p),)).fetchone():
                    res["unchanged"] += 1
                    continue
                cache = cache or _candidates(store, project)
                r = import_pdf(store, p, project=project, cache=cache, actor=actor)
                res[r["status"]] += 1
            except Exception as e:   # one broken PDF must not stop the others
                store.db.rollback()
                res["failed"] += 1
                out.setdefault("_errors", []).append(f"{p.name}: {type(e).__name__}: {e}"[:200])
        if pdfs:
            out[project] = dict(res)
    return out
