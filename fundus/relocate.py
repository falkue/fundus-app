"""Rewrite absolute filesystem paths stored in the database after a folder moved (reorganisation of ~/Projects into
category folders, 31 Oct 2026; see fundus/layout.py).

`rewrite(db, old, new)` replaces the path prefix `old` by `new`:
- in PATH_COLUMNS (columns that hold one absolute path) only where the value is `old` or starts with `old + '/'`;
- in free-text columns (notes, update hints, JSON results, trash records) where `old` – or its `~/…` form – occurs as a
  whole path component (followed by '/', the end or a character that cannot continue a folder name).
So `/Projects/Fundus` never touches `/Projects/Fundus-Daten`. Before writing it takes a consistent copy of the database
(SQLite backup API) into data/backup/pre-rewrite-<ts>.sqlite, changes everything in one transaction and writes one
changelog entry. Idempotent: a second run finds nothing. The dry run opens the database read-only.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# (table, column) holding exactly one absolute path
PATH_COLUMNS = (
    ("exhibit_version", "png_path"), ("exhibit_version", "html_path"), ("exhibit_version", "script_path"),
    ("exhibit_deck", "deck_path"), ("exhibit_sequence", "pdf_path"), ("exhibit_pdf_seen", "path"),
    ("photo", "source_path"), ("dataset_file", "source_path"),
)
# free-text / JSON columns that may mention paths: these names in every table, plus the explicit pairs
TEXT_NAMES = ("notes", "note", "description", "update_hint", "verification_note")
TEXT_COLUMNS = (("exhibit_pdf_seen", "result"), ("trash", "origin"), ("trash", "removed"))
# history and derived data stay as they are (the changelog documents the old paths; indexes and caches are rebuilt)
SKIP_TABLES = re.compile(r"^(changelog|meta|sqlite_.*|search_index.*|vec_.*|.*_fts.*|cleanup_journal|query_log)$")
# a folder name continues with a word character, '-', or '.' followed by a word character
_END = r"(?![\w\-])(?!\.\w)"


def _norm(p: str) -> str:
    p = os.path.expanduser(str(p).strip())
    if not os.path.isabs(p):
        from fundus.layout import projects_root
        p = str(projects_root() / p)
    return os.path.normpath(p)


def _tilde(p: str) -> str | None:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home + "/") else None


def _columns(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(table, column, 'path' | 'text') present in this database."""
    out = []
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'")]
    for t in tables:
        if SKIP_TABLES.match(t):
            continue
        cols = {r[1] for r in db.execute(f'PRAGMA table_info("{t}")')}
        for c in sorted(cols):
            if (t, c) in PATH_COLUMNS:
                out.append((t, c, "path"))
            elif c in TEXT_NAMES or (t, c) in TEXT_COLUMNS:
                out.append((t, c, "text"))
    return out


def _plan(db: sqlite3.Connection, old: str, new: str) -> dict[tuple[str, str], list[tuple[object, str]]]:
    """{(table, column): [(rowid, new value)]} for every value that changes."""
    pat_parts = [re.escape(old)]
    t_old = _tilde(old)
    if t_old:
        pat_parts.append(re.escape(t_old))
    rx = re.compile("(" + "|".join(pat_parts) + ")" + _END)
    t_new = _tilde(new)

    def sub_text(v: str) -> str:
        return rx.sub(lambda m: new if m.group(1) == old else (t_new or new), v)

    plan = {}
    n = len(old)
    for t, c, how in _columns(db):
        rows = []
        if how == "path":
            q = f'SELECT rowid, "{c}" FROM "{t}" WHERE "{c}" = ? OR substr("{c}", 1, ?) = ?'
            for rid, v in db.execute(q, (old, n + 1, old + "/")):
                rows.append((rid, new + v[n:]))
        else:
            needles = [old] + ([t_old] if t_old else [])
            where = " OR ".join(f'instr("{c}", ?) > 0' for _ in needles)
            for rid, v in db.execute(f'SELECT rowid, "{c}" FROM "{t}" WHERE typeof("{c}") = \'text\' AND ({where})', needles):
                nv = sub_text(v)
                if nv != v:
                    rows.append((rid, nv))
        if rows:
            plan[(t, c)] = rows
    return plan


def rewrite(db_path, old: str, new: str, dry_run: bool = False, actor: str = "cli:paths-rewrite",
            backup_dir=None) -> dict:
    """Rewrite the path prefix old -> new everywhere (see module doc). Returns counts per 'table.column'."""
    old, new = _norm(old), _norm(new)
    if old == new:
        raise ValueError("--from and --to are the same path")
    from fundus.layout import projects_root
    if old in ("/", str(Path.home()), os.path.normpath(str(projects_root()))) or old.count("/") < 3:
        raise ValueError(f"refusing to rewrite such a short prefix: {old}")
    if new.startswith(old + "/"):
        raise ValueError(f"{new} lies inside {old}: the rewrite would not be idempotent")
    db_path = Path(db_path)
    if dry_run:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            plan = _plan(db, old, new)
        finally:
            db.close()
        counts = {f"{t}.{c}": len(r) for (t, c), r in plan.items()}
        return {"from": old, "to": new, "dry_run": True, "counts": counts, "total": sum(counts.values()),
                "examples": {f"{t}.{c}": r[0][1][:200] for (t, c), r in plan.items()}}

    from fundus.store import ITEM_KINDS, Store
    st = Store(db_path)
    try:
        plan = _plan(st.db, old, new)
        counts = {f"{t}.{c}": len(r) for (t, c), r in plan.items()}
        out = {"from": old, "to": new, "dry_run": False, "counts": counts, "total": sum(counts.values())}
        if not plan:
            out["note"] = "nothing to rewrite (already done or never stored)"
            return out
        bdir = Path(backup_dir) if backup_dir else db_path.parent / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        dst = bdir / f"pre-rewrite-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite"
        with sqlite3.connect(dst) as b:
            st.db.backup(b)
        b.close()
        out["backup"] = str(dst)
        conflicts = {}
        with st.batch(actor):
            plan = _plan(st.db, old, new)   # again inside the transaction: nothing changed between count and write
            for (t, c), rows in plan.items():
                for rid, v in rows:
                    try:
                        st.db.execute(f'UPDATE "{t}" SET "{c}" = ? WHERE rowid = ?', (v, rid))
                    except sqlite3.IntegrityError:
                        conflicts[f"{t}.{c}"] = conflicts.get(f"{t}.{c}", 0) + 1
                        continue
                    if t in ITEM_KINDS:
                        st._touch(t, rid)
                    elif t == "document":
                        st._touch_document(rid)
            counts = {k: v - conflicts.get(k, 0) for k, v in
                      {f"{t}.{c}": len(r) for (t, c), r in plan.items()}.items()}
            st.log("paths", old, "rewrite", {"from": old, "to": new, "counts": counts, "conflicts": conflicts or None,
                                             "backup": str(dst)})
        out.update(counts=counts, total=sum(counts.values()))
        if conflicts:
            out["conflicts"] = conflicts
        return out
    finally:
        st.close()
