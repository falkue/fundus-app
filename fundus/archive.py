"""Archive: slides that stay findable but are no longer listed in their project and slide set.

An archived slide keeps everything – project, set, versions, decks, verification, sharing – and is still found by
fundus_search, the slide search, saved searches, fundus_exhibit and deck orders (search hits carry `archived`). Only the
portal's project/set lists and their counts leave it out; the portal's "Archiv" view lists all archived slides. A new
render of an archived slide adds its version and the slide stays archived. Unarchiving puts it back into the lists.

Not to be confused with a lock (fundus.locks, "Sperre" in the portal): a lock freezes a slide against changes; the
archive only tidies the lists. Both can apply at once.
"""
from __future__ import annotations

from datetime import datetime

from fundus import exhibits as ex
from fundus.store import Store


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ids(store: Store, target: str, refs, project: str | None = None) -> list[int]:
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    if target == "slide":
        return [ex._resolve(store, r, include_internal=True)["id"] for r in refs]
    if target == "set":
        out = []
        for r in refs:
            s = ex.resolve_set(store, r, project)
            out += [x[0] for x in store.db.execute("SELECT id FROM exhibit WHERE set_id = ?", (s["id"],))]
        return out
    if target == "project":
        out = []
        for r in refs:
            out += [x[0] for x in store.db.execute("SELECT id FROM exhibit WHERE project = ?", (ex._slug(r),))]
        return out
    raise ValueError("target: slide | set | project")


def archive(store: Store, target: str, refs, project: str | None = None, actor: str = "cli") -> dict:
    """Archive slides (target 'slide': ids/refs), all slides of sets ('set') or of projects ('project')."""
    with store.batch(actor):
        ids = _ids(store, target, refs, project)
        if any(store.db.execute("SELECT 1 FROM exhibit WHERE id = ? AND project = ?", (i, ex.TRASH_PROJECT)).fetchone() for i in ids):
            raise ValueError("a slide in the trash cannot be archived – restore it first")
        n = 0
        for i in ids:
            n += store.db.execute("UPDATE exhibit SET archived_at = ?, archived_by = ? WHERE id = ? AND archived_at IS NULL",
                                  (_now(), actor, i)).rowcount
            store.log("exhibit", i, "archive", None)
    return {"archived": n, "already": len(ids) - n, "ids": ids[:200]}


def unarchive(store: Store, target: str, refs, project: str | None = None, actor: str = "cli") -> dict:
    with store.batch(actor):
        ids = _ids(store, target, refs, project)
        n = 0
        for i in ids:
            n += store.db.execute("UPDATE exhibit SET archived_at = NULL, archived_by = NULL WHERE id = ? AND archived_at IS NOT NULL",
                                  (i,)).rowcount
            store.log("exhibit", i, "unarchive", None)
    return {"unarchived": n, "ids": ids[:200]}


def listing(store: Store, q: str | None = None, project: str | None = None, limit: int = 200, offset: int = 0) -> dict:
    """Archived slides, newest archived first; q filters title/slug/so-what/project (all words)."""
    where, args = ["e.archived_at IS NOT NULL", "e.project != ?"], [ex.TRASH_PROJECT]
    if project:
        where.append("e.project = ?")
        args.append(ex._slug(project))
    for w in (q or "").split():
        where.append("(e.title LIKE ? OR e.slug LIKE ? OR e.so_what LIKE ? OR e.project LIKE ? OR e.eyebrow LIKE ?)")
        args += [f"%{w}%"] * 5
    sql = (f"SELECT e.*, s.slug AS set_slug, s.title AS set_title, v.png_sha FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id "
           f"LEFT JOIN exhibit_version v ON v.exhibit_id = e.id AND v.n = e.current_version WHERE {' AND '.join(where)}")
    total = store.db.execute(f"SELECT count(*) FROM ({sql})", args).fetchone()[0]
    names = ex.project_titles(store)
    items = []
    for r in store.db.execute(sql + " ORDER BY e.archived_at DESC, e.id DESC LIMIT ? OFFSET ?", args + [limit, offset]):
        internal = r["visibility"] == "internal"
        it = {"id": r["id"], "ref": f"{r['project']}/{r['set_slug']}/{r['slug']}", "project": r["project"],
              "project_title": names.get(r["project"]), "set": r["set_slug"], "set_title": r["set_title"], "slug": r["slug"],
              "kind": r["kind"], "title": r["title"], "so_what": r["so_what"], "version": r["current_version"],
              "archived_at": r["archived_at"], "archived_by": r["archived_by"]}
        if r["png_sha"]:
            it["preview"] = ex.url(r["png_sha"], "preview.png", r["project"], internal)
            it["png"] = ex.url(r["png_sha"], "png", r["project"], internal)
        items.append({k: v for k, v in it.items() if v not in (None, "")})
    by = dict(store.db.execute("SELECT project, count(*) FROM exhibit WHERE archived_at IS NOT NULL AND project != ? GROUP BY project",
                               (ex.TRASH_PROJECT,)).fetchall())
    return {"total": total, "items": items, "projects": [{"project": p, "title": names.get(p), "n": n} for p, n in sorted(by.items())]}


def count(store: Store) -> int:
    return store.db.execute("SELECT count(*) FROM exhibit WHERE archived_at IS NOT NULL AND project != ?",
                            (ex.TRASH_PROJECT,)).fetchone()[0]
