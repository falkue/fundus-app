"""Change detection for imports: skip a source whose files AND whose import code are unchanged since the last
successful import. Keeps the nightly `import all` cheap when Fundus holds millions of items.

The fingerprint covers (path, size, mtime) of the source files plus the content of the code that turns them into
items (the importer module and the Store rules: store, units, geo, vocab, publisher lists), so a rule change re-imports
everything automatically. FUNDUS_FORCE_IMPORT=1 forces a full import."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

PKG = Path(__file__).resolve().parent
RULE_FILES = ("store.py", "units.py", "geo.py", "vocab.py", "publisher_aliases.py", "publisher_types.py", "schema.sql")


def _code_hash(module_file: str | Path) -> str:
    h = hashlib.sha1()
    for f in [Path(module_file), *(PKG / n for n in RULE_FILES)]:
        if f.exists():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def source_fingerprint(paths, module_file, patterns=("*",)) -> str:
    """Fingerprint of files (or directory trees, filtered by glob patterns) plus the import code."""
    h = hashlib.sha1(_code_hash(module_file).encode())
    for p in sorted(Path(x) for x in paths):
        files = [p] if p.is_file() else sorted({f for pat in patterns for f in p.rglob(pat) if f.is_file()})
        for f in files:
            st = f.stat()
            h.update(f"{f}|{st.st_size}|{st.st_mtime_ns}".encode())
    return h.hexdigest()


def forced() -> bool:
    return os.environ.get("FUNDUS_FORCE_IMPORT", "") not in ("", "0", "false")


def unchanged(store, key: str, fp: str) -> bool:
    if forced():
        return False
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (f"import:{key}",)).fetchone()
    return bool(r and r[0] == fp)


def remember(store, key: str, fp: str) -> None:
    store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"import:{key}", fp))
    store.db.commit()
