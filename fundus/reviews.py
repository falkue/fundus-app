"""Reviews and the feedback inbox (Fundus Review, stage 0: local foundation).

A review freezes a set of slides (exhibit + version) for checking – by people with a personal link (the review app on
Firebase, later stage) and/or by another AI system (Gemini on Vertex AI, later stage). Both are independent paths: a
Gemini-only review creates no links and publishes nothing.

Everything a reviewer or Falk says about a slide becomes a row in `feedback`: comments, drawings, doubtful or wrong checks,
AI findings. Claude sessions work through it – `inbox()` groups the open points per slide (with PNG, script, sources),
`claim()` marks them in progress, `resolve()` closes each with a comment: incorporated (what changed, in which new version),
answered, rejected (why) or decision (Falk decides). Resolved points form the change log of a slide.

Checks of sources and data points by independent reviewers (people or another AI, never a Claude session) go to
`item_check`: one confirming reviewer = verified, two = confirmed (bestätigt), a current 'wrong' = disputed.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from fundus import exhibits as ex
from fundus.store import ITEM_KINDS, Store

REVIEW_URL = os.environ.get("FUNDUS_REVIEW_URL", "https://fundus-review.web.app").rstrip("/")
SCOPES = ("selection", "set", "deck")
FOCUS = ("sources", "numbers", "logic", "language")
VERDICTS = ("ok", "doubt", "wrong")
SEVERITIES = ("high", "medium", "low")
WEIGHTS = ("error", "suggestion")   # an error blocks the slide's stage until processed; a suggestion never does
OUTCOMES = ("incorporated", "answered", "rejected", "decision")
OUTCOME_WORDS = {"eingearbeitet": "incorporated", "beantwortet": "answered", "zurückgewiesen": "rejected",
                 "zurueckgewiesen": "rejected", "abgelehnt": "rejected", "entscheiden": "decision",
                 "entscheidung": "decision", "done": "incorporated", "fixed": "incorporated"}
OPEN = ("open", "in_progress")
CLAIM_HOURS = 2
EXPIRES_DAYS = 30
NOTE_MIN = 5
TEXT_MAX = 4000
NAME_MAX = 80
GEMINI = "Gemini"
CHECK_KINDS = ITEM_KINDS + ("series", "document", "number")
ASPECTS = ("usage", "source")
SEV_ORDER = {"high": 0, "medium": 1, None: 2, "low": 3}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean(t, n: int = TEXT_MAX) -> str:
    t = " ".join(str(t or "").split()) if n <= NAME_MAX else str(t or "").strip()
    if len(t) > n:
        raise ValueError(f"text longer than {n} characters")
    return t


def fid(ref) -> int:
    """Feedback id from 118, '118' or 'F-118'."""
    m = re.fullmatch(r"\s*(?:F-?)?(\d+)\s*", str(ref), re.I)
    if not m:
        raise ValueError(f"'{ref}' is not a feedback id (F-118)")
    return int(m.group(1))


# ------------------------------------------------------------------ reviewers
def parse_name(name: str) -> tuple[str, str]:
    """('Keller', 'Anna') from 'Keller, Anna' (Name, Vorname) or 'Anna Keller'."""
    n = _clean(name, NAME_MAX)
    if not n:
        raise ValueError("a reviewer needs a name ('Name, Vorname')")
    if "," in n:
        last, first = (x.strip() for x in n.split(",", 1))
    else:
        parts = n.split(" ")
        last, first = parts[-1], " ".join(parts[:-1])
    if not last:
        raise ValueError(f"no last name in '{name}' – write 'Name, Vorname'")
    return last, first


def display(row) -> str:
    if row is None:
        return "Falk (Portal)"
    return f"{row['first_name']} {row['last_name']}".strip()


def _reviewer_dict(row, extra: dict | None = None) -> dict:
    d = {"id": row["id"], "kind": row["kind"], "name": display(row), "last_name": row["last_name"],
         "first_name": row["first_name"], "org": row["org"], "label": f"{row['last_name']}, {row['first_name']}".rstrip(", ")}
    return {**d, **(extra or {})}


def list_reviewers(store: Store, include_ai: bool = False) -> list[dict]:
    rows = store.db.execute(
        "SELECT r.*, (SELECT count(*) FROM review_access a WHERE a.reviewer_id = r.id) AS reviews, "
        "(SELECT count(*) FROM item_check c WHERE c.reviewer_id = r.id) AS checks FROM reviewer r "
        "WHERE r.deleted_at IS NULL" + ("" if include_ai else " AND r.kind = 'person'") +
        " ORDER BY r.kind DESC, lower(r.last_name), lower(r.first_name)").fetchall()
    return [_reviewer_dict(r, {"reviews": r["reviews"], "checks": r["checks"]}) for r in rows]


def reviewer(store: Store, ref, org: str | None = None, create: bool = True, kind: str = "person"):
    """Reviewer row for an id or a name ('Keller, Anna'); new names are created (create=True)."""
    if isinstance(ref, int) or re.fullmatch(r"\d+", str(ref).strip()):
        row = store.db.execute("SELECT * FROM reviewer WHERE id = ?", (int(ref),)).fetchone()
        if row is None:
            raise LookupError(f"reviewer {ref} not found")
        return row
    last, first = parse_name(str(ref))
    row = store.db.execute("SELECT * FROM reviewer WHERE kind = ? AND lower(last_name) = lower(?) AND lower(first_name) = lower(?)",
                           (kind, last, first)).fetchone()
    if row is not None:
        if row["deleted_at"]:
            store.db.execute("UPDATE reviewer SET deleted_at = NULL WHERE id = ?", (row["id"],))
        return store.db.execute("SELECT * FROM reviewer WHERE id = ?", (row["id"],)).fetchone()
    if not create:
        raise LookupError(f"no reviewer '{ref}'")
    cur = store.db.execute("INSERT INTO reviewer(kind, last_name, first_name, org) VALUES (?,?,?,?)",
                           (kind, last, first, _clean(org, NAME_MAX) or None))
    store.log("reviewer", cur.lastrowid, "insert", {"kind": kind})
    return store.db.execute("SELECT * FROM reviewer WHERE id = ?", (cur.lastrowid,)).fetchone()


def ai_reviewer(store: Store, name: str = GEMINI):
    row = store.db.execute("SELECT * FROM reviewer WHERE kind = 'ai' AND lower(last_name) = lower(?)", (name,)).fetchone()
    if row is not None:
        return row
    cur = store.db.execute("INSERT INTO reviewer(kind, last_name, first_name) VALUES ('ai', ?, '')", (name,))
    return store.db.execute("SELECT * FROM reviewer WHERE id = ?", (cur.lastrowid,)).fetchone()


def add_reviewer(store: Store, name: str, org: str | None = None, actor: str = "cli") -> dict:
    with store.batch(actor):
        row = reviewer(store, name, org=org)
        if org and row["org"] != org:
            store.db.execute("UPDATE reviewer SET org = ? WHERE id = ?", (_clean(org, NAME_MAX), row["id"]))
    return _reviewer_dict(store.db.execute("SELECT * FROM reviewer WHERE id = ?", (row["id"],)).fetchone())


def delete_reviewer(store: Store, ref, actor: str = "cli") -> dict:
    """Hide a person from the picker and revoke their links; their checks and feedback stay (anonymised display stays)."""
    with store.batch(actor):
        row = reviewer(store, ref, create=False)
        store.db.execute("UPDATE reviewer SET deleted_at = ? WHERE id = ?", (_now(), row["id"]))
        n = store.db.execute("UPDATE review_access SET revoked_at = ? WHERE reviewer_id = ? AND revoked_at IS NULL",
                             (_now(), row["id"])).rowcount
        store.log("reviewer", row["id"], "delete", {"revoked_links": n})
    return {"id": row["id"], "deleted": True, "revoked_links": n}


# ------------------------------------------------------------------ reviews
def link(token: str) -> str:
    return f"{REVIEW_URL}/r/{token}"


def _slides_for(store: Store, project: str, slides=None, set_ref=None, deck=None) -> tuple[str, int | None, int | None, list[int]]:
    """(scope, set_id, sequence_id, [exhibit ids in order]) – the current version of each slide is reviewed."""
    if slides:
        ids = []
        for s in slides:
            row = ex._resolve(store, s, include_internal=True, project=project)
            if row["project"] != project:
                raise ValueError(f"slide {s} belongs to project {row['project']}, not {project}")
            if row["id"] not in ids:
                ids.append(row["id"])
        sets = {r[0] for r in store.db.execute(f"SELECT DISTINCT set_id FROM exhibit WHERE id IN ({','.join('?' * len(ids))})", ids)}
        return "selection", (next(iter(sets)) if len(sets) == 1 else None), None, ids
    if deck not in (None, ""):
        from fundus import decks
        seq = decks.get_sequence(store, str(deck), project=project)
        ids = list(dict.fromkeys(s["id"] for s in seq["slides"]))
        return "deck", seq.get("set_id"), seq["id"], ids
    if set_ref not in (None, ""):
        srow = ex.resolve_set(store, set_ref, project)
        ids = [r[0] for r in store.db.execute(
            "SELECT id FROM exhibit WHERE set_id = ? ORDER BY coalesce(updated_at, created_at) DESC, id DESC", (srow["id"],))]
        return "set", srow["id"], None, ids
    raise ValueError("give slides (marked slides), set or deck")


def create(store: Store, project: str, slides=None, set_ref=None, deck=None, title: str | None = None, people=(),
           gemini: bool = False, focus=None, expires_days: int | None = EXPIRES_DAYS, model: str | None = None,
           actor: str = "cli") -> dict:
    """Freeze slides for review. people: names ('Keller, Anna') or reviewer ids – one link each (works once the review is
    published to the review app); gemini: queue a check by Gemini (no links, nothing public). At least one of both."""
    people = [p for p in (people or []) if str(p).strip()]
    if not people and not gemini:
        raise ValueError("choose who checks: people (names) and/or gemini=true")
    from fundus import gemini as gm
    model = gm.check_model(model) if gemini else None
    focus = [f for f in (focus or (list(FOCUS[:3]) if gemini else [])) if f]
    bad = [f for f in focus if f not in FOCUS]
    if bad:
        raise ValueError(f"unknown focus {bad}; allowed: {', '.join(FOCUS)}")
    project = ex._slug(project)
    scope, set_id, seq_id, ids = _slides_for(store, project, slides, set_ref, deck)
    if not ids:
        raise ValueError("no slides to review")
    if not title:
        if scope == "selection":
            st_title = store.db.execute("SELECT title FROM exhibit_set WHERE id = ?", (set_id,)).fetchone() if set_id else None
            title = (f"{len(ids)} Folie" if len(ids) == 1 else f"{len(ids)} Folien") + (f" aus {st_title[0]}" if st_title else "")
        elif seq_id:
            r = store.db.execute("SELECT coalesce(title, name) FROM exhibit_sequence WHERE id = ?", (seq_id,)).fetchone()
            title = r[0]
        elif set_id:
            title = store.db.execute("SELECT title FROM exhibit_set WHERE id = ?", (set_id,)).fetchone()[0]
        else:
            title = f"{len(ids)} Folien"
        title = f"{title} · {datetime.now().strftime('%d.%m.%Y')}"
    title = " ".join(_clean(title, 160).split())
    expires = ((datetime.now(timezone.utc) + timedelta(days=int(expires_days))).strftime("%Y-%m-%d %H:%M:%S")
               if people and expires_days else None)
    with store.batch(actor):
        cur = store.db.execute(
            "INSERT INTO review(project, title, scope, set_id, sequence_id, status, gemini_state, gemini_model, focus, expires_at, "
            "created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project, title, scope, set_id, seq_id, "draft" if people else "open", "queued" if gemini else None, model,
             json.dumps(focus) if focus else None, expires, actor))
        rid = cur.lastrowid
        for pos, eid in enumerate(ids, 1):
            v = store.db.execute("SELECT current_version FROM exhibit WHERE id = ?", (eid,)).fetchone()[0]
            store.db.execute("INSERT INTO review_slide(review_id, position, exhibit_id, version) VALUES (?,?,?,?)",
                             (rid, pos, eid, v or 1))
        for p in people:
            _grant(store, rid, reviewer(store, p))
        store.log("review", rid, "create", {"project": project, "scope": scope, "slides": len(ids), "people": len(people),
                                            "gemini": bool(gemini)})
    return get(store, rid)


def _grant(store: Store, rid: int, row) -> None:
    if row["kind"] != "person":
        raise ValueError("links are for people; AI checks run without a link")
    had = store.db.execute("SELECT revoked_at FROM review_access WHERE review_id = ? AND reviewer_id = ?", (rid, row["id"])).fetchone()
    if had is None:
        store.db.execute("INSERT INTO review_access(review_id, reviewer_id, token) VALUES (?,?,?)",
                         (rid, row["id"], secrets.token_urlsafe(24)))
    elif had["revoked_at"]:   # re-invited: a new link, the old one stays dead
        store.db.execute("UPDATE review_access SET token = ?, revoked_at = NULL, created_at = ? WHERE review_id = ? AND reviewer_id = ?",
                         (secrets.token_urlsafe(24), _now(), rid, row["id"]))


def _review_row(store: Store, ref):
    row = store.db.execute("SELECT * FROM review WHERE id = ?", (int(ref),)).fetchone() if str(ref).isdigit() else None
    if row is None:
        raise LookupError(f"review {ref} not found")
    return row


def _counts(store: Store, where: str, args) -> dict:
    c = {(r[0], r[1]): r[2] for r in store.db.execute(f"SELECT status, coalesce(weight, 'error'), count(*) FROM feedback WHERE {where} "
                                                      "GROUP BY 1, 2", args)}
    get = lambda st, w="error": c.get((st, w), 0)
    return {"open": get("open") + get("in_progress"), "suggestions": get("open", "suggestion") + get("in_progress", "suggestion"),
            "decision": get("decision") + get("decision", "suggestion"),
            "done": sum(get(s) + get(s, "suggestion") for s in ("incorporated", "answered", "rejected")), "total": sum(c.values())}


def _brief(store: Store, row) -> dict:
    people = store.db.execute("SELECT count(*) FROM review_access WHERE review_id = ? AND revoked_at IS NULL", (row["id"],)).fetchone()[0]
    n = store.db.execute("SELECT count(*) FROM review_slide WHERE review_id = ?", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "project": row["project"], "title": row["title"], "scope": row["scope"], "set_id": row["set_id"],
            "sequence_id": row["sequence_id"], "round": row["round"], "status": row["status"],
            "gemini": row["gemini_state"], "gemini_model": row["gemini_model"],
            "gemini_progress": _progress(row["gemini_report"]), "focus": json.loads(row["focus"]) if row["focus"] else [], "slides": n,
            "people": people, "expires_at": row["expires_at"], "created_at": row["created_at"],
            "published_at": row["published_at"], "completed_at": row["completed_at"], "closed_at": row["closed_at"],
            "feedback": _counts(store, "review_id = ?", (row["id"],)),
            "progress": [{k: p[k] for k in ("name", "done", "total", "percent", "state", "last")} for p in progress(store, row["id"])["people"]]}


def _progress(report: str | None) -> dict | None:
    if not report:
        return None
    r = json.loads(report)
    return {k: r.get(k) for k in ("done", "total", "tokens", "error", "started_at", "finished_at") if r.get(k) is not None}


def list_reviews(store: Store, project: str | None = None, status: str | None = None) -> list[dict]:
    f, a = [], []
    if project:
        f.append("project = ?"); a.append(ex._slug(project))
    if status:
        f.append("status = ?"); a.append(status)
    rows = store.db.execute("SELECT * FROM review" + (f" WHERE {' AND '.join(f)}" if f else "") + " ORDER BY id DESC", a).fetchall()
    return [_brief(store, r) for r in rows]


def get(store: Store, ref) -> dict:
    row = _review_row(store, ref)
    out = _brief(store, row)
    slides = []
    for s in store.db.execute(
            "SELECT rs.position, rs.version, e.id, e.slug, e.title, e.kind, e.current_version, e.visibility, e.project, "
            "v.png_sha FROM review_slide rs JOIN exhibit e ON e.id = rs.exhibit_id "
            "LEFT JOIN exhibit_version v ON v.exhibit_id = e.id AND v.n = rs.version WHERE rs.review_id = ? ORDER BY rs.position",
            (row["id"],)):
        internal = s["visibility"] == "internal"
        slides.append({"position": s["position"], "id": s["id"], "slug": s["slug"], "title": s["title"], "kind": s["kind"],
                       "version": s["version"], "current": s["current_version"],
                       "preview": ex.url(s["png_sha"], "preview.png", s["project"], internal),
                       "png": ex.url(s["png_sha"], "png", s["project"], internal),
                       "feedback": _counts(store, "review_id = ? AND exhibit_id = ?", (row["id"], s["id"]))})
    out["slides_list"] = slides
    out["reviewers"] = [
        _reviewer_dict(r, {"link": link(r["token"]) if not r["revoked_at"] else None, "invited_at": r["invited"],
                           "revoked_at": r["revoked_at"]})
        for r in store.db.execute("SELECT r.*, a.token, a.created_at AS invited, a.revoked_at FROM review_access a "
                                  "JOIN reviewer r ON r.id = a.reviewer_id WHERE a.review_id = ? ORDER BY lower(r.last_name)",
                                  (row["id"],))]
    out["published"] = row["published_at"] is not None
    prog = progress(store, row["id"])
    out["progress"], out["progress_slides"] = prog["people"], prog["slides"]
    for s in out["slides_list"]:
        s["reviewed_by"] = prog["slides"].get(s["position"], [])
    if row["gemini_report"]:
        rep = json.loads(row["gemini_report"])
        pos = {s["id"]: s["position"] for s in slides}
        out["gemini_report"] = {"deck": rep.get("deck"), "searches": rep.get("searches"), "slides": sorted(
            [{"exhibit": int(k), **v} for k, v in (rep.get("slides") or {}).items()], key=lambda x: x.get("position") or pos.get(int(x["exhibit"]), 0))}
    if out["reviewers"] and not out["published"]:
        out["note"] = "Links work once the review sync has published the review (within a minute; after a Gemini pre-check has finished)."
    return out


def invite(store: Store, ref, people, actor: str = "cli") -> dict:
    row = _review_row(store, ref)
    if row["status"] == "closed":
        raise ValueError(f"review {row['id']} is closed")
    with store.batch(actor):
        for p in people or []:
            _grant(store, row["id"], reviewer(store, p))
        store.log("review", row["id"], "invite", {"people": len(people or [])})
    return get(store, row["id"])


def revoke(store: Store, ref, person, actor: str = "cli") -> dict:
    row = _review_row(store, ref)
    with store.batch(actor):
        r = reviewer(store, person, create=False)
        n = store.db.execute("UPDATE review_access SET revoked_at = ? WHERE review_id = ? AND reviewer_id = ? AND revoked_at IS NULL",
                             (_now(), row["id"], r["id"])).rowcount
        if not n:
            raise LookupError(f"{display(r)} has no active link for review {row['id']}")
        store.log("review", row["id"], "revoke", {"reviewer": r["id"]})
    return get(store, row["id"])


def close(store: Store, ref, actor: str = "cli") -> dict:
    """Finish the review for everyone: links stay readable until they expire, nothing can be changed any more."""
    row = _review_row(store, ref)
    with store.batch(actor):
        store.db.execute("UPDATE review SET status = 'closed', closed_at = coalesce(closed_at, ?), completed_at = coalesce(completed_at, ?) "
                         "WHERE id = ?", (_now(), _now(), row["id"]))
        store.db.execute("UPDATE review_access SET finished_at = coalesce(finished_at, ?) WHERE review_id = ?", (_now(), row["id"]))
        store.log("review", row["id"], "close", None)
    return get(store, row["id"])


def purge(store: Store, ref, actor: str = "cli") -> dict:
    """Close and delete the review from the review app: all links invalid (the sync removes it from Firebase).
    The feedback stays in Fundus."""
    row = _review_row(store, ref)
    with store.batch(actor):
        store.db.execute("UPDATE review SET status = 'closed', closed_at = coalesce(closed_at, ?) WHERE id = ?", (_now(), row["id"]))
        store.db.execute("UPDATE review_access SET revoked_at = coalesce(revoked_at, ?) WHERE review_id = ?", (_now(), row["id"]))
        store.log("review", row["id"], "purge", None)
    return get(store, row["id"])


# ------------------------------------------------------------------ checks and verification stages
def _target(kind: str, tid) -> tuple[str, str]:
    if kind not in CHECK_KINDS:
        raise ValueError(f"check target must be one of {', '.join(CHECK_KINDS)}")
    return kind, str(tid)


def verification(store: Store, kind: str, tid, exhibit_id: int | None = None) -> dict:
    """Stage of a source, data point or slide number from independent checks (latest verdict per reviewer):
    unchecked | verified (1 reviewer ok) | confirmed (2+ ok) | disputed (a current 'wrong'); doubts are listed.
    With exhibit_id only the verdicts on THAT slide count ("supports this slide"); without, all slides. For documents the
    separate check "the source itself is genuine" (aspect source) counts everywhere: a 'wrong' there disputes every use."""
    kind, tid = _target(kind, tid)
    rows = store.db.execute(
        "SELECT c.*, r.kind AS rkind, r.first_name, r.last_name FROM item_check c JOIN reviewer r ON r.id = c.reviewer_id "
        "WHERE c.target_kind = ? AND c.target_id = ? ORDER BY c.id", (kind, tid)).fetchall()
    use, src = {}, {}
    for r in rows:
        if r["aspect"] == "source":
            src[r["reviewer_id"]] = r
        elif exhibit_id is None or r["exhibit_id"] in (None, exhibit_id):
            use[(r["reviewer_id"], r["exhibit_id"]) if exhibit_id is None else r["reviewer_id"]] = r
    pack = lambda latest: {v: [{"reviewer": display(r), "ai": r["rkind"] == "ai", "at": r["created_at"], "note": r["note"],
                                "evidence": r["evidence"], "exhibit": r["exhibit_id"]} for r in latest.values() if r["verdict"] == v]
                           for v in VERDICTS}
    by, gen = pack(use), pack(src)
    ok_people = {x["reviewer"] for x in by["ok"]}
    stage = ("disputed" if by["wrong"] or gen["wrong"] else "confirmed" if len(ok_people) >= 2 else "verified" if ok_people else "unchecked")
    out = {"stage": stage, "confirmations": len(ok_people), "ok": by["ok"], "doubt": by["doubt"], "wrong": by["wrong"]}
    if kind == "document":
        out["source"] = {"stage": "disputed" if gen["wrong"] else "verified" if gen["ok"] else "doubt" if gen["doubt"] else "unchecked",
                         "ok": gen["ok"], "doubt": gen["doubt"], "wrong": gen["wrong"]}
    return out


def record_check(store: Store, kind: str, tid, reviewer_row, verdict: str, note: str | None = None,
                 evidence: str | None = None, review_id: int | None = None, feedback_id: int | None = None,
                 exhibit_id: int | None = None, aspect: str = "usage") -> dict:
    """Store one independent check and carry the stage into the item's status (verified / disputed). aspect 'usage' (does it
    support this slide, exhibit_id) or 'source' (is the source itself genuine – documents only)."""
    kind, tid = _target(kind, tid)
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if aspect not in ASPECTS or (aspect == "source" and kind != "document"):
        raise ValueError("aspect must be usage, or source for documents")
    store.db.execute("INSERT INTO item_check(target_kind, target_id, reviewer_id, verdict, note, evidence, review_id, feedback_id, "
                     "exhibit_id, aspect) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (kind, tid, reviewer_row["id"], verdict, note, evidence, review_id, feedback_id, exhibit_id, aspect))
    v = verification(store, kind, tid, exhibit_id)
    if kind in ITEM_KINDS:
        row = store.db.execute(f"SELECT status FROM {kind} WHERE id = ?", (int(tid),)).fetchone()
        if row is not None and row["status"] != "retracted":
            want = {"disputed": "disputed", "verified": "verified", "confirmed": "verified"}.get(v["stage"])
            if want and want != row["status"]:
                store.update(kind, int(tid), {"status": want})
                store.log(kind, tid, "verify" if want == "verified" else "dispute",
                          {"note": f"Review: {verdict} von {display(reviewer_row)}", "stage": v["stage"]})
    return v


# ------------------------------------------------------------------ feedback
def add(store: Store, exhibit, text: str, source: str = "owner", reviewer_ref=None, review_id: int | None = None,
        kind: str = "comment", version: int | None = None, target: tuple[str, str] | None = None, verdict: str | None = None,
        severity: str | None = None, note: str | None = None, shape=None, evidence: str | None = None,
        external_id: str | None = None, actor: str = "cli", aspect: str = "usage", weight: str | None = None) -> dict:
    """One feedback point on a slide. A check (kind 'check', target + verdict) is also recorded as an independent check
    when it comes from a person or an AI; an 'ok' check is only recorded, it opens no point. external_id makes repeated
    syncs idempotent."""
    if source not in ("person", "ai", "owner"):
        raise ValueError("source must be person, ai or owner")
    if severity is not None and severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if weight is not None and weight not in WEIGHTS:
        raise ValueError(f"weight must be one of {', '.join(WEIGHTS)}")
    text = _clean(text)
    if kind == "check":
        if not target or verdict not in VERDICTS:
            raise ValueError("a check needs target (kind, id) and verdict ok | doubt | wrong")
        target = _target(*target)
    elif not text:
        raise ValueError("feedback needs a text")
    if external_id:
        had = store.db.execute("SELECT id FROM feedback WHERE external_id = ?", (external_id,)).fetchone()
        if had:
            return {"id": had[0], "ref": f"F-{had[0]}", "duplicate": True}
    row = ex._resolve(store, exhibit, include_internal=True)
    rrow = None
    if source == "ai":
        rrow = reviewer(store, reviewer_ref, kind="ai") if reviewer_ref else ai_reviewer(store)
    elif source == "person":
        if reviewer_ref is None:
            raise ValueError("feedback from a person needs the reviewer")
        rrow = reviewer(store, reviewer_ref, create=False)
    v = int(version or row["current_version"] or 1)
    with store.batch(actor):
        fid_ = None
        if not (kind == "check" and verdict == "ok"):
            cur = store.db.execute(
                "INSERT INTO feedback(review_id, exhibit_id, version, source, reviewer_id, kind, target_kind, target_id, verdict, "
                "severity, weight, text, note, shape, external_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, row["id"], v, source, rrow["id"] if rrow else None, kind, target[0] if target else None,
                 target[1] if target else None, verdict, severity, weight, text or _check_text(store, target, verdict), _clean(note) or None,
                 json.dumps(shape) if shape is not None else None, external_id))
            fid_ = cur.lastrowid
            store.log("exhibit", row["id"], "feedback", {"feedback": fid_, "source": source})
        stage = None
        if kind == "check" and rrow is not None:
            stage = record_check(store, target[0], target[1], rrow, verdict, note=_clean(note) or None, evidence=evidence,
                                 review_id=review_id, feedback_id=fid_, exhibit_id=row["id"], aspect=aspect)
    out = {"id": fid_, "ref": f"F-{fid_}" if fid_ else None, "exhibit": row["id"], "version": v}
    if stage:
        out["verification"] = stage
    return out


def _check_text(store: Store, target, verdict) -> str:
    word = {"doubt": "fraglich", "wrong": "falsch", "ok": "korrekt"}.get(verdict, verdict)
    return f"{_target_label(store, *target) if target else 'Quelle'}: {word}"


def _target_label(store: Store, kind: str, tid: str) -> str:
    if kind == "document":
        r = store.db.execute("SELECT title FROM document WHERE id = ?", (int(tid),)).fetchone()
        return f"Quelle „{r[0]}“" if r else f"Quelle {tid}"
    if kind == "series":
        return f"Reihe {tid}"
    if kind == "number":
        r = store.db.execute("SELECT number FROM exhibit_number WHERE id = ?", (int(tid),)).fetchone()
        return f"Zahl {r[0]}" if r else f"Zahl {tid}"
    return f"{kind} {tid}"


def _target_info(store: Store, kind: str | None, tid: str | None) -> dict | None:
    if not kind:
        return None
    from fundus import query as q
    if kind == "document":
        r = store.db.execute("SELECT d.id, d.title, d.url, d.published, p.name AS publisher FROM document d "
                             "LEFT JOIN publisher p ON p.id = d.publisher_id WHERE d.id = ?", (int(tid),)).fetchone()
        return {"kind": "document", "id": int(tid), **({k: r[k] for k in ("title", "url", "published", "publisher")} if r else {})}
    if kind == "number":
        r = store.db.execute("SELECT id, number, claim FROM exhibit_number WHERE id = ?", (int(tid),)).fetchone()
        return {"kind": "number", "id": int(tid), **({"label": f"{r['number']} – {r['claim'] or ''}".strip(" –")} if r else {})}
    if kind == "series":
        r = store.db.execute("SELECT id, name, unit FROM series WHERE id = ?", (tid,)).fetchone()
        return {"kind": "series", "id": tid, **({"name": r["name"], "unit": r["unit"]} if r else {})}
    r = store.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (int(tid),)).fetchone()
    return q._label(store, kind, r) if r else {"kind": kind, "id": tid}


def _fb_dict(store: Store, r, names: dict) -> dict:
    d = {"id": r["id"], "ref": f"F-{r['id']}", "review": r["review_id"], "exhibit": r["exhibit_id"], "version": r["version"],
         "source": r["source"], "author": names.get(r["reviewer_id"], "Falk (Portal)") if r["reviewer_id"] else "Falk (Portal)",
         "kind": r["kind"], "severity": r["severity"], "weight": r["weight"] or "error", "verdict": r["verdict"], "text": r["text"], "note": r["note"],
         "status": r["status"], "created_at": r["created_at"]}
    if r["target_kind"]:
        d["target"] = _target_info(store, r["target_kind"], r["target_id"])
    if r["shape"]:
        d["shape"] = json.loads(r["shape"])
    if r["instruction"]:
        d["instruction"] = r["instruction"]
    if r["status"] == "in_progress":
        d.update(claimed_by=r["claimed_by"], claimed_at=r["claimed_at"])
    if r["status"] in OUTCOMES:
        d.update(resolution=r["resolution"], resolved_version=r["resolved_version"], resolved_by=r["resolved_by"],
                 resolved_at=r["resolved_at"])
    return {k: v for k, v in d.items() if v is not None}


def _names(store: Store) -> dict:
    return {r["id"]: display(r) for r in store.db.execute("SELECT * FROM reviewer")}


def _stale(claimed_at: str | None) -> bool:
    if not claimed_at:
        return True
    t = datetime.strptime(claimed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - t > timedelta(hours=CLAIM_HOURS)


def _rows(store: Store, project=None, set_ref=None, review=None, exhibit=None, status="open", limit: int = 300):
    f, a = ["1=1"], []
    if exhibit not in (None, ""):
        f.append("f.exhibit_id = ?"); a.append(ex._resolve(store, exhibit, include_internal=True)["id"])
    if review not in (None, ""):
        f.append("f.review_id = ?"); a.append(int(review))
    if project:
        f.append("e.project = ?"); a.append(ex._slug(project))
    if set_ref not in (None, ""):
        f.append("e.set_id = ?"); a.append(ex.resolve_set(store, set_ref, ex._slug(project) if project else None)["id"])
    states = {"open": OPEN, "decision": ("decision",), "done": ("incorporated", "answered", "rejected"),
              "all": ("open", "in_progress") + OUTCOMES}
    if status not in states:
        raise ValueError(f"status must be one of {', '.join(states)}")
    f.append(f"f.status IN ({','.join('?' * len(states[status]))})"); a += list(states[status])
    return store.db.execute(f"SELECT f.* FROM feedback f JOIN exhibit e ON e.id = f.exhibit_id WHERE {' AND '.join(f)} "
                            "ORDER BY f.exhibit_id, f.id LIMIT ?", a + [int(limit)]).fetchall()


def _slide_head(store: Store, eid: int, position: int | None = None) -> dict:
    e = store.db.execute(ex._ROW + " WHERE e.id = ?", (eid,)).fetchone()
    v = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, e["current_version"])).fetchone()
    internal = e["visibility"] == "internal"
    out = {"exhibit": eid, "ref": f"{e['project']}/{e['set_slug']}/{e['slug']}", "title": e["title"], "kind": e["kind"],
           "current_version": e["current_version"]}
    if v is not None:
        out.update({k: x for k, x in ex.version_urls(v, e["project"], internal).items() if k in ("png", "preview", "html")})
        out["png_path"] = v["png_path"]
        out["script_path"] = v["script_path"]
    if position:
        out["position"] = position
    return out


def inbox(store: Store, project=None, set_ref=None, review=None, exhibit=None, status: str = "open",
          claim: bool = False, by: str | None = None, limit: int = 300) -> dict:
    """Feedback grouped per slide for a session to work through: slide (ref, current version, PNG/HTML urls, PNG and script
    path on the Spark), its points sorted by weight, conflicts between checks of the same target marked, equal checks of
    several reviewers bundled ('votes'). claim=True marks the returned open points in progress for `by` (others see that;
    the claim expires after CLAIM_HOURS)."""
    rows = _rows(store, project, set_ref, review, exhibit, status, limit)
    names = _names(store)
    by = by or "claude-session"
    if claim:
        now = _now()
        mine = [r["id"] for r in rows if (r["weight"] or "error") == "error" and (r["status"] == "open" or (r["status"] == "in_progress" and
                                                                    (r["claimed_by"] == by or _stale(r["claimed_at"]))))]
        if mine:
            with store.batch("mcp"):
                store.db.execute(f"UPDATE feedback SET status = 'in_progress', claimed_by = ?, claimed_at = ? "
                                 f"WHERE id IN ({','.join('?' * len(mine))})", [by, now, *mine])
            rows = _rows(store, project, set_ref, review, exhibit, status, limit)
    slides: dict[int, dict] = {}
    pos = {}
    if review not in (None, ""):
        pos = dict(store.db.execute("SELECT exhibit_id, position FROM review_slide WHERE review_id = ?", (int(review),)).fetchall())
    for r in rows:
        s = slides.get(r["exhibit_id"])
        if s is None:
            s = slides[r["exhibit_id"]] = {**_slide_head(store, r["exhibit_id"], pos.get(r["exhibit_id"])), "reviewed_versions": [],
                                           "items": []}
        if r["version"] not in s["reviewed_versions"]:
            s["reviewed_versions"].append(r["version"])
        d = _fb_dict(store, r, names)
        if r["status"] == "in_progress" and r["claimed_by"] != by and not _stale(r["claimed_at"]):
            d["locked"] = f"in Arbeit von {r['claimed_by']} seit {r['claimed_at']}"
        s["items"].append(d)
    for s in slides.values():
        checks: dict[tuple, list] = {}
        for d in s["items"]:
            if d["kind"] == "check" and d.get("target"):
                checks.setdefault((d["target"]["kind"], str(d["target"]["id"])), []).append(d)
        for (k, t), ds in checks.items():
            v = verification(store, k, t)
            if v["ok"] and any(d["verdict"] in ("doubt", "wrong") for d in ds):
                for d in ds:
                    d["conflict"] = f"{len(v['ok'])} Prüfer halten die Quelle für korrekt: " + ", ".join(x["reviewer"] for x in v["ok"])
            same = {}
            for d in ds:
                same.setdefault(d["verdict"], []).append(d)
            for group in same.values():
                if len(group) > 1:
                    for d in group:
                        d["votes"] = [g["author"] for g in group]
        s["items"].sort(key=lambda d: (d["status"] not in OPEN, d.get("weight") == "suggestion", SEV_ORDER.get(d.get("severity"), 2) if d["kind"] != "check"
                                       else (0 if d.get("verdict") == "wrong" else 1), d["id"]))
        s["open"] = sum(1 for d in s["items"] if d["status"] in OPEN and d.get("weight") != "suggestion")
        s["suggestions"] = sum(1 for d in s["items"] if d["status"] in OPEN and d.get("weight") == "suggestion")
    out = sorted(slides.values(), key=lambda s: (s.get("position") or 10**6, -s["open"], s["exhibit"]))
    total = sum(len(s["items"]) for s in out)
    res = {"status": status, "slides": out, "items": total}
    if status == "open" and total:
        res["how"] = ("Points have a weight: 'error' (wrong or unsourced number, wrong or missing source, factual error, broken "
                      "calculation, contradiction) must be processed – it holds the slide's stage. 'suggestion' (wording, layout, "
                      "structure, nice-to-have) is optional: take it up if it improves the slide, otherwise leave it or answer it. "
                      "Work through every error: change the slide script and render (render.sh or `fundus render` registers the new version), then "
                      "fundus_feedback_resolve(ids, 'incorporated', note=what changed). Points you do not follow: 'answered' or "
                      "'rejected' with the reason. Contradicting sources or questions for Falk: 'decision'. Better links in "
                      "notes: check the source, add it with fundus_add and say so in the note. 'Quelle fehlt': add the source to "
                      "the slide's source line, register it with fundus_add (document with url + publisher) and name it in "
                      "mck.page(..., fundus=[…]). 'Zahl nicht belegbar' / 'Zahl falsch': find the source and correct the number, "
                      "or remove it – never keep a number without a source. Never mark items verified yourself.")
    return res


def resolve(store: Store, ids, outcome: str, note: str, version: int | None = None, by: str | None = None,
            actor: str = "mcp") -> dict:
    """Close feedback points with a comment. incorporated: the slide has a newer version than the reviewed one (default:
    its current version; `version` to name it); answered / rejected: the reason; decision: the question for Falk."""
    outcome = OUTCOME_WORDS.get(str(outcome).strip().lower(), str(outcome).strip().lower())
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)} (eingearbeitet, beantwortet, zurückgewiesen, entscheiden)")
    note = _clean(note)
    if len(note) < NOTE_MIN:
        raise ValueError("note is required: what changed (incorporated), why not (answered/rejected) or what Falk must decide")
    ids = [fid(i) for i in ([ids] if isinstance(ids, (str, int)) else ids or [])]
    if not ids:
        raise ValueError("give the feedback ids (F-118)")
    rows = store.db.execute(f"SELECT f.*, e.current_version FROM feedback f JOIN exhibit e ON e.id = f.exhibit_id "
                            f"WHERE f.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    missing = set(ids) - {r["id"] for r in rows}
    if missing:
        raise LookupError(f"feedback {', '.join(f'F-{i}' for i in sorted(missing))} not found")
    plan = []
    for r in rows:
        rv = None
        if outcome == "incorporated":
            rv = int(version) if version is not None else r["current_version"]
            if not store.db.execute("SELECT 1 FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (r["exhibit_id"], rv)).fetchone():
                raise ValueError(f"F-{r['id']}: slide {r['exhibit_id']} has no version {rv}")
            if rv <= r["version"]:
                raise ValueError(f"F-{r['id']}: no newer version than the reviewed v{r['version']} – render the changed slide "
                                 f"first (render.sh or `fundus render` registers it), or use 'answered' if nothing had to change")
        plan.append((r, rv))
    now = _now()
    with store.batch(actor):
        for r, rv in plan:
            store.db.execute("UPDATE feedback SET status = ?, resolution = ?, resolved_version = ?, resolved_by = ?, resolved_at = ?, "
                             "claimed_by = NULL, claimed_at = NULL WHERE id = ?", (outcome, note, rv, by or "claude-session", now, r["id"]))
            store.log("exhibit", r["exhibit_id"], "feedback_" + outcome, {"feedback": r["id"], "version": rv, "note": note})
    pin_verified(store, ids=sorted({r["exhibit_id"] for r, _ in plan}))
    return {"resolved": [f"F-{r['id']}" for r, _ in plan], "outcome": outcome,
            "versions": sorted({rv for _, rv in plan if rv is not None})}


def reopen(store: Store, ref, instruction: str | None = None, actor: str = "portal") -> dict:
    """Falk reopens a point (e.g. after deciding): back to open, with his instruction for the next session."""
    i = fid(ref)
    row = store.db.execute("SELECT * FROM feedback WHERE id = ?", (i,)).fetchone()
    if row is None:
        raise LookupError(f"feedback F-{i} not found")
    instr = _clean(instruction) or None
    with store.batch(actor):
        store.db.execute("UPDATE feedback SET status = 'open', claimed_by = NULL, claimed_at = NULL, "
                         "instruction = coalesce(?, instruction) WHERE id = ?", (instr, i))
        store.log("exhibit", row["exhibit_id"], "feedback_reopen", {"feedback": i, "instruction": instr})
    return _fb_dict(store, store.db.execute("SELECT * FROM feedback WHERE id = ?", (i,)).fetchone(), _names(store))


def changelog(store: Store, exhibit) -> list[dict]:
    """What happened to the slide's feedback, newest first: per resolved point the outcome, comment, new version and author."""
    eid = ex._resolve(store, exhibit, include_internal=True)["id"]
    names = _names(store)
    rows = store.db.execute("SELECT * FROM feedback WHERE exhibit_id = ? AND status IN ('incorporated','answered','rejected') "
                            "AND coalesce(resolved_by, '') != 'gemini-superseded' ORDER BY resolved_at DESC, id DESC", (eid,)).fetchall()
    return [{"ref": f"F-{r['id']}", "outcome": r["status"], "note": r["resolution"], "from_version": r["version"],
             "to_version": r["resolved_version"], "author": names.get(r["reviewer_id"], "Falk (Portal)") if r["reviewer_id"] else "Falk (Portal)",
             "feedback": r["text"], "at": r["resolved_at"], "by": r["resolved_by"]} for r in rows]


# ------------------------------------------------------------------ sources and data points of a slide
_PAGE = re.compile(r"(?:\[?Seite|\bS\.|\bp\.|\bpp?\.|\bpage)\s*(\d{1,4})", re.I)


def deep_link(url: str | None, quote: str | None = None, page_hint: str | None = None) -> str | None:
    """Link to the place in the source: a PDF page (#page=N from 'S. 14', 'p. 14', '[Seite 14]') or, for web pages, a text
    fragment (#:~:text=…, highlighted by Chrome, Edge and Safari) from the quoted sentence."""
    if not url:
        return None
    base = url.split("#")[0]
    is_pdf = base.lower().split("?")[0].endswith(".pdf")
    for hint in (page_hint, quote):
        m = _PAGE.search(hint or "")
        if m and is_pdf:
            return f"{base}#page={m.group(1)}"
    if quote and not is_pdf:
        q = re.sub(r"\[[^\]]*\]|[\"“”„«»]|\.\.\.|…", " ", quote)
        q = re.split(r"(?<=[!?])\s|(?<=[a-z]{3}\.)\s", " ".join(q.split()))[0]   # first sentence, not at 'Mrd.' / 'S.'
        words = q.split()[:9]
        if words:
            words[-1] = words[-1].rstrip(",;:.")
        if len(words) >= 3:
            from urllib.parse import quote as qe
            return f"{base}#:~:text={qe(' '.join(words), safe='')}"
    return None


def _best_evidence(store: Store, kind: str, tid) -> dict | None:
    """The latest quote a reviewer gave for a source or data point (people before AI)."""
    r = store.db.execute("SELECT c.evidence, c.note, r.kind AS rk, r.first_name, r.last_name FROM item_check c JOIN reviewer r ON r.id = c.reviewer_id "
                         "WHERE c.target_kind = ? AND c.target_id = ? AND c.evidence IS NOT NULL AND c.evidence != '' "
                         "ORDER BY r.kind = 'person' DESC, c.id DESC LIMIT 1", (kind, str(tid))).fetchone()
    return {"quote": r["evidence"], "by": display(r)} if r else None


def slide_evidence(store: Store, exhibit) -> dict:
    """Sources (documents named on the slide) and data points (Fundus items it shows) with their verification stage and
    link check – what reviewers check, and what the portal shows next to the slide."""
    from fundus import query as q
    e = ex._resolve(store, exhibit, include_internal=True)
    links = store.db.execute("SELECT target_kind, target_id, role, source FROM exhibit_link WHERE exhibit_id = ? AND role IN ('source','data') "
                             "ORDER BY role DESC, target_kind, target_id", (e["id"],)).fetchall()
    sources, data, seen_docs, datasets_used = [], [], set(), []
    lc = {r["url"]: r for r in store.db.execute("SELECT * FROM link_check")}
    for l in links:
        if l["target_kind"] == "document":
            if l["target_id"] in seen_docs:
                continue
            seen_docs.add(l["target_id"])
            info = _target_info(store, "document", l["target_id"])
            chk = lc.get(info.get("url"))
            evid = _best_evidence(store, "document", l["target_id"])
            sources.append({**info, "manual": l["source"] == "manual", "verification": verification(store, "document", l["target_id"], e["id"]),
                            "link": {"ok": bool(chk["ok"]), "status": chk["status"], "checked_at": chk["checked_at"],
                                     "error": chk["error"]} if chk else None,
                            "evidence": evid, "deep_url": deep_link(info.get("url"), (evid or {}).get("quote"))})
        elif l["target_kind"] == "dataset":
            r = store.db.execute("SELECT * FROM dataset WHERE id = ?", (int(l["target_id"]),)).fetchone()
            if r is not None:
                from fundus import datasets as _ds
                datasets_used.append({k: v for k, v in {"ref": f"dataset:{r['id']}", "slug": r["slug"], "title": r["title"],
                                                       "publisher": r["publisher"], "period": r["period"], "version": r["current_version"],
                                                       "update_due": _ds.due_reason(r), "manual": l["source"] == "manual"}.items()
                                      if v not in (None, "")})
        elif l["target_kind"] in ITEM_KINDS or l["target_kind"] == "series":
            info = _target_info(store, l["target_kind"], l["target_id"])
            if l["target_kind"] in ITEM_KINDS:
                r = store.db.execute(f"SELECT * FROM {l['target_kind']} WHERE id = ?", (int(l["target_id"]),)).fetchone()
                if r is not None:
                    info["source"] = q.doc_brief(store, r["document_id"])
                    info["status"] = r["status"]
                    cols = r.keys()
                    loc = r["locator"] if "locator" in cols else None
                    quote = r["quote_context"] if "quote_context" in cols else None
                    evid = _best_evidence(store, l["target_kind"], l["target_id"])
                    info.update({k: v for k, v in {"locator": loc, "quote": quote or (evid or {}).get("quote")}.items() if v})
                    info["deep_url"] = deep_link((info["source"] or {}).get("url"), quote or (evid or {}).get("quote"), loc)
            data.append({**info, "manual": l["source"] == "manual",
                         "verification": verification(store, l["target_kind"], l["target_id"], e["id"])})
    return {"exhibit": e["id"], "sources": sources, "data": data, **({"datasets": datasets_used} if datasets_used else {}),
            "sources_text": e["sources"], "numbers": slide_numbers(store, e["id"])}


def slide_numbers(store: Store, eid: int) -> list[dict]:
    """The numbers of the slide from its latest independent number check (version, verdict, quote, link to the place)."""
    last = store.db.execute("SELECT review_id, version FROM exhibit_number WHERE exhibit_id = ? ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    if not last:
        return []
    names = _names(store)
    rows = store.db.execute("SELECT * FROM exhibit_number WHERE exhibit_id = ? AND review_id IS ? AND version = ? ORDER BY id",
                            (eid, last["review_id"], last["version"])).fetchall()
    order = {"wrong": 0, "unverified": 1, "derived": 2, "sourced": 3}
    out = [{"id": r["id"], "number": r["number"], "claim": r["claim"], "verdict": r["verdict"], "note": r["note"], "evidence": r["evidence"],
            "people": verification(store, "number", r["id"], eid),
            "url": r["url"], "deep_url": deep_link(r["url"], r["evidence"]) or r["url"], "version": r["version"],
            "by": names.get(r["reviewer_id"]), "at": r["created_at"], "review": r["review_id"]} for r in rows]
    return sorted(out, key=lambda x: order.get(x["verdict"], 9))


def link_items(store: Store, exhibit, items, remove: bool = False, actor: str = "cli") -> dict:
    """Manual links slide -> Fundus items ('observation:123', 'series:bfs-x', 'document:45'): documents as sources, items
    and series as data shown on the slide. Manual links survive re-renders (rule links are recomputed)."""
    from fundus import locks
    e = ex._resolve(store, exhibit, include_internal=True)
    locks.guard_exhibit(store, e["id"], "changing its linked sources and data")
    done = []
    with store.batch(actor):
        for it in [items] if isinstance(items, str) else items or []:
            m = re.fullmatch(r"\s*([a-z]+)\s*[:#/ ]\s*([\w.-]+)\s*", str(it))
            if not m:
                raise ValueError(f"'{it}' – write kind:id, e.g. observation:123, series:bfs-genai-use-ch, document:45")
            kind = m.group(1).lower()
            if kind == "dataset":           # a whole dataset the slide's analysis rests on (fundus.datasets)
                tid = m.group(2).removeprefix("dataset:")
            else:
                kind, tid = _target(kind, m.group(2))
            key = tid if kind == "series" else int(tid)
            if not store.db.execute(f"SELECT 1 FROM {kind} WHERE id = ?", (key,)).fetchone():
                raise LookupError(f"{kind} {tid} not found")
            role = "source" if kind == "document" else "data"
            if remove:
                store.db.execute("DELETE FROM exhibit_link WHERE exhibit_id = ? AND target_kind = ? AND target_id = ? AND role = ?",
                                 (e["id"], kind, tid, role))
            else:
                store.db.execute("INSERT INTO exhibit_link(exhibit_id, target_kind, target_id, role, source, evidence) "
                                 "VALUES (?,?,?,?,'manual','linked by hand') ON CONFLICT(exhibit_id, target_kind, target_id, role) "
                                 "DO UPDATE SET source = 'manual'", (e["id"], kind, tid, role))
            done.append(f"{kind}:{tid}")
        store.log("exhibit", e["id"], "unlink" if remove else "link", {"items": done})
    return {"exhibit": e["id"], "removed" if remove else "linked": done, **slide_evidence(store, e["id"])}


# ------------------------------------------------------------------ link check
UA = "Mozilla/5.0 (X11; Linux x86_64) FundusLinkCheck/1.0"


def check_url(u: str, timeout: float = 12) -> dict:
    last = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(u, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return {"ok": 1, "status": r.status, "final_url": r.geturl(), "error": None}
        except urllib.error.HTTPError as e:
            last = {"ok": 0, "status": e.code, "final_url": None, "error": str(e.reason)}
            if e.code in (403, 405, 429, 501) and method == "HEAD":
                continue
            if e.code in (401, 403, 429):   # paywall / bot wall: the page exists
                last["ok"] = 1
            return last
        except Exception as e:   # DNS, TLS, timeout
            last = {"ok": 0, "status": None, "final_url": None, "error": str(e)[:200]}
    return last


def review_urls(store: Store, rid: int) -> list[str]:
    return [r[0] for r in store.db.execute(
        "SELECT DISTINCT d.url FROM review_slide rs JOIN exhibit_link l ON l.exhibit_id = rs.exhibit_id AND l.target_kind = 'document' "
        "JOIN document d ON d.id = CAST(l.target_id AS INTEGER) WHERE rs.review_id = ? AND d.url IS NOT NULL AND d.url != ''", (rid,))]


def check_links(store: Store, urls, max_age_days: int = 7) -> dict:
    fresh = {r[0] for r in store.db.execute("SELECT url FROM link_check WHERE checked_at > datetime('now', ?)", (f"-{max_age_days} days",))}
    todo = [u for u in dict.fromkeys(urls) if u and u not in fresh]
    bad = 0
    for u in todo:
        r = check_url(u if "://" in u else "https://" + u)
        bad += not r["ok"]
        store.db.execute("INSERT INTO link_check(url, ok, status, final_url, error, checked_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
                         "ON CONFLICT(url) DO UPDATE SET ok = excluded.ok, status = excluded.status, final_url = excluded.final_url, "
                         "error = excluded.error, checked_at = excluded.checked_at", (u, r["ok"], r["status"], r["final_url"], r["error"]))
        store.db.commit()
    return {"checked": len(todo), "broken": bad, "skipped_fresh": len(set(urls)) - len(todo)}


def check_review_links(db_path, rid: int) -> dict:
    """Own connection: runs in a background thread after a review is created."""
    st = Store(db_path)
    try:
        return check_links(st, review_urls(st, rid))
    finally:
        st.close()


# ------------------------------------------------------------------ portal summaries
def open_counts(store: Store, project: str | None = None) -> dict:
    """Open points per slide (for tiles) and per project/set (for the sidebar)."""
    f, a = "", []
    if project:
        f, a = " AND e.project = ?", [ex._slug(project)]
    rows = store.db.execute("SELECT f.exhibit_id, e.project, e.set_id, "
                            "sum(f.status IN ('open','in_progress') AND coalesce(f.weight, 'error') = 'error') AS open, "
                            "sum(f.status IN ('open','in_progress') AND f.weight = 'suggestion') AS suggestions, "
                            "sum(f.status = 'decision') AS decision FROM feedback f JOIN exhibit e ON e.id = f.exhibit_id "
                            f"WHERE f.status IN ('open','in_progress','decision'){f} GROUP BY f.exhibit_id", a).fetchall()
    return {"exhibits": {r["exhibit_id"]: {"open": r["open"], "decision": r["decision"], "suggestions": r["suggestions"]} for r in rows},
            "decisions": sum(r["decision"] for r in rows), "open": sum(r["open"] for r in rows)}


# ------------------------------------------------------------------ verification stage of a slide
LEVELS = {"not_needed": 2, "no_sources": 0, "unchecked": 0, "changed": 1, "open": 1, "incorporated": 2, "checked": 2, "confirmed": 3}
STAGE_LABEL = {"not_needed": "keine Quellen nötig", "no_sources": "Quellen fehlen", "unchecked": "ungeprüft", "changed": "seit Prüfung geändert", "open": "offene Punkte",
               "incorporated": "eingearbeitet", "checked": "geprüft", "confirmed": "bestätigt"}
_DIGITS = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?")


def _has_numbers(extract: dict) -> bool:
    text = " ".join(str(extract.get(k) or "") for k in ("title", "so_what", "body", "meta"))
    text += " " + " ".join(extract.get("labels") or []) + " " + " ".join(extract.get("chart_heads") or [])
    text = re.sub(r"\b\d{1,2}[:.]\d{2}\s*(?:–|-|bis)?\s*(?:\d{1,2}[:.]\d{2})?\s*(?:Uhr|h|am|pm)?\b", " ", text)   # times of an agenda
    hits = 0
    for m in re.finditer(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(%|×|x\b|pp\b|Mrd|Mio|bn\b|CHF|USD|EUR|€|\$)?", text):
        num, unit = m.group(1), m.group(2)
        if re.fullmatch(r"(19|20)\d{2}", num) and not unit:
            continue                                  # a year
        if unit or re.search(r"[.,]", num) or (len(num) >= 2 and not num.startswith("0")):
            hits += 1                                 # a value – not a list number like 1, 2 or 01
    return hits >= 2


def _twins(store: Store, ids: list[int]) -> dict[int, list[tuple]]:
    """Identical copies of a slide in the project (another slide set, or a deliberate copy in the same set): an unmodified
    copy (same HTML, or same PNG for renders without HTML) shares the verification stage with its original – checks,
    feedback, confirmations on that content count on both.
    Once one of them is changed, its new version stands on its own (seit Prüfung geändert), and the other keeps what was
    gathered on the shared content; what is recorded on one after the other was changed does not cross over. Per slide:
    [(other exhibit, its identical version, when the other moved on past it, own identical version, when this slide moved
    on past it)] – the latest own version that has a copy; None: not moved on."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    best: dict[tuple, tuple] = {}
    for r in store.db.execute(
            f"SELECT e.id, v.n own, o.exhibit_id other, o.n FROM exhibit e JOIN exhibit_version v ON v.exhibit_id = e.id AND v.n <= e.current_version "
            f"JOIN exhibit_version o ON o.exhibit_id != e.id AND (o.png_sha = v.png_sha OR (v.html_sha IS NOT NULL AND o.html_sha = v.html_sha)) "
            f"JOIN exhibit oe ON oe.id = o.exhibit_id AND oe.project = e.project AND oe.kind = e.kind "
            f"WHERE e.id IN ({ph}) AND e.kind != 'section'", ids):
        k = (r["id"], r["other"])
        best[k] = max(best.get(k, (0, 0)), (r["own"], r["n"]))
    moved = lambda e, n: store.db.execute("SELECT min(registered_at) FROM exhibit_version WHERE exhibit_id = ? AND n > ?", (e, n)).fetchone()[0]
    out: dict[int, list[tuple]] = {}
    for (eid, other), (own, n) in best.items():
        out.setdefault(eid, []).append((other, n, moved(other, n), own, moved(eid, own)))
    return out


def _twin_brief(store: Store, eid: int, version: int) -> dict:
    r = store.db.execute("SELECT e.id, e.slug, e.current_version, s.slug set_slug, s.title set_title FROM exhibit e "
                         "JOIN exhibit_set s ON s.id = e.set_id WHERE e.id = ?", (eid,)).fetchone()
    return {"exhibit": r["id"], "slug": r["slug"], "set": r["set_slug"], "set_title": r["set_title"], "version": version,
            "is_current": version == r["current_version"]}


def slide_status(store: Store, project: str | None = None, ids: list[int] | None = None) -> dict[int, dict]:
    """Verification stage per slide (current version) – what the portal shows on tiles and next to the slide:
    no_sources (numbers without any source) · unchecked · open (feedback open or waiting for Falk) · changed (rendered anew
    after a check that found nothing to incorporate) · incorporated (every point of the check processed with a comment) ·
    checked (checked by an independent reviewer, nothing to change) · confirmed (incorporated or checked, and an invited
    person confirmed a source, data point or number, gave feedback that was processed, or went through the current version). level = confidence 0-3: use level >= 2 for final decks, leave out or flag level <= 1."""
    f, a = [], []
    if project:
        f.append("e.project = ?"); a.append(ex._slug(project))
    if ids:
        f.append(f"e.id IN ({','.join('?' * len(ids))})"); a += [int(i) for i in ids]
    rows = store.db.execute("SELECT e.id, e.kind, e.current_version, v.extract, coalesce(v.html_sha, v.png_sha) AS chash "
                            "FROM exhibit e JOIN exhibit_version v "
                            "ON v.exhibit_id = e.id AND v.n = e.current_version" + (f" WHERE {' AND '.join(f)}" if f else ""), a).fetchall()
    if not rows:
        return {}
    idl = [r["id"] for r in rows]
    cur_of = {r["id"]: r["current_version"] or 1 for r in rows}
    twins = _twins(store, idl)
    # every exhibit whose records count: the slides asked for and their identical copies in other slide sets
    srcs = {eid: [(eid, None, None, None, None)] + twins.get(eid, []) for eid in idl}
    allids = sorted({t[0] for v in srcs.values() for t in v})
    ph = ",".join("?" * len(allids))

    def fold(eid, rows_by_src, fn):
        for y, w, cut, own, gone in srcs[eid]:
            for rec in rows_by_src.get(y, ()):
                if y == eid:
                    fn(rec, rec["version"] if "version" in rec.keys() else None, False)
                    continue
                u = rec["version"] if "version" in rec.keys() else None
                t = rec["created_at"] if "created_at" in rec.keys() else None
                if gone and t and t >= gone:
                    continue                     # recorded on the other after this slide was changed
                if u is not None:
                    if u > w:
                        continue                 # the copy moved on after the shared version: not this content
                    u = own if u == w else own - 1
                elif cut and t and t >= cut:
                    continue
                fn(rec, u, True)

    def own_cur(eid, y):
        return next(t[3] for t in srcs[eid] if t[0] == y) == cur_of[eid]

    def by_src(sql, args):
        d: dict[int, list] = {}
        for rec in store.db.execute(sql, args):
            d.setdefault(rec["exhibit_id"], []).append(rec)
        return d

    links = by_src(f"SELECT DISTINCT exhibit_id, target_id FROM exhibit_link WHERE target_kind = 'document' AND exhibit_id IN ({ph})", allids)
    fbrows = by_src(f"SELECT exhibit_id, version, status, coalesce(weight, 'error') w, resolved_version rv, 1 n, created_at, "
                    f"source = 'person' persons, source IN ('ai','person') checked FROM feedback WHERE exhibit_id IN ({ph})", allids)
    numrows = by_src(f"SELECT exhibit_id, version, verdict, count(*) c, created_at FROM exhibit_number WHERE exhibit_id IN ({ph}) "
                     f"GROUP BY exhibit_id, version, verdict, created_at", allids)
    chkrows = by_src(f"SELECT c.exhibit_id, c.created_at, r.kind, c.verdict, r.first_name || ' ' || r.last_name who "
                     f"FROM item_check c JOIN reviewer r ON r.id = c.reviewer_id WHERE c.aspect = 'usage' AND c.exhibit_id IN ({ph}) "
                     f"AND c.id IN (SELECT max(id) FROM item_check GROUP BY target_kind, target_id, reviewer_id, exhibit_id, aspect)", allids)
    progrows = by_src(f"SELECT DISTINCT s.exhibit_id, s.version, rv.first_name || ' ' || rv.last_name AS who FROM review_progress p "
                      f"JOIN review_slide s ON s.review_id = p.review_id AND s.position = p.position "
                      f"JOIN reviewer rv ON rv.id = p.reviewer_id WHERE s.exhibit_id IN ({ph})", allids)
    exchk = by_src(f"SELECT exhibit_id, version, result, created_at, id FROM exhibit_check WHERE exhibit_id IN ({ph})", allids)

    docs, fb, nums, checks, done_by, last_check, clean_now = {}, {}, {}, {}, {}, {}, set()
    for eid in idl:
        cur = cur_of[eid]
        ds: set = set()
        fold(eid, links, lambda rec, u, tw: ds.add(rec["target_id"]))
        if ds:
            docs[eid] = len(ds)
        d = {"open": 0, "decision": 0, "done": 0, "suggestions": 0, "max_resolved": 0, "max_version": 0, "persons": 0}
        seen: list = []

        def add_fb(rec, u, tw, d=d, seen=seen, eid=eid):
            status = rec["status"]
            if tw and status == "incorporated" and own_cur(eid, rec["exhibit_id"]) \
                    and (rec["rv"] or 0) > next(t[1] for t in srcs[eid] if t[0] == rec["exhibit_id"]):
                status = "open"                  # fixed only in the other copy, this one still shows the content: not fixed here
            if rec["checked"]:
                seen.append(u)
            if rec["w"] == "suggestion" and status in OPEN:
                d["suggestions"] += rec["n"]     # suggestions never block the stage
                return
            key = "open" if status in OPEN else "decision" if status == "decision" else "done"
            d[key] += rec["n"]
            d["max_version"] = max(d["max_version"], u or 0)
            d["persons"] += rec["persons"] or 0
        fold(eid, fbrows, add_fb)
        if any(d.values()):
            fb[eid] = d
        nv: dict = {}
        fold(eid, numrows, lambda rec, u, tw: nv.setdefault(u, {}).__setitem__(rec["verdict"], nv.get(u, {}).get(rec["verdict"], 0) + rec["c"]))
        if nv:
            top = max(nv)
            nums[eid] = {"version": top} | nv[top]
            seen += list(nv)
        c = {"person_ok": 0, "ai_ok": 0, "who": set()}

        def add_chk(rec, u, tw, c=c):
            if rec["verdict"] == "ok":
                c["person_ok" if rec["kind"] == "person" else "ai_ok"] += 1
            if rec["who"] and rec["who"].strip():
                c["who"].add(rec["who"].strip())
        fold(eid, chkrows, add_chk)
        if c["person_ok"] or c["ai_ok"] or c["who"]:
            checks[eid] = c
        who: list = []
        fold(eid, progrows, lambda rec, u, tw: who.append(rec["who"].strip()) if u == cur and rec["who"].strip() not in who else None)
        if who:
            done_by[eid] = who
        latest: list = []

        def add_exchk(rec, u, tw):
            seen.append(u)
            if u == cur:
                latest.append((rec["created_at"], rec["id"], rec["result"]))
        fold(eid, exchk, add_exchk)
        if seen:
            last_check[eid] = max(seen)
        # the latest check of exactly the current content was clean: the revision was verified (points answered after it –
        # suggestions, explanations – change nothing on the slide; a fix would have made a new version)
        if latest and max(latest)[2] == "clean":
            clean_now.add(eid)
    foreign = _foreign_notes(store, idl)
    out = {}
    for r in rows:
        eid, cur = r["id"], r["current_version"] or 1
        x = json.loads(r["extract"]) if r["extract"] else {}
        embedded = len(x.get("source_list") or [])
        d_fb = fb.get(eid, {"open": 0, "decision": 0, "done": 0, "suggestions": 0, "max_resolved": 0, "max_version": 0, "persons": 0})
        n = nums.get(eid, {})
        c = checks.get(eid, {"person_ok": 0, "ai_ok": 0, "who": set()})
        checked_v = last_check.get(eid)
        info = {"version": cur, "sources": docs.get(eid, 0), "embedded_sources": embedded,
                "numbers": {k: n.get(k, 0) for k in ("sourced", "derived", "wrong", "unverified")} if n else None,
                "numbers_version": n.get("version"), "feedback": {k: d_fb[k] for k in ("open", "decision", "done", "suggestions")},
                "checked_version": checked_v, "confirmations": c["person_ok"], "ai_confirmations": c["ai_ok"],
                "reviewers": sorted(c["who"] | set(done_by.get(eid, []))), "reviewed_by": done_by.get(eid, [])}
        if twins.get(eid):
            info["shared_with"] = [_twin_brief(store, y, w) | {"own_version": own} for y, w, _, own, _ in twins[eid]]
        if checked_v is None and eid in done_by:
            checked_v = info["checked_version"] = cur      # a person went through it: it has been looked at
        if r["kind"] == "section":
            stage = None
        elif checked_v is None and x.get("no_sources"):
            stage = "not_needed"         # marked in the file: agenda, title, schedule – no sources relevant
            info["no_sources"] = x["no_sources"]
        elif checked_v is None:
            stage = "no_sources" if not docs.get(eid) and not embedded and _has_numbers(x) else "unchecked"
        elif d_fb["open"] or d_fb["decision"]:
            stage = "open"
        elif cur > checked_v and not d_fb["done"]:
            stage = "changed"            # rendered anew after a check that found nothing to incorporate: not checked any more
        elif d_fb["done"] and eid not in clean_now:
            stage = "incorporated"       # every point processed with a comment – the revision itself not yet checked again
        else:
            stage = "checked"
        if stage in ("incorporated", "checked") and (c["person_ok"] or d_fb["persons"] or eid in done_by):
            stage = "confirmed"          # level 3: incorporated or checked, and an invited person confirmed or went through it
        label = STAGE_LABEL.get(stage) if stage else None
        if stage and foreign.get(eid):
            stage, label = _apply_foreign(info, stage, label, foreign[eid], r["chash"])
        info["stage"] = stage
        info["level"] = LEVELS.get(stage) if stage else None
        info["label"] = label
        out[eid] = info
    # the latest fixed verified version (exhibit_verified): stays offered when the slide has moved on
    for r in store.db.execute(
            f"SELECT p.exhibit_id, v.n, p.stage, p.level, p.pinned_at FROM exhibit_verified p JOIN exhibit_version v ON v.id = p.version_id "
            f"WHERE p.exhibit_id IN ({','.join('?' * len(idl))}) ORDER BY v.n", idl):
        if r["exhibit_id"] in out:
            o = out[r["exhibit_id"]]
            o["verified"] = {"version": r["n"], "stage": r["stage"], "level": r["level"], "label": STAGE_LABEL.get(r["stage"]),
                             "pinned_at": r["pinned_at"], "is_current": r["n"] == o["version"]}
    return out


def _foreign_notes(store: Store, ids: list[int]) -> dict[int, list]:
    """Verification stages slides had at their sender (slide packages, fundus.package): {exhibit id: [rows]}."""
    if not ids:
        return {}
    out: dict[int, list] = {}
    try:
        for r in store.db.execute(f"SELECT * FROM exhibit_foreign_stage WHERE exhibit_id IN ({','.join('?' * len(ids))}) "
                                  f"ORDER BY level DESC, id DESC", ids):
            out.setdefault(r["exhibit_id"], []).append(r)
    except sqlite3.OperationalError:          # a database opened read-only before the migration
        return {}
    return out


def _apply_foreign(info: dict, stage: str, label: str | None, notes: list, chash: str | None) -> tuple[str, str | None]:
    """The stage a slide had at its sender counts as long as the current version has exactly that content (same hash):
    "geprüft bei <Absender>". It lapses with the first change – then the slide shows "seit Prüfung geändert" unless it was
    checked here. Own open points always win."""
    good = [n for n in notes if (n["level"] or 0) >= 2]
    hit = next((n for n in good if n["content_hash"] == chash), None)
    if hit is not None:
        info["foreign"] = {"stage": hit["stage"], "level": hit["level"], "origin": hit["origin"], "checked_at": hit["checked_at"],
                           "reviewers": json.loads(hit["reviewers"]) if hit["reviewers"] else [], "received": hit["received_id"]}
        if stage != "open" and LEVELS.get(stage, 0) < (hit["level"] or 0) and hit["stage"] in LEVELS:
            word = "bestätigt" if hit["stage"] == "confirmed" else "geprüft"
            return hit["stage"], f"{word} bei {hit['origin']}"
        return stage, label
    if good:
        info["foreign_lapsed"] = {"origin": good[0]["origin"], "stage": good[0]["stage"], "checked_at": good[0]["checked_at"]}
        if stage in ("unchecked", "no_sources"):
            return "changed", f"seit Prüfung bei {good[0]['origin']} geändert"
    return stage, label


PIN_STAGES = ("checked", "confirmed")


def pin_verified(store: Store, project: str | None = None, ids: list[int] | None = None) -> list[dict]:
    """Fix every slide whose current version is 'checked' or 'confirmed' (level >= 2): the version is kept as the verified
    one, also after the slide is developed further. A later 'confirmed' upgrades the pin. Returns the new or upgraded pins."""
    changed = []
    for eid, s in slide_status(store, project=project, ids=ids).items():
        if s.get("stage") not in PIN_STAGES:
            continue
        vid = store.db.execute("SELECT id FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, s["version"])).fetchone()
        if vid is None:
            continue
        had = store.db.execute("SELECT level FROM exhibit_verified WHERE exhibit_id = ? AND version_id = ?", (eid, vid[0])).fetchone()
        if had is None:
            store.db.execute("INSERT INTO exhibit_verified(exhibit_id, version_id, stage, level) VALUES (?,?,?,?)",
                             (eid, vid[0], s["stage"], s["level"]))
        elif had[0] < s["level"]:
            store.db.execute("UPDATE exhibit_verified SET stage = ?, level = ?, updated_at = ? WHERE exhibit_id = ? AND version_id = ?",
                             (s["stage"], s["level"], _now(), eid, vid[0]))
        else:
            continue
        changed.append({"exhibit": eid, "version": s["version"], "stage": s["stage"]})
    if changed:
        store.db.commit()
    return changed


# ------------------------------------------------------------------ review progress ("Folie durchgesehen")
def set_progress(store: Store, rid: int, reviewer_id: int, positions, actor: str = "review-app") -> dict:
    """Replace the set of slides a reviewer has marked as gone through (from the review app). Unknown positions are ignored."""
    slides = dict(store.db.execute("SELECT position, exhibit_id FROM review_slide WHERE review_id = ?", (rid,)).fetchall())
    want = {int(p) for p in positions or [] if str(p).lstrip("-").isdigit() and int(p) in slides}
    have = {r[0] for r in store.db.execute("SELECT position FROM review_progress WHERE review_id = ? AND reviewer_id = ?", (rid, reviewer_id))}
    finished = bool(slides) and want >= set(slides)
    if want == have and not finished:
        return {"changed": 0}
    with store.batch(actor):
        for p in have - want:
            store.db.execute("DELETE FROM review_progress WHERE review_id = ? AND reviewer_id = ? AND position = ?", (rid, reviewer_id, p))
        for p in want - have:
            store.db.execute("INSERT INTO review_progress(review_id, reviewer_id, position, exhibit_id) VALUES (?,?,?,?)",
                             (rid, reviewer_id, p, slides[p]))
        store.log("review", rid, "progress", {"reviewer": reviewer_id, "done": len(want)})
        if finished:   # every slide reviewed: this person's review is finished, the link becomes read-only
            if store.db.execute("UPDATE review_access SET finished_at = ? WHERE review_id = ? AND reviewer_id = ? AND finished_at IS NULL",
                                (_now(), rid, reviewer_id)).rowcount:
                store.log("review", rid, "finished", {"reviewer": reviewer_id})
            if not store.db.execute("SELECT 1 FROM review_access WHERE review_id = ? AND revoked_at IS NULL AND finished_at IS NULL", (rid,)).fetchone():
                store.db.execute("UPDATE review SET completed_at = coalesce(completed_at, ?) WHERE id = ?", (_now(), rid))
    return {"changed": len(want ^ have), "done": len(want), "finished": finished}


def progress(store: Store, rid: int) -> dict:
    """Per invited person: slides gone through, percent of the review, last activity, own contributions; per slide: who is through."""
    total = store.db.execute("SELECT count(*) FROM review_slide WHERE review_id = ?", (rid,)).fetchone()[0]
    people = []
    for r in store.db.execute("SELECT r.*, a.revoked_at, a.finished_at FROM review_access a JOIN reviewer r ON r.id = a.reviewer_id WHERE a.review_id = ? "
                              "ORDER BY lower(r.last_name)", (rid,)):
        done, last = store.db.execute("SELECT count(*), max(done_at) FROM review_progress WHERE review_id = ? AND reviewer_id = ?",
                                      (rid, r["id"])).fetchone()
        fb, fb_last = store.db.execute("SELECT count(*), max(created_at) FROM feedback WHERE review_id = ? AND reviewer_id = ?",
                                       (rid, r["id"])).fetchone()
        checks = store.db.execute("SELECT count(*) FROM item_check WHERE review_id = ? AND reviewer_id = ?", (rid, r["id"])).fetchone()[0]
        pct = round(100 * done / total) if total else 0
        state = "done" if r["finished_at"] or (total and done >= total) else "active" if done or fb or checks else "invited"
        people.append({"id": r["id"], "name": display(r), "done": done, "total": total, "percent": pct, "state": state, "finished_at": r["finished_at"],
                       "contributions": fb, "checks": checks, "last": max(filter(None, [last, fb_last]), default=None),
                       "revoked": r["revoked_at"] is not None})
    per_slide: dict[int, list[str]] = {}
    for r in store.db.execute("SELECT p.position, rv.first_name, rv.last_name FROM review_progress p JOIN reviewer rv ON rv.id = p.reviewer_id "
                              "WHERE p.review_id = ? ORDER BY p.position", (rid,)):
        per_slide.setdefault(r["position"], []).append(f"{r['first_name']} {r['last_name']}".strip())
    return {"total": total, "people": people, "slides": per_slide}


def reviewed_by(store: Store, eid: int) -> list[str]:
    """People who have gone through the current version of a slide in some review."""
    return [f"{r[0]} {r[1]}".strip() for r in store.db.execute(
        "SELECT DISTINCT rv.first_name, rv.last_name FROM review_progress p JOIN reviewer rv ON rv.id = p.reviewer_id "
        "JOIN review_slide s ON s.review_id = p.review_id AND s.position = p.position "
        "JOIN exhibit e ON e.id = s.exhibit_id WHERE s.exhibit_id = ? AND s.version = e.current_version", (eid,))]
