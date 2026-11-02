"""Gemini review: source text and excerpts, service-account token, a whole review run with a faked model (feedback,
boxes, checks, verification, deck remarks, progress report, retry)."""
import base64
import json

import pytest

from fundus import gemini as gm
from fundus import reviews as rv
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


def test_html_text_and_needles():
    t = gm.html_text("<html><script>var x=1</script><style>p{}</style><p>Adoption 18,3 %</p><div>82% of dealers</div></html>")
    assert "var x" not in t and "Adoption 18,3 %" in t and "82% of dealers" in t
    assert gm.needles("82% of dealers in Q2 2026", "18.3", None, "1’250 Händler") == ["82", "2026", "18.3", "1’250"]


def test_excerpt_keeps_passages_around_numbers():
    filler = "lorem ipsum " * 20000
    text = "HEAD " + filler + " the share was 82% in Q2 " + filler + " and 18,3 % in Germany " + filler
    out = gm.excerpt(text, ["82", "18.3"], limit=8000, window=60)
    assert len(out) < 9000 and out.startswith("HEAD")
    assert "the share was 82%" in out and "18,3 % in Germany" in out and "[excerpt:" in out
    assert gm.excerpt("short text", ["1"]) == "short text"


def test_fetch_source_cached(store, monkeypatch, tmp_path):
    monkeypatch.setenv("FUNDUS_SOURCE_CACHE", str(tmp_path / "cache"))
    calls = []

    class R:
        headers = {"content-type": "text/html"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"<html><p>82% of dealers use AI</p></html>"
    monkeypatch.setattr(gm.urllib.request, "urlopen", lambda req, timeout=0: calls.append(req.full_url) or R())
    a = gm.fetch_source(store, "https://example.org/x")
    b = gm.fetch_source(store, "https://example.org/x")
    assert a["ok"] and a["kind"] == "html" and "82% of dealers" in b["text"] and len(calls) == 1

    def boom(req, timeout=0):
        raise gm.urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(gm.urllib.request, "urlopen", boom)
    c = gm.fetch_source(store, "https://example.org/blocked")
    assert not c["ok"] and "403" in c["error"]


def test_service_account_token_is_a_signed_jwt(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    kf = tmp_path / "sa.json"
    kf.write_text(json.dumps({"client_email": "x@p.iam.gserviceaccount.com", "private_key": pem, "private_key_id": "k1",
                              "token_uri": "https://oauth2.test/token"}))
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return json.dumps({"access_token": "ya29.test", "expires_in": 3600}).encode()
    def fake(req, timeout=0):
        seen["url"], seen["data"] = req.full_url, req.data.decode()
        return R()
    monkeypatch.setattr(gm.urllib.request, "urlopen", fake)
    tok, exp = gm._sa_token(str(kf))
    assert tok == "ya29.test" and seen["url"] == "https://oauth2.test/token"
    jwt = dict(x.split("=", 1) for x in seen["data"].split("&"))["assertion"]
    head, claim, sig = jwt.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert json.loads(pad(claim))["iss"] == "x@p.iam.gserviceaccount.com"
    key.public_key().verify(pad(sig), f"{head}.{claim}".encode(), padding.PKCS1v15(), hashes.SHA256())   # raises if wrong


def build(store, root):
    doc = store.document("Cox AI tracker", url="https://www.coxautoinc.com/tracker", publisher="Cox Automotive")
    obs = store.add("observation", {"metric": "US dealers using AI", "value": 82, "unit": "%", "period": "2026-Q2", "document_id": doc})
    store.db.commit()
    out = []
    for i in range(2):
        html = page(f"Dealers {i}").replace("<head>", f'<head><meta name="fundus-refs" content="document:{doc} observation:{obs}">', 1)
        out.append(reg(store, write(root, f"Acme-Dealerships/exhibits/kpi-{i}", html, png=png_bytes(rgb=(9, 9 * i, 90)))))
    return doc, obs, out


def fake_model(doc, obs, fail_pos=None):
    calls = []

    def generate(model, parts, schema, max_tokens=16384, search=False):
        calls.append((model, schema is gm.SLIDE_SCHEMA))
        assert search == (schema is gm.SLIDE_SCHEMA)                       # web search only for the slide checks
        prompt = " ".join(p.get("text", "") for p in parts)
        if schema is gm.DECK_SCHEMA:
            return {"summary": "Roter Faden schlüssig", "comments": [{"text": "Folie 2 wiederholt Folie 1", "severity": "medium", "positions": [2]}]}, \
                {"promptTokenCount": 100, "candidatesTokenCount": 10}
        pos = int(__import__("re").search(r"slide (\d+) of \d+", prompt).group(1))
        if pos == fail_pos:
            raise RuntimeError("Vertex AI 503: overloaded")
        assert "=== SOURCE source_id=" in prompt and "82% of dealers" in prompt and f"observation:{obs}" in prompt
        return {"verdict": "issues", "summary": f"Folie {pos} solide", "comments": [
            {"type": "error", "evidence": "Cox: Q2 2026", "text": "Zeitraum Q2 2026 im Titel nennen", "severity": "high", "category": "numbers", "box": [100, 50, 200, 500]},
            {"type": "suggestion", "text": "Achse beschriften", "severity": "low", "category": "design"}],
            "source_checks": [{"source_id": doc, "verdict": "ok", "note": "82% belegt", "evidence": "82% of dealers report using AI"},
                              {"source_id": 999999, "verdict": "wrong", "note": "erfunden"}],
            "datapoint_checks": [{"ref": f"observation:{obs}", "verdict": "ok", "note": "stimmt"}],
            "number_checks": [{"number": "82 %", "claim": "US-Händler mit KI", "verdict": "sourced", "note": "Cox Q2 2026", "evidence": "82% of dealers"},
                              {"number": "41 %", "claim": "Nutzen > Kosten", "verdict": "unverified", "note": "weder Quelle noch Web", "box": [600, 700, 700, 950]},
                              {"number": "-47 pts", "claim": "Lücke Umsatz", "verdict": "derived", "note": "22 - 69 = -47"},
                              {"number": "x", "claim": "?", "verdict": "maybe", "note": ""}],
            "claim_checks": [{"claim": "KI wird auf Erwartung gekauft", "verdict": "doubt", "note": "nur US-Daten", "url": "https://example.org/a"}],
            "missing_sources": [{"claim": "AUTOHAUS-Panel 41 %", "note": "Panel Aug 2026 nachtragen", "url": "https://autohaus.de/panel"}]}, \
            {"promptTokenCount": 1000, "candidatesTokenCount": 50, "thoughtsTokenCount": 20}
    return generate, calls


def test_run_review_turns_answers_into_feedback_and_checks(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    gen, calls = fake_model(doc, obs)
    monkeypatch.setattr(gm, "generate", gen)
    r = rv.create(store, "acme-dealerships", slides=[x["exhibit_id"] for x in s], gemini=True, model="gemini-3.8-flash")
    out = gm.run_review(store.path, r["id"])
    assert out["state"] == "done" and out["done"] == 2 and out["total"] == 2 and out["error"] is None
    assert out["tokens"] == {"prompt": 2100, "output": 150} and {c[0] for c in calls} == {"gemini-3.8-flash"}
    store.db.rollback()
    rows = store.db.execute("SELECT kind, severity, text, shape, source, verdict FROM feedback WHERE review_id = ? ORDER BY id", (r["id"],)).fetchall()
    kinds = [x["kind"] for x in rows]
    assert kinds.count("shape") == 4 and kinds.count("comment") == 7          # per slide: 2 boxes (comment, unverified number), 3 plain; + deck
    texts = [x["text"] for x in rows]
    assert sum(t.startswith("Number not verifiable: 41 %") for t in texts) == 2 and sum(t.startswith("Source missing: AUTOHAUS") for t in texts) == 2
    assert sum(t.startswith("Statement doubtful") for t in texts) == 2
    assert all(x["severity"] == "high" for x in rows if x["text"].startswith("Number not verifiable"))
    shapes = [json.loads(x["shape"]) for x in rows if x["shape"]]
    assert {"tool": "rect", "x": 0.05, "y": 0.1, "w": 0.45, "h": 0.1} in shapes and {"tool": "rect", "x": 0.7, "y": 0.6, "w": 0.25, "h": 0.1} in shapes
    assert all(x["source"] == "ai" for x in rows) and not any(x["kind"] == "check" for x in rows)   # ok checks open no point
    assert any(x["text"].startswith("Across slides (slides 2)") for x in rows)   # a selection, not a deck
    v = rv.verification(store, "observation", obs)
    assert v["stage"] == "verified" and v["ok"][0]["reviewer"] == "Gemini" and v["ok"][0]["ai"]
    assert rv.verification(store, "document", doc)["stage"] == "verified"
    assert store.db.execute("SELECT status FROM observation WHERE id = ?", (obs,)).fetchone()[0] == "verified"
    g = rv.get(store, r["id"])
    assert g["gemini"] == "done" and g["gemini_progress"]["done"] == 2
    assert [x["summary"] for x in g["gemini_report"]["slides"]] == ["Folie 1 solide", "Folie 2 solide"]
    sl = g["gemini_report"]["slides"][0]
    assert sl["ignored"] == 2 and g["gemini_report"]["deck"]["summary"] == "Roter Faden schlüssig"
    assert sl["numbers"] == {"sourced": 1, "derived": 1, "wrong": 0, "unverified": 1} and sl["missing_sources"] == 1
    nums = rv.slide_numbers(store, s[0]["exhibit_id"])
    assert [x["verdict"] for x in nums] == ["unverified", "derived", "sourced"] and nums[0]["by"] == "Gemini"
    assert rv.slide_evidence(store, s[0]["exhibit_id"])["numbers"][2]["evidence"] == "82% of dealers"
    # the inbox shows Gemini's points to the sessions
    box = rv.inbox(store, review=r["id"])
    assert box["items"] == 11 and box["slides"][0]["items"][0]["author"] == "Gemini"
    # a second run does nothing new
    n = len(calls)
    gm.run_review(store.path, r["id"])
    assert len(calls) == n
    assert store.db.execute("SELECT count(*) FROM feedback WHERE review_id = ?", (r["id"],)).fetchone()[0] == 11


def test_failed_slide_and_retry(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    gen, _ = fake_model(doc, obs, fail_pos=2)
    monkeypatch.setattr(gm, "generate", gen)
    r = rv.create(store, "acme-dealerships", slides=[x["exhibit_id"] for x in s], gemini=True, focus=["sources", "numbers"])
    out = gm.run_review(store.path, r["id"])
    assert out["state"] == "done" and out["done"] == 1 and "1 von 2" in out["error"]
    rep = rv.get(store, r["id"])["gemini_report"]
    assert "503" in rep["slides"][1]["error"] and rep["deck"] is None           # no 'logic' focus: no deck check
    gen2, calls2 = fake_model(doc, obs)
    monkeypatch.setattr(gm, "generate", gen2)
    out = gm.run_review(store.path, r["id"], retry=True)
    assert out["done"] == 2 and out["error"] is None and len(calls2) == 1        # only the failed slide again


def test_unreachable_source_is_named_and_everything_failed(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": False, "error": "HTTP 403"})
    seen = []

    def gen(model, parts, schema, max_tokens=16384, search=False):
        seen.append(" ".join(p.get("text", "") for p in parts))
        raise RuntimeError("Vertex AI 400: bad request")
    monkeypatch.setattr(gm, "generate", gen)
    r = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True)
    out = gm.run_review(store.path, r["id"])
    assert out["state"] == "failed" and "not retrievable (HTTP 403)" in seen[0] and "=== QUELLE" not in seen[0]
    with pytest.raises(LookupError):
        gm.run_review(store.path, rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], people=["Keller, Anna"])["id"])


def test_resume_queued(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    started = []
    monkeypatch.setattr(gm, "start", lambda db, rid, retry=False: started.append(rid))
    r1 = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True)
    rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], people=["Keller, Anna"])
    assert gm.resume(store.path) == [r1["id"]] and started == [r1["id"]]


def test_gemini_does_not_check_twice(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    gen, calls = fake_model(doc, obs)
    monkeypatch.setattr(gm, "generate", gen)
    r1 = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True, focus=["sources", "numbers"])
    gm.run_review(store.path, r1["id"])
    assert len(calls) == 1
    # same version in a new review: not checked again, the earlier result is carried over
    r2 = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True, focus=["sources", "numbers"])
    out = gm.run_review(store.path, r2["id"])
    sl = rv.get(store, r2["id"])["gemini_report"]["slides"][0]
    assert len(calls) == 1 and out["done"] == 1 and "bereits in Prüfung" in sl["skipped"] and sl["numbers"]["sourced"] == 1
    # a new version is checked, but the source and data point Gemini verified are not judged again, known numbers are passed on
    reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-0",
                     page("Dealers 0 neu").replace("<head>", f'<head><meta name="fundus-refs" content="document:{doc} observation:{obs}">', 1),
                     png=png_bytes(rgb=(250, 1, 1))))
    seen = []

    def gen2(model, parts, schema, max_tokens=16384, search=False):
        seen.append(" ".join(p.get("text", "") for p in parts))
        return {"summary": "ok", "comments": [], "number_checks": [], "claim_checks": [], "missing_sources": [],
                "source_checks": [{"source_id": doc, "verdict": "wrong", "note": "soll ignoriert werden"}],
                "datapoint_checks": [{"ref": f"observation:{obs}", "verdict": "wrong", "note": "soll ignoriert werden"}]}, {}
    monkeypatch.setattr(gm, "generate", gen2)
    r3 = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True, focus=["sources", "numbers"])
    gm.run_review(store.path, r3["id"])
    assert len(seen) == 1 and "FOLLOW-UP check" in seen[0]          # revised after a check with errors: only the fixes
    sl = rv.get(store, r3["id"])["gemini_report"]["slides"][0]
    assert sl["mode"] == "followup" and sl["clean"] is True
    assert rv.verification(store, "observation", obs)["stage"] == "verified"      # nothing in the follow-up disputed it


def test_gemini_only_review_is_closed_when_done(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    gen, _ = fake_model(doc, obs)
    monkeypatch.setattr(gm, "generate", gen)
    g = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True, focus=["sources"])
    p = rv.create(store, "acme-dealerships", slides=[s[1]["exhibit_id"]], gemini=True, people=["Keller, Anna"], focus=["sources"])
    gm.run_review(store.path, g["id"]); gm.run_review(store.path, p["id"])
    assert rv.get(store, g["id"])["status"] == "closed" and rv.get(store, g["id"])["closed_at"]
    assert rv.get(store, p["id"])["status"] == "draft"                 # people still to come


def test_errors_and_suggestions(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})

    def gen(model, parts, schema, max_tokens=16384, search=False):
        return {"verdict": "clean", "summary": "fine", "number_checks": [], "claim_checks": [], "missing_sources": [], "datapoint_checks": [],
                "source_checks": [{"source_id": doc, "verdict": "doubt", "note": "edition unclear"}],
                "comments": [{"type": "error", "text": "Claimed error without proof", "severity": "high", "category": "logic"},
                             {"type": "error", "evidence": "title 82%, chart 28%", "text": "Title contradicts chart", "severity": "high", "category": "logic"},
                             {"type": "suggestion", "text": "Shorter title", "severity": "low", "category": "language"}]}, {}
    monkeypatch.setattr(gm, "generate", gen)
    r = rv.create(store, "acme-dealerships", slides=[s[0]["exhibit_id"]], gemini=True, focus=["sources"])
    gm.run_review(store.path, r["id"])
    w = {x["text"]: x["weight"] for x in store.db.execute("SELECT text, coalesce(weight,'error') weight FROM feedback WHERE review_id = ?", (r["id"],))}
    assert w["Title contradicts chart"] == "error" and w["Claimed error without proof"] == "suggestion" and w["Shorter title"] == "suggestion"
    assert [v for k, v in w.items() if "fraglich" in k or "Quelle" in k] == ["suggestion"]          # a doubtful source does not block
    sl = rv.get(store, r["id"])["gemini_report"]["slides"][0]
    assert sl["errors"] == 1 and sl["suggestions"] == 2 and sl["clean"] is False      # one error with evidence
    st = rv.slide_status(store, ids=[s[0]["exhibit_id"]])[s[0]["exhibit_id"]]
    assert st["stage"] == "open" and st["feedback"]["open"] == 1 and st["feedback"]["suggestions"] == 3
    box = rv.inbox(store, review=r["id"], claim=True, by="x")
    items = box["slides"][0]["items"]
    assert items[0]["weight"] == "error" and {i["status"] for i in items if i["weight"] == "suggestion"} == {"open"}   # suggestions not claimed
    err = next(i for i in items if i["weight"] == "error")
    rv.resolve(store, [err["ref"]], "answered", "Title fixed in wording, chart is right")
    assert rv.slide_status(store, ids=[s[0]["exhibit_id"]])[s[0]["exhibit_id"]]["stage"] == "incorporated"   # suggestions left open: still level 2


def test_followup_checks_only_the_fixes(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    e = s[0]["exhibit_id"]
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    gen, _ = fake_model(doc, obs)
    monkeypatch.setattr(gm, "generate", gen)
    r1 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r1["id"])
    errs = [x for x in rv.inbox(store, exhibit=e)["slides"][0]["items"] if x["weight"] == "error"]
    assert errs
    reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-0", page("Dealers 0 fixed"), png=png_bytes(rgb=(240, 3, 3))))
    for x in errs:
        rv.resolve(store, [x["ref"]], "incorporated", "fixed")
    assert rv.slide_status(store, ids=[e])[e]["stage"] == "incorporated"
    refs = [x["ref"] for x in errs]
    seen = []

    def gen2(model, parts, schema, max_tokens=16384, search=False):
        seen.append((schema is gm.FOLLOWUP_SCHEMA, search, " ".join(p.get("text", "") for p in parts)))
        return {"verdict": "issues", "summary": "one not fixed",
                "fixes": [{"ref": refs[0], "fixed": False, "note": "still says 2025"}] + [{"ref": r, "fixed": True, "note": "ok"} for r in refs[1:]],
                "new_errors": [{"text": "New footnote number unsourced", "severity": "medium", "evidence": "footnote 12% has no source"}]}, {}
    monkeypatch.setattr(gm, "generate", gen2)
    r2 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r2["id"])
    assert seen[0][0] and not seen[0][1] and "Text diff" in seen[0][2] and refs[0] in seen[0][2]
    sl = rv.get(store, r2["id"])["gemini_report"]["slides"][0]
    assert sl["mode"] == "followup" and sl["not_fixed"] == 1 and sl["new_errors"] == 1 and not sl["clean"]
    reopened = store.db.execute("SELECT status, instruction FROM feedback WHERE id = ?", (int(refs[0][2:]),)).fetchone()
    assert reopened["status"] == "open" and "not fixed" in reopened["instruction"]
    assert rv.slide_status(store, ids=[e])[e]["stage"] == "open"
    # everything fixed in the next round: the slide is checked again (level 2, 'checked' instead of 'incorporated')
    reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-0", page("Dealers 0 fixed twice"), png=png_bytes(rgb=(241, 4, 4))))
    for x in rv.inbox(store, exhibit=e)["slides"][0]["items"]:
        if x["weight"] == "error":
            rv.resolve(store, [x["ref"]], "incorporated", "fixed now")
    monkeypatch.setattr(gm, "generate", lambda *a, **k: ({"verdict": "clean", "summary": "all fixed", "fixes": [], "new_errors": []}, {}))
    r3 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r3["id"])
    assert rv.slide_status(store, ids=[e])[e]["stage"] == "checked"


def test_answered_points_on_the_checked_version(store, projects_root, monkeypatch):
    doc, obs, s = build(store, projects_root)
    e = s[0]["exhibit_id"]
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    # a clean check with one suggestion; the session answers the suggestion later: still 'checked', not 'incorporated'
    monkeypatch.setattr(gm, "generate", lambda *a, **k: ({"verdict": "clean", "summary": "fine", "comments": [
        {"text": "Maybe add the year", "severity": "low", "type": "suggestion"}], "number_checks": [], "claim_checks": [],
        "missing_sources": [], "source_checks": [], "datapoint_checks": []}, {}))
    r1 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r1["id"])
    sug = [f"F-{r[0]}" for r in store.db.execute("SELECT id FROM feedback WHERE exhibit_id = ? AND weight = 'suggestion'", (e,))]
    assert sug
    for ref in sug:
        rv.resolve(store, [ref], "answered", "year is in the source line")
    assert rv.slide_status(store, ids=[e])[e]["stage"] == "checked"
    # an error answered without a new version: the next Gemini run checks the answer (follow-up) instead of skipping
    monkeypatch.setattr(gm, "generate", lambda *a, **k: ({"verdict": "issues", "summary": "x", "comments": [
        {"text": "Number 82% not in source", "severity": "high", "type": "error", "evidence": "source says 28%"}],
        "number_checks": [], "claim_checks": [], "missing_sources": [], "source_checks": [], "datapoint_checks": []}, {}))
    e = s[1]["exhibit_id"]          # a second slide, first checked with an error
    r2 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r2["id"])
    for x in rv.inbox(store, exhibit=e)["slides"][0]["items"]:
        rv.resolve(store, [x["ref"]], "answered", "the source says 82%, page 3")
    assert rv.slide_status(store, ids=[e])[e]["stage"] == "incorporated"
    seen = []

    def gen(model, parts, schema, max_tokens=16384, search=False):
        seen.append(schema is gm.FOLLOWUP_SCHEMA)
        return {"verdict": "clean", "summary": "answer holds", "fixes": [], "new_errors": []}, {}
    monkeypatch.setattr(gm, "generate", gen)
    r3 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r3["id"])
    assert seen == [True] and rv.slide_status(store, ids=[e])[e]["stage"] == "checked"


def test_verified_version_is_pinned(store, projects_root, monkeypatch):
    from fundus import exhibits as ex
    doc, obs, s = build(store, projects_root)
    e = s[0]["exhibit_id"]
    monkeypatch.setattr(gm, "fetch_source", lambda st, url: {"ok": True, "kind": "html", "text": "82% of dealers report using AI"})
    monkeypatch.setattr(gm, "generate", lambda *a, **k: ({"verdict": "clean", "summary": "fine", "comments": [], "number_checks": [],
                                                         "claim_checks": [], "missing_sources": [], "source_checks": [], "datapoint_checks": []}, {}))
    r = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r["id"])
    st = rv.slide_status(store, ids=[e])[e]
    assert st["stage"] == "checked" and st["verified"]["version"] == st["version"] and st["verified"]["is_current"]
    # developed further: the new version is not verified, the checked one stays offered
    reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-0", page("Dealers 0 reworked"), png=png_bytes(rgb=(120, 3, 3))))
    st2 = rv.slide_status(store, ids=[e])[e]
    assert st2["stage"] == "changed" and st2["verified"]["version"] == st["version"] and not st2["verified"]["is_current"]
    d = ex.get(store, e)
    assert d["verified_version"]["version"] == st["version"] and "newer" in d["verified_version"]["note"]
    assert d["verified_version"]["urls"] and [v for v in d["versions"] if v.get("verified") == "checked"]
    row = store.db.execute("SELECT x.*, NULL AS set_id FROM exhibit x WHERE id = ?", (e,)).fetchone()
    assert ex.summary_fields(store, row)["verified_version"]["version"] == st["version"]
    # checked again: the new version is pinned too, the latest pin is offered
    r2 = rv.create(store, "acme-dealerships", slides=[e], gemini=True, focus=["sources"])
    gm.run_review(store.path, r2["id"])
    st3 = rv.slide_status(store, ids=[e])[e]
    assert st3["verified"]["version"] == st2["version"] and st3["verified"]["is_current"]
    assert store.db.execute("SELECT count(*) FROM exhibit_verified WHERE exhibit_id = ?", (e,)).fetchone()[0] == 2
