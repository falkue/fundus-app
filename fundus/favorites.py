"""Favourites: slides Falk (portal) or a session (MCP, CLI) marks with a star.

A favourite is a flag on the slide (`exhibit.favorite_at`, `favorite_by`); it follows the slide through moves and new
versions and changes nothing else. The portal filters project and set lists to favourites, the slide search takes
`favorites=True`, and `fundus_slides(['favorites'])` / `'favorites:<project>'` returns them as a list.
"""
from __future__ import annotations

from datetime import datetime

from fundus import exhibits as ex
from fundus.store import Store


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ids(store: Store, refs) -> list[int]:
    refs = refs if isinstance(refs, (list, tuple)) else [refs]
    if not refs:
        raise ValueError("refs: slide ids or 'project/set/slug'")
    return list(dict.fromkeys(ex._resolve(store, r, include_internal=True)["id"] for r in refs))


def mark(store: Store, refs, on: bool = True, actor: str = "cli") -> dict:
    """Mark (on=True) or unmark slides as favourites. Slides in the trash cannot be marked."""
    with store.batch(actor):
        ids = _ids(store, refs)
        n = 0
        for i in ids:
            if on:
                if store.db.execute("SELECT 1 FROM exhibit WHERE id = ? AND project = ?", (i, ex.TRASH_PROJECT)).fetchone():
                    raise ValueError(f"slide {i} is in the trash – restore it first")
                n += store.db.execute("UPDATE exhibit SET favorite_at = ?, favorite_by = ? WHERE id = ? AND favorite_at IS NULL",
                                      (_now(), actor, i)).rowcount
            else:
                n += store.db.execute("UPDATE exhibit SET favorite_at = NULL, favorite_by = NULL WHERE id = ? "
                                      "AND favorite_at IS NOT NULL", (i,)).rowcount
            store.log("exhibit", i, "favorite" if on else "unfavorite", None)
    return {("marked" if on else "unmarked"): n, "unchanged": len(ids) - n, "ids": ids[:200]}


def ids(store: Store, project: str | None = None, set_ref: str | None = None) -> list[int]:
    """Favourite slide ids (newest mark first), optionally of one project or set; never from the trash."""
    where, args = ["favorite_at IS NOT NULL", "project != ?"], [ex.TRASH_PROJECT]
    if project:
        p = ex._slug(project)
        where.append("project = ?")
        args.append(p)
        if set_ref not in (None, ""):
            where.append("set_id = ?")
            args.append(ex.resolve_set(store, set_ref, p)["id"])
    return [r[0] for r in store.db.execute(f"SELECT id FROM exhibit WHERE {' AND '.join(where)} "
                                           "ORDER BY favorite_at DESC, id DESC", args)]


def listing(store: Store, project: str | None = None, set_ref: str | None = None, include_internal: bool = True,
            visible_project: str | None = None) -> dict:
    """The favourites as slide briefs (ref, project, set, title, stage, urls), with counts per project."""
    from fundus import slide_search as ss
    res = ss.resolve_refs(store, [f"favorites:{project}/{set_ref}" if project and set_ref not in (None, "") else
                                  f"favorites:{project}" if project else "favorites"],
                          include_internal=include_internal, visible_project=visible_project)
    by = dict(store.db.execute("SELECT project, count(*) FROM exhibit WHERE favorite_at IS NOT NULL AND project != ? "
                               "GROUP BY project ORDER BY 2 DESC", (ex.TRASH_PROJECT,)).fetchall())
    return {"count": res["count"], "by_project": by, "slides": res["slides"]}
