"""Deleting and moving slides, slide sets and projects – deleting always goes to the trash first.

Trash (Papierkorb): a trashed slide, set or project moves into the hidden project `_trash` (exhibits.TRASH_PROJECT); its
origin is kept in `trash`, together with the deck/saved-search rows that were taken out (so a restore puts everything
back). Trashed slides disappear from the gallery, the slide search, fundus_search, decks and saved searches. After 30 days
(nightly `fundus trash purge --expired`) or when Falk empties the trash in the portal they are deleted for good: rows,
versions, feedback, and the stored files no other slide uses. The evidence of a project (data points, documents) is not
touched – only its slides.

The project folders still hold the files, so the nightly sync and render.sh (or fundus render) would register a deleted slide again. That
is prevented: a slide in the trash is found by its old identity (`exhibit_alias`, reason 'trashed') or because its set
is in the trash, and is skipped; a purged slide leaves its content hashes (`exhibit_tombstone`), so an unchanged file is
skipped too – a new render with changed content is a new slide.

Move: a slide into another set (any project), a set into another project, a project into another project (merge: its
sets move over, its main set joins the target's main set). The old identity stays as an alias ('moved'), so the next
render of the same file adds a version to the moved slide instead of creating a new one.

Archived (locked) slides and sets and slides under review are never moved or deleted (locks.guard_*).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fundus import exhibits as ex
from fundus import locks
from fundus.store import Store

TRASH = ex.TRASH_PROJECT
TRASH_SET = "papierkorb"
DAYS = 30


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rows(store: Store, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in store.db.execute(sql, list(args))]


def _trash_set(store: Store) -> int:
    r = store.db.execute("SELECT id FROM exhibit_set WHERE project = ? AND slug = ?", (TRASH, TRASH_SET)).fetchone()
    if r:
        return r[0]
    now = _now()
    return store.db.execute("INSERT INTO exhibit_set(project, slug, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                            (TRASH, TRASH_SET, "Papierkorb", now, now)).lastrowid


def _free_slug(store: Store, table: str, scope_col: str, scope, slug: str) -> str:
    """slug, slug-2, slug-3 … – the first one not taken in the scope (set or project)."""
    cand, n = slug, 1
    while store.db.execute(f"SELECT 1 FROM {table} WHERE {scope_col} = ? AND slug = ?", (scope, cand)).fetchone():
        n += 1
        cand = f"{slug}-{n}"
    return cand


def _guard(store: Store, eids: list[int], what: str) -> None:
    for eid in eids:
        locks.guard_exhibit(store, eid, what)
        locks.guard_review(store, eid, what)


def _take_out(store: Store, eids: list[int]) -> dict:
    """Remove the slides from decks, deck copies and saved searches; return the rows for a restore."""
    if not eids:
        return {}
    ph = ",".join("?" * len(eids))
    out = {"sequence_items": _rows(store, f"SELECT * FROM exhibit_sequence_item WHERE exhibit_id IN ({ph})", eids),
           "collection_items": _rows(store, f"SELECT * FROM slide_collection_item WHERE exhibit_id IN ({ph})", eids),
           "deck_rows": _rows(store, f"SELECT * FROM exhibit_deck WHERE exhibit_id IN ({ph})", eids)}
    store.db.execute(f"DELETE FROM exhibit_sequence_item WHERE exhibit_id IN ({ph})", eids)
    store.db.execute(f"DELETE FROM slide_collection_item WHERE exhibit_id IN ({ph})", eids)
    store.db.execute(f"DELETE FROM exhibit_deck WHERE exhibit_id IN ({ph})", eids)
    store.db.execute(f"DELETE FROM search_index WHERE item_kind = 'exhibit' AND item_id IN ({ph})", [str(e) for e in eids])
    return out


def _put_back(store: Store, removed: dict) -> None:
    for r in removed.get("collection_items") or []:
        if store.db.execute("SELECT 1 FROM slide_collection WHERE id = ?", (r["collection_id"],)).fetchone():
            store.db.execute(f"INSERT OR IGNORE INTO slide_collection_item({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                             list(r.values()))
    for r in removed.get("deck_rows") or []:
        store.db.execute(f"INSERT OR IGNORE INTO exhibit_deck({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    for r in removed.get("sequence_items") or []:
        if not store.db.execute("SELECT 1 FROM exhibit_sequence WHERE id = ?", (r["sequence_id"],)).fetchone():
            continue
        if store.db.execute("SELECT 1 FROM exhibit_sequence_item WHERE sequence_id = ? AND position = ?",
                            (r["sequence_id"], r["position"])).fetchone():     # the place was taken meanwhile: at the end
            r = {**r, "position": store.db.execute("SELECT coalesce(max(position), 0) + 1 FROM exhibit_sequence_item "
                                                   "WHERE sequence_id = ?", (r["sequence_id"],)).fetchone()[0]}
        store.db.execute(f"INSERT INTO exhibit_sequence_item({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))


def _merge_removed(a: dict, b: dict) -> dict:
    return {k: (a.get(k) or []) + (b.get(k) or []) for k in set(a) | set(b)}


def _label(store: Store, eid: int) -> str:
    r = store.db.execute("SELECT title, slug FROM exhibit WHERE id = ?", (eid,)).fetchone()
    return (r["title"] or r["slug"]) if r else str(eid)


def _record(store: Store, kind: str, label: str, project: str, ref: str, eids: list[int], origin: dict, removed: dict,
            actor: str) -> int:
    now = datetime.now()
    return store.db.execute(
        "INSERT INTO trash(kind, label, project, ref, exhibits, origin, removed, trashed_at, trashed_by, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, label, project, ref, json.dumps(eids), json.dumps(origin, ensure_ascii=False), json.dumps(removed, default=str),
         now.strftime("%Y-%m-%d %H:%M:%S"), actor, (now + timedelta(days=DAYS)).strftime("%Y-%m-%d %H:%M:%S"))).lastrowid


# ------------------------------------------------------------------ to the trash
def trash_slides(store: Store, refs, actor: str = "cli") -> dict:
    """Slides (ids or refs) into the trash – one trash entry per slide."""
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    out = []
    with store.batch(actor):
        rows = [ex._resolve(store, r, include_internal=True) for r in refs]
        if any(r["project"] == TRASH for r in rows):
            raise ValueError("already in the trash")
        _guard(store, [r["id"] for r in rows], "deleting")
        for r in rows:
            locks.guard_set(store, r["set_id"], "deleting a slide of it")
        tset = _trash_set(store)
        for r in rows:
            eid = r["id"]
            removed = _take_out(store, [eid])
            origin = {"project": r["project"], "set_id": r["set_id"], "set_slug": r["set_slug"], "slug": r["slug"]}
            store.db.execute("INSERT OR REPLACE INTO exhibit_alias(project, set_id, slug, exhibit_id, reason) VALUES (?,?,?,?, 'trashed')",
                             (r["project"], r["set_id"], r["slug"], eid))
            store.db.execute("UPDATE exhibit SET project = ?, set_id = ?, slug = ? WHERE id = ?", (TRASH, tset, f"{r['slug']}--{eid}", eid))
            tid = _record(store, "exhibit", _label(store, eid), r["project"], f"{r['project']}/{r['set_slug']}/{r['slug']}",
                          [eid], origin, removed, actor)
            store.log("exhibit", eid, "trash", {"trash": tid, **origin})
            out.append({"trash_id": tid, "exhibit_id": eid, "ref": f"{r['project']}/{r['set_slug']}/{r['slug']}"})
    return {"trashed": out, "days": DAYS}


def _trash_set_row(store: Store, s, actor: str) -> tuple[list[int], dict, dict]:
    """Move one set with its slides into the trash (inside a batch). Returns (exhibit ids, origin, removed rows)."""
    locks.guard_set(store, s["id"], "deleting")
    eids = [r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ?", (s["id"],))]
    _guard(store, eids, "deleting")
    removed = _take_out(store, eids)
    seqs = _rows(store, "SELECT * FROM exhibit_sequence WHERE set_id = ?", (s["id"],))
    if seqs:                       # decks of this set go with it (their items were taken out above)
        removed["sequences"] = seqs
        store.db.execute("DELETE FROM exhibit_sequence WHERE set_id = ?", (s["id"],))
    origin = {"project": s["project"], "set_id": s["id"], "set_slug": s["slug"], "title": s["title"], "is_default": s["is_default"]}
    store.db.execute("UPDATE exhibit_set SET project = ?, slug = ?, is_default = 0 WHERE id = ?",
                     (TRASH, f"{s['project']}--{s['slug']}--{s['id']}", s["id"]))
    store.db.execute("UPDATE exhibit SET project = ? WHERE set_id = ?", (TRASH, s["id"]))
    return eids, origin, removed


def trash_set(store: Store, ref, project: str | None = None, actor: str = "cli") -> dict:
    with store.batch(actor):
        s = ex.resolve_set(store, ref, project)
        if s["project"] == TRASH:
            raise ValueError("already in the trash")
        eids, origin, removed = _trash_set_row(store, s, actor)
        tid = _record(store, "set", s["title"] or s["slug"], s["project"], f"{s['project']}/{s['slug']}", eids,
                      {"sets": [origin]}, removed, actor)
        store.log("exhibit_set", s["id"], "trash", {"trash": tid, "slides": len(eids)})
    return {"trash_id": tid, "set": f"{s['project']}/{s['slug']}", "slides": len(eids), "days": DAYS}


def trash_project(store: Store, project: str, actor: str = "cli") -> dict:
    """All slide sets, slides and decks of a project into the trash (its evidence – data points, documents – stays)."""
    p = ex._slug(project)
    if p == TRASH:
        raise ValueError("that is the trash")
    with store.batch(actor):
        sets = store.db.execute("SELECT * FROM exhibit_set WHERE project = ?", (p,)).fetchall()
        if not sets and not store.db.execute("SELECT 1 FROM exhibit WHERE project = ?", (p,)).fetchone():
            raise LookupError(f"project '{p}' has no slides")
        eids, origins, removed = [], [], {}
        for s in sets:
            e, o, r = _trash_set_row(store, s, actor)
            eids += e
            origins.append(o)
            removed = _merge_removed(removed, r)
        seqs = _rows(store, "SELECT * FROM exhibit_sequence WHERE project = ?", (p,))
        if seqs:
            removed["sequences"] = (removed.get("sequences") or []) + seqs
            for q in seqs:
                removed["sequence_items"] = (removed.get("sequence_items") or []) + _rows(
                    store, "SELECT * FROM exhibit_sequence_item WHERE sequence_id = ?", (q["id"],))
            store.db.execute("DELETE FROM exhibit_sequence WHERE project = ?", (p,))
        prow = _rows(store, "SELECT * FROM project WHERE slug = ?", (p,))
        store.db.execute("DELETE FROM project WHERE slug = ?", (p,))
        title = (prow[0]["title"] if prow else None) or p
        tid = _record(store, "project", title, p, p, eids, {"sets": origins, "project_row": prow}, removed, actor)
        store.log("project", p, "trash", {"trash": tid, "slides": len(eids), "sets": len(sets)})
    return {"trash_id": tid, "project": p, "sets": len(sets), "slides": len(eids), "days": DAYS}


def trash(store: Store, target: str, refs, project: str | None = None, actor: str = "cli") -> dict:
    """target 'slide' (refs: one or more slide ids/refs) | 'set' (ref: id or project/set) | 'project' (ref: project slug)."""
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    if target == "slide":
        return trash_slides(store, refs, actor)
    if target == "set":
        return {"trashed": [trash_set(store, r, project, actor) for r in refs]}
    if target == "project":
        return {"trashed": [trash_project(store, r, actor) for r in refs]}
    raise ValueError("target: slide | set | project")


# ------------------------------------------------------------------ restore
def _row(store: Store, tid) -> dict:
    r = store.db.execute("SELECT * FROM trash WHERE id = ?", (int(tid),)).fetchone()
    if r is None:
        raise LookupError(f"no trash entry {tid}")
    if r["state"] != "trashed":
        raise ValueError(f"trash entry {tid} was already {r['state']}")
    return dict(r)


def restore(store: Store, tid, actor: str = "cli") -> dict:
    """Put a trashed slide, set or project back where it was (another slug if the old one is taken meanwhile)."""
    with store.batch(actor):
        t = _row(store, tid)
        origin, removed = json.loads(t["origin"]), json.loads(t["removed"] or "{}")
        notes = []
        if t["kind"] == "exhibit":
            eid = json.loads(t["exhibits"])[0]
            p = origin["project"]
            s = store.db.execute("SELECT id, project FROM exhibit_set WHERE id = ?", (origin["set_id"],)).fetchone()
            set_id = origin["set_id"] if s is not None and s["project"] == p else ex.ensure_default_set(store, p)
            if set_id != origin["set_id"]:
                notes.append("its slide set is gone or in the trash – restored into the main set")
            slug = _free_slug(store, "exhibit", "set_id", set_id, origin["slug"])
            store.db.execute("UPDATE exhibit SET project = ?, set_id = ?, slug = ? WHERE id = ?", (p, set_id, slug, eid))
            store.db.execute("DELETE FROM exhibit_alias WHERE exhibit_id = ? AND reason = 'trashed'", (eid,))
            store._touch("exhibit", eid)
            where = f"{p}/{slug}"
        else:
            if t["kind"] == "project" and origin.get("project_row"):
                for pr in origin["project_row"]:
                    store.db.execute("INSERT OR IGNORE INTO project(slug, title, created_at, updated_at) VALUES (?,?,?,?)",
                                     (pr["slug"], pr["title"], pr["created_at"], pr["updated_at"]))
            for o in origin["sets"]:
                p = o["project"]
                slug = _free_slug(store, "exhibit_set", "project", p, o["set_slug"])
                default = 1 if o.get("is_default") and not store.db.execute(
                    "SELECT 1 FROM exhibit_set WHERE project = ? AND is_default = 1", (p,)).fetchone() else 0
                store.db.execute("UPDATE exhibit_set SET project = ?, slug = ?, is_default = ? WHERE id = ?", (p, slug, default, o["set_id"]))
                store.db.execute("UPDATE exhibit SET project = ? WHERE set_id = ?", (p, o["set_id"]))
                if slug != o["set_slug"]:
                    notes.append(f"set {o['set_slug']} came back as {slug} (the name was taken meanwhile)")
            for q in removed.get("sequences") or []:
                q = {**q, "name": q["name"]}
                if store.db.execute("SELECT 1 FROM exhibit_sequence WHERE project = ? AND name = ?", (q["project"], q["name"])).fetchone():
                    q["name"] = f"{q['name']}-wiederhergestellt"
                store.db.execute(f"INSERT OR IGNORE INTO exhibit_sequence({', '.join(q)}) VALUES ({', '.join('?' * len(q))})", list(q.values()))
            for eid in json.loads(t["exhibits"]):
                store._touch("exhibit", eid)
            where = t["ref"]
        _put_back(store, removed)
        store.db.execute("UPDATE trash SET state = 'restored', done_at = ?, done_by = ? WHERE id = ?", (_now(), actor, t["id"]))
        store.log("trash", t["id"], "restore", {"kind": t["kind"], "ref": t["ref"]})
    return {"restored": t["id"], "kind": t["kind"], "to": where, "notes": notes}


# ------------------------------------------------------------------ purge
def _purge_one(store: Store, t: dict, actor: str) -> dict:
    eids = json.loads(t["exhibits"])
    shas: set[tuple[str, str]] = set()
    if eids:
        ph = ",".join("?" * len(eids))
        for v in store.db.execute(f"SELECT exhibit_id, png_sha, html_sha, html_ext, script_sha FROM exhibit_version "
                                  f"WHERE exhibit_id IN ({ph})", eids):
            shas |= {(v["png_sha"], "png"), (v["png_sha"], "preview.png")}
            if v["html_sha"]:
                shas.add((v["html_sha"], v["html_ext"] or "html"))
            if v["script_sha"]:
                shas.add((v["script_sha"], "py"))
            r = store.db.execute("SELECT project, slug FROM exhibit WHERE id = ?", (v["exhibit_id"],)).fetchone()
            store.db.execute("INSERT OR IGNORE INTO exhibit_tombstone(png_sha, project, slug, purged_at) VALUES (?,?,?,?)",
                             (v["png_sha"], json.loads(t["origin"]).get("project") or t["project"], r["slug"] if r else None, _now()))
        sid = [str(e) for e in eids]
        store.db.execute(f"DELETE FROM exhibit_view WHERE exhibit_id IN ({ph})", eids)
        store.db.execute(f"DELETE FROM exhibit_lock WHERE target = 'exhibit' AND target_id IN ({ph})", eids)
        store.db.execute(f"DELETE FROM entity_link WHERE item_kind = 'exhibit' AND item_id IN ({ph})", sid)
        store.db.execute(f"DELETE FROM search_index WHERE item_kind = 'exhibit' AND item_id IN ({ph})", sid)
        store.db.execute(f"DELETE FROM exhibit_alias WHERE exhibit_id IN ({ph})", eids)
        store.db.execute(f"DELETE FROM exhibit WHERE id IN ({ph})", eids)          # versions, links, feedback … cascade
        # their vectors go with the next embedding run (embed_pending drops vectors of deleted items)
    if t["kind"] in ("set", "project"):
        for o in json.loads(t["origin"]).get("sets") or []:
            store.db.execute("DELETE FROM exhibit_lock WHERE target = 'set' AND target_id = ?", (o["set_id"],))
            store.db.execute("DELETE FROM exhibit_set WHERE id = ? AND project = ?", (o["set_id"], TRASH))
    store.db.execute("UPDATE trash SET state = 'purged', done_at = ?, done_by = ? WHERE id = ?", (_now(), actor, t["id"]))
    store.log("trash", t["id"], "purge", {"kind": t["kind"], "ref": t["ref"], "slides": len(eids)})
    return {"id": t["id"], "slides": len(eids), "shas": shas}


def _drop_files(store: Store, shas: set) -> int:
    """Delete stored files that no remaining slide version uses."""
    n = 0
    for sha, ext in shas:
        col = "png_sha" if ext in ("png", "preview.png") else "html_sha" if ext not in ("py",) else "script_sha"
        if store.db.execute(f"SELECT 1 FROM exhibit_version WHERE {col} = ? LIMIT 1", (sha,)).fetchone():
            continue
        p = ex.file_path(store, sha, ext)
        if p.exists():
            p.unlink()
            n += 1
    return n


def purge(store: Store, ids=None, expired: bool = False, everything: bool = False, actor: str = "cli") -> dict:
    """Delete for good: given trash entries, the expired ones (older than 30 days) or the whole trash."""
    with store.batch(actor):
        if everything:
            rows = _rows(store, "SELECT * FROM trash WHERE state = 'trashed'")
        elif expired:
            rows = _rows(store, "SELECT * FROM trash WHERE state = 'trashed' AND expires_at <= ?", (_now(),))
        else:
            rows = [_row(store, i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
        done = [_purge_one(store, t, actor) for t in rows]
    files = _drop_files(store, set().union(*[d["shas"] for d in done])) if done else 0
    return {"purged": len(done), "slides": sum(d["slides"] for d in done), "files_deleted": files}


# ------------------------------------------------------------------ listing
def listing(store: Store, state: str = "trashed", limit: int = 200) -> dict:
    out = []
    for t in _rows(store, "SELECT * FROM trash WHERE state = ? ORDER BY trashed_at DESC, id DESC LIMIT ?", (state, limit)):
        eids = json.loads(t["exhibits"])
        prev = []
        for eid in eids[:4]:
            v = store.db.execute("SELECT v.png_sha FROM exhibit e JOIN exhibit_version v ON v.exhibit_id = e.id AND v.n = e.current_version "
                                 "WHERE e.id = ?", (eid,)).fetchone()
            if v:
                prev.append(ex.url(v["png_sha"], "preview.png", TRASH, True))
        secs = (datetime.strptime(t["expires_at"], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds()
        left = -(-int(secs) // 86400) if t["state"] == "trashed" else None        # days, rounded up
        out.append({"id": t["id"], "kind": t["kind"], "label": t["label"], "project": t["project"], "ref": t["ref"],
                    "slides": len(eids), "trashed_at": t["trashed_at"], "trashed_by": t["trashed_by"], "expires_at": t["expires_at"],
                    "days_left": max(0, left) if left is not None else None, "previews": prev, "state": t["state"]})
    n = store.db.execute("SELECT count(*), coalesce(sum(json_array_length(exhibits)), 0) FROM trash WHERE state = 'trashed'").fetchone()
    return {"items": out, "entries": n[0], "slides": n[1], "days": DAYS}


# ------------------------------------------------------------------ move
def move_slides(store: Store, refs, to_set, project: str | None = None, actor: str = "cli") -> dict:
    """Slides into another set (`to_set`: id or 'project/set'; any project). The old identity stays as an alias."""
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    out = []
    with store.batch(actor):
        target = ex.resolve_set(store, to_set, project)
        if target["project"] == TRASH:
            raise ValueError("use trash to delete")
        locks.guard_set(store, target["id"], "moving slides into it")
        rows = [ex._resolve(store, r, include_internal=True) for r in refs]
        for r in rows:
            if r["project"] == TRASH:
                raise ValueError(f"{r['slug']} is in the trash – restore it first")
            if r["set_id"] == target["id"]:
                continue
            _guard(store, [r["id"]], "moving")
            locks.guard_set(store, r["set_id"], "moving a slide out of it")
            slug = _free_slug(store, "exhibit", "set_id", target["id"], r["slug"])
            store.db.execute("INSERT OR REPLACE INTO exhibit_alias(project, set_id, slug, exhibit_id, reason) VALUES (?,?,?,?, 'moved')",
                             (r["project"], r["set_id"], r["slug"], r["id"]))
            store.db.execute("UPDATE exhibit SET project = ?, set_id = ?, slug = ?, updated_at = ? WHERE id = ?",
                             (target["project"], target["id"], slug, _now(), r["id"]))
            store.log("exhibit", r["id"], "move", {"from": f"{r['project']}/{r['set_slug']}/{r['slug']}",
                                                   "to": f"{target['project']}/{target['slug']}/{slug}"})
            store._touch("exhibit", r["id"])
            out.append({"exhibit_id": r["id"], "from": f"{r['project']}/{r['set_slug']}/{r['slug']}",
                        "to": f"{target['project']}/{target['slug']}/{slug}"})
    return {"moved": out}


def _move_set_row(store: Store, s, to_project: str, actor: str) -> str:
    locks.guard_set(store, s["id"], "moving")
    eids = [r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ?", (s["id"],))]
    _guard(store, eids, "moving")
    slug = _free_slug(store, "exhibit_set", "project", to_project, s["slug"])
    default = 1 if s["is_default"] and not store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ? AND is_default = 1",
                                                            (to_project,)).fetchone() else 0
    store.db.execute("UPDATE exhibit_set SET project = ?, slug = ?, is_default = ?, updated_at = ? WHERE id = ?",
                     (to_project, slug, default, _now(), s["id"]))
    store.db.execute("UPDATE exhibit SET project = ? WHERE set_id = ?", (to_project, s["id"]))
    for q in store.db.execute("SELECT id, name FROM exhibit_sequence WHERE set_id = ?", (s["id"],)).fetchall():
        name = q["name"]
        while store.db.execute("SELECT 1 FROM exhibit_sequence WHERE project = ? AND name = ? AND id != ?", (to_project, name, q["id"])).fetchone():
            name += "-2"
        store.db.execute("UPDATE exhibit_sequence SET project = ?, name = ? WHERE id = ?", (to_project, name, q["id"]))
    store.db.execute("UPDATE review SET project = ? WHERE set_id = ?", (to_project, s["id"]))
    for e in eids:
        store._touch("exhibit", e)
    store.log("exhibit_set", s["id"], "move", {"from": f"{s['project']}/{s['slug']}", "to": f"{to_project}/{slug}"})
    return f"{to_project}/{slug}"


def move_set(store: Store, ref, to_project: str, project: str | None = None, actor: str = "cli") -> dict:
    to_project = ex._slug(to_project)
    if to_project == TRASH:
        raise ValueError("use trash to delete")
    with store.batch(actor):
        s = ex.resolve_set(store, ref, project)
        if s["project"] == to_project:
            return {"moved": None, "note": "already in that project"}
        to = _move_set_row(store, s, to_project, actor)
    return {"moved": f"{s['project']}/{s['slug']}", "to": to}


def move_project(store: Store, project: str, to_project: str, actor: str = "cli") -> dict:
    """Merge a project into another: its sets move over; its main set's slides join the target's main set (and the
    folders of the main set map there, so new renders land there too)."""
    p, to = ex._slug(project), ex._slug(to_project)
    if p == to or TRASH in (p, to):
        raise ValueError("choose another project")
    moved = []
    with store.batch(actor):
        sets = store.db.execute("SELECT * FROM exhibit_set WHERE project = ?", (p,)).fetchall()
        if not sets:
            raise LookupError(f"project '{p}' has no slide sets")
        target_main = store.db.execute("SELECT id FROM exhibit_set WHERE project = ? AND is_default = 1", (to,)).fetchone()
        for s in sets:
            if s["is_default"] and target_main:
                locks.guard_set(store, s["id"], "moving")
                locks.guard_set(store, target_main[0], "moving slides into it")
                eids = [r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ?", (s["id"],))]
                if eids:
                    _move_slides_in_batch(store, eids, target_main[0])
                store.db.execute("UPDATE exhibit_set_folder SET set_id = ? WHERE set_id = ?", (target_main[0], s["id"]))
                for q in store.db.execute("SELECT id, name FROM exhibit_sequence WHERE set_id = ?", (s["id"],)).fetchall():
                    store.db.execute("UPDATE exhibit_sequence SET set_id = ? WHERE id = ?", (target_main[0], q["id"]))
                store.db.execute("UPDATE review SET set_id = ? WHERE set_id = ?", (target_main[0], s["id"]))
                store.db.execute("DELETE FROM exhibit_set WHERE id = ?", (s["id"],))
                moved.append(f"{p}/{s['slug']} → {to} (Hauptfoliensatz)")
            else:
                moved.append(_move_set_row(store, s, to, actor))
        for q in store.db.execute("SELECT id, name FROM exhibit_sequence WHERE project = ?", (p,)).fetchall():
            name = q["name"]
            while store.db.execute("SELECT 1 FROM exhibit_sequence WHERE project = ? AND name = ? AND id != ?", (to, name, q["id"])).fetchone():
                name += "-2"
            store.db.execute("UPDATE exhibit_sequence SET project = ?, name = ? WHERE id = ?", (to, name, q["id"]))
        store.db.execute("UPDATE exhibit_deck SET project = ? WHERE project = ?", (to, p))
        store.db.execute("UPDATE review SET project = ? WHERE project = ?", (to, p))
        store.db.execute("DELETE FROM project WHERE slug = ?", (p,))
        store.log("project", p, "move", {"into": to, "sets": moved})
    return {"merged": p, "into": to, "sets": moved}


def _move_slides_in_batch(store: Store, eids: list[int], target_set: int) -> None:
    t = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (target_set,)).fetchone()
    for eid in eids:
        r = ex._resolve(store, eid, include_internal=True)
        _guard(store, [eid], "moving")
        slug = _free_slug(store, "exhibit", "set_id", target_set, r["slug"])
        store.db.execute("INSERT OR REPLACE INTO exhibit_alias(project, set_id, slug, exhibit_id, reason) VALUES (?,?,?,?, 'moved')",
                         (r["project"], r["set_id"], r["slug"], eid))
        store.db.execute("UPDATE exhibit SET project = ?, set_id = ?, slug = ? WHERE id = ?", (t["project"], target_set, slug, eid))
        store._touch("exhibit", eid)


def move(store: Store, target: str, refs, to: str, project: str | None = None, actor: str = "cli") -> dict:
    """target 'slide' (to: set id or 'project/set') | 'set' (to: project) | 'project' (to: project – merge)."""
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    if target == "slide":
        return move_slides(store, refs, to, project, actor)
    if target == "set":
        return {"moved": [move_set(store, r, to, project, actor) for r in refs]}
    if target == "project":
        return {"moved": [move_project(store, r, to, actor) for r in refs]}
    raise ValueError("target: slide | set | project")


# ------------------------------------------------------------------ registration (exhibits.register asks these)
def check_register(store: Store, project: str, set_id: int, slug: str, png_sha: str) -> tuple[str | None, int | None, str | None]:
    """(status, exhibit id, note) for a slide about to be registered: 'trashed' (in the trash: skip), 'purged' (this
    content was deleted for good: skip), or (None, moved exhibit id, None) when the slide was moved – None, None, None
    otherwise."""
    s = store.db.execute("SELECT project FROM exhibit_set WHERE id = ?", (set_id,)).fetchone()
    if s and s["project"] == TRASH:
        return "trashed", None, "its slide set is in the trash (Fundus portal → Papierkorb) – restore it there to keep working on it"
    a = store.db.execute("SELECT exhibit_id, reason FROM exhibit_alias WHERE project = ? AND set_id = ? AND slug = ?",
                         (project, set_id, slug)).fetchone()
    if a:
        e = store.db.execute("SELECT project FROM exhibit WHERE id = ?", (a["exhibit_id"],)).fetchone()
        if e is not None and e["project"] == TRASH:
            return "trashed", a["exhibit_id"], "this slide is in the trash (Fundus portal → Papierkorb) – restore it there to keep working on it"
        if e is not None:
            return None, a["exhibit_id"], None
    if store.db.execute("SELECT 1 FROM exhibit_tombstone WHERE png_sha = ?", (png_sha,)).fetchone():
        return "purged", None, "this exact render was deleted from Fundus – a changed render is registered again"
    return None, None, None
