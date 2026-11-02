"""Foliensuche: semantic search over slides only, across all projects and slide sets.

search(): hybrid retrieval (query.search, kind exhibit) → filters by creation year and confidence (verification stage
0-3) → the local LLM (gemma4:26b) groups the hits into 3-6 aspects of the question and drops
unrelated ones. Without the LLM the hits come back ungrouped (`aspects_note` says why).
Saved searches (slide_collection): a name, the question, filters, aspects and a fixed selection of slides; can be edited
and searched again. resolve_refs() combines saved searches, slide sets and decks into one deduplicated slide list
('search:<slug>', 'set:<project>/<set>', 'deck:<project>/<deck>') – what sessions use to work on a pre-selection.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fundus import exhibits as ex
from fundus import reviews as rv
from fundus.store import Store

POOL = 120            # hits retrieved before the filters
ASPECT_TIMEOUT = 75   # s: the portal waits for the grouping; slower (model busy, looping) → hits come ungrouped
SHOW = 40             # hits handed to the aspect grouping / shown
ASPECTS_SYSTEM = """You group slides found for a question into its aspects.
Input: the question and numbered slides (id, project, title, key message).
Return JSON {"aspects": [{"name": "...", "summary": "...", "slides": [ids]}], "unrelated": [ids]}.
- 3 to 6 aspects that answer different parts of the question; name = 2-5 words, summary = one sentence what these slides
  show. Write names and summaries in the output language given below.
- Every slide id goes into exactly one aspect, or into "unrelated" if it does not help answer the question.
- Order aspects from most to least central; inside an aspect order slides from most to least relevant."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def created_years(store: Store, ids: list[int] | None = None) -> dict[int, int]:
    """Creation year of each slide: the year of its first version (content date of the render)."""
    q = "SELECT exhibit_id, min(created_at) FROM exhibit_version"
    a: list = []
    if ids:
        q += f" WHERE exhibit_id IN ({','.join('?' * len(ids))})"
        a = list(ids)
    return {r[0]: int(r[1][:4]) for r in store.db.execute(q + " GROUP BY exhibit_id", a) if r[1]}


def years(store: Store) -> list[dict]:
    """Creation years with slide counts (for the year filter)."""
    from collections import Counter
    c = Counter(created_years(store).values())
    return [{"year": y, "slides": n} for y, n in sorted(c.items(), reverse=True)]


def _years_arg(v) -> list[int] | None:
    if v in (None, "", "all", [], "alle"):
        return None
    vals = v if isinstance(v, (list, tuple)) else re.split(r"[,\s]+", str(v))
    out = sorted({int(x) for x in vals if str(x).strip().isdigit()})
    return out or None


def _levels_arg(min_confidence=None, levels=None) -> set[int] | None:
    if levels not in (None, "", []):
        vals = levels if isinstance(levels, (list, tuple)) else re.split(r"[,\s]+", str(levels))
        return {int(x) for x in vals if str(x).strip().isdigit()}
    if min_confidence not in (None, ""):
        return set(range(int(min_confidence), 4))
    return None


def _brief(store: Store, eid: int, stages: dict, yrs: dict, include_internal: bool, project: str | None) -> dict | None:
    row = store.db.execute("SELECT x.* FROM exhibit x WHERE id = ?", (eid,)).fetchone()
    if row is None or row["project"] == ex.TRASH_PROJECT:
        return None
    if row["visibility"] == "internal" and not include_internal and row["project"] != project:
        return None
    s = ex.summary_fields(store, row)
    st = stages.get(eid) or {}
    out = {"id": eid, "ref": f"{row['project']}/{row['slug']}", "project": row["project"], "set": s.get("set"),
           "set_title": s.get("set_title"), "kind": row["kind"], "title": s.get("title"), "so_what": s.get("so_what"),
           "year": yrs.get(eid), "version": row["current_version"], "stage": st.get("stage"), "confidence": st.get("level"),
           "label": st.get("label"), "preview_url": s.get("preview_url"), "png_url": s.get("png_url")}
    if s.get("verified_version"):
        out["verified_version"] = s["verified_version"]
    if row["archived_at"]:
        out["archived"] = row["archived_at"][:10]
    if row["favorite_at"]:
        out["favorite"] = True
    if row["visibility"] == "internal":
        out["visibility"] = "internal"
    return {k: v for k, v in out.items() if v is not None}


def _filter(store: Store, ids: list[int], years_=None, levels=None, project=None, kind=None, favorites=False) -> list[int]:
    if not ids:
        return []
    yrs = created_years(store, ids)
    st = rv.slide_status(store, ids=ids)
    keep = []
    for i in ids:
        r = store.db.execute("SELECT project, kind, favorite_at FROM exhibit WHERE id = ?", (i,)).fetchone()
        if r is None or r["kind"] == "section" or r["project"] == ex.TRASH_PROJECT:
            continue
        if project and r["project"] != ex._slug(project):
            continue
        if kind and r["kind"] != kind:
            continue
        if favorites and not r["favorite_at"]:
            continue
        if years_ and yrs.get(i) not in years_:
            continue
        if levels is not None and (st.get(i) or {}).get("level") not in levels:
            continue
        keep.append(i)
    return keep


LANGS = {"de": "German", "en": "English", "uk": "Ukrainian"}


_ASPECT_CACHE: "OrderedDict[tuple, tuple]" = None     # (question, lang, slide ids + titles) → result; same hits = same groups
ASPECT_CACHE_SIZE = 128


def group_aspects(question: str, slides: list[dict], chat=None, lang: str | None = None) -> tuple[list[dict], list[int], str | None]:
    """(aspects [{name, summary, slides}], unrelated ids, note) – by the local LLM; on failure one group, note says why.
    Results for the same question and the same hits are kept in memory (the model needs ~30 s), failures are not."""
    global _ASPECT_CACHE
    if not slides:
        return [], [], None
    from collections import OrderedDict
    if _ASPECT_CACHE is None:
        _ASPECT_CACHE = OrderedDict()
    key = (" ".join(question.casefold().split()), (lang or "").lower()[:2],
           tuple((s["id"], s.get("title") or "", (s.get("so_what") or "")[:80]) for s in slides))
    if chat is None and key in _ASPECT_CACHE:
        _ASPECT_CACHE.move_to_end(key)
        return _ASPECT_CACHE[key]
    res = _group_aspects(question, slides, chat, lang)
    if chat is None and res[2] is None:
        _ASPECT_CACHE[key] = res
        while len(_ASPECT_CACHE) > ASPECT_CACHE_SIZE:
            _ASPECT_CACHE.popitem(last=False)
    return res


def _group_aspects(question: str, slides: list[dict], chat=None, lang: str | None = None) -> tuple[list[dict], list[int], str | None]:
    try:
        from fundus.entities import OllamaChat
        # ~40 tokens per slide (id, share of a name/summary) is plenty; the cap stops a looping answer within seconds
        chat = chat or OllamaChat(max_tokens=600 + 40 * len(slides), timeout=ASPECT_TIMEOUT)
        if hasattr(chat, "check_capacity"):
            chat.check_capacity()
        lines = [f"{s['id']} | {s['project']} | {s.get('title') or ''} | {(s.get('so_what') or '')[:220]}" for s in slides]
        out_lang = LANGS.get((lang or "").lower()[:2], "the language of the question")
        ans = chat.complete(ASPECTS_SYSTEM, f"Output language: {out_lang}\n\nQuestion: {question}\n\nSlides:\n" + "\n".join(lines))
    except Exception as e:   # Ollama busy or down, bad JSON: the hits stay usable, just not grouped
        return [{"name": "Treffer", "summary": None, "slides": [s["id"] for s in slides]}], [], f"no aspects: {e}"[:200]
    known = {s["id"] for s in slides}
    used, aspects = set(), []
    for a in ans.get("aspects") or []:
        ids = [int(i) for i in a.get("slides") or [] if str(i).isdigit() and int(i) in known and int(i) not in used]
        if ids:
            used.update(ids)
            aspects.append({"name": str(a.get("name") or "Aspekt")[:80], "summary": (str(a.get("summary"))[:300] if a.get("summary") else None),
                            "slides": ids})
    unrelated = [int(i) for i in ans.get("unrelated") or [] if str(i).isdigit() and int(i) in known and int(i) not in used]
    rest = [s["id"] for s in slides if s["id"] not in used and s["id"] not in unrelated]
    if rest:   # the LLM forgot some: keep them visible
        aspects.append({"name": "Weitere Treffer", "summary": None, "slides": rest})
    return aspects, unrelated, None


def search(store: Store, question: str, years=None, min_confidence=None, levels=None, project: str | None = None,
           kind: str | None = None, limit: int = SHOW, aspects: bool = True, include_internal: bool = False,
           visible_project: str | None = None, chat=None, lang: str | None = None, favorites: bool = False) -> dict:
    """Slides for a question, filtered by creation year and confidence (and to favourites), grouped into aspects of the
    question. With favorites and no question: all favourites."""
    from fundus import query as q
    question = (question or "").strip()
    if not question and not favorites:
        raise ValueError("ask a question")
    ys, lv = _years_arg(years), _levels_arg(min_confidence, levels)
    if question:
        res = q.search(store, question, kind="exhibit", limit=POOL, include_internal=include_internal,
                       visible_project=visible_project)
        ids = list(dict.fromkeys(int(r["id"]) for r in res.get("results", [])))
        if favorites:   # favourites the ranked pool missed still count, after the ranked ones
            from fundus import favorites as fav
            ids += [i for i in fav.ids(store, project) if i not in ids]
    else:
        from fundus import favorites as fav
        res, ids, aspects = {}, fav.ids(store, project), False
    cap = 500 if favorites else 80
    ids = _filter(store, ids, ys, lv, project, kind, favorites)[:max(1, min(int(limit), cap))]
    stages, yrs = rv.slide_status(store, ids=ids), created_years(store, ids)
    slides = [b for b in (_brief(store, i, stages, yrs, include_internal, visible_project) for i in ids) if b]
    out = {"question": question, "filters": {k: v for k, v in {"years": ys, "levels": sorted(lv) if lv is not None else None,
                                                                "project": project, "kind": kind,
                                                                "favorites": favorites or None}.items() if v},
           "count": len(slides), "slides": slides}
    if res.get("fallback"):
        out["search_note"] = res["fallback"]
    if aspects:
        asp, unrelated, note = group_aspects(question, slides, chat, lang)
        out["aspects"], out["unrelated"] = asp, unrelated
        if note:
            out["aspects_note"] = note
    return {k: v for k, v in out.items() if v not in (None, [], {})} | {"count": len(slides), "slides": slides}


# ------------------------------------------------------------------ saved searches
def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")).strip("-")
    return s[:60] or "suche"


def _coll(store: Store, ref):
    ref = str(ref).strip()
    for p in ("search:", "suche:"):
        if ref.lower().startswith(p):
            ref = ref[len(p):]
    r = (store.db.execute("SELECT * FROM slide_collection WHERE id = ?", (int(ref),)).fetchone() if ref.isdigit() else None) \
        or store.db.execute("SELECT * FROM slide_collection WHERE slug = ? OR lower(name) = lower(?)", (_slugify(ref), ref)).fetchone()
    if r is None:
        names = [x[0] for x in store.db.execute("SELECT slug FROM slide_collection ORDER BY updated_at DESC LIMIT 30")]
        near = [n for n in names if _slugify(ref)[:5] in n] or names[:8]
        raise LookupError(f"saved search '{ref}' not found" + (f"; known: {', '.join(near)}" if near else ""))
    return r


def save(store: Store, name: str, exhibits: list, question: str | None = None, filters: dict | None = None,
         aspects: list[dict] | None = None, note: str | None = None, by: str = "portal") -> dict:
    """Save a selection under a name. exhibits: ids or refs; aspects [{name, summary, slides}] assign each slide an aspect."""
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise ValueError("a saved search needs a name")
    slug = _slugify(name)
    if store.db.execute("SELECT 1 FROM slide_collection WHERE slug = ?", (slug,)).fetchone():
        raise ValueError(f"a saved search '{slug}' exists already – choose another name or add to it")
    ids = _ids(store, exhibits)
    if not ids:
        raise ValueError("no slides to save")
    of = {int(i): a["name"] for a in aspects or [] for i in a.get("slides") or []}
    cur = store.db.execute("INSERT INTO slide_collection(slug, name, query, filters, aspects, note, created_by) VALUES (?,?,?,?,?,?,?)",
                           (slug, name, question, json.dumps(filters) if filters else None,
                            json.dumps([{"name": a["name"], "summary": a.get("summary")} for a in aspects or []]) if aspects else None,
                            note, by))
    _put(store, cur.lastrowid, ids, of)
    store.log("collection", cur.lastrowid, "create", {"name": name, "slides": len(ids)})
    store.db.commit()
    return get(store, cur.lastrowid)


def _ids(store: Store, exhibits) -> list[int]:
    out = []
    for x in exhibits or []:
        r = ex._resolve(store, x, include_internal=True) if not (isinstance(x, int) or str(x).isdigit()) else \
            store.db.execute("SELECT id FROM exhibit WHERE id = ?", (int(x),)).fetchone()
        if r is None:
            raise LookupError(f"slide {x} not found")
        if r["id"] not in out:
            out.append(r["id"])
    return out


def _put(store: Store, cid: int, ids: list[int], aspect_of: dict | None = None) -> int:
    pos = store.db.execute("SELECT coalesce(max(position), 0) FROM slide_collection_item WHERE collection_id = ?", (cid,)).fetchone()[0]
    n = 0
    for i in ids:
        v = store.db.execute("SELECT current_version FROM exhibit WHERE id = ?", (i,)).fetchone()[0]
        cur = store.db.execute("INSERT OR IGNORE INTO slide_collection_item(collection_id, exhibit_id, position, aspect, version) "
                               "VALUES (?,?,?,?,?)", (cid, i, pos + n + 1, (aspect_of or {}).get(i), v))
        n += cur.rowcount
    store.db.execute("UPDATE slide_collection SET updated_at = ? WHERE id = ?", (_now(), cid))
    return n


def get(store: Store, ref, include_internal: bool = True, visible_project: str | None = None) -> dict:
    r = _coll(store, ref)
    items = store.db.execute("SELECT * FROM slide_collection_item WHERE collection_id = ? ORDER BY position", (r["id"],)).fetchall()
    ids = [i["exhibit_id"] for i in items]
    stages, yrs = rv.slide_status(store, ids=ids), created_years(store, ids)
    slides, hidden = [], 0
    for it in items:
        b = _brief(store, it["exhibit_id"], stages, yrs, include_internal, visible_project)
        if b is None:
            hidden += 1
            continue
        slides.append({**b, "aspect": it["aspect"], "saved_version": it["version"]})
    asp = json.loads(r["aspects"]) if r["aspects"] else []
    names = [a["name"] for a in asp] + sorted({s["aspect"] for s in slides if s.get("aspect")} - {a["name"] for a in asp})
    groups = [{"name": n, "summary": next((a.get("summary") for a in asp if a["name"] == n), None),
               "slides": [s["id"] for s in slides if s.get("aspect") == n]} for n in names]
    groups = [g for g in groups if g["slides"]]
    loose = [s["id"] for s in slides if not s.get("aspect") or s["aspect"] not in names]
    if loose:
        groups.append({"name": "Ohne Aspekt" if groups else "Folien", "summary": None, "slides": loose})
    projects = sorted({s["project"] for s in slides})
    out = {"id": r["id"], "ref": f"search:{r['slug']}", "slug": r["slug"], "name": r["name"], "question": r["query"],
           "filters": json.loads(r["filters"]) if r["filters"] else None, "note": r["note"], "count": len(slides),
           "projects": projects, "internal": any(s.get("visibility") == "internal" for s in slides),
           "aspects": groups, "slides": slides, "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if hidden:
        out["hidden"] = f"{hidden} slides are internal to other projects and not shown"
    return {k: v for k, v in out.items() if v not in (None, [], {})} | {"count": len(slides), "slides": slides}


def list_collections(store: Store) -> list[dict]:
    rows = store.db.execute("SELECT c.*, (SELECT count(*) FROM slide_collection_item i WHERE i.collection_id = c.id) n "
                            "FROM slide_collection c ORDER BY lower(c.name)").fetchall()
    return [{"id": r["id"], "ref": f"search:{r['slug']}", "slug": r["slug"], "name": r["name"], "question": r["query"],
             "slides": r["n"], "updated_at": r["updated_at"]} for r in rows]


def update(store: Store, ref, name: str | None = None, note: str | None = None, add: list | None = None,
           remove: list | None = None, aspect: str | None = None) -> dict:
    """Rename, annotate, add slides (optionally into an aspect) or remove slides."""
    r = _coll(store, ref)
    cid = r["id"]
    if name is not None:
        name = " ".join(str(name).split())[:120]
        if not name:
            raise ValueError("name must not be empty")
        slug = _slugify(name)
        if slug != r["slug"] and store.db.execute("SELECT 1 FROM slide_collection WHERE slug = ?", (slug,)).fetchone():
            raise ValueError(f"a saved search '{slug}' exists already")
        store.db.execute("UPDATE slide_collection SET name = ?, slug = ? WHERE id = ?", (name, slug, cid))
    if note is not None:
        store.db.execute("UPDATE slide_collection SET note = ? WHERE id = ?", (note.strip() or None, cid))
    if add:
        ids = _ids(store, add)
        _put(store, cid, ids, {i: aspect for i in ids} if aspect else None)
    if remove:
        ids = _ids(store, remove)
        store.db.execute(f"DELETE FROM slide_collection_item WHERE collection_id = ? AND exhibit_id IN ({','.join('?' * len(ids))})",
                         (cid, *ids))
    store.db.execute("UPDATE slide_collection SET updated_at = ? WHERE id = ?", (_now(), cid))
    store.log("collection", cid, "update", {"name": name, "add": len(add or []), "remove": len(remove or [])})
    store.db.commit()
    return get(store, cid)


def delete(store: Store, ref) -> dict:
    r = _coll(store, ref)
    store.db.execute("DELETE FROM slide_collection WHERE id = ?", (r["id"],))
    store.log("collection", r["id"], "delete", {"name": r["name"]})
    store.db.commit()
    return {"deleted": r["id"], "name": r["name"]}


def rerun(store: Store, ref, chat=None) -> dict:
    """The saved question with its filters on today's slides: new candidates (not in the selection yet) for adding."""
    r = _coll(store, ref)
    if not r["query"]:
        raise ValueError("this saved search has no question to run again")
    f = json.loads(r["filters"]) if r["filters"] else {}
    res = search(store, r["query"], years=f.get("years"), levels=f.get("levels"), project=f.get("project"), kind=f.get("kind"),
                 include_internal=True, chat=chat)
    have = {x[0] for x in store.db.execute("SELECT exhibit_id FROM slide_collection_item WHERE collection_id = ?", (r["id"],))}
    new = [s for s in res["slides"] if s["id"] not in have]
    aspect_of = {i: a["name"] for a in res.get("aspects") or [] for i in a["slides"]}
    return {"collection": r["id"], "name": r["name"], "new": [{**s, "aspect": aspect_of.get(s["id"])} for s in new],
            "count": len(new), "already": len(have)}


# ------------------------------------------------------------------ references: saved searches, slide sets, decks
def resolve_refs(store: Store, refs: list[str], min_confidence=None, levels=None, years=None, include_internal: bool = True,
                 visible_project: str | None = None) -> dict:
    """One deduplicated slide list from several references:
    'search:<slug>' (saved search) · 'set:<project>/<set slug or id>' (slide set) · 'deck:<project>/<deck name or id>' ·
    'favorites' / 'favorites:<project>[/<set>]' (favourites).
    A bare name is tried as a saved search. Filters by confidence and creation year apply to the union."""
    order, origin = [], {}
    for ref in refs or []:
        ref = str(ref).strip()
        kind, _, rest = ref.partition(":") if ":" in ref else (
            (ref, "", "") if ref.lower() in ("favorites", "favourites", "favoriten") else ("search", "", ref))
        kind = {"suche": "search", "satz": "set", "foliensatz": "set"}.get(kind.lower(), kind.lower())
        if kind in ("favorites", "favourites", "favoriten"):
            from fundus import favorites as fav
            proj, _, name = rest.partition("/")
            ids = fav.ids(store, proj or None, name or None)
        elif kind == "search":
            ids = [i[0] for i in store.db.execute("SELECT exhibit_id FROM slide_collection_item WHERE collection_id = ? ORDER BY position",
                                                  (_coll(store, rest)["id"],))]
        elif kind in ("set", "deck"):
            proj, _, name = rest.partition("/")
            if not name:
                raise ValueError(f"{kind} reference needs project/name: '{ref}'")
            _, _, _, ids = rv._slides_for(store, ex._slug(proj), set_ref=name if kind == "set" else None,
                                          deck=name if kind == "deck" else None)
        else:
            raise ValueError(f"unknown reference '{ref}' – use search:<name>, set:<project>/<set>, deck:<project>/<deck>, favorites[:<project>[/<set>]]")
        for i in ids:
            if i not in origin:
                order.append(i)
                origin[i] = []
            origin[i].append(ref)
    lv, ys = _levels_arg(min_confidence, levels), _years_arg(years)
    keep = _filter(store, order, ys, lv) if (lv is not None or ys) else [i for i in order]
    stages, yrs = rv.slide_status(store, ids=keep), created_years(store, keep)
    slides = []
    for i in keep:
        b = _brief(store, i, stages, yrs, include_internal, visible_project)
        if b:
            slides.append({**b, "from": origin[i]})
    from collections import Counter
    return {"refs": refs, "count": len(slides), "projects": sorted({s["project"] for s in slides}),
            "stages": dict(Counter(s.get("stage") for s in slides if s.get("stage"))),
            "dropped_by_filter": len(order) - len(keep), "slides": slides}


def slide_files(store: Store, items: list[str], version: str = "auto") -> dict:
    """Slides as files in presentation order – for sessions that build something from a deck (HTML presentation …).
    items: 'deck:<project>/<deck>', 'set:<project>/<set>', 'search:<name>', exhibit ids/refs ('exhibit:<ref>' or bare) or
    paths of rendered PNG/HTML files (matched by content). version: 'auto' (a deck's own version, else the current one) |
    'current' | 'verified' (the verified version where one exists, else as 'auto'). Each slide: ref, title, so_what, stage,
    confidence, version, html (stored copy, else the original path), png, and 'warning' when it is unchecked or a newer
    unchecked version was skipped. Unregistered files come back as {file, registered: false}."""
    from pathlib import Path
    from fundus import decks
    if version not in ("auto", "current", "verified"):
        raise ValueError("version: auto | current | verified")
    picks: list[tuple[int | None, int | None, str, str | None]] = []   # (exhibit id, pinned version, origin, file)
    for item in items or []:
        item = str(item).strip()
        kind, _, rest = item.partition(":")
        kind = {"suche": "search", "satz": "set", "foliensatz": "set"}.get(kind.lower(), kind.lower()) if rest else ""
        if kind == "deck":
            proj, _, name = rest.partition("/")
            seq = decks.get_sequence(store, name, project=ex._slug(proj))
            picks += [(s["id"], s["version"], item, None) for s in seq["slides"]]
        elif kind in ("set", "search"):
            picks += [(i, None, item, None) for i in
                      (x["id"] for x in resolve_refs(store, [item], include_internal=True)["slides"])]
        elif kind == "exhibit" or (not kind and not Path(item).suffix):
            picks.append((ex._resolve(store, rest if kind else item, include_internal=True)["id"], None, item, None))
        else:
            p = Path(item).expanduser()
            if not p.is_file():
                raise LookupError(f"no file '{item}'")
            sha = ex.sha256(p.read_bytes())
            col = "html_sha" if p.suffix.lower() in (".html", ".htm", ".svg") else "png_sha"
            r = store.db.execute(f"SELECT exhibit_id, n FROM exhibit_version WHERE {col} = ? ORDER BY n DESC LIMIT 1", (sha,)).fetchone()
            picks.append((r[0], r[1], item, str(p.resolve())) if r else (None, None, item, str(p.resolve())))
    ids = [i for i, *_ in picks if i]
    stages = rv.slide_status(store, ids=ids) if ids else {}
    out, seen = [], set()
    for eid, pinned, origin, file in picks:
        if eid is None:
            out.append({"file": file, "registered": False, "from": origin})
            continue
        if (eid, pinned) in seen:
            continue
        seen.add((eid, pinned))
        row = store.db.execute("SELECT * FROM exhibit WHERE id = ?", (eid,)).fetchone()
        st = stages.get(eid) or {}
        ver = st.get("verified") or {}
        n = pinned if (pinned and version == "auto") or (pinned and version == "verified" and not ver) else row["current_version"]
        if version == "verified" and ver:
            n = ver["version"]
        v = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, n)).fetchone()
        if v is None:
            continue
        html = None
        if v["html_sha"]:
            stored = ex.file_path(store, v["html_sha"], v["html_ext"] or "html")
            html = str(stored) if stored.is_file() else (v["html_path"] if v["html_path"] and Path(v["html_path"]).is_file() else None)
        png = ex.file_path(store, v["png_sha"], "png")
        s = {"id": eid, "ref": f"{row['project']}/{row['slug']}", "project": row["project"], "slug": row["slug"],
             "kind": row["kind"], "title": v["title"] or row["title"], "so_what": row["so_what"], "sources": row["sources"],
             "version": n, "current_version": row["current_version"], "stage": st.get("stage"), "confidence": st.get("level"),
             "label": st.get("label"), "html": html, "html_source": v["html_path"],
             "png": str(png) if png.is_file() else v["png_path"],
             "width": v["width"], "height": v["height"], "from": origin}
        if ver:
            s["verified_version"] = ver["version"]
        if row["visibility"] == "internal":
            s["visibility"] = "internal"
        warn = []
        if row["kind"] != "section" and (st.get("level") or 0) < 2:
            warn.append(f"confidence {st.get('level') or 0} ({st.get('label') or 'unchecked'})")
        if ver and n != ver["version"]:
            warn.append(f"v{n} is not the verified version (v{ver['version']})")
        if warn:
            s["warning"] = "; ".join(warn)
        out.append({k: x for k, x in s.items() if x is not None})
    return {"items": items, "version": version, "count": len(out), "slides": out}
