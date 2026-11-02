"""Fundus tidy – the standard data-hygiene procedure (Falk, 1 Nov 2026: "fully automatic, decide the open cases yourself,
define a standard for units and a set of scripts that works together with Claude").

One command does the routine part; the cases that need judgement go to Claude as a compact list and come back as
decisions. Every step is journalled in cleanup_run/cleanup_journal and can be undone (`fundus cleanup undo RUN`).

    fundus tidy run [--no-units]       analyse → automatic classes (variants, safe duplicates, orphans) → unit standard
                                       → analyse again; prints the KPI before/after and how many cases wait for review
    fundus tidy units [--apply]        the unit standard: plan (default) or apply
    fundus tidy review [--out FILE]    open probable duplicates and contradictions as compact JSON for Claude
    fundus tidy decide FILE            apply Claude's decisions [{"id": N, "action": "merge|reject|dispute|later",
                                       "keep": ID?, "why": "…"}]
    fundus tidy report                 KPI, open cases, unit spellings, last runs

The standard itself (units, populations, qualifiers) is documented in docs/DATA-STANDARD.md; the rules live here.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime

from fundus import cleanup
from fundus.store import Store
from fundus.units import normalise_unit

# ------------------------------------------------------------------ unit standard
# "% of <who>": who was counted → population; the unit stays "%". Head nouns of populations (singular/plural, lower case).
POP_NOUNS = {
    "respondent", "company", "companies", "firm", "enterprise", "employer", "establishment", "hospital", "organisation",
    "organization", "sme", "business", "ceo", "cmo", "cco", "cdo", "cio", "cfo", "executive", "leader", "manager", "marketer",
    "designer", "developer", "clinician", "physician", "nurse", "radiologist", "patient", "customer", "consumer", "user",
    "adult", "person", "people", "worker", "employee", "student", "instructor", "practice", "clinic", "trust", "team",
    "brand", "agency", "seller", "buyer", "fan", "visitor", "abandoner", "household", "dealer", "retailer", "merchant",
    "startup", "country", "group", "committee", "multinational", "occupation", "participant", "self-automator",
    "practitioner", "professional", "decision-maker", "it", "gen", "fortune", "us", "uk", "eu", "b2b", "ai-using",
    "work-ai", "design-active", "surveyed", "responding", "affected", "system-affiliated", "trained", "non-using", "new",
}
# words that mark a denominator ("% of GDP"): the unit keeps them – a different measure, not a population
DENOMINATORS = {
    "gdp", "revenue", "revenues", "sales", "turnover", "profit", "budget", "budgets", "spend", "spending", "costs", "cost",
    "expenses", "expense", "time", "hours", "employment", "labor", "labour", "workforce", "traffic", "visits", "pce",
    "baseline", "capacity", "code", "units", "shipments", "tasks", "conversations", "queries", "questions", "responses",
    "interactions", "transcripts", "messages", "journeys", "appointments", "cases", "discharges", "hospitalisations",
    "incidents", "defects", "documents", "applications", "projects", "processes", "roles", "jobs", "skills", "value",
    "market", "economy", "compute", "elements", "fields", "issues", "activities", "decisions", "patents", "level",
    "salary", "ad", "e-commerce", "net", "prior-year", "total", "frontier", "hiring", "work", "working", "office-day",
    "purchase", "project", "team", "seller", "nursing", "grading", "recommended", "audited", "flagged", "emphasis",
    "build", "external", "digital", "technology", "influencer", "marketing", "sales", "supply", "private-payer", "s&p",
    "mainstream", "cv", "api", "genai", "agentic", "enterprise", "reported", "devices", "acute", "hospital", "health",
    "trust", "it", "1", "2024", "a", "weekdays", "hours", "population",
}
QUALIFIER = re.compile(
    r"\((?:approx\.?|about|around|nearly|almost|at least|more than|over|under|just over|just under|less than|up to|"
    r"upper bound|lower bound|upper end|low end|range|midpoint|reported|as reported|claim|forecast|estimate|own |"
    r"rubric|max|median|relative|absolute|increase|percentage points as stated|points or percent not stated|one in|"
    r"[0-9]|['\"‘“]).*?\)", re.I)
SIMPLE = {  # exact spellings → standard (after whitespace normalisation, case-insensitive key)
    "tchf": "CHF k", "minutes/day": "minutes per day", "hours/week": "hours per week", "hours/day": "hours per day",
    "downloads/week": "downloads per week", "downloads/month": "downloads per month", "sessions/month": "sessions per month",
    "incidents/month": "incidents per month", "violations/month": "violations per month", "procedures/weekday": "procedures per weekday",
    "usd/month": "USD per month", "usd/provider/month": "USD per provider per month", "eur/patient": "EUR per patient",
    "x (fold)": "x", "x (factor)": "x", "x (multiple)": "x", "multiple": "x", "times as likely": "x",
    "% yoy": "% YoY", "% yoy growth": "% YoY", "% growth": "% change", "% increase": "% change", "% change": "% change",
    "% per year (cagr)": "% CAGR", "% cagr": "% CAGR", "fraction (0-1)": "fraction (0–1)", "fraction correct (0-1)": "fraction correct (0–1)",
    "score/100": "score (0–100)", "score 0-100": "score (0–100)", "score (0-100)": "score (0–100)", "points (0-100)": "score (0–100)",
    "score (0–100)": "score (0–100)", "score (1-7)": "score (1–7)", "mean score (1–7)": "score (1–7)", "mean score (1-7)": "score (1–7)",
    "points (1-7)": "score (1–7)", "score (1–5)": "score (1–5)", "mean (1-5)": "score (1–5)", "score (1–6)": "score (1–6)",
    "score (0-10)": "score (0–10)", "stars of 5": "stars (of 5)", "mean score of 5": "score (1–5)",
    "percentage points (absolute)": "pp", "percentage points higher": "pp", "pp balance": "pp", "min": "minutes",
    "share": "%", "% share": "%", "% (percentage points as stated)": "pp",
}
RANGE_DASH = re.compile(r"(\d)\s*-\s*(\d)")


def standard_unit(unit: str | None) -> tuple[str | None, str | None, str | None]:
    """(unit by the standard, population taken out of it, qualifier for the notes) – or the unit unchanged with
    population/qualifier None when no rule applies (also when the result would still be a compound like
    'x; y', 'a -> b' or 'nouns, more text' – those stay for a human look)."""
    if unit is None or not str(unit).strip():
        return unit, None, None
    to, pop, qual = _standard_unit(unit)
    if to != unit and (re.search(r";|->", to or "") or ("," in (to or "") and not str(to).startswith("index"))
                       or re.search(r";|->", pop or "")):
        return unit, None, None
    return to, pop, qual


def _standard_unit(unit: str) -> tuple[str | None, str | None, str | None]:
    u = re.sub(r"\s+", " ", str(unit)).strip()
    if u.lower() in SIMPLE:                 # exact spellings first ("% (percentage points as stated)" → pp)
        return SIMPLE[u.lower()], None, None
    qual = None
    # qualifiers in brackets ("(approx.)", "(at least)", "(31 sure + 51 suspect)") go to the notes
    m = QUALIFIER.search(u)
    if m and re.fullmatch(r"\(\s*\d{4}(\s+(dollars|usd|eur|chf|prices|base))?\s*\)", m.group(0), re.I):
        m = None                           # a price base year ("USD bn (2019 dollars)") belongs to the unit
    if m and not re.match(r"^(index|score|points|mean|stars|fraction)", u, re.I):
        qual = m.group(0).strip("() ")
        u = (u[:m.start()] + u[m.end():]).strip(" ;,")
    if u.lower() in SIMPLE:
        return SIMPLE[u.lower()], None, qual
    u = RANGE_DASH.sub(r"\1–\2", u)
    m = re.match(r"^index,\s*(.+?=\s*100)$", u, re.I)
    if m:
        return f"index ({m.group(1)})", None, qual
    m = re.match(r"^count\s+(?!per\b|of\b)([a-z].*)$", u, re.I)            # "count vehicles" → "vehicles"
    if m:
        return m.group(1), None, qual
    m = re.match(r"^%\s+of\s+(.+)$", u, re.I)
    if m:
        who = m.group(1).strip()
        words = [w.strip("()'\".,").lower() for w in who.split()]
        if words and not (set(words) & DENOMINATORS) and any(w.rstrip("s") in POP_NOUNS or w in POP_NOUNS for w in words):
            return "%", who, qual
        return "% of " + who, None, qual
    n = normalise_unit(u)
    return n, None, qual


def unit_plan(store: Store) -> list[dict]:
    """Every unit spelling that the standard changes: {unit, to, population, qualifier, rows: {table: n}}."""
    plan = {}
    for table in ("observation", "forecast", "series"):
        for unit, n in store.db.execute(f"SELECT unit, count(*) FROM {table} WHERE unit IS NOT NULL GROUP BY unit"):
            to, pop, qual = standard_unit(unit)
            if (to, pop, qual) == (unit, None, None) or to in (None, ""):
                continue
            e = plan.setdefault(unit, {"unit": unit, "to": to, "population": pop, "qualifier": qual, "rows": {}})
            e["rows"][table] = n
    return sorted(plan.values(), key=lambda e: -sum(e["rows"].values()))


def _append(text: str | None, add: str) -> str:
    return add if not text else (text if add in text else f"{text} | {add}")


def apply_units(store: Store, actor: str = "cli:tidy", run: int | None = None) -> dict:
    """Apply the unit standard (journalled: undo with `fundus cleanup undo RUN`)."""
    plan = unit_plan(store)
    own = run is None
    if own:
        run = cleanup._start(store, "apply", actor)     # an apply run (cleanup_run.kind): undo works as for any apply
    j = cleanup.Journal(store, run)
    changed = Counter()
    with store.batch(actor):
        for e in plan:
            for table in e["rows"]:
                has_pop = table in ("observation", "series")
                cols = ["unit", "notes"] + (["population"] if has_pop else [])
                j.snap(table, "unit = ?", [e["unit"]], cols=cols)
                rows = store.db.execute(f"SELECT id, notes{', population' if has_pop else ''} FROM {table} WHERE unit = ?",
                                        (e["unit"],)).fetchall()
                for r in rows:
                    ch = {"unit": e["to"]}
                    if e["population"] and has_pop and not (r["population"] or "").strip():
                        ch["population"] = e["population"]
                    if e["qualifier"]:
                        ch["notes"] = _append(r["notes"], f"Unit as given: {e['unit']}")
                    store.update(table, r["id"], ch)
                    changed[table] += 1
    summary = {"step": "units", "spellings": len(plan), "rows": dict(changed)}
    if own:
        cleanup._finish(store, run, summary)
    return {"run": run, **summary}


# ------------------------------------------------------------------ the routine
def kpi(store: Store) -> dict:
    s = cleanup.score(store)
    units = store.db.execute("SELECT count(DISTINCT unit) FROM (SELECT unit FROM observation UNION ALL SELECT unit FROM forecast "
                             "UNION ALL SELECT unit FROM series) WHERE unit IS NOT NULL").fetchone()[0]
    open_ = Counter(r[0] for r in store.db.execute("SELECT class FROM cleanup_candidate WHERE state = 'open'"))
    return {"score": s.get("score"), "affected": s.get("affected"), "unit_spellings": units,
            "open": dict(open_), "review": open_.get("probable", 0) + open_.get("contradiction", 0)}


def run(store: Store, units: bool = True, actor: str = "cli:tidy", backup: bool = True) -> dict:
    """The automatic part: analyse, apply the automatic classes, the unit standard, analyse again."""
    before = None
    cleanup.analyze(store, actor)
    before = kpi(store)
    out = {"before": before}
    out["apply"] = {k: v for k, v in cleanup.apply(store, actor=actor, backup=backup).items() if k in ("run", "records", "failed", "score")}
    if units:
        out["units"] = apply_units(store, actor)
        cleanup.analyze(store, actor)      # new spellings may make new duplicates visible
        out["apply_after_units"] = {k: v for k, v in cleanup.apply(store, actor=actor, backup=False).items()
                                    if k in ("run", "records", "failed", "score")}
    cleanup.analyze(store, actor)
    out["after"] = kpi(store)
    return out


# ------------------------------------------------------------------ review with Claude
def _obs(store: Store, oid) -> dict | None:
    r = store.db.execute("SELECT o.*, d.title AS doc, d.url FROM observation o LEFT JOIN document d ON d.id = o.document_id "
                         "WHERE o.id = ?", (int(oid),)).fetchone()
    if r is None:
        return None
    return {k: v for k, v in {"id": r["id"], "metric": r["metric"], "category": r["category"], "value": r["value"],
                              "unit": r["unit"], "period": r["period"], "geo": r["geo"], "population": r["population"],
                              "series": r["series_id"], "locator": r["locator"], "status": r["status"],
                              "notes": (r["notes"] or "")[:200] or None, "doc": r["document_id"]}.items() if v not in (None, "")}


def _brief(store: Store, kind: str, iid) -> dict | None:
    if kind == "observation":
        return _obs(store, iid)
    if kind == "series":
        r = store.db.execute("SELECT s.*, (SELECT count(*) FROM observation o WHERE o.series_id = s.id) AS n FROM series s "
                             "WHERE id = ?", (str(iid),)).fetchone()
        return {k: r[k] for k in ("id", "name", "unit", "geo", "population", "publisher_id", "n") if r and r[k] not in (None, "")} if r else None
    if kind == "publisher":
        r = store.db.execute("SELECT p.*, (SELECT count(*) FROM document d WHERE d.publisher_id = p.id) AS n FROM publisher p "
                             "WHERE id = ?", (str(iid),)).fetchone()
        return {k: r[k] for k in ("id", "name", "type", "country", "url", "n") if r and r[k] not in (None, "")} if r else None
    return cleanup._brief(store, kind, iid)


def review(store: Store, limit: int = 500) -> list[dict]:
    """Open findings that need judgement, compact: {id, class, kind, reason, detail, records: [...]} for Claude."""
    out = []
    for r in store.db.execute("SELECT * FROM cleanup_candidate WHERE state = 'open' AND class IN ('probable', 'contradiction') "
                              "ORDER BY class, kind, id LIMIT ?", (limit,)):
        items = json.loads(r["items"])
        out.append({"id": r["id"], "class": r["class"], "kind": r["kind"], "keep": r["keep_id"], "reason": r["reason"],
                    "detail": json.loads(r["detail"]) if r["detail"] else None,
                    "records": [b for b in (_brief(store, r["kind"], i) for i in items) if b]})
    return out


DECISIONS = ("merge", "reject", "dispute", "later", "delete")


def decide(store: Store, decisions: list[dict], actor: str = "cli:tidy") -> dict:
    """Apply Claude's decisions (one run per call, undoable). Each: {id, action, keep?, why}. 'why' is kept in the run."""
    bad = [d for d in decisions if d.get("action") not in DECISIONS or "id" not in d]
    if bad:
        raise ValueError(f"each decision needs id and action ({' | '.join(DECISIONS)}): {bad[:3]}")
    by = defaultdict(list)
    for d in decisions:
        by[(d["action"], str(d.get("keep") or ""))].append(d)
    results, failed = [], []
    for (action, keep), ds in by.items():
        ids = [int(d["id"]) for d in ds]
        try:
            if keep:
                for i in ids:          # keep applies to one finding at a time
                    results.append(cleanup.decide(store, [i], action, keep=keep, actor=actor))
            else:
                results.append(cleanup.decide(store, ids, action, actor=actor))
        except Exception as e:
            failed.append({"ids": ids, "action": action, "error": f"{type(e).__name__}: {e}"[:200]})
    store.log("cleanup", "tidy", "decide", {"decisions": [{k: d.get(k) for k in ("id", "action", "keep", "why")} for d in decisions]})
    store.db.commit()
    return {"decided": len(decisions) - sum(len(f["ids"]) for f in failed), "failed": failed,
            "runs": [r.get("run") for r in results if r.get("run")], "after": kpi(store)}


def report(store: Store) -> dict:
    top = store.db.execute("SELECT unit, count(*) n FROM (SELECT unit FROM observation UNION ALL SELECT unit FROM forecast UNION ALL "
                           "SELECT unit FROM series) WHERE unit IS NOT NULL GROUP BY unit ORDER BY n DESC LIMIT 25").fetchall()
    return {"kpi": kpi(store), "units_top": [[r[0], r[1]] for r in top], "unit_plan": len(unit_plan(store)),
            "runs": cleanup.runs(store, 8), "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
