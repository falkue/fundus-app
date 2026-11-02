"""Layout of the project folders under ~/Projects.

Since the reorganisation of 31 Oct 2026 projects live one level deeper, in category folders:
``~/Projects/<Category>/<Folder>/…`` (Category ∈ CATEGORIES, Folder e.g. ``26_Northwind``). The old flat layout
``~/Projects/<Folder>/…`` keeps working (compatibility symlinks at the old paths, and folders not moved yet).

Project slugs are the identity of a project in Fundus (slides, slide sets, reviews, review loops, locks, decks). A
project that existed before the move keeps the slug its old folder produced: FOLDER_SLUGS maps the new
``<Category>/<Folder>`` to that slug. A new folder gets the slug of its name without the year prefix
(``27_Foo-Bar`` → ``foo-bar``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fundus.store import slug as _slug

CATEGORIES = ("Software", "Websites", "Teaching", "Research", "Clients", "Publications", "Other")

# old folder (relative to ~/Projects) -> new folder; from ~/projects-move/mapping.tsv (31 Oct 2026)
MOVES: dict[str, str] = {}   # one-time folder moves of an installation (none here)


def folder_slug(name: str) -> str:
    """Slug of a project folder in the new layout: its name without the year prefix ('27_Foo-Bar' -> 'foo-bar')."""
    return _slug(re.sub(r"^\d{2}_", "", name)) or _slug(name)


def old_slug(old: str) -> str:
    """Slug the old folder produced before the move: the slug of its own name (for 'Test/kybernet' the leaf, since the
    container folders P/, Test/ and _Archiv/ never held a project of their own in Fundus)."""
    return _slug(old.rsplit("/", 1)[-1])


# new '<Category>/<Folder>' -> slug of the project before the move (only project folders, not sub-folders)
FOLDER_SLUGS = {new: old_slug(old) for old, new in MOVES.items() if new.count("/") == 1}


def projects_root() -> Path:
    from fundus import config
    return config.projects_root()


def project_dir(old: str, env: str | None = None) -> str:
    """Absolute folder of a project given by its old name relative to ~/Projects ('Acme-Dealerships'): the
    environment variable `env` when set, else the new location when it exists, else the old one, else the new one.
    Lets importers work before, during and after the move."""
    if env and os.environ.get(env):
        return os.path.expanduser(os.environ[env])
    root = projects_root()
    cands = [root / MOVES[old]] if old in MOVES else []
    cands.append(root / old)
    for c in cands:
        if c.is_dir():
            return str(c)
    return str(cands[0])
