"""Projects: own display names for project slugs. The slug ('northwind', 'acme-dealerships') stays the identity in
tags, origin, exhibits and slide sets; the title is only what people see (portal, listings)."""
from __future__ import annotations

from .store import Store, slug as _slug

TITLE_MAX = 120


def titles(store: Store) -> dict[str, str]:
    """{slug: title} of all projects with an own name."""
    return dict(store.db.execute("SELECT slug, title FROM project").fetchall())


def known(store: Store) -> list[str]:
    """Every project slug Fundus knows: slide sets, exhibits, project tags, import origins and named projects."""
    rows = store.db.execute(
        "SELECT project FROM exhibit_set UNION SELECT project FROM exhibit UNION "
        "SELECT value FROM tag WHERE facet = 'project' UNION SELECT project FROM origin WHERE project IS NOT NULL "
        "UNION SELECT slug FROM project").fetchall()
    return sorted({r[0] for r in rows if r[0] and r[0] != "_trash"})   # _trash: the hidden trash project (fundus.trash)


def list_projects(store: Store) -> list[dict]:
    t = titles(store)
    return [{"project": p, "title": t.get(p)} for p in known(store)]


def set_title(store: Store, project: str, title: str | None, actor: str = "cli") -> dict:
    """Give a project its own name; empty/None removes it (the slug is shown again). The slug never changes."""
    p = _slug(project or "")
    if not p or p not in known(store):
        raise LookupError(f"unknown project '{project}'")
    new = " ".join((title or "").split()) or None
    if new and len(new) > TITLE_MAX:
        raise ValueError(f"title longer than {TITLE_MAX} characters")
    old = titles(store).get(p)
    if new == old:
        return {"project": p, "title": old, "changed": False}
    with store.batch(actor):
        if new:
            store.db.execute("INSERT INTO project(slug, title) VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET "
                             "title = excluded.title, updated_at = CURRENT_TIMESTAMP", (p, new))
        else:
            store.db.execute("DELETE FROM project WHERE slug = ?", (p,))
        store.log("project", p, "update", {"title": {"old": old, "new": new}})
    return {"project": p, "title": new, "changed": True}


# Order of the projects in the portal's sidebar (one setting for every device, stored in meta)
SORTS = ("name_asc", "name_desc", "created_desc", "created_asc")
SORT_KEY = "portal:project_sort"


def get_sort(store: Store) -> str:
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (SORT_KEY,)).fetchone()
    return r[0] if r and r[0] in SORTS else "name_asc"


def set_sort(store: Store, sort: str, actor: str = "cli") -> dict:
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {', '.join(SORTS)}")
    old = get_sort(store)
    with store.batch(actor):
        store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (SORT_KEY, sort))
        if sort != old:
            store.log("portal", "project_sort", "update", {"project_sort": {"old": old, "new": sort}})
    return {"project_sort": sort}


def created(store: Store) -> dict[str, str]:
    """{project: creation date}: its first slide (content date), else its first slide set (projects without slides)."""
    out = dict(store.db.execute("SELECT project, min(created_at) FROM exhibit_set GROUP BY project").fetchall())
    out.update({p: c for p, c in store.db.execute("SELECT project, min(created_at) FROM exhibit GROUP BY project") if c})
    return out
