"""Dataset catalog & archive (3 Nov 2026): the admin view over all datasets and the archive duties around them.

    catalog(store, removed=False)          {stats, facets, datasets} for the portal catalog – a few SQL queries, no profile parsing
    log_use(store, ref, tool, …)           usage log (dataset_use); usage_counts / usage_detail derive uses, seen, last use
    publisher_groups(store, publishers)    normalised publisher for facets (entities, own data, cut at the first separator)
    check_lifecycle(store, d, changes)     lifecycle active | superseded (→ successor) | withdrawn; `dormant` is computed
    add_reference(store, path, …)          a folder/file registered WITHOUT copying (holding 'reference'); refresh_locations()
    fixity(store, budget_gb, all)          re-hash stored originals, open Parquet copies; orphan files reported (never deleted
                                           except by purge_orphans, which only Falk triggers)
    citation(d, group) · citation_bibtex   how to cite a dataset
    lineage(store, did) · version_changes(store, d)   two levels up/down; tables/columns/rows changed per version

Reference datasets: the local model only ever sees README/Markdown files and the file listing, never data contents
(Falk's rule). Usage logging never fails the caller.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fundus.dataset_texts import LABELS as ACT_LABELS
from fundus.store import Store, slug as _slug

REAL_USES = ("show", "preview", "query", "path")          # counted as uses (uses_total, uses_30d, last_used, dormant)
SEEN = ("search_hit", "ask_hit", "portal_view")            # reported separately as seen_30d
LIFECYCLES = ("active", "superseded", "withdrawn")
HOLDINGS = ("stored", "reference")
DORMANT_DAYS = 90
OWN_GROUP = "Eigene Daten (Jane Doe)"
MANIFEST_MAX = 2000
ORPHAN_AGE_H = 24
ISO_DAY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-\d{2})?$")
SIZE_CLASSES = (("lt10mb", 10 * 1024 ** 2), ("lt100mb", 100 * 1024 ** 2), ("lt1gb", 1024 ** 3), ("ge1gb", None))

LABELS = {
    "frequency": {"annual": "jährlich", "semiannual": "halbjährlich", "quarterly": "quartalsweise", "monthly": "monatlich",
                  "irregular": "unregelmässig", "once": "einmalig", None: "ohne Rhythmus"},
    "visibility": {"public": "öffentlich", "internal": "intern"},
    "lifecycle": {"active": "aktiv", "superseded": "ersetzt", "withdrawn": "zurückgezogen"},
    "holding": {"stored": "in Fundus gespeichert", "reference": "Referenz"},
    "kind": {"data": "Daten", "crosswalk": "Crosswalk", "text": "Text mit Fassungen"},
    "act_type": ACT_LABELS,
    "size": {"lt10mb": "< 10 MB", "lt100mb": "10–100 MB", "lt1gb": "100 MB – 1 GB", "ge1gb": "≥ 1 GB"},
    "status": {"new": "wird beschrieben", "described": "beschrieben", "failed": "Beschreibung fehlgeschlagen"},
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _did(store: Store, ref) -> int:
    if isinstance(ref, int):
        return ref
    m = re.fullmatch(r"(?:dataset:)?(\d+)", str(ref).strip())
    if m:
        return int(m.group(1))
    from fundus import datasets as ds
    return ds._row(store, ref)["id"]


# ------------------------------------------------------------------ usage log
def log_use(store: Store, ref, tool: str, actor: str | None = None, project: str | None = None, detail=None,
            version: int | None = None) -> None:
    """Record one use of a dataset. Never raises: a failing log must not fail the caller."""
    try:
        did = _did(store, ref)
        if version is None:
            r = store.db.execute("SELECT current_version FROM dataset WHERE id = ?", (did,)).fetchone()
            version = r[0] if r else None
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        store.db.execute("INSERT INTO dataset_use(dataset_id, version, tool, actor, project, detail) VALUES (?,?,?,?,?,?)",
                         (did, version, tool, actor, _slug(project) if project else None, (detail or None) and detail[:500]))
        store.db.commit()
    except Exception:
        pass


def log_uses(store: Store, refs, tool: str, actor: str | None = None, project: str | None = None) -> None:
    for r in refs:
        log_use(store, r, tool, actor=actor, project=project)


def _ago(days: int) -> str:
    # dataset_use.ts is CURRENT_TIMESTAMP (UTC); compare in UTC
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def usage_counts(store: Store) -> dict[int, dict]:
    """{dataset id: {uses_total, uses_30d, last_used, seen_30d}} in one query."""
    real, seen = ",".join(f"'{t}'" for t in REAL_USES), ",".join(f"'{t}'" for t in SEEN)
    d30 = _ago(30)
    out = {}
    for did, tot, u30, last, s30 in store.db.execute(
            f"SELECT dataset_id, sum(tool IN ({real})), sum(tool IN ({real}) AND ts >= ?), max(CASE WHEN tool IN ({real}) THEN ts END), "
            f"sum(tool IN ({seen}) AND ts >= ?) FROM dataset_use GROUP BY dataset_id", (d30, d30)):
        out[did] = {"uses_total": tot or 0, "uses_30d": u30 or 0, "last_used": last, "seen_30d": s30 or 0}
    return out


def usage_detail(store: Store, did: int) -> dict:
    """Uses of one dataset: totals, by tool, per day over 90 days (real uses), the 10 most recent entries."""
    real = ",".join(f"'{t}'" for t in REAL_USES)
    q = store.db.execute
    tot, d30, last = q(f"SELECT sum(tool IN ({real})), sum(tool IN ({real}) AND ts >= ?), max(CASE WHEN tool IN ({real}) THEN ts END) "
                       f"FROM dataset_use WHERE dataset_id = ?", (_ago(30), did)).fetchone()
    by_tool = dict(q("SELECT tool, count(*) FROM dataset_use WHERE dataset_id = ? GROUP BY tool ORDER BY 2 DESC", (did,)).fetchall())
    daily = q(f"SELECT substr(ts, 1, 10), count(*) FROM dataset_use WHERE dataset_id = ? AND tool IN ({real}) AND ts >= ? "
              f"GROUP BY 1 ORDER BY 1", (did, _ago(90))).fetchall()
    recent = [{"ts": r[0], "tool": r[1], "actor": r[2], "project": r[3]} for r in q(
        "SELECT ts, tool, actor, project FROM dataset_use WHERE dataset_id = ? ORDER BY id DESC LIMIT 10", (did,))]
    return {"total": tot or 0, "d30": d30 or 0, "last": last, "by_tool": by_tool, "daily_90": [[d, n] for d, n in daily],
            "recent": recent}


def is_dormant(d, last_used: str | None, today: date | None = None) -> bool:
    """Active, created more than 90 days ago and no real use for 90 days."""
    if (d["lifecycle"] or "active") != "active" or d["removed_at"]:
        return False
    cut = ((today or date.today()) - timedelta(days=DORMANT_DAYS)).isoformat()
    return (d["created_at"] or "")[:10] < cut and (not last_used or last_used[:10] < cut)


# ------------------------------------------------------------------ publisher group
_SEP = re.compile(r" \(|, | – | / ")


def _cut(p: str) -> str:
    return _SEP.split(p, 1)[0].strip(" ,;–-/") or p.strip()


def publisher_groups(store: Store, publishers) -> dict[str, str]:
    """{publisher: group}: own data → OWN_GROUP; an organisation known to fundus.entities (by the full string or the part
    before the first ' (' / ', ' / ' – ' / ' / ') → its canonical name; else that first part."""
    from fundus.entities import norm
    pubs = {p for p in publishers if p}
    out, cand = {}, {}
    for p in pubs:
        if p.strip().casefold().startswith("jane doe"):
            out[p] = OWN_GROUP
        else:
            cand[p] = (norm(p), norm(_cut(p)))
    norms = sorted({n for pair in cand.values() for n in pair if n})
    names: dict[str, str] = {}
    for i in range(0, len(norms), 500):
        chunk = norms[i:i + 500]
        for an, name in store.db.execute(
                f"SELECT a.alias_norm, e.name FROM entity_alias a JOIN entity e ON e.id = a.entity_id WHERE e.kind = 'organisation' "
                f"AND a.alias_norm IN ({','.join('?' * len(chunk))}) ORDER BY e.id DESC", chunk):
            names[an] = name                      # the lowest entity id wins (ordered descending, last write)
    for p, (full, short) in cand.items():
        out[p] = names.get(full) or names.get(short) or _cut(p)
    return out


def publisher_group(store: Store, publisher: str | None) -> str | None:
    return publisher_groups(store, [publisher]).get(publisher) if publisher else None


# ------------------------------------------------------------------ periods, sizes, citation
def period_years(period: str | None) -> tuple[int | None, int | None]:
    """('1999-2026' | '2009-H1 – 2026-H1' | '2025') → (from, to) as years; (None, None) if none found."""
    ys = [int(y) for y in re.findall(r"(?<!\d)(1[89]\d\d|2[01]\d\d)(?!\d)", period or "")]
    return (min(ys), max(ys)) if ys else (None, None)


def size_class(b: int | None) -> str:
    for name, lim in SIZE_CLASSES:
        if lim is None or (b or 0) < lim:
            return name
    return "ge1gb"


def _year(published: str | None) -> str | None:
    m = re.search(r"(?<!\d)(1[89]\d\d|2[01]\d\d)(?!\d)", published or "")
    return m.group(1) if m else None


def citation(d, group: str | None = None) -> str:
    """'<publisher> (<year>): <title>. <url>. Abgerufen <date>. Fundus dataset:<id> v<n>.'"""
    who = group or d["publisher"] or "o. A."
    when = (d["retrieved_at"] or d["created_at"] or "")[:10]
    title = (d["source_title"] or d["title"] or "").strip().rstrip(".")
    parts = [f"{who} ({_year(d['published']) or 'o. J.'}): {title}."]
    if (d["url"] or "").strip():
        parts.append(f"{d['url'].strip().rstrip('.')}.")
    if when:
        parts.append(f"Abgerufen {when}.")
    parts.append(f"Fundus dataset:{d['id']} v{d['current_version']}.")
    return " ".join(parts)


def citation_bibtex(d, group: str | None = None) -> str:
    esc = lambda s: str(s).replace("{", "\\{").replace("}", "\\}")
    when = (d["retrieved_at"] or d["created_at"] or "")[:10]
    fields = [("author", "{" + esc(group or d["publisher"] or "o. A.") + "}"), ("title", esc(d["source_title"] or d["title"])),
              ("year", _year(d["published"])), ("howpublished", f"\\url{{{d['url']}}}" if d["url"] else None),
              ("urldate", when or None), ("note", f"Fundus dataset:{d['id']} v{d['current_version']}")]
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
    return f"@misc{{fundus_dataset_{d['id']},\n{body}\n}}"


# ------------------------------------------------------------------ lifecycle
def check_lifecycle(store: Store, d, changes: dict) -> dict:
    """Validate and complete lifecycle changes (lifecycle, superseded_by, lifecycle_note); returns the column changes."""
    from fundus import datasets as ds
    out = {}
    life = changes.get("lifecycle", d["lifecycle"] or "active")
    if life not in LIFECYCLES:
        raise ValueError(f"lifecycle: {' | '.join(LIFECYCLES)}")
    succ = d["superseded_by"]
    if "superseded_by" in changes:
        v = changes["superseded_by"]
        if v in (None, ""):
            succ = None
        else:
            try:
                s = ds._row(store, v)
            except LookupError:
                raise ValueError(f"superseded_by: dataset {v} not found") from None
            if s["id"] == d["id"]:
                raise ValueError("superseded_by: a dataset cannot replace itself")
            succ = s["id"]
            if "lifecycle" not in changes:
                life = "superseded"
            elif life != "superseded":
                raise ValueError("superseded_by only goes with lifecycle 'superseded'")
    if life == "superseded" and not succ:
        raise ValueError("lifecycle 'superseded' needs superseded_by (the dataset that replaces it)")
    if life != "superseded":
        succ = None
    out["lifecycle"], out["superseded_by"] = life, succ
    if "lifecycle_note" in changes:
        out["lifecycle_note"] = (str(changes["lifecycle_note"]).strip() or None) if changes["lifecycle_note"] is not None else None
    if life != (d["lifecycle"] or "active") or succ != d["superseded_by"]:
        out["lifecycle_at"] = _now()
    return out


# ------------------------------------------------------------------ reference datasets
def scan(path: Path) -> dict:
    """bytes, files, newest_mtime and the manifest of a folder or file (symlinks not followed, hidden files skipped)."""
    entries, total, newest = [], 0, 0.0
    if path.is_file():
        st = path.stat()
        entries.append((path.name, st.st_size, st.st_mtime))
    else:
        for root, dirs, fs in os.walk(path):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(fs):
                if f.startswith("."):
                    continue
                p = Path(root) / f
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not p.is_file() or p.is_symlink():
                    continue
                entries.append((str(p.relative_to(path)), st.st_size, st.st_mtime))
    for _, b, mt in entries:
        total += b
        newest = max(newest, mt)
    man = [{"name": n, "bytes": b, "mtime": datetime.fromtimestamp(mt).isoformat(timespec="seconds")} for n, b, mt in entries[:MANIFEST_MAX]]
    if len(entries) > MANIFEST_MAX:
        man.append({"name": f"… und {len(entries) - MANIFEST_MAX} weitere", "more": len(entries) - MANIFEST_MAX})
    return {"bytes": total, "files": len(entries),
            "newest_mtime": datetime.fromtimestamp(newest).isoformat(timespec="seconds") if entries else None, "manifest": man}


def add_reference(store: Store, path, title: str | None = None, description: str | None = None, source_title: str | None = None,
                  publisher: str | None = None, url_: str | None = None, license: str | None = None, published: str | None = None,
                  project: str | None = None, visibility: str = "public", frequency: str | None = None,
                  next_expected: str | None = None, update_hint: str | None = None, note: str | None = None,
                  retrieved_at: str | None = None, script: str | None = None, keywords: list[str] | None = None,
                  actor: str = "cli") -> dict:
    """Register a folder or file as a dataset WITHOUT copying, profiling or converting it (holding 'reference'). The local
    model later describes it from README/Markdown files and the file listing only."""
    from fundus import datasets as ds
    p = Path(str(path)).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"not found: {p}")
    p = p.resolve()
    if visibility not in ("public", "internal"):
        raise ValueError("visibility: public | internal")
    if frequency and frequency not in ds.FREQUENCIES:
        raise ValueError(f"frequency: {' | '.join(ds.FREQUENCIES)}")
    if next_expected and not ds.ISO_MONTH.match(next_expected):
        raise ValueError("next_expected: ISO month, e.g. 2027-09")
    if retrieved_at and not ISO_DAY.match(retrieved_at):
        raise ValueError("retrieved_at: ISO date, e.g. 2026-11-03")
    dup = store.db.execute("SELECT l.dataset_id FROM dataset_location l JOIN dataset d ON d.id = l.dataset_id "
                           "WHERE l.path = ? AND d.removed_at IS NULL", (str(p),)).fetchone()
    if dup:
        raise ValueError(f"{p} is already registered as dataset:{dup[0]}")
    s = scan(p)
    now = _now()
    t = title or p.name.replace("_", " ").replace("-", " ")
    with store.batch(actor):
        did = store.db.execute(
            "INSERT INTO dataset(slug, title, description, source_title, publisher, url, license, published, project, visibility, "
            "keywords, note, current_version, status, created_at, updated_at, created_by, holding, frequency, next_expected, "
            "update_hint, retrieved_at, script) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'new',?,?,?,'reference',?,?,?,?,?)",
            (ds._unique_slug(store, t), t, description, source_title, publisher, url_, license, published,
             _slug(project) if project else None, visibility, json.dumps(keywords or [], ensure_ascii=False), note, now, now, actor,
             frequency, next_expected, update_hint, retrieved_at, script)).lastrowid
        store.db.execute("INSERT INTO dataset_version(dataset_id, n, note, created_at, created_by) VALUES (?,1,?,?,?)",
                         (did, note, now, actor))
        store.db.execute("INSERT INTO dataset_location(dataset_id, version, path, bytes, files, newest_mtime, checked_at, status, "
                         "manifest) VALUES (?,1,?,?,?,?,?,'ok',?)",
                         (did, str(p), s["bytes"], s["files"], s["newest_mtime"], now, json.dumps(s["manifest"], ensure_ascii=False)))
        store.log("dataset", did, "insert", {"version": 1, "holding": "reference", "path": str(p), "files": s["files"], "bytes": s["bytes"]})
        slug = store.db.execute("SELECT slug FROM dataset WHERE id = ?", (did,)).fetchone()[0]
    return {"dataset": did, "ref": f"dataset:{did}", "slug": slug, "version": 1, "holding": "reference", "path": str(p),
            "files": s["files"], "bytes": s["bytes"]}


def location(store: Store, did: int, manifest: bool = True) -> dict | None:
    r = store.db.execute("SELECT * FROM dataset_location WHERE dataset_id = ? ORDER BY version DESC LIMIT 1", (did,)).fetchone()
    if not r:
        return None
    out = {k: r[k] for k in ("path", "bytes", "files", "newest_mtime", "checked_at", "status")}
    if manifest:
        try:
            out["manifest"] = json.loads(r["manifest"] or "[]")
        except ValueError:
            out["manifest"] = []
    return out


def refresh_locations(store: Store, actor: str = "cli") -> dict:
    """Re-scan every reference dataset's location: bytes, files, newest file, status ok | missing | changed."""
    res = {"checked": 0, "ok": 0, "changed": [], "missing": []}
    rows = store.db.execute("SELECT l.* FROM dataset_location l JOIN dataset d ON d.id = l.dataset_id "
                            "WHERE d.removed_at IS NULL").fetchall()
    for r in rows:
        p, now = Path(r["path"]), _now()
        res["checked"] += 1
        if not p.exists():
            status, s = "missing", None
        else:
            s = scan(p)
            status = "changed" if (s["bytes"], s["files"], s["newest_mtime"]) != (r["bytes"], r["files"], r["newest_mtime"]) else "ok"
        with store.batch(actor):
            if s is None:
                store.db.execute("UPDATE dataset_location SET status = ?, checked_at = ? WHERE dataset_id = ? AND version = ?",
                                 (status, now, r["dataset_id"], r["version"]))
            else:
                store.db.execute("UPDATE dataset_location SET bytes = ?, files = ?, newest_mtime = ?, manifest = ?, status = ?, "
                                 "checked_at = ? WHERE dataset_id = ? AND version = ?",
                                 (s["bytes"], s["files"], s["newest_mtime"], json.dumps(s["manifest"], ensure_ascii=False), status,
                                  now, r["dataset_id"], r["version"]))
            if status != "ok" and status != r["status"] or status == "changed":
                store.log("dataset", r["dataset_id"], "location", {"status": status, "path": r["path"],
                                                                    **({"bytes": s["bytes"], "files": s["files"]} if s else {})})
        if status == "ok":
            res["ok"] += 1
        else:
            res[status].append(f"dataset:{r['dataset_id']}")
    return res


DOC_NAMES = re.compile(r"(^readme)|(\.(md|markdown)$)", re.I)


def reference_inputs(store: Store, d, max_docs: int = 5, max_chars: int = 8000) -> tuple[str, str]:
    """(file listing, README/Markdown texts) for describing a reference dataset – never data contents."""
    loc = location(store, d["id"]) or {}
    man = loc.get("manifest") or []
    listing = "\n".join(f"- {e['name']}" + (f" ({e['bytes']} bytes)" if "bytes" in e else "") for e in man[:300])
    if len(man) > 300:
        listing += f"\n- … {loc.get('files', len(man)) - 300} more files"
    texts, root = [], Path(loc.get("path") or "")
    cands = [e["name"] for e in man if "bytes" in e and DOC_NAMES.search(Path(e["name"]).name) and e["name"].count("/") <= 2]
    if root.is_file():
        cands = []
    for name in sorted(cands, key=lambda n: (n.count("/"), not Path(n).name.lower().startswith("readme"), n))[:max_docs]:
        try:
            with open(root / name, "r", encoding="utf-8", errors="replace") as f:
                texts.append(f"--- {name}\n{f.read(max_chars)}")
        except OSError:
            continue
    return listing or "(no files)", "\n\n".join(texts) or "(no README or Markdown file)"


# ------------------------------------------------------------------ fixity and orphans
def _parquet_ok(p: Path) -> bool:
    if not p.is_file():
        return False
    try:
        import duckdb
        con = duckdb.connect()
        try:
            con.execute("SELECT count(*) FROM parquet_metadata(?)", [str(p)]).fetchone()
        finally:
            con.close()
        return True
    except Exception:
        return False


def fixity(store: Store, budget_gb: float = 20, all: bool = False, actor: str = "cli") -> dict:
    """Re-hash stored originals (sha256 = the stored name) and open the Parquet copies, oldest-verified first, until
    `budget_gb` of originals are hashed (all=True: everything). Results go to dataset_file.verified_at/verified_ok,
    failures to the changelog. Also reports orphan files (see orphans()) – reporting only, nothing is deleted."""
    from fundus import datasets as ds
    rows = store.db.execute("SELECT id, dataset_id, version, name, sha, ext, parquet_sha, verified_ok FROM dataset_file "
                            "ORDER BY verified_at IS NOT NULL, verified_at, id").fetchall()
    budget, used = budget_gb * 1024 ** 3, 0
    hashed: dict[str, tuple[bool, str | None]] = {}
    pq: dict[str, bool] = {}
    res = {"checked": 0, "ok": 0, "failed": [], "bytes_hashed": 0, "left": 0}
    for i, r in enumerate(rows):
        if r["sha"] not in hashed:
            if not all and hashed and used >= budget:     # at least one file per run
                res["left"] = len(rows) - i
                break
            p = ds.file_path(store, r["sha"], r["ext"])
            if not p.is_file():
                hashed[r["sha"]] = (False, "original missing")
            else:
                used += p.stat().st_size
                hashed[r["sha"]] = (True, None) if ds._sha_file(p) == r["sha"] else (False, "checksum differs")
        ok, why = hashed[r["sha"]]
        if ok and r["parquet_sha"]:
            if r["parquet_sha"] not in pq:
                pq[r["parquet_sha"]] = _parquet_ok(ds.file_path(store, r["parquet_sha"], "parquet"))
            if not pq[r["parquet_sha"]]:
                ok, why = False, "Parquet copy missing or unreadable"
        prev = store.actor
        store.actor = actor
        try:
            store.db.execute("UPDATE dataset_file SET verified_at = ?, verified_ok = ? WHERE id = ?", (_now(), int(ok), r["id"]))
            if not ok:
                store.log("dataset", r["dataset_id"], "fixity", {"file": r["name"], "file_id": r["id"], "version": r["version"],
                                                                 "error": why})
            store.db.commit()
        finally:
            store.actor = prev
        res["checked"] += 1
        if ok:
            res["ok"] += 1
        else:
            res["failed"].append({"dataset": f"dataset:{r['dataset_id']}", "file_id": r["id"], "file": r["name"], "error": why})
    res["bytes_hashed"] = used
    res["orphans"] = orphans(store)
    return res


def orphans(store: Store, record: bool = True) -> dict:
    """Files under the dataset folder that no dataset_file row references (originals, Parquet copies, *.tmp* leftovers)
    and stale folders in incoming/ – only those untouched for 24 hours (an import may be running). Reporting only; the
    result is kept in meta 'dataset_orphans' for the catalog."""
    from fundus import datasets as ds
    root = ds.files_dir(store)
    known = set()
    for sha, ext, psha in store.db.execute("SELECT sha, ext, parquet_sha FROM dataset_file"):
        known.add(f"{sha}.{ext}")
        if psha:
            known.add(f"{psha}.parquet")
    cut = time.time() - ORPHAN_AGE_H * 3600
    found = []
    if root.is_dir():
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and re.fullmatch(r"[0-9a-f]{2}", sub.name):
                for f in sub.iterdir():
                    try:
                        st = f.lstat()
                    except OSError:
                        continue
                    if f.name not in known and st.st_mtime < cut:
                        found.append({"path": str(f), "bytes": st.st_size if f.is_file() else 0,
                                      "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                                      "kind": "tmp" if ".tmp" in f.name else "file"})
        inc = root / "incoming"
        if inc.is_dir():
            for sub in sorted(inc.iterdir()):
                size, newest = 0, sub.lstat().st_mtime
                for r_, _, fs in os.walk(sub) if sub.is_dir() else [(str(inc), [], [sub.name])]:
                    for f in fs:
                        try:
                            st = (Path(r_) / f).lstat()
                        except OSError:
                            continue
                        size += st.st_size
                        newest = max(newest, st.st_mtime)
                if newest < cut:
                    found.append({"path": str(sub), "bytes": size, "mtime": datetime.fromtimestamp(newest).isoformat(timespec="seconds"),
                                  "kind": "incoming"})
    out = {"files": len(found), "bytes": sum(x["bytes"] for x in found), "checked_at": _now(), "list": found[:500]}
    if record:
        try:
            store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('dataset_orphans', ?)",
                             (json.dumps({**{k: out[k] for k in ("files", "bytes", "checked_at")}, "list": found[:200]}),))
            store.db.commit()
        except Exception:
            pass
    return out


def purge_orphans(store: Store, actor: str = "cli") -> dict:
    """Delete the orphans orphans() reports (only on Falk's explicit `fundus datasets orphans --purge`)."""
    import shutil
    rep = orphans(store, record=False)
    n, b = 0, 0
    for x in rep["list"]:
        p = Path(x["path"])
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
            n, b = n + 1, b + x["bytes"]
        except OSError:
            continue
    prev = store.actor
    store.actor = actor
    store.log("dataset", "-", "purge_orphans", {"deleted": n, "bytes": b})
    store.db.commit()
    store.actor = prev
    orphans(store)
    return {"deleted": n, "bytes": b, "more": max(0, rep["files"] - len(rep["list"]))}


# ------------------------------------------------------------------ lineage and version changes
def lineage(store: Store, did: int, levels: int = 2) -> dict:
    """Nodes (ref, title, depth -levels..levels, visibility) and edges (input → derived) around a dataset."""
    q = store.db.execute
    up_sql = ("SELECT i.input_id, i.dataset_id FROM dataset_input i JOIN dataset d ON d.id = i.dataset_id "
              "AND i.version = d.current_version WHERE i.dataset_id = ?")
    down_sql = ("SELECT i.input_id, i.dataset_id FROM dataset_input i JOIN dataset d ON d.id = i.dataset_id "
                "AND i.version = d.current_version WHERE i.input_id = ? AND d.removed_at IS NULL")
    depth, edges = {did: 0}, set()
    for sql, sign, pick in ((up_sql, -1, 0), (down_sql, 1, 1)):
        frontier = [did]
        for lvl in range(1, levels + 1):
            nxt = []
            for cur in frontier:
                for a, b in q(sql, (cur,)):
                    edges.add((a, b))
                    other = (a, b)[pick]
                    if other not in depth:
                        depth[other] = sign * lvl
                        nxt.append(other)
            frontier = nxt
    nodes = []
    for i, dep in sorted(depth.items(), key=lambda x: (x[1], x[0])):
        r = q("SELECT title, visibility, removed_at FROM dataset WHERE id = ?", (i,)).fetchone()
        if r:
            nodes.append({"ref": f"dataset:{i}", "id": i, "title": r["title"], "depth": dep, "visibility": r["visibility"],
                          **({"removed": True} if r["removed_at"] else {})})
    have = {n["id"] for n in nodes}
    return {"nodes": nodes, "edges": [{"from": f"dataset:{a}", "to": f"dataset:{b}"} for a, b in sorted(edges) if a in have and b in have]}


def version_changes(store: Store, d) -> dict[int, dict]:
    """{version: {tables_added, tables_removed, columns_added {table: [..]}, columns_removed {table: [..]}, rows_delta}}
    against the previous version (from the stored profiles; with table_scope 'merged' the effective tables)."""
    merged = d["table_scope"] == "merged"
    if store.db.execute("SELECT count(*) FROM dataset_version WHERE dataset_id = ?", (d["id"],)).fetchone()[0] < 2:
        return {}
    tabs: dict[int, dict[str, dict]] = {}
    rows = store.db.execute("SELECT f.id, f.version, coalesce(f.table_name, f.name) t, f.rows, j.value ->> 'name' "
                            "FROM dataset_file f LEFT JOIN json_each(CASE WHEN json_valid(f.profile) THEN f.profile END, '$.columns') j "
                            "WHERE f.dataset_id = ? AND f.role = 'table' ORDER BY f.version, f.id", (d["id"],)).fetchall()
    for fid, v, t, n, col in rows:
        e = tabs.setdefault(v, {}).setdefault(t, {"rows": n or 0, "cols": [], "fid": fid})
        if e["fid"] == fid and col is not None:
            e["cols"].append(col)
    versions = [r[0] for r in store.db.execute("SELECT n FROM dataset_version WHERE dataset_id = ? ORDER BY n", (d["id"],))]
    out, prev, eff = {}, None, {}
    for v in versions:
        cur = {**eff, **tabs.get(v, {})} if merged else tabs.get(v, {})
        eff = cur
        if prev is not None:
            out[v] = {"tables_added": sorted(set(cur) - set(prev)), "tables_removed": sorted(set(prev) - set(cur)),
                      "columns_added": {t: [c for c in cur[t]["cols"] if c not in set(prev[t]["cols"])] for t in cur if t in prev},
                      "columns_removed": {t: [c for c in prev[t]["cols"] if c not in set(cur[t]["cols"])] for t in cur if t in prev},
                      "rows_delta": sum(x["rows"] for x in cur.values()) - sum(x["rows"] for x in prev.values())}
            for k in ("columns_added", "columns_removed"):
                out[v][k] = {t: c for t, c in out[v][k].items() if c}
        prev = cur
    return out


# ------------------------------------------------------------------ catalog
FILE_COLS = ("f.id, f.dataset_id, f.version, f.role, f.name, f.table_name, f.sha, f.ext, f.bytes, f.rows, f.parquet_sha, "
             "f.verified_at, f.verified_ok, coalesce(f.columns_n, 0) AS cols")
# FILE_COLS never touches the profile: the covering index ix_dataset_file_catalog (store.py) then serves the catalog
# without reading the profile blobs (145 → 37 ms for 27,000 files)


# positions in a FILE_COLS row (plain tuples: sqlite3.Row lookups by name were a third of the catalog time)
F_ID, F_DID, F_VER, F_ROLE, F_NAME, F_TABLE, F_SHA, F_EXT, F_BYTES, F_ROWS, F_PSHA, F_VAT, F_VOK, F_COLS = range(14)


def aggregate(d, files) -> dict:
    """Counts of a dataset from its file rows (FILE_COLS tuples; any versions): tables, rows, columns of the main
    (largest) table, columns_max, columns_total, bytes (originals of the current tables/documents, a workbook once), formats."""
    cur = d["current_version"]
    if d["table_scope"] == "merged":
        seen, tabs = set(), []
        for f in sorted((f for f in files if f[F_ROLE] == "table" and f[F_VER] <= cur), key=lambda f: (-f[F_VER], f[F_ID])):
            k = f[F_TABLE] or f[F_NAME]
            if k not in seen:
                seen.add(k)
                tabs.append(f)
    else:
        tabs = [f for f in files if f[F_VER] == cur and f[F_ROLE] == "table"]
    docs = [f for f in files if f[F_VER] == cur and f[F_ROLE] == "document"]
    main = min(tabs, key=lambda f: (-(f[F_ROWS] or 0), f[F_ID])) if tabs else None
    cols = [f[F_COLS] or 0 for f in tabs]
    both = tabs + docs
    return {"tables": len(tabs), "rows": sum(f[F_ROWS] or 0 for f in tabs), "columns": (main[F_COLS] or 0) if main else 0,
            "columns_max": max(cols, default=0), "columns_total": sum(cols),
            "bytes": sum({f[F_SHA]: f[F_BYTES] or 0 for f in both}.values()),
            "formats": sorted({f[F_EXT] for f in both})}


def _tuples(store: Store, sql: str, args=()) -> list[tuple]:
    cur = store.db.cursor()
    cur.row_factory = None
    return cur.execute(sql, args).fetchall()


def file_rows(store: Store, did: int) -> list:
    return _tuples(store, f"SELECT {FILE_COLS} FROM dataset_file f WHERE f.dataset_id = ?", (did,))


_size_cache: dict[str, int] = {}


def _disk_size(root: str, sha: str, ext: str) -> int:
    """Size of a content-addressed file under root (cached: such files never change; missing files are not cached).
    Plain strings and os.stat: pathlib cost more than half of the catalog time for 50,000 files."""
    k = f"{root}/{sha[:2]}/{sha}.{ext}"
    n = _size_cache.get(k)
    if n is not None:
        return n
    try:
        n = os.stat(k).st_size
    except OSError:
        return 0
    _size_cache[k] = n
    return n


def catalog(store: Store, removed: bool = False) -> dict:
    """Every dataset (internal included – the portal is admin) with catalog fields, facets and stock figures."""
    from fundus import datasets as ds
    q = store.db.execute
    rows = q(f"SELECT * FROM dataset WHERE removed_at IS {'NOT ' if removed else ''}NULL ORDER BY updated_at DESC").fetchall()
    files: dict[int, list] = {}
    for f in _tuples(store, f"SELECT {FILE_COLS} FROM dataset_file f JOIN dataset d ON d.id = f.dataset_id "
                            f"WHERE d.removed_at IS {'NOT ' if removed else ''}NULL"):
        files.setdefault(f[F_DID], []).append(f)
    inputs_n: dict[int, int] = {}
    stale: dict[int, list[str]] = {}
    for did, inp, iv, icur in q("SELECT i.dataset_id, i.input_id, i.input_version, x.current_version FROM dataset_input i "
                                "JOIN dataset d ON d.id = i.dataset_id AND i.version = d.current_version "
                                "JOIN dataset x ON x.id = i.input_id"):
        inputs_n[did] = inputs_n.get(did, 0) + 1
        if icur > iv:
            stale.setdefault(did, []).append(f"dataset:{inp}")
    used_by_n = dict(q("SELECT i.input_id, count(*) FROM dataset_input i JOIN dataset d ON d.id = i.dataset_id "
                       "AND i.version = d.current_version WHERE d.removed_at IS NULL GROUP BY i.input_id").fetchall())
    slides_n = {int(k): n for k, n in q("SELECT l.target_id, count(DISTINCT e.id) FROM exhibit_link l JOIN exhibit e ON e.id = l.exhibit_id "
                                        "WHERE l.target_kind = 'dataset' AND e.project != '_trash' GROUP BY l.target_id") if str(k).isdigit()}
    uses = usage_counts(store)
    locs = {r[0]: r for r in q("SELECT dataset_id, path, bytes, files, status, checked_at FROM dataset_location ORDER BY version")}
    groups = publisher_groups(store, [d["publisher"] for d in rows])
    today = date.today()
    root = str(ds.files_dir(store))

    out, disk_seen = [], {}
    st = {"datasets": len(rows), "tables": 0, "rows": 0, "bytes_original": 0, "bytes_parquet": 0, "bytes_reference": 0,
          "internal": 0, "update_due": 0, "dormant": 0, "superseded": 0, "withdrawn": 0, "references": 0, "uses_30d": 0,
          "texts": 0}
    fix = {"ok": 0, "failed": 0, "unchecked": 0, "oldest_verified": None, "failures": []}
    for d in rows:
        fl = files.get(d["id"], [])
        agg = aggregate(d, fl)
        disk, mine = 0, set()
        for f in fl:
            for sha, ext, key in ((f[F_SHA], f[F_EXT], "bytes_original"), (f[F_PSHA], "parquet", "bytes_parquet")):
                if not sha or (sha, ext) in mine:
                    continue
                mine.add((sha, ext))
                n = _disk_size(root, sha, ext)
                disk += n
                if (sha, ext) not in disk_seen:
                    disk_seen[(sha, ext)] = n
                    st[key] += n
            vok, vat = f[F_VOK], f[F_VAT]
            if vok is None:
                fix["unchecked"] += 1
            elif vok:
                fix["ok"] += 1
            else:
                fix["failed"] += 1
                if len(fix["failures"]) < 200:
                    fix["failures"].append({"dataset": f"dataset:{d['id']}", "title": d["title"], "file_id": f[F_ID],
                                            "file": f[F_NAME], "version": f[F_VER], "verified_at": vat})
            if vat and (fix["oldest_verified"] is None or vat < fix["oldest_verified"]):
                fix["oldest_verified"] = vat
        holding = d["holding"] or "stored"
        loc = locs.get(d["id"])
        if holding == "reference":
            agg["bytes"] = loc["bytes"] if loc else 0
            st["references"] += 1
            st["bytes_reference"] += agg["bytes"] or 0
        u = uses.get(d["id"], {})
        b = ds._brief(store, d, agg={**agg, "stale": stale.get(d["id"], []), "publisher_group": groups.get(d["publisher"])})
        pf, pt = period_years(d["period"])
        dormant = is_dormant(d, u.get("last_used"), today)
        b.update({"period_from": pf, "period_to": pt, "size_class": size_class(agg["bytes"]), "bytes_disk": disk,
                  "uses_30d": u.get("uses_30d", 0), "uses_total": u.get("uses_total", 0), "seen_30d": u.get("seen_30d", 0),
                  "last_used": u.get("last_used"), "lifecycle": d["lifecycle"] or "active", "superseded_by": d["superseded_by"],
                  "holding": holding, "dormant": dormant, "publisher_group": groups.get(d["publisher"]),
                  "inputs_n": inputs_n.get(d["id"], 0), "used_by_n": used_by_n.get(d["id"], 0), "slides_n": slides_n.get(d["id"], 0),
                  "created_at": d["created_at"]})
        if loc:
            b["location"] = {"path": loc["path"], "bytes": loc["bytes"], "files": loc["files"], "status": loc["status"],
                             "checked_at": loc["checked_at"]}
        out.append(b)
        st["tables"] += agg["tables"]
        st["rows"] += agg["rows"]
        st["internal"] += d["visibility"] == "internal"
        st["update_due"] += bool(b.get("update_due"))
        st["dormant"] += dormant
        st["superseded"] += b["lifecycle"] == "superseded"
        st["withdrawn"] += b["lifecycle"] == "withdrawn"
        st["uses_30d"] += b["uses_30d"]
        st["texts"] += bool(d["act_type"])
    st["fixity"] = fix
    try:
        orph = json.loads((q("SELECT value FROM meta WHERE key = 'dataset_orphans'").fetchone() or ["{}"])[0])
    except ValueError:
        orph = {}
    st["orphans"] = {"files": orph.get("files"), "bytes": orph.get("bytes"), "checked_at": orph.get("checked_at"),
                     "list": orph.get("list") or []}
    return {"stats": st, "facets": facets(store, out), "datasets": out}


def facets(store: Store, items: list[dict]) -> dict:
    """{facet: [{value, label, count}]} over the catalog items, most frequent first."""
    vocab = {(f, v): label for f, v, label in store.db.execute("SELECT facet, value, label FROM vocab")}
    projects = dict(store.db.execute("SELECT slug, title FROM project").fetchall())
    counts: dict[str, dict] = {}

    def add(facet, value):
        counts.setdefault(facet, {})
        counts[facet][value] = counts[facet].get(value, 0) + 1
    for x in items:
        add("project", x.get("project"))
        for f in ("industry", "topic", "region"):
            for v in dict.fromkeys((x.get("tags") or {}).get(f) or []):
                add(f, v)
        add("publisher_group", x.get("publisher_group"))
        add("frequency", x.get("frequency"))
        add("visibility", x.get("visibility"))
        add("lifecycle", x.get("lifecycle"))
        add("holding", x.get("holding"))
        add("kind", x.get("kind") or "data")
        if x.get("kind") == "text":            # texts with versions: jurisdiction and act type (only texts carry them)
            add("jurisdiction", x.get("jurisdiction"))
            add("act_type", x.get("act_type"))
        add("size", x.get("size_class"))
        add("status", x.get("status"))

    def label(facet, v):
        if facet == "project":
            return projects.get(v) or v or "ohne Projekt"
        if facet in ("industry", "topic", "region"):
            return vocab.get((facet, v)) or v
        if facet in LABELS:
            return LABELS[facet].get(v, v if v is not None else "–")
        return v if v is not None else "ohne Angabe"
    order = ("project", "industry", "topic", "region", "publisher_group", "frequency", "visibility", "lifecycle", "holding", "kind",
             "jurisdiction", "act_type", "size", "status")
    out = {}
    for f in order:
        vals = counts.get(f, {})
        if f == "size":
            keys = [k for k, _ in SIZE_CLASSES if k in vals]
        else:
            keys = sorted(vals, key=lambda v: (-vals[v], str(label(f, v)).casefold()))
        out[f] = [{"value": k, "label": label(f, k), "count": vals[k]} for k in keys]
    return out


# ------------------------------------------------------------------ detail
def _mini(r) -> dict:
    return {"id": r["id"], "ref": f"dataset:{r['id']}", "slug": r["slug"], "title": r["title"], "version": r["current_version"],
            "lifecycle": r["lifecycle"] or "active", "visibility": r["visibility"],
            **({"removed_at": r["removed_at"]} if r["removed_at"] else {})}


def detail_extras(store: Store, d, out: dict) -> dict:
    """What datasets.get adds for the detail page: uses, citation, lifecycle (+ successor/predecessors), holding and
    location, provenance, lineage and per-version changes."""
    q = store.db.execute
    group = out.get("publisher_group")
    out["uses"] = usage_detail(store, d["id"])
    out["dormant"] = is_dormant(d, out["uses"]["last"])
    out["citation"], out["citation_bibtex"] = citation(d, group), citation_bibtex(d, group)
    out["lifecycle"] = d["lifecycle"] or "active"
    for k in ("superseded_by", "lifecycle_note", "lifecycle_at", "retrieved_at", "script"):
        out[k] = d[k]
    out["successor"] = None
    if d["superseded_by"]:
        s = q("SELECT * FROM dataset WHERE id = ?", (d["superseded_by"],)).fetchone()
        out["successor"] = _mini(s) if s else None
    out["predecessors"] = [_mini(r) for r in q("SELECT * FROM dataset WHERE superseded_by = ? ORDER BY id", (d["id"],))]
    out["holding"] = d["holding"] or "stored"
    out["location"] = location(store, d["id"]) if out["holding"] == "reference" else None
    out["lineage"] = lineage(store, d["id"])
    ch = version_changes(store, d)
    for v in out.get("versions") or []:
        if v["n"] in ch:
            v["changes"] = ch[v["n"]]
    return out
