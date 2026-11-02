"""Read API over a Store: search, get, series, overview, update targets, vocabulary.

All functions take a `Store` as first argument and return plain JSON-serialisable dicts/lists, so the
CLI and the MCP server can hand them on unchanged.

Visibility: items with visibility 'internal' (own surveys, client data, interview notes) are hidden
unless `include_internal=True` or a `project` filter is given that matches one of the item's project
tags. A series counts as internal when it has observations and none of them is public. Exhibits (slides/charts,
fundus/exhibits.py) carry their own `project` and `visibility` (set by the sharing policy) and follow the same rule.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import namedtuple
from datetime import date, timedelta

from fundus import entities as ent
from fundus.store import ITEM_KINDS, Store

ALL_KINDS = ITEM_KINDS + ("series", "exhibit")
EXHIBIT_TYPES = ("slide", "chart", "poster", "section")      # kind='chart' etc. = exhibits of that type
EXHIBIT_STATUSES = ("draft", "final", "presented")
# Column holding the item's own date (what "since" compares against before the document date).
DATE_COL = {"observation": "period", "forecast": "made_on", "study": None, "quote": "said_on",
            "practice": "year", "fact": "date"}
# Column used as the one-line label of an item.
LABEL_COL = {"observation": "metric", "forecast": "statement", "study": "finding", "quote": "text",
             "practice": "what", "fact": "title", "series": "name", "exhibit": "title"}
CLIP = 300


# ------------------------------------------------------------------ small helpers
def clip(v, n: int = CLIP):
    if isinstance(v, str) and len(v) > n:
        return v[: n - 1].rstrip() + "…"
    return v


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _row(store: Store, kind: str, item_id) -> sqlite3.Row | None:
    key = str(item_id) if kind == "series" else int(item_id)
    return store.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (key,)).fetchone()


Period = namedtuple("Period", "gran start end last_start")
"""gran: day|month|quarter|half|year; start/end: first and last day; last_start: start of the final
sub-period (differs from start only for ranges such as '2026-2030')."""
GRAN_ORDER = {"day": 0, "month": 1, "quarter": 2, "half": 3, "year": 4}


def _month_end(y: int, m: int) -> date:
    return (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)


def _mk(gran: str, y: int, a: int = 1, d: int = 1) -> Period | None:
    try:
        if gran == "year":
            return Period("year", date(y, 1, 1), date(y, 12, 31), date(y, 1, 1))
        if gran == "half":
            st = date(y, 1 if a == 1 else 7, 1)
            return Period("half", st, _month_end(y, 6 if a == 1 else 12), st)
        if gran == "quarter":
            st = date(y, (a - 1) * 3 + 1, 1)
            return Period("quarter", st, _month_end(y, a * 3), st)
        if gran == "month":
            return Period("month", date(y, a, 1), _month_end(y, a), date(y, a, 1))
        dd = date(y, a, d)
        return Period("day", dd, dd, dd)
    except ValueError:
        return None


def _single(s: str) -> Period | None:
    s = s.strip()
    if m := re.fullmatch(r"(?:FY\s?)?(\d{4})", s, re.I):
        return _mk("year", int(m[1]))
    if m := re.fullmatch(r"FY\s?(\d{2})", s, re.I):
        return _mk("year", 2000 + int(m[1]))
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        return _mk("month", int(m[1]), int(m[2])) if 1 <= int(m[2]) <= 12 else None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        return _mk("day", int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{4})\s?-?\s?([QH])([1-4])", s, re.I):
        g = "quarter" if m[2].upper() == "Q" else "half"
        return _mk(g, int(m[1]), int(m[3])) if g == "quarter" or int(m[3]) <= 2 else None
    if m := re.fullmatch(r"([QH])([1-4])\s?[-/ ]?\s?(\d{4})", s, re.I):
        g = "quarter" if m[1].upper() == "Q" else "half"
        return _mk(g, int(m[3]), int(m[2])) if g == "quarter" or int(m[2]) <= 2 else None
    return None


def parse_period(p) -> Period | None:
    """Parse a period string. Single periods: '2026', 'FY2019', '2026-08', '2026-08-15', '2026-Q3', 'Q2 2025',
    '2025-H2', 'H1 2024'. Ranges resolve to their END part: '2026-2030' → 2030, '2025/26' → 2026,
    '2025-11/12' → 2025-12. Otherwise the last four-digit year in the text ('by 2030', 'mid-2027')."""
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    if r := _single(s):
        return r
    m = re.fullmatch(r"(?:FY\s?)?(\d{4})\s*[-–—/]\s*(\d{4})", s, re.I)
    if m:
        a, b = _mk("year", int(m[1])), _mk("year", int(m[2]))
        return Period("year", a.start, b.end, b.start) if a and b and b.end >= a.start else b
    m = re.fullmatch(r"(?:FY\s?)?(\d{4})\s*[-–—/]\s*(\d{2})", s, re.I)
    if m:   # '2025/26', '2026-30' (two-digit month forms were handled by _single)
        y = int(m[1])
        y2 = (y // 100) * 100 + int(m[2])
        if y2 > y:
            a, b = _mk("year", y), _mk("year", y2)
            return Period("year", a.start, b.end, b.start)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})\s*[-–—/]\s*(\d{1,2})", s)
    if m and 1 <= int(m[2]) <= 12 and 1 <= int(m[3]) <= 12:
        y, m1, m2 = int(m[1]), int(m[2]), int(m[3])
        a, b = _mk("month", y, m1), _mk("month", y + (m2 < m1), m2)
        return Period("month", a.start, b.end, b.start)
    parts = re.split(r"\s*(?:/|–|—|\bto\b|\s-\s)\s*", s)
    if len(parts) == 2 and (a := _single(parts[0])) and (b := _single(parts[1])) and b.end >= a.start:
        return Period(b.gran, a.start, b.end, b.last_start)
    years = re.findall(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", s)
    if years:
        a, b = _mk("year", int(years[0])), _mk("year", int(years[-1]))
        return Period("year", min(a.start, b.start), b.end, b.start)
    return None


def period_start(p) -> date | None:
    r = parse_period(p)
    return r.start if r else None


def period_end(p) -> date | None:
    r = parse_period(p)
    return r.end if r else None


def _period_key(p):
    """Sort key for period strings (unparseable last, then lexicographic)."""
    r = parse_period(p)
    return (r is None, r.start if r else date.max, r.end if r else date.max, str(p or ""))


def _sql_functions(store: Store) -> None:
    """Register fundus_period_end(text) → ISO end date on this connection (used by the 'since' filter)."""
    if getattr(store, "_query_fns", None) is store.db:
        return
    def pend(v):
        e = period_end(v)
        return e.isoformat() if e else None
    store.db.create_function("fundus_period_end", 1, pend, deterministic=True)
    store._query_fns = store.db


def descendants(store: Store, facet: str, value: str) -> list[str]:
    """The value plus all narrower values below it (vocab.parent), e.g. 'europe' → europe, dach, de, …"""
    rows = store.db.execute("SELECT value, parent FROM vocab WHERE facet = ?", (facet,)).fetchall()
    children: dict[str, list[str]] = {}
    for v, p in rows:
        if p:
            children.setdefault(p, []).append(v)
    out, todo = [], [value]
    while todo:
        v = todo.pop()
        if v not in out:
            out.append(v)
            todo += children.get(v, [])
    return out


def tags_of(store: Store, kind: str, item_id) -> dict:
    out: dict[str, list[str]] = {}
    for f, v in store.db.execute("SELECT facet, value FROM tag WHERE item_kind = ? AND item_id = ? ORDER BY facet, value",
                                 (kind, str(item_id))):
        out.setdefault(f, []).append(v)
    return out


def doc_brief(store: Store, document_id) -> dict | None:
    if not document_id:
        return None
    r = store.db.execute("SELECT d.title, d.url, d.published, d.kind, d.journal, p.name AS publisher FROM document d "
                         "LEFT JOIN publisher p ON p.id = d.publisher_id WHERE d.id = ?", (document_id,)).fetchone()
    if r is None:
        return None
    return _compact({"publisher": r["publisher"], "journal": r["journal"], "title": clip(r["title"], 160), "url": r["url"],
                     "published": r["published"], "doc_id": document_id})


def _in_project(store: Store, kind: str, item_id, project) -> bool:
    if not project:
        return False
    if kind == "exhibit":
        return bool(store.db.execute("SELECT 1 FROM exhibit WHERE id = ? AND project = ?", (int(item_id), project)).fetchone())
    if store.db.execute("SELECT 1 FROM tag WHERE item_kind = ? AND item_id = ? AND facet = 'project' AND value = ?",
                        (kind, str(item_id), project)).fetchone():
        return True
    if kind == "series":   # a series belongs to every project its observations come from
        return bool(store.db.execute(
            "SELECT 1 FROM observation o JOIN tag t ON t.item_kind = 'observation' AND t.item_id = CAST(o.id AS TEXT) "
            "WHERE o.series_id = ? AND t.facet = 'project' AND t.value = ?", (item_id, project)).fetchone())
    return False


def is_visible(store: Store, kind: str, row, include_internal=False, project=None) -> bool:
    if include_internal:
        return True
    if kind == "series":
        n, n_public = store.db.execute("SELECT count(*), coalesce(sum(visibility = 'public'), 0) FROM observation "
                                       "WHERE series_id = ?", (row["id"],)).fetchone()
        internal = n > 0 and n_public == 0
    else:
        internal = "visibility" in row.keys() and row["visibility"] == "internal"
    return not internal or _in_project(store, kind, row["id"], project)


# ------------------------------------------------------------------ SQL filter builder
def vis_sql(kind: str, alias: str, include_internal=False, project=None) -> tuple[str, list]:
    """' AND …' clause keeping public rows, plus internal rows tagged with `project` (never 'no filter'
    just because a project string was given)."""
    trash = f" AND {alias}.project != '_trash'" if kind == "exhibit" else ""   # slides in the trash (fundus.trash)
    if include_internal:
        return trash, []
    base = f"{alias}.visibility != 'internal'"
    if project and kind == "exhibit":   # exhibits carry their project in a column
        return f"{trash} AND ({base} OR {alias}.project = ?)", [project]
    if project:
        return (f" AND ({base} OR EXISTS (SELECT 1 FROM tag tp WHERE tp.item_kind = '{kind}' "
                f"AND tp.item_id = CAST({alias}.id AS TEXT) AND tp.facet = 'project' AND tp.value = ?))", [project])
    return f"{trash} AND {base}", []


def _series_vis(include_internal=False, project=None) -> tuple[str | None, list]:
    """Series is visible unless it has observations and none of them is visible to the caller."""
    if include_internal:
        return None, []
    ov, op = vis_sql("observation", "o", False, project)
    sql = (f"NOT (EXISTS (SELECT 1 FROM observation o WHERE o.series_id = x.id) AND NOT EXISTS "
           f"(SELECT 1 FROM observation o WHERE o.series_id = x.id{ov}))")
    if project:
        sql = (f"({sql} OR EXISTS (SELECT 1 FROM tag ts WHERE ts.item_kind = 'series' AND ts.item_id = x.id "
               f"AND ts.facet = 'project' AND ts.value = ?))")
        op = [*op, project]
    return sql, op


def _tag_exists(kind: str, facet: str, values: list[str]) -> tuple[str, list]:
    marks = ",".join("?" * len(values))
    own = (f"EXISTS (SELECT 1 FROM tag t WHERE t.item_kind = '{kind}' AND t.item_id = CAST(x.id AS TEXT) "
           f"AND t.facet = ? AND t.value IN ({marks}))")
    params = [facet, *values]
    if kind == "series":   # a series also matches through the tags of its observations
        own = (f"({own} OR EXISTS (SELECT 1 FROM observation o JOIN tag t ON t.item_kind = 'observation' "
               f"AND t.item_id = CAST(o.id AS TEXT) WHERE o.series_id = x.id AND t.facet = ? AND t.value IN ({marks})))")
        params += [facet, *values]
    return own, params


def _filters(store: Store, kind: str, industry=None, topic=None, region=None, since=None, status=None,
             include_internal=False, project=None, visible_project=None, entity_ids=None,
             exhibit_kind=None) -> tuple[list[str], list]:
    """WHERE parts for table alias x. `project` filters to items of that project (and makes its internal
    items visible); `visible_project` only makes that project's internal items visible; `entity_ids` keeps items
    linked to one of these entities (any role); `exhibit_kind` keeps exhibits of one type (slide/chart/poster/section)."""
    where, params = [], []
    if entity_ids is not None:
        marks = ",".join("?" * len(entity_ids)) or "NULL"
        where.append(f"EXISTS (SELECT 1 FROM entity_link el WHERE el.entity_id IN ({marks}) AND el.item_kind = '{kind}' "
                     f"AND el.item_id = CAST(x.id AS TEXT))")
        params += list(entity_ids)
    if kind == "exhibit":
        return _exhibit_filters(store, where, params, industry or topic or region, since, status, include_internal,
                                project, visible_project, exhibit_kind)
    for facet, value in (("industry", industry), ("topic", topic), ("region", region)):
        if value:
            vals: list[str] = []
            for v in [value] if isinstance(value, str) else value:
                vals += descendants(store, facet, v)
            sql, p = _tag_exists(kind, facet, vals)
            where.append(sql)
            params += p
    if project:
        sql, p = _tag_exists(kind, "project", [project])
        where.append(sql)
        params += p
    elif not include_internal:
        if kind == "series":
            sql, p = _series_vis(False, visible_project)
        else:
            sql, p = vis_sql(kind, "x", False, visible_project)
            sql = sql.removeprefix(" AND ")
        where.append(sql)
        params += p
    if status and kind != "series":
        where.append("x.status = ?")
        params.append(status)
    if since:   # compare parsed period ends, not strings ('FY2019', 'Q2 2025' …)
        start = period_start(since)
        if start is None:
            raise ValueError(f"cannot parse since '{since}' (use e.g. 2024, 2024-06, 2024-Q2)")
        _sql_functions(store)
        if kind == "series":
            ov, op = vis_sql("observation", "o", include_internal, project or visible_project)
            where.append(f"(SELECT max(fundus_period_end(o.period)) FROM observation o WHERE o.series_id = x.id{ov}) >= ?")
            params += op
        else:
            col = DATE_COL.get(kind)
            own = f"fundus_period_end(x.{col}), " if col else ""
            where.append(f"coalesce({own}fundus_period_end((SELECT published FROM document WHERE id = x.document_id)), "
                         f"substr(x.created_at, 1, 10)) >= ?")
        params.append(start.isoformat())
    return where, params


def _exhibit_filters(store, where, params, tagged, since, status, include_internal, project, visible_project,
                     exhibit_kind) -> tuple[list[str], list]:
    """Exhibits have no industry/topic/region tags (such filters exclude them); project and visibility are columns."""
    if tagged:
        where.append("0")
    if exhibit_kind:
        where.append("x.kind = ?")
        params.append(exhibit_kind)
    if project:
        where.append("x.project = ?")
        params.append(project)
    elif not include_internal:
        sql, p = vis_sql("exhibit", "x", False, visible_project)
        where.append(sql.removeprefix(" AND "))
        params += p
    if status:
        if status in EXHIBIT_STATUSES:
            where.append("x.status = ?")
            params.append(status)
        else:
            where.append("0")
    if since:
        start = period_start(since)
        if start is None:
            raise ValueError(f"cannot parse since '{since}' (use e.g. 2024, 2024-06, 2024-Q2)")
        where.append("substr(coalesce(x.updated_at, x.created_at), 1, 10) >= ?")
        params.append(start.isoformat())
    return where, params


def _kinds(kind) -> tuple[str, ...]:
    if not kind:
        return ALL_KINDS
    ks = (kind,) if isinstance(kind, str) else tuple(kind)
    for k in ks:
        if k not in ALL_KINDS:
            raise ValueError(f"unknown kind '{k}' (one of {', '.join(ALL_KINDS + EXHIBIT_TYPES)})")
    return ks


# ------------------------------------------------------------------ item summaries
def series_stats(store: Store, series_id: str, include_internal=False, project=None) -> dict:
    vis, vp = vis_sql("observation", "o", include_internal, project)
    r = store.db.execute(f"SELECT count(*), count(DISTINCT period), min(period), max(period) FROM observation o "
                         f"WHERE o.series_id = ?{vis}", (series_id, *vp)).fetchone()
    periods = [p for (p,) in store.db.execute(f"SELECT DISTINCT period FROM observation o WHERE o.series_id = ? "
                                               f"AND o.period IS NOT NULL{vis}", (series_id, *vp))]
    periods.sort(key=_period_key)
    return {"n_obs": r[0], "n_periods": r[1], "first_period": periods[0] if periods else r[2],
            "last_period": periods[-1] if periods else r[3]}


def summary(store: Store, kind: str, row, include_internal=False, project=None, with_tags=True) -> dict:
    """Compact one-item view used in search results and overviews."""
    s: dict = {"kind": kind, "id": row["id"]}
    if kind == "exhibit":
        from fundus import exhibits as exm
        s.update(exm.summary_fields(store, row))
        return _compact(s)
    if kind == "observation":
        s.update(metric=clip(row["metric"], 200), category=clip(row["category"], 120), value=row["value"],
                 value_text=row["value_text"], unit=row["unit"], period=row["period"], geo=row["geo"],
                 n=row["n"], series_id=row["series_id"])
    elif kind == "forecast":
        s.update(statement=clip(row["statement"]), value=row["value"], value_low=row["value_low"],
                 value_high=row["value_high"], unit=row["unit"], target_period=row["target_period"],
                 made_on=row["made_on"], forecaster=row["forecaster"], outcome=row["outcome"],
                 series_id=row["series_id"])
    elif kind == "study":
        s.update(finding=clip(row["finding"]), design=row["design"], n=row["n"], region=row["region"],
                 sector=row["sector"])
    elif kind == "quote":
        s.update(text=clip(row["text"]), speaker=row["speaker"], role=clip(row["role"], 80),
                 organisation=row["organisation"], said_on=row["said_on"])
    elif kind == "practice":
        s.update(organisation=row["organisation"], what=clip(row["what"]), year=row["year"],
                 result=clip(row["result"], 160))
    elif kind == "fact":
        s.update(fact_kind=row["kind"], title=clip(row["title"], 160), what=clip(row["what"], 200), date=row["date"])
    elif kind == "series":
        pub = store.db.execute("SELECT name FROM publisher WHERE id = ?", (row["publisher_id"],)).fetchone()
        s.update(name=clip(row["name"], 200), unit=row["unit"], publisher=pub[0] if pub else None,
                 frequency=row["frequency"], next_expected=row["next_expected"],
                 **series_stats(store, row["id"], include_internal, project))
    if kind != "series":
        s["status"] = row["status"]
        if row["visibility"] == "internal":
            s["visibility"] = "internal"
        s["source"] = doc_brief(store, row["document_id"])
    if with_tags:
        t = tags_of(store, kind, row["id"])
        s["tags"] = t
    return _compact(s)


# ------------------------------------------------------------------ search
_ADVANCED = re.compile(r'"|\bAND\b|\bOR\b|\bNOT\b|\bNEAR\b|\*')


def fts_query(text: str, mode: str = "all") -> str:
    """Turn free text into a safe FTS5 query: every word quoted, prefix-matched from 3 letters on,
    joined by AND (mode 'all') or OR (mode 'any')."""
    tokens = re.findall(r"\w+", text or "", re.UNICODE)
    terms = [f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in tokens]
    return (" OR " if mode == "any" else " ").join(terms)


def _search_kind(store, kind, match, filters, params, limit):
    idcol = "x.id = s.item_id" if kind == "series" else "x.id = CAST(s.item_id AS INTEGER)"
    where = " AND ".join(["search_index MATCH ?", "s.item_kind = ?", *filters])
    sql = (f"SELECT x.*, bm25(search_index) AS _rank FROM search_index s JOIN {kind} x ON {idcol} "
           f"WHERE {where} ORDER BY _rank LIMIT ?")
    return store.db.execute(sql, [match, kind, *params, limit]).fetchall()


SEARCH_MODES = ("hybrid", "lexical", "semantic")
RRF_K = 60                  # Reciprocal Rank Fusion constant
SEM_TIMEOUT = 4.0           # seconds for the query embedding before falling back to lexical
SEM_POOL = 100              # candidates per ranking that enter the fusion
SEM_MIN_SCORE = 0.0         # cosine floor for semantic candidates
LEX_ANY_WEIGHT = 0.1        # RRF weight of the lexical ranking when it only matched ANY word (all words: 1.0).
                            # Eval set (40 queries, 37 fall back to any-word): nDCG@10 0.45 at weight 1.0 → 0.64
                            # at 0.1; such hits then mainly break ties among semantic candidates.
EXACT_MAX = 2000            # filtered candidate sets up to this size are scored exactly (no coarse index)
COARSE_K = 400              # first coarse (binary) KNN size; grows ×4 up to COARSE_K_MAX when filters drop hits.
                            # Measured on 18k items: recall vs exact 1.00 @10, 0.96 @100 (k=200: 1.00 / 0.89).
COARSE_K_MAX = 4096
ENT_WEIGHT = 3.0            # RRF weight of the entity ranking (items linked to entities named in the query).
                            # Eval set (frozen copy 2026-10-24, 7 organisation queries), nDCG@10 organisation with
                            # floor 0.25 / bonus as below: w 1 → 0.785, 2 → 0.795, 3 → 0.811 (0.725 without entities);
                            # other intents unchanged at every weight.
ENT_CAP = 3000              # linked items scored per query (strongest roles first)
ENT_REL_FLOOR = 0.25        # linked items whose cosine is more than this below the best semantic hit stay out
                            # (organisation nDCG: none 0.785, 0.3 0.811, 0.25 0.811, 0.2 0.819, 0.15 0.769, 0.1 0.742)
# Added to the cosine similarity of a linked item: an item about/by the entity beats one that only mentions it.
# Mentions by prominence (entities.links_for): in the headline of the main text, elsewhere in it, only in secondary
# fields (notes, results, lists of surveyed organisations).
ROLE_BONUS = {"subject": 0.12, "organisation": 0.12, "speaker": 0.12, "mentioned_head": 0.12, "publisher": 0.08,
              "via": 0.04, "mentioned": 0.0, "mentioned_minor": -0.10}


class SemanticSkip(Exception):
    """Semantic search not possible for this call; the reason goes to `fallback`."""


def _lexical_hits(store, text, kinds, fkw, per_kind, match):
    """bm25 hits [(rank, kind, row)] sorted best first, plus the match mode that produced them."""
    candidates = []
    if _ADVANCED.search(text):
        candidates.append(("raw", text))
    candidates += [(mode, fts_query(text, mode)) for mode in ("all", "any") if match == "auto" or match == mode]
    for mode, fq in candidates:
        if not fq or (mode == "any" and match == "auto" and len(re.findall(r"\w+", text)) < 2):
            continue
        hits = []
        try:
            for k in kinds:
                f, p = _filters(store, k, **fkw)
                hits += [(r["_rank"], k, r) for r in _search_kind(store, k, fq, f, p, per_kind)]
        except sqlite3.OperationalError:
            continue
        if hits:
            hits.sort(key=lambda h: h[0])
            return hits, mode
    return [], None


def _allowed_count(store, kinds, fkw, cap) -> int:
    """Number of items passing the filters, counted only up to cap + 1."""
    n = 0
    for k in kinds:
        f, p = _filters(store, k, **fkw)
        where = (" WHERE " + " AND ".join(f)) if f else ""
        n += store.db.execute(f"SELECT count(*) FROM (SELECT 1 FROM {k} x{where} LIMIT ?)", [*p, cap + 1 - n]).fetchone()[0]
        if n > cap:
            break
    return n


def _exact_candidates(store, space, qv, kinds, fkw, n):
    """Small filtered set: score every allowed item (SQL filters joined with its stored vector)."""
    from fundus import embed
    scored = []
    for k in kinds:
        f, p = _filters(store, k, **fkw)
        where = (" WHERE " + " AND ".join(f)) if f else ""
        rows = store.db.execute(
            f"SELECT x.id, e.vector FROM {k} x CROSS JOIN embedding e ON e.item_kind = '{k}' AND e.item_id = CAST(x.id AS TEXT) "
            f"AND e.model = ? AND e.dim = ? AND e.quant = ?{where}", [space.model, space.dim, space.quant, *p]).fetchall()
        if rows:
            sc = embed.decode([r[1] for r in rows], space.quant, space.dim) @ qv
            scored += [(float(s), k, str(r[0])) for s, r in zip(sc, rows)]
    scored.sort(key=lambda t: -t[0])
    return scored[:n]


def _coarse_candidates(store, space, qv, kinds, fkw, n):
    """Large set: coarse KNN on the binary vec0 index (kind/visibility as metadata filters), SQL post-filter
    for everything else, exact rescoring of the survivors; widens k while filters leave too few."""
    from fundus import embed
    embed.load_vec(store)
    qbits = embed.bits(qv[None, :])[0]
    meta, mp = "", []
    if set(kinds) != set(ALL_KINDS):
        meta += f" AND kind IN ({','.join('?' * len(kinds))})"
        mp += list(kinds)
    if not (fkw.get("include_internal") or fkw.get("project") or fkw.get("visible_project")):
        meta += " AND internal = 0"
    k0 = max(COARSE_K, n)
    while True:
        k0 = min(k0, COARSE_K_MAX)
        knn = store.db.execute(f"SELECT rowid FROM {space.vec_table} WHERE embedding MATCH vec_bit(?) AND k = ?{meta}",
                               [qbits, k0, *mp]).fetchall()
        ids = [r[0] for r in knn]
        emb_rows = []
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            emb_rows += store.db.execute(f"SELECT item_kind, item_id, vector FROM embedding WHERE id IN "
                                         f"({','.join('?' * len(part))})", part).fetchall()
        ok: set[tuple[str, str]] = set()
        for k in kinds:
            kid = [r[1] for r in emb_rows if r[0] == k]
            if not kid:
                continue
            f, p = _filters(store, k, **fkw)
            keys = kid if k == "series" else [int(i) for i in kid]
            for i in range(0, len(keys), 900):
                part = keys[i:i + 900]
                ok |= {(k, str(r[0])) for r in store.db.execute(
                    f"SELECT x.id FROM {k} x WHERE x.id IN ({','.join('?' * len(part))})" + "".join(f" AND {w}" for w in f),
                    [*part, *p])}
        surv = [r for r in emb_rows if (r[0], r[1]) in ok]
        if len(surv) >= n or len(knn) < k0 or k0 >= COARSE_K_MAX:
            break
        k0 *= 4
    if not surv:
        return []
    sc = embed.decode([r[2] for r in surv], space.quant, space.dim) @ qv
    scored = sorted(((float(s), r[0], r[1]) for s, r in zip(sc, surv)), key=lambda t: -t[0])
    return scored[:n]


def _semantic_hits(store, text, kinds, fkw, n, embedder=None, space=None, qv_out: dict | None = None):
    """Nearest items by cosine similarity [(score, kind, row)], best first. Only items passing the same SQL
    filters (incl. visibility) as the lexical path can be returned; rows are re-checked when fetched."""
    from fundus import embed
    space = space or embed.active_space(store)
    if space is None or not store.db.execute("SELECT 1 FROM embedding WHERE model = ? AND dim = ? AND quant = ? LIMIT 1",
                                             (space.model, space.dim, space.quant)).fetchone():
        raise SemanticSkip("no_embeddings")
    emb = embedder or embed.embedder_for(space)
    t0 = time.monotonic()
    try:
        qv = emb.embed_query(text, timeout=SEM_TIMEOUT)
    except Exception as e:   # Ollama down, capacity rule, socket timeout …
        msg = str(e)
        raise SemanticSkip("timeout" if "timed out" in msg.lower() else f"error: {msg[:160]}") from e
    if time.monotonic() - t0 > SEM_TIMEOUT:
        raise SemanticSkip("timeout")
    qv = embed.normalise(qv, space.dim)[0]
    if qv_out is not None:
        qv_out.update(qv=qv, space=space)
    if _allowed_count(store, kinds, fkw, EXACT_MAX) <= EXACT_MAX:
        picked = _exact_candidates(store, space, qv, kinds, fkw, n)
    else:
        picked = _coarse_candidates(store, space, qv, kinds, fkw, n)
    picked = [t for t in picked if t[0] >= SEM_MIN_SCORE]
    rows: dict[tuple[str, str], sqlite3.Row] = {}
    for k in {k for _, k, _ in picked}:
        ids = [i for _, kk, i in picked if kk == k]
        f, p = _filters(store, k, **fkw)
        keys = ids if k == "series" else [int(i) for i in ids]
        for r in store.db.execute(f"SELECT x.* FROM {k} x WHERE x.id IN ({','.join('?' * len(keys))})"
                                  + "".join(f" AND {w}" for w in f), [*keys, *p]):
            rows[(k, str(r["id"]))] = r
    return [(s, k, rows[(k, i)]) for s, k, i in picked if (k, i) in rows]


def detect_entities(store: Store, text: str) -> list[int]:
    """Entities named in a query (alias n-grams on the normalised query; abbreviations in upper case), plus their
    direct children ('McKinsey' → McKinsey Global Institute)."""
    try:
        found = list(dict.fromkeys(e for e, _ in ent.resolver(store).mentions(text, query=True)))
    except sqlite3.Error:
        return []
    if found:
        marks = ",".join("?" * len(found))
        found += [r[0] for r in store.db.execute(f"SELECT id FROM entity WHERE parent_id IN ({marks}) AND kind != 'person'",
                                                 found) if r[0] not in found]
    return found


def _role_case(alias: str = "l") -> str:
    b = ROLE_BONUS
    plain = " ".join(f"WHEN {alias}.role = '{r}' THEN {v}" for r, v in b.items() if not r.startswith("mentioned"))
    return (f"CASE {plain} WHEN {alias}.role = 'mentioned' THEN (CASE WHEN {alias}.confidence >= 0.85 THEN "
            f"{b['mentioned_head']} WHEN {alias}.confidence >= 0.65 THEN {b['mentioned']} ELSE {b['mentioned_minor']} END) "
            f"ELSE 0 END")


def _entity_hits(store, qv, space, entity_ids, kinds, fkw, n, top_cos: float | None = None):
    """Items linked to the entities, ranked by cosine similarity to the query plus a role bonus
    [(score, kind, row)]. Same SQL filters (incl. visibility) as the other rankings."""
    from fundus import embed
    marks = ",".join("?" * len(entity_ids))
    scored = []
    for k in kinds:
        f, p = _filters(store, k, **fkw)
        join = "x.id = l.item_id" if k == "series" else "x.id = CAST(l.item_id AS INTEGER)"
        rows = store.db.execute(
            f"SELECT x.id, max({_role_case()}) AS b, e.vector FROM entity_link l CROSS JOIN {k} x ON {join} "
            f"CROSS JOIN embedding e ON e.item_kind = '{k}' AND e.item_id = l.item_id AND e.model = ? AND e.dim = ? "
            f"AND e.quant = ? WHERE l.entity_id IN ({marks}) AND l.item_kind = '{k}'" + "".join(f" AND {w}" for w in f)
            + " GROUP BY x.id ORDER BY b DESC LIMIT ?",
            [space.model, space.dim, space.quant, *entity_ids, *p, ENT_CAP]).fetchall()
        if rows:
            sc = embed.decode([r[2] for r in rows], space.quant, space.dim) @ qv
            floor = top_cos - ENT_REL_FLOOR if (ENT_REL_FLOOR is not None and top_cos is not None) else -9.0
            scored += [(float(s) + r[1], k, str(r[0])) for s, r in zip(sc, rows) if float(s) >= floor]
    scored.sort(key=lambda t: -t[0])
    scored = scored[:n]
    rows: dict[tuple[str, str], sqlite3.Row] = {}
    for k in {k for _, k, _ in scored}:
        ids = [i for _, kk, i in scored if kk == k]
        keys = ids if k == "series" else [int(i) for i in ids]
        for r in store.db.execute(f"SELECT x.* FROM {k} x WHERE x.id IN ({','.join('?' * len(keys))})", keys):
            rows[(k, str(r["id"]))] = r
    return [(s, k, rows[(k, i)]) for s, k, i in scored if (k, i) in rows]


def entity_names(store: Store, kind: str, item_id, limit: int = 4) -> list[str]:
    """Names of the entities linked to an item, strongest role first (compact `entities` field of results)."""
    rows = store.db.execute("SELECT e.name, l.role FROM entity_link l JOIN entity e ON e.id = l.entity_id "
                            "WHERE l.item_kind = ? AND l.item_id = ?", (kind, str(item_id))).fetchall()
    rows = sorted(rows, key=lambda r: ent.ROLE_RANK.get(r[1], 9))
    return list(dict.fromkeys(r[0] for r in rows))[:limit]


def resolve_entity(store: Store, name: str) -> list[int]:
    """Entity ids for the `entity=` filter (name or alias; children included); ValueError if unknown."""
    ids = ent.find(store, name)
    if not ids:
        raise ValueError(f"unknown entity '{name}' (try fundus_entity or a different spelling)")
    marks = ",".join("?" * len(ids))
    return ids + [r[0] for r in store.db.execute(f"SELECT id FROM entity WHERE parent_id IN ({marks}) AND kind != 'person'",
                                                 ids) if r[0] not in ids]


def rrf(rankings: list[list], k: int = RRF_K, weights: list[float] | None = None) -> list[tuple[object, float]]:
    """(Weighted) Reciprocal Rank Fusion: score(d) = sum over rankings of w / (k + rank), rank starting at 1.
    Returns [(key, score)] best first; ties keep the order of first appearance (earlier rankings first)."""
    score: dict = {}
    first: dict = {}
    weights = weights or [1.0] * len(rankings)
    for ri, (ranking, w) in enumerate(zip(rankings, weights)):
        for rank, key in enumerate(ranking, 1):
            score[key] = score.get(key, 0.0) + w / (k + rank)
            first.setdefault(key, (rank, ri))
    return sorted(score.items(), key=lambda t: (-t[1], first[t[0]]))


def search(store: Store, text: str | None = None, kind=None, industry=None, topic=None, region=None,
           since=None, status=None, limit: int = 20, include_internal: bool = False, project=None,
           match: str = "auto", visible_project=None, mode: str = "hybrid", embedder=None, space=None,
           entity=None, entities: bool = True) -> dict:
    """Search over all items and series, with tag/status/date filters. Tag filters include narrower
    vocabulary values ('automotive' also matches 'automotive-retail'). Without text it lists the newest
    matching items (in every mode).

    mode: 'lexical' = full-text (bm25); match: 'auto' (all words, falling back to any word), 'all', 'any'.
          'semantic' = embedding similarity (multilingual: German queries find English items, synonyms).
          'hybrid' (default) = both rankings fused with Reciprocal Rank Fusion (k=60); a lexical ranking that
          only matched ANY word (not all) enters with weight LEX_ANY_WEIGHT.
    If semantic search is not possible (no vectors, Ollama down/busy, query embedding > 4 s) the lexical
    result is returned and `fallback` says why. All filters and visibility rules apply to both paths.
    `project` filters to one project; `visible_project` only unhides that project's internal items.
    `space` (embed.Space) overrides the active embedding space (evaluation of alternative spaces).
    Entities: in hybrid mode, organisations/people/products named in the query (aliases: 'Unispital Zürich' = 'USZ' =
    University Hospital Zurich) add a third ranking – the items linked to them, by similarity to the query plus a role
    bonus (weight ENT_WEIGHT; `entities=False` switches it off). `entity` filters every mode to items linked to that
    entity (name or alias). Each result carries the names of its linked entities.
    Exhibits (slides/charts) are searched with everything else; kind='exhibit' searches only them, kind='slide' |
    'chart' | 'poster' | 'section' only exhibits of that type."""
    exhibit_kind = None
    if isinstance(kind, str) and kind in EXHIBIT_TYPES:
        kind, exhibit_kind = "exhibit", kind
    kinds = _kinds(kind)
    mode = mode or "hybrid"
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown mode '{mode}' (one of {', '.join(SEARCH_MODES)})")
    grant = project or visible_project
    fkw = dict(industry=industry, topic=topic, region=region, since=since, status=status,
               include_internal=include_internal, project=project, visible_project=visible_project)
    if exhibit_kind:
        fkw["exhibit_kind"] = exhibit_kind
    if entity:
        fkw["entity_ids"] = resolve_entity(store, entity)
    limit = max(1, min(int(limit or 20), 200))
    if status:
        kinds = tuple(k for k in kinds if k != "series" and (k != "exhibit" or status in EXHIBIT_STATUSES))
        if not kinds:
            raise ValueError(f"status '{status}' does not apply to kind {kind}")
    text = (text or "").strip()
    fallback = None
    effective = mode
    detected: list[str] = []
    if not text:
        hits = []
        for k in kinds:
            f, p = _filters(store, k, **fkw)
            where = (" WHERE " + " AND ".join(f)) if f else ""
            rows = store.db.execute(f"SELECT x.* FROM {k} x{where} ORDER BY x.created_at DESC, x.rowid DESC LIMIT ?",
                                    [*p, limit + 1]).fetchall()
            hits += [(r["created_at"], k, r) for r in rows]
        hits.sort(key=lambda h: h[0], reverse=True)
        ranked = [(k, r) for _, k, r in hits]
        match_mode, effective = "newest", None
    else:
        sem = None
        qinfo: dict = {}
        if mode in ("hybrid", "semantic"):
            try:
                sem = _semantic_hits(store, text, kinds, fkw, max(SEM_POOL, limit + 1), embedder, space, qinfo)
            except SemanticSkip as e:
                fallback, effective = str(e), "lexical"
        if effective == "semantic":
            ranked = [(k, r) for _, k, r in sem]
            match_mode = "semantic"
        elif effective == "lexical":
            hits, match_mode = _lexical_hits(store, text, kinds, fkw, limit + 1, match)
            ranked = [(k, r) for _, k, r in hits]
        else:   # hybrid
            lex, match_mode = _lexical_hits(store, text, kinds, fkw, max(SEM_POOL, limit + 1), match)
            lex = lex[:SEM_POOL]
            rows = {(k, str(r["id"])): r for _, k, r in sem}
            rows.update({(k, str(r["id"])): r for _, k, r in lex})
            rankings = [[(k, str(r["id"])) for _, k, r in lex], [(k, str(r["id"])) for _, k, r in sem]]
            weights = [LEX_ANY_WEIGHT if match_mode == "any" else 1.0, 1.0]
            ents = detect_entities(store, text) if entities and "qv" in qinfo else []
            if ents:
                eh = _entity_hits(store, qinfo["qv"], qinfo["space"], ents, kinds, fkw, max(SEM_POOL, limit + 1),
                                  top_cos=sem[0][0] if sem else None)
                if eh:
                    rows.update({(k, str(r["id"])): r for _, k, r in eh})
                    rankings.append([(k, str(r["id"])) for _, k, r in eh])
                    weights.append(ENT_WEIGHT)
                    hit_ents = {e for e, in store.db.execute(
                        f"SELECT DISTINCT entity_id FROM entity_link WHERE entity_id IN ({','.join('?' * len(ents))}) "
                        f"AND (item_kind || ':' || item_id) IN ({','.join('?' * len(eh))})",
                        [*ents, *[f"{k}:{r['id']}" for _, k, r in eh]])}
                    detected = [store.db.execute("SELECT name FROM entity WHERE id = ?", (e,)).fetchone()[0]
                                for e in ents if e in hit_ents]
            fused = rrf(rankings, weights=weights)
            ranked = [(key[0], rows[key]) for key, _ in fused]
            match_mode = match_mode or "semantic"
    results = [summary(store, k, r, include_internal, grant) for k, r in ranked[:limit]]
    for res in results:
        names = entity_names(store, res["kind"], res["id"])
        if names:
            res["entities"] = names
    out = {"query": text or None, "mode": effective, "match": match_mode, "count": len(results), "results": results}
    if detected:
        out["entities_detected"] = detected[:5]
    if entity:
        out["entity"] = entity
    if fallback:
        out["fallback"] = fallback
    if len(ranked) > limit:
        out["more"] = True
    if text and match_mode == "any" and match == "auto" and effective == "lexical":
        out["note"] = "no item matched all words; showing items matching any word"
    return _compact(out) | {"count": len(results), "results": results}


def search_logged(store: Store, actor: str, text: str = "", mode: str = "hybrid", **filters) -> dict:
    """search() plus one search_log row – used by the MCP tool fundus_search and `fundus search`."""
    t0 = time.perf_counter()
    res = search(store, text, mode=mode, **filters)
    log_search(store, actor, text, mode, filters, res, (time.perf_counter() - t0) * 1000)
    return res


def log_search(store: Store, actor: str, text, mode, filters: dict, result: dict, ms: float) -> None:
    """One search_log row per search call (MCP tool and CLI); never lets logging break a search."""
    try:
        res = result.get("results") or []
        store.db.execute(
            "INSERT INTO search_log(actor, query, mode, filters, n_results, top_ids, ms, fallback) VALUES (?,?,?,?,?,?,?,?)",
            (actor, (text or "").strip(), mode, json.dumps({k: v for k, v in filters.items() if v not in (None, "", False, [], {})},
                                                  ensure_ascii=False, default=str),
             result.get("count", len(res)), json.dumps([f"{r['kind']}:{r['id']}" for r in res[:10]]),
             round(ms, 1), result.get("fallback")))
        store.db.commit()
    except (sqlite3.Error, KeyError, TypeError):
        pass


# ------------------------------------------------------------------ get
def get(store: Store, kind: str, item_id, include_internal: bool = False, project=None, max_text: int = 2000,
        with_related: bool = True) -> dict:
    """One item with document, publisher, tags, origin and recent change history. For observations the
    series (metadata + all values); for series see `series()`. `related`: linked entities with roles, for
    observations of a series the latest period and revisions by newer editions, and up to 5 items about the same
    main entity."""
    if kind in ITEM_KINDS + ("series", "document") and str(item_id) != store.follow(kind, item_id) \
            and _row(store, kind, item_id) is None:      # merged by the clean-up: show where it lives on
        out = get(store, kind, store.follow(kind, item_id), include_internal, project, max_text, with_related)
        out["redirected_from"] = f"{kind}:{item_id}"
        return out
    if kind == "series":
        return series(store, str(item_id), include_internal=include_internal, project=project)
    if kind == "document":
        return document(store, int(item_id), include_internal=include_internal, project=project)
    if kind == "exhibit":
        from fundus import exhibits as exm
        return exm.get(store, item_id, include_internal=include_internal, project=project)
    if kind not in ITEM_KINDS:
        raise ValueError(f"unknown kind '{kind}'")
    row = _row(store, kind, item_id)
    if row is None:
        raise LookupError(f"{kind} {item_id} not found")
    if not is_visible(store, kind, row, include_internal, project):
        raise PermissionError(f"{kind} {item_id} is internal; pass include_internal=true (or its project)")
    out = {"kind": kind, **{k: clip(row[k], max_text) for k in row.keys() if row[k] not in (None, "")}}
    if row["document_id"]:
        d = store.db.execute("SELECT * FROM document WHERE id = ?", (row["document_id"],)).fetchone()
        if d:
            doc = {k: clip(d[k], 500) for k in d.keys() if d[k] not in (None, "") and k not in ("url_key",)}
            if d["publisher_id"]:
                p = store.db.execute("SELECT * FROM publisher WHERE id = ?", (d["publisher_id"],)).fetchone()
                if p:
                    doc["publisher"] = {k: p[k] for k in p.keys() if p[k] not in (None, "")}
            out["document"] = doc
    out["tags"] = tags_of(store, kind, item_id)
    out["origin"] = [_compact(dict(r)) for r in store.db.execute(
        "SELECT project, source_path, source_key, importer, imported_at FROM origin WHERE item_kind = ? AND item_id = ?",
        (kind, str(item_id)))]
    out["history"] = [_compact({**dict(r), "detail": clip(r["detail"], 300)}) for r in store.db.execute(
        "SELECT ts, action, detail, actor FROM changelog WHERE item_kind = ? AND item_id = ? ORDER BY id DESC LIMIT 5",
        (kind, str(item_id)))]
    if kind == "observation" and row["series_id"]:
        try:
            s = series(store, row["series_id"], include_internal=include_internal, project=project, max_obs=60)
            s.pop("forecasts", None)
            out["series"] = s
        except PermissionError:
            out["series"] = {"id": row["series_id"], "note": "series is internal"}
    if with_related:
        rel = related(store, kind, row, include_internal, project)
        if rel:
            out["related"] = rel
    return _compact(out)


def _label(store: Store, kind: str, row) -> dict:
    """Very compact item view for context lists."""
    d = {"kind": kind, "id": row["id"], "label": clip(row[LABEL_COL[kind]], 140)}
    if kind == "exhibit":
        return _compact({**d, "exhibit_type": row["kind"], "project": row["project"], "slug": row["slug"]})
    if kind == "observation":
        d.update(value=row["value"] if row["value"] is not None else row["value_text"], unit=row["unit"], period=row["period"])
    elif kind == "forecast":
        d.update(target_period=row["target_period"])
    if kind != "series" and row["document_id"]:
        pub = store.db.execute("SELECT p.name FROM document d JOIN publisher p ON p.id = d.publisher_id WHERE d.id = ?",
                               (row["document_id"],)).fetchone()
        if pub:
            d["publisher"] = pub[0]
    return _compact(d)


def related(store: Store, kind: str, row, include_internal: bool = False, project=None, n_items: int = 5) -> dict:
    """Context around one item: its entities (with roles); for an observation in a series the value of the latest
    period and whether a newer edition reported this period differently; up to `n_items` visible items linked to
    the item's main entity (subject/organisation/speaker before publisher before mentions)."""
    out: dict = {}
    iid = str(row["id"])
    links = store.db.execute("SELECT e.id, e.name, e.kind, l.role, l.confidence FROM entity_link l "
                             "JOIN entity e ON e.id = l.entity_id WHERE l.item_kind = ? AND l.item_id = ?", (kind, iid)).fetchall()
    by_ent: dict[int, dict] = {}
    for eid, name, ekind, role, conf in sorted(links, key=lambda r: (ent.ROLE_RANK.get(r[3], 9), -(r[4] or 0))):
        e = by_ent.setdefault(eid, {"name": name, "kind": ekind, "roles": [], "head": False})
        e["roles"].append(role)
        e["head"] |= role == "mentioned" and (conf or 0) >= 0.85
    if by_ent:
        out["entities"] = [{"name": v["name"], "kind": v["kind"], "role": ",".join(v["roles"])} for v in list(by_ent.values())[:8]]
    if kind == "observation" and row["series_id"]:
        vis, vp = vis_sql("observation", "o", include_internal, project)
        obs = store.db.execute(f"SELECT o.id, o.period, o.category, o.value, o.value_text, o.unit, o.document_id, d.published "
                               f"FROM observation o LEFT JOIN document d ON d.id = o.document_id WHERE o.series_id = ?{vis}",
                               (row["series_id"], *vp)).fetchall()
        same_cat = [o for o in obs if (o["category"] or "") == (row["category"] or "")] or obs
        if same_cat:
            last = max(same_cat, key=lambda o: (_period_key(o["period"]), o["published"] or ""))
            out["series_latest"] = _compact({"id": last["id"], "period": last["period"],
                                             "value": last["value"] if last["value"] is not None else last["value_text"],
                                             "unit": last["unit"], "is_this": last["id"] == row["id"] or None})
        mine = store.db.execute("SELECT published FROM document WHERE id = ?", (row["document_id"],)).fetchone() if row["document_id"] else None
        mine = (mine[0] if mine else None) or ""
        revs = [o for o in same_cat if o["id"] != row["id"] and o["period"] == row["period"] and o["value"] is not None
                and row["value"] is not None and abs(o["value"] - row["value"]) > 1e-9 and (o["published"] or "") > mine]
        if revs:
            out["revised_by_newer_edition"] = [_compact({"id": o["id"], "value": o["value"], "unit": o["unit"],
                                                         "published": o["published"]}) for o in revs[:3]]
    # main entity: what the item is about (subject/organisation/speaker, a headline mention), else its publisher
    order = sorted(by_ent, key=lambda e: (0 if set(by_ent[e]["roles"]) & {"subject", "organisation", "speaker"} else
                                          1 if by_ent[e]["head"] else 2 if "publisher" in by_ent[e]["roles"] else 3))
    for main in order[:3]:
        cands = store.db.execute(
            f"SELECT item_kind, item_id, min({ent_rank_sql()}) AS rk FROM entity_link WHERE entity_id = ? "
            "AND NOT (item_kind = ? AND item_id = ?) GROUP BY item_kind, item_id ORDER BY rk, length(item_id) DESC, item_id DESC "
            "LIMIT 200", (main, kind, iid)).fetchall()
        items = []
        for k, i, _ in cands:
            r = _row(store, k, i) if k == "series" or str(i).isdigit() else None
            if r is not None and is_visible(store, k, r, include_internal, project):
                items.append(_label(store, k, r))
                if len(items) >= n_items:
                    break
        if items:
            out["same_entity"] = {"entity": by_ent[main]["name"], "items": items}
            break
    return out


def ent_rank_sql() -> str:
    return "CASE role " + " ".join(f"WHEN '{r}' THEN {i}" for r, i in ent.ROLE_RANK.items()) + " ELSE 9 END"


def entity_profile(store: Store, name: str, include_internal: bool = False, project=None, per_role: int = 3) -> dict:
    """Profile of an organisation/person/product: aliases, parent/children, counts of visible linked items by kind,
    role and project, top items per role and the 10 entities sharing most items with it. Entities whose items are
    all internal are only shown with include_internal or their project."""
    ids = ent.find(store, name)
    if not ids:
        raise LookupError(f"no entity named '{name}'")
    fkw = dict(include_internal=include_internal, visible_project=project)
    for eid in ids:
        prof = _profile(store, eid, fkw, per_role)
        if prof:
            if len(ids) > 1:
                others = [store.db.execute("SELECT name FROM entity WHERE id = ?", (o,)).fetchone()[0] for o in ids if o != eid]
                prof["other_matches"] = others[:5]
            return prof
    raise LookupError(f"no entity named '{name}'")


def _profile(store: Store, eid: int, fkw: dict, per_role: int) -> dict | None:
    db = store.db
    e = db.execute("SELECT * FROM entity WHERE id = ?", (eid,)).fetchone()
    by_kind, by_role, by_project, co = {}, {}, {}, {}
    top: dict[str, list] = {}
    exhibits_out = None
    for k in ALL_KINDS:
        f, p = _filters(store, k, **fkw)
        join = "x.id = l.item_id" if k == "series" else "x.id = CAST(l.item_id AS INTEGER)"
        base = f"FROM entity_link l CROSS JOIN {k} x ON {join} WHERE l.entity_id = ? AND l.item_kind = '{k}'" + \
               "".join(f" AND {w}" for w in f)
        n = db.execute(f"SELECT count(DISTINCT x.id) {base}", [eid, *p]).fetchone()[0]
        if not n:
            continue
        if k == "exhibit":   # slides/charts about or naming the entity: own section, subject before mentions
            by_kind[k] = n
            from fundus import exhibits as exm
            rows = db.execute(f"SELECT x.*, min(CASE l.role WHEN 'subject' THEN 0 ELSE 1 END) AS rk, max(l.confidence) AS cf "
                              f"{base} GROUP BY x.id ORDER BY rk, cf DESC, x.updated_at DESC LIMIT 8", [eid, *p]).fetchall()
            exhibits_out = {"count": n,
                            "by_project": dict(db.execute(f"SELECT x.project, count(DISTINCT x.id) {base} GROUP BY x.project",
                                                          [eid, *p]).fetchall()),
                            "by_role": dict(db.execute(f"SELECT l.role, count(*) {base} GROUP BY l.role", [eid, *p]).fetchall()),
                            "items": [_compact({"id": r["id"], "role": "subject" if r["rk"] == 0 else "mentioned",
                                                **{kk: vv for kk, vv in exm.summary_fields(store, r).items()
                                                   if kk in ("exhibit_type", "title", "project", "preview_url")}})
                                      for r in rows]}
            continue
        by_kind[k] = n
        for role, c in db.execute(f"SELECT l.role, count(*) {base} GROUP BY l.role", [eid, *p]):
            by_role[role] = by_role.get(role, 0) + c
        for pr, c in db.execute(f"SELECT t.value, count(DISTINCT x.id) {base.replace('WHERE', 'JOIN tag t ON t.item_kind = l.item_kind AND t.item_id = l.item_id AND t.facet = ' + chr(39) + 'project' + chr(39) + ' WHERE', 1)} "
                                f"GROUP BY t.value", [eid, *p]):
            by_project[pr] = by_project.get(pr, 0) + c
        for oid, c in db.execute(f"SELECT l2.entity_id, count(DISTINCT x.id) {base.replace('WHERE', 'JOIN entity_link l2 ON l2.item_kind = l.item_kind AND l2.item_id = l.item_id AND l2.entity_id != l.entity_id WHERE', 1)} "
                                 f"GROUP BY l2.entity_id", [eid, *p]):
            co[oid] = co.get(oid, 0) + c
        order = "x.id DESC" if k != "series" else "x.id"
        for role in ent.ROLES:
            rows = db.execute(f"SELECT x.* {base} AND l.role = ? ORDER BY {order} LIMIT ?", [eid, *p, role, per_role]).fetchall()
            top.setdefault(role, []).extend(_label(store, k, r) for r in rows)
    if not by_kind and not fkw.get("include_internal"):
        return None
    aliases = [r[0] for r in db.execute("SELECT alias FROM entity_alias WHERE entity_id = ? AND source NOT IN "
                                        "('raw', 'compound', 'publisher_alias') ORDER BY rowid", (eid,))]
    parent = db.execute("SELECT name FROM entity WHERE id = ?", (e["parent_id"],)).fetchone() if e["parent_id"] else None
    children = [r[0] for r in db.execute("SELECT name FROM entity WHERE parent_id = ? ORDER BY name LIMIT 10", (eid,))]
    names = {}
    top_co = sorted(co.items(), key=lambda t: -t[1])[:10]
    for oid, _ in top_co:
        r = db.execute("SELECT name, kind FROM entity WHERE id = ?", (oid,)).fetchone()
        names[oid] = r
    return _compact({
        "id": eid, "name": e["name"], "kind": e["kind"], "description": e["description"], "country": e["country"],
        "parent": parent[0] if parent else None, "children": children,
        "aliases": [a for a in dict.fromkeys(aliases) if a != e["name"]][:25],
        "counts": {"by_kind": by_kind, "by_role": by_role,
                   "by_project": dict(sorted(by_project.items(), key=lambda t: -t[1])[:10])},
        "top_items": {role: items[:per_role] for role, items in top.items() if items},
        "co_occurring": [{"name": names[o][0], "kind": names[o][1], "shared_items": c} for o, c in top_co if names.get(o)],
        "exhibits": exhibits_out,
    })


def doc_visible(store: Store, doc_id: int, include_internal=False, project=None) -> bool:
    """A document is hidden when items reference it and none of them is visible to the caller."""
    if include_internal:
        return True
    n_any = n_vis = 0
    for k in ITEM_KINDS:
        vis, vp = vis_sql(k, "x", False, project)
        n_any += store.db.execute(f"SELECT count(*) FROM {k} x WHERE x.document_id = ?", (doc_id,)).fetchone()[0]
        n_vis += store.db.execute(f"SELECT count(*) FROM {k} x WHERE x.document_id = ?{vis}", (doc_id, *vp)).fetchone()[0]
    return n_any == 0 or n_vis > 0


def document(store: Store, doc_id: int, include_internal: bool = False, project=None, limit: int = 50) -> dict:
    d = store.db.execute("SELECT * FROM document WHERE id = ?", (doc_id,)).fetchone()
    if d is None:
        raise LookupError(f"document {doc_id} not found")
    if not doc_visible(store, doc_id, include_internal, project):
        raise PermissionError(f"document {doc_id} is only used by internal items; pass include_internal=true (or the project)")
    out = {"kind": "document", **{k: d[k] for k in d.keys() if d[k] not in (None, "") and k != "url_key"}}
    if d["publisher_id"]:
        p = store.db.execute("SELECT * FROM publisher WHERE id = ?", (d["publisher_id"],)).fetchone()
        if p:
            out["publisher"] = {k: p[k] for k in p.keys() if p[k] not in (None, "")}
    items = []
    for k in ITEM_KINDS:
        vis, vp = vis_sql(k, "x", include_internal, project)
        for r in store.db.execute(f"SELECT * FROM {k} x WHERE x.document_id = ?{vis} LIMIT ?", (doc_id, *vp, limit)):
            s = summary(store, k, r, include_internal, project, with_tags=False)
            s.pop("source", None)
            items.append(s)
    out["items"] = items[:limit]
    out["n_items"] = len(items)
    return out


# ------------------------------------------------------------------ series
def series(store: Store, series_id: str, include_internal: bool = False, project=None, max_obs: int = 500) -> dict:
    """Series metadata + observations sorted by period (source per value) + linked forecasts."""
    row = _row(store, "series", series_id)
    if row is None:
        raise LookupError(f"series {series_id} not found")
    if not is_visible(store, "series", row, include_internal, project):
        raise PermissionError(f"series {series_id} is internal; pass include_internal=true (or its project)")
    out = {"kind": "series", **{k: row[k] for k in row.keys() if row[k] not in (None, "")}}
    if row["publisher_id"]:
        p = store.db.execute("SELECT name, type FROM publisher WHERE id = ?", (row["publisher_id"],)).fetchone()
        if p:
            out["publisher"] = _compact(dict(p))
    out["tags"] = tags_of(store, "series", series_id)
    vis, vp = vis_sql("observation", "o", include_internal, project)
    obs = store.db.execute(f"SELECT * FROM observation o WHERE o.series_id = ?{vis}", (series_id, *vp)).fetchall()
    obs = sorted(obs, key=lambda r: (_period_key(r["period"]), r["category"] or "", r["id"]))
    docs: dict = {}
    values = []
    for r in obs[:max_obs]:
        if r["document_id"] not in docs:
            docs[r["document_id"]] = doc_brief(store, r["document_id"])
        values.append(_compact({"id": r["id"], "period": r["period"], "category": clip(r["category"], 120),
                                "value": r["value"], "value_text": r["value_text"], "unit": r["unit"],
                                "geo": r["geo"], "n": r["n"], "status": r["status"],
                                "visibility": "internal" if r["visibility"] == "internal" else None,
                                "locator": clip(r["locator"], 80), "source": docs[r["document_id"]]}))
    out["observations"] = values
    out.update(series_stats(store, series_id, include_internal, project))
    if len(obs) > max_obs:
        out["truncated"] = f"showing {max_obs} of {len(obs)} observations"
    fvis, fp = vis_sql("forecast", "f", include_internal, project)
    out["forecasts"] = [summary(store, "forecast", r, include_internal, project, with_tags=False) for r in store.db.execute(
        f"SELECT * FROM forecast f WHERE f.series_id = ?{fvis} ORDER BY f.target_period", (series_id, *fp))]
    return _compact(out) | {"observations": values, "forecasts": out["forecasts"]}


def find_series(store: Store, text: str, limit: int = 20, include_internal: bool = False, project=None,
                visible_project=None) -> list[dict]:
    """Series whose name, id, publisher, unit or tags match the text (FTS plus a substring fallback)."""
    res = search(store, text, kind="series", limit=limit, include_internal=include_internal, project=project,
                 visible_project=visible_project, mode="lexical")["results"]
    grant = project or visible_project
    seen = {r["id"] for r in res}
    if len(res) < limit and text:
        like = f"%{text.strip().lower()}%"
        for r in store.db.execute("SELECT * FROM series WHERE lower(id) LIKE ? OR lower(name) LIKE ? LIMIT ?",
                                  (like, like, limit)):
            if r["id"] in seen or (project and not _in_project(store, "series", r["id"], project)):
                continue
            if is_visible(store, "series", r, include_internal, grant):
                res.append(summary(store, "series", r, include_internal, grant))
                seen.add(r["id"])
    return res[:limit]


# ------------------------------------------------------------------ overview
def overview(store: Store, industry=None, topic=None, region=None, include_internal: bool = False, project=None,
             limit: int = 10) -> dict:
    """What do we have on X? Counts per kind, top series (most periods), newest items, open/due
    forecasts, tag breakdown and the number of series waiting for an update check."""
    args = dict(industry=industry, topic=topic, region=region, include_internal=include_internal, project=project)
    counts = {}
    for k in ALL_KINDS:
        f, p = _filters(store, k, **args)
        where = (" WHERE " + " AND ".join(f)) if f else ""
        counts[k] = store.db.execute(f"SELECT count(*) FROM {k} x{where}", p).fetchone()[0]
    # top series by number of distinct periods
    f, p = _filters(store, "series", **args)
    where = (" WHERE " + " AND ".join(f)) if f else ""
    vis, vp = vis_sql("observation", "o", include_internal, project)
    rows = store.db.execute(
        f"SELECT x.*, (SELECT count(DISTINCT period) FROM observation o WHERE o.series_id = x.id{vis}) AS _np "
        f"FROM series x{where} ORDER BY _np DESC, x.id LIMIT ?", [*vp, *p, limit]).fetchall()
    top_series = [summary(store, "series", r, include_internal, project, with_tags=False) for r in rows]
    newest = search(store, None, kind=ITEM_KINDS, limit=limit, **args)["results"]
    for n in newest:
        n.pop("tags", None)
    # open / due forecasts, soonest target first
    f, p = _filters(store, "forecast", **args)
    f.append("x.outcome IN ('open','due')")
    frows = store.db.execute(f"SELECT x.* FROM forecast x WHERE {' AND '.join(f)}", p).fetchall()
    frows = sorted(frows, key=lambda r: (r["outcome"] != "due", _period_key(r["target_period"])))
    forecasts = [summary(store, "forecast", r, include_internal, project, with_tags=False) for r in frows[:limit]]
    # breakdown by the facet not filtered on
    other = "topic" if industry and not topic else "industry"
    counts_by = tag_counts(store, other, **args)
    breakdown = dict(sorted(((v, n) for (_, v), n in counts_by.items()), key=lambda t: (-t[1], t[0]))[:25])
    targets = update_targets(store, industry=industry, topic=topic, limit=0,
                             include_internal=include_internal, project=project)
    return _compact({"filter": _compact({"industry": industry, "topic": topic, "region": region, "project": project}),
                     "counts": counts, f"by_{other}": breakdown, "top_series": top_series,
                     "forecasts_open_or_due": forecasts,
                     "forecasts_total_open_or_due": len(frows),
                     "series_needing_update_check": targets["total"],
                     "newest": newest}) | {"counts": counts}


# ------------------------------------------------------------------ update targets
def update_targets(store: Store, industry=None, topic=None, older_than_days: int = 180, today: date | None = None,
                   limit: int = 30, include_internal: bool = False, project=None) -> dict:
    """Series that should be checked for a new edition: next_expected has passed ('overdue'), never
    checked ('never_checked') or last checked more than `older_than_days` ago ('stale_check').
    Series with a future next_expected and one-off series (frequency 'once') are skipped."""
    today = today or date.today()
    cutoff = (today - timedelta(days=older_than_days)).isoformat()
    f, p = _filters(store, "series", industry=industry, topic=topic, include_internal=include_internal, project=project)
    f.append("coalesce(x.frequency, '') != 'once'")
    rows = store.db.execute(f"SELECT x.* FROM series x WHERE {' AND '.join(f)}", p).fetchall()
    # edition trackers of a dataset that has its own cadence: the dataset is listed instead (fundus.datasets)
    tracked = {str(r[0]) for r in store.db.execute("SELECT id FROM dataset WHERE removed_at IS NULL AND "
                                                    "(frequency IS NOT NULL OR next_expected IS NOT NULL)")} \
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'dataset'").fetchone() else set()
    out = []
    for r in rows:
        m = re.search(r"\bdataset:(\d+)\b", r["update_hint"] or "")
        if m and m.group(1) in tracked:
            continue
        nxt = period_start(r["next_expected"]) if r["next_expected"] else None
        if nxt and nxt <= today:
            reason = "overdue"
        elif nxt:
            continue
        elif not r["last_checked"]:
            reason = "never_checked"
        elif r["last_checked"][:10] < cutoff:
            reason = "stale_check"
        else:
            continue
        st = series_stats(store, r["id"], include_internal, project)
        pub = store.db.execute("SELECT name FROM publisher WHERE id = ?", (r["publisher_id"],)).fetchone()
        out.append(_compact({"id": r["id"], "name": clip(r["name"], 160), "publisher": pub[0] if pub else None,
                             "frequency": r["frequency"], "n_periods": st["n_periods"], "last_period": st["last_period"],
                             "next_expected": r["next_expected"], "last_checked": r["last_checked"],
                             "update_hint": clip(r["update_hint"]), "reason": reason}))
    order = {"overdue": 0, "stale_check": 1, "never_checked": 2}
    out.sort(key=lambda s: (order[s["reason"]], _period_key(s.get("next_expected")) if s["reason"] == "overdue"
                            else _period_key(s.get("last_period"))))
    res = {"total": len(out), "today": today.isoformat(), "targets": out[:limit] if limit else []}
    if limit and len(out) > limit:
        res["more"] = len(out) - limit
    if tracked or store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'dataset'").fetchone():
        from fundus import datasets
        dd = datasets.due(store, older_than_days, include_internal=include_internal, project=project, today=today)
        if dd:
            res["datasets"] = dd[:limit] if limit else dd
            res["datasets_total"] = len(dd)
    return res


# ------------------------------------------------------------------ vocabulary
def tag_counts(store: Store, facet: str | None = None, **filters) -> dict[tuple[str, str], int]:
    """{(facet, value): number of VISIBLE items/series carrying the tag}; `filters` as for _filters()."""
    out: dict[tuple[str, str], int] = {}
    for k in ALL_KINDS:
        f, p = _filters(store, k, **filters)
        join = "x.id = t.item_id" if k == "series" else "x.id = CAST(t.item_id AS INTEGER)"
        where = [f"t.item_kind = '{k}'", *(["t.facet = ?"] if facet else []), *f]
        for fa, v, n in store.db.execute(f"SELECT t.facet, t.value, count(*) FROM tag t JOIN {k} x ON {join} "
                                         f"WHERE {' AND '.join(where)} GROUP BY t.facet, t.value",
                                         [*([facet] if facet else []), *p]):
            out[(fa, v)] = out.get((fa, v), 0) + n
    return out


def vocab(store: Store, facet: str | None = None, include_internal: bool = False, project=None) -> dict:
    """Controlled vocabulary per facet with usage counts over visible items:
    {facet: [{value, label, parent, description, n}]} plus project slugs in use."""
    counts = tag_counts(store, None, include_internal=include_internal, visible_project=project)
    out: dict[str, list] = {}
    q = "SELECT facet, value, parent, label, description FROM vocab"
    rows = store.db.execute(q + (" WHERE facet = ?" if facet else "") + " ORDER BY facet, coalesce(parent, value), value",
                            (facet,) if facet else ()).fetchall()
    for r in rows:
        out.setdefault(r["facet"], []).append(_compact({"value": r["value"], "label": r["label"], "parent": r["parent"],
                                                        "description": r["description"],
                                                        "n": counts.get((r["facet"], r["value"]), 0)}))
    if not facet or facet == "project":
        out["project"] = [{"value": v, "n": n} for (f, v), n in sorted(counts.items()) if f == "project"]
    return out
