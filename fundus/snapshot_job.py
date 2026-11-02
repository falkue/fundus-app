"""Backup snapshot for the timer (fundus-snapshot.timer, hourly at :35, before restic-projects at :40).

Writes data/backup/fundus.sqlite only when it is worth it:
- skipped when the content has not changed since the last snapshot (signature over every table's row count, highest
  row id and newest created/updated timestamp, read through a read-only connection; file timestamps are useless because
  every Store() open writes meta);
- but never older than FUNDUS_SNAPSHOT_MAX_HOURS (default 6): edits that change neither a row count nor a timestamp
  (e.g. a status flag on a slide) still reach the backup within that time;
- for a large database (> FUNDUS_SNAPSHOT_LARGE_GB, default 5) at most every FUNDUS_SNAPSHOT_LARGE_HOURS (default 6)
  hours: a multi-GB copy every hour costs I/O for little gain (restic deduplicates, but must read everything).
FUNDUS_SNAPSHOT_FORCE=1 always writes. The live database itself is excluded from restic (restic-projects.sh); this
snapshot is the consistent copy that gets backed up."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

from fundus.store import DEFAULT_DB

SIG_SKIP = {"meta"}                     # written on every Store() open, carries no content
SIG_TS_COLS = ("updated_at", "created_at", "resolved_at", "imported_at", "ts")


def signature(con: sqlite3.Connection) -> str:
    """Row count, highest rowid and newest timestamp of every ordinary table (virtual tables are covered by their
    shadow tables; tables a read-only connection cannot read, e.g. without the vec0 module, are skipped)."""
    parts = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                            "AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"):
        if t in SIG_SKIP:
            continue
        try:
            cols = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
            ts = [c for c in SIG_TS_COLS if c in cols]
            sel = ", ".join(["count(*)", "max(rowid)"] + [f'max("{c}")' for c in ts])
            parts[t] = con.execute(f'SELECT {sel} FROM "{t}"').fetchone()
        except sqlite3.DatabaseError:
            continue
    if "usage" in parts:
        parts["usage_calls"] = con.execute("SELECT sum(calls) FROM usage").fetchone()
    return json.dumps(parts, sort_keys=True, default=str)


def main() -> int:
    db = DEFAULT_DB
    snap = db.parent / "backup" / "fundus.sqlite"
    sig_file = snap.with_suffix(".sig")
    force = os.environ.get("FUNDUS_SNAPSHOT_FORCE", "") not in ("", "0", "false")
    large_gb = float(os.environ.get("FUNDUS_SNAPSHOT_LARGE_GB", "5"))
    large_h = float(os.environ.get("FUNDUS_SNAPSHOT_LARGE_HOURS", "6"))
    max_h = float(os.environ.get("FUNDUS_SNAPSHOT_MAX_HOURS", "6"))
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        sig = signature(src)
        size_gb = db.stat().st_size / 1e9
        if snap.exists() and not force:
            age_h = (time.time() - snap.stat().st_mtime) / 3600
            if sig_file.exists() and sig_file.read_text() == sig and age_h < max_h:
                print(json.dumps({"skipped": "unchanged since last snapshot", "age_hours": round(age_h, 2)}))
                return 0
            if size_gb > large_gb and age_h < large_h:
                print(json.dumps({"skipped": f"large database ({size_gb:.1f} GB): next snapshot after {large_h:g} h",
                                  "age_hours": round(age_h, 2)}))
                return 0
        snap.parent.mkdir(parents=True, exist_ok=True)
        tmp = snap.with_suffix(".tmp")
        with sqlite3.connect(tmp) as out:
            src.backup(out)
        tmp.replace(snap)
        sig_file.write_text(sig)
        print(json.dumps({"snapshot": str(snap), "bytes": snap.stat().st_size, "size_gb": round(size_gb, 3)}))
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
