"""Review sync: Spark ⇄ review app on Firebase (Fundus Review). All connections go out from the Spark.

One cycle (every 30 s as fundus-review-sync.service, or `fundus review sync`):
- publish reviews with people that are still drafts (after a Gemini pre-check has finished): slide images to Storage under
  r/<secret>/, review, slides (with sources and data points and their verification stage), reviewers and one access
  document per personal link (access/<sha256(token)>: review, reviewer, name, active, expires) to Firestore;
- keep published reviews current: links (revoked, re-invited, expiry), the stages of sources and data points, Gemini's
  findings (read-only for reviewers), answers and status of every point ('eingearbeitet in v5: …');
- pull new contributions (items with synced == false) into the feedback inbox and mark them synced;
- remove closed or expired reviews from Firebase (after a last pull); the feedback stays in Fundus.
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import time
from datetime import datetime, timezone

from fundus import exhibits as ex
from fundus import reviews as rv
from fundus.store import Store

INTERVAL = 30
AI_AUTHOR = "ai:gemini"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if s else None


def _sig(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:16]


def _state(row) -> dict:
    return json.loads(row["remote_state"]) if row["remote_state"] else {}


def _save_state(store: Store, rid: int, state: dict) -> None:
    store.db.execute("UPDATE review SET remote_state = ? WHERE id = ?", (json.dumps(state), rid))
    store.db.commit()


def _jpeg(store: Store, png_sha: str, width: int = 1920) -> bytes:
    from PIL import Image
    im = Image.open(ex.file_path(store, png_sha, "png")).convert("RGB")
    im.thumbnail((width, width))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def slide_evidence(store: Store, eid: int) -> dict:
    """Sources and data points of a slide as the review app shows them (stage of independent checks included)."""
    e = rv.slide_evidence(store, eid)
    srcs = [{"id": s["id"], "title": s.get("title"), "publisher": s.get("publisher"), "published": s.get("published"),
             "url": s.get("url"), "link_ok": (s.get("link") or {}).get("ok"), "stage": s["verification"]["stage"],
             "confirmations": s["verification"]["confirmations"], "deep_url": s.get("deep_url"),
             "source_stage": (s["verification"].get("source") or {}).get("stage", "unchecked"),
             "quote": (s.get("evidence") or {}).get("quote")} for s in e["sources"]]
    data = [{"kind": d["kind"], "id": str(d["id"]), "label": d.get("label") or d.get("name"), "value": d.get("value"),
             "unit": d.get("unit"), "period": d.get("period"), "source_title": (d.get("source") or {}).get("title"),
             "source_url": (d.get("source") or {}).get("url"), "stage": d["verification"]["stage"],
             "confirmations": d["verification"]["confirmations"], "locator": d.get("locator"), "quote": d.get("quote"),
             "deep_url": d.get("deep_url")} for d in e["data"]]
    nums = [{**{k: x.get(k) for k in ("id", "number", "claim", "verdict", "note", "evidence", "deep_url", "by")},
             "stage": x["people"]["stage"], "confirmations": x["people"]["confirmations"]} for x in e.get("numbers") or []]
    return {"sources": srcs, "data": data, "sources_text": e.get("sources_text"), "numbers": nums}


def _slides(store: Store, rid: int) -> list:
    return store.db.execute("SELECT rs.position, rs.exhibit_id, rs.version, v.png_sha, v.title AS vtitle, e.title, e.eyebrow "
                            "FROM review_slide rs JOIN exhibit e ON e.id = rs.exhibit_id "
                            "JOIN exhibit_version v ON v.exhibit_id = rs.exhibit_id AND v.n = rs.version "
                            "WHERE rs.review_id = ? ORDER BY rs.position", (rid,)).fetchall()


def _access_docs(store: Store, row) -> dict[str, dict]:
    """access/<hash> for every link of the review (inactive when revoked, the person deleted or the review closed)."""
    exp = _ts(row["expires_at"]) or _now()
    out = {}
    for a in store.db.execute("SELECT a.*, r.first_name, r.last_name, r.deleted_at FROM review_access a "
                              "JOIN reviewer r ON r.id = a.reviewer_id WHERE a.review_id = ?", (row["id"],)):
        out[hashlib.sha256(a["token"].encode()).hexdigest()] = {
            "review": str(row["id"]), "reviewer": str(a["reviewer_id"]), "name": f"{a['first_name']} {a['last_name']}".strip(),
            "active": a["revoked_at"] is None and a["deleted_at"] is None, "expires": exp,
            "finished": a["finished_at"] is not None}   # finished: the link stays readable, writing is refused
    return out


def publish(store: Store, fc, rid: int) -> dict:
    row = store.db.execute("SELECT * FROM review WHERE id = ?", (rid,)).fetchone()
    secret = row["remote_secret"] or secrets.token_urlsafe(18)
    store.db.execute("UPDATE review SET remote_secret = ? WHERE id = ?", (secret, rid))
    store.db.commit()
    from fundus import projects
    writes, slides_sig = [], {}
    for s in _slides(store, rid):
        base = f"r/{secret}/{s['position']}-v{s['version']}"
        image = fc.upload(base + ".jpg", _jpeg(store, s["png_sha"]), "image/jpeg")
        prev_p = ex.file_path(store, s["png_sha"], "preview.png")
        preview = fc.upload(base + "-p.png", prev_p.read_bytes(), "image/png") if prev_p.exists() else image
        evd = slide_evidence(store, s["exhibit_id"])
        slides_sig[str(s["position"])] = _sig(evd)
        writes.append(fc.w_set(f"reviews/{rid}/slides/{s['position']}", {
            "position": s["position"], "exhibit": s["exhibit_id"], "version": s["version"], "title": s["vtitle"] or s["title"],
            "eyebrow": s["eyebrow"], "image": image, "preview": preview, **evd}))
    people = store.db.execute("SELECT r.* FROM review_access a JOIN reviewer r ON r.id = a.reviewer_id WHERE a.review_id = ?", (rid,)).fetchall()
    for p in people:
        writes.append(fc.w_set(f"reviews/{rid}/reviewers/{p['id']}", {"name": rv.display(p), "kind": "person"}))
    if row["gemini_state"]:
        writes.append(fc.w_set(f"reviews/{rid}/reviewers/{AI_AUTHOR}", {"name": "Gemini", "kind": "ai"}))
    title = projects.titles(store).get(row["project"]) or row["project"]
    writes.append(fc.w_set(f"reviews/{rid}", {"title": row["title"], "project": title, "round": row["round"], "status": "open",
                                              "slideCount": len(slides_sig), "expires": _ts(row["expires_at"]), "gemini": bool(row["gemini_state"]),
                                              "published": _now()}))
    acc = _access_docs(store, row)
    writes += [fc.w_set(f"access/{h}", d) for h, d in acc.items()]
    fc.commit(writes)
    state = {"access": {h: d["reviewer"] for h, d in acc.items()}, "access_sig": _sig(acc), "slides": slides_sig}
    store.db.execute("UPDATE review SET status = 'open', published_at = ?, remote_state = ? WHERE id = ?",
                     (_now().strftime("%Y-%m-%d %H:%M:%S"), json.dumps(state), rid))
    store.log("review", rid, "publish", {"slides": len(slides_sig), "links": len(acc)})
    store.db.commit()
    return {"published": rid, "slides": len(slides_sig), "links": len(acc)}


def sync_access(store: Store, fc, row) -> int:
    state = _state(row)
    acc = _access_docs(store, row)
    if _sig(acc) == state.get("access_sig"):
        return 0
    writes = [fc.w_set(f"access/{h}", d) for h, d in acc.items()]
    writes += [fc.w_delete(f"access/{h}") for h in state.get("access", {}) if h not in acc]   # replaced links (re-invited)
    people = store.db.execute("SELECT r.* FROM review_access a JOIN reviewer r ON r.id = a.reviewer_id WHERE a.review_id = ?", (row["id"],))
    writes += [fc.w_set(f"reviews/{row['id']}/reviewers/{p['id']}", {"name": rv.display(p), "kind": "person"}) for p in people]
    fc.commit(writes)
    state.update(access={h: d["reviewer"] for h, d in acc.items()}, access_sig=_sig(acc))
    _save_state(store, row["id"], state)
    return len(writes)


def refresh_slides(store: Store, fc, row) -> int:
    """Carry new verification stages (people, Gemini) and link checks into the published slides."""
    state = _state(row)
    old = state.get("slides", {})
    writes = []
    for s in _slides(store, row["id"]):
        evd = slide_evidence(store, s["exhibit_id"])
        sg = _sig(evd)
        if old.get(str(s["position"])) != sg:
            writes.append(fc.w_merge(f"reviews/{row['id']}/slides/{s['position']}", evd))
            old[str(s["position"])] = sg
    if writes:
        fc.commit(writes)
        state["slides"] = old
        _save_state(store, row["id"], state)
    return len(writes)


def _fb_sig(r) -> str:
    return f"{r['status']}|{r['resolution'] or ''}|{r['resolved_version'] or ''}"


def _remote_status(r) -> dict:
    out = {"status": r["status"]}
    if r["status"] in ("incorporated", "answered", "rejected"):
        out.update(reply=r["resolution"], resolvedVersion=r["resolved_version"])
    return out


def push_ai(store: Store, fc, rid: int) -> int:
    """Gemini's findings, read-only for the reviewers (they see what the AI found and can comment on it)."""
    pos = {r["exhibit_id"]: r["position"] for r in store.db.execute("SELECT exhibit_id, position FROM review_slide WHERE review_id = ?", (rid,))}
    rows = store.db.execute("SELECT * FROM feedback WHERE review_id = ? AND source = 'ai' AND remote_state IS NULL", (rid,)).fetchall()
    writes = []
    for r in rows:
        if r["exhibit_id"] not in pos:
            continue
        d = {"slide": pos[r["exhibit_id"]], "exhibit": r["exhibit_id"], "version": r["version"], "kind": r["kind"], "text": r["text"],
             "author": AI_AUTHOR, "authorName": "Gemini", "created": _ts(r["created_at"]) or _now(), "synced": True,
             "fundusId": f"F-{r['id']}", "weight": r["weight"] or "error", **_remote_status(r)}
        if r["shape"]:
            d["shape"] = json.loads(r["shape"])
        if r["verdict"]:
            d.update(verdict=r["verdict"], target={"kind": r["target_kind"], "id": str(r["target_id"])})
        if r["note"]:
            d["note"] = r["note"]
        writes.append(fc.w_set(f"reviews/{rid}/items/f{r['id']}", d))
    if writes:
        fc.commit(writes)
        store.db.executemany("UPDATE feedback SET remote_state = ? WHERE id = ?", [(_fb_sig(r), r["id"]) for r in rows if r["exhibit_id"] in pos])
        store.db.commit()
    return len(writes)


def push_status(store: Store, fc, rid: int) -> int:
    """Answers and status of every point that is in the review app (reviewers' own and Gemini's)."""
    rows = store.db.execute("SELECT * FROM feedback WHERE review_id = ? AND remote_state IS NOT NULL", (rid,)).fetchall()
    writes, done = [], []
    for r in rows:
        if _fb_sig(r) == r["remote_state"]:
            continue
        ext = r["external_id"] or ""
        docid = ext.split(":", 2)[2] if ext.startswith(f"fs:{rid}:") else f"f{r['id']}"
        writes.append(fc.w_merge(f"reviews/{rid}/items/{docid}", _remote_status(r)))
        done.append((_fb_sig(r), r["id"]))
    if writes:
        fc.commit(writes)
        store.db.executemany("UPDATE feedback SET remote_state = ? WHERE id = ?", done)
        store.db.commit()
    return len(writes)


def pull(store: Store, fc, rid: int) -> dict:
    """New contributions of the reviewers → feedback inbox (and independent checks); then mark them synced."""
    items = fc.query(f"reviews/{rid}", "items", "synced", "EQUAL", False)
    if not items:
        return {"pulled": 0, "invalid": 0}
    active = {str(a["reviewer_id"]) for a in store.db.execute("SELECT reviewer_id FROM review_access WHERE review_id = ?", (rid,))}
    slides = {r["position"]: r for r in store.db.execute("SELECT * FROM review_slide WHERE review_id = ?", (rid,))}
    writes, n, bad = [], 0, 0
    for it in items:
        path = f"reviews/{rid}/items/{it['_id']}"
        s = slides.get(it.get("slide"))
        why = None
        if str(it.get("author")) not in active:
            why = "unbekannte Person"
        elif s is None:
            why = "unbekannte Folie"
        if why is None:
            try:
                target, kind = None, it.get("kind")
                if kind == "check":
                    t = it.get("target") or {}
                    evd = slide_evidence(store, s["exhibit_id"])
                    ok = ({("document", str(x["id"])) for x in evd["sources"]} | {(x["kind"], str(x["id"])) for x in evd["data"]}
                          | {("number", str(x["id"])) for x in evd["numbers"]})
                    target = (str(t.get("kind")), str(t.get("id")))
                    if target not in ok:
                        raise ValueError("Quelle oder Datenpunkt gehört nicht zu dieser Folie")
                res = rv.add(store, s["exhibit_id"], str(it.get("text") or ""), source="person", reviewer_ref=int(it["author"]),
                             review_id=rid, kind=kind if kind in ("comment", "shape", "check") else "comment", version=s["version"],
                             target=target, verdict=it.get("verdict") if kind == "check" else None, note=it.get("note"),
                             shape=it.get("shape") if kind == "shape" else None, external_id=f"fs:{rid}:{it['_id']}", actor="review-app",
                             aspect="source" if kind == "check" and it.get("aspect") == "source" and target and target[0] == "document" else "usage")
                upd = {"synced": True, "fundusId": res.get("ref"), "status": "open" if res.get("ref") else "recorded"}
                if res.get("id"):
                    store.db.execute("UPDATE feedback SET remote_state = ? WHERE id = ?", ("open||", res["id"]))
                    store.db.commit()
                writes.append(fc.w_merge(path, upd))
                n += 1
                continue
            except (ValueError, LookupError) as e:
                why = str(e)
        writes.append(fc.w_merge(path, {"synced": True, "status": "rejected", "reply": f"Nicht übernommen: {why}"}))
        bad += 1
    fc.commit(writes)
    return {"pulled": n, "invalid": bad}


def pull_progress(store: Store, fc, rid: int) -> int:
    """'Folie durchgesehen' per reviewer: reviews/<rid>/progress/<reviewer> = {slides: [positions]}."""
    active = {str(a["reviewer_id"]) for a in store.db.execute("SELECT reviewer_id FROM review_access WHERE review_id = ? AND revoked_at IS NULL", (rid,))}
    n = 0
    for d in fc.list(f"reviews/{rid}/progress"):
        if d["_id"] in active:
            n += rv.set_progress(store, rid, int(d["_id"]), d.get("slides") or [])["changed"]
    return n


def unpublish(store: Store, fc, row) -> dict:
    rid = row["id"]
    state = _state(row)
    writes = []
    for sub in ("slides", "reviewers", "items", "progress"):
        writes += [fc.w_delete(f"reviews/{rid}/{sub}/{d['_id']}") for d in fc.list(f"reviews/{rid}/{sub}")]
    writes.append(fc.w_delete(f"reviews/{rid}"))
    writes += [fc.w_delete(f"access/{h}") for h in state.get("access", {})]
    fc.commit(writes)
    files = fc.delete_prefix(f"r/{row['remote_secret']}/") if row["remote_secret"] else 0
    state["unpublished_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
    _save_state(store, rid, state)
    store.log("review", rid, "unpublish", {"documents": len(writes), "files": files})
    store.db.commit()
    return {"unpublished": rid, "documents": len(writes), "files": files}


def cycle(store: Store, fc) -> dict:
    out = {"published": [], "pulled": 0, "pushed": 0, "unpublished": [], "errors": []}
    rows = store.db.execute("SELECT * FROM review WHERE id IN (SELECT DISTINCT review_id FROM review_access) ORDER BY id").fetchall()
    now = _now()
    for row in rows:
        rid = row["id"]
        try:
            st = _state(row)
            if st.get("unpublished_at"):
                continue
            expired = row["expires_at"] and _ts(row["expires_at"]) < now
            if row["published_at"] is None:
                if row["status"] == "draft" and row["gemini_state"] not in ("queued", "running") and not expired:
                    out["published"].append(publish(store, fc, rid))
                continue
            purged = row["status"] == "closed" and not store.db.execute(
                "SELECT 1 FROM review_access WHERE review_id = ? AND revoked_at IS NULL", (rid,)).fetchone()
            if purged or expired:   # deleted on request or expired: remove from Firebase; a closed review stays readable
                out["pulled"] += pull(store, fc, rid)["pulled"] + pull_progress(store, fc, rid)
                out["unpublished"].append(unpublish(store, fc, store.db.execute("SELECT * FROM review WHERE id = ?", (rid,)).fetchone()))
                continue
            out["pulled"] += pull(store, fc, rid)["pulled"] + pull_progress(store, fc, rid)
            row = store.db.execute("SELECT * FROM review WHERE id = ?", (rid,)).fetchone()   # progress may have finished someone
            out["pushed"] += sync_access(store, fc, row) + refresh_slides(store, fc, row) + push_ai(store, fc, rid)
            out["pushed"] += push_status(store, fc, rid)
        except Exception as e:   # one review must not stop the others
            store.db.rollback()
            out["errors"].append(f"review {rid}: {e}"[:300])
    try:   # people's checks and progress can lift slides to 'checked'/'confirmed': fix those versions
        from fundus import reviews as rv
        out["pinned"] = len(rv.pin_verified(store))
    except Exception as e:
        store.db.rollback()
        out["errors"].append(f"pin: {e}"[:300])
    return out


def loop(db_path, interval: int = INTERVAL, client=None) -> None:
    from fundus.firebase import Client
    fc = client or Client()
    while True:
        st = None
        try:
            st = Store(db_path)                 # inside the try: a locked database means 'next round', not a crash
            st.actor = "review-sync"
            res = cycle(st, fc)
            if res["published"] or res["pulled"] or res["pushed"] or res["unpublished"] or res["errors"]:
                print(json.dumps({"ts": _now().strftime("%Y-%m-%d %H:%M:%S"), **res}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"ts": _now().strftime("%Y-%m-%d %H:%M:%S"), "error": str(e)[:300]}), flush=True)
        finally:
            if st is not None:
                st.close()
        time.sleep(interval)
