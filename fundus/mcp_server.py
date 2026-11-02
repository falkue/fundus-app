"""Fundus MCP server (streamable HTTP, Tailnet only).

    python -m fundus.mcp_server [--db PATH] [--host HOST] [--port 8010] [--scheduler auto|on|off]
    fundus serve …               (the same; fundus/cli.py)

Host and port default to $FUNDUS_MCP_HOST/$FUNDUS_MCP_PORT, the config file (fundus/config.py) or 127.0.0.1:8010.
The in-process job scheduler (fundus/scheduler.py) starts unless the systemd timers do the jobs (see scheduler.enabled).

Every tool returns compact JSON text. Tool functions are plain module-level functions (fundus_*), so
they can be tested without the transport; `build_server()` registers them on an MCPServer.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import functools
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fundus import exhibits as ex
from fundus import forecasts as fc
from fundus import projects
from fundus import query as q
from fundus import reviews as rv
from fundus import store as store_mod
from fundus.store import DEFAULT_DB, ITEM_KINDS, Store, slug

from fundus import config as _config  # noqa: E402

HOST = _config.server_host()       # $FUNDUS_MCP_HOST, config file, else 127.0.0.1 (Falk's unit passes --host)
PORT = _config.server_port()
ACTOR = "mcp"

INSTRUCTIONS = """Fundus is Falk's central evidence archive (figures, time series, forecasts, studies, quotes, practices, facts – each with its source).
Workflow for every research task:
1. Look in Fundus FIRST: fundus_overview (what exists on an industry/topic) and fundus_search / fundus_find_series. fundus_search is hybrid (full-text + multilingual semantic + organisations/people named in the query): ask in your own words, German or English. fundus_entity profiles an organisation or person.
2. Use what is there and cite it with its source (publisher, title, URL, period). Check fundus_update_targets for series that need a new edition.
3. Research only the gaps and updates.
4. Write EVERY new sourced figure, quote, study, practice or fact back with fundus_add (document with url + publisher, period, unit). New editions of a series go in as observations with that series_id; after checking a series call fundus_mark_checked.
5. Store statements about the future as kind 'forecast' (target_period, value or value_low/value_high, unit, made_on), linked to a series when one will show the actual.
6. Tag industry, topic and region with slugs from fundus_vocab (add_vocab only if really missing). New items are 'unverified' unless you verified them against the source (verified=true + verification_note).
Slides and charts (exhibits of the mck-exhibit skill) are stored too: before building a slide, look for existing ones with fundus_search(kind='exhibit', or 'chart'/'slide') and open one with fundus_exhibit (versions, decks, preview/PNG/HTML URLs and the generating script to adapt). Rendering with the skill's render.sh – or `fundus render <html>` where render.sh is not available (macOS, Windows) – registers new slides and versions automatically. A project can hold several slide sets (Foliensätze): when the user explicitly starts a new one, register it first with fundus_exhibit_set(action='create', project, title, folders=[its own folder]) and render into that folder. Archived (locked) slides and sets cannot be changed, moved or re-rendered (status 'locked'); never try to get around a lock – copy the slide or set with fundus_exhibit_copy and work on the copy. Only Falk unlocks.
Foliensuche: fundus_slide_search(question, year, min_confidence) finds slides across all projects, grouped into aspects; save_as stores the result as a saved search. fundus_slides(['search:<name>', 'set:<project>/<set>', 'deck:<project>/<deck>', 'favorites']) returns the slides of saved searches, slide sets, decks and favourites as one list – use it when Falk refers to a saved search or his favourites; fundus_exhibit_favorite marks favourites (star). When you assemble, reorder or streamline a deck, pass its final slide order with fundus_exhibit_deck (from a deck script: mck.fundus_deck) – the portal shows decks in that order. Falk browses slides per project and slide set in the Fundus portal: {PORTAL} (link a project as #folien?p=<project>, a set as #folien?p=<project>&set=<set id>).
Every slide carries a verification stage (fundus_search/fundus_exhibit: review_stage + confidence 0-3: no_sources/unchecked 0, open/changed 1, incorporated/checked 2, confirmed 3). When assembling a FINAL deck use slides with confidence >= 2; leave out slides with confidence <= 1 or tell Falk which ones are doubtful and why (fundus_exhibit shows review_status with open points and unsourced numbers). When building a slide, name ALL sources it was built from in the file – also those not in the visible source line – with mck.page(..., sources=[{title, url, publisher, published}], fundus=['observation:4871', …]) (or fundus_exhibit_link): no number on a slide without a source; reviewers and Gemini check exactly these.
Reviews and feedback: Falk releases slides for review in the portal (people by personal link, or Gemini on Vertex AI – two separate paths; fundus_review does the same; a Gemini check runs right away in the background and its findings arrive as feedback from 'Gemini'). When Falk says to look at / work in the feedback on the slides: fundus_feedback(project=…) returns the open points per slide (claims them for you), change and re-render the slides, then close EVERY point with fundus_feedback_resolve and a comment: incorporated (what changed), answered or rejected (why), or decision (Falk decides). Finally report what you incorporated, rejected and what is left for Falk. Independent checks (people, Gemini) verify sources and data points – never mark them verified yourself.
Photos: Falk keeps an archive of his field-research photos (human-centred design: hospitals, factories, shops, homes, workshops, prototypes). Before searching the web or generating an image for a slide, look there with fundus_photo_search(question, purpose='external'|'teaching'|'internal') – only photos whose rights allow the purpose come back. Put a photo on a slide with the mck-exhibit helper mck_photo (photo(ref, purpose) + photo_img; its credit goes into the slide's sources); the render records the use. When Falk asks to add photos (files, folders, or the pictures in slide decks/PDFs), use fundus_photo_import with a survey (field study/source) and the context he gives; the local model keywords them in the background.
Fundus Cloud: on Falk's explicit instruction, fundus_cloud(action='share', refs, recipients) sends slides end-to-end encrypted to another person's Fundus; internal slides need his confirmation after you show him the list (confirm_internal=true). Never connect or register – that needs his password and happens only in the portal (Verwaltung) or the terminal."""

# ---------------------------------------------------------------- store access
_db_path = os.environ.get("FUNDUS_DB") or str(DEFAULT_DB)
_local = threading.local()
_lock = threading.RLock()          # SQLite allows one writer; tools run in worker threads


def configure(db_path) -> None:
    """Point the tools at another database (tests, scratch runs)."""
    global _db_path
    _db_path = str(db_path)
    _local.__dict__.clear()


def _store() -> Store:
    st = getattr(_local, "store", None)
    if st is None or getattr(_local, "path", None) != _db_path:
        st = Store(_db_path)
        st.actor = ACTOR
        _local.store, _local.path = st, _db_path
    st._vocab = None                # another thread may have added vocabulary
    return st


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _run(fn, *a, **kw) -> str:
    with _lock:
        st = _store()
        try:
            return _json(fn(st, *a, **kw))
        except (LookupError, PermissionError, ValueError, TypeError, sqlite3.Error) as e:
            st.db.rollback()
            return _json({"error": str(e.args[0]) if e.args else str(e)})


# ---------------------------------------------------------------- write helpers (shared, testable)
DOC_FIELDS = {"kind", "published", "doi", "journal", "authors", "language", "access", "is_vendor", "notes"}
SERIES_FIELDS = {"unit", "geo", "population", "frequency", "method", "comparability", "update_hint",
                 "next_expected", "last_checked", "notes"}


def _check_tags(st: Store, tags: dict | None, need_industry_topic=True) -> dict:
    tags = {k: [slug(x) for x in ([v] if isinstance(v, str) else list(v or [])) if x]
            for k, v in (tags or {}).items()}
    for facet in tags:
        if facet not in ("industry", "topic", "region"):
            raise ValueError(f"unknown tag facet '{facet}' (industry, topic, region; project via 'project')")
    if need_industry_topic and (not tags.get("industry") or not tags.get("topic")):
        raise ValueError("tags need at least one 'industry' and one 'topic' slug (see fundus_vocab; "
                         "use industry 'cross-industry' + topic 'ai' for AI in general)")
    known = st.vocab_values()
    unknown = [f"{f}:{v}" for f, vs in tags.items() for v in vs if (f, v) not in known]
    if unknown:
        raise ValueError(f"unknown vocabulary {unknown} – pick from fundus_vocab or register with fundus_add_vocab")
    return tags


def _document(st: Store, doc: dict) -> int:
    doc = dict(doc)
    url = doc.pop("url", None)
    publisher = doc.pop("publisher", None)
    ptype = doc.pop("publisher_type", None)
    title = doc.pop("title", None)
    if publisher and ptype:
        st.publisher(publisher, type=ptype)
    if not title:   # CONTRACT: publisher + period, else URL host
        if publisher:
            title = f"{publisher} {doc.get('published') or ''}".strip()
        elif url:
            title = urlsplit(url if "://" in url else "https://" + url).netloc.removeprefix("www.")
        else:
            raise ValueError("document needs a title, a publisher or a url")
    unknown = set(doc) - DOC_FIELDS
    if unknown:
        raise ValueError(f"unknown document fields {sorted(unknown)}; allowed: title, url, publisher, "
                         f"publisher_type, {', '.join(sorted(DOC_FIELDS))}")
    if "is_vendor" in doc:
        doc["is_vendor"] = 1 if doc["is_vendor"] else 0
    if doc.get("doi") and not doc.get("journal"):
        # the journal comes from the DOI record, not from the publisher name: a house like Elsevier has thousands
        meta = _crossref_once(st, doc["doi"])
        if meta and meta.get("journal"):
            doc["journal"] = meta["journal"]
    detail = store_mod.publisher_detail(publisher) if publisher else []
    if detail and not doc.get("doi") and not st.db.execute("SELECT 1 FROM publisher WHERE id = ?", (publisher,)).fetchone():
        # 'University of St.Gallen (master's thesis)': the bracket describes this document – keep it in its notes
        note = "Publisher detail: " + "; ".join(detail)
        if note not in (doc.get("notes") or ""):
            doc["notes"] = f"{doc['notes']} | {note}" if doc.get("notes") else note
    if doc.get("doi") and publisher and "(" in publisher:
        # "Elsevier (Long Range Planning)": the bracket names one journal, the publisher row stands for the house;
        # a new house is created without it (existing ones match on the same slug anyway)
        house = re.sub(r"\s*\(.*?\)", "", publisher).strip()
        if house and not st.db.execute("SELECT 1 FROM publisher WHERE id = ?", (publisher,)).fetchone():
            if not doc.get("journal"):
                doc["journal"] = publisher[len(house):].strip(" ()") or None
            publisher = house
    return st.document(title, url=url, publisher=publisher, **{k: v for k, v in doc.items() if v not in (None, "")})


def _crossref_once(st: Store, doi: str) -> dict | None:
    """Crossref lookup unless the document with this DOI already has a journal."""
    from fundus import crossref
    row = st.db.execute("SELECT journal FROM document WHERE lower(doi) = lower(?)", (doi,)).fetchone()
    if row and row["journal"]:
        return None
    return crossref.lookup(doi)


def _norm_text(v) -> str:
    return " ".join(str(v or "").lower().split())


def _series_conflict(st: Store, sid: str, name: str, unit, publisher) -> dict | None:
    """Existing definition of series `sid` if it differs from the given name/unit/publisher, else None."""
    row = st.db.execute("SELECT s.*, p.name AS pub FROM series s LEFT JOIN publisher p ON p.id = s.publisher_id "
                        "WHERE s.id = ?", (sid,)).fetchone()
    if row is None:
        return None
    diffs = []
    if name and _norm_text(name) != _norm_text(row["name"]):
        diffs.append("name")
    if unit and row["unit"] and _norm_text(unit) != _norm_text(row["unit"]):
        diffs.append("unit")
    if publisher and row["publisher_id"]:
        pslug = getattr(store_mod, "publisher_slug", slug)
        if publisher != row["publisher_id"] and pslug(publisher) != row["publisher_id"]:
            diffs.append("publisher")
    if not diffs:
        return None
    return {"differs_in": diffs, "existing": q._compact({"id": row["id"], "name": row["name"], "unit": row["unit"],
                                                       "publisher": row["pub"], "frequency": row["frequency"]})}


def _series(st: Store, s: dict, tags: dict | None = None) -> tuple[str, str | None]:
    """Create or attach a series. Returns (series_id, warning) – warning when an existing series with this id
    has a different definition (the item is still attached; check it is really the same metric)."""
    s = dict(s)
    sid, name = s.pop("id", None), s.pop("name", None)
    if not sid or not name:
        raise ValueError("series needs 'id' (slug) and 'name' (metric definition in words)")
    publisher = s.pop("publisher", None)
    unknown = set(s) - SERIES_FIELDS
    if unknown:
        raise ValueError(f"unknown series fields {sorted(unknown)}; allowed: id, name, publisher, {', '.join(sorted(SERIES_FIELDS))}")
    conflict = _series_conflict(st, slug(sid, 120), name, s.get("unit"), publisher)
    sid = st.series(sid, name, publisher=publisher, **{k: v for k, v in s.items() if v not in (None, "")})
    if tags:
        st.tag("series", sid, **tags)
    warning = None
    if conflict:
        warning = (f"series '{sid}' already exists with a different {', '.join(conflict['differs_in'])}: "
                   f"{_json(conflict['existing'])} – attached anyway; if it is not the same metric, use another series id")
    return sid, warning


def add_item(st: Store, kind: str, fields: dict, document: dict | None = None, tags: dict | None = None,
             project: str = "session", series: dict | None = None, verified: bool = False,
             verification_note: str | None = None, source_key: str | None = None) -> dict:
    if kind not in ITEM_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ITEM_KINDS)}")
    fields = {k: v for k, v in dict(fields or {}).items() if v not in (None, "")}
    for k in ("id", "created_at", "updated_at", "document_id", "status"):
        fields.pop(k, None)   # status is set by the verified flag, document via `document`
    if verified and not (verification_note or "").strip():
        raise ValueError("verified=true needs a verification_note (what was checked against which source)")
    tags = _check_tags(st, tags)
    project = (project or "session").strip()
    warnings = []
    max_id = st.db.execute(f"SELECT coalesce(max(id), 0) FROM {kind}").fetchone()[0]
    with st.batch(ACTOR):
        if document:
            fields["document_id"] = _document(st, document)
            if not document.get("url"):
                warnings.append("document has no url – add one so the figure can be checked")
        else:
            warnings.append("no document/source given – item stays unverified; add a document with url + publisher")
        if series:
            if kind not in ("observation", "forecast"):
                raise ValueError("series can only be attached to observations or forecasts")
            fields["series_id"], w = _series(st, series, tags)
            if w:
                warnings.append(w)
        elif fields.get("series_id"):
            if not st.db.execute("SELECT 1 FROM series WHERE id = ?", (fields["series_id"],)).fetchone():
                raise ValueError(f"series '{fields['series_id']}' does not exist – pass `series` {{id, name, …}} to create it")
        if verified and document:
            fields["status"] = "verified"
            fields["notes"] = (f"{fields['notes']} | " if fields.get("notes") else "") + f"Verified: {verification_note.strip()}"
        elif verified:
            warnings.append("verified ignored: an item without a document cannot be verified")
        key = source_key or hashlib.sha1(_json([kind, sorted(fields.items())]).encode()).hexdigest()[:16]
        item_id = st.add(kind, fields, tags=tags, origin=(project, "mcp", key))
        warnings.extend(st.warnings)
        if fields.get("status") == "verified":
            st.log(kind, item_id, "verify", {"note": verification_note})
    created = item_id > max_id
    label = fields.get(q.LABEL_COL[kind]) or ""
    dups = []
    if label:
        for r in q.search(st, label[:200], kind=kind, limit=6, include_internal=False, visible_project=project, match="any",
                          mode="lexical")["results"]:
            if r["id"] != item_id:
                r.pop("tags", None)
                dups.append(r)
    out = {"kind": kind, "id": item_id, "created": created,
           "status": st.db.execute(f"SELECT status FROM {kind} WHERE id = ?", (item_id,)).fetchone()[0]}
    if not created:
        out["note"] = "an identical item already existed; it was updated instead of duplicated"
    if fields.get("series_id"):
        out["series_id"] = fields["series_id"]
    if warnings:
        out["warnings"] = warnings
    out["possible_duplicates"] = dups[:5]
    return out


def update_item(st: Store, kind: str, id, changes: dict, note: str | None = None, include_internal: bool = False,
                project: str | None = None) -> dict:
    if kind == "dataset":                    # datasets have their own fields (fundus.datasets.EDITABLE); changes are logged
        from fundus import datasets
        d = datasets.get(st, id, include_internal=include_internal, project=project)
        out = datasets.update(st, d["id"], changes or {}, actor=ACTOR)
        if note:
            st.log("dataset", d["id"], "note", {"note": note})
            st.db.commit()
        return out
    allowed_kinds = ITEM_KINDS + ("series", "document", "dataset")
    if kind not in allowed_kinds:
        raise ValueError(f"kind must be one of {', '.join(allowed_kinds)}")
    changes = dict(changes or {})
    tags = changes.pop("tags", None)
    for k in ("id", "created_at", "updated_at", "url_key"):
        if k in changes:
            raise ValueError(f"'{k}' cannot be changed")
    key = str(id) if kind == "series" else int(id)
    row = st.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (key,)).fetchone()
    if row is None:
        raise LookupError(f"{kind} {id} not found")
    visible = (q.doc_visible(st, key, include_internal, project) if kind == "document"
               else q.is_visible(st, kind, row, include_internal, project))
    if not visible:
        raise PermissionError(f"{kind} {id} is internal; pass include_internal=true (or its project)")
    unknown = set(changes) - set(row.keys()) - ({"publisher"} if kind == "document" else set())
    if unknown:
        raise ValueError(f"unknown {kind} fields {sorted(unknown)}")
    if kind == "document" and "url" in changes:
        raise ValueError("document url cannot be changed here (it is the de-duplication key)")
    if kind == "document" and "publisher" in changes:
        # publishing house by name or id (an unknown name creates the publisher); the journal is its own field
        name = changes.pop("publisher")
        if not name:
            changes["publisher_id"] = None
        else:
            exists = st.db.execute("SELECT 1 FROM publisher WHERE id = ?", (name,)).fetchone()
            changes["publisher_id"] = name if exists else st.publisher(name)
    verifying = changes.get("status") == "verified" and row["status"] != "verified" if "status" in row.keys() else False
    if verifying and not (note or "").strip():
        raise ValueError("setting status 'verified' needs a note saying what was checked against which source")
    if note and "notes" in row.keys() and "notes" not in changes:
        prefix = "Verified: " if verifying else ""
        changes["notes"] = (f"{row['notes']} | " if row["notes"] else "") + prefix + note.strip()
    with st.batch(ACTOR):
        diff = st.update(kind, key, changes) if changes else {}
        if tags:
            st.tag(kind, key, **_check_tags(st, tags, need_industry_topic=False))
        if verifying:
            st.log(kind, key, "verify", {"note": note})
        elif note:
            st.log(kind, key, "note", {"note": note})
    return {"kind": kind, "id": id, "changed": {k: v[1] for k, v in diff.items()}, "tags_added": tags or None}


def mark_checked(st: Store, series_id: str, next_expected: str | None = None, update_hint: str | None = None,
                 note: str | None = None, checked_on: str | None = None) -> dict:
    if not st.db.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone():
        raise LookupError(f"series {series_id} not found")
    changes = {"last_checked": checked_on or date.today().isoformat()}
    if next_expected:
        changes["next_expected"] = next_expected
    if update_hint:
        changes["update_hint"] = update_hint
    with st.batch(ACTOR):
        st.update("series", series_id, changes)
        st.log("series", series_id, "check", {**changes, "note": note})
    return {"series_id": series_id, **changes}


def forecast_list(st: Store, state: str = "due", industry=None, topic=None, include_internal=False,
                  project=None, limit: int = 20) -> dict:
    fc.due(st, mark=True, actor=ACTOR)
    f, p = q._filters(st, "forecast", industry=industry, topic=topic, include_internal=include_internal, project=project)
    states = {"due": ("due",), "open": ("open", "due"), "resolved": ("hit", "partly", "miss", "unresolvable"),
              "all": fc.OUTCOMES}
    if state not in states:
        raise ValueError("state must be due, open, resolved or all")
    f.append(f"x.outcome IN ({','.join('?' * len(states[state]))})")
    p += list(states[state])
    rows = st.db.execute(f"SELECT x.* FROM forecast x WHERE {' AND '.join(f)}", p).fetchall()
    rows = sorted(rows, key=lambda r: q._period_key(r["target_period"]))
    out = []
    for r in rows[:limit]:
        s = q.summary(st, "forecast", r, include_internal, project, with_tags=False)
        if r["outcome"] in ("open", "due"):
            prop = fc.evaluate(st, r)
            if prop:
                s["proposal"] = {k: v for k, v in prop.items() if k != "forecast_id"}
        elif r["resolution_note"]:
            s["resolution_note"] = q.clip(r["resolution_note"], 200)
        out.append(s)
    return {"state": state, "total": len(rows), "forecasts": out}


# ---------------------------------------------------------------- tools
def fundus_search(text: str = "", kind: str | None = None, industry: str | None = None, topic: str | None = None,
                  region: str | None = None, since: str | None = None, status: str | None = None, limit: int = 20,
                  include_internal: bool = False, project: str | None = None, mode: str = "hybrid",
                  entity: str | None = None) -> str:
    """Search everything in Fundus BEFORE researching anything yourself.

    text: what you are looking for. Ask in any language and in your own words – the default hybrid mode combines
          full-text matching with multilingual semantic search, so a German question finds English items and
          synonyms match ('Spital' ~ hospital ~ health system, 'GenAI' ~ generative AI). Empty text lists the newest
          items matching the filters.
    mode: hybrid (default; full-text and semantic rankings fused) | lexical (words only: all words must match,
          falling back to any word; FTS syntax "exact phrase", OR, NOT, prefix* works) | semantic (meaning only).
          If semantic search is unavailable the lexical result is returned with a 'fallback' note.
    kind: observation | forecast | study | quote | practice | fact | series | exhibit (slides/charts/posters/section
          slides built with the mck-exhibit skill; or 'slide' | 'chart' | 'poster' | 'section' for one exhibit type).
          Without kind, exhibits come back together with the evidence as kind 'exhibit' (exhibit_type, title, so_what,
          project, decks, version, preview_url, png_url) – open one with fundus_exhibit.
    industry/topic/region: vocabulary slugs (fundus_vocab); broader values include narrower ones ('automotive' ⊃ 'automotive-retail', 'dach' ⊃ 'de').
    since: year/month/quarter ('2024', '2024-06', '2024-Q2'); keeps items whose period/date (else document date) ends on or after its start.
    status: verified | unverified | disputed | retracted (exhibits: draft | final | presented).
    include_internal / project: internal items (own or client data) are hidden unless include_internal=true or project names their project.
    entity: only items linked to this organisation/person/product (any spelling: 'USZ', 'Unispital Zürich', 'McKinsey').
          Organisations named in the text are recognised anyway and their items ranked up (see fundus_entity).
    Returns compact hits with value, period, status, source (publisher, title, url), tags and linked entities.
    Use fundus_get for full detail."""
    return _run(q.search_logged, ACTOR, text, mode=mode, kind=kind, industry=industry, topic=topic, region=region,
                since=since, status=status, limit=limit, include_internal=include_internal, project=project,
                entity=entity)


def fundus_get(kind: str, id: str | int, include_internal: bool = False, project: str | None = None) -> str:
    """Full record of one item: all fields, document + publisher, tags, origin (which project/file it came from)
    and recent change history. kind: observation | forecast | study | quote | practice | fact | series | document | exhibit
    (= fundus_exhibit).
    `checks`: independent review checks (people, Gemini) with stage verified (1) | confirmed (2+) | disputed.
    For an observation the whole series (all periods with sources) is included. `related`: linked entities with roles,
    the series' latest period and revisions of this period by newer editions, up to 5 items about the same entity."""
    def _get(st: Store):
        out = q.get(st, kind, id, include_internal=include_internal, project=project)
        if kind in rv.CHECK_KINDS and isinstance(out, dict):
            v = rv.verification(st, kind, id)
            if v["stage"] != "unchecked" or v["doubt"]:
                out["checks"] = v   # independent checks (people, Gemini): verified 1, confirmed 2+, disputed
        return out
    return _run(_get)


def fundus_entity(name: str, include_internal: bool = False, project: str | None = None) -> str:
    """Profile of an organisation, person or product (any spelling or abbreviation: 'USZ', 'Universitätsspital Zürich',
    'McKinsey'): canonical name, aliases, parent/children, number of linked items by kind, role (publisher,
    organisation, speaker, subject, via, mentioned) and project, top items per role, and the 10 entities sharing most
    items with it. Then use fundus_search(entity=…) for everything linked to it."""
    return _run(q.entity_profile, name, include_internal=include_internal, project=project)


def fundus_series(series_id: str, include_internal: bool = False, project: str | None = None) -> str:
    """A time series: metric definition, unit, publisher, comparability notes, update hint, next_expected,
    all observations sorted by period (value + source per value) and the forecasts linked to it."""
    return _run(q.series, series_id, include_internal=include_internal, project=project)


def fundus_find_series(text: str, limit: int = 20, include_internal: bool = False, project: str | None = None) -> str:
    """Find time series by words in their name, id, publisher, unit or tags. Returns id, name, unit, publisher,
    number of periods and first/last period. Check this before creating a new series."""
    return _run(q.find_series, text, limit=limit, include_internal=include_internal, project=project)


def fundus_overview(industry: str | None = None, topic: str | None = None, region: str | None = None,
                    include_internal: bool = False, project: str | None = None) -> str:
    """'What do we have on X?' in one call: counts per kind, breakdown by topic/industry, top series (most periods),
    open/due forecasts, number of series waiting for an update check, newest items. Start every research task here."""
    return _run(q.overview, industry=industry, topic=topic, region=region, include_internal=include_internal,
                project=project)


def fundus_update_targets(industry: str | None = None, topic: str | None = None, older_than_days: int = 180,
                          limit: int = 30) -> str:
    """Series and datasets that should be checked for a newer edition: next_expected passed ('overdue'), never checked, or
    not checked for older_than_days. Series with last period, frequency and update_hint (where to look); `datasets` with
    frequency, next_expected, update_hint and url (an edition tracker series that names 'dataset:N' in its update_hint is
    listed through its dataset). After checking: new series values with fundus_add (series_id set), a new dataset
    edition with fundus_dataset_add(dataset=<ref>); then fundus_mark_checked (series id or 'dataset:N')."""
    return _run(q.update_targets, industry=industry, topic=topic, older_than_days=older_than_days, limit=limit)


def fundus_mark_checked(series_id: str, next_expected: str | None = None, update_hint: str | None = None,
                        note: str | None = None) -> str:
    """Record that a series – or a dataset: series_id='dataset:N' – was checked for a new edition today (also when nothing
    new was found). next_expected: ISO month ('2027-03') when the next edition is expected; for a dataset without it a
    passed next_expected moves on by its cadence (annual +12 months …), and its linked tracker series are checked too.
    update_hint: where/how to look next time."""
    if str(series_id).startswith("dataset:"):
        from fundus import datasets
        return _run(lambda st: datasets.mark_checked(st, series_id, next_expected=next_expected, update_hint=update_hint,
                                                     note=note, actor=ACTOR))
    return _run(mark_checked, series_id, next_expected=next_expected, update_hint=update_hint, note=note)


def fundus_forecasts(state: str = "due", industry: str | None = None, topic: str | None = None,
                     include_internal: bool = False, project: str | None = None, limit: int = 20) -> str:
    """Forecasts to follow up. state: due (target period reached; default) | open (open + due) | resolved | all.
    Open forecasts whose target period is over are marked 'due' first. Each open/due forecast with a linked
    series that already has a usable actual value carries a 'proposal' (hit/partly/miss + actual + note).
    auto_apply=false plus 'flags' means the match is not exact (sub-period, covering or later period, category-only,
    unit mismatch) – check before resolving. Proposals are NOT applied – confirm with fundus_resolve_forecast."""
    return _run(forecast_list, state=state, industry=industry, topic=topic, include_internal=include_internal,
                project=project, limit=limit)


def fundus_resolve_forecast(id: int, outcome: str, actual_value: float | None = None,
                            observation_id: int | None = None, note: str | None = None,
                            include_internal: bool = False, project: str | None = None) -> str:
    """Resolve a forecast against reality. outcome: hit | partly | miss | unresolvable (or open/due to reopen, which
    clears actual value, observation and note). observation_id: the observation holding the actual value (its value
    is used if actual_value is omitted). note: how it was judged and from which source.
    Internal forecasts need include_internal=true or their project."""
    return _run(fc.resolve, int(id), outcome, actual_value=actual_value, observation_id=observation_id, note=note,
                actor=ACTOR, include_internal=include_internal, project=project)


def fundus_add(kind: str, fields: dict[str, Any], document: dict[str, Any] | None = None,
               tags: dict[str, list[str]] | None = None, project: str = "session",
               series: dict[str, Any] | None = None, verified: bool = False, verification_note: str | None = None,
               source_key: str | None = None) -> str:
    """Write ONE new sourced finding back to Fundus (after research). Identical items are merged, not duplicated.

    kind: observation (a number) | forecast (statement about the future) | study (empirical finding) | quote |
          practice (what a named organisation does) | fact (regulation, tool, event, definition, framework, assessment, organisation).
    fields (English content; original wording in 'original'/'quote_context'):
      observation: metric*, value (number) or value_text (ranges/text, no invented midpoints), unit, period* (what the value refers to:
                   '2025', '2025-Q2', '2026-08'), category, geo, population, n, method, locator (page/chart), quote_context, series_id, notes
      forecast:    statement*, original, forecaster, metric, value | value_low/value_high, unit, target_period*, made_on, scenario, geo, series_id
      study:       finding*, design, n, region, sector, intervention, outcome, effect, moderators, citations
      quote:       text*, original, language, speaker, role, organisation, said_on, context
      practice:    what*, organisation, segment, country, year, kind, scale, result, people, quote
      fact:        kind* (regulation|tool|event|definition|framework|assessment|organisation|other), title*, what, date, geo, implication, attributes (dict)
      any kind:    visibility 'internal' for own/client data (default public), notes.
    document: {title, url, publisher, publisher_type (official|academic|consultancy|analyst|vendor|association|media|company|ngo|other),
               kind (survey_report|report|article|paper|press_release|video|podcast|website|dataset|book|interview|other),
               published (ISO date/month/year), doi, journal, authors, language, access, is_vendor, notes}. Always give url + publisher.
    publisher = the house only ("Elsevier", "SAGE"); the journal goes into journal – with a doi Fundus fills it from Crossref.
    tags: {"industry": [...], "topic": [...], "region": [...]} – slugs from fundus_vocab; industry and topic are required.
    project: project slug of the current work (default 'session'); becomes a project tag.
    series: {id, name, unit, publisher, frequency, geo, population, method, comparability, update_hint, next_expected} to create/attach
            a time series (observation/forecast only); or set fields.series_id to an existing one (fundus_find_series).
    verified: true only if you checked the value against the source yourself – then verification_note is required.
    Returns the id, whether it was new, and possible near-duplicates already in Fundus (check them!)."""
    return _run(add_item, kind, fields, document=document, tags=tags, project=project, series=series,
                verified=verified, verification_note=verification_note, source_key=source_key)


def fundus_add_series(id: str, name: str, unit: str | None = None, publisher: str | None = None,
                      frequency: str | None = None, geo: str | None = None, population: str | None = None,
                      method: str | None = None, comparability: str | None = None, update_hint: str | None = None,
                      next_expected: str | None = None, notes: str | None = None,
                      tags: dict[str, list[str]] | None = None, project: str | None = None) -> str:
    """Create (or fill empty fields of) a time series: a metric measured repeatedly in a comparable way.
    id: slug like 'cdk-dealer-ai-adoption'; name: the metric definition in words; frequency: annual|semiannual|quarterly|
    monthly|irregular|once; update_hint: where to look for the next edition; next_expected: ISO month.
    Run fundus_find_series first. Returns the id, similar existing series, and a warning if a series with this id
    already exists with a different definition. project: your project (its internal series count as similar too)."""
    def _do(st: Store):
        t = _check_tags(st, tags, need_industry_topic=False) if tags else None
        with st.batch(ACTOR):
            sid, warning = _series(st, {"id": id, "name": name, "unit": unit, "publisher": publisher, "frequency": frequency,
                               "geo": geo, "population": population, "method": method, "comparability": comparability,
                               "update_hint": update_hint, "next_expected": next_expected, "notes": notes}, t)
        similar = [s for s in q.find_series(st, name, limit=6, include_internal=False, visible_project=project)
                   if s["id"] != sid]
        for s in similar:
            s.pop("tags", None)
        out = {"series_id": sid, "similar_series": similar[:5]}
        if warning:
            out["warning"] = warning
        return out
    return _run(_do)


def fundus_update(kind: str, id: str | int, changes: dict[str, Any], note: str | None = None,
                  include_internal: bool = False, project: str | None = None) -> str:
    """Change fields of an existing item, series, document or dataset (kind='dataset', id N: title, description, period,
    geo, license, source fields, project, visibility, keywords, tags, frequency, next_expected, update_hint, last_checked),
    e.g. verify:
    {"status": "verified"} + note (what was checked) | {"status": "disputed"} + note | correct a value/period |
    {"visibility": "internal"} | set series next_expected | document: {"publisher": "Elsevier", "journal": "Long Range Planning"}. changes may include "tags": {"region": ["de"]} to add tags.
    Changes to tracked fields (value, status, outcome, …) are logged. Internal items need include_internal=true
    or their project."""
    return _run(update_item, kind, id, changes, note=note, include_internal=include_internal, project=project)


def fundus_vocab(facet: str | None = None, include_internal: bool = False, project: str | None = None) -> str:
    """Controlled tag vocabulary (industry, topic, region; plus project slugs in use) with parent and usage count
    (counts cover public items, plus internal ones if include_internal or of the given project).
    Use only these slugs in tags; broader values include narrower ones in filters."""
    return _run(q.vocab, facet, include_internal=include_internal, project=project)


def fundus_add_vocab(facet: str, value: str, label: str, parent: str | None = None,
                     description: str | None = None) -> str:
    """Register a missing tag value (facet: industry | topic | region). Only when nothing in fundus_vocab fits;
    give a parent where a broader value exists and a one-line description."""
    def _do(st: Store):
        if facet not in ("industry", "topic", "region"):
            raise ValueError("facet must be industry, topic or region")
        with st.batch(ACTOR):
            v = st.add_vocab(facet, value, label, parent=parent, description=description)
            st.log("vocab", f"{facet}:{v}", "insert", {"label": label, "parent": parent})
        return {"facet": facet, "value": v}
    return _run(_do)


def exhibit_get(st: Store, id_or_slug, include_internal: bool = False, project: str | None = None, script: bool = True) -> dict:
    out = ex.get(st, id_or_slug, include_internal=include_internal, project=project, script=script)
    ex.record_view(st, out["id"], "mcp")
    fb = rv.open_counts(st)["exhibits"].get(out["id"])
    if fb:
        out["feedback_open"] = fb
    stage = rv.slide_status(st, ids=[out["id"]]).get(out["id"])
    if stage and stage.get("stage"):
        out["review_status"] = stage   # verification stage: no_sources | unchecked | open | changed | checked | confirmed
    log = rv.changelog(st, out["id"])
    if log:
        out["changelog"] = log[:20]
    return out


def fundus_exhibit(id_or_slug: str, include_internal: bool = False, project: str | None = None, script: bool = True) -> str:
    """One stored slide/chart (exhibit) with everything needed to reuse or adapt it.
    id_or_slug: exhibit id (from fundus_search kind 'exhibit'), 'project/set/slug' ('northwind/main/governance-1a'),
    'project/slug' (the slide of the project's default set, or the only one with that slug across its slide sets) or a
    slug (unique among visible exhibits). Returns the slide set (id, slug, title), metadata (kind, action title, eyebrow, meta, so-what, chart types, sources,
    palette, language, status), urls of the current render (preview = 640 px PNG, png = full 3840x2160, html, script), all
    versions (date, palette variant, fit, density verdict, file paths and urls), deck memberships, links to Fundus documents/
    items/entities and – script=true – the generating Python script's source text (adapt it, then render with render.sh or `fundus render`,
    which registers the new version). Files are served over HTTP on the Tailnet (curl/open the urls)."""
    return _run(exhibit_get, id_or_slug, include_internal=include_internal, project=project, script=script)


def exhibit_add(st: Store, html: str, png: str, script: str | None = None, project: str | None = None,
                deck: str | None = None, note: str = "", set: str | None = None) -> dict:
    if not html or not png:
        raise ValueError("html and png (paths on the Spark) are required")
    return ex.register(st, html, png, script=script, project=project, deck=deck, note=note or "", actor=ACTOR, set_ref=set)


def fundus_exhibit_add(html: str, png: str, script: str | None = None, project: str | None = None,
                       deck: str | None = None, note: str = "", set: str | None = None) -> str:
    """Register a rendered slide/chart manually (normally render.sh or `fundus render` does this automatically). html, png, script: absolute
    paths ON THE SPARK (the HTML page, its rendered PNG, optionally the generating .py – found automatically in the
    project's skripte/ otherwise). project: slug (default: from the path, e.g. ~/Projects/Clients/26_Northwind → northwind);
    deck: deck name if it is a deck copy; set: slide set slug (default: the set whose folder contains the PNG, else the
    project's default set). Identical content is not stored twice (status duplicate/deck_copy); a changed PNG, HTML or
    script becomes a new version. Returns exhibit id, set, slug, version and status."""
    return _run(exhibit_add, html, png, script=script, project=project, deck=deck, note=note, set=set)


def exhibit_set(st: Store, action: str = "list", project: str | None = None, title: str | None = None,
                folders: list[str] | str | None = None, slug: str | None = None, note: str | None = None) -> Any:
    if isinstance(folders, str):
        folders = [folders]
    if action == "create":
        if not project or not title:
            raise ValueError("create needs project and title (and normally folders=[its own folder])")
        return ex.create_set(st, project, title, folders or [], slug=slug, note=note, actor=ACTOR)
    if action == "list":
        return {"sets": ex.list_sets(st, project)}
    if action == "show":
        ref = slug or title
        if not ref:
            raise ValueError("show needs project and slug (or 'project/set' as slug, or the set id)")
        return ex.show_set(st, ref, project=project)
    raise ValueError("action must be create, list or show")


def fundus_exhibit_set(action: str = "list", project: str | None = None, title: str | None = None,
                       folders: list[str] | None = None, slug: str | None = None, note: str | None = None) -> str:
    """Slide sets (Foliensätze) of a project. A project can hold several sets; every project has a default set ('main')
    that catches all renders outside other sets' folders. A render belongs to the set whose folder (project-relative
    prefix) contains its PNG. When the user explicitly starts a NEW slide set, create it first, then render into its folder:
    action='create', project (slug, e.g. 'northwind'), title ('Board-Workshop März'), folders (project-relative, e.g.
    ['decks/board-workshop']; absolute paths under ~/Projects/<project>/ work too), optional slug and note – slides already
    registered under those folders move into the new set (conflicts are reported).
    action='list' [project]: sets with folders and counts. action='show' project + slug (or slug='project/set' or the
    set id): one set with its slides and decks."""
    return _run(exhibit_set, action, project=project, title=title, folders=folders, slug=slug, note=note)


def exhibit_lock(st: Store, action: str = "status", set: str | None = None, exhibit: str | None = None,
                 project: str | None = None, note: str | None = None) -> Any:
    from fundus import locks
    if action == "lock":
        return locks.lock(st, set_ref=set, exhibit=exhibit, project=project, note=note, actor=ACTOR)
    if action == "status":
        if exhibit:
            row = ex._resolve(st, exhibit, include_internal=True, project=ex._slug(project) if project else None)
            return {"exhibit": row["id"], "locked": locks.exhibit_lock(st, row["id"])}
        if set:
            row = ex.resolve_set(st, set, project)
            return {"set": row["id"], "locked": locks.set_lock(st, row["id"])}
        return locks.states(st, project)
    if action == "history":
        return {"locks": locks.history(st, project)}
    if action == "unlock":
        raise PermissionError("unlocking is Falk's decision alone: he does it in the portal (with confirmation) or in the terminal. "
                              "Do not try other ways – to work on an archived slide or set, copy it with fundus_exhibit_copy")
    raise ValueError("action must be lock, status or history")


def fundus_exhibit_lock(action: str = "status", set: str | None = None, exhibit: str | None = None,
                        project: str | None = None, note: str | None = None) -> str:
    """Archive locks. action='lock' freezes a slide set (set: 'project/set', id, or slug + project) or a single slide
    (exhibit: id or 'project/set/slug'), with an optional note (e.g. 'gehalten am 3.11.2026'). A locked slide takes no
    new version (re-renders are refused with status 'locked'), cannot move to another set, and a locked set takes no new
    slides, folder or name changes or deck-order changes; reviews and feedback still work. action='status' (set |
    exhibit | project) and action='history' [project]. There is NO unlock for agents – only Falk unlocks, in the portal
    or the terminal. To continue working on a locked slide or set, copy it with fundus_exhibit_copy (the copy is free)."""
    return _run(exhibit_lock, action, set=set, exhibit=exhibit, project=project, note=note)


def exhibit_copy(st: Store, exhibit: str | None = None, set: str | None = None, to_set: str | None = None,
                 title: str | None = None, folder: str | None = None, slug: str | None = None, version: int | None = None,
                 project: str | None = None) -> Any:
    from fundus import locks
    if bool(exhibit) == bool(set):
        raise ValueError("give exhibit (copy one slide) or set (copy a whole slide set)")
    if exhibit:
        return locks.copy_exhibit(st, exhibit, to_set=to_set, slug=slug, folder=folder, version=version, project=project, actor=ACTOR)
    if not title or not folder:
        raise ValueError("copying a set needs title (of the new set) and folder (its own project-relative folder)")
    return locks.copy_set(st, set, title, folder, slug=slug, project=project, actor=ACTOR)


def fundus_exhibit_copy(exhibit: str | None = None, set: str | None = None, to_set: str | None = None,
                        title: str | None = None, folder: str | None = None, slug: str | None = None,
                        version: int | None = None, project: str | None = None) -> str:
    """Copy slides – the way to continue working on a locked (archived) slide or set; copies are never locked.
    One slide: exhibit (id or 'project/set/slug'), to_set (target set; default its own set if not locked), optional slug
    (new name, needed in the same set), folder (project-relative; default the target set's folder) and version (default
    the current). Its HTML and PNG are written as <slug>.html/.png into the folder and registered; then edit and re-render
    that copy. A whole set: set ('project/set' or id), title and folder of the NEW set (optional slug) – all slides and the
    set's deck orders are copied. Unmodified copies share the verification stage with their originals."""
    return _run(exhibit_copy, exhibit=exhibit, set=set, to_set=to_set, title=title, folder=folder, slug=slug,
                version=version, project=project)


def exhibit_trash(st: Store, action: str = "list", target: str | None = None, refs: list[str] | None = None,
                  trash_id: int | None = None, project: str | None = None) -> Any:
    from fundus import trash
    if action == "list":
        return trash.listing(st)
    if action == "trash":
        if not target or not refs:
            raise ValueError("trash needs target (slide | set | project) and refs")
        return trash.trash(st, target, refs, project=project, actor=ACTOR)
    if action == "restore":
        if trash_id is None:
            raise ValueError("restore needs trash_id (from action='list')")
        return trash.restore(st, trash_id, actor=ACTOR)
    raise ValueError("action: list | trash | restore (emptying the trash is Falk's, in the portal)")


def fundus_exhibit_trash(action: str = "list", target: str | None = None, refs: list[str] | None = None,
                         trash_id: int | None = None, project: str | None = None) -> str:
    """Delete slides, slide sets or projects – always into the trash (Papierkorb), never for good: it is emptied after 30
    days or by Falk in the portal. Only when Falk asks for it. action='trash' with target 'slide' (refs: slide ids or
    'project/set/slug'), 'set' (refs: 'project/set' or set id) or 'project' (refs: project slugs – all its slides, sets
    and decks; its evidence such as data points stays). Trashed slides leave decks, saved searches and the search; a
    render of the same file is skipped while it is in the trash. action='list': the trash with trash ids and days left.
    action='restore' with trash_id: back to where it was. Archived (locked) slides/sets and slides under review cannot be
    deleted."""
    return _run(exhibit_trash, action=action, target=target, refs=refs, trash_id=trash_id, project=project)


def exhibit_archive(st: Store, action: str = "list", target: str = "slide", refs: list[str] | None = None,
                    query: str | None = None, project: str | None = None) -> Any:
    from fundus import archive
    if action == "list":
        return archive.listing(st, query, project, limit=100)
    if not refs:
        raise ValueError(f"{action} needs refs")
    if action == "archive":
        return archive.archive(st, target, refs, project=project, actor=ACTOR)
    if action == "unarchive":
        return archive.unarchive(st, target, refs, project=project, actor=ACTOR)
    raise ValueError("action: list | archive | unarchive")


def fundus_exhibit_archive(action: str = "list", target: str = "slide", refs: list[str] | None = None,
                           query: str | None = None, project: str | None = None) -> str:
    """Archive of slides: an archived slide stays findable by everyone (fundus_search, fundus_slide_search, saved
    searches, fundus_exhibit, decks – hits carry `archived`) but is no longer listed in its project and slide set in the
    portal. Nothing else changes (versions, decks, verification). action='archive' or 'unarchive' with target 'slide'
    (refs: slide ids or 'project/set/slug'), 'set' (refs: 'project/set' or id – all its slides) or 'project' (refs:
    project slugs – all its slides); only when Falk asks for it. action='list' (optional query words, project): the
    archived slides. Not the same as a lock (fundus_exhibit_lock freezes a slide against changes)."""
    return _run(exhibit_archive, action=action, target=target, refs=refs, query=query, project=project)


def exhibit_favorite(st: Store, action: str = "list", refs: list[str] | None = None, project: str | None = None,
                     set: str | None = None) -> Any:
    from fundus import favorites
    if action == "list":
        return favorites.listing(st, project, set)
    if not refs:
        raise ValueError(f"{action} needs refs")
    if action in ("add", "mark"):
        return favorites.mark(st, refs, True, actor=ACTOR)
    if action in ("remove", "unmark"):
        return favorites.mark(st, refs, False, actor=ACTOR)
    raise ValueError("action: list | add | remove")


def fundus_exhibit_favorite(action: str = "list", refs: list[str] | None = None, project: str | None = None,
                            set: str | None = None) -> str:
    """Favourite slides (a star; Falk sets it in the portal, sessions can set it too). action='add' or 'remove' with
    refs (slide ids or 'project/set/slug') marks or unmarks them – set it when Falk calls a slide a favourite or asks you
    to; nothing else about the slide changes. action='list' (optional project, set): the favourites with stage and urls.
    Elsewhere: fundus_slide_search(favorites=True) searches only favourites (without a question: all of them),
    fundus_slides(['favorites'] or ['favorites:<project>/<set>']) returns them as a slide list, and search hits and
    fundus_exhibit carry `favorite`."""
    return _run(exhibit_favorite, action=action, refs=refs, project=project, set=set)


def dataset_add(st: Store, paths: list[str], title: str | None = None, description: str | None = None,
                source_title: str | None = None, publisher: str | None = None, url: str | None = None, license: str | None = None,
                published: str | None = None, project: str | None = None, visibility: str = "public", dataset: str | None = None,
                note: str | None = None, keywords: list[str] | None = None, frequency: str | None = None,
                next_expected: str | None = None, update_hint: str | None = None, kind: str | None = None,
                crosswalk: dict | None = None, code_systems: dict | None = None, inputs: list | None = None,
                table_scope: str | None = None) -> Any:
    from fundus import datasets
    res = datasets.add(st, paths if isinstance(paths, list) else [paths], title=title, description=description,
                       source_title=source_title, publisher=publisher, url_=url, license=license, published=published,
                       project=project, visibility=visibility, dataset=dataset, note=note, keywords=keywords,
                       frequency=frequency, next_expected=next_expected, update_hint=update_hint, actor=ACTOR,
                       kind=kind, crosswalk=crosswalk, code_systems=code_systems, inputs=inputs, table_scope=table_scope)
    _photo_wake.set()                         # the background worker describes it with the local model
    res["portal"] = f"{ex.PUBLIC_URL}/portal/#daten?d={res['slug']}"
    res["description"] = "queued: the local model writes a summary, keywords and column meanings in the background (~10-60 s)"
    return res


def fundus_dataset_add(paths: list[str], title: str | None = None, description: str | None = None,
                       source_title: str | None = None, publisher: str | None = None, url: str | None = None,
                       license: str | None = None, published: str | None = None, project: str | None = None,
                       visibility: str = "public", dataset: str | None = None, note: str | None = None,
                       keywords: list[str] | None = None, frequency: str | None = None, next_expected: str | None = None,
                       update_hint: str | None = None, kind: str | None = None, crosswalk: dict | None = None,
                       code_systems: dict | None = None, inputs: list | None = None, table_scope: str | None = None) -> str:
    """Store a dataset in Fundus – data files on the Spark: tables (CSV/TSV/TXT, Excel .xlsx – every sheet, JSON/JSONL,
    Parquet; also many GB) and accompanying documents (codebook, method: PDF, Markdown, Word). Each table gets a Parquet
    copy and a column profile; the local model (never a cloud model) then describes it. Give what you know: title,
    description (what it is, how it was made), source_title/publisher/url/license/published, project. Client or own
    non-public data: visibility='internal'. dataset (ref or slug): add the files as a NEW VERSION of that dataset. Returns
    the dataset ref, tables with rows/columns and the portal link. Regularly published data: frequency (annual | semiannual
    | quarterly | monthly | irregular | once), next_expected (ISO month of the next edition) and update_hint (download URL,
    rebuild script) – fundus_update_targets then reminds of the next edition. Files of several GB take minutes to convert and profile –
    for those run `{FUNDUS_CLI} datasets add <path> --title … --project …` in the
    background instead (no tool timeout).
    Linked data: code_systems {"column" | "table/column": "NAICS-2022"} names the code system of key columns (searchable,
    fundus_crosswalk() lists the systems in use). kind='crosswalk' + crosswalk {from_system, from_version, to_system,
    to_version, valid_from, valid_to, quality, method, table, columns: {role: column}} for a mapping table (roles from_code,
    to_code, from_label, to_label, weight, relation, quality, valid_from, valid_to; or from_system/to_system columns when one
    table holds several pairs). inputs ["dataset:12", {"ref": "dataset:7", "version": 2, "note": "…"}]: the datasets a derived
    dataset was built from – it is flagged stale when an input gets a newer version, and it becomes internal if an input is.
    table_scope='merged': the dataset shows the newest copy of every table across versions (a version may re-deliver only
    some tables)."""
    return _run_own(dataset_add, paths, title=title, description=description, source_title=source_title, publisher=publisher,
                    url=url, license=license, published=published, project=project, visibility=visibility, dataset=dataset,
                    note=note, keywords=keywords, frequency=frequency, next_expected=next_expected, update_hint=update_hint,
                    kind=kind, crosswalk=crosswalk, code_systems=code_systems, inputs=inputs, table_scope=table_scope)


def fundus_dataset_search(question: str = "", project: str | None = None, include_internal: bool = False,
                          limit: int = 20, kind: str | None = None, code_system: str | None = None,
                          stale: bool = False, ai: bool = False) -> str:
    """Find datasets in Fundus before an analysis – BEFORE downloading data elsewhere. question: what the data should
    contain (words matched against titles, descriptions, keywords, column names and their meanings, code systems, source,
    project; empty = all). kind: 'data' | 'crosswalk'. code_system: only datasets with a column in that system (e.g.
    'NAICS-2022') or crosswalks from/to it. stale=true: derived datasets whose inputs have newer versions. Internal datasets
    only with include_internal=true or their project. Hits: ref, title, summary, rows, columns, period, geo, source. Then
    fundus_dataset(ref) for the columns and file paths.
    ai=true: a free-text question about the stock ("Haben wir Jobpostings zum Marketing?") answered by the LOCAL model
    (gemma4 on the Spark) from the dataset descriptions only, not the data – takes about 1–2 minutes. Returns answer
    (ja | teilweise | nein), summary, hits (fit direkt | teilweise, why, columns) and related datasets; check a hit with
    fundus_dataset(ref) before using it (descriptions can be wrong)."""
    from fundus import datasets
    if ai:
        from fundus import dataset_ask
        return _run_own(lambda st: dataset_ask.ask(st, question, include_internal=include_internal, project=project,
                                                   visible_project=project))
    return _run(lambda st: datasets.search(st, question, project=project, include_internal=include_internal,
                                           limit=min(int(limit or 20), 100), visible_project=project, kind=kind,
                                           code_system=code_system, stale=stale))


def fundus_crosswalk(system_from: str | None = None, system_to: str | None = None, codes: list[str] | str | None = None,
                     at: str | None = None, limit: int = 500, include_internal: bool = False, project: str | None = None) -> str:
    """Code crosswalks (industry, geography, size, AI technique … – datasets of kind 'crosswalk'). Without systems: the code
    systems Fundus knows (on dataset columns and in crosswalks) and all crosswalks. With system_from and system_to (e.g.
    'SIC-SEC', 'NAICS-2022'; spelling is forgiving): the mapping rows – codes (one or a list) narrows them, at (ISO date) keeps
    rows valid then. Each row: from_code/label, to_code/label, weight, relation (1:1, n:1, 1:n, m:n), quality, and the
    crosswalk dataset it comes from; a crosswalk stored the other way round is read backwards ('reverse'). No crosswalk yet:
    store one with fundus_dataset_add(kind='crosswalk', crosswalk={…})."""
    from fundus import datasets

    def run(st):
        if not system_from and not system_to:
            return {"code_systems": datasets.code_system_registry(st, include_internal, project),
                    "crosswalks": datasets.crosswalks(st, include_internal=include_internal, project=project)}
        if not (system_from and system_to):
            return {"crosswalks": datasets.crosswalks(st, system_from, system_to, include_internal, project)}
        return datasets.crosswalk_lookup(st, system_from, system_to, codes=codes, at=at, limit=min(int(limit or 500), 5000),
                                         include_internal=include_internal, project=project)
    return _run_own(run)


def dataset_one(st: Store, ref: str, action: str = "get", sql: str | None = None, table: str | None = None,
                offset: int = 0, limit: int = 50, project: str | None = None, include_internal: bool = False,
                changes: dict | None = None) -> Any:
    from fundus import datasets
    d = datasets.get(st, ref, include_internal=include_internal, project=project)
    if action == "get":
        return d
    if action == "update":
        if not changes:
            raise ValueError(f"update needs changes, e.g. {{'frequency': 'annual', 'next_expected': '2027-09', 'update_hint': '…'}}; "
                             f"editable: {', '.join(sorted(datasets.EDITABLE))}")
        return datasets.update(st, ref, changes, actor=ACTOR)
    if action == "preview":
        return datasets.preview(st, ref, table=table, offset=offset, limit=limit)
    if action == "query":
        if not sql:
            raise ValueError("query needs sql (SELECT … FROM <table view> – see views in the answer, the first table is also t)")
        return datasets.query(st, ref, sql, limit=limit or 500)
    raise ValueError("action: get | preview | query | update")


def fundus_dataset(ref: str, action: str = "get", sql: str | None = None, table: str | None = None, offset: int = 0,
                   limit: int = 50, project: str | None = None, include_internal: bool = False,
                   changes: dict | None = None) -> str:
    """One dataset. action='get': description, source, tables with every column (type, nulls, distinct, min/max, mean,
    examples, meaning) and the local file paths – on the Spark load a table directly for an analysis:
    pandas.read_parquet(path) or duckdb.sql("SELECT … FROM read_parquet('path')"). action='preview': rows of a table
    (table name, offset, limit ≤ 500). action='query': read-only SQL (DuckDB) over the dataset's tables, each a view named
    after the table, the first also `t`; at most 2000 rows, 60 s. action='update' with changes {field: value}: description,
    source fields, project, visibility, keywords, tags and the edition cadence (frequency, next_expected, update_hint,
    last_checked); linked data: code_systems {"column": "NAICS-2022"} (merged, null removes one), kind + crosswalk {…},
    inputs [refs] (lineage), table_scope 'version' | 'merged'. Publishing a dataset built from internal inputs needs
    confirm_public=true. get also shows inputs (stale when an input has a newer version), used_by and each column's
    code_system. Internal datasets need include_internal or project."""
    return _run_own(dataset_one, ref, action=action, sql=sql, table=table, offset=offset, limit=limit, project=project,
                    include_internal=include_internal, changes=changes)        # own connection: a long query must not block the other tools


def exhibit_move(st: Store, target: str, refs: list[str], to: str, project: str | None = None) -> Any:
    from fundus import trash
    return trash.move(st, target, refs, to, project=project, actor=ACTOR)


def fundus_exhibit_move(target: str, refs: list[str], to: str, project: str | None = None) -> str:
    """Move slides, slide sets or projects – only when Falk asks for it. target 'slide': refs (slide ids or
    'project/set/slug') into the set `to` ('project/set' or set id; any project). target 'set': refs ('project/set' or id)
    into the project `to`. target 'project': refs (project slugs) merged into the project `to` – their sets move over,
    their main set's slides join the target's main set. The files stay where they are: the next render of a moved slide
    adds a version to it at its new place. A name taken in the target gets a suffix (-2). Archived (locked) slides/sets
    and slides under review cannot be moved."""
    return _run(exhibit_move, target=target, refs=refs, to=to, project=project)


def exhibit_deck(st: Store, action: str = "set", project: str | None = None, name: str | None = None,
                 slides: list[str] | None = None, title: str | None = None, set: str | None = None, pdf: str | None = None) -> Any:
    from fundus import decks
    if action == "set":
        if not project or not name or not slides:
            raise ValueError("set needs project, name and slides (PNG paths or exhibit refs in slide order)")
        return decks.set_sequence(st, project, name, slides, title=title, set_ref=set, pdf=pdf, actor=ACTOR)
    if action == "list":
        return {"decks": decks.list_sequences(st, project)}
    if action == "show":
        if not name:
            raise ValueError("show needs name ('project/name' or the deck id) or project + name")
        return decks.get_sequence(st, name, project=project)
    if action == "pdf":
        if not pdf:
            raise ValueError("pdf needs pdf (absolute path on the Spark)")
        return decks.import_pdf(st, pdf, project=project, name=name, actor=ACTOR)
    raise ValueError("action must be set, list, show or pdf")


def fundus_exhibit_deck(action: str = "set", project: str | None = None, name: str | None = None,
                        slides: list[str] | None = None, title: str | None = None, set: str | None = None,
                        pdf: str | None = None) -> str:
    """The slide ORDER of a deck. Whenever you assemble, reorder or streamline a deck (PDF, presentation, final sequence),
    pass the final order here – otherwise Fundus and the portal do not know it: action='set', project (slug), name
    ('deck-v4'), slides = the PNG paths on the Spark (absolute or project-relative; unregistered renders are registered
    first) or exhibit refs (id, 'project/slug') in slide order, optional title (shown in the portal), set (slide set slug)
    and pdf (the PDF built from it). Calling again with the same name replaces the order. From a deck script use
    mck.fundus_deck(seq, name, title=…, pdf=…) instead. action='list' [project]; action='show' name ('project/name' or id);
    action='pdf' pdf=<path>: read the order from a deck PDF (pages matched to stored slides)."""
    return _run(exhibit_deck, action, project=project, name=name, slides=slides, title=title, set=set, pdf=pdf)


def review_tool(st: Store, action: str = "list", project: str | None = None, slides: list[str] | None = None,
                set: str | None = None, deck: str | None = None, title: str | None = None, people: list[str] | None = None,
                focus: list[str] | None = None, review: int | None = None, person: str | None = None,
                expires_days: int = rv.EXPIRES_DAYS, model: str | None = None, iterations: int | None = None,
                loop: int | None = None, refs: list[str] | None = None) -> Any:
    from fundus import review_loop as rl
    if refs and action in ("share", "gemini", "loop"):   # saved searches, sets, decks → the slides of one project
        from fundus import slide_search as ss
        got = ss.resolve_refs(st, refs)
        projs = sorted({s["project"] for s in got["slides"]})
        if project:
            project = ex._slug(project)
        elif len(projs) == 1:
            project = projs[0]
        else:
            raise ValueError(f"the references span several projects ({', '.join(projs)}) – a review belongs to one project; "
                             "pass project=… (once per project)")
        slides = [str(s["id"]) for s in got["slides"] if s["project"] == project]
        if not slides:
            raise ValueError(f"no slides of {project} in {refs}")
        set = deck = None
    if action == "loop":
        if not project:
            raise ValueError("loop needs project and slides (exhibit refs) or set or deck")
        out = rl.start(st, project, slides=slides, set_ref=set, deck=deck, iterations=iterations, focus=focus, model=model, by=ACTOR)
        if _IN_SERVER:
            rl.tick(st, _start_gemini)
        out = rl.status(st, out["loop"])
        out["note"] = (f"Gemini checks now; up to {out['max_rounds']} rounds. Ask fundus_review(action='loop_status', loop={out['loop']}) "
                       "– when it says your_turn, work in the errors (fundus_feedback → re-render → fundus_feedback_resolve for "
                       "every point); the next round starts by itself. It ends early once no slide needs changes.")
        return out
    if action == "loop_status":
        if _IN_SERVER:
            rl.tick(st, _start_gemini)
        return rl.status(st, loop, project=project)
    if action == "loop_stop":
        if loop is None:
            loop = rl.status(st, None, project=project)["loop"]
        return rl.stop(st, loop)
    if action in ("share", "gemini"):
        if not project:
            raise ValueError(f"{action} needs project and slides (exhibit refs) or set or deck")
        if action == "share" and not people:
            raise ValueError("share needs people (names 'Name, Vorname') – for an AI-only check use action='gemini'")
        out = rv.create(st, project, slides=slides, set_ref=set, deck=deck, title=title,
                        people=people if action == "share" else [], gemini=action == "gemini", focus=focus,
                        expires_days=expires_days, model=model, actor=ACTOR)
        if out.get("gemini"):
            _start_gemini(out["id"])
            out["note"] = ("Gemini checks the slides now (about 20-60 s per slide); the findings arrive in fundus_feedback. "
                           "Progress: fundus_review(action='show', review=…).")
        return out
    if action == "list":
        return {"reviews": rv.list_reviews(st, project)}
    if action == "people":
        return {"reviewers": rv.list_reviewers(st, include_ai=True)}
    if action == "models":
        from fundus import gemini as gm
        return gm.models()
    if review is None:
        raise ValueError(f"{action} needs review (id)")
    if action == "show":
        return rv.get(st, review)
    if action == "invite":
        return rv.invite(st, review, people or ([person] if person else []), actor=ACTOR)
    if action == "revoke":
        if not person:
            raise ValueError("revoke needs person")
        return rv.revoke(st, review, person, actor=ACTOR)
    if action == "close":
        return rv.close(st, review, actor=ACTOR)
    if action == "purge":
        return rv.purge(st, review, actor=ACTOR)
    if action == "rerun":
        _start_gemini(int(review), retry=True)
        return {"review": int(review), "note": "Gemini checks the slides that failed or are missing again"}
    raise ValueError("action must be share, gemini, loop, loop_status, loop_stop, list, show, invite, revoke, close, rerun, "
                     "people or models")


def fundus_review(action: str = "list", project: str | None = None, slides: list[str] | None = None,
                  set: str | None = None, deck: str | None = None, title: str | None = None,
                  people: list[str] | None = None, focus: list[str] | None = None, review: int | None = None,
                  person: str | None = None, expires_days: int = 30, model: str | None = None,
                  iterations: int | None = None, loop: int | None = None, refs: list[str] | None = None) -> str:
    """Release slides for review. Two separate paths:
    action='share': people check by personal link – project + slides (exhibit refs of marked slides) | set | deck, people
    (names 'Name, Vorname'; known reviewers are reused, new ones created; reviewers type nothing, their name comes from the
    link). Links work once the review app publishes the review.
    action='gemini': an AI check by Gemini on Vertex AI – no links, nothing published; focus: sources, numbers, logic, language;
    model: Vertex model id (default: the strongest, see action='models').
    action='list' [project] | 'show' review | 'invite' review + people | 'revoke' review + person | 'close' review |
    'people' (known reviewers) | 'models' (Gemini models to choose from) | 'rerun' review (Gemini: failed slides again). The slides are frozen at their current version. Feedback arrives in fundus_feedback.
    action='loop': a Gemini check LOOP from your session – project + slides | set | deck, iterations (Gemini rounds, default
    5, at most 8). Gemini checks; you work in the errors (fundus_feedback → change, re-render → fundus_feedback_resolve for EVERY
    point); as soon as no error is open, the next round checks only the revised slides (the fixes). Ends early when no slide
    needs changes. action='loop_status' loop | project: state (checking | waiting | done | stopped), rounds, progress,
    your_turn = the slides to work on now. action='loop_stop' loop | project. To wait without asking again and again, run in
    the background: {FUNDUS_CLI} review loop-status <loop> --wait
    refs (share | gemini | loop): instead of slides/set/deck, references like ['search:<saved search>', 'set:<p>/<set>',
    'deck:<p>/<deck>'] – their slides of one project (pass project when they span several)."""
    return _run(review_tool, action, project=project, slides=slides, set=set, deck=deck, title=title, people=people,
                focus=focus, review=review, person=person, expires_days=expires_days, model=model, iterations=iterations,
                loop=loop, refs=refs)


def fundus_feedback(project: str | None = None, set: str | None = None, review: int | None = None,
                    exhibit: str | None = None, status: str = "open", claim: bool = True, by: str | None = None,
                    limit: int = 200) -> str:
    """The feedback inbox – use it when Falk says 'look at the feedback on the slides and work it in'. Returns the points
    grouped per slide: slide ref, current version, PNG/HTML urls, png_path and script_path on the Spark, and every point
    (F-id, author: a person, Gemini or Falk, kind comment | shape | check, severity, verdict, target source/data point,
    text, note e.g. a better link, Falk's instruction). Conflicts between reviewers and equal votes are marked.
    status: open (default) | decision | done | all; filter by project, set, review or exhibit. claim=true (default for
    open) marks the returned points in progress for you (`by`, e.g. the project name) so no other session takes them.
    Then for EVERY point call fundus_feedback_resolve with a comment – and report to Falk what you incorporated, what you
    rejected and why, and what he must decide."""
    return _run(rv.inbox, project=project, set_ref=set, review=review, exhibit=exhibit, status=status,
                claim=claim and status == "open", by=by, limit=limit)


def fundus_feedback_resolve(ids: list[str], outcome: str, note: str, version: int | None = None,
                            by: str | None = None) -> str:
    """Close feedback points (ids like 'F-118') with a comment – required:
    outcome='incorporated' (eingearbeitet): note = what you changed; the slide must have a newer version than the reviewed
      one (render first; default: its current version, or pass version).
    outcome='answered' (beantwortet): nothing had to change – note says why. outcome='rejected' (zurückgewiesen): note gives
      the reason. outcome='decision': Falk must decide (contradicting sources, swapping a source, taste) – note = the question.
    Reviewers and the slide's change log show the comment. Never set items 'verified' yourself; independent checks do that."""
    out = _run(rv.resolve, ids, outcome, note, version=version, by=by, actor=ACTOR)
    _loop_tick()          # the last open error of a loop round processed: the next round starts at once
    return out


def fundus_exhibit_link(exhibit: str, items: list[str], remove: bool = False) -> str:  # items may include 'dataset:N'
    """Link a slide to the Fundus items it shows, so reviewers can check exactly these: items like 'observation:4871',
    'series:bfs-genai-use-ch' (data shown) or 'document:45' (a source). New slides do this at render time with
    mck.page(..., fundus=[…]) or mck.fundus_refs(html, […]). remove=true drops the links. Returns the slide's sources and
    data points with their verification stage (unchecked | verified: 1 independent check | confirmed: 2+ | disputed)."""
    return _run(rv.link_items, exhibit, items, remove=remove, actor=ACTOR)


def _slide_search(question, years=None, min_confidence=None, levels=None, project=None, kind=None, limit=40,
                  aspects=True, include_internal=False, visible_project=None, lang=None, favorites=False) -> dict:
    """Retrieval under the store lock; the aspect grouping by the local LLM (seconds) runs outside it."""
    from fundus import slide_search as ss
    with _lock:
        out = ss.search(_store(), question, years=years, min_confidence=min_confidence, levels=levels, project=project, kind=kind,
                        limit=limit, aspects=False, include_internal=include_internal, visible_project=visible_project,
                        favorites=favorites)
    if aspects and out["question"]:
        asp, unrelated, note = ss.group_aspects(out["question"], out["slides"], lang=lang)
        out["aspects"] = asp
        if unrelated:
            out["unrelated"] = unrelated
        if note:
            out["aspects_note"] = note
    return out


def fundus_slide_search(question: str = "", year: str | None = None, min_confidence: int | None = None, project: str | None = None,
                        kind: str | None = None, limit: int = 40, aspects: bool = True, include_internal: bool = False,
                        save_as: str | None = None, favorites: bool = False) -> str:
    """Foliensuche – semantic search over SLIDES only, across all projects and slide sets. Ask a question in your own
    words (German or English); returns the best matching slides (ref, project, set, title, so_what, creation year, stage,
    confidence 0-3, verified_version, preview/png urls) grouped into 3-6 aspects of the question (by the local LLM).
    year: creation year(s) '2026' or '2025,2026' (default: the whole archive). min_confidence: 2 = only checked/confirmed
    slides (use for final decks). project / kind (slide | chart | poster) narrow it down. include_internal / project:
    internal slides of other projects stay hidden. save_as='Name' stores the result as a saved search (search:<slug>)
    that you or other sessions can use later with fundus_slides(['search:<slug>']). favorites=True: only slides marked as
    favourites (star); without a question it returns all favourites (of the project, if given)."""
    try:
        out = _slide_search(question, years=year, min_confidence=min_confidence, project=project, kind=kind, limit=limit,
                            aspects=aspects, include_internal=include_internal, visible_project=project, favorites=favorites)
        if save_as:
            from fundus import slide_search as ss
            with _lock:
                saved = ss.save(_store(), save_as, [s["id"] for s in out["slides"]], question=out["question"],
                                filters=out.get("filters"), aspects=out.get("aspects"), by=ACTOR)
            out["saved"] = {"ref": saved["ref"], "name": saved["name"], "slides": saved["count"]}
        return _json(out)
    except (LookupError, ValueError, TypeError, sqlite3.Error) as e:
        with _lock:
            _store().db.rollback()
        return _json({"error": str(e)})


def fundus_slides(refs: list[str], min_confidence: int | None = None, year: str | None = None,
                  include_internal: bool = False, project: str | None = None) -> str:
    """The slides of one or more references as ONE deduplicated list – use it when Falk says 'take the saved search X'
    or 'use set Y and search Z': refs like 'search:<slug>' (saved search; a bare name works too),
    'set:<project>/<set slug or id>' (slide set), 'deck:<project>/<deck name or id>', 'favorites' or
    'favorites:<project>[/<set>]' (slides marked as favourites). Filters min_confidence (2 = checked
    or confirmed) and year (creation year) apply to the union. Each slide: ref, project, title, stage, confidence,
    verified_version, urls and 'from' (which reference brought it). Open one with fundus_exhibit."""
    from fundus import slide_search as ss
    return _run(lambda st: ss.resolve_refs(st, refs, min_confidence=min_confidence, years=year,
                                           include_internal=include_internal, visible_project=project))


def fundus_saved_search(action: str = "list", ref: str | None = None, name: str | None = None, slides: list[str] | None = None,
                        add: list[str] | None = None, remove: list[str] | None = None, aspect: str | None = None,
                        note: str | None = None) -> str:
    """Saved slide searches (named selections across projects). action='list' | 'get' ref | 'save' name + slides
    (exhibit refs/ids; to save a search result use fundus_slide_search(save_as=…)) | 'update' ref [name, note, add, remove,
    aspect for added slides] | 'rerun' ref (the saved question on today's slides: new candidates) | 'delete' ref.
    ref: 'search:<slug>', the slug, the name or the id."""
    from fundus import slide_search as ss
    if action == "list":
        return _run(lambda st: {"saved_searches": ss.list_collections(st)})
    if action == "save":
        return _run(lambda st: ss.save(st, name, slides or [], note=note, by=ACTOR))
    if not ref:
        return _json({"error": f"{action} needs ref"})
    if action == "get":
        return _run(lambda st: ss.get(st, ref))
    if action == "update":
        return _run(lambda st: ss.update(st, ref, name=name, note=note, add=add, remove=remove, aspect=aspect))
    if action == "delete":
        return _run(lambda st: ss.delete(st, ref))
    if action == "rerun":
        with _lock:
            r = ss._coll(_store(), ref)
        return _json(ss.rerun(Store(_db_path), r["id"]))
    return _json({"error": "action must be list, get, save, update, rerun or delete"})



# ---------------------------------------------------------------- photo archive
def photo_search(st: Store, question: str = "", dims: dict | None = None, purpose: str = "external",
                 orientation: str | None = None, people: str | None = None, group: str | None = None,
                 min_width: int | None = None, limit: int = 12, media: str = "photo") -> Any:
    from fundus import photos
    res = photos.search(st, question, dims=dims, purpose=purpose, orientation=orientation, people=people, group=group,
                        min_width=min_width, limit=min(int(limit or 12), 50), media=media)
    for p in res["photos"]:
        p.pop("view_url", None)
    if res.get("hidden_by_rights"):
        res["note"] = (f"{res['hidden_by_rights']} more photos match but are not cleared for purpose '{purpose}' "
                       "(consent/usage/faces) – tell Falk if one of them is needed; he can clear it in the portal.")
    return res


def fundus_photo_search(question: str = "", dims: dict | None = None, purpose: str = "external",
                        orientation: str | None = None, people: str | None = None, group: str | None = None,
                        min_width: int | None = None, limit: int = 12, media: str = "photo") -> str:
    """Find photos (or videos: media='video', both: 'all') in Falk's media archive (field research: hospitals, factories, shops, homes, workshops, prototypes –
    human-centred design work) BEFORE searching the web or generating images for a slide or graphic.
    question: what the photo should show, own words, German or English (matched against descriptions, interpretation,
    keywords and by meaning). dims: {dimension: [values]} – setting, activity, role, artefact, phase (HCD: understand,
    define, ideate, prototype, test, implement), method, mood, industry, region, keyword (values: fundus_photo('dims')).
    purpose: where the photo will be shown – 'external' (client decks, publications; default), 'teaching' (lectures) or
    'internal' (own working material); only photos whose rights (consent, usage, identifiable faces) allow it come back.
    orientation: landscape | portrait | square. people: 'none' (no people) | 'some'. group: field study or album.
    Hits carry id, ref 'photo:<id>' or 'video:<id>', caption, alt text, keywords, rights and thumb/slide URLs; videos
    also duration, audio and video_url. Open one with fundus_photo. On a slide: mck_photo (photos) or mck_video (videos)
    from the mck-exhibit skill."""
    return _run(photo_search, question, dims=dims, purpose=purpose, orientation=orientation, people=people, group=group,
                min_width=min_width, limit=limit, media=media)


def photo_one(st: Store, ref: str, action: str = "get", exhibit: str | None = None, project: str | None = None) -> Any:
    from fundus import photos
    if ref == "dims" or action == "dims":
        return photos.dims(st)
    if action == "get":
        return photos.get(st, ref)
    if action == "use":
        return photos.record_use(st, ref, exhibit=exhibit, project=project)
    raise ValueError("action: get | use | dims")


def fundus_photo(ref: str, action: str = "get", exhibit: str | None = None, project: str | None = None) -> str:
    """One photo or video of the archive. action='get' (ref 'photo:<id>', 'video:<id>' or id): captions (de/en), alt texts, interpretation,
    keywords, rights (consent, usage, faces, cleared_for), credit, groups, uses, near-duplicates and the files:
    slide_path (local 2400 px JPEG without metadata – embed this one in slides), slide_url, original_url.
    action='use' with exhibit ('project/set/slug' or id) and project: record that a slide uses the photo – do this for
    every photo you put on a slide, and name the returned credit ('Photo: …') in the slide's sources
    (mck.page(..., sources=[{'title': 'Field photo <ref>', 'publisher': <credit>}])). ref='dims': the keyword vocabulary."""
    return _run(photo_one, ref, action=action, exhibit=exhibit, project=project)


_photo_wake = threading.Event()


def photo_import(st: Store, paths: list[str], survey: str | None = None, project: str | None = None,
                 period: str | None = None, place: str | None = None, title: str | None = None, note: str | None = None,
                 consent: str = "yes", usage: str = "publication", album: str | None = None, annotate: bool = True) -> Any:
    from fundus import photos
    if isinstance(paths, str):
        paths = [paths]
    res = photos.import_paths(st, paths, survey=survey, project=project, period=period, place=place, title=title,
                              note=note, consent=consent, usage=usage, album=album)
    res["ids"] = res["ids"][:200]
    if annotate and res["imported"]:
        _photo_wake.set()
        res["annotation"] = ("queued: the local vision model describes and keywords the new photos in the background "
                             f"(about 20 s per photo; {len(photos.pending(st))} waiting). Falk reviews them in the portal: "
                             f"{ex.PUBLIC_URL}/portal/#mediathek")
    return res


def fundus_photo_import(paths: list[str], survey: str | None = None, project: str | None = None,
                        period: str | None = None, place: str | None = None, title: str | None = None,
                        note: str | None = None, consent: str = "yes", usage: str = "publication",
                        album: str | None = None, annotate: bool = True) -> str:
    """Add photos and videos to the media archive – only when Falk asks for it. paths: files or folders on the Spark (JPEG,
    PNG, TIFF, WebP, HEIC, camera RAW; a JPEG/HEIC and RAW of the same shot become one photo; videos MP4, MOV, M4V, MKV,
    WebM, AVI, MTS … – a playable 1080p copy, poster and frames are made, which takes a while for long videos; a .mov next
    to a photo of the same name is a Live Photo and stays a photo) and slide decks or documents (.pptx, .docx, .key,
    .pdf), whose embedded pictures and videos are taken out (icons and logos skipped; charts, screenshots and
    logos that the vision model recognises as no photo are hidden automatically). Identical files are never stored twice.
    survey: the field study / source the photos belong to (created if new; with project, period, place) – pass it, it
    is the main grouping. title/note: context for all new photos (what they show, where they come from); the vision model
    uses it. consent/usage: default yes/publication (Falk, 28 Oct 2026: everything he uploads may be used publicly);
    pass other values only when Falk says so. The files stay on the Spark; the local model (never a cloud model)
    annotates them in the background."""
    return _run_own(photo_import, paths, survey=survey, project=project, period=period, place=place, title=title, note=note,
                    consent=consent, usage=usage, album=album, annotate=annotate)


def cloud_tool(st: Store, action: str = "status", refs: list[str] | None = None, recipients: list[str] | None = None,
               confirm_internal: bool = False) -> Any:
    from fundus import cloud
    cdir = cloud.cloud_dir(st)
    if action == "status":
        return cloud.status(cdir)
    if action == "inbox":
        polled = cloud.poll(st) if cloud.connected(cdir) else {"skipped": "nicht verbunden"}
        return {"poll": polled, **cloud.inbox(cdir, 50)}
    if action == "share":
        return cloud.share(st, refs or [], recipients or [], confirm_internal=bool(confirm_internal), actor=ACTOR)
    raise ValueError("action: status | share | inbox")


def fundus_cloud(action: str = "status", refs: list[str] | None = None, recipients: list[str] | None = None,
                 confirm_internal: bool = False) -> str:
    """Fundus Cloud: send slides end-to-end encrypted to the Fundus of another person (one package per send; Firebase is
    only the mailbox, Google sees no content). Only on Falk's explicit instruction.
    action='status': connected?, own email and fingerprint (6 words), contacts, package counts, last poll.
    action='share': refs ('project:<p>', 'set:<project>/<set id or slug>', 'exhibit:<id>') to recipients (emails with a
      Fundus Cloud account). If any slide is internal (client or own non-public data), the first call returns
      needs_confirmation with the list and reasons – show it to Falk and call again with confirm_internal=true ONLY after
      he confirms. Recipients whose key changed since first contact are refused.
    action='inbox': pick up new packages now (they are imported as received slide sets; unconfirmed senders are marked) and
      list the local log of sent and received packages.
    Connecting or registering needs Falk's password and happens only in the portal (Verwaltung) or the terminal
    (fundus cloud connect) – never from a session."""
    from fundus import cloud
    st = Store(_db_path)
    st.actor = ACTOR
    try:
        return _json(cloud_tool(st, action, refs=refs, recipients=recipients, confirm_internal=confirm_internal))
    except (LookupError, PermissionError, ValueError, TypeError, ImportError, cloud.CloudError, sqlite3.Error) as e:
        st.db.rollback()
        return _json({"error": str(e.args[0]) if e.args else str(e)})
    finally:
        st.close()


def _run_own(fn, *a, **kw) -> str:
    """Like _run, but on an own connection without the server lock: long imports (decoding hundreds of photos) must not
    block the other tools. Every photo is committed on its own, so SQLite's writer lock is held only briefly."""
    st = Store(_db_path)
    st.actor = ACTOR
    try:
        return _json(fn(st, *a, **kw))
    except (LookupError, PermissionError, ValueError, TypeError, FileNotFoundError, sqlite3.Error) as e:
        st.db.rollback()
        return _json({"error": str(e.args[0]) if e.args else str(e)})
    finally:
        st.close()


TOOLS = [fundus_search, fundus_get, fundus_entity, fundus_series, fundus_find_series, fundus_overview, fundus_update_targets,
         fundus_mark_checked, fundus_forecasts, fundus_resolve_forecast, fundus_add, fundus_add_series,
         fundus_update, fundus_vocab, fundus_add_vocab, fundus_exhibit, fundus_exhibit_add, fundus_exhibit_set,
         fundus_exhibit_deck, fundus_exhibit_link, fundus_exhibit_lock, fundus_exhibit_copy, fundus_exhibit_trash, fundus_exhibit_move, fundus_exhibit_archive, fundus_exhibit_favorite, fundus_review, fundus_feedback, fundus_feedback_resolve,
         fundus_slide_search, fundus_slides, fundus_saved_search, fundus_photo_search, fundus_photo, fundus_photo_import,
         fundus_dataset_add, fundus_dataset_search, fundus_dataset, fundus_crosswalk, fundus_cloud]


def record_usage(tool: str) -> None:
    """Count one call per tool and day (for the dashboard); never lets a counting problem break a call."""
    with _lock:
        try:
            st = _store()
            st.db.execute("INSERT INTO usage(day, tool, calls) VALUES (date('now', 'localtime'), ?, 1) "
                          "ON CONFLICT(day, tool) DO UPDATE SET calls = calls + 1", (tool,))
            st.db.commit()
        except sqlite3.Error:
            pass


def _counted(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        record_usage(fn.__name__)
        return fn(*a, **kw)
    return wrapper


async def exhibit_file(request):
    """GET /exhibits/<sha256>.png | .preview.png | .html | .svg | .py – stored exhibit files, read-only. Only hash names
    pass (no paths); files of internal exhibits need ?project=<its project> (or include_internal=1)."""
    from starlette.responses import FileResponse, PlainTextResponse
    name = request.path_params.get("name", "")
    qp = request.query_params
    try:
        with _lock:
            st = _store()
            path, ctype, eid = ex.resolve_file(st, name, project=qp.get("project"),
                                               include_internal=qp.get("include_internal") in ("1", "true"))
            # the gallery's lightbox is browsing, not reuse: it does not count as a view (src=gallery)
            if not name.endswith(".preview.png") and request.method == "GET" and qp.get("src") != "gallery":
                ex.record_view(st, eid, "http")
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except PermissionError as e:
        return PlainTextResponse(str(e), status_code=403)
    except LookupError:
        return PlainTextResponse("not found", status_code=404)
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
    if ctype.startswith("text/html") or ctype.startswith("image/svg"):
        headers["Content-Security-Policy"] = "sandbox allow-scripts"
    return FileResponse(path, media_type=ctype, headers=headers)


# ---------------------------------------------------------------- Fundus portal (own web interface, same origin)
PORTAL_DIR = Path(__file__).resolve().parent.parent / "portal"


def _api_response(request, payload, status=200):
    from starlette.responses import JSONResponse
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def _same_origin(request) -> bool:
    """Writes only from the portal itself: a browser sends Origin on every POST; it must be this server. Requests
    without Origin (curl, scripts in the Tailnet) pass – CSRF needs a browser."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    return origin in (f"http://{host}", f"https://{host}")


async def _json_body(request):
    """Body of a portal write: same origin, application/json, a JSON object. Returns (body, None) or (None, error response)."""
    if not _same_origin(request):
        return None, _api_response(request, {"error": "cross-origin write refused"}, 403)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None, _api_response(request, {"error": "send application/json"}, 415)
    try:
        body = await request.json()
    except ValueError:
        return None, _api_response(request, {"error": "invalid JSON"}, 400)
    if not isinstance(body, dict):
        return None, _api_response(request, {"error": "invalid body"}, 400)
    return body, None


async def exhibit_sharing(request):
    """POST /api/exhibits/sharing {"project": p | "set": <set id> | "id": n, "sharing": "shared" | "project" | "inherit"}
    – visibility of the slides for other projects: per project (all slides without an own or set setting), per slide set
    (all its slides without an own setting) or per slide (override). Chain: slide > set > project > policy."""
    body, err = await _json_body(request)
    if err:
        return err
    sharing = body.get("sharing")
    sharing = None if sharing in ("inherit", None, "") else sharing
    try:
        with _lock:
            st = _store()
            if body.get("id") is not None:
                data = ex.set_sharing(st, str(body["id"]), sharing, actor="portal")
            elif body.get("set") is not None:
                if not str(body["set"]).isdigit():
                    return _api_response(request, {"error": "'set' must be the set id"}, 400)
                data = ex.set_set_sharing(st, str(body["set"]), sharing, actor="portal")
            elif body.get("project"):
                data = ex.set_project_sharing(st, str(body["project"]), sharing, actor="portal")
            else:
                return _api_response(request, {"error": "give 'project', 'set' or 'id'"}, 400)
    except PermissionError as e:
        return _api_response(request, {"error": str(e)}, 403)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, data)


async def names(request):
    """POST /api/names {"project": p, "title": t} | {"set": <set id>, "title": t} – own display names. Identities stay:
    the project slug and the set id/slug never change. An empty project title removes the name (the slug shows again);
    a slide set always keeps a title."""
    body, err = await _json_body(request)
    if err:
        return err
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _api_response(request, {"error": "'title' must be text"}, 400)
    try:
        with _lock:
            st = _store()
            if body.get("set") is not None:
                if not str(body["set"]).isdigit():
                    return _api_response(request, {"error": "'set' must be the set id"}, 400)
                t = " ".join((title or "").split())
                if not t:
                    return _api_response(request, {"error": "a slide set needs a title"}, 400)
                if len(t) > projects.TITLE_MAX:
                    return _api_response(request, {"error": f"title longer than {projects.TITLE_MAX} characters"}, 400)
                row = ex.resolve_set(st, str(body["set"]))
                if t == row["title"]:
                    data = {"project": row["project"], **ex.set_info(st, row), "changed": False}
                else:
                    data = {**ex.rename_set(st, str(body["set"]), title=t, actor="portal"), "changed": True}
            elif body.get("project"):
                data = projects.set_title(st, str(body["project"]), title, actor="portal")
            else:
                return _api_response(request, {"error": "give 'project' or 'set'"}, 400)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, PermissionError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, data)


_stats_cache: dict = {}


async def stats_api(request):
    """GET /api/stats – Fundus-Statistik fürs Portal (fundus.stats_export.build): Bestand, Folien, Nutzung, Betrieb.
    Eine Minute zwischengespeichert; gerechnet in einem Worker-Thread, damit der Server derweil antwortet."""
    from starlette.concurrency import run_in_threadpool
    from fundus import stats_export
    hit = _stats_cache.get("data")
    if hit and time.monotonic() - _stats_cache.get("at", 0) < 60:
        return _api_response(request, hit)

    def compute():
        with _lock:
            return stats_export.build(_store(), probe_mcp=False)
    try:
        data = json.loads(_json(await run_in_threadpool(compute)))
    except (sqlite3.Error, OSError, ValueError) as e:
        return _api_response(request, {"error": str(e)}, 500)
    _stats_cache.update(data=data, at=time.monotonic())
    return _api_response(request, data)


async def locks_api(request):
    """POST /api/locks {"action": "lock" | "unlock", "set": <set id> | "id": <exhibit id>, "note": …, "confirm": <slug>} –
    archive locks from the portal. Unlocking is Falk's explicit decision: only from the portal page in a browser (Origin
    header required) and with the slug of the set or slide typed back as confirm. GET: active locks and history
    (?project=)."""
    from fundus import locks
    if request.method == "GET":
        with _lock:
            st = _store()
            p = request.query_params.get("project") or None
            return _api_response(request, {"active": locks.states(st, p), "history": locks.history(st, p)})
    body, err = await _json_body(request)
    if err:
        return err
    action = body.get("action")
    if action not in ("lock", "unlock"):
        return _api_response(request, {"error": "action must be lock or unlock"}, 400)
    if (body.get("set") is None) == (body.get("id") is None):
        return _api_response(request, {"error": "give 'set' (set id) or 'id' (exhibit id)"}, 400)
    ref = str(body.get("set") if body.get("set") is not None else body.get("id"))
    if not ref.isdigit():
        return _api_response(request, {"error": "'set' and 'id' are numeric ids"}, 400)
    kw = {"set_ref": ref} if body.get("set") is not None else {"exhibit": ref}
    note = body.get("note") if isinstance(body.get("note"), str) else None
    try:
        with _lock:
            st = _store()
            if action == "lock":
                data = locks.lock(st, note=note, actor="portal", **kw)
            else:
                if request.headers.get("origin") is None:
                    return _api_response(request, {"error": "unlock only from the portal in a browser"}, 403)
                want = ex.resolve_set(st, ref)["slug"] if "set_ref" in kw else st.db.execute(
                    "SELECT slug FROM exhibit WHERE id = ?", (int(ref),)).fetchone()
                want = want if isinstance(want, str) else (want[0] if want else None)
                if want is None:
                    return _api_response(request, {"error": "not found"}, 404)
                if str(body.get("confirm") or "").strip() != want:
                    return _api_response(request, {"error": f"type '{want}' to confirm"}, 400)
                data = locks.unlock(st, note=note, actor="falk:portal", **kw)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, PermissionError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, data)


async def portal_settings(request):
    """POST /api/portal/settings {"project_sort": "name_asc" | "name_desc" | "created_desc" | "created_asc"} – order of
    the projects in the portal's sidebar, one setting for every device (GET: part of /api/exhibits)."""
    body, err = await _json_body(request)
    if err:
        return err
    try:
        with _lock:
            data = projects.set_sort(_store(), str(body.get("project_sort")), actor="portal")
    except (ValueError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, data)


def _api_call(request, fn, *a, **kw):
    """Run a portal API call under the store lock and map errors to HTTP status codes."""
    try:
        with _lock:
            data = fn(_store(), *a, **kw)
    except PermissionError as e:
        return _api_response(request, {"error": str(e)}, 403)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, TypeError, sqlite3.Error) as e:
        _store().db.rollback()
        return _api_response(request, {"error": str(e.args[0]) if e.args else str(e)}, 400)
    return _api_response(request, data)


async def slide_search_api(request):
    """GET /api/slide-search?q=…&years=2026,2025&levels=2,3&project=…&kind=…&aspects=0 – Foliensuche (portal: all slides)."""
    import anyio
    qp = request.query_params
    try:
        out = await anyio.to_thread.run_sync(lambda: _slide_search(
            qp.get("q") or "", years=qp.get("years") or None, levels=qp.get("levels") or None, project=qp.get("project") or None,
            kind=qp.get("kind") or None, limit=int(qp.get("limit") or 40), aspects=qp.get("aspects") != "0", include_internal=True,
            lang=qp.get("lang") or None, favorites=qp.get("favorites") in ("1", "true")))
    except (LookupError, ValueError, TypeError) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, out)


async def favorites_api(request):
    """GET /api/favorites?project=&set= – favourite slides. POST /api/favorites {refs, on: true|false} (fundus.favorites)."""
    from fundus import favorites
    if request.method == "GET":
        qp = request.query_params
        return _api_call(request, lambda st: favorites.listing(st, qp.get("project") or None, qp.get("set") or None))
    body, err = await _json_body(request)
    if err:
        return err
    return _api_call(request, lambda st: favorites.mark(st, body.get("refs") or [], bool(body.get("on", True)), actor="portal"))


async def collections_api(request):
    """GET /api/collections (saved searches + creation years) | /api/collections/<id>. POST /api/collections
    {name, question, filters, aspects, slides} saves; POST /api/collections/<id> {action: update | delete | rerun, …}."""
    from fundus import slide_search as ss
    cid = request.path_params.get("cid")
    if request.method == "GET":
        if cid:
            return _api_call(request, ss.get, cid)
        return _api_call(request, lambda st: {"saved_searches": ss.list_collections(st), "years": ss.years(st)})
    body, err = await _json_body(request)
    if err:
        return err
    if not cid:
        return _api_call(request, ss.save, body.get("name"), body.get("slides") or [], question=body.get("question"),
                         filters=body.get("filters"), aspects=body.get("aspects"), note=body.get("note"), by="portal")
    act = body.get("action")
    if act == "update":
        return _api_call(request, ss.update, cid, name=body.get("name"), note=body.get("note"), add=body.get("add"),
                         remove=body.get("remove"), aspect=body.get("aspect"))
    if act == "delete":
        return _api_call(request, ss.delete, cid)
    if act == "rerun":
        import anyio
        try:
            out = await anyio.to_thread.run_sync(lambda: ss.rerun(Store(_db_path), cid))
        except (LookupError, ValueError) as e:
            return _api_response(request, {"error": str(e)}, 400)
        return _api_response(request, out)
    return _api_response(request, {"error": "action must be update, delete or rerun"}, 400)


def _check_links_later(rid: int) -> None:
    threading.Thread(target=rv.check_review_links, args=(_db_path, rid), daemon=True, name=f"links-{rid}").start()


# Only the long-running server drives loops and runs Gemini checks in background threads. A short-lived process that
# imports this module (a session calling fundus_feedback_resolve from Python) would start a round and die with it –
# the review then stays 'running' forever (Northwind, 26.10.). There the server's ticker starts the round within 20 s.
_IN_SERVER = False


def _loop_tick() -> None:
    from fundus import review_loop as rl
    if not _IN_SERVER:
        return
    try:
        with _lock:
            rl.tick(_store(), _start_gemini)
            rv.pin_verified(_store())          # fix every version that reached 'checked' / 'confirmed'
    except Exception:
        pass


def _loop_ticker() -> None:
    """Drive the Gemini check loops (fundus_review action='loop') every 20 s."""
    import time
    while True:
        time.sleep(20)
        _loop_tick()


def _start_gemini(rid: int, retry: bool = False) -> None:
    from fundus import gemini as gm
    # outside the server: a non-daemon thread, so the process waits for the check instead of killing it on exit
    gm.start(_db_path, rid, retry=retry, daemon=_IN_SERVER)


async def reviewers_api(request):
    """GET /api/reviewers – known people (Name, Vorname) for the picker. POST {name, org} adds one; {id, delete: true}
    hides a person and revokes their links."""
    if request.method == "GET":
        return _api_call(request, lambda st: {"reviewers": rv.list_reviewers(st)})
    body, err = await _json_body(request)
    if err:
        return err
    if body.get("delete"):
        return _api_call(request, rv.delete_reviewer, body.get("id"), actor="portal")
    return _api_call(request, rv.add_reviewer, str(body.get("name") or ""), org=body.get("org"), actor="portal")


async def reviews_api(request):
    """GET /api/reviews[?project=p] | /api/reviews/<id>. POST /api/reviews {project, slides | set | deck, title, people,
    gemini, focus, expires_days} creates a review (then checks its source links in the background);
    POST /api/reviews/<id> {action: invite | revoke | close, people | person}."""
    rid = request.path_params.get("rid")
    if request.method == "GET":
        if request.url.path.endswith("/models"):
            from fundus import gemini as gm
            return _api_call(request, lambda st: gm.models())
        if rid:
            return _api_call(request, rv.get, rid)
        from fundus import review_loop as rl
        return _api_call(request, lambda st: {"reviews": rv.list_reviews(st, request.query_params.get("project") or None),
                                              "reviewers": rv.list_reviewers(st),
                                              "loops": rl.list_loops(st, request.query_params.get("project") or None)})
    body, err = await _json_body(request)
    if err:
        return err
    if rid:
        act = body.get("action")
        if act == "invite":
            return _api_call(request, rv.invite, rid, body.get("people") or [], actor="portal")
        if act == "revoke":
            return _api_call(request, rv.revoke, rid, body.get("person"), actor="portal")
        if act == "close":
            return _api_call(request, rv.close, rid, actor="portal")
        if act == "purge":
            return _api_call(request, rv.purge, rid, actor="portal")
        if act == "rerun":
            _start_gemini(int(rid), retry=True)
            return _api_call(request, rv.get, rid)
        return _api_response(request, {"error": "action must be invite, revoke, close or rerun"}, 400)
    people = body.get("people") or []
    if not isinstance(people, list):
        return _api_response(request, {"error": "'people' must be a list"}, 400)
    resp = _api_call(request, rv.create, str(body.get("project") or ""), slides=body.get("slides") or None,
                     set_ref=body.get("set"), deck=body.get("deck"), title=body.get("title"), people=people,
                     gemini=bool(body.get("gemini")), focus=body.get("focus"), expires_days=body.get("expires_days") or rv.EXPIRES_DAYS,
                     model=body.get("model") or None, actor="portal")
    if resp.status_code == 200:
        made = json.loads(resp.body)
        _check_links_later(made["id"])
        if made.get("gemini"):
            _start_gemini(made["id"])
    return resp


async def feedback_api(request):
    """GET /api/feedback?project&set&review&exhibit&status – the inbox grouped per slide (no claiming);
    GET /api/feedback/counts?project – open points per slide. POST {exhibit, text, severity} adds Falk's own point;
    POST {id, action: 'reopen', instruction} reopens one (e.g. after a decision)."""
    qp = request.query_params
    if request.method == "GET":
        if request.url.path.endswith("/counts"):
            return _api_call(request, lambda st: {**rv.open_counts(st, qp.get("project") or None),
                                                  "status": rv.slide_status(st, qp.get("project") or None) if qp.get("project") else {}})
        return _api_call(request, rv.inbox, project=qp.get("project") or None, set_ref=qp.get("set") or None,
                         review=qp.get("review") or None, exhibit=qp.get("exhibit") or None, status=qp.get("status") or "open")
    body, err = await _json_body(request)
    if err:
        return err
    if body.get("action") == "reopen":
        return _api_call(request, rv.reopen, body.get("id"), body.get("instruction"), actor="portal")
    return _api_call(request, rv.add, body.get("exhibit"), str(body.get("text") or ""), source="owner",
                     severity=body.get("severity") or None, version=body.get("version"), actor="portal")


async def slide_review_api(request):
    """GET /api/slide-review/<exhibit> – what the portal shows next to a slide: sources and data points with verification
    stage and link check, all feedback on the slide (any status) and its change log."""
    ref = request.path_params.get("ref")
    return _api_call(request, lambda st: {**rv.slide_evidence(st, ref), "feedback": rv.inbox(st, exhibit=ref, status="all")["slides"],
                                          "changelog": rv.changelog(st, ref)})


PORTAL_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
                ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}


async def portal_file(request):
    """GET /portal/<path> – static files of the Fundus portal (portal/ in the repository); '/' and '/portal' lead here."""
    from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
    rel = request.path_params.get("path", "")
    if request.url.path in ("/", "/portal"):
        return RedirectResponse("/portal/", status_code=307)
    target = (PORTAL_DIR / (rel or "index.html")).resolve()
    if PORTAL_DIR.resolve() not in target.parents or not target.is_file() or target.suffix not in PORTAL_TYPES:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(target, media_type=PORTAL_TYPES[target.suffix],
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


async def site_icon(request):
    """GET /favicon.ico and /apple-touch-icon*.png – browsers (Safari on iPad/iPhone) ask the site root for these."""
    from starlette.responses import FileResponse
    name = "favicon.ico" if request.url.path.endswith(".ico") else "apple-touch-icon.png"
    return FileResponse(PORTAL_DIR / "icons" / name, media_type=PORTAL_TYPES[Path(name).suffix],
                        headers={"Cache-Control": "public, max-age=604800"})


async def exhibit_api(request):
    """GET /api/exhibits[?project=p[&set=<id or slug>]] and /api/exhibits/<id | project/slug | project/set/slug> –
    read-only JSON for the web gallery/portal (admin view of the own archive: internal exhibits included; Tailnet only
    like the MCP endpoint)."""
    ref = request.path_params.get("ref")
    try:
        with _lock:
            st = _store()
            if ref:
                data = ex.get(st, ref, include_internal=True, script=False)
            else:
                data = ex.gallery(st, request.query_params.get("project") or None,
                                  set_ref=request.query_params.get("set") or None)
    except PermissionError as e:
        return _api_response(request, {"error": str(e)}, 403)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    return _api_response(request, data)


# ---------------------------------------------------------------- photo archive: files, portal API, background annotation
_photo_state = {"enabled": False, "current": None, "done": 0, "failed": 0, "last_error": None, "waiting_since": None}


def _photo_worker() -> None:
    """Annotate new photos one by one with the local vision model (own connection, never holds the store lock during the
    model call). Waits while Ollama is unreachable or over an optional FUNDUS_MAX_MODELS limit; wakes on imports (_photo_wake) or every minute."""
    from fundus import embed, photos
    _photo_state["enabled"] = True
    while True:
        _photo_wake.wait(60)
        _photo_wake.clear()
        try:
            st = Store(_db_path)
        except sqlite3.Error:
            continue
        try:
            while True:
                ids = photos.pending(st)
                if not ids:
                    break
                llm = photos.OllamaVision()
                try:
                    llm.check_capacity()
                except embed.EmbedUnavailable as e:
                    _photo_state.update(last_error=str(e), waiting_since=_photo_state["waiting_since"] or time.strftime("%H:%M"))
                    time.sleep(120)
                    continue
                _photo_state.update(current=ids[0], waiting_since=None)
                try:
                    photos.annotate_one(st, ids[0], llm)
                    _photo_state["done"] += 1
                except Exception as e:
                    _photo_state["failed"] += 1
                    _photo_state["last_error"] = f"photo {ids[0]}: {e}"[:300]
                _photo_state["current"] = None
                time.sleep(1)               # leave gaps for other users of the shared model
            photos.embed_pending(st)
            from fundus import datasets     # new datasets: described by the same local model, one by one
            for did in datasets.pending(st, retry_timeouts=True):
                try:
                    datasets.describe_one(st, did)
                except Exception as e:
                    _photo_state["last_error"] = f"dataset {did}: {e}"[:300]
                    try:   # visible in the portal ("Beschreibung fehlgeschlagen") instead of being retried silently each minute
                        st.db.rollback()
                        st.db.execute("UPDATE dataset SET status = 'failed', error = ? WHERE id = ? AND status = 'new'",
                                      (f"{type(e).__name__}: {e}"[:500], did))
                        st.db.commit()
                    except sqlite3.Error:
                        pass
                time.sleep(1)
        except Exception as e:              # never let the worker die
            _photo_state["last_error"] = str(e)[:300]
        finally:
            st.close()


async def photo_file(request):
    """GET /photos/<sha256>.thumb.jpg | .view.jpg | .slide.jpg | .orig.<ext> | .video.mp4 | .mute.mp4 | .sheet.jpg – photo files (derived JPEGs carry no
    metadata; originals as imported, as download)."""
    from fundus import photos
    from starlette.responses import FileResponse, PlainTextResponse
    try:
        with _lock:
            path, ctype, dl = photos.resolve_file(_store(), request.path_params.get("name", ""))
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except LookupError:
        return PlainTextResponse("not found", status_code=404)
    if path.name.endswith(".mute.mp4") and not path.exists():
        try:
            await asyncio.to_thread(photos.ensure_mute, Store(_db_path), path.name.split(".")[0])
        except Exception as e:
            return PlainTextResponse(f"no muted copy: {e}", status_code=500)
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
    return FileResponse(path, media_type=ctype, headers=headers, filename=dl or None)


def _photo_query(st: Store, qp) -> dict:
    from fundus import photos
    dims = {}
    if qp.get("dims"):
        dims = json.loads(qp.get("dims"))
        if not isinstance(dims, dict):
            raise ValueError("dims must be a JSON object")
    return photos.search(st, qp.get("q") or "", dims=dims, purpose=qp.get("purpose") or "all", group=qp.get("group") or None,
                         status=qp.get("status") or None, orientation=qp.get("orientation") or None,
                         people=qp.get("people") or None, faces=qp.get("faces") or None, sort=qp.get("sort") or None,
                         limit=min(int(qp.get("limit") or 120), 500), offset=int(qp.get("offset") or 0),
                         removed=qp.get("removed") == "1", media=qp.get("media") or None)


async def photos_api(request):
    """GET /api/photos?q=&dims={"setting":[…]}&group=&status=new|annotated|confirmed|failed|unconfirmed&orientation=
    &people=none|some&faces=&purpose=all&sort=taken|imported&limit=&offset=&removed=1 – search/list (portal: all rights).
    GET /api/photos/meta – vocabulary with counts, groups, stats, state of the background annotation.
    GET /api/photos/<id> – one photo. POST /api/photos/<id> {title, note, place, taken_at, credit, consent, usage, faces,
    sharing, tags: {add, confirm, reject, remove}, action: confirm | reannotate | remove | restore}.
    POST /api/photos/bulk {ids, action: tag | confirm | reannotate | remove | restore | update | group_add | group_remove,
    tags, changes, group}."""
    import anyio
    from fundus import photos
    pid = request.path_params.get("pid")
    if request.method == "GET":
        if request.url.path.endswith("/meta"):
            return _api_call(request, lambda st: {"dims": photos.dims(st), "groups": photos.groups(st), "stats": photos.stats(st),
                                                   "worker": {**_photo_state, "pending": len(photos.pending(st))}})
        if pid is not None:
            return _api_call(request, photos.get, pid)
        def run():
            with _lock:
                return _photo_query(_store(), request.query_params)
        try:
            return _api_response(request, await anyio.to_thread.run_sync(run))
        except LookupError as e:
            return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
        except (ValueError, TypeError, sqlite3.Error) as e:
            return _api_response(request, {"error": str(e)}, 400)
    body, err = await _json_body(request)
    if err:
        return err

    def one(st, pid):
        changes = {k: v for k, v in body.items() if k in photos.EDITABLE}
        out = photos.update(st, pid, changes) if changes else None
        t = body.get("tags") or {}
        if t:
            out = photos.set_tags(st, pid, add=t.get("add"), confirm=t.get("confirm"), reject=t.get("reject"), remove=t.get("remove"))
        act = body.get("action")
        if act == "confirm":
            photos.confirm(st, pid)
        elif act == "reannotate":
            photos.reannotate(st, pid)
            _photo_wake.set()
        elif act in ("remove", "restore"):
            photos.remove(st, [pid], restore=act == "restore")
        elif act:
            raise ValueError("action: confirm | reannotate | remove | restore")
        return photos.get(st, pid)

    def bulk(st):
        ids = [int(i) for i in body.get("ids") or []]
        if not ids:
            raise ValueError("no ids")
        act = body.get("action")
        if act == "tag":
            t = body.get("tags") or {}
            return photos.set_tags(st, ids, add=t.get("add"), confirm=t.get("confirm"), reject=t.get("reject"), remove=t.get("remove"))
        if act == "confirm":
            return photos.confirm(st, ids)
        if act == "reannotate":
            _photo_wake.set()
            return photos.reannotate(st, ids)
        if act in ("remove", "restore"):
            return photos.remove(st, ids, restore=act == "restore")
        if act == "update":
            for i in ids:
                photos.update(st, i, body.get("changes") or {})
            return {"updated": len(ids)}
        if act in ("group_add", "group_remove"):
            g = body.get("group")
            gid = photos.create_group(st, g.get("kind", "album"), g["title"])["id"] if isinstance(g, dict) else int(g)
            fn = photos.add_to_group if act == "group_add" else photos.remove_from_group
            return fn(st, gid, ids)
        raise ValueError("action: tag | confirm | reannotate | remove | restore | update | group_add | group_remove")

    if request.url.path.endswith("/bulk"):
        return _api_call(request, bulk)
    return _api_call(request, one, pid)


async def photo_groups_api(request):
    """GET /api/photo-groups. POST /api/photo-groups {kind: survey|album, title, project, period, place, description};
    POST /api/photo-groups/<id> {action: update | delete, …} (deleting a group never deletes photos)."""
    from fundus import photos
    gid = request.path_params.get("gid")
    if request.method == "GET":
        return _api_call(request, photos.groups)
    body, err = await _json_body(request)
    if err:
        return err
    if gid is None:
        return _api_call(request, lambda st: photos.create_group(st, body.get("kind") or "album", body.get("title") or "",
                                                                 **{k: body.get(k) for k in ("project", "period", "place", "description")}))
    if body.get("action") == "delete":
        return _api_call(request, photos.delete_group, gid)
    return _api_call(request, lambda st: photos.update_group(st, gid, **{k: v for k, v in body.items() if k != "action"}))


async def trash_api(request):
    """GET /api/trash[?state=trashed|restored|purged] – the trash. POST /api/trash {action: trash (target slide|set|project,
    refs) | restore (id) | purge (ids) | empty} – Falk in the portal; emptying deletes for good."""
    from fundus import trash
    if request.method == "GET":
        return _api_call(request, lambda st: trash.listing(st, request.query_params.get("state") or "trashed"))
    body, err = await _json_body(request)
    if err:
        return err
    a = body.get("action")
    if a == "trash":
        return _api_call(request, lambda st: trash.trash(st, body.get("target") or "", body.get("refs") or [],
                                                         project=body.get("project"), actor="portal"))
    if a == "restore":
        return _api_call(request, lambda st: trash.restore(st, body.get("id"), actor="portal"))
    if a == "purge":
        return _api_call(request, lambda st: trash.purge(st, ids=body.get("ids") or [], actor="portal"))
    if a == "empty":
        return _api_call(request, lambda st: trash.purge(st, everything=True, actor="portal"))
    return _api_response(request, {"error": "action: trash | restore | purge | empty"}, 400)


async def archive_api(request):
    """GET /api/archive?q=&project=&limit=&offset= – archived slides. POST /api/archive {action: archive | unarchive,
    target: slide | set | project, refs} (fundus.archive)."""
    from fundus import archive
    if request.method == "GET":
        qp = request.query_params
        return _api_call(request, lambda st: archive.listing(st, qp.get("q"), qp.get("project"), min(int(qp.get("limit") or 200), 500),
                                                             int(qp.get("offset") or 0)))
    body, err = await _json_body(request)
    if err:
        return err
    fn = {"archive": archive.archive, "unarchive": archive.unarchive}.get(body.get("action"))
    if fn is None:
        return _api_response(request, {"error": "action: archive | unarchive"}, 400)
    return _api_call(request, lambda st: fn(st, body.get("target") or "slide", body.get("refs") or [], project=body.get("project"),
                                            actor="portal"))


async def received_api(request):
    """Slide packages from other Fundus installations (fundus.package). GET /api/received – list · /api/received/<id> –
    the original as it arrived, with the plan (what accepting would do) and, after an accept, the own copies ·
    /api/received/<id>/file/<sha>.<ext> – a file of the original (hash names only; HTML sandboxed).
    POST /api/received/<id> {action: accept (project, slides, decisions) | reject | install_skill (force) | plan}."""
    from starlette.responses import FileResponse, PlainTextResponse
    from fundus import package as pk
    rid = request.path_params.get("rid")
    if request.method == "GET":
        name = request.path_params.get("name")
        if name is not None:
            try:
                with _lock:
                    path, ctype = pk.received_file(_store(), rid, name)
            except ValueError as e:
                return PlainTextResponse(str(e), status_code=400)
            except LookupError:
                return PlainTextResponse("not found", status_code=404)
            headers = {"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"}
            if ctype.startswith("text/html") or ctype.startswith("image/svg"):
                headers["Content-Security-Policy"] = "sandbox allow-scripts"     # someone else's HTML: never our origin
            return FileResponse(path, media_type=ctype, headers=headers)
        if rid is None:
            return _api_call(request, pk.received_list)
        return _api_call(request, pk.detail, rid)
    body, err = await _json_body(request)
    if err:
        return err
    a = body.get("action")
    if a == "accept":
        return _api_call(request, lambda st: pk.accept(st, rid, body.get("project") or "", slides=body.get("slides") or None,
                                                       decisions=body.get("decisions") or None, actor="portal"))
    if a == "reject":
        return _api_call(request, lambda st: pk.reject(st, rid, actor="portal"))
    if a == "install_skill":
        return _api_call(request, lambda st: pk.install_skill(st, rid, force=bool(body.get("force")), actor="portal"))
    if a == "plan":
        return _api_call(request, pk.plan, rid)
    return _api_response(request, {"error": "action: accept | reject | install_skill | plan"}, 400)


async def sent_api(request):
    """GET /api/sent – packages sent or exported for sending (fundus.package.record_sent)."""
    from fundus import package as pk
    return _api_call(request, pk.sent_list)


def _own_call(request, fn, *a, **kw):
    """Like _api_call, on an own connection without the server lock (dataset previews and queries can take a while)."""
    st = Store(_db_path)
    try:
        return _api_response(request, fn(st, *a, **kw))
    except PermissionError as e:
        return _api_response(request, {"error": str(e)}, 403)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, TypeError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else str(e)}, 400)
    finally:
        st.close()


async def datasets_api(request):
    """GET /api/datasets?q=&project=&removed=1 – list/search (portal: all, internal included). POST /api/datasets/ask
    {question, project} – free-text question answered by the local model (fundus.dataset_ask). GET /api/datasets/<id> –
    detail. POST /api/datasets/<id> {action: update (changes) | remove | restore | redescribe}.
    GET /api/datasets/<id>/preview?table=&offset=&limit=&order=col:desc. POST /api/datasets/<id>/query {sql}."""
    from starlette.concurrency import run_in_threadpool
    from fundus import datasets
    did, sub = request.path_params.get("did"), request.path_params.get("sub")
    qp = request.query_params
    if did is None and request.method == "POST" and request.url.path.rstrip("/").endswith("/ask"):
        body, err = await _json_body(request)       # free-text question to the local model (1–2 min, own connection)
        if err:
            return err
        from fundus import dataset_ask
        return await run_in_threadpool(_own_call, request, lambda st: dataset_ask.ask(
            st, body.get("question") or "", include_internal=True, project=body.get("project") or None))
    if did is None and qp.get("ref"):
        return _api_call(request, datasets.get, qp.get("ref"))
    if did is None:
        return _api_call(request, lambda st: {**datasets.search(st, qp.get("q") or "", project=qp.get("project") or None,
                                                                 include_internal=True, removed=qp.get("removed") == "1",
                                                                 limit=min(int(qp.get("limit") or 200), 500)),
                                               "stats": datasets.stats(st)})
    if sub == "preview":
        return await run_in_threadpool(_own_call, request, lambda st: datasets.preview(
            st, did, table=qp.get("table") or None, offset=int(qp.get("offset") or 0), limit=int(qp.get("limit") or 100),
            order=qp.get("order") or None))
    if request.method == "GET":
        return _api_call(request, datasets.get, did)
    body, err = await _json_body(request)
    if err:
        return err
    if sub == "query":
        return await run_in_threadpool(_own_call, request, lambda st: datasets.query(st, did, body.get("sql") or "", limit=500))
    a = body.get("action")
    if a == "update":
        return _api_call(request, lambda st: datasets.update(st, did, body.get("changes") or {}, actor="portal"))
    if a in ("remove", "restore"):
        return _api_call(request, lambda st: datasets.remove(st, did, restore=a == "restore", actor="portal"))
    if a == "checked":
        return _api_call(request, lambda st: (datasets.mark_checked(st, did, next_expected=body.get("next_expected") or None,
                                                                    actor="portal"), datasets.get(st, did))[1])
    if a == "redescribe":
        r = _api_call(request, datasets.redescribe, did)
        _photo_wake.set()
        return r
    return _api_response(request, {"error": "action: update | remove | restore | redescribe"}, 400)


async def dataset_upload(request):
    """POST /api/datasets/upload (multipart: files[], title, description, source_title, publisher, url, license, published,
    project, visibility, note, dataset = new version of) – the portal's upload."""
    import anyio
    from fundus import datasets
    if not _same_origin(request):
        return _api_response(request, {"error": "cross-origin write refused"}, 403)
    try:
        form = await request.form(max_files=500, max_fields=50, max_part_size=datasets.MAX_BYTES)
    except Exception as e:
        return _api_response(request, {"error": f"upload failed: {e}"}, 400)
    with _lock:
        base = datasets.files_dir(_store()) / "incoming" / f"upload-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        for f in form.getlist("files"):
            if not getattr(f, "filename", None):
                continue
            name = Path(f.filename).name
            if not name or name.startswith("."):
                continue
            target = base / f"{n:04d}" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                await anyio.to_thread.run_sync(shutil.copyfileobj, f.file, out)
            n += 1
        if not n:
            return _api_response(request, {"error": "no files"}, 400)
        meta = {k: (form.get(k) or None) for k in ("title", "description", "source_title", "publisher", "license", "published",
                                                   "project", "note", "dataset")}

        def run():
            st = Store(_db_path)
            try:
                return datasets.add(st, [base], url_=form.get("url") or None, visibility=form.get("visibility") or "public",
                                    actor="portal", **meta)
            finally:
                st.close()
        try:
            res = await anyio.to_thread.run_sync(run)
        except (ValueError, LookupError, FileNotFoundError, sqlite3.Error) as e:
            return _api_response(request, {"error": str(e)}, 400)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    _photo_wake.set()
    return _api_response(request, res)


async def dataset_file(request):
    """GET /datasets/<sha256>.<ext>[?name=…] – a stored dataset file (original, document or Parquet copy) as download."""
    from fundus import datasets
    from starlette.responses import FileResponse, PlainTextResponse
    try:
        with _lock:
            path, ctype = datasets.resolve_file(_store(), request.path_params.get("name", ""))
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    except LookupError:
        return PlainTextResponse("not found", status_code=404)
    name = Path(request.query_params.get("name") or path.name).name
    return FileResponse(path, media_type=ctype, filename=name, headers={"X-Content-Type-Options": "nosniff"})


async def move_api(request):
    """POST /api/exhibits/move {target: slide|set|project, refs, to} – move slides into a set, a set into a project, or
    merge a project into another (fundus.trash.move)."""
    from fundus import trash
    body, err = await _json_body(request)
    if err:
        return err
    return _api_call(request, lambda st: trash.move(st, body.get("target") or "", body.get("refs") or [], body.get("to") or "",
                                                    project=body.get("project"), actor="portal"))


_cleanup_job: dict = {}                      # the one running clean-up job of the portal (apply/decide/undo/analyze)
_cleanup_guard = threading.Lock()


def _cleanup_thread(action: str, kw: dict) -> None:
    from fundus import cleanup
    st = Store(_db_path)                     # own connection, no server lock: a long run must not block the tools
    try:
        fn = {"analyze": cleanup.analyze, "apply": cleanup.apply, "decide": cleanup.decide, "undo": cleanup.undo}[action]
        res = fn(st, actor="portal", **kw)
        _cleanup_job.update(state="done", result=res, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:                   # the portal shows it
        _cleanup_job.update(state="failed", error=f"{type(e).__name__}: {e}", finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        st.close()
        _stats_cache.clear()                 # the statistics show the new score right away


async def cleanup_api(request):
    """Data hygiene for the statistics (fundus.cleanup).
    GET /api/cleanup → status (score, open findings per class, trend, recent runs, the running job);
    GET /api/cleanup?view=candidates&class=probable&state=open&limit=&offset= → findings with the records side by side;
    GET /api/cleanup?view=runs. POST {action: analyze | apply | undo (run) | decide (ids, decision, keep)} – runs in the
    background (one job at a time); poll GET for `job`."""
    from fundus import cleanup
    if request.method == "GET":
        qp = request.query_params
        view = qp.get("view") or "status"
        if view == "candidates":
            return _api_call(request, lambda st: cleanup.candidates(st, qp.get("class") or None, qp.get("state") or "open",
                                                                    min(int(qp.get("limit") or 50), 200),
                                                                    int(qp.get("offset") or 0)))
        if view == "runs":
            return _api_call(request, lambda st: {"runs": cleanup.runs(st, 50)})
        return _api_call(request, lambda st: {**cleanup.status(st), "job": dict(_cleanup_job)})
    body, err = await _json_body(request)
    if err:
        return err
    action = body.get("action")
    if action == "decide" and body.get("decision") in ("reject", "later", "reopen"):   # instant, no job
        _stats_cache.clear()
        return _api_call(request, lambda st: cleanup.decide(st, body.get("ids") or [], body["decision"], actor="portal"))
    kw = {}
    if action == "decide":
        kw = {"ids": body.get("ids") or [], "action": body.get("decision"), "keep": body.get("keep")}
    elif action == "undo":
        kw = {"run_id": int(body.get("run") or 0)}
    elif action not in ("analyze", "apply"):
        return _api_response(request, {"error": "action: analyze | apply | decide | undo"}, 400)
    with _cleanup_guard:
        if _cleanup_job.get("state") == "running":
            return _api_response(request, {"error": "a clean-up run is still going", "job": dict(_cleanup_job)}, 409)
        _cleanup_job.clear()
        _cleanup_job.update(state="running", action=action, started=time.strftime("%Y-%m-%d %H:%M:%S"))
        threading.Thread(target=_cleanup_thread, args=(action, kw), name="cleanup", daemon=True).start()
    return _api_response(request, {"job": dict(_cleanup_job)}, 202)


async def photo_upload(request):
    """POST /api/photos/upload (multipart: files[], survey, project, period, place, consent, usage, note) – the portal's
    upload; files land in data/photos/incoming/ and are imported like a folder (decks/PDFs: their pictures and videos;
    videos are transcoded before the answer comes)."""
    import anyio
    import shutil
    from fundus import photos
    if not _same_origin(request):
        return _api_response(request, {"error": "cross-origin write refused"}, 403)
    try:
        from fundus.videos import MAX_VIDEO_BYTES
        form = await request.form(max_files=2000, max_fields=50, max_part_size=max(photos.MAX_FILE_BYTES, MAX_VIDEO_BYTES))
    except Exception as e:
        return _api_response(request, {"error": f"upload failed: {e}"}, 400)
    with _lock:
        base = photos.files_dir(_store()) / "incoming" / f"upload-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in form.getlist("files"):
        if not hasattr(f, "filename") or not f.filename:
            continue
        name = Path(f.filename).name.replace("/", "_")
        if not name or name.startswith("."):
            continue
        target = base / f"{n:04d}" / name             # own folder per file: same names from different folders stay apart
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            await anyio.to_thread.run_sync(shutil.copyfileobj, f.file, out)
        n += 1
    if not n:
        return _api_response(request, {"error": "no files"}, 400)
    meta = {k: (form.get(k) or None) for k in ("survey", "project", "period", "place", "note", "album")}

    def run():                                  # own connection, no server lock (see _run_own)
        st = Store(_db_path)
        try:
            return photos.import_paths(st, [base], consent=form.get("consent") or photos.DEFAULT_CONSENT,
                                       usage=form.get("usage") or photos.DEFAULT_USAGE, **meta)
        finally:
            st.close()
    try:
        res = await anyio.to_thread.run_sync(run)
    except (ValueError, LookupError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e)}, 400)
    _photo_wake.set()
    return _api_response(request, res)


def site_text(text: str) -> str:
    """Fill the install-specific placeholders of instructions and tool descriptions: {PORTAL} (portal URL of this
    install) and {FUNDUS_CLI} (this install's interpreter running the Fundus CLI)."""
    import sys
    py = sys.executable or "python"
    cli = f'"{py}" -m fundus.cli' if " " in py else f"{py} -m fundus.cli"
    return text.replace("{PORTAL}", f"{ex.PUBLIC_URL}/portal/").replace("{FUNDUS_CLI}", cli)


# ---------------------------------------------------------------- Fundus Cloud (fundus/cloud.py)
_cloud_jobs: dict[str, dict] = {}


def _cloud_dir():
    from fundus import cloud
    return cloud.cloud_dir(_db_path)


def _cloud_call(request, fn, *a, **kw):
    """Run a Fundus Cloud call (network, Argon2) – in a worker thread via run_in_threadpool – and map errors to codes."""
    from fundus import cloud, cloud_crypto as cc
    try:
        return _api_response(request, fn(*a, **kw))
    except PermissionError as e:
        return _api_response(request, {"error": str(e)}, 403)
    except LookupError as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else "not found"}, 404)
    except (ValueError, TypeError, cc.CryptoError, sqlite3.Error) as e:
        return _api_response(request, {"error": str(e.args[0]) if e.args else str(e)}, 400)
    except ImportError as e:
        return _api_response(request, {"error": "Folienpakete (fundus/package.py) sind in diesem Fundus noch nicht installiert"
                                                + (f" ({e.name})" if getattr(e, "name", None) else "")}, 501)
    except cloud.CloudError as e:
        return _api_response(request, {"error": str(e)}, 502)


def _cloud_store_call(fn):
    st = Store(_db_path)
    st.actor = "portal"
    try:
        return fn(st)
    finally:
        st.close()


def _cloud_share_job(job: dict, refs, recipients, confirm_internal: bool) -> None:
    from fundus import cloud

    def progress(phase, done, total):
        job.update(phase=phase, done=done, total=total)
    try:
        res = _cloud_store_call(lambda st: cloud.share(st, refs, recipients, confirm_internal=confirm_internal,
                                                       actor="portal", progress=progress))
        job.update(state="confirm" if res.get("needs_confirmation") else "done", result=res, phase="done")
    except Exception as e:     # noqa: BLE001 – reported to the portal
        job.update(state="error", error=str(e.args[0]) if e.args else type(e).__name__)
    job["finished"] = time.time()


async def cloud_api(request):
    """Fundus Cloud for the portal (Verwaltung, Teilen):
    GET  /api/cloud/status · /api/cloud/contacts · /api/cloud/poll (local package log) · /api/cloud/share?job=<id>
    POST /api/cloud/connect {email, password} · /api/cloud/disconnect · /api/cloud/contacts {email, action}
         /api/cloud/lookup {emails} · /api/cloud/share {action: check, refs} | {action: start, refs, recipients,
         confirm_internal} (runs as a job; poll GET ?job=) · /api/cloud/poll (pick up now)."""
    from starlette.concurrency import run_in_threadpool
    from fundus import cloud
    sub = request.path_params.get("sub", "status")
    cdir = _cloud_dir()
    if request.method == "GET":
        if sub == "status":
            return await run_in_threadpool(_cloud_call, request, cloud.status, cdir)
        if sub == "contacts":
            return await run_in_threadpool(_cloud_call, request, cloud.contacts, cdir)
        if sub == "poll":
            return await run_in_threadpool(_cloud_call, request, cloud.inbox, cdir)
        if sub == "share":
            job = _cloud_jobs.get(request.query_params.get("job") or "")
            if not job:
                return _api_response(request, {"error": "kein solcher Auftrag"}, 404)
            return _api_response(request, job)
        return _api_response(request, {"error": "not found"}, 404)
    body, err = await _json_body(request)
    if err:
        return err
    if sub == "connect":
        return await run_in_threadpool(_cloud_call, request, cloud.connect, cdir, str(body.get("email") or ""),
                                       str(body.get("password") or ""))
    if sub == "disconnect":
        return await run_in_threadpool(_cloud_call, request, cloud.disconnect, cdir)
    if sub == "contacts":
        return await run_in_threadpool(_cloud_call, request, cloud.set_contact, cdir, str(body.get("email") or ""),
                                       str(body.get("action") or ""))
    if sub == "lookup":
        emails = body.get("emails") or []
        if not isinstance(emails, list):
            return _api_response(request, {"error": "emails: Liste"}, 400)
        return await run_in_threadpool(_cloud_call, request, cloud.lookup, cdir, [str(e) for e in emails][:50])
    if sub == "poll":
        return await run_in_threadpool(_cloud_call, request, _cloud_store_call, cloud.poll)
    if sub == "share":
        refs = [str(r) for r in (body.get("refs") or [])]
        if body.get("action") == "check":
            return await run_in_threadpool(_cloud_call, request, _cloud_store_call, lambda st: cloud.check_share(st, refs))
        if body.get("action") != "start":
            return _api_response(request, {"error": "action: check | start"}, 400)
        for k in [k for k, j in _cloud_jobs.items() if j.get("finished") and time.time() - j["finished"] > 3600]:
            _cloud_jobs.pop(k, None)
        jid = hashlib.sha1(f"{time.time()}{refs}".encode()).hexdigest()[:12]
        job = _cloud_jobs[jid] = {"job": jid, "state": "running", "phase": "start", "done": 0, "total": 0, "started": time.time()}
        threading.Thread(target=_cloud_share_job, name=f"cloud-share-{jid}", daemon=True,
                         args=(job, refs, [str(e) for e in (body.get("recipients") or [])], bool(body.get("confirm_internal")))).start()
        return _api_response(request, job)
    return _api_response(request, {"error": "not found"}, 404)


def _cloud_poller() -> None:
    """Check the Fundus Cloud inbox every few minutes while connected (only in the long-running server)."""
    from fundus import cloud
    while True:
        time.sleep(cloud.POLL_SECONDS)
        cdir = _cloud_dir()
        if not cloud.connected(cdir):
            continue
        try:
            _cloud_store_call(cloud.poll)
        except Exception as e:     # noqa: BLE001 – keep polling
            try:
                cloud._meta(cdir, "last_error", f"{type(e).__name__}: {e}"[:500])
            except Exception:      # noqa: BLE001
                pass


def build_server():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(name="fundus", title="Fundus evidence archive", instructions=site_text(INSTRUCTIONS), version="0.1.0")
    for fn in TOOLS:
        if fn.__doc__:
            fn.__doc__ = site_text(fn.__doc__)
        server.add_tool(_counted(fn), structured_output=False)
    server.custom_route("/exhibits/{name}", methods=["GET", "HEAD"], name="exhibit_file", include_in_schema=False)(exhibit_file)
    server.custom_route("/api/exhibits", methods=["GET"], name="exhibit_api", include_in_schema=False)(exhibit_api)
    server.custom_route("/api/exhibits/sharing", methods=["POST"], name="exhibit_sharing", include_in_schema=False)(exhibit_sharing)
    server.custom_route("/api/exhibits/move", methods=["POST"], name="move_api", include_in_schema=False)(move_api)
    server.custom_route("/api/names", methods=["POST"], name="names", include_in_schema=False)(names)
    server.custom_route("/api/portal/settings", methods=["POST"], name="portal_settings", include_in_schema=False)(portal_settings)
    server.custom_route("/api/locks", methods=["GET", "POST"], name="locks_api", include_in_schema=False)(locks_api)
    server.custom_route("/api/stats", methods=["GET"], name="stats_api", include_in_schema=False)(stats_api)
    server.custom_route("/api/exhibits/{ref:path}", methods=["GET"], name="exhibit_api_one", include_in_schema=False)(exhibit_api)
    server.custom_route("/api/reviewers", methods=["GET", "POST"], name="reviewers_api", include_in_schema=False)(reviewers_api)
    server.custom_route("/api/reviews", methods=["GET", "POST"], name="reviews_api", include_in_schema=False)(reviews_api)
    server.custom_route("/api/reviews/models", methods=["GET"], name="review_models", include_in_schema=False)(reviews_api)
    server.custom_route("/api/reviews/{rid:int}", methods=["GET", "POST"], name="review_api", include_in_schema=False)(reviews_api)
    server.custom_route("/api/feedback", methods=["GET", "POST"], name="feedback_api", include_in_schema=False)(feedback_api)
    server.custom_route("/api/feedback/counts", methods=["GET"], name="feedback_counts", include_in_schema=False)(feedback_api)
    server.custom_route("/api/slide-search", methods=["GET"], name="slide_search", include_in_schema=False)(slide_search_api)
    server.custom_route("/api/collections", methods=["GET", "POST"], name="collections_api", include_in_schema=False)(collections_api)
    server.custom_route("/api/collections/{cid:int}", methods=["GET", "POST"], name="collection_api", include_in_schema=False)(collections_api)
    server.custom_route("/api/slide-review/{ref:path}", methods=["GET"], name="slide_review", include_in_schema=False)(slide_review_api)
    server.custom_route("/photos/{name}", methods=["GET", "HEAD"], name="photo_file", include_in_schema=False)(photo_file)
    server.custom_route("/api/cleanup", methods=["GET", "POST"], name="cleanup_api", include_in_schema=False)(cleanup_api)
    server.custom_route("/api/trash", methods=["GET", "POST"], name="trash_api", include_in_schema=False)(trash_api)
    server.custom_route("/api/archive", methods=["GET", "POST"], name="archive_api", include_in_schema=False)(archive_api)
    server.custom_route("/api/favorites", methods=["GET", "POST"], name="favorites_api", include_in_schema=False)(favorites_api)
    server.custom_route("/api/received", methods=["GET"], name="received_api", include_in_schema=False)(received_api)
    server.custom_route("/api/received/{rid:int}", methods=["GET", "POST"], name="received_one", include_in_schema=False)(received_api)
    server.custom_route("/api/received/{rid:int}/file/{name}", methods=["GET", "HEAD"], name="received_file",
                        include_in_schema=False)(received_api)
    server.custom_route("/api/sent", methods=["GET"], name="sent_api", include_in_schema=False)(sent_api)
    server.custom_route("/api/datasets", methods=["GET"], name="datasets_api", include_in_schema=False)(datasets_api)
    server.custom_route("/api/datasets/upload", methods=["POST"], name="dataset_upload", include_in_schema=False)(dataset_upload)
    server.custom_route("/api/datasets/ask", methods=["POST"], name="dataset_ask", include_in_schema=False)(datasets_api)
    server.custom_route("/api/datasets/{did:int}", methods=["GET", "POST"], name="dataset_api", include_in_schema=False)(datasets_api)
    server.custom_route("/api/datasets/{did:int}/{sub}", methods=["GET", "POST"], name="dataset_sub", include_in_schema=False)(datasets_api)
    server.custom_route("/datasets/{name}", methods=["GET"], name="dataset_file", include_in_schema=False)(dataset_file)
    server.custom_route("/api/photos", methods=["GET"], name="photos_api", include_in_schema=False)(photos_api)
    server.custom_route("/api/photos/meta", methods=["GET"], name="photos_meta", include_in_schema=False)(photos_api)
    server.custom_route("/api/photos/upload", methods=["POST"], name="photo_upload", include_in_schema=False)(photo_upload)
    server.custom_route("/api/photos/bulk", methods=["POST"], name="photos_bulk", include_in_schema=False)(photos_api)
    server.custom_route("/api/photos/{pid:int}", methods=["GET", "POST"], name="photo_api", include_in_schema=False)(photos_api)
    server.custom_route("/api/photo-groups", methods=["GET", "POST"], name="photo_groups", include_in_schema=False)(photo_groups_api)
    server.custom_route("/api/photo-groups/{gid:int}", methods=["POST"], name="photo_group", include_in_schema=False)(photo_groups_api)
    server.custom_route("/api/cloud/{sub}", methods=["GET", "POST"], name="cloud_api", include_in_schema=False)(cloud_api)
    for path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                 "/apple-touch-icon-{size}.png", "/apple-touch-icon-{size}-precomposed.png"):
        server.custom_route(path, methods=["GET", "HEAD"], name=f"icon{path}", include_in_schema=False)(site_icon)
    for path in ("/", "/portal", "/portal/{path:path}"):
        server.custom_route(path, methods=["GET", "HEAD"], name=f"portal{path}", include_in_schema=False)(portal_file)
    return server


def transport_security(host: str = HOST, port: int = PORT, probe_tailscale: bool = True):
    """DNS-rebinding protection: the Host headers of this machine (bind address, host name, Tailscale name and IPs,
    localhost, config allowed_hosts) – derived at start instead of a fixed list."""
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = list(dict.fromkeys([f"{host}:{port}", *_config.allowed_hosts(host, port, probe_tailscale)]))
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Fundus MCP server (streamable HTTP)")
    ap.add_argument("--db", help=f"database (default $FUNDUS_DB or {DEFAULT_DB})")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--scheduler", choices=("auto", "on", "off"), default="auto",
                    help="in-process job scheduler (import, embed, tidy, snapshot): auto = on unless the systemd timers "
                         "are installed; $FUNDUS_SCHEDULER=1/0 overrides auto")
    a = ap.parse_args(argv)
    if a.db:
        configure(a.db)
    # public URL of this install (config/env, else the bind address) for links; server.json tells CLI processes
    url = _config.announce_server(a.host, a.port, Path(_db_path).parent)
    from fundus import datasets as _ds, photos as _ph
    ex.PUBLIC_URL = _ph.PUBLIC_URL = _ds.PUBLIC_URL = url
    from fundus import scheduler
    on, why = scheduler.enabled({"on": True, "off": False}.get(a.scheduler))
    if on:
        scheduler.start(_db_path)
    print(f"fundus: {url}/portal/ - scheduler {'on' if on else 'off'} ({why})", flush=True)
    global _IN_SERVER
    _IN_SERVER = True
    if os.environ.get("FUNDUS_GEMINI_RESUME", "1") == "1":
        from fundus import gemini as gm
        threading.Timer(5, gm.resume, args=(_db_path,)).start()   # Gemini checks interrupted by a restart
        threading.Thread(target=_loop_ticker, daemon=True, name="loop-ticker").start()
    if os.environ.get("FUNDUS_PHOTO_ANNOTATE", "1") == "1":
        threading.Thread(target=_photo_worker, daemon=True, name="photo-worker").start()
    if os.environ.get("FUNDUS_CLOUD_POLL", "1") == "1":
        threading.Thread(target=_cloud_poller, daemon=True, name="cloud-poller").start()
    build_server().run("streamable-http", host=a.host, port=a.port, streamable_http_path="/mcp",
                       transport_security=transport_security(a.host, a.port))


if __name__ == "__main__":
    main()
