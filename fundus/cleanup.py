"""Data hygiene: find duplicates, spelling variants, contradictions and orphans; fix the safe ones; undo any run.

analyze(store) looks at the whole archive (read-only apart from its own tables) and files findings in cleanup_candidate:

    class        what                                                              action
    variant      geo not in the code notation (fundus.geo.canonical_geo),          automatic (apply)
                 units not in the unit notation, stray whitespace in names
    safe         certain duplicates: identical data points of one source (also    automatic (apply)
                 Store.dedupe's rules), documents with the same DOI or the same
                 URL and title, publishers with the same name, identical quotes/
                 practices/facts/forecasts/studies of one source
    orphan       publishers that nothing refers to                                automatic (apply)
    probable     likely duplicates: documents with the same title and publisher   Falk decides (decide)
                 but another URL, data points of one source with the same value
                 and similar wording, series with the same definition, series
                 without values
    contradiction  one source, same metric, period, place and unit, different     Falk decides: mark disputed
                 values                                                            or ignore

apply(store) runs the automatic classes after a backup; decide(store, ids, action) carries out Falk's decisions; every
change is journalled (before-images of the touched rows) so undo(store, run) restores the state before that run.
Merging keeps the better record (verified before unverified, the one in a series, the older one), fills its gaps from
the other, moves tags, origins, slide links, entity links and checks, and leaves a redirect (item_redirect), so a slide
that cites 'observation:4871' still finds the data point. Internal and public records are never merged. A group Falk
marked 'no duplicate' is never proposed again. The quality score (share of records without an open finding) is kept
per analysis run for the statistics.
"""
from __future__ import annotations

import json
import re
import unicodedata
import sqlite3
from datetime import datetime
from pathlib import Path

from fundus.geo import canonical_geo
from fundus.store import ITEM_KINDS, Store, ident_key, norm_url
from fundus.units import normalise_unit

CLASSES = ("variant", "safe", "orphan", "probable", "contradiction")
AUTO = ("variant", "safe", "orphan")
CORE = ("observation", "forecast", "study", "quote", "practice", "fact", "document", "publisher", "series")
GEO_COLS = (("observation", "geo"), ("forecast", "geo"), ("fact", "geo"), ("series", "geo"))
UNIT_COLS = (("observation", "unit"), ("forecast", "unit"), ("series", "unit"))
SPACE_COLS = (("document", "title"), ("publisher", "name"), ("series", "name"), ("observation", "metric"))
TEXT_KEY = {"quote": ("text", "speaker"), "practice": ("organisation", "what"), "fact": ("kind", "title"),
            "forecast": ("statement",), "study": ("finding",)}
RANK = {"verified": 0, "unverified": 1, "disputed": 2, "retracted": 3}
LEGAL = r"\b(inc|incorporated|ltd|limited|llc|gmbh|ag|sa|plc|corp|corporation|co|company|group|se|nv|bv)\b\.?"


# ------------------------------------------------------------------ small helpers
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ws(t: str | None) -> str | None:
    return re.sub(r"\s+", " ", t).strip() if isinstance(t, str) else t


def _txt(t) -> str:
    return re.sub(r"[^\w]+", " ", str(t or "").casefold()).strip()


def _cat(t) -> str:
    """Category key: case and spaces folded, symbols kept – '≥ USD 1bn' ≠ '< USD 1bn', 'C#' ≠ 'C++' (1 Nov 2026)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(t or "")).casefold()).strip()


def _toks(t) -> set[str]:
    return set(_txt(t).split()) - {"of", "the", "a", "an", "in", "to", "and", "for", "by", "with", "on", "at", "who", "that"}


def _jac(a, b) -> float:
    x, y = _toks(a), _toks(b)
    return len(x & y) / len(x | y) if x | y else 1.0


def _pubname(n) -> str:
    return re.sub(r"\s+", " ", re.sub(LEGAL, " ", _txt(n))).strip()


def _sig(kind: str, ids) -> str:
    return f"{kind}:" + ",".join(sorted(str(i) for i in ids))


# ------------------------------------------------------------------ journal (undo)
def _keycols(db, table: str) -> list[str]:
    sql = (db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone() or [""])[0] or ""
    if "WITHOUT ROWID" in sql.upper():
        return [r[1] for r in sorted((r for r in db.execute(f"PRAGMA table_info({table})") if r[5]), key=lambda r: r[5])]
    return ["_rk"]                              # rowid, selected as "rowid AS _rk" (plain "rowid" comes back as "id")


def _sel(keys: list[str], cols: list[str] | None = None) -> str:
    head = ["rowid AS _rk"] if keys == ["_rk"] else list(keys)
    return ", ".join(head + ([c for c in cols if c not in keys] if cols else ["*"]))


def _where_key(keys: list[str]) -> str:
    return " AND ".join(("rowid" if k == "_rk" else k) + " = ?" for k in keys)


class Journal:
    """Before-images of row regions (table + WHERE). restore() puts a region back exactly: rows that appeared are
    deleted, changed ones updated, deleted ones re-inserted. cols=[…] journals only those columns (update-only)."""

    def __init__(self, store: Store, run_id: int):
        self.st, self.run, self.seq = store, run_id, 0

    def snap(self, table: str, where: str, args=(), cols: list[str] | None = None) -> None:
        keys = _keycols(self.st.db, table)
        rows = [dict(r) for r in self.st.db.execute(f"SELECT {_sel(keys, cols)} FROM {table} WHERE {where}", list(args))]
        self.seq += 1
        self.st.db.execute("INSERT INTO cleanup_journal(run_id, seq, tbl, where_sql, args, cols, rows) VALUES (?,?,?,?,?,?,?)",
                           (self.run, self.seq, table, where, json.dumps(list(args)), json.dumps(cols) if cols else None,
                            json.dumps(rows, default=str)))


def _restore(db, table: str, where: str, args: list, cols: list | None, rows: list[dict]) -> None:
    keys = _keycols(db, table)
    key = lambda r: tuple(r[k] for k in keys)
    if cols:                                   # update-only region
        for r in rows:
            db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in cols)} WHERE {_where_key(keys)}",
                       [r[c] for c in cols] + list(key(r)))
        return
    cur = {key(dict(r)): dict(r) for r in db.execute(f"SELECT {_sel(keys)} FROM {table} WHERE {where}", args)}
    want = {key(r): r for r in rows}
    for k in cur.keys() - want.keys():
        db.execute(f"DELETE FROM {table} WHERE {_where_key(keys)}", list(k))
    for k, r in want.items():
        names = [c for c in r if c != "_rk"]
        if k in cur:
            db.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in names)} WHERE {_where_key(keys)}",
                       [r[c] for c in names] + list(k))
        else:
            allc = (["rowid"] if keys == ["_rk"] else []) + names
            vals = ([r["_rk"]] if keys == ["_rk"] else []) + [r[c] for c in names]
            db.execute(f"INSERT INTO {table}({', '.join(allc)}) VALUES ({', '.join('?' * len(allc))})", vals)


# ------------------------------------------------------------------ redirects
def resolve(store: Store, kind: str, iid) -> str:
    """The id a merged record lives on under (Store.follow)."""
    return store.follow(kind, iid)


# ------------------------------------------------------------------ analysis
def _better(rows: list, kind: str) -> list:
    """Order: the record to keep first."""
    def k(r):
        filled = sum(1 for c in r.keys() if r[c] not in (None, ""))
        return (RANK.get(r["status"], 9) if "status" in r.keys() else 0,
                0 if ("series_id" in r.keys() and r["series_id"]) else 1, -filled, str(r["id"]).zfill(12))
    return sorted(rows, key=k)


def _variants(store: Store) -> list[dict]:
    out = []
    for table, col in GEO_COLS:
        groups: dict[tuple, list] = {}
        for iid, g in store.db.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL"):
            c = canonical_geo(g)
            if c != g:
                groups.setdefault((g, c), []).append(iid)
        for (g, c), ids in groups.items():
            out.append({"kind": table, "class": "variant", "signature": f"geo:{table}:{g}->{c}", "items": ids,
                        "reason": f"Region „{g}“ → „{c}“", "detail": {"op": "set", "table": table, "col": col,
                                                                       "from": g, "to": c, "n": len(ids)}})
    for table, col in UNIT_COLS:
        groups = {}
        for iid, u in store.db.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL"):
            n = normalise_unit(u)
            if n != u:
                groups.setdefault((u, n), []).append(iid)
        for (u, n), ids in groups.items():
            out.append({"kind": table, "class": "variant", "signature": f"unit:{table}:{u}->{n}", "items": ids,
                        "reason": f"Einheit „{u}“ → „{n}“", "detail": {"op": "set", "table": table, "col": col,
                                                                       "from": u, "to": n, "n": len(ids)}})
    for table, col in SPACE_COLS:
        ids = [r[0] for r in store.db.execute(f"SELECT id, {col} FROM {table}") if isinstance(r[1], str) and _ws(r[1]) != r[1]]
        if ids:
            out.append({"kind": table, "class": "variant", "signature": f"space:{table}:{col}", "items": ids,
                        "reason": f"Leerzeichen in {table}.{col}", "detail": {"op": "space", "table": table, "col": col,
                                                                             "n": len(ids)}})
    return out


def _observation_groups(store: Store) -> list[dict]:
    out = []
    # Store.dedupe's rules (same source, period, value, unit, geo, category; one wording contains the other) – safe
    for m in store.dedupe(dry_run=True):
        out.append({"kind": "observation", "class": "safe", "items": [m["keep"], m["drop"]], "keep": m["keep"],
                    "reason": "gleicher Datenpunkt derselben Quelle (gleicher Wert, Zeitraum, Region; Formulierung enthalten)",
                    "detail": {"metric": m["metric"], "other": m["other"], "period": m["period"], "value": m["value"]}})
    taken = {i for g in out for i in g["items"] if i != g["keep"]}       # records that disappear in a safe merge
    rows = store.db.execute(
        "SELECT o.*, coalesce(d.url_key, 'doc:' || o.document_id) AS dkey FROM observation o "
        "LEFT JOIN document d ON d.id = o.document_id WHERE o.document_id IS NOT NULL").fetchall()
    exact: dict[tuple, list] = {}
    near: dict[tuple, list] = {}
    contra: dict[tuple, list] = {}
    for r in rows:
        base = (r["dkey"], r["period"] or "", (canonical_geo(r["geo"]) or "").lower(), _cat(r["category"]),
                normalise_unit(r["unit"]) or "", r["visibility"])
        exact.setdefault(base + (_txt(r["metric"]), r["value"], _txt(r["value_text"]), _txt(r["population"]),
                                 r["series_id"] or ""), []).append(r)
        if r["value"] is not None:
            near.setdefault(base + (r["value"],), []).append(r)
            contra.setdefault(base + (_txt(r["metric"]), _txt(r["population"])), []).append(r)
    # values of the same metric from two different figures/tables of one report are often different measures
    # (another base, year or definition): a contradiction only within one locator, or where one has none
    split = {}
    for k, g in contra.items():
        locs = {_txt(r["locator"]) for r in g if r["locator"]}
        if len(locs) > 1:
            for loc in locs:
                split[k + (loc,)] = [r for r in g if _txt(r["locator"]) == loc or not r["locator"]]
        else:
            split[k] = g
    contra = split
    for g in exact.values():
        g = [r for r in g if r["id"] not in taken]
        if len(g) > 1:
            g = _better(g, "observation")
            out.append({"kind": "observation", "class": "safe", "items": [r["id"] for r in g], "keep": g[0]["id"],
                        "reason": "identischer Datenpunkt derselben Quelle", "detail": {"metric": g[0]["metric"],
                                                                                       "period": g[0]["period"],
                                                                                       "value": g[0]["value"]}})
            taken |= {r["id"] for r in g[1:]}
    for g in near.values():
        g = [r for r in g if r["id"] not in taken]
        if len(g) < 2:
            continue
        g = _better(g, "observation")
        keep = g[0]
        sim = [r for r in g[1:] if _jac(keep["metric"], r["metric"]) >= 0.5 and _txt(keep["metric"]) != _txt(r["metric"])
               and not (keep["series_id"] and r["series_id"] and keep["series_id"] != r["series_id"])]
        if sim:
            items = [keep] + sim
            out.append({"kind": "observation", "class": "probable", "items": [r["id"] for r in items], "keep": keep["id"],
                        "reason": "gleicher Wert derselben Quelle, ähnliche Formulierung",
                        "detail": {"metrics": [r["metric"] for r in items], "period": keep["period"], "value": keep["value"],
                                   "unit": keep["unit"]}})
            taken |= {r["id"] for r in sim}
    for g in contra.values():
        vals = {r["value"] for r in g}
        if len(vals) > 1 and not any(r["series_id"] and r["series_id"] != g[0]["series_id"] for r in g):
            out.append({"kind": "observation", "class": "contradiction", "items": [r["id"] for r in g], "keep": None,
                        "reason": "dieselbe Quelle, Kennzahl, Zeitraum und Region – verschiedene Werte",
                        "detail": {"metric": g[0]["metric"], "period": g[0]["period"], "geo": g[0]["geo"],
                                   "values": sorted(vals), "unit": g[0]["unit"]}})
    return out


def _doc_items(store: Store, did: int) -> int:
    return sum(store.db.execute(f"SELECT count(*) FROM {k} WHERE document_id = ?", (did,)).fetchone()[0] for k in ITEM_KINDS)


def _document_groups(store: Store) -> list[dict]:
    out, taken = [], set()
    docs = store.db.execute("SELECT * FROM document").fetchall()

    def add(cls, g, reason, detail=None):
        nonlocal taken
        g = [r for r in g if r["id"] not in taken]
        if len(g) < 2:
            return
        g = sorted(g, key=lambda r: (-_doc_items(store, r["id"]), 0 if r["doi"] else 1, 0 if r["url"] else 1, r["id"]))
        out.append({"kind": "document", "class": cls, "items": [r["id"] for r in g], "keep": g[0]["id"], "reason": reason,
                    "detail": {"titles": [r["title"] for r in g], "urls": [r["url"] for r in g],
                               "publishers": [r["publisher_id"] for r in g], "published": [r["published"] for r in g],
                               **(detail or {})}})
        taken |= {r["id"] for r in g}
    by: dict[tuple, list] = {}
    for r in docs:
        if r["doi"]:
            by.setdefault(("doi", r["doi"].strip().lower()), []).append(r)
    for g in by.values():
        add("safe", g, "gleiche DOI")
    by = {}
    for r in docs:
        if r["url_key"]:
            by.setdefault((r["url_key"], _txt(r["title"])), []).append(r)
    for g in by.values():
        if len({r["publisher_id"] for r in g if r["publisher_id"]}) <= 1:
            add("safe", g, "gleiche URL und gleicher Titel")
    by = {}
    for r in docs:
        if r["publisher_id"] and len(_txt(r["title"])) >= 12:
            by.setdefault((r["publisher_id"], _txt(r["title"])), []).append(r)
    for g in by.values():
        # yearly editions stay apart
        if len({(r["published"] or "")[:4] for r in g if r["published"]}) > 1:
            continue
        if all(r["url_key"] for r in g) and len({r["url_key"] for r in g}) > 1:
            continue                       # all with their own URL: separate pages ("model page" for gpt-5.1 and 5.2)
        add("probable", g, "gleicher Titel und Herausgeber, andere oder fehlende URL")
    return out


def _publisher_groups(store: Store) -> list[dict]:
    out = []
    pubs = store.db.execute("SELECT * FROM publisher").fetchall()
    exact: dict[str, list] = {}
    loose: dict[str, list] = {}
    for p in pubs:
        exact.setdefault(_txt(p["name"]), []).append(p)
        if _pubname(p["name"]):
            loose.setdefault(_pubname(p["name"]), []).append(p)
    used = lambda pid: store.db.execute("SELECT (SELECT count(*) FROM document WHERE publisher_id = ?) + "
                                        "(SELECT count(*) FROM series WHERE publisher_id = ?)", (pid, pid)).fetchone()[0]
    taken = set()
    for cls, groups, reason in (("safe", exact, "gleicher Name"), ("probable", loose, "gleicher Name ohne Rechtsform")):
        for g in groups.values():
            g = [p for p in g if p["id"] not in taken]
            if len(g) < 2:
                continue
            g = sorted(g, key=lambda p: (-used(p["id"]), len(p["id"]), p["id"]))
            out.append({"kind": "publisher", "class": cls, "items": [p["id"] for p in g], "keep": g[0]["id"], "reason": reason,
                        "detail": {"names": [p["name"] for p in g], "uses": [used(p["id"]) for p in g]}})
            taken |= {p["id"] for p in g}
    targets = {r[0] for r in store.db.execute("SELECT DISTINCT publisher_id FROM publisher_alias")}
    for p in pubs:
        if p["id"] not in taken and p["id"] not in targets and not used(p["id"]):
            out.append({"kind": "publisher", "class": "orphan", "items": [p["id"]], "keep": None,
                        "reason": "Herausgeber ohne Dokument und ohne Reihe", "detail": {"names": [p["name"]]}})
    return out


def _series_groups(store: Store) -> list[dict]:
    out = []
    by: dict[tuple, list] = {}
    for s in store.db.execute("SELECT * FROM series").fetchall():
        by.setdefault((s["publisher_id"] or "", _txt(s["name"]), normalise_unit(s["unit"]) or "",
                       (canonical_geo(s["geo"]) or "").lower()), []).append(s)
    n_of = lambda sid: store.db.execute("SELECT (SELECT count(*) FROM observation WHERE series_id = ?) + "
                                        "(SELECT count(*) FROM forecast WHERE series_id = ?)", (sid, sid)).fetchone()[0]
    for g in by.values():
        if len(g) > 1:
            g = sorted(g, key=lambda s: (-n_of(s["id"]), s["created_at"] or "", s["id"]))
            out.append({"kind": "series", "class": "probable", "items": [s["id"] for s in g], "keep": g[0]["id"],
                        "reason": "gleiche Definition (Herausgeber, Name, Einheit, Region)",
                        "detail": {"names": [s["name"] for s in g], "values": [n_of(s["id"]) for s in g]}})
    # empty on purpose: edition trackers (update_hint / next_expected / frequency set, or 'tracker' in the name – Falk:
    # reminders of new editions are tracker series without values)
    for s in store.db.execute("SELECT s.id, s.name FROM series s WHERE NOT EXISTS (SELECT 1 FROM observation o WHERE "
                              "o.series_id = s.id) AND NOT EXISTS (SELECT 1 FROM forecast f WHERE f.series_id = s.id) "
                              "AND NOT EXISTS (SELECT 1 FROM exhibit_link l WHERE l.target_kind = 'series' AND l.target_id = s.id) "
                              "AND coalesce(s.update_hint, '') = '' AND coalesce(s.next_expected, '') = '' "
                              "AND coalesce(s.frequency, '') = '' AND lower(s.name) NOT LIKE '%tracker%'"):
        out.append({"kind": "series", "class": "probable", "items": [s["id"]], "keep": None, "action": "delete",
                    "reason": "Reihe ohne Werte", "detail": {"names": [s["name"]]}})
    return out


def _text_groups(store: Store) -> list[dict]:
    out = []
    for kind, cols in TEXT_KEY.items():
        by: dict[tuple, list] = {}
        for r in store.db.execute(f"SELECT * FROM {kind}").fetchall():
            key = tuple(_txt(r[c]) for c in cols)
            if any(key):
                by.setdefault((r["document_id"] or 0, r["visibility"]) + key, []).append(r)
        for g in by.values():
            if len(g) > 1:
                g = _better(g, kind)
                out.append({"kind": kind, "class": "safe", "items": [r["id"] for r in g], "keep": g[0]["id"],
                            "reason": "gleicher Text derselben Quelle", "detail": {"text": str(g[0][cols[0]])[:200]}})
    return out


def find(store: Store) -> list[dict]:
    """All findings (not stored)."""
    out = _variants(store) + _observation_groups(store) + _document_groups(store) + _publisher_groups(store) \
        + _series_groups(store) + _text_groups(store)
    for f in out:
        f.setdefault("signature", _sig(f["kind"] + (":" + f.get("action", "") if f.get("action") else "")
                                       + (":c" if f["class"] == "contradiction" else ""), f["items"]))
    return out


def _affected(f: dict) -> int:
    if f["class"] == "variant":
        return f["detail"]["n"]
    if f["class"] in ("orphan", "contradiction") or f.get("action") == "delete":
        return len(f["items"])
    return len(f["items"]) - 1


def _ensure(store: Store) -> None:
    store.db.executescript("""
CREATE TABLE IF NOT EXISTS cleanup_run (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('analyze','apply','decide','undo')),
  state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','done','failed','undone')),
  started_at TEXT NOT NULL, finished_at TEXT, actor TEXT, summary TEXT, undo_of INTEGER, backup TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS cleanup_candidate (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, class TEXT NOT NULL, signature TEXT NOT NULL UNIQUE, items TEXT NOT NULL,
  keep_id TEXT, action TEXT, reason TEXT, detail TEXT, affected INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','applied','rejected','later','gone')),
  found_run INTEGER, seen_run INTEGER, decided_at TEXT, decided_run INTEGER);
CREATE INDEX IF NOT EXISTS ix_cleanup_candidate_state ON cleanup_candidate(state, class);
CREATE TABLE IF NOT EXISTS cleanup_journal (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, seq INTEGER NOT NULL, tbl TEXT NOT NULL, where_sql TEXT NOT NULL,
  args TEXT, cols TEXT, rows TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_cleanup_journal_run ON cleanup_journal(run_id, seq);
""")


def _start(store: Store, kind: str, actor: str, **kw) -> int:
    _ensure(store)
    cur = store.db.execute("INSERT INTO cleanup_run(kind, started_at, actor, undo_of, backup) VALUES (?,?,?,?,?)",
                           (kind, _now(), actor, kw.get("undo_of"), kw.get("backup")))
    store.db.commit()
    return cur.lastrowid


def _finish(store: Store, run: int, summary: dict, state: str = "done", error: str | None = None) -> None:
    store.db.execute("UPDATE cleanup_run SET state = ?, finished_at = ?, summary = ?, error = ? WHERE id = ?",
                     (state, _now(), json.dumps(summary, ensure_ascii=False, default=str), error, run))
    store.db.commit()


def analyze(store: Store, actor: str = "cli") -> dict:
    """Find everything, file it (new findings open, vanished ones 'gone', decided ones keep their decision) and store
    the quality score of this moment."""
    run = _start(store, "analyze", actor)
    try:
        found = find(store)
        seen = set()
        for f in found:
            seen.add(f["signature"])
            row = store.db.execute("SELECT id, state FROM cleanup_candidate WHERE signature = ?", (f["signature"],)).fetchone()
            vals = (f["kind"], f["class"], json.dumps(f["items"]), None if f.get("keep") is None else str(f["keep"]),
                    f.get("action"), f["reason"], json.dumps(f["detail"], ensure_ascii=False, default=str), _affected(f))
            if row is None:
                store.db.execute("INSERT INTO cleanup_candidate(kind, class, items, keep_id, action, reason, detail, affected, "
                                 "signature, found_run, seen_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals + (f["signature"], run, run))
            else:
                state = row["state"] if row["state"] in ("rejected", "later") else "open"
                store.db.execute("UPDATE cleanup_candidate SET kind=?, class=?, items=?, keep_id=?, action=?, reason=?, detail=?, "
                                 "affected=?, state=?, seen_run=? WHERE id = ?", vals + (state, run, row["id"]))
        for r in store.db.execute("SELECT id, signature FROM cleanup_candidate WHERE state IN ('open','later')").fetchall():
            if r["signature"] not in seen:
                store.db.execute("UPDATE cleanup_candidate SET state = 'gone' WHERE id = ?", (r["id"],))
        store.db.commit()
        summary = score(store)
        _finish(store, run, summary)
        return {"run": run, **summary}
    except Exception as e:
        store.db.rollback()
        _finish(store, run, {}, "failed", f"{type(e).__name__}: {e}")
        raise


def score(store: Store) -> dict:
    """Quality score: share of the core records (data points, documents, publishers, series …) without an open finding."""
    total = sum(store.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CORE)
    by = {c: {"groups": 0, "records": 0} for c in CLASSES}
    for r in store.db.execute("SELECT class, count(*), coalesce(sum(affected), 0) FROM cleanup_candidate "
                              "WHERE state IN ('open','later') GROUP BY class"):
        by[r[0]] = {"groups": r[1], "records": r[2]}
    affected = sum(v["records"] for v in by.values())
    auto = sum(by[c]["records"] for c in AUTO)
    return {"total": total, "affected": affected, "score": round(1 - min(affected, total) / total, 4) if total else None,
            "classes": by, "auto_records": auto, "open_decisions": by["probable"]["groups"] + by["contradiction"]["groups"],
            "rejected": store.db.execute("SELECT count(*) FROM cleanup_candidate WHERE state = 'rejected'").fetchone()[0]}


# ------------------------------------------------------------------ changes
def _move_links(store: Store, j: Journal, kind: str, keep: str, drop: str) -> None:
    for table, kcol, icol in (("exhibit_link", "target_kind", "target_id"), ("item_check", "target_kind", "target_id"),
                              ("entity_link", "item_kind", "item_id")):
        store.db.execute(f"UPDATE OR IGNORE {table} SET {icol} = ? WHERE {kcol} = ? AND {icol} = ?", (keep, kind, drop))
        store.db.execute(f"DELETE FROM {table} WHERE {kcol} = ? AND {icol} = ?", (kind, drop))


def _snap_links(j: Journal, kind: str, ids: list[str]) -> None:
    ph = ",".join("?" * len(ids))
    j.snap("exhibit_link", f"target_kind = ? AND target_id IN ({ph})", [kind, *ids])
    j.snap("item_check", f"target_kind = ? AND target_id IN ({ph})", [kind, *ids])
    if kind in ("observation", "forecast", "study", "quote", "practice", "fact", "series"):
        j.snap("entity_link", f"item_kind = ? AND item_id IN ({ph})", [kind, *ids])
    j.snap("tag", f"item_kind = ? AND item_id IN ({ph})", [kind, *ids])
    j.snap("origin", f"item_kind = ? AND item_id IN ({ph})", [kind, *ids])
    j.snap("item_redirect", f"item_kind = ? AND (old_id IN ({ph}) OR new_id IN ({ph}))", [kind, *ids, *ids])


def _redirect(store: Store, kind: str, old: str, new: str, run: int) -> None:
    store.db.execute("UPDATE item_redirect SET new_id = ? WHERE item_kind = ? AND new_id = ?", (new, kind, old))
    store.db.execute("INSERT OR REPLACE INTO item_redirect(item_kind, old_id, new_id, run_id) VALUES (?,?,?,?)",
                     (kind, old, new, run))


def merge(store: Store, j: Journal, kind: str, keep, drop) -> None:
    """Fold record `drop` into `keep` (journalled). Kinds: the item kinds, document, publisher, series."""
    keep, drop = str(keep), str(drop)
    if keep == drop:
        return
    ids = [keep, drop]
    if kind in ITEM_KINDS:
        a, b = (store.db.execute(f"SELECT visibility FROM {kind} WHERE id = ?", (int(x),)).fetchone() for x in ids)
        if a is None or b is None:
            return
        if a[0] != b[0]:
            raise ValueError(f"{kind} {keep}/{drop}: internal and public records are never merged")
        j.snap(kind, "id IN (?, ?)", [int(keep), int(drop)])
        if kind == "observation":
            j.snap("forecast", "actual_observation_id IN (?, ?)", [int(keep), int(drop)])
        _snap_links(j, kind, ids)
        store._merge_item(kind, int(keep), int(drop))
        _move_links(store, j, kind, keep, drop)
    elif kind == "document":
        j.snap("document", "id IN (?, ?)", [int(keep), int(drop)])
        for k in ITEM_KINDS:
            j.snap(k, "document_id IN (?, ?)", [int(keep), int(drop)])
        _snap_links(j, "document", ids)
        row_k = store.db.execute("SELECT * FROM document WHERE id = ?", (int(keep),)).fetchone()
        row_d = store.db.execute("SELECT * FROM document WHERE id = ?", (int(drop),)).fetchone()
        if row_k is None or row_d is None:
            return
        store._fill("document", "id", int(keep), row_k, {k: row_d[k] for k in row_d.keys() if k not in ("id", "created_at")})
        for k in ITEM_KINDS:
            for (iid,) in store.db.execute(f"SELECT id FROM {k} WHERE document_id = ?", (int(drop),)).fetchall():
                store._touch(k, iid)
            store.db.execute(f"UPDATE {k} SET document_id = ? WHERE document_id = ?", (int(keep), int(drop)))
        store.db.execute("UPDATE OR IGNORE tag SET item_id = ? WHERE item_kind = 'document' AND item_id = ?", (keep, drop))
        store.db.execute("DELETE FROM tag WHERE item_kind = 'document' AND item_id = ?", (drop,))
        store.db.execute("UPDATE origin SET item_id = ?, merged = 1 WHERE item_kind = 'document' AND item_id = ?", (keep, drop))
        _move_links(store, j, "document", keep, drop)
        store.db.execute("DELETE FROM document WHERE id = ?", (int(drop),))
        store.log("document", keep, "merge", {"merged": int(drop)})
        store._touch_document(int(keep))
    elif kind == "publisher":
        j.snap("publisher", "id IN (?, ?)", ids)
        j.snap("document", "publisher_id IN (?, ?)", ids, cols=["publisher_id"])
        j.snap("series", "publisher_id IN (?, ?)", ids, cols=["publisher_id"])
        j.snap("publisher_alias", "publisher_id IN (?, ?) OR alias IN (?, ?)", ids + ids)
        store.merge_publisher(drop, keep)
    elif kind == "series":
        j.snap("series", "id IN (?, ?)", ids)
        j.snap("observation", "series_id IN (?, ?)", ids, cols=["series_id"])
        j.snap("forecast", "series_id IN (?, ?)", ids, cols=["series_id"])
        _snap_links(j, "series", ids)
        row_k = store.db.execute("SELECT * FROM series WHERE id = ?", (keep,)).fetchone()
        row_d = store.db.execute("SELECT * FROM series WHERE id = ?", (drop,)).fetchone()
        if row_k is None or row_d is None:
            return
        store._fill("series", "id", keep, row_k, {k: row_d[k] for k in row_d.keys() if k not in ("id", "created_at")})
        for k in ("observation", "forecast"):
            for (iid,) in store.db.execute(f"SELECT id FROM {k} WHERE series_id = ?", (drop,)).fetchall():
                store._touch(k, iid)
            store.db.execute(f"UPDATE {k} SET series_id = ? WHERE series_id = ?", (keep, drop))
        store.db.execute("UPDATE OR IGNORE tag SET item_id = ? WHERE item_kind = 'series' AND item_id = ?", (keep, drop))
        store.db.execute("DELETE FROM tag WHERE item_kind = 'series' AND item_id = ?", (drop,))
        store.db.execute("UPDATE origin SET item_id = ?, merged = 1 WHERE item_kind = 'series' AND item_id = ?", (keep, drop))
        _move_links(store, j, "series", keep, drop)
        store.db.execute("DELETE FROM search_index WHERE item_kind = 'series' AND item_id = ?", (drop,))
        store.db.execute("DELETE FROM series WHERE id = ?", (drop,))
        store.log("series", keep, "merge", {"merged": drop})
        store._touch("series", keep)
    else:
        raise ValueError(f"cannot merge {kind}")
    _redirect(store, kind, drop, keep, j.run)


def _delete(store: Store, j: Journal, kind: str, iid) -> None:
    if kind == "publisher":
        j.snap("publisher", "id = ?", [iid])
        store.db.execute("DELETE FROM publisher WHERE id = ?", (iid,))
    elif kind == "series":
        j.snap("series", "id = ?", [iid])
        j.snap("tag", "item_kind = 'series' AND item_id = ?", [iid])
        store.db.execute("DELETE FROM tag WHERE item_kind = 'series' AND item_id = ?", (iid,))
        store.db.execute("DELETE FROM search_index WHERE item_kind = 'series' AND item_id = ?", (iid,))
        store.db.execute("DELETE FROM series WHERE id = ?", (iid,))
    else:
        raise ValueError(f"cannot delete {kind}")
    store.log(kind, iid, "delete", {"reason": "cleanup"})


def _set_values(store: Store, j: Journal, d: dict, ids: list) -> int:
    """Variant fix: set one column (geo/unit) or tidy whitespace for the given rows; keeps `ident` in step."""
    table, col = d["table"], d["col"]
    has_ident = table in ITEM_KINDS and col in ("geo", "unit")
    n = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        j.snap(table, f"id IN ({ph})", chunk, cols=[col] + (["ident"] if has_ident else []))
        for r in store.db.execute(f"SELECT * FROM {table} WHERE id IN ({ph})", chunk).fetchall():
            new = _ws(r[col]) if d["op"] == "space" else d["to"]
            if d["op"] == "set" and r[col] != d["from"]:
                continue                            # changed meanwhile
            if new == r[col]:
                continue
            if has_ident:
                data = {k: r[k] for k in r.keys()}
                data[col] = new
                store.db.execute(f"UPDATE {table} SET {col} = ?, ident = ? WHERE id = ?", (new, ident_key(table, data), r["id"]))
            else:
                store.db.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (new, r["id"]))
            store._touch(table, r["id"]) if table in ITEM_KINDS + ("series",) else None
            n += 1
    if n:
        store.log(table, "*", "harmonise", {"col": col, "from": d.get("from"), "to": d.get("to"), "n": n})
    return n


def _carry_out(store: Store, j: Journal, c, action: str) -> int:
    """One candidate row → changes. Returns the number of records changed."""
    items, kind, d = json.loads(c["items"]), c["kind"], json.loads(c["detail"] or "{}")
    if c["class"] == "variant":
        return _set_values(store, j, d, items)
    if action == "delete" or (c["class"] == "orphan" and action == "merge"):
        for i in items:
            _delete(store, j, kind, i)
        return len(items)
    if action == "dispute":
        n = 0
        for i in items:
            r = store.db.execute(f"SELECT status FROM {kind} WHERE id = ?", (int(i),)).fetchone()
            if r and r[0] == "unverified":
                j.snap(kind, "id = ?", [int(i)], cols=["status"])
                store.update(kind, int(i), {"status": "disputed"})
                n += 1
        return n
    keep = c["keep_id"]
    n = 0
    for i in items:
        if str(i) != str(keep):
            merge(store, j, kind, keep, i)
            n += 1
    return n


def _backup(store: Store) -> str:
    dst = store.path.parent / f"backup-before-cleanup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite"
    out = sqlite3.connect(dst)
    try:
        store.db.backup(out)
    finally:
        out.close()
    # the nightly tidy makes one per night: keep the newest BACKUPS_KEPT of these automatic backups (other backups untouched)
    for old in sorted(store.path.parent.glob("backup-before-cleanup-*.sqlite"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return str(dst)


BACKUPS_KEPT = 3


def apply(store: Store, actor: str = "cli", backup: bool = True, classes=AUTO) -> dict:
    """Carry out the automatic classes (variants, safe duplicates, orphans): fresh analysis first, a backup, then one
    transaction per finding (other sessions are blocked only briefly). Returns counts; undo(store, run) reverses it."""
    analyze(store, actor)
    bk = _backup(store) if backup else None
    run = _start(store, "apply", actor, backup=bk)
    j = Journal(store, run)
    done = {c: {"groups": 0, "records": 0} for c in classes}
    failed = []
    for c in store.db.execute(f"SELECT * FROM cleanup_candidate WHERE state = 'open' AND class IN ({','.join('?' * len(classes))}) "
                              "ORDER BY CASE class WHEN 'variant' THEN 0 WHEN 'safe' THEN 1 ELSE 2 END, id",
                              list(classes)).fetchall():
        try:
            with store.batch(actor):
                n = _carry_out(store, j, c, c["action"] or "merge")
                store.db.execute("UPDATE cleanup_candidate SET state = 'applied', decided_at = ?, decided_run = ? WHERE id = ?",
                                 (_now(), run, c["id"]))
            done[c["class"]]["groups"] += 1
            done[c["class"]]["records"] += n
        except Exception as e:                  # one odd group must not stop the run
            failed.append({"candidate": c["id"], "error": f"{type(e).__name__}: {e}"[:200]})
    summary = {"done": done, "failed": failed, "backup": bk}
    _finish(store, run, summary)
    after = analyze(store, actor)
    return {"run": run, **summary, "score": after["score"]}


def decide(store: Store, ids, action: str, keep=None, actor: str = "portal") -> dict:
    """Falk's decision on findings: merge (into keep_id or `keep`) | delete (series without values) | dispute
    (contradictions: unverified ones become 'disputed') | reject ('no duplicate' – never proposed again) | later | reopen."""
    if action not in ("merge", "delete", "dispute", "reject", "later", "reopen"):
        raise ValueError("action: merge | delete | dispute | reject | later | reopen")
    _ensure(store)
    ids = [int(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
    rows = [store.db.execute("SELECT * FROM cleanup_candidate WHERE id = ?", (i,)).fetchone() for i in ids]
    if any(r is None for r in rows):
        raise LookupError("unknown finding")
    if action in ("reject", "later", "reopen"):
        state = {"reject": "rejected", "later": "later", "reopen": "open"}[action]
        store.db.executemany("UPDATE cleanup_candidate SET state = ?, decided_at = ? WHERE id = ?", [(state, _now(), i) for i in ids])
        store.db.commit()
        return {"decided": len(ids), "state": state, **score(store)}
    run = _start(store, "decide", actor)
    j = Journal(store, run)
    n, failed = 0, []
    for r in rows:
        if r["state"] not in ("open", "later"):
            continue
        if keep is not None and len(rows) == 1:
            if str(keep) not in [str(x) for x in json.loads(r["items"])]:
                raise ValueError("keep must be one of the group")
            store.db.execute("UPDATE cleanup_candidate SET keep_id = ? WHERE id = ?", (str(keep), r["id"]))
            r = store.db.execute("SELECT * FROM cleanup_candidate WHERE id = ?", (r["id"],)).fetchone()
        try:
            with store.batch(actor):
                n += _carry_out(store, j, r, action)
                store.db.execute("UPDATE cleanup_candidate SET state = 'applied', decided_at = ?, decided_run = ? WHERE id = ?",
                                 (_now(), run, r["id"]))
        except Exception as e:
            failed.append({"candidate": r["id"], "error": f"{type(e).__name__}: {e}"[:200]})
    _finish(store, run, {"action": action, "candidates": ids, "records": n, "failed": failed})
    return {"run": run, "records": n, "failed": failed, **score(store)}


def undo(store: Store, run_id: int, actor: str = "portal") -> dict:
    """Restore every row the run changed to its state before the run (later edits of the same rows are lost – the
    portal says so), reopen its findings."""
    _ensure(store)
    r = store.db.execute("SELECT * FROM cleanup_run WHERE id = ?", (run_id,)).fetchone()
    if r is None or r["kind"] not in ("apply", "decide"):
        raise LookupError(f"no apply/decide run {run_id}")
    if r["state"] != "done":
        raise ValueError(f"run {run_id} is {r['state']}")
    run = _start(store, "undo", actor, undo_of=run_id)
    touched: set[tuple[str, str]] = set()
    with store.batch(actor):
        # rows come back in reverse order of the run, so a reference may briefly point to a row restored a step later:
        # check foreign keys at commit (the pragma only holds inside an open transaction, hence the first write)
        store.db.execute("UPDATE cleanup_run SET state = state WHERE id = ?", (run_id,))
        store.db.execute("PRAGMA defer_foreign_keys = ON")
        for e in store.db.execute("SELECT * FROM cleanup_journal WHERE run_id = ? ORDER BY seq DESC", (run_id,)).fetchall():
            rows = json.loads(e["rows"])
            _restore(store.db, e["tbl"], e["where_sql"], json.loads(e["args"] or "[]"),
                     json.loads(e["cols"]) if e["cols"] else None, rows)
            if e["tbl"] in ITEM_KINDS + ("series",):
                touched |= {(e["tbl"], str(x["id"])) for x in rows if "id" in x}
        for kind, iid in touched:
            store._touch(kind, iid)
        store.db.execute("UPDATE cleanup_candidate SET state = 'open', decided_at = NULL, decided_run = NULL WHERE decided_run = ?",
                         (run_id,))
        store.db.execute("UPDATE cleanup_run SET state = 'undone' WHERE id = ?", (run_id,))
        store.log("cleanup", run_id, "undo", {"records": len(touched)})
    _finish(store, run, {"undone": run_id, "records": len(touched)})
    after = analyze(store, actor)
    return {"run": run, "undone": run_id, "records": len(touched), "score": after["score"]}


# ------------------------------------------------------------------ reading (portal, CLI)
def candidates(store: Store, cls: str | None = None, state: str = "open", limit: int = 50, offset: int = 0) -> dict:
    _ensure(store)
    where, args = ["state = ?"], [state]
    if cls:
        where.append("class = ?")
        args.append(cls)
    total = store.db.execute(f"SELECT count(*) FROM cleanup_candidate WHERE {' AND '.join(where)}", args).fetchone()[0]
    rows = store.db.execute(f"SELECT * FROM cleanup_candidate WHERE {' AND '.join(where)} ORDER BY affected DESC, id "
                            "LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {"total": total, "items": [_view(store, r) for r in rows]}


def _view(store: Store, r) -> dict:
    items = json.loads(r["items"])
    out = {"id": r["id"], "kind": r["kind"], "class": r["class"], "reason": r["reason"], "state": r["state"],
           "keep": r["keep_id"], "action": r["action"], "affected": r["affected"], "detail": json.loads(r["detail"] or "{}"),
           "count": len(items)}
    if r["class"] != "variant":
        out["records"] = [_brief(store, r["kind"], i) for i in items[:8]]
    else:
        out["sample"] = items[:5]
    return out


def _brief(store: Store, kind: str, iid) -> dict:
    key = str(iid) if kind in ("publisher", "series") else int(iid)
    row = store.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (key,)).fetchone()
    if row is None:
        return {"id": iid, "gone": True}
    pick = {"observation": ("metric", "value", "value_text", "unit", "period", "geo", "category", "population", "status", "series_id"),
            "document": ("title", "url", "publisher_id", "published", "doi", "kind"),
            "publisher": ("name", "type", "country", "url"), "series": ("name", "publisher_id", "unit", "geo", "frequency"),
            "quote": ("text", "speaker", "status"), "practice": ("organisation", "what", "status"),
            "fact": ("kind", "title", "status"), "forecast": ("statement", "target_period", "status"),
            "study": ("finding", "status")}[kind]
    b = {"id": iid, **{k: row[k] for k in pick if k in row.keys() and row[k] not in (None, "")}}
    if "document_id" in row.keys() and row["document_id"]:
        d = store.db.execute("SELECT title, url FROM document WHERE id = ?", (row["document_id"],)).fetchone()
        if d:
            b["document"] = {"id": row["document_id"], "title": d["title"], "url": d["url"]}
    if kind in ITEM_KINDS + ("document", "series"):
        b["slides"] = store.db.execute("SELECT count(DISTINCT exhibit_id) FROM exhibit_link WHERE target_kind = ? AND target_id = ?",
                                       (kind, str(iid))).fetchone()[0]
    return b


def runs(store: Store, limit: int = 30) -> list[dict]:
    _ensure(store)
    return [{**{k: r[k] for k in r.keys() if k != "summary"}, "summary": json.loads(r["summary"] or "{}")}
            for r in store.db.execute("SELECT * FROM cleanup_run ORDER BY id DESC LIMIT ?", (limit,))]


def status(store: Store) -> dict:
    """For the statistics: the current score, open findings per class, the trend of the last analyses, recent runs."""
    _ensure(store)
    last = store.db.execute("SELECT * FROM cleanup_run WHERE kind = 'analyze' AND state = 'done' ORDER BY id DESC LIMIT 1").fetchone()
    trend = []
    for r in store.db.execute("SELECT finished_at, summary FROM cleanup_run WHERE kind = 'analyze' AND state = 'done' "
                              "ORDER BY id DESC LIMIT 400"):
        s = json.loads(r["summary"] or "{}")
        day = (r["finished_at"] or "")[:10]
        if s.get("score") is not None and not any(t["date"] == day for t in trend):
            trend.append({"date": day, "score": s["score"], "affected": s.get("affected")})
        if len(trend) >= 60:
            break
    out = {"last_analysis": last["finished_at"] if last else None, **(json.loads(last["summary"]) if last else {}),
           "trend": list(reversed(trend)),
           "runs": [x for x in runs(store, 12) if x["kind"] != "analyze"][:8],
           "running": [dict(r) for r in store.db.execute("SELECT id, kind, started_at FROM cleanup_run WHERE state = 'running'")]}
    return out
