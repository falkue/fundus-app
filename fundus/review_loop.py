"""Gemini check loop, started from a Claude session (fundus_review action='loop').

Round 1: Gemini checks the given slides (slides | set | deck). Then the loop waits for the session: it works in every
error (fundus_feedback / fundus_feedback_resolve). As soon as no error on the loop's slides is open any more, the next round
starts by itself with the slides that were revised or answered (stage 'incorporated' or 'changed') – Gemini checks only the
fixes there (follow-up). The loop ends when no slide needs changes, when max_rounds is reached, or on stop. Points waiting
for Falk ('decision') do not hold the loop up; they are listed in the status.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fundus import reviews as rv
from fundus.store import Store

DEFAULT_ROUNDS = 5
MAX_ROUNDS = 8             # hard limit (Falk): never more than 8 Gemini rounds
RECHECK = ("incorporated", "changed")
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row(store: Store, loop_id) -> dict:
    r = store.db.execute("SELECT * FROM review_loop WHERE id = ?", (int(loop_id),)).fetchone()
    if r is None:
        raise LookupError(f"loop {loop_id} not found")
    return dict(r)


def _set(store: Store, loop_id: int, **kv) -> None:
    kv["updated_at"] = _now()
    store.db.execute(f"UPDATE review_loop SET {', '.join(k + ' = ?' for k in kv)} WHERE id = ?", (*kv.values(), loop_id))
    store.db.commit()


def start(store: Store, project: str, slides=None, set_ref=None, deck=None, iterations: int | None = None, focus=None,
          model: str | None = None, by: str | None = None) -> dict:
    """Queue a loop; the ticker (tick) starts round 1. iterations: Gemini rounds at most (default 5)."""
    from fundus import exhibits as ex
    n = DEFAULT_ROUNDS if iterations is None else int(iterations)
    if not 1 <= n <= MAX_ROUNDS:
        raise ValueError(f"iterations must be 1-{MAX_ROUNDS}")
    project = ex._slug(project)
    _, _, _, ids = rv._slides_for(store, project, slides, set_ref, deck)   # validates the scope now, not in the ticker
    if not ids:
        raise ValueError("no slides to check")
    busy = store.db.execute("SELECT id FROM review_loop WHERE project = ? AND state IN ('queued','checking','waiting')",
                            (project,)).fetchone()
    if busy:
        raise ValueError(f"loop {busy[0]} is still running for {project} – fundus_review(action='loop_status', loop={busy[0]}) "
                         "or action='loop_stop'")
    scope = {"slides": [str(s) for s in slides]} if slides else {"deck": deck} if deck not in (None, "") else {"set": set_ref}
    cur = store.db.execute("INSERT INTO review_loop(project, scope, focus, model, max_rounds, exhibits, rounds, created_by) "
                           "VALUES (?,?,?,?,?,?,?,?)", (project, json.dumps(scope), json.dumps(focus) if focus else None, model, n,
                                                       json.dumps(ids), "[]", by))
    store.db.commit()
    return status(store, cur.lastrowid)


def stop(store: Store, loop_id) -> dict:
    lp = _row(store, loop_id)
    if lp["state"] in ("done", "stopped"):
        return status(store, lp["id"])
    _set(store, lp["id"], state="stopped", reason="stopped by request")
    return status(store, lp["id"])


def _open_errors(store: Store, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return dict(store.db.execute(
        f"SELECT exhibit_id, count(*) FROM feedback WHERE exhibit_id IN ({','.join('?' * len(ids))}) "
        "AND coalesce(weight, 'error') = 'error' AND status IN ('open', 'in_progress') GROUP BY exhibit_id", ids).fetchall())


def _decisions(store: Store, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    return dict(store.db.execute(
        f"SELECT exhibit_id, count(*) FROM feedback WHERE exhibit_id IN ({','.join('?' * len(ids))}) AND status = 'decision' "
        "GROUP BY exhibit_id", ids).fetchall())


def _round_summary(store: Store, rid: int) -> dict:
    rep = rv.get(store, rid).get("gemini_report") or {}
    sl = rep.get("slides") or []
    sl = sl if isinstance(sl, list) else list(sl.values())
    state = store.db.execute("SELECT gemini_state FROM review WHERE id = ?", (rid,)).fetchone()[0]
    return {"review": rid, "slides": len(sl), "state": state,
            "with_errors": sum(1 for s in sl if s.get("clean") is False or s.get("errors") or s.get("not_fixed") or s.get("new_errors")),
            "fixed": sum(s.get("fixed") or 0 for s in sl), "not_fixed": sum(s.get("not_fixed") or 0 for s in sl),
            "new_errors": sum(s.get("new_errors") or 0 for s in sl), "failed": sum(1 for s in sl if s.get("error")),
            "skipped": sum(1 for s in sl if s.get("skipped"))}


def _new_round(store: Store, lp: dict, slides: list[int] | None, start_fn) -> None:
    scope = json.loads(lp["scope"])
    n = lp["round"] + 1
    kw = {"slides": [str(i) for i in slides]} if slides is not None else {
        "slides": scope.get("slides"), "set_ref": scope.get("set"), "deck": scope.get("deck")}
    out = rv.create(store, lp["project"], gemini=True, focus=json.loads(lp["focus"]) if lp["focus"] else None, model=lp["model"],
                    title=None, actor=lp["created_by"] or "loop", **kw)
    t = store.db.execute("SELECT title FROM review WHERE id = ?", (out["id"],)).fetchone()[0]
    store.db.execute("UPDATE review SET title = ? WHERE id = ?", (f"Prüfschleife {lp['id']} · Runde {n}/{lp['max_rounds']} · {t}"[:160], out["id"]))
    _set(store, lp["id"], round=n, state="checking", current_review=out["id"])
    start_fn(out["id"])


def tick(store: Store, start_fn) -> list[int]:
    """Advance every active loop one step. start_fn(review_id) runs a Gemini check in the background. Returns touched loops."""
    touched = []
    with _lock:
        for lp in [dict(r) for r in store.db.execute("SELECT * FROM review_loop WHERE state IN ('queued','checking','waiting')")]:
            try:
                ids = json.loads(lp["exhibits"] or "[]")
                if lp["state"] == "queued":
                    _new_round(store, lp, None, start_fn)
                    touched.append(lp["id"])
                    continue
                if lp["state"] == "checking":
                    g = store.db.execute("SELECT gemini_state FROM review WHERE id = ?", (lp["current_review"],)).fetchone()
                    if g is None:
                        _set(store, lp["id"], state="stopped", reason="the round's review was deleted")
                    elif g[0] in ("done", "failed"):
                        rounds = json.loads(lp["rounds"] or "[]") + [{"round": lp["round"], **_round_summary(store, lp["current_review"]),
                                                                     "finished_at": _now()}]
                        _set(store, lp["id"], rounds=json.dumps(rounds), state="waiting")
                        lp = _row(store, lp["id"])
                    else:
                        continue
                if lp["state"] != "waiting":
                    touched.append(lp["id"])
                    continue
                if _open_errors(store, ids):
                    continue                     # the session's turn: work in the errors
                stages = rv.slide_status(store, ids=ids)
                todo = [i for i in ids if (stages.get(i) or {}).get("stage") in RECHECK]
                if not todo:
                    left = _decisions(store, ids)
                    _set(store, lp["id"], state="done",
                         reason="no slide needs changes" + (f" – {sum(left.values())} points wait for Falk's decision" if left else ""))
                elif lp["round"] >= lp["max_rounds"]:
                    _set(store, lp["id"], state="done", reason=f"{lp['max_rounds']} rounds reached – {len(todo)} slides not checked again")
                else:
                    _new_round(store, lp, todo, start_fn)
                touched.append(lp["id"])
            except Exception as e:   # one broken loop must not stop the others
                _set(store, lp["id"], state="stopped", reason=f"error: {e}"[:300])
    return touched


def status(store: Store, loop_id=None, project: str | None = None) -> dict:
    """A loop's state, rounds, the running check's progress and – when it is the session's turn – what to do next."""
    if loop_id is None:
        if not project:
            raise ValueError("loop_status needs loop (id) or project")
        from fundus import exhibits as ex
        r = store.db.execute("SELECT id FROM review_loop WHERE project = ? ORDER BY id DESC LIMIT 1", (ex._slug(project),)).fetchone()
        if r is None:
            raise LookupError(f"no loop for {project}")
        loop_id = r[0]
    lp = _row(store, loop_id)
    ids = json.loads(lp["exhibits"] or "[]")
    out = {"loop": lp["id"], "project": lp["project"], "scope": json.loads(lp["scope"]), "state": lp["state"],
           "round": lp["round"], "max_rounds": lp["max_rounds"], "reason": lp["reason"], "slides": len(ids),
           "rounds": json.loads(lp["rounds"] or "[]"), "started_at": lp["created_at"], "updated_at": lp["updated_at"]}
    if lp["state"] == "checking" and lp["current_review"]:
        rep = rv.get(store, lp["current_review"]).get("gemini_report") or {}
        out["running"] = {"review": lp["current_review"], "done": rep.get("done"), "total": rep.get("total")}
    out["kpi"] = kpi(out["rounds"])
    errs, dec = _open_errors(store, ids), _decisions(store, ids)
    stages = rv.slide_status(store, ids=ids)
    from collections import Counter
    out["stages"] = dict(Counter(s.get("stage") for s in stages.values() if s.get("stage")))
    out["open_errors"] = sum(errs.values())
    out["decisions"] = sum(dec.values())
    if lp["state"] == "waiting" and errs:
        refs = [r[0] for r in store.db.execute(
            f"SELECT coalesce(project || '/' || slug, id) FROM exhibit WHERE id IN ({','.join('?' * len(errs))})", list(errs))]
        out["your_turn"] = {"slides": refs,
                            "next": f"Work in the errors: fundus_feedback(project='{lp['project']}') → change and re-render the slides → "
                                    "fundus_feedback_resolve for EVERY point. Round "
                                    f"{lp['round'] + 1} starts by itself once no error is open (max {lp['max_rounds']} rounds)."}
    elif lp["state"] == "checking":
        out["next"] = "Gemini is checking – ask again in a few minutes (or run the CLI with --wait in the background)."
    elif lp["state"] == "waiting":
        out["next"] = "All errors processed – the next round starts within half a minute."
    return out


def kpi(rounds: list[dict]) -> dict:
    """Convergence of a loop: the volume (slides Gemini actually checked, without skipped ones) and the slides with errors per
    round should shrink. shrink_pct[k] = how much smaller round k+1 was than round k; convergence_pct = the mean of these;
    first_pass_clean_pct = share of round-1 slides without errors."""
    vol = [max(0, (r.get("slides") or 0) - (r.get("skipped") or 0)) for r in rounds]
    err = [r.get("with_errors") or 0 for r in rounds]
    shrink = [round(100 * (1 - b / a), 1) if a else None for a, b in zip(vol, vol[1:])]
    valid = [x for x in shrink if x is not None]
    return {"volume_per_round": vol, "errors_per_round": err, "shrink_pct": shrink,
            "convergence_pct": round(sum(valid) / len(valid), 1) if valid else None,
            "first_pass_clean_pct": round(100 * (1 - err[0] / vol[0]), 1) if vol and vol[0] else None,
            "shrinking": all(b <= a for a, b in zip(vol, vol[1:])),
            "clean_after_round": next((r["round"] for r in rounds if not r.get("with_errors")), None)}


def list_loops(store: Store, project: str | None = None, limit: int = 20) -> list[dict]:
    """Recent loops with state, rounds and KPI (portal: Prüfungen)."""
    q, a = "SELECT id FROM review_loop", []
    if project:
        q, a = q + " WHERE project = ?", [project]
    return [status(store, r[0]) for r in store.db.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit)).fetchall()]


def wait(db_path, loop_id: int, timeout: int = 3600, interval: int = 15) -> dict:
    """Block until the loop needs the session (waiting with open errors) or has ended (CLI --wait)."""
    import time
    t0 = time.time()
    while True:
        st = Store(db_path)
        try:
            s = status(st, loop_id)
        finally:
            st.close()
        if s["state"] in ("done", "stopped") or s.get("your_turn") or time.time() - t0 > timeout:
            return s
        time.sleep(interval)
