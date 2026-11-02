"""Ask the dataset catalogue a free-text question ("Haben wir Jobpostings zum Marketing?") – answered by the LOCAL model
(gemma4 on the Spark, never a cloud model: datasets can be client data) from the descriptions only, never from the data.

Two stages (tested 2 Nov 2026 against a one-stage prompt, which missed related datasets and mixed up units of observation):
1. Recall: the whole visible catalogue as compact cards (sorted by id, in the system prompt, so Ollama reuses the cached
   prefix for the next question) → the model proposes candidates and search terms (German and English). The keyword
   search (`datasets.search`) with the question and every term adds its best hits.
2. Judgement: detailed cards of the candidates (own description first, model summary, unit of observation, tables with
   column meanings) → answer ja | teilweise | nein, a short summary, hits (direkt | teilweise, why, columns) and related
   datasets (useful for the intent behind the question).
The answer is validated: only candidate refs, only real column names, at most 6 related, no ref twice.
"""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict

from fundus import datasets as ds
from fundus.store import Store

MAX_CANDIDATES = 30
MAX_STAGE1 = 20
MAX_RELATED = 6
MAX_COLUMNS = 8

SYSTEM1 = """Du bist der Katalog-Assistent eines Datenarchivs. Unten steht der Katalog aller Datensätze (nur Beschreibungen,
nicht die Daten). Deine Aufgabe: zu einer Frage alle Datensätze vorschlagen, die sie beantworten ODER für die Absicht
dahinter nützlich sein könnten (gleiches Thema, andere Messgrösse, Nachbarthema, Berufs-/Branchen-/Code-Tabellen).
Lieber zu viele als einen übersehen, höchstens 20, als dataset-Nummern aus dem Katalog (z. B. "dataset:12").
Gib ausserdem in terms 6–10 kurze Suchbegriffe auf Deutsch und Englisch (Synonyme, Fachbegriffe, verwandte Messgrössen),
mit denen man im Katalog weitersuchen sollte.

# Katalog

"""

SYSTEM2 = """Du bist der Katalog-Assistent eines Datenarchivs. Du bekommst eine Frage und ausführliche Steckbriefe einiger
Datensätze (Beschreibungen und Spalten mit Bedeutung, nicht die Daten). Beurteile genau, was jeder Datensatz enthält.
Regeln:
- Die Beschreibung vom Menschen hat Vorrang; die Modell-Zusammenfassung und die Spaltenbedeutungen können Fehler enthalten.
- Achte auf die Beobachtungseinheit: Daten über Personen sind keine Daten über Unternehmen, Beschäftigung ist keine
  Stellenanzeige, eine Region ist nicht eine andere.
- hits: enthält genau das Gefragte (fit "direkt") oder einen klar benannten Teil davon (fit "teilweise", sag was fehlt).
  columns: die nötigen Spalten, exakt wie angegeben (nur der Spaltenname).
- related: kein Treffer, aber nützlich für die Absicht hinter der Frage (sag wofür). Höchstens 6. Was weder Treffer noch
  nützlich ist, weglassen.
- answer: "ja" bei mindestens einem direkten Treffer, "teilweise" bei nur Teiltreffern, sonst "nein".
  summary: 1–2 ehrliche Sätze auf Deutsch.
- Erfinde nichts; nur dataset-Nummern aus den Steckbriefen."""

SCHEMA1 = {"type": "object", "properties": {"terms": {"type": "array", "items": {"type": "string"}},
                                             "candidates": {"type": "array", "items": {"type": "string"}}},
           "required": ["terms", "candidates"]}
SCHEMA2 = {"type": "object", "properties": {
    "answer": {"type": "string", "enum": ["ja", "teilweise", "nein"]}, "summary": {"type": "string"},
    "hits": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string"}, "fit": {"type": "string", "enum": ["direkt", "teilweise"]}, "why": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}}}, "required": ["ref", "fit", "why", "columns"]}},
    "related": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string"}, "why": {"type": "string"}}, "required": ["ref", "why"]}}},
    "required": ["answer", "summary", "hits", "related"]}

_llm = None
_cache: OrderedDict = OrderedDict()
CACHE_SIZE = 64


def set_llm(llm) -> None:
    """Tests: a fake with complete(system, user, schema=None) -> dict."""
    global _llm
    _llm = llm
    _cache.clear()


def _chat():
    if _llm is not None:
        return _llm
    from fundus.entities import OllamaChat
    return OllamaChat(model=ds.LLM_MODEL, max_tokens=3000, timeout=900)


def _columns(store: Store, d) -> list[tuple[str, list]]:
    """[(table name, profile columns)] of the current version."""
    return [(t["name"], json.loads(t["profile"] or "{}").get("columns", [])) for t in ds._tables(store, d)]


def _cut(v, n: int) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= n else v[:n].rstrip() + " …"


def card(store: Store, d) -> str:
    """Compact card for stage 1."""
    cols = [c["name"] for _, cs in _columns(store, d) for c in cs]
    unit = json.loads(d["annotation"] or "{}").get("unit_of_observation") or ""
    return (f"### dataset:{d['id']} – {d['title']}\n"
            f"Zeitraum: {d['period'] or '?'} · Region: {d['geo'] or '?'} · Herausgeber: {d['publisher'] or '?'}\n"
            f"Beschreibung: {_cut(d['description'], 500)}\n"
            f"Modell-Zusammenfassung: {_cut(d['summary_de'] or d['summary_en'], 350)}\n"
            f"Beobachtungseinheit: {_cut(unit, 150)}\n"
            f"Spalten: {', '.join(map(str, cols[:30]))}{' …' if len(cols) > 30 else ''}")


def detail_card(store: Store, d) -> str:
    """Detailed card for stage 2: own description first, unit of observation, up to 5 tables × 40 columns with meanings."""
    ann = json.loads(d["annotation"] or "{}")
    meaning = ann.get("columns") or {}
    lines = []
    for name, cs in _columns(store, d)[:5]:
        parts = []
        for c in cs[:40]:
            s = str(c["name"])
            if meaning.get(s):
                s += f" = {_cut(meaning[s], 90)}"
            if c.get("examples"):
                s += f" [z. B. {', '.join(_cut(x, 25) for x in c['examples'][:3])}]"
            parts.append(s)
        lines.append(f"Tabelle {name}: " + "; ".join(parts) + (" …" if len(cs) > 40 else ""))
    return (f"### dataset:{d['id']} – {d['title']}\n"
            f"Zeitraum: {d['period'] or '?'} · Region: {d['geo'] or '?'} · Herausgeber: {d['publisher'] or '?'}"
            f"{' · Art: Umschlüsselung' if d['kind'] == 'crosswalk' else ''}\n"
            f"Beschreibung (Mensch, hat Vorrang): {_cut(d['description'], 1500)}\n"
            f"Modell-Zusammenfassung: {_cut(d['summary_de'] or d['summary_en'], 600)}\n"
            f"Beobachtungseinheit: {ann.get('unit_of_observation') or '?'}\n" + "\n".join(lines))


def _ref_id(x) -> int | None:
    m = re.search(r"(\d+)", str(x or ""))
    return int(m.group(1)) if m else None


def _norm(q: str) -> str:
    return " ".join(ds._fold(q).split())


def _visible_rows(store: Store, include_internal: bool, project: str | None, visible_project: str | None) -> dict:
    where, args = ["removed_at IS NULL"], []
    if project:
        where.append("project = ?")
        args.append(ds._slug(project))
    return {d["id"]: d for d in store.db.execute(f"SELECT * FROM dataset WHERE {' AND '.join(where)} ORDER BY id", args)
            if ds._visible(d, include_internal, visible_project or project)}


def ask(store: Store, question: str, include_internal: bool = False, project: str | None = None,
        visible_project: str | None = None, llm=None) -> dict:
    """Answer a free-text question about which datasets exist (see module doc). Returns {question, answer, summary, hits,
    related, candidates, model, seconds, cached}. Takes 20 s – 2 min on the Spark (local model)."""
    question = (question or "").strip()
    if len(question) < 3:
        raise ValueError("question: a free-text question, e.g. 'Haben wir Jobpostings zum Marketing?'")
    rows = _visible_rows(store, include_internal, project, visible_project)
    fp = (len(rows), max((d["updated_at"] or "" for d in rows.values()), default=""), max(rows, default=0))
    key = (_norm(question), bool(include_internal), project or "", visible_project or "", fp)
    if llm is None and key in _cache:
        _cache.move_to_end(key)
        return {**_cache[key], "cached": True}
    model = llm or _chat()
    t0 = time.time()
    if not rows:
        return {"question": question, "answer": "nein", "summary": "Es gibt keine (sichtbaren) Datensätze.", "hits": [],
                "related": [], "candidates": 0, "model": getattr(model, "model", "llm"), "seconds": 0.0, "cached": False}

    # stage 1: candidates and search terms over the whole catalogue (cached prefix)
    catalogue = "\n\n".join(card(store, d) for d in rows.values())
    a1 = model.complete(SYSTEM1 + catalogue, f"Frage: {question}", schema=SCHEMA1)
    a1 = a1 if isinstance(a1, dict) else {}
    terms = [str(t)[:80] for t in (a1.get("terms") or []) if isinstance(t, str) and t.strip()][:10]
    cand = [i for i in (_ref_id(x) for x in (a1.get("candidates") or [])[:MAX_STAGE1 * 2]) if i in rows][:MAX_STAGE1]
    for term in [question] + terms:
        for x in ds.search(store, term, project=project, include_internal=include_internal, limit=3,
                           visible_project=visible_project)["datasets"]:
            if x["id"] in rows:
                cand.append(x["id"])
    cand = list(dict.fromkeys(cand))[:MAX_CANDIDATES]
    if not cand:
        out = {"answer": "nein", "summary": "Im Katalog passt kein Datensatz zur Frage.", "hits": [], "related": []}
    else:
        # stage 2: judgement on detailed cards
        user = f"Frage: {question}\n\n# Steckbriefe\n\n" + "\n\n".join(detail_card(store, rows[i]) for i in cand)
        a2 = model.complete(SYSTEM2, user, schema=SCHEMA2)
        out = _validate(store, a2 if isinstance(a2, dict) else {}, rows, cand)
    res = {"question": question, **out, "terms": terms, "candidates": len(cand),
           "model": getattr(model, "model", "llm"), "seconds": round(time.time() - t0, 1), "cached": False}
    if llm is None:
        _cache[key] = res
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return res


def _info(store: Store, d) -> dict:
    b = ds._brief(store, d)
    return {k: b[k] for k in ("ref", "id", "slug", "title", "rows", "period", "geo", "publisher", "visibility", "project")
            if k in b}


def _validate(store: Store, a: dict, rows: dict, cand: list[int]) -> dict:
    allowed, seen = set(cand), set()
    hits = []
    for h in a.get("hits") or []:
        i = _ref_id(h.get("ref")) if isinstance(h, dict) else None
        if i not in allowed or i in seen:
            continue
        seen.add(i)
        real = {str(c["name"]) for _, cs in _columns(store, rows[i]) for c in cs}
        cols = []
        for c in h.get("columns") or []:
            c = str(c).strip()
            c = c.split(":", 1)[1].strip() if c not in real and ":" in c else c     # "table: column" → column
            for part in ([c] if c in real else [p.strip() for p in c.split(",")]):
                if part in real and part not in cols:
                    cols.append(part)
        hits.append({**_info(store, rows[i]), "fit": "direkt" if h.get("fit") == "direkt" else "teilweise",
                     "why": _cut(h.get("why"), 400), "columns": cols[:MAX_COLUMNS]})
    related = []
    for r in a.get("related") or []:
        i = _ref_id(r.get("ref")) if isinstance(r, dict) else None
        if i not in allowed or i in seen:
            continue
        seen.add(i)
        related.append({**_info(store, rows[i]), "why": _cut(r.get("why"), 300)})
        if len(related) >= MAX_RELATED:
            break
    # the answer follows the hits that survived validation, not the model's own label
    answer = "ja" if any(h["fit"] == "direkt" for h in hits) else "teilweise" if hits else "nein"
    return {"answer": answer, "summary": _cut(a.get("summary"), 600) or None, "hits": hits, "related": related}
