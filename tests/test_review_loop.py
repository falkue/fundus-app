"""Gemini check loop: round 1 → the session works the errors in → the next round checks only the revised slides → ends
early once nothing needs changing; KPI on the shrinking volume."""
from fundus import gemini as gm
from fundus import review_loop as rl
from fundus import reviews as rv
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)
from test_gemini import build


def test_loop_until_clean(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    a, b = s[0]["exhibit_id"], s[1]["exhibit_id"]
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    calls = []

    def gen(model, parts, schema, max_tokens=16384, search=False):
        text = " ".join(p.get("text", "") for p in parts)
        calls.append(schema is gm.FOLLOWUP_SCHEMA)
        if schema is gm.FOLLOWUP_SCHEMA:
            return {"verdict": "clean", "summary": "fixed", "fixes": [], "new_errors": []}, {}
        bad = "slide 1 of 2" in text
        return {"verdict": "issues" if bad else "clean", "summary": "x", "comments": [
            {"text": "Year missing in title", "severity": "high", "type": "error", "evidence": "source is 2026-Q2"}] if bad else [],
            "number_checks": [], "claim_checks": [], "missing_sources": [], "source_checks": [], "datapoint_checks": []}, {}
    monkeypatch.setattr(gm, "generate", gen)
    run = lambda rid: gm.run_review(store.path, rid)
    lp = rl.start(store, "acme-dealerships", slides=[str(a), str(b)], iterations=None, focus=["sources"])
    assert lp["state"] == "queued" and lp["max_rounds"] == 5
    rl.tick(store, run)                      # round 1 runs (synchronously here)
    rl.tick(store, run)                      # round 1 done → waiting for the session
    st = rl.status(store, lp["loop"])
    assert st["state"] == "waiting" and st["round"] == 1 and st["your_turn"]["slides"] and st["open_errors"] == 1
    rl.tick(store, run)
    assert rl.status(store, lp["loop"])["round"] == 1          # still the session's turn
    # the session fixes slide a and resolves the point → round 2 checks only slide a (follow-up)
    reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-0", page("Dealers 0 Q2 2026"), png=png_bytes(rgb=(200, 1, 1))))
    for x in rv.inbox(store, exhibit=a)["slides"][0]["items"]:
        rv.resolve(store, [x["ref"]], "incorporated", "year added")
    rl.tick(store, run)
    rl.tick(store, run)
    st = rl.status(store, lp["loop"])
    assert st["round"] == 2 and calls[-1] is True
    assert [r["slides"] for r in st["rounds"]] == [2, 1]
    assert st["state"] == "done" and "no slide needs changes" in st["reason"]       # ended early, not after 5 rounds
    k = st["kpi"]
    assert k["volume_per_round"] == [2, 1] and k["errors_per_round"] == [1, 0] and k["shrink_pct"] == [50.0]
    assert k["first_pass_clean_pct"] == 50.0 and k["shrinking"] and k["clean_after_round"] == 2


def test_loop_limits(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    lp = rl.start(store, "acme-dealerships", slides=[str(s[0]["exhibit_id"])], iterations=2)
    assert lp["max_rounds"] == 2
    try:
        rl.start(store, "acme-dealerships", slides=[str(s[0]["exhibit_id"])])
        raise AssertionError("second loop for the same project must be refused")
    except ValueError as e:
        assert "still running" in str(e)
    assert rl.stop(store, lp["loop"])["state"] == "stopped"
    try:
        rl.start(store, "acme-dealerships", slides=[str(s[0]["exhibit_id"])], iterations=0)
        raise AssertionError
    except ValueError:
        pass
    try:
        rl.start(store, "acme-dealerships", slides=[str(s[0]["exhibit_id"])], iterations=9)
        raise AssertionError("more than 8 rounds must be refused")
    except ValueError as e:
        assert "1-8" in str(e)
    assert rl.start(store, "acme-dealerships", slides=[str(s[0]["exhibit_id"])], iterations=8)["max_rounds"] == 8


def test_only_the_server_drives_loops(monkeypatch):
    """A short-lived process importing mcp_server must not start rounds (its Gemini thread would die with it)."""
    from fundus import mcp_server as ms
    from fundus import review_loop as rl
    calls = []
    monkeypatch.setattr(rl, "tick", lambda st, fn: calls.append(1) or [])
    monkeypatch.setattr(ms, "_IN_SERVER", False)
    ms._loop_tick()
    assert calls == []
