"""Statistics about the archive and its operation, for the NAS dashboard (`fundus metrics`).

compute(store) describes the data: volume, composition, time series, coverage, forecasts, sources, quality and
activity. health() describes the operation on the Spark: MCP server, timers, snapshot, last import, usage.
Both return plain JSON-serialisable dicts; the dashboard collector runs `python -m fundus.cli metrics`.

Block `exhibits` (slides/charts, fundus/exhibits.py) – cached part (recomputed when exhibits change):
  logical                 logical slides (project + set + slug)    versions        stored versions
  sets                    slide sets (Foliensätze) over all projects (every project has a default set 'main')
  byProject               [{project, logical, versions, decks, sets}]   withScriptShare share of slides whose script is known
  byKind / byPalette / byLanguage   {slide|chart|poster|section: n} / {blue|hsg|unknown: n} / {de|en|unknown: n}
  topChartTypes           [{type, n}] (top 10 over current versions)
  policy, shared, internal, released   sharing policy and counts of public / internal / released exhibits
  decks                   {count, memberships, slidesPerDeck: [{project, deck, slides}]}
  density                 {measured, OK, CHECK, SPLIT, splitCheckShare} over each slide's latest measured version
Live part (always fresh, like activity):
  versions7d, versions30d, versionsByDay [{date, n}] (30 days, by content date of the version)
  usage                   {exhibitCallsByDay [{date, calls}] (fundus_exhibit), searchHitsByDay [{date, searches}] (searches
                          whose top 10 contained an exhibit), kindFilterSearches30d (kind exhibit/slide/chart/…), views30d,
                          topViewed [{id, title, project, kind, views}] (5 most-opened exhibits, 30 days: fundus_exhibit calls
                          + full PNG/HTML fetches)}"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from fundus.store import ITEM_KINDS, Store

STATUSES = ("verified", "unverified", "disputed", "retracted")
OUTCOMES = ("open", "due", "hit", "partly", "miss", "unresolvable")


def _year(text) -> int | None:
    m = re.search(r"(19|20)\d{2}", str(text or ""))
    return int(m.group(0)) if m else None


def _last_year(text) -> int | None:
    ys = re.findall(r"(?:19|20)\d{2}", str(text or ""))
    return int(ys[-1]) if ys else None


def _rows(st: Store, sql: str, *params) -> list[dict]:
    return [dict(r) for r in st.db.execute(sql, params).fetchall()]


def _compute_full(st: Store, today: dt.date) -> dict:
    today = today or dt.date.today()
    q = st.db.execute
    totals = {t: q(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ITEM_KINDS + ("series", "document", "publisher")}
    items_total = sum(totals[k] for k in ITEM_KINDS)

    # composition ---------------------------------------------------------------------------------------------
    by_kind_status = []
    vis = {"public": 0, "internal": 0}
    for k in ITEM_KINDS:
        row = {"kind": k, **{s: 0 for s in STATUSES}}
        for s, n in q(f"SELECT status, count(*) FROM {k} GROUP BY status"):
            row[s] = n
        by_kind_status.append(row)
        for v, n in q(f"SELECT visibility, count(*) FROM {k} GROUP BY visibility"):
            vis[v] = vis.get(v, 0) + n
    verified = sum(r["verified"] for r in by_kind_status)

    labels = {(f, v): l for f, v, l in q("SELECT facet, value, label FROM vocab")}
    parent = {(f, v): p for f, v, p in q("SELECT facet, value, parent FROM vocab")}

    def facet_counts(facet: str, limit: int | None = None) -> list[dict]:
        rows = _rows(st, "SELECT value, count(*) AS items, count(DISTINCT item_kind) AS kinds FROM tag WHERE facet = ? "
                         "AND item_kind != 'document' GROUP BY value ORDER BY items DESC", facet)
        out = [{"value": r["value"], "label": labels.get((facet, r["value"]), r["value"]), "items": r["items"],
                "parent": parent.get((facet, r["value"]))} for r in rows]
        return out[:limit] if limit else out

    by_project = []
    for r in _rows(st, "SELECT project, item_kind, count(*) AS n FROM origin WHERE merged = 0 GROUP BY project, item_kind"):
        p = next((x for x in by_project if x["project"] == r["project"]), None)
        if p is None:
            p = {"project": r["project"], "items": 0, "byKind": {}}
            by_project.append(p)
        p["byKind"][r["item_kind"]] = r["n"]
        p["items"] += r["n"]
    for p in by_project:
        tagged = [k for k in ITEM_KINDS]
        ver = 0
        for k in tagged:
            ver += q(f"SELECT count(*) FROM {k} WHERE status = 'verified' AND id IN "
                     f"(SELECT CAST(item_id AS INTEGER) FROM tag WHERE item_kind = ? AND facet = 'project' AND value = ?)",
                     (k, p["project"])).fetchone()[0]
        p["verifiedShare"] = round(ver / p["items"], 3) if p["items"] else None
    by_project.sort(key=lambda x: -x["items"])

    industries = facet_counts("industry")
    topics = facet_counts("topic")
    regions = facet_counts("region")
    top_ind = [x["value"] for x in industries[:10]]
    top_top = [x["value"] for x in topics[:12]]
    cells = {}
    if top_ind and top_top:
        for a, b, n in q(f"""SELECT i.value, t.value, count(*) FROM tag i JOIN tag t ON t.item_kind = i.item_kind
                             AND t.item_id = i.item_id AND t.facet = 'topic' WHERE i.facet = 'industry'
                             AND i.item_kind != 'document' AND i.value IN ({",".join("?" * len(top_ind))})
                             AND t.value IN ({",".join("?" * len(top_top))}) GROUP BY i.value, t.value""",
                         (*top_ind, *top_top)):
            cells[(a, b)] = n
    matrix = {"rows": [{"value": v, "label": labels.get(("industry", v), v)} for v in top_ind],
              "cols": [{"value": v, "label": labels.get(("topic", v), v)} for v in top_top],
              "values": [[cells.get((a, b), 0) for b in top_top] for a in top_ind]}

    # time series ---------------------------------------------------------------------------------------------
    ser = _rows(st, """SELECT s.id, s.name, s.unit, s.frequency, s.next_expected, s.last_checked, p.name AS publisher,
                              count(o.id) AS n, count(DISTINCT o.period) AS periods,
                              min(o.period) AS first, max(o.period) AS last
                       FROM series s LEFT JOIN observation o ON o.series_id = s.id
                       LEFT JOIN publisher p ON p.id = s.publisher_id GROUP BY s.id""")
    hist = {}
    span = {}
    fresh = {"≤ 1 Jahr": 0, "1–2 Jahre": 0, "2–5 Jahre": 0, "über 5 Jahre": 0, "ohne Periode": 0}
    for s in ser:
        if not s["n"]:
            continue
        key = str(s["periods"]) if s["periods"] < 10 else "10+"
        hist[key] = hist.get(key, 0) + 1
        y1, y2 = _year(s["first"]), _last_year(s["last"])
        if y1 and y2 and s["periods"] >= 2:
            sp = y2 - y1
            b = "0" if sp <= 0 else "1" if sp == 1 else "2–3" if sp <= 3 else "4–6" if sp <= 6 else "7+"
            span[b] = span.get(b, 0) + 1
        if not y2:
            fresh["ohne Periode"] += 1
        else:
            age = today.year - y2
            fresh["≤ 1 Jahr" if age <= 1 else "1–2 Jahre" if age <= 2 else "2–5 Jahre" if age <= 5 else "über 5 Jahre"] += 1
    order = [str(i) for i in range(1, 10)] + ["10+"]
    due = sum(1 for s in ser if s["n"] and (s["frequency"] != "once") and
              ((s["next_expected"] and s["next_expected"] <= today.isoformat()[:7]) or not s["last_checked"]))
    top_series = sorted([s for s in ser if s["periods"] >= 2], key=lambda s: (-s["periods"], s["id"]))[:12]
    series_block = {
        "total": len(ser), "withObservations": sum(1 for s in ser if s["n"]),
        "multiPeriod": sum(1 for s in ser if s["periods"] >= 2),
        "periodsHistogram": [{"periods": k, "series": hist[k]} for k in order if k in hist],
        "spanYears": [{"span": k, "series": span[k]} for k in ("0", "1", "2–3", "4–6", "7+") if k in span],
        "freshness": [{"bucket": k, "series": v} for k, v in fresh.items()],
        "dueForCheck": due, "neverChecked": sum(1 for s in ser if s["n"] and not s["last_checked"]),
        "top": [{"id": s["id"], "name": s["name"], "periods": s["periods"], "first": s["first"], "last": s["last"],
                 "publisher": s["publisher"], "unit": s["unit"]} for s in top_series],
    }

    # coverage over time --------------------------------------------------------------------------------------
    def year_hist(sql: str) -> list[dict]:
        c = {}
        for (v,) in q(sql):
            y = _year(v)
            if y and 1990 <= y <= today.year + 15:
                c[y] = c.get(y, 0) + 1
        return [{"year": y, "n": c[y]} for y in sorted(c)]
    coverage = {"observationsByYear": year_hist("SELECT period FROM observation"),
                "documentsByPublishedYear": year_hist("SELECT published FROM document"),
                "forecastTargetYears": year_hist("SELECT target_period FROM forecast")}

    # forecasts -----------------------------------------------------------------------------------------------
    by_outcome = {o: 0 for o in OUTCOMES}
    for o, n in q("SELECT outcome, count(*) FROM forecast GROUP BY outcome"):
        by_outcome[o] = n
    by_target = {}
    for tp, o in q("SELECT target_period, outcome FROM forecast"):
        y = _last_year(tp)
        if not y:
            continue
        row = by_target.setdefault(y, {"year": y, **{x: 0 for x in OUTCOMES}})
        row[o] += 1
    resolved = by_outcome["hit"] + by_outcome["partly"] + by_outcome["miss"]
    past = sum(r[x] for y, r in by_target.items() if y < today.year for x in ("open", "due"))
    due_list = _rows(st, """SELECT f.id, f.statement, f.target_period, f.value, f.unit, coalesce(f.forecaster, p.name) AS who
                            FROM forecast f LEFT JOIN document d ON d.id = f.document_id
                            LEFT JOIN publisher p ON p.id = d.publisher_id
                            WHERE f.outcome = 'due' AND f.visibility = 'public' ORDER BY f.target_period LIMIT 12""")
    forecasts = {"total": totals["forecast"], "byOutcome": by_outcome,
                 "byTargetYear": [by_target[y] for y in sorted(by_target)],
                 "pastTargetUnresolved": past, "resolved": resolved,
                 "hitRate": round((by_outcome["hit"] + 0.5 * by_outcome["partly"]) / resolved, 3) if resolved else None,
                 "linkedToSeries": q("SELECT count(*) FROM forecast WHERE series_id IS NOT NULL").fetchone()[0],
                 "dueList": [{**r, "statement": r["statement"][:160]} for r in due_list]}

    # sources -------------------------------------------------------------------------------------------------
    d = q("SELECT count(*), sum(url IS NOT NULL), sum(doi IS NOT NULL), sum(is_vendor) FROM document").fetchone()
    pub_types = _rows(st, """SELECT coalesce(p.type, 'unbekannt') AS type, count(DISTINCT p.id) AS publishers,
                                    count(d.id) AS documents
                             FROM publisher p LEFT JOIN document d ON d.publisher_id = p.id GROUP BY 1 ORDER BY documents DESC""")
    items_per_pub = {}
    for k in ITEM_KINDS:
        for pid, n in q(f"SELECT d.publisher_id, count(*) FROM {k} x JOIN document d ON d.id = x.document_id "
                        "WHERE d.publisher_id IS NOT NULL GROUP BY d.publisher_id"):
            items_per_pub[pid] = items_per_pub.get(pid, 0) + n
    names = {r[0]: (r[1], r[2]) for r in q("SELECT id, name, type FROM publisher")}
    top_pub = sorted(items_per_pub.items(), key=lambda x: -x[1])[:15]
    docs_per_pub = dict(q("SELECT publisher_id, count(*) FROM document GROUP BY publisher_id").fetchall())
    conc = sorted(items_per_pub.values(), reverse=True)
    sources = {"documents": d[0], "withUrl": d[1] or 0, "withDoi": d[2] or 0, "vendor": d[3] or 0,
               "byKind": dict(q("SELECT coalesce(kind, 'unbekannt'), count(*) FROM document GROUP BY 1 ORDER BY 2 DESC").fetchall()),
               "publisherTypes": pub_types,
               "topPublishers": [{"id": p, "name": names.get(p, (p, None))[0], "type": names.get(p, (p, None))[1],
                                  "items": n, "documents": docs_per_pub.get(p, 0)} for p, n in top_pub],
               "top10Share": round(sum(conc[:10]) / sum(conc), 3) if conc else None,
               "publishersWithOneDocument": sum(1 for v in docs_per_pub.values() if v == 1)}

    # quality -------------------------------------------------------------------------------------------------
    no_src = sum(q(f"SELECT count(*) FROM {k} WHERE document_id IS NULL").fetchone()[0] for k in ITEM_KINDS)
    units = _rows(st, "SELECT unit, count(*) AS n FROM observation WHERE unit IS NOT NULL GROUP BY unit ORDER BY n DESC")
    quality = {
        "verifiedShare": round(verified / items_total, 3) if items_total else None,
        "disputed": sum(r["disputed"] for r in by_kind_status), "retracted": sum(r["retracted"] for r in by_kind_status),
        "itemsWithoutSource": no_src,
        "quotesWithoutSpeaker": q("SELECT count(*) FROM quote WHERE coalesce(speaker, '') = ''").fetchone()[0],
        "observationsWithoutUnit": q("SELECT count(*) FROM observation WHERE value IS NOT NULL AND unit IS NULL").fetchone()[0],
        "observationsWithoutPeriod": q("SELECT count(*) FROM observation WHERE coalesce(period, '') = ''").fetchone()[0],
        "observationsWithoutPopulation": q("SELECT count(*) FROM observation WHERE coalesce(population, '') = ''").fetchone()[0],
        "mergedDuplicates": q("SELECT count(*) FROM origin WHERE merged = 1").fetchone()[0],
        "distinctUnits": len(units), "unitsTop": units[:12],
    }

    activity = _activity(st, today)
    exhibits = _exhibits_static(st)

    return {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "db": {"bytes": st.path.stat().st_size if st.path.exists() else None, "path": str(st.path)},
            "totals": totals, "itemsTotal": items_total, "byKindStatus": by_kind_status, "visibility": vis,
            "byProject": by_project, "byIndustry": industries, "byTopic": topics[:25], "byRegion": regions[:20],
            "vocab": {f: q("SELECT count(*) FROM vocab WHERE facet = ?", (f,)).fetchone()[0] for f in ("industry", "topic", "region")},
            "matrix": matrix, "series": series_block, "coverage": coverage, "forecasts": forecasts,
            "sources": sources, "quality": quality, "activity": activity, "exhibits": exhibits}


def _exhibits_static(st: Store) -> dict:
    q = st.db.execute
    if not q("SELECT 1 FROM sqlite_master WHERE name = 'exhibit'").fetchone():
        return {}
    logical = q("SELECT count(*) FROM exhibit").fetchone()[0]
    by_project = _rows(st, """SELECT e.project, count(*) AS logical,
                                     (SELECT count(*) FROM exhibit_version v JOIN exhibit e2 ON e2.id = v.exhibit_id
                                      WHERE e2.project = e.project) AS versions,
                                     (SELECT count(DISTINCT deck) FROM exhibit_deck d WHERE d.project = e.project) AS decks,
                                     (SELECT count(*) FROM exhibit_set s WHERE s.project = e.project) AS sets
                              FROM exhibit e GROUP BY e.project ORDER BY logical DESC""")
    charts: dict[str, int] = {}
    for (ct,) in q("SELECT chart_types FROM exhibit WHERE chart_types IS NOT NULL"):
        for t in {x.strip() for x in ct.split(",") if x.strip()}:
            charts[t] = charts.get(t, 0) + 1
    vis = dict(q("SELECT visibility, count(*) FROM exhibit GROUP BY visibility").fetchall())
    pol = q("SELECT value FROM meta WHERE key = 'exhibit_sharing'").fetchone()
    dens = {"measured": 0, "OK": 0, "CHECK": 0, "SPLIT": 0}
    for (d,) in q("""SELECT v.density FROM exhibit_version v WHERE v.density IS NOT NULL AND v.n = (SELECT max(n) FROM
                     exhibit_version v2 WHERE v2.exhibit_id = v.exhibit_id AND v2.density IS NOT NULL)"""):
        try:
            verdict = json.loads(d).get("verdict")
        except ValueError:
            continue
        if verdict in ("OK", "CHECK", "SPLIT"):
            dens["measured"] += 1
            dens[verdict] += 1
    dens["splitCheckShare"] = round((dens["CHECK"] + dens["SPLIT"]) / dens["measured"], 3) if dens["measured"] else None
    with_script = q("SELECT count(DISTINCT exhibit_id) FROM exhibit_version WHERE script_sha IS NOT NULL").fetchone()[0]
    return {
        "logical": logical, "versions": q("SELECT count(*) FROM exhibit_version").fetchone()[0], "byProject": by_project,
        "sets": q("SELECT count(*) FROM exhibit_set").fetchone()[0],
        "withScriptShare": round(with_script / logical, 3) if logical else None,
        "byKind": dict(q("SELECT kind, count(*) FROM exhibit GROUP BY kind ORDER BY 2 DESC").fetchall()),
        "byPalette": dict(q("SELECT coalesce(palette, 'unknown'), count(*) FROM exhibit GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "byLanguage": dict(q("SELECT coalesce(language, 'unknown'), count(*) FROM exhibit GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "topChartTypes": [{"type": t, "n": n} for t, n in sorted(charts.items(), key=lambda x: -x[1])[:10]],
        "policy": pol[0] if pol else "share_everything", "shared": vis.get("public", 0), "internal": vis.get("internal", 0),
        "released": q("SELECT count(*) FROM exhibit WHERE released = 1").fetchone()[0],
        "decks": {"count": q("SELECT count(*) FROM (SELECT DISTINCT project, deck FROM exhibit_deck)").fetchone()[0],
                  "memberships": q("SELECT count(*) FROM exhibit_deck").fetchone()[0],
                  "slidesPerDeck": _rows(st, "SELECT project, deck, count(*) AS slides FROM exhibit_deck GROUP BY project, deck "
                                             "ORDER BY project, deck")},
        "density": dens,
    }


def _exhibits_live(st: Store, today: dt.date) -> dict:
    """Versions of the last 30 days and how sessions use the slides (fundus_exhibit calls, search hits, views)."""
    q = st.db.execute
    if not q("SELECT 1 FROM sqlite_master WHERE name = 'exhibit'").fetchone():
        return {}
    since = (today - dt.timedelta(days=29)).isoformat()
    week = (today - dt.timedelta(days=6)).isoformat()
    days = [(today - dt.timedelta(days=29 - i)).isoformat() for i in range(30)]
    per_day = dict(q("SELECT substr(created_at, 1, 10), count(*) FROM exhibit_version WHERE created_at >= ? GROUP BY 1",
                     (since,)).fetchall())
    calls = dict(q("SELECT day, calls FROM usage WHERE tool = 'fundus_exhibit' AND day >= ?", (since,)).fetchall())
    hits: dict[str, int] = {}
    kind_filter = 0
    for ts, filters, top in q("SELECT ts, filters, top_ids FROM search_log WHERE ts >= ?", (since,)):
        if top and '"exhibit:' in top:
            hits[ts[:10]] = hits.get(ts[:10], 0) + 1
        try:
            if (json.loads(filters or "{}").get("kind") or "") in ("exhibit", "slide", "chart", "poster", "section"):
                kind_filter += 1
        except ValueError:
            pass
    views = _rows(st, """SELECT v.exhibit_id AS id, sum(v.n) AS views, e.title, e.project, e.kind FROM exhibit_view v
                         JOIN exhibit e ON e.id = v.exhibit_id WHERE v.day >= ? GROUP BY v.exhibit_id ORDER BY views DESC LIMIT 5""",
                  since)
    return {
        "versions7d": q("SELECT count(*) FROM exhibit_version WHERE created_at >= ?", (week,)).fetchone()[0],
        "versions30d": sum(per_day.values()),
        "versionsByDay": [{"date": d, "n": per_day.get(d, 0)} for d in days],
        "usage": {"exhibitCallsByDay": [{"date": d, "calls": calls.get(d, 0)} for d in days],
                  "searchHitsByDay": [{"date": d, "searches": hits.get(d, 0)} for d in days],
                  "kindFilterSearches30d": kind_filter,
                  "views30d": q("SELECT coalesce(sum(n), 0) FROM exhibit_view WHERE day >= ?", (since,)).fetchone()[0],
                  "topViewed": [{**v, "title": (v["title"] or "")[:140]} for v in views]},
    }


def _activity(st: Store, today: dt.date) -> dict:
    """Last 30 days of imports, session writes and MCP tool calls (cheap with the changelog ts index)."""
    q = st.db.execute
    # ------------------------------------------------------------------------------------------------
    since = (today - dt.timedelta(days=29)).isoformat()
    days = {}
    for day, actor, action, n in q("""SELECT substr(ts, 1, 10), actor, action, count(*) FROM changelog
                                      WHERE ts >= ? GROUP BY 1, 2, 3""", (since,)):
        row = days.setdefault(day, {"date": day, "imports": 0, "sessions": 0, "maintenance": 0})
        cls = "maintenance" if (actor or "").startswith("importer:maintenance") else \
              "imports" if (actor or "").startswith("importer:") else "sessions"
        row[cls] += n
    usage = _rows(st, "SELECT day, tool, calls FROM usage WHERE day >= ? ORDER BY day", since) \
        if q("SELECT 1 FROM sqlite_master WHERE name = 'usage'").fetchone() else []
    usage_days, tools = {}, {}
    for u in usage:
        usage_days[u["day"]] = usage_days.get(u["day"], 0) + u["calls"]
        tools[u["tool"]] = tools.get(u["tool"], 0) + u["calls"]
    week = (today - dt.timedelta(days=6)).isoformat()
    activity = {
        "days": [days[k] for k in sorted(days)],
        "toolCallsByDay": [{"date": k, "calls": usage_days[k]} for k in sorted(usage_days)],
        "toolCalls30d": sorted(({"tool": k, "calls": v} for k, v in tools.items()), key=lambda x: -x["calls"]),
        "sessionWrites7d": q("SELECT count(*) FROM changelog WHERE ts >= ? AND actor = 'mcp' AND action IN ('insert', 'update', 'verify', 'resolve', 'check')",
                             (week,)).fetchone()[0],
        "sessionInserts30d": q("SELECT count(*) FROM changelog WHERE ts >= ? AND actor = 'mcp' AND action = 'insert'",
                               (since,)).fetchone()[0],
        "lastImport": q("SELECT max(ts) FROM changelog WHERE actor LIKE 'importer:%'").fetchone()[0],
        "lastSessionWrite": q("SELECT max(ts) FROM changelog WHERE actor = 'mcp'").fetchone()[0],
    }

    return activity


CACHE_KEY = "metrics:cache"


def _signature(st: Store, today: dt.date) -> str:
    """Changes whenever items, tags, origins or the changelog change, or the day changes."""
    q = st.db.execute
    parts = [today.isoformat()]
    for t in ("changelog", "origin", "tag", "observation", "forecast", "study", "quote", "practice", "fact",
              "series", "document", "publisher", "exhibit", "exhibit_version", "exhibit_deck"):
        parts.append(str(q(f"SELECT max(rowid) FROM {t}").fetchone()[0]))
    parts.append(str(q("SELECT count(*) || ':' || coalesce(sum(visibility = 'internal'), 0) || ':' || coalesce(sum(released), 0) "
                       "|| ':' || coalesce(sum(set_id * id), 0) FROM exhibit").fetchone()[0]))
    parts.append(str(q("SELECT count(*) || ':' || coalesce(max(updated_at), '') FROM exhibit_set").fetchone()[0]))
    parts.append(str(q("SELECT count(*) || ':' || coalesce(sum(length(density)), 0) FROM exhibit_version").fetchone()[0]))
    return "|".join(parts)


def compute(st: Store, today: dt.date | None = None, use_cache: bool = True) -> dict:
    """Statistics for the dashboard. The expensive part (full scans) is cached in meta and recomputed only when the
    data changed; activity (last 30 days) and the database size are always fresh."""
    today = today or dt.date.today()
    st.db.execute("CREATE INDEX IF NOT EXISTS ix_changelog_ts ON changelog(ts)")
    sig = _signature(st, today)
    cached = None
    if use_cache:
        r = st.db.execute("SELECT value FROM meta WHERE key = ?", (CACHE_KEY,)).fetchone()
        if r:
            try:
                c = json.loads(r[0])
                cached = c["data"] if c.get("sig") == sig else None
            except (ValueError, KeyError):
                cached = None
    if cached is None:
        cached = _compute_full(st, today)
        if use_cache:
            st.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                          (CACHE_KEY, json.dumps({"sig": sig, "data": cached}, default=str)))
            st.db.commit()
        cached["cache"] = "computed"
    else:
        cached["cache"] = "hit"
    out = dict(cached)
    out["activity"] = _activity(st, today)
    out["exhibits"] = {**(cached.get("exhibits") or {}), **_exhibits_live(st, today)}
    try:                                   # datasets (fundus.datasets): stock, descriptions, formats, use
        from fundus import datasets as _ds
        out["datasets"] = _ds.metrics(st)
    except Exception as e:
        out["datasets"] = {"error": f"{type(e).__name__}: {e}"}
    try:                                   # data hygiene (fundus.cleanup): score of the last analysis and its trend
        from fundus import cleanup
        c = cleanup.status(st)
        out["cleanup"] = {k: c.get(k) for k in ("score", "affected", "total", "classes", "auto_records", "open_decisions",
                                                 "rejected", "last_analysis", "trend", "runs", "running")}
    except Exception as e:
        out["cleanup"] = {"error": f"{type(e).__name__}: {e}"}
    out["generatedAt"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out["db"] = {"bytes": st.path.stat().st_size if st.path.exists() else None, "path": str(st.path)}
    return out


# ---------------------------------------------------------------- operation on the Spark
UNITS = ("fundus-mcp.service", "fundus-snapshot.timer", "fundus-import.timer", "fundus-embed.timer", "fundus-cleanup.timer")


def _systemctl(*args) -> str:
    if not shutil.which("systemctl"):       # macOS, Windows: the in-process scheduler runs the jobs
        return ""
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _unit_info(unit: str) -> dict:
    props = _systemctl("show", unit, "--property=ActiveState,SubState,Result,NextElapseUSecRealtime,"
                       "LastTriggerUSec,ActiveEnterTimestamp,ExecMainStatus")
    d = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
    return {"unit": unit, "active": d.get("ActiveState"), "sub": d.get("SubState"), "result": d.get("Result"),
            "next": d.get("NextElapseUSecRealtime") or None, "lastTrigger": d.get("LastTriggerUSec") or None,
            "since": d.get("ActiveEnterTimestamp") or None}


def _job_result(service: str) -> dict:
    d = dict(line.split("=", 1) for line in _systemctl("show", service, "--property=Result,ExecMainStatus,"
                                                       "ExecMainExitTimestamp").splitlines() if "=" in line)
    return {"result": d.get("Result"), "exitStatus": d.get("ExecMainStatus"), "finished": d.get("ExecMainExitTimestamp") or None}


def _scheduler_job(sched: dict, name: str) -> dict:
    """A job of the in-process scheduler in the shape of _job_result (systemd)."""
    j = next((j for j in sched.get("jobs", []) if j["name"] == name), {})
    return {"result": j.get("last_result"), "exitStatus": j.get("last_exit"), "finished": j.get("last_end")}


def health(st: Store, url: str | None = None, probe_mcp: bool = True) -> dict:
    """Operation on the Spark: MCP answers, timers, last jobs, snapshot. probe_mcp=False inside the server itself (it would
    have to call itself; that it answers the request is the probe)."""
    from fundus import config, scheduler
    url = url or config.public_url() + "/mcp"
    t0 = time.monotonic()
    probe = {"ok": False, "ms": None, "error": None}
    if not probe_mcp:
        probe = {"ok": True, "ms": 0, "error": None, "self": True}
    else:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fundus-health", "version": "1"}}}).encode()
            req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                     "Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = r.status == 200 and b"serverInfo" in r.read()
            probe = {"ok": ok, "ms": round((time.monotonic() - t0) * 1000), "error": None if ok else "unexpected answer"}
        except Exception as e:
            probe = {"ok": False, "ms": round((time.monotonic() - t0) * 1000), "error": str(e)[:160]}
    snap = st.path.parent / "backup" / "fundus.sqlite"
    snap_age = round((time.time() - snap.stat().st_mtime) / 3600, 1) if snap.exists() else None
    up_to_date = None
    try:   # snapshots are skipped while the content is unchanged, so age alone is no sign of staleness
        import sqlite3 as _sq
        from fundus.snapshot_job import signature
        sig_file = snap.with_suffix(".sig")
        if snap.exists() and sig_file.exists():
            con = _sq.connect(f"file:{st.path}?mode=ro", uri=True, timeout=10)
            try:
                up_to_date = signature(con) == sig_file.read_text()
            finally:
                con.close()
    except Exception:
        up_to_date = None
    sched = scheduler.status(st.path)
    systemd = bool(shutil.which("systemctl")) and not sched["enabled"]
    return {"mcp": {"url": url, **probe}, "units": [_unit_info(u) for u in UNITS] if systemd else [],
            "scheduler": sched,
            "lastImportJob": _job_result("fundus-import.service") if systemd else _scheduler_job(sched, "import"),
            "lastSnapshotJob": _job_result("fundus-snapshot.service") if systemd else _scheduler_job(sched, "snapshot"),
            "snapshot": {"path": str(snap), "ageHours": snap_age, "bytes": snap.stat().st_size if snap.exists() else None,
                         "upToDate": up_to_date},
            "repo": "https://github.com/falkue/fundus-app"}
