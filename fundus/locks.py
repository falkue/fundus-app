"""Locks for the long-term archive: a slide set (Foliensatz, e.g. a presentation as given) or a single slide is frozen.

Locked means: no new version of a slide (a re-render is refused, the archived version stays), no move into another set,
no new slide in a locked set, no folder or name change of the set, no new or replaced deck order of the set. Reviews,
feedback and sharing settings stay possible – they do not change the slides.

A lock is removed only by copying (the copy is a new, unlocked slide or set; an unmodified copy shares the verification
stage with its original) or by Falk himself: in the portal with confirmation, or interactively in the terminal
(`fundus exhibits unlock …`). There is deliberately no MCP tool to unlock. Every lock and unlock stays in exhibit_lock
as history and in the change log.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from fundus import exhibits as ex
from fundus.store import Store

UNLOCK_HINT = ("only Falk can unlock (portal, with confirmation); to work on it, copy it: "
               "fundus_exhibit_copy(exhibit=…, to_set=…) or fundus_exhibit_copy(set=…, title=…, folder=…)")


class Locked(PermissionError):
    """A write on a locked slide or set."""


def _brief(row) -> dict:
    return {"lock": row["id"], "target": row["target"], "locked_at": row["locked_at"], "locked_by": row["locked_by"],
            **({"note": row["note"]} if row["note"] else {})}


def set_lock(store: Store, set_id: int) -> dict | None:
    r = store.db.execute("SELECT * FROM exhibit_lock WHERE target = 'set' AND target_id = ? AND unlocked_at IS NULL", (set_id,)).fetchone()
    return _brief(r) if r else None


def exhibit_lock(store: Store, eid: int) -> dict | None:
    """The lock that freezes a slide: its own or its set's (None: free)."""
    r = store.db.execute("SELECT l.* FROM exhibit_lock l WHERE l.unlocked_at IS NULL AND ((l.target = 'exhibit' AND l.target_id = ?) "
                         "OR (l.target = 'set' AND l.target_id = (SELECT set_id FROM exhibit WHERE id = ?))) "
                         "ORDER BY l.target = 'exhibit' DESC LIMIT 1", (eid, eid)).fetchone()
    return _brief(r) if r else None


def states(store: Store, project: str | None = None) -> dict:
    """Active locks for the gallery: {'sets': {set id: brief}, 'exhibits': {exhibit id: brief}} (own slide locks only)."""
    out = {"sets": {}, "exhibits": {}}
    q = ("SELECT l.* FROM exhibit_lock l LEFT JOIN exhibit_set s ON l.target = 'set' AND s.id = l.target_id "
         "LEFT JOIN exhibit e ON l.target = 'exhibit' AND e.id = l.target_id WHERE l.unlocked_at IS NULL")
    args = []
    if project:
        q += " AND coalesce(s.project, e.project) = ?"
        args.append(ex._slug(project))
    for r in store.db.execute(q, args):
        out["sets" if r["target"] == "set" else "exhibits"][r["target_id"]] = _brief(r)
    return out


def _name(store: Store, eid: int) -> str:
    r = store.db.execute("SELECT e.project, s.slug set_slug, e.slug FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id "
                         "WHERE e.id = ?", (eid,)).fetchone()
    return f"{r['project']}/{r['set_slug']}/{r['slug']}" if r else str(eid)


def guard_exhibit(store: Store, eid: int, what: str) -> None:
    lk = exhibit_lock(store, eid)
    if lk:
        how = "its slide set is locked" if lk["target"] == "set" else "it is locked"
        raise Locked(f"slide {_name(store, eid)} is archived ({how} since {lk['locked_at']}): {what} refused – {UNLOCK_HINT}")


def guard_set(store: Store, set_id: int, what: str) -> None:
    lk = set_lock(store, set_id)
    if lk:
        r = store.db.execute("SELECT project, slug FROM exhibit_set WHERE id = ?", (set_id,)).fetchone()
        raise Locked(f"slide set {r['project']}/{r['slug']} is archived (locked since {lk['locked_at']}): {what} refused – {UNLOCK_HINT}")


# ------------------------------------------------------------------ while a review runs
# A slide is frozen while it is under review – nobody should change what a reviewer is looking at. Derived from the
# reviews themselves (no lock row): it starts when Gemini is queued or the review is published to people, and ends by
# itself when Gemini is done, every invited person has finished or lost the assignment, the review is closed, or its
# invitation expires (review.expires_at).
_IN_REVIEW = ("SELECT r.id, r.title, r.expires_at, r.gemini_state, s.exhibit_id, "
              "(SELECT group_concat(rv.first_name || ' ' || rv.last_name, ', ') FROM review_access a JOIN reviewer rv ON rv.id = a.reviewer_id "
              " WHERE a.review_id = r.id AND a.revoked_at IS NULL AND a.finished_at IS NULL) AS people "
              "FROM review r JOIN review_slide s ON s.review_id = r.id "
              "WHERE r.status != 'closed' AND (r.expires_at IS NULL OR r.expires_at > ?) "
              "AND (r.gemini_state IN ('queued','running') OR (r.status = 'open' AND EXISTS (SELECT 1 FROM review_access a "
              "     WHERE a.review_id = r.id AND a.revoked_at IS NULL AND a.finished_at IS NULL)))")


def _review_brief(r) -> dict:
    gem = r["gemini_state"] in ("queued", "running")
    return {"review": r["id"], "title": r["title"], "people": r["people"] or None, "gemini": gem,
            "until": r["expires_at"] if r["people"] else None}


def review_locks(store: Store, ids=None, project: str | None = None) -> dict[int, list[dict]]:
    """{exhibit id: [running reviews that freeze it]} for the given slides or a project."""
    q, a = _IN_REVIEW, [ex._now()]
    if ids is not None:
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        q += f" AND s.exhibit_id IN ({','.join('?' * len(ids))})"
        a += ids
    if project:
        q += " AND r.project = ?"
        a.append(ex._slug(project))
    out: dict[int, list[dict]] = {}
    for r in store.db.execute(q, a):
        lst = out.setdefault(r["exhibit_id"], [])
        if not any(x["review"] == r["id"] for x in lst):
            lst.append(_review_brief(r))
    return out


def guard_review(store: Store, eid: int, what: str) -> None:
    rs = review_locks(store, [eid]).get(eid)
    if not rs:
        return
    parts = []
    for r in rs:
        who = (f"Gemini" if r["gemini"] else "") + (" and " if r["gemini"] and r["people"] else "") + (r["people"] or "")
        parts.append(f"review #{r['review']} ({who}{', invitation until ' + r['until'][:10] if r['until'] else ''})")
    raise Locked(f"slide {_name(store, eid)} is under review: {'; '.join(parts)} – {what} refused until the review is done, "
                 "the reviewer's assignment is withdrawn or the invitation expires; to work on it now, copy it (fundus_exhibit_copy)")


# ------------------------------------------------------------------ lock / unlock
def lock(store: Store, set_ref=None, exhibit=None, project: str | None = None, note: str | None = None, actor: str = "cli") -> dict:
    """Lock a slide set (set_ref: id, 'project/set' or slug with project) or a single slide (exhibit: id or ref)."""
    if bool(set_ref) == bool(exhibit):
        raise ValueError("give either set (a slide set) or exhibit (one slide)")
    if set_ref:
        row = ex.resolve_set(store, set_ref, project)
        target, tid, label = "set", row["id"], f"{row['project']}/{row['slug']}"
        vers = {str(r[0]): r[1] for r in store.db.execute("SELECT id, current_version FROM exhibit WHERE set_id = ?", (tid,))}
        had = set_lock(store, tid)
    else:
        row = ex._resolve(store, exhibit, include_internal=True, project=ex._slug(project) if project else None)
        target, tid, label = "exhibit", row["id"], _name(store, row["id"])
        vers = {str(row["id"]): row["current_version"]}
        had = store.db.execute("SELECT * FROM exhibit_lock WHERE target = 'exhibit' AND target_id = ? AND unlocked_at IS NULL",
                               (tid,)).fetchone()
        had = _brief(had) if had else None
    if had:
        return {"target": target, "id": tid, "name": label, "locked": True, "already": True, **had}
    with store.batch(actor):
        lid = store.db.execute("INSERT INTO exhibit_lock(target, target_id, versions, note, locked_by) VALUES (?,?,?,?,?)",
                               (target, tid, json.dumps(vers), (note or "").strip() or None, actor)).lastrowid
        store.log("exhibit_set" if target == "set" else "exhibit", tid, "lock", {"lock": lid, "versions": vers, "note": note})
    return {"target": target, "id": tid, "name": label, "locked": True, "slides": len(vers),
            **_brief(store.db.execute("SELECT * FROM exhibit_lock WHERE id = ?", (lid,)).fetchone())}


def unlock(store: Store, set_ref=None, exhibit=None, project: str | None = None, note: str | None = None, actor: str = "falk") -> dict:
    """Remove a lock – only for Falk's explicit request (portal with confirmation, interactive terminal). Never from an agent."""
    if bool(set_ref) == bool(exhibit):
        raise ValueError("give either set or exhibit")
    if set_ref:
        row = ex.resolve_set(store, set_ref, project)
        target, tid, label = "set", row["id"], f"{row['project']}/{row['slug']}"
    else:
        row = ex._resolve(store, exhibit, include_internal=True, project=ex._slug(project) if project else None)
        target, tid, label = "exhibit", row["id"], _name(store, row["id"])
    r = store.db.execute("SELECT * FROM exhibit_lock WHERE target = ? AND target_id = ? AND unlocked_at IS NULL", (target, tid)).fetchone()
    if r is None:
        hint = ""
        if target == "exhibit" and exhibit_lock(store, tid):
            hint = " – it is frozen by its slide set's lock; unlock the set"
        raise LookupError(f"{label} has no lock of its own{hint}")
    with store.batch(actor):
        store.db.execute("UPDATE exhibit_lock SET unlocked_at = ?, unlocked_by = ?, unlock_note = ? WHERE id = ?",
                         (ex._now(), actor, (note or "").strip() or None, r["id"]))
        store.log("exhibit_set" if target == "set" else "exhibit", tid, "unlock", {"lock": r["id"], "note": note})
    return {"target": target, "id": tid, "name": label, "locked": False, "lock": r["id"]}


def history(store: Store, project: str | None = None) -> list[dict]:
    q = ("SELECT l.*, coalesce(s.project, e.project) project, coalesce(s.slug, e.slug) slug FROM exhibit_lock l "
         "LEFT JOIN exhibit_set s ON l.target = 'set' AND s.id = l.target_id LEFT JOIN exhibit e ON l.target = 'exhibit' AND e.id = l.target_id")
    args = []
    if project:
        q += " WHERE coalesce(s.project, e.project) = ?"
        args.append(ex._slug(project))
    return [{k: r[k] for k in ("id", "target", "target_id", "project", "slug", "note", "locked_by", "locked_at", "unlocked_by",
                                "unlocked_at", "unlock_note") if r[k] is not None}
            for r in store.db.execute(q + " ORDER BY l.id DESC", args)]


# ------------------------------------------------------------------ copies (unlocked)
def _project_root(store: Store, project: str) -> Path:
    root = ex.project_roots_by_slug(store).get(project)
    if root is None:
        raise LookupError(f"project folder of '{project}' not found under ~/Projects")
    return root


def _target_folder(store: Store, set_row, folder: str | None) -> str:
    if folder:
        return ex.norm_folder(folder, set_row["project"])
    fl = [r[0] for r in store.db.execute("SELECT folder FROM exhibit_set_folder WHERE set_id = ? AND folder != '' ORDER BY folder",
                                         (set_row["id"],))]
    return fl[0] if fl else "exhibits"


def _place(src: Path, dst: Path) -> None:
    if dst.exists():
        if dst.read_bytes() != src.read_bytes():
            raise ValueError(f"{dst} exists with other content – give another slug or folder")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def copy_exhibit(store: Store, exhibit, to_set=None, slug: str | None = None, folder: str | None = None,
                 version: int | None = None, project: str | None = None, actor: str = "cli") -> dict:
    """Copy a slide (default: its current version, i.e. the locked one) into a slide set (default: its own set – only
    when that set is not locked) as a new, unlocked slide: the HTML/PNG are written into the target set's folder (or
    `folder`, project-relative) as <slug>.html/.png and registered there. The copy shares the verification stage with
    the original as long as it is unchanged. Returns the registration of the copy."""
    row = ex._resolve(store, exhibit, include_internal=True, project=ex._slug(project) if project else None)
    dst_set = ex.resolve_set(store, to_set, row["project"]) if to_set not in (None, "") else \
        store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (row["set_id"],)).fetchone()
    guard_set(store, dst_set["id"], "a new slide (copy)")
    n = int(version or row["current_version"])
    v = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (row["id"], n)).fetchone()
    if v is None:
        raise LookupError(f"{_name(store, row['id'])} has no version {n}")
    new_slug = ex.exhibit_slug(slug) if slug else row["slug"]
    if store.db.execute("SELECT 1 FROM exhibit WHERE set_id = ? AND slug = ?", (dst_set["id"], new_slug)).fetchone():
        raise ValueError(f"slide set {dst_set['project']}/{dst_set['slug']} has a slide '{new_slug}' already – give slug=…")
    dest = _project_root(store, row["project"]) / _target_folder(store, dst_set, folder)
    png = dest / f"{new_slug}.png"
    _place(ex.file_path(store, v["png_sha"], "png"), png)
    html = None
    if v["html_sha"]:
        html = dest / f"{new_slug}.{v['html_ext'] or 'html'}"
        _place(ex.file_path(store, v["html_sha"], v["html_ext"] or "html"), html)
    script = v["script_path"] if v["script_path"] and os.path.isfile(v["script_path"]) else None
    out = ex.register(store, html, png, script=script, project=row["project"], set_ref=str(dst_set["id"]), fresh=True,
                      note=f"copy of {_name(store, row['id'])} v{n}", actor=actor)
    out["copy_of"] = {"exhibit": row["id"], "name": _name(store, row["id"]), "version": n}
    return out


def copy_set(store: Store, set_ref, title: str, folder: str, slug: str | None = None, project: str | None = None,
             actor: str = "cli") -> dict:
    """Copy a slide set (typically a locked presentation) into a new, unlocked set with its own folder: every slide in
    its current version, and the set's deck orders (named '<deck> (<new set>)')."""
    from fundus import decks
    src = ex.resolve_set(store, set_ref, project)
    new = ex.create_set(store, src["project"], title, folders=[folder], slug=slug, actor=actor)
    copies, mapping = [], {}
    for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ? ORDER BY id", (src["id"],)).fetchall():
        c = copy_exhibit(store, r[0], to_set=str(new["id"]), actor=actor)
        mapping[r[0]] = c["exhibit_id"]
        copies.append({"from": r[0], "to": c["exhibit_id"], "slug": c["slug"]})
    seqs = []
    for s in store.db.execute("SELECT id, name, title FROM exhibit_sequence WHERE set_id = ? ORDER BY id", (src["id"],)).fetchall():
        ids = [mapping.get(i[0]) for i in store.db.execute("SELECT exhibit_id FROM exhibit_sequence_item WHERE sequence_id = ? "
                                                             "ORDER BY position", (s["id"],))]
        ids = [str(i) for i in ids if i]
        if ids:
            got = decks.set_sequence(store, src["project"], f"{s['name']} ({new['slug']})", ids, title=s["title"],
                                     set_ref=str(new["id"]), actor=actor)
            seqs.append({"from": s["name"], "to": got["name"]})
    return {"project": src["project"], "from": {"id": src["id"], "slug": src["slug"]},
            "set": {"id": new["id"], "slug": new["slug"], "title": new["title"], "folders": new["folders"]},
            "slides": copies, "decks": seqs}
