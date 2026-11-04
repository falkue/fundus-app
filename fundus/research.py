"""Research registry (3 Nov 2026): series, hypotheses (strictly pre-registered), findings (exploratory), results,
interpretations, evidence and links.

    create_series / update_series / series_get / series_list
    create_hypothesis → preregister (freezes: timestamp + sha256) → start → add_result → set_interpretation
    add_deviation (append-only, after the freeze) · withdraw · revise (new hypothesis, old one stays, linked 'revises')
    create_finding · add_evidence · link · derive_hypothesis(finding) · get_hypothesis / get_finding
    search · board · evidence_map · worklist · overview · research_for_dataset

Falk's rules (binding): a frozen pre-registration can never be overwritten (also enforced by SQLite triggers); changes
are deviations with what, why, when and whether data had been seen. Every hypothesis is recorded as soon as it is
stated. Visibility is internal by default; public only on explicit request. All verdicts count the same – nothing
ranks or hides 'not supported' or 'inconclusive'. A result without a complete interpretation stays on the work list.
A finding is always exploratory and can never become a confirmed hypothesis; a hypothesis can be derived from it.
No model writes interpretations (authors naming a local model are refused).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from fundus import config as _config
from fundus.store import Store, slug as _slug

VERDICTS = ("supported", "partly_supported", "not_supported", "inconclusive")
VERDICT_ALIASES = {"supported": "supported", "partly supported": "partly_supported", "partially supported": "partly_supported",
                   "partly_supported": "partly_supported", "partially_supported": "partly_supported",
                   "not supported": "not_supported", "not_supported": "not_supported", "rejected": "not_supported",
                   "inconclusive": "inconclusive", "unentschieden": "inconclusive", "gestützt": "supported",
                   "teilweise gestützt": "partly_supported", "nicht gestützt": "not_supported"}
STATUSES = ("idea", "preregistered", "running", "result", "revised", "withdrawn")
FINDING_STATUSES = ("draft", "recorded", "revised", "withdrawn")
KINDS = ("quantitative", "qualitative", "mixed")
STRENGTHS = ("single_observation", "recurring_pattern", "saturated", "quantified")
EVIDENCE_KINDS = ("dataset", "observation", "document", "exhibit", "photo", "video", "quote", "note", "file")
RELATIONS = ("sharpens", "replicates", "contradicts", "extends", "derived_from", "competes_with", "revises")
TIMINGS = ("before_analysis", "after_data_seen")
SECTIONS = ("summary", "explanation", "alternatives", "limits", "theory", "relations", "next")
COMPLETE_SECTIONS = ("summary", "explanation", "limits", "theory")
PREREG_FIELDS = ("statement", "direction", "unit", "sample", "measures", "data", "method", "controls", "decision_rule",
                 "robustness_plan", "extra")
PREREG_REQUIRED = ("statement", "direction", "measures", "data", "method", "decision_rule")
IDEA_DAYS = 14
# authors that are refused for interpretations: no model writes them (Falk, 3 Nov 2026)
MODEL_AUTHORS = re.compile(r"gemma|qwen|llama|deepseek|nemotron|mistral|ollama|importer|gemini", re.I)

LABELS = {
    "verdict": {"supported": "gestützt", "partly_supported": "teilweise gestützt", "not_supported": "nicht gestützt",
                "inconclusive": "unentschieden"},
    "status": {"idea": "Idee", "preregistered": "präregistriert", "running": "läuft", "result": "Ergebnis",
               "revised": "überarbeitet", "withdrawn": "zurückgezogen"},
    "finding_status": {"draft": "Entwurf", "recorded": "erfasst", "revised": "überarbeitet", "withdrawn": "zurückgezogen"},
    "kind": {"quantitative": "quantitativ", "qualitative": "qualitativ", "mixed": "gemischt"},
    "strength": {"single_observation": "Einzelbeobachtung", "recurring_pattern": "wiederkehrendes Muster",
                 "saturated": "gesättigt", "quantified": "quantifiziert"},
    "relation": {"sharpens": "schärft", "replicates": "repliziert", "contradicts": "widerspricht", "extends": "erweitert",
                 "derived_from": "entstanden aus", "competes_with": "konkurriert mit", "revises": "überarbeitet"},
    "timing": {"before_analysis": "vor der Analyse", "after_data_seen": "nach Sicht auf die Daten"},
    "interpretation": {"missing": "Interpretation fehlt", "incomplete": "Interpretation unvollständig",
                       "complete": "Interpretation vollständig"},
    "section": {"summary": "Was das Ergebnis sagt", "explanation": "Warum es so ausfiel", "alternatives": "Andere Erklärungen",
                "limits": "Grenzen (Identifikation, Daten, Messung)", "theory": "Bedeutung für die Theorie",
                "relations": "Bezug zu anderen Hypothesen und Findings", "next": "Nächste Schritte"},
    "flag": {"interpretation_missing": "Interpretation fehlt", "data_newer_than_result": "Daten neuer als Ergebnis",
             "prereg_after_result": "Präregistrierung nach dem Ergebnis", "prereg_time_unknown": "Zeitpunkt der Präregistrierung unbekannt",
             "prereg_missing": "Ergebnis ohne Präregistrierung", "prereg_incomplete": "Präregistrierung unvollständig",
             "prereg_hash_mismatch": "Prüfsumme der Präregistrierung stimmt nicht", "imported_partial": "nur aus Konzept/Übersicht importiert",
             "sample_only": "nur Stichprobenlauf", "partial_data": "Daten unvollständig", "idea_without_prereg": "Idee ohne Präregistrierung",
             "deviation_after_data": "Abweichung nach Sicht auf die Daten"},
}


# ------------------------------------------------------------------ small helpers
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _j(v) -> str | None:
    if v is None or v == [] or v == {}:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


def _l(s, default=None):
    if s in (None, ""):
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _txt(v) -> str | None:
    """Text field: dicts/lists as JSON, strings stripped, empty → None."""
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return _j(v)
    v = str(v).strip()
    return v or None


def _commit(store: Store) -> None:
    if not getattr(store, "_research_batch", False):
        store.db.commit()


def _actor(store: Store, actor: str | None) -> str:
    return actor or getattr(store, "actor", None) or "api"


def portal_url(kind: str, iid: int) -> str:
    base = _config.public_url().rstrip("/")
    return f"{base}/portal/#forschung?{'h' if kind == 'hypothesis' else 'f'}={iid}"


def norm_verdict(v) -> str:
    k = str(v or "").strip().lower().replace("-", " ")
    k = re.sub(r"\s*\(.*\)$", "", k).strip()
    if k in VERDICT_ALIASES:
        return VERDICT_ALIASES[k]
    if k.replace(" ", "_") in VERDICTS:
        return k.replace(" ", "_")
    raise ValueError(f"verdict must be one of {', '.join(VERDICTS)} (got {v!r})")


def norm_direction(v) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip().lower()
    table = {"+": "+", "positive": "+", "pos": "+", "up": "+", "-": "−", "−": "−", "–": "−", "negative": "−", "neg": "−",
             "down": "−", "≠": "≠", "!=": "≠", "two-sided": "≠", "two_sided": "≠", "differs": "≠", "none": "none",
             "qualitative": "none", "n/a": "none", "keine": "none", "qualitativ": "none"}
    if s in table:
        return table[s]
    raise ValueError("direction: + | − | ≠ | none (qualitative)")


def interpretation_state(sec: dict | None) -> str:
    if not sec or not any((sec.get(k) or "").strip() for k in SECTIONS):
        return "missing"
    return "complete" if all((sec.get(k) or "").strip() for k in COMPLETE_SECTIONS) else "incomplete"


def _visible(row, include_internal: bool, project: str | None) -> bool:
    return include_internal or row["visibility"] != "internal" or bool(project and row["project"] == _slug(project))


def _series_num(slug: str) -> str | None:
    m = re.search(r"(\d+)$", slug or "")
    return m.group(1) if m else None


def _file_sha(path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ------------------------------------------------------------------ lookups
def series_row(store: Store, ref):
    if ref in (None, ""):
        return None
    if isinstance(ref, int) or re.fullmatch(r"(?:series:)?\d+", str(ref).strip()):
        sid = int(str(ref).split(":")[-1])
        r = store.db.execute("SELECT * FROM research_series WHERE id = ?", (sid,)).fetchone()
        if r:
            return r
        r = store.db.execute("SELECT * FROM research_series WHERE slug = ?", (f"serie-{sid}",)).fetchone()
    else:
        s = str(ref).strip().removeprefix("series:")
        r = store.db.execute("SELECT * FROM research_series WHERE slug = ? OR lower(title) = lower(?)", (_slug(s), s)).fetchone()
        if not r and re.fullmatch(r"(?:series|serie)[- ]?\d+", s, re.I):
            r = store.db.execute("SELECT * FROM research_series WHERE slug = ?", (f"serie-{_series_num(s)}",)).fetchone()
    if not r:
        raise LookupError(f"research series {ref} not found")
    return r


def _by_code(store: Store, table: str, code: str, series=None):
    sql = f"SELECT * FROM {table} WHERE (lower(code) = lower(?) OR EXISTS (SELECT 1 FROM json_each(coalesce(aliases, '[]')) " \
          f"WHERE lower(value) = lower(?)))"
    args: list = [code, code]
    if series is not None:
        sql += " AND series_id = ?"
        args.append(series_row(store, series)["id"])
    rows = store.db.execute(sql + " ORDER BY (lower(code) = lower(?)) DESC, id", (*args, code)).fetchall()
    exact = [r for r in rows if r["code"].lower() == code.lower()]
    rows = exact or rows
    if len(rows) > 1:
        raise LookupError(f"{code} is ambiguous ({len(rows)} matches) – pass series= or the numeric ref "
                          f"({', '.join(f'{table}:{r[0]}' for r in rows[:5])})")
    return rows[0] if rows else None


def hyp_row(store: Store, ref, series=None):
    if isinstance(ref, int) or re.fullmatch(r"(?:hypothesis:)?\d+", str(ref).strip()):
        r = store.db.execute("SELECT * FROM hypothesis WHERE id = ?", (int(str(ref).split(":")[-1]),)).fetchone()
    else:
        r = _by_code(store, "hypothesis", str(ref).strip().removeprefix("hypothesis:"), series)
    if not r:
        raise LookupError(f"hypothesis {ref} not found")
    return r


def finding_row(store: Store, ref, series=None):
    if isinstance(ref, int) or re.fullmatch(r"(?:finding:)?\d+", str(ref).strip()):
        r = store.db.execute("SELECT * FROM finding WHERE id = ?", (int(str(ref).split(":")[-1]),)).fetchone()
    else:
        r = _by_code(store, "finding", str(ref).strip().removeprefix("finding:"), series)
    if not r:
        raise LookupError(f"finding {ref} not found")
    return r


def _node(store: Store, ref) -> tuple[str, object]:
    """('hypothesis' | 'finding', row) for 'hypothesis:N', 'finding:N', a code (H… / F…)."""
    s = str(ref).strip()
    if s.startswith("finding:") or re.match(r"^F[\d-]", s):
        return "finding", finding_row(store, s)
    return "hypothesis", hyp_row(store, s)


def _prereg(store: Store, hid: int):
    return store.db.execute("SELECT * FROM hypothesis_prereg WHERE hypothesis_id = ?", (hid,)).fetchone()


def _results(store: Store, hid: int) -> list:
    # current = the newest full run (a sample/pilot run only counts when there is no full run); duplicates never count
    return store.db.execute("SELECT * FROM hypothesis_result WHERE hypothesis_id = ? AND duplicate_of IS NULL "
                            "ORDER BY sample_run ASC, at DESC, id DESC", (hid,)).fetchall()


def _norm_effect(t) -> str:
    t = str(t or "")
    for a, b in (("“", '"'), ("”", '"'), ("„", '"'), ("«", '"'), ("»", '"'), ("‘", "'"), ("’", "'"), ("`", "'"),
                 ("−", "-")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _r6(v):
    return None if v is None else round(float(v), 6)


def result_key(hid, verdict, effect, estimate, ci_low, ci_high, n, n_text, sample_run) -> tuple:
    """Content key of a result: two results with the same key are the same result (whatever their source or time)."""
    nn = str(int(n)) if n not in (None, "") else re.sub(r"\s+", " ", str(n_text or "")).strip()
    return (int(hid), norm_verdict(verdict), _norm_effect(effect), _r6(estimate), _r6(ci_low), _r6(ci_high), nn,
            int(bool(sample_run)))


def _row_key(r) -> tuple:
    return result_key(r["hypothesis_id"], r["verdict"], r["effect"], r["estimate"], r["ci_low"], r["ci_high"], r["n"],
                      r["n_text"], r["sample_run"])


def _current_result(store: Store, hid: int):
    r = _results(store, hid)
    return r[0] if r else None


def _interp(store: Store, owner_kind: str, owner_id: int):
    return store.db.execute("SELECT * FROM interpretation WHERE owner_kind = ? AND owner_id = ? ORDER BY id DESC LIMIT 1",
                            (owner_kind, owner_id)).fetchone()


# ------------------------------------------------------------------ series
def create_series(store: Store, slug: str, title: str, programme: str | None = None, project: str | None = None,
                  summary: str | None = None, proposal_path: str | None = None, theory_cards: list | None = None,
                  visibility: str = "internal", actor: str | None = None, source: str = "registered") -> dict:
    s = _slug(slug or title)
    if not s or not (title or "").strip():
        raise ValueError("a series needs slug and title")
    if visibility not in ("public", "internal"):
        raise ValueError("visibility: public | internal")
    if store.db.execute("SELECT 1 FROM research_series WHERE slug = ?", (s,)).fetchone():
        raise ValueError(f"series {s} exists – use action='update'")
    now = _now()
    cur = store.db.execute(
        "INSERT INTO research_series(slug, title, programme, project, summary, proposal_path, theory_cards, visibility, source, "
        "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (s, title.strip(), programme, _slug(project) if project else None, summary, proposal_path, _j(_cards(theory_cards)),
         visibility, source, _actor(store, actor), now, now))
    store.log("research_series", cur.lastrowid, "insert", {"slug": s})
    _commit(store)
    return series_get(store, s)


def _cards(cards) -> list | None:
    if not cards:
        return None
    out = []
    for i, c in enumerate(cards, 1):
        if isinstance(c, str):
            c = {"name": c}
        out.append({"id": str(c.get("id") or f"T{i}"), "name": c.get("name") or c.get("id"), "claim": c.get("claim"),
                    "scholars": [{"name": s.get("name") if isinstance(s, dict) else s,
                                  "works": (s.get("works") or []) if isinstance(s, dict) else []} for s in c.get("scholars") or []]})
    return out


SERIES_EDITABLE = ("title", "programme", "project", "summary", "proposal_path", "theory_cards", "visibility")


def update_series(store: Store, ref, changes: dict, actor: str | None = None) -> dict:
    r = series_row(store, ref)
    bad = set(changes or {}) - set(SERIES_EDITABLE)
    if bad or not changes:
        raise ValueError(f"editable: {', '.join(SERIES_EDITABLE)}")
    vals = {}
    for k, v in changes.items():
        if k == "theory_cards":
            v = _j(_cards(v))
        elif k == "project":
            v = _slug(v) if v else None
        elif k == "visibility" and v not in ("public", "internal"):
            raise ValueError("visibility: public | internal")
        vals[k] = v
    store.db.execute(f"UPDATE research_series SET {', '.join(f'{k} = ?' for k in vals)}, updated_at = ? WHERE id = ?",
                     (*vals.values(), _now(), r["id"]))
    store.log("research_series", r["id"], "update", {"fields": sorted(vals)})
    _commit(store)
    return series_get(store, r["id"])


def _series_brief(r) -> dict | None:
    if r is None:
        return None
    return {"id": r["id"], "slug": r["slug"], "title": r["title"], "project": r["project"], "visibility": r["visibility"]}


def series_list(store: Store, include_internal: bool = True, project: str | None = None) -> list[dict]:
    b = {x["slug"]: x for x in board(store, include_internal, project)}
    out = []
    for r in store.db.execute("SELECT * FROM research_series ORDER BY id"):
        if not _visible(r, include_internal, project):
            continue
        out.append({**_series_brief(r), "programme": r["programme"], "summary": (r["summary"] or "")[:400] or None,
                    "counts": b.get(r["slug"], {}).get("counts"), "verdicts": b.get(r["slug"], {}).get("verdicts"),
                    "findings": b.get(r["slug"], {}).get("findings", 0)})
    return out


def series_get(store: Store, ref, include_internal: bool = True, project: str | None = None) -> dict:
    r = series_row(store, ref)
    if not _visible(r, include_internal, project):
        raise PermissionError(f"series {r['slug']} is internal; pass include_internal=true or its project")
    ctx = _ctx(store)
    hyps = [_brief_h(store, h, ctx) for h in store.db.execute(
        "SELECT * FROM hypothesis WHERE series_id = ? ORDER BY id", (r["id"],)) if _visible(h, include_internal, project)]
    hyps.sort(key=lambda x: _code_key(x["code"]))
    finds = [_brief_f(store, f, ctx) for f in store.db.execute(
        "SELECT * FROM finding WHERE series_id = ? ORDER BY id", (r["id"],)) if _visible(f, include_internal, project)]
    finds.sort(key=lambda x: _code_key(x["code"]))
    b = next((x for x in board(store, include_internal, project) if x["slug"] == r["slug"]), {})
    return {**_series_brief(r), "programme": r["programme"], "summary": r["summary"], "proposal_path": r["proposal_path"],
            "theory_cards": _l(r["theory_cards"], []), "source": r["source"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "counts": b.get("counts"), "verdicts": b.get("verdicts"),
            "hypotheses": hyps, "findings": finds}


def _code_key(code: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", code or "")]


# ------------------------------------------------------------------ hypotheses
def _next_code(store: Store, table: str, prefix: str, series_id: int | None, series_slug: str | None) -> str:
    num = _series_num(series_slug) if series_slug else None
    base = f"{prefix}{num}." if num else (f"{prefix}{series_id}." if series_id else f"{prefix}-")
    n = 0
    for (c,) in store.db.execute(f"SELECT code FROM {table} WHERE coalesce(series_id, 0) = ?", (series_id or 0,)):
        m = re.fullmatch(re.escape(base) + r"(\d+)", c)
        if m:
            n = max(n, int(m.group(1)))
    return f"{base}{n + 1}"


def _check_tags(store: Store, tags) -> dict | None:
    if not tags:
        return None
    if not isinstance(tags, dict):
        raise ValueError("tags: {industry: [...], topic: [...], region: [...]} (values from fundus_vocab)")
    known = store.vocab_values()
    out = {}
    for facet, vals in tags.items():
        vals = [vals] if isinstance(vals, str) else list(vals or [])
        for v in vals:
            if (facet, v) not in known:
                raise ValueError(f"unknown tag {facet}={v} – use fundus_vocab")
        if vals:
            out[facet] = vals
    return out or None


def create_hypothesis(store: Store, statement: str, code: str | None = None, series=None, kind: str = "quantitative",
                      title: str | None = None, direction=None, theory_refs: list | None = None, project: str | None = None,
                      tags: dict | None = None, visibility: str = "internal", prereg: dict | None = None,
                      derived_from_finding=None, actor: str | None = None, source: str = "registered",
                      imported_partial: bool = False, aliases: list | None = None, meta: dict | None = None,
                      status: str = "idea", created_at: str | None = None, revises: int | None = None) -> dict:
    """Record a hypothesis as soon as it is stated (status idea). prereg = draft fields (frozen only by preregister)."""
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("a hypothesis needs a statement")
    if kind not in KINDS:
        raise ValueError(f"kind: {' | '.join(KINDS)}")
    if visibility not in ("public", "internal"):
        raise ValueError("visibility: public | internal")
    if status not in STATUSES:
        raise ValueError(f"status: {' | '.join(STATUSES)}")
    s = series_row(store, series) if series not in (None, "") else None
    sid = s["id"] if s else None
    code = (code or "").strip() or _next_code(store, "hypothesis", "H", sid, s["slug"] if s else None)
    if store.db.execute("SELECT 1 FROM hypothesis WHERE coalesce(series_id, 0) = ? AND lower(code) = lower(?)",
                        (sid or 0, code)).fetchone():
        raise ValueError(f"hypothesis {code} exists in this series")
    fid = finding_row(store, derived_from_finding)["id"] if derived_from_finding not in (None, "") else None
    proj = _slug(project) if project else (s["project"] if s else None)
    now = _now()
    cur = store.db.execute(
        "INSERT INTO hypothesis(code, series_id, kind, title, statement, direction, theory_refs, status, visibility, project, tags, "
        "aliases, derived_from_finding, revises, source, imported_partial, meta, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, sid, kind, (title or "").strip() or None, statement, norm_direction(direction), _j(theory_refs or None), status,
         visibility, proj, _j(_check_tags(store, tags)), _j(aliases or None), fid, revises, source, int(bool(imported_partial)),
         _j(meta), _actor(store, actor), created_at or now, now))
    hid = cur.lastrowid
    store.log("hypothesis", hid, "insert", {"code": code, "source": source})
    if prereg:
        save_prereg_draft(store, hid, prereg)
    if fid:
        _link_ids(store, "hypothesis", hid, "finding", fid, "derived_from", None, actor)
    _index_h(store, hid)
    _commit(store)
    return get_hypothesis(store, hid)


def _norm_data(v):
    """Planned data: list of 'dataset:N vK' / {ref, version, note} – or free text (material for qualitative work)."""
    if v in (None, "", []):
        return None
    if isinstance(v, str):
        if re.fullmatch(r"\s*(dataset:\d+(\s*v\d+)?[\s,;]*)+", v):
            return [{"ref": r, **({"version": ver} if ver else {})} for r, ver in parse_dataset_refs(v)]
        return v.strip()
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, dict):
            out.append({k: x.get(k) for k in ("ref", "version", "note", "material") if x.get(k) not in (None, "")})
        else:
            refs = parse_dataset_refs(str(x))
            if refs and re.fullmatch(r"\s*dataset:\d+(\s*v\d+)?\s*", str(x)):
                out.append({"ref": refs[0][0], **({"version": refs[0][1]} if refs[0][1] else {})})
            else:
                out.append({"note": str(x)})
    return out or None


def parse_dataset_refs(text: str) -> list[tuple[str, int | None]]:
    """[('dataset:47', 3), ('dataset:100', None)] from 'dataset:47 v3, dataset:100' (first version mention wins)."""
    out, seen = [], set()
    for m in re.finditer(r"dataset:(\d+)(?:\s*\(?v(\d+)\b)?", text or ""):
        ref = f"dataset:{m.group(1)}"
        if ref not in seen:
            seen.add(ref)
            out.append((ref, int(m.group(2)) if m.group(2) else None))
    return out


def _prereg_values(fields: dict) -> dict:
    vals = {}
    for k in PREREG_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if k == "direction":
            v = norm_direction(v)
        elif k == "data":
            nd = _norm_data(v)
            v = nd if isinstance(nd, str) or nd is None else _j(nd)
        elif k == "extra":
            v = _j(v) if isinstance(v, (dict, list)) else _txt(v)
        else:
            v = _txt(v)
        vals[k] = v
    return vals


def save_prereg_draft(store: Store, ref, fields: dict) -> dict:
    h = hyp_row(store, ref)
    bad = set(fields or {}) - set(PREREG_FIELDS) - {"text"}
    if bad:
        raise ValueError(f"unknown pre-registration fields {sorted(bad)}; fields: {', '.join(PREREG_FIELDS)}")
    p = _prereg(store, h["id"])
    if p and p["frozen_at"]:
        raise PermissionError(f"the pre-registration of {h['code']} is frozen ({p['frozen_at']}) – record a deviation instead")
    vals = _prereg_values(fields)
    if "text" in fields:
        vals["text"] = _txt(fields["text"])
    if p:
        if vals:
            store.db.execute(f"UPDATE hypothesis_prereg SET {', '.join(f'{k} = ?' for k in vals)} WHERE hypothesis_id = ?",
                             (*vals.values(), h["id"]))
    else:
        cols = ["hypothesis_id", "created_at", *vals]
        store.db.execute(f"INSERT INTO hypothesis_prereg({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         (h["id"], _now(), *vals.values()))
    _commit(store)
    return dict(_prereg(store, h["id"]))


def canonical_prereg(code: str, p: dict) -> str:
    """The canonical JSON whose sha256 seals a pre-registration."""
    body = {"hypothesis": code, **{k: p.get(k) for k in PREREG_FIELDS}, "text": p.get("text"), "frozen_at": p.get("frozen_at"),
            "source": p.get("source")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _missing_prereg(p: dict) -> list[str]:
    return [k for k in PREREG_REQUIRED if not (p.get(k) or "").strip()]


def preregister(store: Store, ref, fields: dict | None = None, actor: str | None = None, series=None) -> dict:
    """Freeze the pre-registration (draft + fields): refused when statement, direction, measures, data/material, method
    or decision rule is missing. Afterwards nothing of it can change – only deviations are appended."""
    h = hyp_row(store, ref, series)
    if h["status"] in ("withdrawn", "revised"):
        raise ValueError(f"{h['code']} is {h['status']}")
    p = _prereg(store, h["id"])
    if p and p["frozen_at"]:
        raise PermissionError(f"{h['code']} is already pre-registered (frozen {p['frozen_at']}, sha256 {p['sha256'][:12]}…)")
    if fields:
        save_prereg_draft(store, h["id"], fields)
    p = dict(_prereg(store, h["id"]) or {})
    p.setdefault("statement", None)
    if not p.get("statement"):
        p["statement"] = h["statement"]
    if not p.get("direction") and h["direction"]:
        p["direction"] = h["direction"]
    if h["kind"] == "qualitative" and not p.get("direction"):
        p["direction"] = "none"
    miss = _missing_prereg(p)
    if miss:
        raise ValueError(f"pre-registration of {h['code']} refused – missing: {', '.join(miss)} "
                         f"(required: statement, direction, measures/operationalisation, data or material, method, decision rule)")
    return _freeze(store, h, p, _now(), "registered", "registered", actor)


def _freeze(store: Store, h, p: dict, frozen_at: str, time_source: str, source: str, actor: str | None) -> dict:
    p = {**p, "frozen_at": frozen_at, "source": source}
    sha = hashlib.sha256(canonical_prereg(h["code"], p).encode()).hexdigest()
    vals = {k: p.get(k) for k in (*PREREG_FIELDS, "text")}
    if _prereg(store, h["id"]):
        store.db.execute(f"UPDATE hypothesis_prereg SET {', '.join(f'{k} = ?' for k in vals)}, frozen_at = ?, sha256 = ?, "
                         f"time_source = ?, source = ? WHERE hypothesis_id = ?",
                         (*vals.values(), frozen_at, sha, time_source, source, h["id"]))
    else:
        store.db.execute(f"INSERT INTO hypothesis_prereg(hypothesis_id, {', '.join(vals)}, frozen_at, sha256, time_source, source, "
                         f"created_at) VALUES ({', '.join('?' * (len(vals) + 6))})",
                         (h["id"], *vals.values(), frozen_at, sha, time_source, source, _now()))
    upd = {"status": "preregistered"} if h["status"] == "idea" else {}
    if h["direction"] is None and p.get("direction"):
        upd["direction"] = p["direction"]
    if upd:
        store.db.execute(f"UPDATE hypothesis SET {', '.join(f'{k} = ?' for k in upd)}, updated_at = ? WHERE id = ?",
                         (*upd.values(), _now(), h["id"]))
    store.log("hypothesis", h["id"], "prereg", {"frozen_at": frozen_at, "sha256": sha, "source": source})
    _index_h(store, h["id"])
    _commit(store)
    return get_hypothesis(store, h["id"])


def start(store: Store, ref, actor: str | None = None) -> dict:
    h = hyp_row(store, ref)
    p = _prereg(store, h["id"])
    if not (p and p["frozen_at"]):
        raise PermissionError(f"{h['code']} is not pre-registered – preregister before any estimation or analysis")
    if h["status"] in ("withdrawn", "revised"):
        raise ValueError(f"{h['code']} is {h['status']}")
    store.db.execute("UPDATE hypothesis SET status = 'running', updated_at = ? WHERE id = ?", (_now(), h["id"]))
    store.log("hypothesis", h["id"], "start")
    _commit(store)
    return get_hypothesis(store, h["id"])


def effective_prereg(store: Store, hid: int) -> dict | None:
    """The frozen pre-registration with the deviations applied in order (for display: 'as amended')."""
    p = _prereg(store, hid)
    if not p:
        return None
    out = {k: p[k] for k in (*PREREG_FIELDS, "text")}
    for d in store.db.execute("SELECT * FROM hypothesis_deviation WHERE hypothesis_id = ? ORDER BY id", (hid,)):
        if d["field"] in out:
            out[d["field"]] = d["after"]
    return out


def add_deviation(store: Store, ref, field: str, after, reason: str, timing: str, before=None, at: str | None = None,
                  actor: str | None = None) -> dict:
    """Append a deviation from the frozen plan: what (field, before → after), why, when, and whether data had been seen."""
    h = hyp_row(store, ref)
    p = _prereg(store, h["id"])
    if not (p and p["frozen_at"]):
        raise ValueError(f"{h['code']} has no frozen pre-registration – change the draft instead (prereg)")
    if timing not in TIMINGS:
        raise ValueError("timing: before_analysis | after_data_seen")
    if not (reason or "").strip():
        raise ValueError("a deviation needs a reason")
    if not (field or "").strip():
        raise ValueError(f"field: which part changes (e.g. {', '.join(PREREG_FIELDS[:7])} or free text)")
    if before is None:
        before = (effective_prereg(store, h["id"]) or {}).get(field)
    store.db.execute("INSERT INTO hypothesis_deviation(hypothesis_id, at, field, before, after, reason, timing, by) "
                     "VALUES (?,?,?,?,?,?,?,?)", (h["id"], at or _now(), field.strip(), _txt(before), _txt(after),
                                                  reason.strip(), timing, _actor(store, actor)))
    store.log("hypothesis", h["id"], "deviation", {"field": field, "timing": timing})
    if field.strip() == "direction" and after not in (None, ""):   # the list shows hypothesis.direction: follow the plan
        store.db.execute("UPDATE hypothesis SET direction = ? WHERE id = ?", (_txt(after), h["id"]))
    _index_h(store, h["id"])
    _commit(store)
    return get_hypothesis(store, h["id"])


def _version_at(store: Store, did: int, at: str | None) -> int | None:
    if at:
        r = store.db.execute("SELECT max(n) FROM dataset_version WHERE dataset_id = ? AND created_at <= ?", (did, at)).fetchone()
        if r and r[0]:
            return r[0]
    r = store.db.execute("SELECT current_version FROM dataset WHERE id = ?", (did,)).fetchone()
    return r[0] if r else None


def norm_inputs(store: Store, inputs, at: str | None = None) -> list[dict] | None:
    """[{ref: 'dataset:N', version, note}] – a dataset without a version gets the version that was current at `at`."""
    if not inputs:
        return None
    out = []
    for x in inputs if isinstance(inputs, list) else [inputs]:
        if isinstance(x, dict):
            d = {k: x.get(k) for k in ("ref", "version", "note") if x.get(k) not in (None, "")}
        else:
            s = str(x).strip()
            refs = parse_dataset_refs(s)
            if refs:
                rest = re.sub(r"dataset:\d+(\s*\(?v\d+\)?)?", "", s).strip(" ()–-,;")
                d = {"ref": refs[0][0], **({"version": refs[0][1]} if refs[0][1] else {}), **({"note": rest} if rest else {})}
            else:
                d = {"note": s}
        m = re.fullmatch(r"dataset:(\d+)", str(d.get("ref") or ""))
        if m:
            did = int(m.group(1))
            if store.db.execute("SELECT 1 FROM dataset WHERE id = ?", (did,)).fetchone():
                if not d.get("version"):
                    d["version"] = _version_at(store, did, at)
            else:
                d["missing"] = True
        out.append(d)
    return out


def _norm_scripts(scripts) -> list[dict] | None:
    if not scripts:
        return None
    out = []
    for s in scripts if isinstance(scripts, list) else [scripts]:
        d = dict(s) if isinstance(s, dict) else {"path": str(s)}
        if d.get("path") and not d.get("sha256"):
            sha = _file_sha(Path(d["path"]).expanduser())
            if sha:
                d["sha256"] = sha
        out.append(d)
    return out


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"not a number: {v!r}")


def add_result(store: Store, ref, verdict, effect: str | None = None, estimate=None, ci_low=None, ci_high=None,
               ci_text: str | None = None, unit: str | None = None, n=None, n_text: str | None = None,
               robustness: str | None = None, caveats: str | None = None, method_doc: str | None = None, scripts=None,
               run_log: str | None = None, environment: str | None = None, inputs=None, outputs=None, measures=None,
               sample_run: bool = False, partial_data: str | None = None, at: str | None = None, actor: str | None = None,
               source: str = "registered", interpretation: dict | None = None, skipped: bool = False,
               evidence: list | None = None) -> dict:
    """Record a result (a rerun adds a new row; the newest full run is current). Every verdict counts the same.
    Registered hypotheses need a frozen pre-registration first."""
    h = hyp_row(store, ref)
    v = norm_verdict(verdict)
    if h["status"] == "withdrawn":
        raise ValueError(f"{h['code']} is withdrawn")
    p = _prereg(store, h["id"])
    if source == "registered" and not (p and p["frozen_at"]):
        raise PermissionError(f"{h['code']} is not pre-registered – a result needs a frozen pre-registration "
                              f"(for an exploratory result record a finding instead)")
    if isinstance(n, str) and not n.strip().isdigit():
        n_text, n = n_text or n, None
    key = result_key(h["id"], v, _txt(effect), _num(estimate), _num(ci_low), _num(ci_high), n, _txt(n_text), sample_run)
    last = _current_result(store, h["id"])
    if last is not None and _row_key(last) == key:
        out = get_hypothesis(store, h["id"])
        out.update({"unchanged": True, "result": f"result:{last['id']}",
                    "note": "identisch mit dem aktuellen Ergebnis – nicht doppelt angelegt"})
        return out
    at = at or _now()
    ins = norm_inputs(store, inputs, at)
    cur = store.db.execute(
        "INSERT INTO hypothesis_result(hypothesis_id, verdict, effect, estimate, ci_low, ci_high, ci_text, unit, n, n_text, robustness, "
        "caveats, method_doc, scripts, run_log, environment, inputs, outputs, measures, sample_run, partial_data, skipped, at, by, "
        "source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h["id"], v, _txt(effect), _num(estimate), _num(ci_low), _num(ci_high), _txt(ci_text), _txt(unit),
         int(n) if n not in (None, "") else None, _txt(n_text), _txt(robustness), _txt(caveats), _txt(method_doc),
         _j(_norm_scripts(scripts)), _txt(run_log), _txt(environment), _j(ins), _j(outputs or None), _j(measures or None),
         int(bool(sample_run)), _txt(partial_data), int(bool(skipped)), at, _actor(store, actor), source, _now()))
    rid = cur.lastrowid
    if h["status"] != "revised":
        full = store.db.execute("SELECT 1 FROM hypothesis_result WHERE hypothesis_id = ? AND sample_run = 0 "
                                "AND duplicate_of IS NULL LIMIT 1",
                                (h["id"],)).fetchone()
        # a sample/pilot run alone leaves the hypothesis 'running'; the first full run makes it 'result'
        new = "result" if full else ("running" if h["status"] in ("idea", "preregistered", "running") else h["status"])
        store.db.execute("UPDATE hypothesis SET status = ?, updated_at = ? WHERE id = ?", (new, _now(), h["id"]))
    _log_dataset_use(store, ins, h["code"])
    for e in evidence or []:
        add_evidence(store, f"result:{rid}", **e)
    store.log("hypothesis", h["id"], "result", {"result": rid, "verdict": v, "source": source})
    if interpretation:
        set_interpretation(store, "result", rid, interpretation, author=actor)
    _index_h(store, h["id"])
    _commit(store)
    return get_hypothesis(store, h["id"])


def _log_dataset_use(store: Store, inputs, code: str) -> None:
    """Usage log of datasets (dataset_use, tool 'hypothesis'); written directly so a dry run can roll it back."""
    for x in inputs or []:
        m = re.fullmatch(r"dataset:(\d+)", str(x.get("ref") or ""))
        if m and not x.get("missing"):
            store.db.execute("INSERT INTO dataset_use(dataset_id, version, tool, actor, detail) VALUES (?,?,?,?,?)",
                             (int(m.group(1)), x.get("version"), "hypothesis", getattr(store, "actor", None), code))


def _owner(store: Store, owner_kind: str, owner_ref) -> tuple[str, int, object]:
    """('hypothesis_result' | 'finding', id, hypothesis row or finding row)."""
    if owner_kind in ("result", "hypothesis_result"):
        r = store.db.execute("SELECT * FROM hypothesis_result WHERE id = ?", (int(str(owner_ref).split(":")[-1]),)).fetchone()
        if not r:
            raise LookupError(f"result {owner_ref} not found")
        return "hypothesis_result", r["id"], hyp_row(store, r["hypothesis_id"])
    if owner_kind == "finding":
        f = finding_row(store, owner_ref)
        return "finding", f["id"], f
    raise ValueError("owner: result | finding")


def set_interpretation(store: Store, owner_kind: str, owner_ref, sections: dict, author: str | None = None,
                       source: str = "registered") -> dict:
    """A new version of the interpretation (newest current). Sections: summary, explanation, alternatives, limits, theory,
    relations, next (markdown). Complete = summary + explanation + limits + theory. No model writes interpretations."""
    kind, oid, parent = _owner(store, owner_kind, owner_ref)
    bad = set(sections or {}) - set(SECTIONS)
    if bad:
        raise ValueError(f"unknown sections {sorted(bad)}; sections: {', '.join(SECTIONS)}")
    sec = {k: _txt(sections.get(k)) for k in SECTIONS}
    if not any(sec.values()):
        raise ValueError("an interpretation needs at least one section")
    author = author or getattr(store, "actor", None) or "api"
    if source == "registered" and MODEL_AUTHORS.search(author):
        raise PermissionError("interpretations are written by people or the session's Claude – never by a model or an importer")
    store.db.execute(f"INSERT INTO interpretation(owner_kind, owner_id, {', '.join(SECTIONS)}, author, at, source) "
                     f"VALUES (?,?,{', '.join('?' * len(SECTIONS))},?,?,?)", (kind, oid, *sec.values(), author, _now(), source))
    if kind == "hypothesis_result":
        store.log("hypothesis", parent["id"], "interpret", {"result": oid, "state": interpretation_state(sec)})
        _index_h(store, parent["id"])
    else:
        store.log("finding", oid, "interpret", {"state": interpretation_state(sec)})
        _index_f(store, oid)
    _commit(store)
    return {"owner": f"{'result' if kind == 'hypothesis_result' else 'finding'}:{oid}", "state": interpretation_state(sec),
            "interpretation": sec}


def withdraw(store: Store, kind: str, ref, reason: str, actor: str | None = None) -> dict:
    if not (reason or "").strip():
        raise ValueError("withdrawing needs a reason")
    if kind == "hypothesis":
        h = hyp_row(store, ref)
        store.db.execute("UPDATE hypothesis SET status = 'withdrawn', withdrawn_reason = ?, updated_at = ? WHERE id = ?",
                         (reason.strip(), _now(), h["id"]))
        store.log("hypothesis", h["id"], "withdraw", {"reason": reason})
        _commit(store)
        return get_hypothesis(store, h["id"])
    f = finding_row(store, ref)
    store.db.execute("UPDATE finding SET status = 'withdrawn', withdrawn_reason = ?, updated_at = ? WHERE id = ?",
                     (reason.strip(), _now(), f["id"]))
    store.log("finding", f["id"], "withdraw", {"reason": reason})
    _commit(store)
    return get_finding(store, f["id"])


H_EDITABLE = ("title", "tags", "theory_refs", "project", "kind", "statement", "direction", "published_in", "visibility")


def update_hypothesis(store: Store, ref, changes: dict, actor: str | None = None) -> dict:
    """Edit what is not frozen: title, tags, theory_refs, project, published_in, visibility (public only on explicit request);
    statement, direction and kind only before the pre-registration is frozen."""
    h = hyp_row(store, ref)
    bad = set(changes or {}) - set(H_EDITABLE)
    if bad or not changes:
        raise ValueError(f"editable: {', '.join(H_EDITABLE)} (status changes through the actions)")
    p = _prereg(store, h["id"])
    frozen = bool(p and p["frozen_at"])
    vals = {}
    for k, v in changes.items():
        if k in ("statement", "direction", "kind") and frozen:
            raise PermissionError(f"{k} of {h['code']} is frozen with the pre-registration – record a deviation or revise")
        if k == "direction":
            v = norm_direction(v)
        elif k == "kind" and v not in KINDS:
            raise ValueError(f"kind: {' | '.join(KINDS)}")
        elif k == "visibility" and v not in ("public", "internal"):
            raise ValueError("visibility: public | internal")
        elif k == "tags":
            v = _j(_check_tags(store, v))
        elif k == "theory_refs":
            v = _j(v or None)
        elif k == "project":
            v = _slug(v) if v else None
        elif k == "statement" and not (v or "").strip():
            raise ValueError("statement cannot be empty")
        vals[k] = v
    store.db.execute(f"UPDATE hypothesis SET {', '.join(f'{k} = ?' for k in vals)}, updated_at = ? WHERE id = ?",
                     (*vals.values(), _now(), h["id"]))
    store.log("hypothesis", h["id"], "update", {"fields": sorted(vals)})
    _index_h(store, h["id"])
    _commit(store)
    return get_hypothesis(store, h["id"])


def revise(store: Store, ref, statement: str | None = None, reason: str | None = None, actor: str | None = None,
           **fields) -> dict:
    """A new version of the hypothesis (own code <code>-r<k>, own pre-registration); the old one stays as 'revised',
    linked with relation 'revises'."""
    h = hyp_row(store, ref)
    if not (reason or "").strip():
        raise ValueError("revising needs a reason")
    k = 2
    base = re.sub(r"-r\d+$", "", h["code"])
    while store.db.execute("SELECT 1 FROM hypothesis WHERE coalesce(series_id, 0) = ? AND code = ?",
                           (h["series_id"] or 0, f"{base}-r{k}")).fetchone():
        k += 1
    allowed = {"title", "kind", "direction", "theory_refs", "tags", "prereg"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"revise takes statement, reason and {', '.join(sorted(allowed))}")
    new = create_hypothesis(
        store, statement or h["statement"], code=f"{base}-r{k}", series=h["series_id"], kind=fields.get("kind") or h["kind"],
        title=fields.get("title") or h["title"], direction=fields.get("direction") or h["direction"],
        theory_refs=fields.get("theory_refs") or _l(h["theory_refs"]), project=h["project"],
        tags=fields.get("tags") or _l(h["tags"]), visibility=h["visibility"], prereg=fields.get("prereg"), actor=actor,
        revises=h["id"])
    store.db.execute("UPDATE hypothesis SET status = 'revised', updated_at = ? WHERE id = ?", (_now(), h["id"]))
    _link_ids(store, "hypothesis", new["id"], "hypothesis", h["id"], "revises", reason, actor)
    store.log("hypothesis", h["id"], "revise", {"new": new["id"], "reason": reason})
    _commit(store)
    return get_hypothesis(store, new["id"])


# ------------------------------------------------------------------ findings
F_FORBIDDEN = {"verdict", "confirmed", "supported", "exploratory", "result"}


def create_finding(store: Store, statement: str, code: str | None = None, series=None, kind: str = "qualitative",
                   title: str | None = None, strength: str | None = None, context: dict | None = None,
                   method: str | None = None, detail: str | None = None, theory_refs: list | None = None,
                   status: str = "recorded", visibility: str = "internal", project: str | None = None,
                   tags: dict | None = None, actor: str | None = None, source: str = "registered",
                   aliases: list | None = None, evidence: list | None = None, created_at: str | None = None,
                   **extra) -> dict:
    """An exploratory result without a prior hypothesis – qualitative or quantitative; always exploratory, never confirmed."""
    if set(extra) & F_FORBIDDEN:
        raise ValueError("a finding is always exploratory: it has no verdict and can never be confirmed – derive a "
                         "hypothesis from it (derive_hypothesis) and pre-register that one")
    if extra:
        raise ValueError(f"unknown fields {sorted(extra)}")
    statement = (statement or "").strip()
    if not statement:
        raise ValueError("a finding needs a statement")
    if kind not in KINDS:
        raise ValueError(f"kind: {' | '.join(KINDS)}")
    if strength is not None and strength not in STRENGTHS:
        raise ValueError(f"strength: {' | '.join(STRENGTHS)}")
    if status not in FINDING_STATUSES or status == "withdrawn":
        raise ValueError("status: draft | recorded")
    if visibility not in ("public", "internal"):
        raise ValueError("visibility: public | internal")
    s = series_row(store, series) if series not in (None, "") else None
    sid = s["id"] if s else None
    code = (code or "").strip() or _next_code(store, "finding", "F", sid, s["slug"] if s else None)
    if store.db.execute("SELECT 1 FROM finding WHERE coalesce(series_id, 0) = ? AND lower(code) = lower(?)", (sid or 0, code)).fetchone():
        raise ValueError(f"finding {code} exists in this series")
    now = _now()
    cur = store.db.execute(
        "INSERT INTO finding(code, series_id, kind, title, statement, strength, context, method, detail, theory_refs, status, "
        "visibility, project, tags, aliases, source, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, sid, kind, (title or "").strip() or None, statement, strength, _j(context or None), _txt(method), _txt(detail),
         _j(theory_refs or None), status, visibility, _slug(project) if project else (s["project"] if s else None),
         _j(_check_tags(store, tags)), _j(aliases or None), source, _actor(store, actor), created_at or now, now))
    fid = cur.lastrowid
    store.log("finding", fid, "insert", {"code": code, "source": source})
    for e in evidence or []:
        add_evidence(store, f"finding:{fid}", **e)
    _index_f(store, fid)
    _commit(store)
    return get_finding(store, fid)


F_EDITABLE = ("title", "statement", "strength", "context", "method", "detail", "theory_refs", "status", "project", "tags",
              "published_in", "visibility", "kind")


def update_finding(store: Store, ref, changes: dict, actor: str | None = None) -> dict:
    f = finding_row(store, ref)
    if set(changes or {}) & F_FORBIDDEN:
        raise ValueError("a finding is always exploratory and can never be confirmed – derive a hypothesis instead")
    bad = set(changes or {}) - set(F_EDITABLE)
    if bad or not changes:
        raise ValueError(f"editable: {', '.join(F_EDITABLE)}")
    vals = {}
    for k, v in changes.items():
        if k == "status" and v not in ("draft", "recorded", "revised"):
            raise ValueError("status: draft | recorded | revised (withdraw through action='withdraw')")
        if k == "strength" and v is not None and v not in STRENGTHS:
            raise ValueError(f"strength: {' | '.join(STRENGTHS)}")
        if k == "kind" and v not in KINDS:
            raise ValueError(f"kind: {' | '.join(KINDS)}")
        if k == "visibility" and v not in ("public", "internal"):
            raise ValueError("visibility: public | internal")
        if k in ("context", "theory_refs"):
            v = _j(v or None)
        elif k == "tags":
            v = _j(_check_tags(store, v))
        elif k == "project":
            v = _slug(v) if v else None
        vals[k] = v
    store.db.execute(f"UPDATE finding SET {', '.join(f'{k} = ?' for k in vals)}, updated_at = ? WHERE id = ?",
                     (*vals.values(), _now(), f["id"]))
    store.log("finding", f["id"], "update", {"fields": sorted(vals)})
    _index_f(store, f["id"])
    _commit(store)
    return get_finding(store, f["id"])


def add_evidence(store: Store, owner, kind: str, ref: str | None = None, version: int | None = None, quote: str | None = None,
                 speaker_role: str | None = None, note: str | None = None, actor: str | None = None) -> dict:
    """Evidence for a finding ('finding:N' / F-code) or a result ('result:N'): dataset, observation, document, exhibit, photo,
    video, quote (speaker ROLE only – never names of private persons), note, file."""
    o = str(owner)
    okind, oid, parent = _owner(store, "result" if o.startswith("result:") else "finding", o.removeprefix("result:"))
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"evidence kind: {' | '.join(EVIDENCE_KINDS)}")
    if kind == "quote":
        if not (quote or "").strip():
            raise ValueError("a quote needs the quote text")
        if not (speaker_role or "").strip():
            raise ValueError("a quote needs speaker_role (role only, e.g. 'Pflegeleitung, Spital A' – never a private person's name)")
    if kind in ("dataset", "observation", "document", "exhibit", "photo", "video") and not ref:
        raise ValueError(f"evidence of kind {kind} needs ref (e.g. {kind}:12)")
    if kind == "dataset" and ref and not version:
        refs = parse_dataset_refs(ref)
        if refs:
            ref, version = refs[0][0], refs[0][1] or _version_at(store, int(refs[0][0].split(":")[1]), None)
    dup = store.db.execute("SELECT id FROM research_evidence WHERE owner_kind = ? AND owner_id = ? AND kind = ? AND "
                           "coalesce(ref, '') = coalesce(?, '') AND coalesce(quote, '') = coalesce(?, '')",
                           (okind, oid, kind, ref, quote)).fetchone()
    if dup:
        return {"id": dup[0], "duplicate": True}
    cur = store.db.execute("INSERT INTO research_evidence(owner_kind, owner_id, kind, ref, version, quote, speaker_role, note, "
                           "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (okind, oid, kind, ref, version, _txt(quote), _txt(speaker_role), _txt(note), _actor(store, actor), _now()))
    if okind == "finding":
        store.log("finding", oid, "evidence", {"kind": kind, "ref": ref})
        if kind == "dataset":
            _log_dataset_use(store, [{"ref": ref, "version": version}], parent["code"])
        _index_f(store, oid)
    else:
        store.log("hypothesis", parent["id"], "evidence", {"result": oid, "kind": kind, "ref": ref})
    _commit(store)
    return {"id": cur.lastrowid, "owner": f"{'finding' if okind == 'finding' else 'result'}:{oid}", "kind": kind, "ref": ref}


def _link_ids(store: Store, fk: str, fid: int, tk: str, tid: int, relation: str, note, actor) -> int | None:
    if relation not in RELATIONS:
        raise ValueError(f"relation: {' | '.join(RELATIONS)}")
    if (fk, fid) == (tk, tid):
        raise ValueError("a link needs two different items")
    cur = store.db.execute("INSERT OR IGNORE INTO research_link(from_kind, from_id, to_kind, to_id, relation, note, created_by, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)", (fk, fid, tk, tid, relation, _txt(note), _actor(store, actor), _now()))
    if cur.rowcount:
        store.log(fk, fid, "link", {"to": f"{tk}:{tid}", "relation": relation})
    return cur.lastrowid if cur.rowcount else None


def link(store: Store, from_ref, to_ref, relation: str, note: str | None = None, actor: str | None = None) -> dict:
    """Link hypotheses/findings: sharpens | replicates | contradicts | extends | derived_from | competes_with | revises."""
    fk, f = _node(store, from_ref)
    tk, t = _node(store, to_ref)
    if relation == "derived_from" and fk == "finding" and tk == "hypothesis":
        raise ValueError("a finding is not derived from a hypothesis test – record it as a finding of its own and link "
                         "'extends' or 'contradicts'")
    lid = _link_ids(store, fk, f["id"], tk, t["id"], relation, note, actor)
    _commit(store)
    return {"link": lid, "from": f"{fk}:{f['id']}", "to": f"{tk}:{t['id']}", "relation": relation,
            "created": lid is not None}


def derive_hypothesis(store: Store, finding_ref, statement: str, actor: str | None = None, **fields) -> dict:
    """A hypothesis derived from a finding (status idea; the finding stays exploratory; link 'derived_from' kept)."""
    f = finding_row(store, finding_ref)
    if f["status"] == "withdrawn":
        raise ValueError(f"finding {f['code']} is withdrawn")
    fields.setdefault("series", f["series_id"])
    fields.setdefault("project", f["project"])
    fields.setdefault("theory_refs", _l(f["theory_refs"]))
    return create_hypothesis(store, statement, derived_from_finding=f["id"], actor=actor, **fields)


# ------------------------------------------------------------------ reading
def _ctx(store: Store) -> dict:
    """Shared lookups for briefs and flags (one query each)."""
    return {"ds": {r[0]: (r[1], r[2]) for r in store.db.execute("SELECT id, current_version, title FROM dataset")},
            "series": {r["id"]: r for r in store.db.execute("SELECT * FROM research_series")}}


def _dataset_stale(inputs, ctx) -> list[dict]:
    out = []
    for x in inputs or []:
        m = re.fullmatch(r"dataset:(\d+)", str(x.get("ref") or ""))
        if not m or not x.get("version"):
            continue
        cur = ctx["ds"].get(int(m.group(1)))
        if cur and cur[0] and cur[0] > int(x["version"]):
            out.append({"ref": x["ref"], "used": int(x["version"]), "current": cur[0], "title": cur[1]})
    return out


def _flags_h(store: Store, h, ctx, results=None, prereg=None) -> list[dict]:
    results = _results(store, h["id"]) if results is None else results
    p = _prereg(store, h["id"]) if prereg is None else prereg
    flags = []

    def add(name, detail=None):
        flags.append({"flag": name, "label": LABELS["flag"][name], **({"detail": detail} if detail else {})})
    cur = results[0] if results else None
    if cur is not None and h["status"] not in ("withdrawn",):
        state = interpretation_state(dict(_interp(store, "hypothesis_result", cur["id"]) or {}))
        if state != "complete":
            add("interpretation_missing", LABELS["interpretation"][state])
        stale = _dataset_stale(_l(cur["inputs"], []), ctx)
        if stale:
            add("data_newer_than_result", stale)
        if all(r["sample_run"] for r in results):
            add("sample_only")
        if cur["partial_data"]:
            add("partial_data", cur["partial_data"])
    if results and not (p and p["frozen_at"]):
        add("prereg_missing")
    if p and p["frozen_at"]:
        if p["time_source"] == "unknown":
            add("prereg_time_unknown", "importiert ohne Zeitstempel")
        else:
            # sample/pilot runs may carry their own earlier pre-registration – only full runs are compared
            full = [r["at"] for r in results if not r["sample_run"]]
            if full and p["frozen_at"] > min(full):
                add("prereg_after_result", f"eingefroren {p['frozen_at']}, erstes Ergebnis {min(full)}")
        if _missing_prereg(dict(p)):
            add("prereg_incomplete", _missing_prereg(dict(p)))
        if hashlib.sha256(canonical_prereg(h["code"], dict(p)).encode()).hexdigest() != p["sha256"]:
            add("prereg_hash_mismatch")
    if h["imported_partial"]:
        add("imported_partial")
    if h["status"] == "idea" and (h["created_at"] or "") < (datetime.now() - timedelta(days=IDEA_DAYS)).strftime("%Y-%m-%d %H:%M:%S"):
        add("idea_without_prereg")
    if store.db.execute("SELECT 1 FROM hypothesis_deviation WHERE hypothesis_id = ? AND timing = 'after_data_seen' LIMIT 1",
                        (h["id"],)).fetchone():
        add("deviation_after_data")
    return flags


def _brief_h(store: Store, h, ctx) -> dict:
    results = _results(store, h["id"])
    cur = results[0] if results else None
    p = _prereg(store, h["id"])
    s = ctx["series"].get(h["series_id"])
    istate = interpretation_state(dict(_interp(store, "hypothesis_result", cur["id"]) or {})) if cur else None
    return {"type": "hypothesis", "id": h["id"], "ref": f"hypothesis:{h['id']}", "code": h["code"], "title": h["title"],
            "statement": h["statement"], "kind": h["kind"], "direction": h["direction"], "status": h["status"],
            "status_label": LABELS["status"][h["status"]], "series": s["slug"] if s else None,
            "verdict": cur["verdict"] if cur else None, "verdict_label": LABELS["verdict"][cur["verdict"]] if cur else None,
            "sample_run": bool(cur["sample_run"]) if cur else None, "results": len(results),
            "interpretation_state": istate, "preregistered_at": p["frozen_at"] if p and p["frozen_at"] else None,
            "theory_refs": _l(h["theory_refs"], []), "visibility": h["visibility"], "project": h["project"],
            "imported_partial": bool(h["imported_partial"]), "source": h["source"],
            "flags": [f["flag"] for f in _flags_h(store, h, ctx, results, p)], "updated_at": h["updated_at"]}


def _flags_f(store: Store, f) -> list[dict]:
    flags = []
    if f["status"] != "withdrawn":
        state = interpretation_state(dict(_interp(store, "finding", f["id"]) or {}))
        if state != "complete":
            flags.append({"flag": "interpretation_missing", "label": LABELS["flag"]["interpretation_missing"],
                          "detail": LABELS["interpretation"][state]})
    return flags


def _brief_f(store: Store, f, ctx) -> dict:
    s = ctx["series"].get(f["series_id"])
    return {"type": "finding", "id": f["id"], "ref": f"finding:{f['id']}", "code": f["code"], "title": f["title"],
            "statement": f["statement"], "kind": f["kind"], "strength": f["strength"], "status": f["status"],
            "status_label": LABELS["finding_status"][f["status"]], "series": s["slug"] if s else None, "exploratory": True,
            "interpretation_state": interpretation_state(dict(_interp(store, "finding", f["id"]) or {})),
            "theory_refs": _l(f["theory_refs"], []), "visibility": f["visibility"], "project": f["project"],
            "source": f["source"], "flags": [x["flag"] for x in _flags_f(store, f)], "updated_at": f["updated_at"]}


def _evidence(store: Store, okind: str, oid: int) -> list[dict]:
    return [{k: r[k] for k in ("id", "kind", "ref", "version", "quote", "speaker_role", "note", "created_at") if r[k] is not None}
            for r in store.db.execute("SELECT * FROM research_evidence WHERE owner_kind = ? AND owner_id = ? ORDER BY id", (okind, oid))]


def _interp_out(store: Store, okind: str, oid: int) -> dict:
    rows = store.db.execute("SELECT * FROM interpretation WHERE owner_kind = ? AND owner_id = ? ORDER BY id DESC", (okind, oid)).fetchall()
    cur = dict(rows[0]) if rows else None
    return {"state": interpretation_state(cur), "state_label": LABELS["interpretation"][interpretation_state(cur)],
            "current": {k: cur[k] for k in (*SECTIONS, "author", "at", "source")} if cur else None,
            "versions": [{"id": r["id"], "author": r["author"], "at": r["at"], "state": interpretation_state(dict(r))} for r in rows]}


def _links(store: Store, kind: str, iid: int) -> list[dict]:
    out = []
    for r in store.db.execute("SELECT * FROM research_link WHERE (from_kind = ? AND from_id = ?) OR (to_kind = ? AND to_id = ?) "
                              "ORDER BY id", (kind, iid, kind, iid)):
        outgoing = r["from_kind"] == kind and r["from_id"] == iid
        ok, oid = (r["to_kind"], r["to_id"]) if outgoing else (r["from_kind"], r["from_id"])
        o = store.db.execute(f"SELECT code, title, statement, status FROM {ok} WHERE id = ?", (oid,)).fetchone()
        out.append({"relation": r["relation"], "relation_label": LABELS["relation"][r["relation"]],
                    "direction": "out" if outgoing else "in", "note": r["note"],
                    "other": {"type": ok, "ref": f"{ok}:{oid}", "id": oid, "code": o["code"] if o else None,
                              "title": (o["title"] or o["statement"][:120]) if o else None, "status": o["status"] if o else None}})
    return out


def _theory_cards(store: Store, series_id: int | None, refs: list) -> list[dict]:
    if not refs:
        return []
    cards = []
    if series_id:
        s = store.db.execute("SELECT theory_cards FROM research_series WHERE id = ?", (series_id,)).fetchone()
        cards = _l(s[0], []) if s else []
    out = []
    for ref in refs:
        c = next((c for c in cards if c.get("id") == ref or (c.get("name") or "").lower() == str(ref).lower()), None)
        out.append(c or {"id": ref, "name": ref})
    return out


def _history(store: Store, kind: str, iid: int, limit: int = 60) -> list[dict]:
    return [{"ts": r[0], "action": r[1], "detail": _l(r[2]), "actor": r[3]} for r in store.db.execute(
        "SELECT ts, action, detail, actor FROM changelog WHERE item_kind = ? AND item_id = ? ORDER BY id DESC LIMIT ?",
        (kind, str(iid), limit))]


def _lineage(store: Store, cur) -> dict:
    """Datasets → scripts → result → outputs (slides/charts, datasets) as a small graph for the portal."""
    if cur is None:
        return {"nodes": [], "edges": []}
    nodes, edges = [], []
    rid = f"result:{cur['id']}"
    nodes.append({"id": rid, "kind": "result", "label": LABELS["verdict"][cur["verdict"]], "verdict": cur["verdict"]})
    scripts = _l(cur["scripts"], [])
    for s in scripts:
        nodes.append({"id": f"script:{s.get('path')}", "kind": "script", "label": Path(str(s.get("path"))).name,
                      "path": s.get("path"), "sha256": s.get("sha256")})
        edges.append([f"script:{s.get('path')}", rid])
    for x in _l(cur["inputs"], []):
        m = re.fullmatch(r"dataset:(\d+)", str(x.get("ref") or ""))
        if not m:
            continue
        d = store.db.execute("SELECT title, current_version, slug FROM dataset WHERE id = ?", (int(m.group(1)),)).fetchone()
        nodes.append({"id": x["ref"], "kind": "dataset", "label": d["title"] if d else x["ref"], "version": x.get("version"),
                      "current_version": d["current_version"] if d else None, "slug": d["slug"] if d else None,
                      "stale": bool(d and x.get("version") and d["current_version"] > int(x["version"]))})
        for s in scripts or [None]:
            edges.append([x["ref"], f"script:{s.get('path')}" if s else rid])
    for o in _l(cur["outputs"], []):
        o = str(o)
        node = {"id": o, "kind": o.split(":")[0], "label": o}
        m = re.fullmatch(r"exhibit:(\d+)", o)
        if m:
            e = store.db.execute("SELECT title, project, slug FROM exhibit WHERE id = ?", (int(m.group(1)),)).fetchone()
            if e:
                node.update({"label": e["title"] or e["slug"], "project": e["project"], "slug": e["slug"]})
        nodes.append(node)
        edges.append([rid, o])
    return {"nodes": nodes, "edges": edges}


def _result_out(store: Store, r) -> dict:
    out = {k: r[k] for k in ("id", "verdict", "effect", "estimate", "ci_low", "ci_high", "ci_text", "unit", "n", "n_text",
                             "robustness", "caveats", "method_doc", "run_log", "environment", "partial_data", "at", "by", "source")}
    out.update({"ref": f"result:{r['id']}", "verdict_label": LABELS["verdict"][r["verdict"]], "sample_run": bool(r["sample_run"]),
                "skipped": bool(r["skipped"]), "scripts": _l(r["scripts"], []), "inputs": _l(r["inputs"], []),
                "outputs": _l(r["outputs"], []), "measures": _l(r["measures"]),
                "evidence": _evidence(store, "hypothesis_result", r["id"]),
                "interpretation": _interp_out(store, "hypothesis_result", r["id"])})
    return out


def get_hypothesis(store: Store, ref, include_internal: bool = True, project: str | None = None, series=None) -> dict:
    h = hyp_row(store, ref, series)
    if not _visible(h, include_internal, project):
        raise PermissionError(f"hypothesis {h['code']} is internal; pass include_internal=true or its project")
    ctx = _ctx(store)
    results = _results(store, h["id"])
    p = _prereg(store, h["id"])
    s = ctx["series"].get(h["series_id"])
    pre = None
    if p:
        pre = {k: p[k] for k in (*PREREG_FIELDS, "text", "frozen_at", "sha256", "time_source", "source", "created_at")}
        pre["data"] = _l(p["data"], p["data"]) if p["data"] and p["data"].startswith("[") else p["data"]
        pre["extra"] = _l(p["extra"], p["extra"]) if p["extra"] and p["extra"].startswith("{") else p["extra"]
        pre["frozen"] = bool(p["frozen_at"])
        if p["frozen_at"]:
            pre["sha_ok"] = hashlib.sha256(canonical_prereg(h["code"], dict(p)).encode()).hexdigest() == p["sha256"]
            pre["missing"] = _missing_prereg(dict(p))
        pre["effective"] = effective_prereg(store, h["id"])
    devs = [dict(d) | {"timing_label": LABELS["timing"][d["timing"]]} for d in store.db.execute(
        "SELECT * FROM hypothesis_deviation WHERE hypothesis_id = ? ORDER BY id", (h["id"],))]
    cur = results[0] if results else None
    fnd = store.db.execute("SELECT id, code, title, statement FROM finding WHERE id = ?", (h["derived_from_finding"],)).fetchone() \
        if h["derived_from_finding"] else None
    revised_by = [{"ref": f"hypothesis:{r[0]}", "code": r[1]} for r in
                  store.db.execute("SELECT id, code FROM hypothesis WHERE revises = ?", (h["id"],))]
    rv = store.db.execute("SELECT id, code FROM hypothesis WHERE id = ?", (h["revises"],)).fetchone() if h["revises"] else None
    theory = _l(h["theory_refs"], [])
    return {
        "type": "hypothesis", "id": h["id"], "ref": f"hypothesis:{h['id']}", "code": h["code"], "title": h["title"],
        "statement": h["statement"], "kind": h["kind"], "kind_label": LABELS["kind"][h["kind"]], "direction": h["direction"],
        "status": h["status"], "status_label": LABELS["status"][h["status"]], "visibility": h["visibility"],
        "published_in": h["published_in"], "project": h["project"], "tags": _l(h["tags"], {}), "aliases": _l(h["aliases"], []),
        "series": _series_brief(s), "theory_refs": theory, "theory_cards": _theory_cards(store, h["series_id"], theory),
        "source": h["source"], "imported_partial": bool(h["imported_partial"]), "meta": _l(h["meta"], {}),
        "withdrawn_reason": h["withdrawn_reason"], "created_by": h["created_by"], "created_at": h["created_at"],
        "updated_at": h["updated_at"],
        "derived_from_finding": {"ref": f"finding:{fnd['id']}", "code": fnd["code"], "title": fnd["title"] or fnd["statement"][:120],
                                 "label": f"entstanden aus Finding {fnd['code']}"} if fnd else None,
        "revises": {"ref": f"hypothesis:{rv['id']}", "code": rv["code"]} if rv else None, "revised_by": revised_by,
        "prereg": pre, "deviations": devs,
        "current_result": _result_out(store, cur) if cur else None,
        "results": [_result_out(store, r) for r in results],
        # technical info: results marked as duplicates (never deleted, ignored everywhere else)
        "duplicates": [dict(r) for r in store.db.execute(
            "SELECT id, source, at, duplicate_of FROM hypothesis_result WHERE hypothesis_id = ? AND duplicate_of IS NOT NULL "
            "ORDER BY id", (h["id"],))],
        "interpretation_state": (interpretation_state(dict(_interp(store, "hypothesis_result", cur["id"]) or {})) if cur else None),
        "links": _links(store, "hypothesis", h["id"]), "flags": _flags_h(store, h, ctx, results, p),
        "lineage": _lineage(store, cur), "history": _history(store, "hypothesis", h["id"]),
        "portal": portal_url("hypothesis", h["id"]), "labels": {"section": LABELS["section"]},
    }


def get_finding(store: Store, ref, include_internal: bool = True, project: str | None = None, series=None) -> dict:
    f = finding_row(store, ref, series)
    if not _visible(f, include_internal, project):
        raise PermissionError(f"finding {f['code']} is internal; pass include_internal=true or its project")
    ctx = _ctx(store)
    s = ctx["series"].get(f["series_id"])
    theory = _l(f["theory_refs"], [])
    derived = [{"ref": f"hypothesis:{r[0]}", "code": r[1], "status": r[2], "title": r[3]} for r in store.db.execute(
        "SELECT id, code, status, coalesce(title, substr(statement, 1, 120)) FROM hypothesis WHERE derived_from_finding = ?", (f["id"],))]
    return {
        "type": "finding", "id": f["id"], "ref": f"finding:{f['id']}", "code": f["code"], "title": f["title"],
        "statement": f["statement"], "kind": f["kind"], "kind_label": LABELS["kind"][f["kind"]], "strength": f["strength"],
        "strength_label": LABELS["strength"].get(f["strength"]), "context": _l(f["context"], {}), "method": f["method"],
        "detail": f["detail"], "status": f["status"], "status_label": LABELS["finding_status"][f["status"]],
        "exploratory": True, "visibility": f["visibility"], "published_in": f["published_in"], "project": f["project"],
        "tags": _l(f["tags"], {}), "aliases": _l(f["aliases"], []), "series": _series_brief(s), "theory_refs": theory,
        "theory_cards": _theory_cards(store, f["series_id"], theory), "source": f["source"],
        "withdrawn_reason": f["withdrawn_reason"], "created_by": f["created_by"], "created_at": f["created_at"],
        "updated_at": f["updated_at"], "evidence": _evidence(store, "finding", f["id"]),
        "interpretation": _interp_out(store, "finding", f["id"]), "derived_hypotheses": derived,
        "links": _links(store, "finding", f["id"]), "flags": _flags_f(store, f), "history": _history(store, "finding", f["id"]),
        "portal": portal_url("finding", f["id"]), "labels": {"section": LABELS["section"]},
    }


# ------------------------------------------------------------------ full-text index
def _index_h(store: Store, hid: int) -> None:
    h = store.db.execute("SELECT * FROM hypothesis WHERE id = ?", (hid,)).fetchone()
    store.db.execute("DELETE FROM research_index WHERE item_kind = 'hypothesis' AND item_id = ?", (str(hid),))
    if not h:
        return
    parts = [h["code"], *(_l(h["aliases"], [])), h["title"], h["statement"]]
    for c in _theory_cards(store, h["series_id"], _l(h["theory_refs"], [])):
        parts += [c.get("id"), c.get("name")] + [s.get("name") for s in c.get("scholars") or []]
    p = _prereg(store, hid)
    if p:
        parts += [p[k] for k in (*PREREG_FIELDS, "text")]
    for d in store.db.execute("SELECT field, after, reason FROM hypothesis_deviation WHERE hypothesis_id = ?", (hid,)):
        parts += list(d)
    for r in store.db.execute("SELECT * FROM hypothesis_result WHERE hypothesis_id = ? AND duplicate_of IS NULL", (hid,)):
        parts += [r["verdict"].replace("_", " "), LABELS["verdict"][r["verdict"]], r["effect"], r["robustness"], r["caveats"],
                  r["method_doc"], r["inputs"]]
        i = _interp(store, "hypothesis_result", r["id"])
        if i:
            parts += [i[k] for k in SECTIONS]
    meta = _l(h["meta"], {})
    parts += [str(v) for v in meta.values()] if isinstance(meta, dict) else []
    store.db.execute("INSERT INTO research_index(item_kind, item_id, body) VALUES ('hypothesis', ?, ?)",
                     (str(hid), " | ".join(str(x) for x in parts if x)))


def _index_f(store: Store, fid: int) -> None:
    f = store.db.execute("SELECT * FROM finding WHERE id = ?", (fid,)).fetchone()
    store.db.execute("DELETE FROM research_index WHERE item_kind = 'finding' AND item_id = ?", (str(fid),))
    if not f:
        return
    parts = [f["code"], *(_l(f["aliases"], [])), f["title"], f["statement"], f["method"], f["detail"], f["context"],
             "exploratory explorativ finding befund"]
    for c in _theory_cards(store, f["series_id"], _l(f["theory_refs"], [])):
        parts += [c.get("id"), c.get("name")] + [s.get("name") for s in c.get("scholars") or []]
    for e in store.db.execute("SELECT ref, quote, speaker_role, note FROM research_evidence WHERE owner_kind = 'finding' AND owner_id = ?", (fid,)):
        parts += list(e)
    i = _interp(store, "finding", fid)
    if i:
        parts += [i[k] for k in SECTIONS]
    store.db.execute("INSERT INTO research_index(item_kind, item_id, body) VALUES ('finding', ?, ?)",
                     (str(fid), " | ".join(str(x) for x in parts if x)))


def reindex(store: Store) -> int:
    store.db.execute("DELETE FROM research_index")
    n = 0
    for (hid,) in store.db.execute("SELECT id FROM hypothesis").fetchall():
        _index_h(store, hid)
        n += 1
    for (fid,) in store.db.execute("SELECT id FROM finding").fetchall():
        _index_f(store, fid)
        n += 1
    _commit(store)
    return n


def _fts(store: Store, question: str) -> dict[tuple[str, int], float] | None:
    toks = [t for t in re.findall(r"\w[\w.]*", question or "") if len(t) > 1 or t.isdigit()]
    if not toks:
        return None
    toks = [t.strip(".") for t in toks]
    for joiner in (" ", " OR "):
        q = joiner.join('"' + t.replace('"', "") + '"*' for t in toks if t)
        try:
            rows = store.db.execute("SELECT item_kind, item_id, bm25(research_index) FROM research_index WHERE research_index MATCH ?",
                                    (q,)).fetchall()
        except Exception:
            rows = []
        if rows:
            return {(r[0], int(r[1])): r[2] for r in rows}
    return {}


def search(store: Store, question: str = "", series=None, kind: str | None = None, status: str | None = None,
           verdict: str | None = None, theory: str | None = None, dataset: str | None = None, project: str | None = None,
           include_internal: bool = False, exploratory: bool | None = None, type: str | None = None,
           flag: str | None = None, limit: int = 50) -> dict:
    """Full text over statements, pre-registrations, results, interpretations and findings, with filters. Ranking is
    by text relevance only – no verdict is ranked higher or lower than another."""
    hits = _fts(store, question)
    ctx = _ctx(store)
    sid = series_row(store, series)["id"] if series not in (None, "") else None
    want_h = type in (None, "", "hypothesis") and exploratory is not True
    want_f = type in (None, "", "finding") and exploratory is not False
    v = norm_verdict(verdict) if verdict else None
    ds_like = None
    if dataset:
        m = re.search(r"(\d+)", str(dataset))
        ds_like = f'%"dataset:{m.group(1)}"%' if m else None
    th = (theory or "").lower().strip() or None
    items = []
    if want_h:
        for h in store.db.execute("SELECT * FROM hypothesis ORDER BY id"):
            if hits is not None and ("hypothesis", h["id"]) not in hits:
                continue
            if not _visible(h, include_internal, project) or (sid and h["series_id"] != sid) or (kind and h["kind"] != kind) \
                    or (status and h["status"] != status) or (project and h["project"] != _slug(project)):
                continue
            if th and th not in json.dumps(_theory_cards(store, h["series_id"], _l(h["theory_refs"], [])), ensure_ascii=False).lower():
                continue
            if ds_like and not store.db.execute(
                    "SELECT 1 FROM hypothesis_result WHERE hypothesis_id = ? AND inputs LIKE ? AND duplicate_of IS NULL UNION ALL "
                    "SELECT 1 FROM hypothesis_prereg WHERE hypothesis_id = ? AND (data LIKE ? OR data LIKE ?) LIMIT 1",
                    (h["id"], ds_like, h["id"], ds_like, ds_like.replace('"', ""))).fetchone():
                continue
            b = _brief_h(store, h, ctx)
            if v and b["verdict"] != v:
                continue
            if flag and flag not in b["flags"]:
                continue
            items.append(b)
    if want_f and not v and status in (None, "", *FINDING_STATUSES):
        for f in store.db.execute("SELECT * FROM finding ORDER BY id"):
            if hits is not None and ("finding", f["id"]) not in hits:
                continue
            if not _visible(f, include_internal, project) or (sid and f["series_id"] != sid) or (kind and f["kind"] != kind) \
                    or (status and f["status"] != status) or (project and f["project"] != _slug(project)):
                continue
            if th and th not in json.dumps(_theory_cards(store, f["series_id"], _l(f["theory_refs"], [])), ensure_ascii=False).lower():
                continue
            if ds_like and not store.db.execute("SELECT 1 FROM research_evidence WHERE owner_kind = 'finding' AND owner_id = ? "
                                                "AND ref = ?", (f["id"], ds_like.strip('%"'))).fetchone():
                continue
            b = _brief_f(store, f, ctx)
            if flag and flag not in b["flags"]:
                continue
            items.append(b)
    if hits:
        items.sort(key=lambda b: hits.get((b["type"], b["id"]), 0))
    else:
        items.sort(key=lambda b: (b["series"] or "~", _code_key(b["code"])))
    return {"question": question, "total": len(items), "hypotheses": sum(1 for b in items if b["type"] == "hypothesis"),
            "findings": sum(1 for b in items if b["type"] == "finding"), "items": items[:max(1, min(int(limit or 50), 1000))]}


def list_hypotheses(store: Store, **kw) -> dict:
    kw.setdefault("include_internal", True)
    return search(store, type="hypothesis", **kw)


def list_findings(store: Store, **kw) -> dict:
    kw.setdefault("include_internal", True)
    return search(store, type="finding", **kw)


# ------------------------------------------------------------------ overviews
def board(store: Store, include_internal: bool = True, project: str | None = None) -> list[dict]:
    """Per series: status counts, verdict counts (current results; all verdicts equal), findings, open interpretations."""
    out, by = [], {}
    for s in store.db.execute("SELECT * FROM research_series ORDER BY id"):
        if _visible(s, include_internal, project):
            by[s["id"]] = {"slug": s["slug"], "title": s["title"], "project": s["project"], "visibility": s["visibility"],
                           "counts": {k: 0 for k in STATUSES}, "verdicts": {k: 0 for k in VERDICTS}, "hypotheses": 0,
                           "findings": 0, "interpretation_missing": 0, "imported_partial": 0}
    none = {"slug": None, "title": "ohne Serie", "project": None, "visibility": "internal", "counts": {k: 0 for k in STATUSES},
            "verdicts": {k: 0 for k in VERDICTS}, "hypotheses": 0, "findings": 0, "interpretation_missing": 0, "imported_partial": 0}
    cur = {r[0]: r for r in store.db.execute(
        "SELECT r.hypothesis_id, r.verdict, r.id, (SELECT count(*) FROM interpretation i WHERE i.owner_kind = 'hypothesis_result' "
        "AND i.owner_id = r.id) FROM hypothesis_result r WHERE r.duplicate_of IS NULL ORDER BY r.hypothesis_id, r.sample_run DESC, r.at, r.id")}
    complete = _complete_owners(store)
    for h in store.db.execute("SELECT id, series_id, status, visibility, project, imported_partial FROM hypothesis"):
        if not _visible(h, include_internal, project):
            continue
        b = by.get(h["series_id"]) if h["series_id"] else none
        if b is None:
            continue
        b["hypotheses"] += 1
        b["counts"][h["status"]] += 1
        b["imported_partial"] += h["imported_partial"]
        r = cur.get(h["id"])
        if r and h["status"] != "withdrawn":
            b["verdicts"][r[1]] += 1
            if ("hypothesis_result", r[2]) not in complete:
                b["interpretation_missing"] += 1
    for f in store.db.execute("SELECT id, series_id, status, visibility, project FROM finding"):
        if not _visible(f, include_internal, project):
            continue
        b = by.get(f["series_id"]) if f["series_id"] else none
        if b is None:
            continue
        b["findings"] += 1
        if f["status"] != "withdrawn" and ("finding", f["id"]) not in complete:
            b["interpretation_missing"] += 1
    out = list(by.values())
    if none["hypotheses"] or none["findings"]:
        out.append(none)
    return out


def _complete_owners(store: Store) -> set[tuple[str, int]]:
    out = set()
    for r in store.db.execute("SELECT i.* FROM interpretation i JOIN (SELECT owner_kind k, owner_id o, max(id) m FROM interpretation "
                              "GROUP BY owner_kind, owner_id) x ON x.m = i.id"):
        if interpretation_state(dict(r)) == "complete":
            out.add((r["owner_kind"], r["owner_id"]))
    return out


def evidence_map(store: Store, include_internal: bool = True, project: str | None = None) -> list[dict]:
    """Theory card × verdict counts (current results), plus ideas/open hypotheses and findings per theory."""
    cards = {}
    for s in store.db.execute("SELECT id, slug, theory_cards, visibility, project FROM research_series"):
        for c in _l(s["theory_cards"], []):
            cards[(s["id"], c.get("id"))] = (s["slug"], c)
    rows: dict = {}

    def row(sid, ref):
        slug, c = cards.get((sid, ref), (None, {"id": ref, "name": ref}))
        key = (slug, c.get("id") or ref)
        if key not in rows:
            rows[key] = {"series": slug, "theory": c.get("id") or ref, "name": c.get("name") or ref,
                         "scholars": [x.get("name") for x in c.get("scholars") or []][:6],
                         "verdicts": {k: 0 for k in VERDICTS}, "open": 0, "findings": 0}
        return rows[key]
    for h in store.db.execute("SELECT * FROM hypothesis WHERE status != 'withdrawn'"):
        if not _visible(h, include_internal, project):
            continue
        cur = _current_result(store, h["id"])
        for ref in _l(h["theory_refs"], []):
            r = row(h["series_id"], ref)
            if cur:
                r["verdicts"][cur["verdict"]] += 1
            else:
                r["open"] += 1
    for f in store.db.execute("SELECT * FROM finding WHERE status != 'withdrawn'"):
        if not _visible(f, include_internal, project):
            continue
        for ref in _l(f["theory_refs"], []):
            row(f["series_id"], ref)["findings"] += 1
    return sorted(rows.values(), key=lambda r: (r["series"] or "~", _code_key(r["theory"])))


def worklist(store: Store, include_internal: bool = True, project: str | None = None, limit: int = 200) -> dict:
    """interpretation_missing (results and findings without a complete interpretation), data_newer_than_result,
    idea_without_prereg (> 14 days)."""
    ctx = _ctx(store)
    miss, stale, ideas, other = [], [], [], []
    for h in store.db.execute("SELECT * FROM hypothesis WHERE status != 'withdrawn' ORDER BY id"):
        if not _visible(h, include_internal, project):
            continue
        fl = _flags_h(store, h, ctx)
        names = {f["flag"] for f in fl}
        if names & {"interpretation_missing", "data_newer_than_result", "idea_without_prereg", "prereg_after_result"}:
            b = _brief_h(store, h, ctx)
            if "interpretation_missing" in names:
                miss.append(b)
            if "data_newer_than_result" in names:
                stale.append({**b, "detail": next(f["detail"] for f in fl if f["flag"] == "data_newer_than_result")})
            if "idea_without_prereg" in names:
                ideas.append(b)
            if "prereg_after_result" in names:
                other.append({**b, "detail": next(f["detail"] for f in fl if f["flag"] == "prereg_after_result")})
    for f in store.db.execute("SELECT * FROM finding WHERE status != 'withdrawn' ORDER BY id"):
        if _visible(f, include_internal, project) and _flags_f(store, f):
            miss.append(_brief_f(store, f, ctx))
    key = lambda b: (b["series"] or "~", _code_key(b["code"]))
    return {"counts": {"interpretation_missing": len(miss), "data_newer_than_result": len(stale), "idea_without_prereg": len(ideas),
                       "prereg_after_result": len(other)},
            "interpretation_missing": sorted(miss, key=key)[:limit], "data_newer_than_result": sorted(stale, key=key)[:limit],
            "idea_without_prereg": sorted(ideas, key=key)[:limit], "prereg_after_result": sorted(other, key=key)[:limit]}


def stats(store: Store, include_internal: bool = True) -> dict:
    q = lambda sql: store.db.execute(sql).fetchone()[0]
    return {"series": q("SELECT count(*) FROM research_series"), "hypotheses": q("SELECT count(*) FROM hypothesis"),
            "preregistered": q("SELECT count(*) FROM hypothesis_prereg WHERE frozen_at IS NOT NULL"),
            "results": q("SELECT count(*) FROM hypothesis_result WHERE duplicate_of IS NULL"), "findings": q("SELECT count(*) FROM finding"),
            "deviations": q("SELECT count(*) FROM hypothesis_deviation"),
            "interpretations": q("SELECT count(*) FROM interpretation")}


def sidebar_stats(store: Store, include_internal: bool = True, project: str | None = None) -> dict:
    """The small counts for the portal sidebar (GET /api/research?stats=1, polled every minute): a few hundred bytes."""
    q = lambda sql: store.db.execute(sql).fetchone()[0]
    w = worklist(store, include_internal, project, limit=0)["counts"]
    return {"series": q("SELECT count(*) FROM research_series"),
            "hypotheses": q("SELECT count(*) FROM hypothesis WHERE status != 'withdrawn'"),
            "findings": q("SELECT count(*) FROM finding WHERE status != 'withdrawn'"),
            "worklist": sum(w.values()), "worklist_counts": w}


def overview(store: Store, include_internal: bool = True, project: str | None = None) -> dict:
    """GET /api/research: board + worklist + evidence map + stats + labels."""
    return {"board": board(store, include_internal, project), "worklist": worklist(store, include_internal, project),
            "evidence_map": evidence_map(store, include_internal, project), "stats": stats(store), "labels": LABELS}


def research_for_dataset(store: Store, did: int, include_internal: bool = True, project: str | None = None) -> list[dict]:
    """Hypotheses (results or pre-registrations) and findings (evidence) that use dataset:did."""
    like = f'%"dataset:{did}"%'
    out = []
    for h in store.db.execute(
            "SELECT * FROM hypothesis h WHERE EXISTS (SELECT 1 FROM hypothesis_result r WHERE r.hypothesis_id = h.id AND r.inputs LIKE ? "
            "AND r.duplicate_of IS NULL) "
            "OR EXISTS (SELECT 1 FROM hypothesis_prereg p WHERE p.hypothesis_id = h.id AND p.data LIKE ?) ORDER BY h.id", (like, like)):
        if not _visible(h, include_internal, project):
            continue
        cur = _current_result(store, h["id"])
        used = None
        if cur:
            used = next((x.get("version") for x in _l(cur["inputs"], []) if x.get("ref") == f"dataset:{did}"), None)
        out.append({"type": "hypothesis", "ref": f"hypothesis:{h['id']}", "id": h["id"], "code": h["code"],
                    "title": h["title"] or h["statement"][:140], "status": h["status"],
                    "verdict": cur["verdict"] if cur else None, "verdict_label": LABELS["verdict"][cur["verdict"]] if cur else None,
                    "version": used})
    for f in store.db.execute("SELECT f.* FROM finding f WHERE EXISTS (SELECT 1 FROM research_evidence e WHERE e.owner_kind = "
                              "'finding' AND e.owner_id = f.id AND e.ref = ?) ORDER BY f.id", (f"dataset:{did}",)):
        if _visible(f, include_internal, project):
            out.append({"type": "finding", "ref": f"finding:{f['id']}", "id": f["id"], "code": f["code"],
                        "title": f["title"] or f["statement"][:140], "status": f["status"], "verdict": None, "exploratory": True})
    out.sort(key=lambda x: (x["type"] != "hypothesis", _code_key(x["code"])))
    return out


# ------------------------------------------------------------------ duplicate results
def _interp_ids(store: Store, rid: int) -> int | None:
    r = store.db.execute("SELECT max(id) FROM interpretation WHERE owner_kind = 'hypothesis_result' AND owner_id = ?", (rid,)).fetchone()
    return r[0] if r else None


def dedupe(store: Store, apply: bool = False, actor: str | None = None) -> dict:
    """Mark identical results (same content key) as duplicates of one keeper – never delete. Keeper: (a) the one with an
    interpretation (the newest interpretation wins), (b) else the newest 'registered' one, (c) else the oldest imported
    one. Interpretations on non-keepers stay where they are and are reported. Idempotent; dry run unless apply."""
    groups = []
    rows = store.db.execute("SELECT r.*, h.code FROM hypothesis_result r JOIN hypothesis h ON h.id = r.hypothesis_id "
                            "WHERE r.duplicate_of IS NULL ORDER BY r.hypothesis_id, r.id").fetchall()
    by: dict[tuple, list] = {}
    for r in rows:
        by.setdefault(_row_key(r), []).append(r)
    marked, on_dup, touched = 0, [], set()
    for key, rs_ in by.items():
        if len(rs_) < 2:
            continue
        inter = [(r, _interp_ids(store, r["id"])) for r in rs_]
        with_i = [(r, i) for r, i in inter if i]
        if with_i:
            keeper, rule = max(with_i, key=lambda x: x[1])[0], "a: interpretation"
        elif any(r["source"] == "registered" for r in rs_):
            keeper = max((r for r in rs_ if r["source"] == "registered"), key=lambda r: (r["at"], r["id"]))
            rule = "b: registered"
        else:
            keeper, rule = min(rs_, key=lambda r: (r["at"], r["id"])), "c: oldest imported"
        dups = [r for r in rs_ if r["id"] != keeper["id"]]
        groups.append({"code": rs_[0]["code"], "hypothesis": rs_[0]["hypothesis_id"], "keeper": keeper["id"], "rule": rule,
                       "keeper_source": keeper["source"], "duplicates": [{"id": r["id"], "source": r["source"], "at": r["at"]}
                                                                         for r in dups]})
        for r, i in inter:
            if i and r["id"] != keeper["id"]:
                on_dup.append({"code": r["code"], "result": r["id"], "keeper": keeper["id"],
                               "note": "Interpretation an Duplikat"})
        if apply:
            for r in dups:
                store.db.execute("UPDATE hypothesis_result SET duplicate_of = ? WHERE id = ? AND duplicate_of IS NULL",
                                 (keeper["id"], r["id"]))
                store.log("hypothesis", r["hypothesis_id"], "duplicate", {"result": r["id"], "duplicate_of": keeper["id"],
                                                                         "rule": rule})
                touched.add(r["hypothesis_id"])
        marked += len(dups)
    if apply:
        for hid in touched:
            _index_h(store, hid)
        _commit(store)
    return {"apply": apply, "groups": len(groups), "duplicates": marked, "interpretation_on_duplicate": on_dup,
            "details": groups, "check": check_interpretations(store) if apply else None}


def check_interpretations(store: Store) -> list[dict]:
    """Every hypothesis with an interpreted result: the current result must carry the interpretation, or be a later
    result with different numbers (a real rerun). Returns the cases where that is not so."""
    out = []
    for (hid,) in store.db.execute("SELECT DISTINCT r.hypothesis_id FROM interpretation i JOIN hypothesis_result r "
                                   "ON i.owner_kind = 'hypothesis_result' AND i.owner_id = r.id").fetchall():
        cur = _current_result(store, hid)
        interp = store.db.execute("SELECT DISTINCT r.* FROM hypothesis_result r JOIN interpretation i ON i.owner_kind = "
                                  "'hypothesis_result' AND i.owner_id = r.id WHERE r.hypothesis_id = ?", (hid,)).fetchall()
        if cur is None:
            continue
        if any(r["id"] == cur["id"] for r in interp):
            continue
        if all(_row_key(r) != _row_key(cur) and cur["at"] > r["at"] for r in interp):
            continue                                   # a real rerun after the interpreted result
        code = store.db.execute("SELECT code FROM hypothesis WHERE id = ?", (hid,)).fetchone()[0]
        out.append({"code": code, "current": cur["id"], "current_at": cur["at"],
                    "interpreted": [{"id": r["id"], "at": r["at"], "duplicate_of": r["duplicate_of"],
                                     "sample_run": bool(r["sample_run"])} for r in interp]})
    return out
