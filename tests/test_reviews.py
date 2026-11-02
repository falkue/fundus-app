"""Reviews and the feedback inbox: reviewers, frozen slide sets, independent checks with verification stages, claiming and
resolving feedback with comments, change log, slide evidence (fundus-refs), MCP tools and portal API."""
import asyncio
import json

import pytest

from fundus import exhibits as ex
from fundus import reviews as rv
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


def evidence(store):
    doc = store.document("BFS Gesundheitskosten 2024", url="https://www.bfs.admin.ch/kosten-2024", publisher="BFS")
    obs = store.add("observation", {"metric": "Health spending Switzerland", "value": 91.5, "unit": "CHF bn", "period": "2022",
                                    "document_id": doc})
    store.db.commit()
    return doc, obs


def slides(store, root, n=3, refs=None):
    out = []
    for i in range(n):
        html = page(f"Slide {i}")
        if refs and i == 0:
            html = html.replace("<head>", f'<head><meta name="fundus-refs" content="{refs}">', 1)
        out.append(reg(store, write(root, f"Contoso/exhibits/slide-{i}", html, png=png_bytes(rgb=(10 * i, 20, 30)))))
    return out


def new_version(store, root, i, title):
    return reg(store, write(root, f"Contoso/exhibits/slide-{i}", page(title), png=png_bytes(rgb=(200, 10 * i + 1, 9))))


# ------------------------------------------------------------------ reviewers
def test_reviewer_names_reused_and_ai_apart(store):
    assert rv.parse_name("Keller, Anna") == ("Keller", "Anna")
    assert rv.parse_name("Anna Maria Keller") == ("Keller", "Anna Maria")
    with pytest.raises(ValueError):
        rv.parse_name("  ")
    a = rv.add_reviewer(store, "Keller, Anna", org="HSG")
    assert rv.add_reviewer(store, "keller, anna")["id"] == a["id"] and a["name"] == "Anna Keller" and a["label"] == "Keller, Anna"
    rv.ai_reviewer(store)
    store.db.commit()
    assert [r["name"] for r in rv.list_reviewers(store)] == ["Anna Keller"]
    assert {r["kind"] for r in rv.list_reviewers(store, include_ai=True)} == {"person", "ai"}
    assert rv.delete_reviewer(store, a["id"])["deleted"] and rv.list_reviewers(store) == []


# ------------------------------------------------------------------ reviews
def test_create_people_and_gemini_paths(store, projects_root):
    s = slides(store, projects_root, 3)
    ids = [x["exhibit_id"] for x in s]
    with pytest.raises(ValueError):
        rv.create(store, "contoso", slides=[ids[0]])                      # nobody checks
    r = rv.create(store, "contoso", slides=[ids[2], ids[0]], people=["Keller, Anna", "Meier, Ben"])
    assert r["scope"] == "selection" and r["status"] == "draft" and r["gemini"] is None and r["slides"] == 2
    assert [x["id"] for x in r["slides_list"]] == [ids[2], ids[0]] and all(x["version"] == 1 for x in r["slides_list"])
    links = [p["link"] for p in r["reviewers"]]
    assert len(links) == 2 and len(set(links)) == 2 and all(u.startswith(rv.REVIEW_URL + "/r/") for u in links)
    assert r["expires_at"] and "note" in r
    g = rv.create(store, "contoso", set_ref="main", gemini=True)          # AI only: no links, nothing to publish
    assert g["status"] == "open" and g["gemini"] == "queued" and g["reviewers"] == [] and g["slides"] == 3
    assert g["focus"] == ["sources", "numbers", "logic"] and g["expires_at"] is None
    # revoke and re-invite: a new link
    old = r["reviewers"][0]
    r2 = rv.revoke(store, r["id"], old["label"])
    assert next(p for p in r2["reviewers"] if p["id"] == old["id"])["link"] is None
    r3 = rv.invite(store, r["id"], [old["label"]])
    assert next(p for p in r3["reviewers"] if p["id"] == old["id"])["link"] not in (None, old["link"])
    assert [x["id"] for x in rv.list_reviews(store, "contoso")] == [g["id"], r["id"]]
    closed = rv.close(store, r["id"])
    assert closed["status"] == "closed" and all(p["link"] for p in closed["reviewers"])   # links stay, read-only
    with pytest.raises(ValueError):
        rv.invite(store, r["id"], ["Neu, Nina"])
    assert all(p["link"] is None for p in rv.purge(store, r["id"])["reviewers"])


def test_deck_scope_and_foreign_slide(store, projects_root):
    from fundus import decks
    s = slides(store, projects_root, 3)
    decks.set_sequence(store, "contoso", "deck-v1", [str(s[2]["exhibit_id"]), str(s[1]["exhibit_id"])], title="Deck v1")
    r = rv.create(store, "contoso", deck="deck-v1", gemini=True)
    assert r["scope"] == "deck" and [x["id"] for x in r["slides_list"]] == [s[2]["exhibit_id"], s[1]["exhibit_id"]]
    assert r["title"].startswith("Deck v1")
    other = reg(store, write(projects_root, "Northwind/exhibits/gov", page("Gov"), png=png_bytes(rgb=(1, 2, 3))))
    with pytest.raises(ValueError):
        rv.create(store, "contoso", slides=[other["exhibit_id"]], gemini=True)


# ------------------------------------------------------------------ evidence and checks
def test_fundus_refs_link_sources_and_data(store, projects_root):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 1, refs=f"observation:{obs} document:{doc} observation:99999")
    ev = rv.slide_evidence(store, s[0]["exhibit_id"])
    assert [x["id"] for x in ev["sources"]] == [doc] and ev["sources"][0]["url"].startswith("https://www.bfs")
    assert [x["id"] for x in ev["data"]] == [obs] and ev["data"][0]["verification"]["stage"] == "unchecked"
    # manual links survive and can be removed
    s2 = slides(store, projects_root, 2)[1]
    out = rv.link_items(store, s2["exhibit_id"], [f"observation:{obs}"])
    assert out["linked"] == [f"observation:{obs}"] and out["data"][0]["manual"]
    assert rv.link_items(store, s2["exhibit_id"], [f"observation:{obs}"], remove=True)["data"] == []
    with pytest.raises(LookupError):
        rv.link_items(store, s2["exhibit_id"], ["observation:99999"])
    with pytest.raises(ValueError):
        rv.link_items(store, s2["exhibit_id"], ["nonsense"])


def test_verification_stages_people_and_ai_count_alike(store, projects_root):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 1, refs=f"observation:{obs} document:{doc}")
    eid = s[0]["exhibit_id"]
    rv.add_reviewer(store, "Keller, Anna")
    rv.add_reviewer(store, "Meier, Ben")
    a = rv.add(store, eid, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("observation", obs), verdict="ok")
    assert a["id"] is None and a["verification"]["stage"] == "verified"            # an ok check opens no point
    assert store.db.execute("SELECT status FROM observation WHERE id = ?", (obs,)).fetchone()[0] == "verified"
    g = rv.add(store, eid, "", source="ai", kind="check", target=("observation", obs), verdict="ok", evidence="Tabelle 3, S. 14")
    assert g["verification"]["stage"] == "confirmed" and g["verification"]["confirmations"] == 2
    assert {x["reviewer"] for x in g["verification"]["ok"]} == {"Anna Keller", "Gemini"}
    # a doubt on the source opens a point; a wrong verdict disputes the data point
    d = rv.add(store, eid, "", source="person", reviewer_ref="Meier, Ben", kind="check", target=("document", doc), verdict="doubt",
               note="Link führt zur Ausgabe 2021, besser: https://www.bfs.admin.ch/kosten-2022")
    assert d["ref"].startswith("F-") and d["verification"]["stage"] == "unchecked"
    w = rv.add(store, eid, "", source="person", reviewer_ref="Meier, Ben", kind="check", target=("observation", obs), verdict="wrong")
    assert w["verification"]["stage"] == "disputed"
    assert store.db.execute("SELECT status FROM observation WHERE id = ?", (obs,)).fetchone()[0] == "disputed"
    # the latest verdict per reviewer counts
    rv.add(store, eid, "", source="person", reviewer_ref="Meier, Ben", kind="check", target=("observation", obs), verdict="ok")
    assert rv.verification(store, "observation", obs)["stage"] == "confirmed"
    with pytest.raises(LookupError):
        rv.add(store, eid, "x", source="person", reviewer_ref="Unbekannt, Udo")    # people come from the review, not ad hoc
    with pytest.raises(ValueError):
        rv.add(store, eid, "", kind="check", target=("observation", obs), verdict="maybe")


# ------------------------------------------------------------------ inbox, claim, resolve, change log
def test_inbox_claim_resolve_and_changelog(store, projects_root):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 2, refs=f"document:{doc}")
    e0, e1 = s[0]["exhibit_id"], s[1]["exhibit_id"]
    r = rv.create(store, "contoso", slides=[e0, e1], people=["Keller, Anna"], gemini=True)
    f1 = rv.add(store, e0, "2025 ist Prognose, bitte kennzeichnen", source="person", reviewer_ref="Keller, Anna", review_id=r["id"],
                severity="high", shape={"tool": "arrow", "points": [[0.4, 0.3], [0.5, 0.4]]})
    f2 = rv.add(store, e0, "", source="ai", review_id=r["id"], kind="check", target=("document", doc), verdict="doubt",
                note="Ausgabe 2021 statt 2024")
    rv.add(store, e0, "", source="person", reviewer_ref="Keller, Anna", review_id=r["id"], kind="check", target=("document", doc),
           verdict="ok")
    f3 = rv.add(store, e1, "Titel über zwei Zeilen", source="ai", review_id=r["id"], severity="low")
    f4 = rv.add(store, e1, "Bitte Quelle ergänzen")                                   # Falk's own point
    box = rv.inbox(store, project="contoso", review=r["id"], claim=True, by="contoso-session")
    assert [x["exhibit"] for x in box["slides"]] == [e0, e1] and box["items"] == 3 and "how" in box
    first = box["slides"][0]
    assert first["position"] == 1 and first["png"].endswith(".png") and first["current_version"] == 1
    assert [i["ref"] for i in first["items"]] == [f1["ref"], f2["ref"]]                # high severity first, then the check
    check = first["items"][1]
    assert check["target"]["kind"] == "document" and "Anna Keller" in check["conflict"] and check["author"] == "Gemini"
    assert all(i["status"] == "in_progress" for x in box["slides"] for i in x["items"])
    # another session sees the lock; Falk's own point (no review) is in the project inbox
    other = rv.inbox(store, project="contoso", claim=True, by="other")
    locked = [i for x in other["slides"] for i in x["items"] if i.get("locked")]
    assert len(locked) == 3 and any(i["ref"] == f4["ref"] and i["author"] == "Falk (Portal)" for x in other["slides"] for i in x["items"])
    # incorporated needs a newer version and a comment
    with pytest.raises(ValueError):
        rv.resolve(store, [f1["ref"]], "eingearbeitet", "Balken 2025 schraffiert")
    with pytest.raises(ValueError):
        rv.resolve(store, [f1["ref"]], "answered", "")
    with pytest.raises(ValueError):
        rv.resolve(store, [f1["ref"]], "maybe", "irgendwas")
    # while Gemini runs or Anna still reviews, the slide is frozen: a re-render is refused
    assert new_version(store, projects_root, 0, "Slide 0 – zu früh")["status"] == "locked"
    store.db.execute("UPDATE review SET gemini_state = 'done', status = 'open' WHERE id = ?", (r["id"],))
    store.db.execute("UPDATE review_access SET finished_at = '2026-10-27 08:00:00' WHERE review_id = ?", (r["id"],))
    store.db.commit()
    new_version(store, projects_root, 0, "Slide 0 – 2025 als Prognose")
    out = rv.resolve(store, [f1["ref"]], "eingearbeitet", "Balken 2025 schraffiert, Fussnote ergänzt", by="contoso-session")
    assert out == {"resolved": [f1["ref"]], "outcome": "incorporated", "versions": [2]}
    assert rv.resolve(store, [f3["id"]], "zurückgewiesen", "Zwei Titelzeilen entsprechen dem Deck-Stil")["outcome"] == "rejected"
    assert rv.resolve(store, f2["ref"], "decision", "Reviewer und Gemini widersprechen sich")["outcome"] == "decision"
    log = rv.changelog(store, e0)
    assert [(x["ref"], x["outcome"], x["from_version"], x["to_version"]) for x in log] == [(f1["ref"], "incorporated", 1, 2)]
    assert log[0]["author"] == "Anna Keller"
    assert [x["ref"] for x in rv.changelog(store, e1)] == [f3["ref"]]
    dec = rv.inbox(store, status="decision")
    assert [i["ref"] for x in dec["slides"] for i in x["items"]] == [f2["ref"]]
    again = rv.reopen(store, f2["ref"], "Ausgabe 2024 behalten, Link auf Tabelle 3 präzisieren")
    assert again["status"] == "open" and again["instruction"].startswith("Ausgabe 2024")
    counts = rv.open_counts(store, "contoso")
    assert counts["exhibits"][e0]["open"] == 1 and counts["exhibits"][e1]["open"] == 1 and counts["decisions"] == 0
    assert rv.get(store, r["id"])["feedback"] == {"open": 1, "suggestions": 0, "decision": 0, "done": 2, "total": 3}
    # repeated sync of the same external point is idempotent
    a1 = rv.add(store, e1, "Extern", source="ai", external_id="fs-1")
    assert rv.add(store, e1, "Extern", source="ai", external_id="fs-1") == {"id": a1["id"], "ref": a1["ref"], "duplicate": True}


def test_stale_claim_can_be_taken_over(store, projects_root):
    s = slides(store, projects_root, 1)
    f = rv.add(store, s[0]["exhibit_id"], "Zahl prüfen")
    rv.inbox(store, claim=True, by="a")
    store.db.execute("UPDATE feedback SET claimed_at = datetime('now', '-3 hours') WHERE id = ?", (f["id"],))
    store.db.commit()
    box = rv.inbox(store, claim=True, by="b")
    item = box["slides"][0]["items"][0]
    assert item["claimed_by"] == "b" and "locked" not in item


def test_link_check_records(store, projects_root, monkeypatch):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 1, refs=f"document:{doc}")
    r = rv.create(store, "contoso", slides=[s[0]["exhibit_id"]], gemini=True)
    assert rv.review_urls(store, r["id"]) == ["https://www.bfs.admin.ch/kosten-2024"]
    monkeypatch.setattr(rv, "check_url", lambda u, timeout=12: {"ok": 0, "status": 404, "final_url": None, "error": "Not Found"})
    assert rv.check_links(store, rv.review_urls(store, r["id"])) == {"checked": 1, "broken": 1, "skipped_fresh": 0}
    assert rv.check_links(store, rv.review_urls(store, r["id"]))["skipped_fresh"] == 1
    link = rv.slide_evidence(store, s[0]["exhibit_id"])["sources"][0]["link"]
    assert link["ok"] is False and link["status"] == 404


# ------------------------------------------------------------------ MCP tools and portal API
def test_mcp_tools(store, projects_root):
    from fundus import mcp_server as m
    s = slides(store, projects_root, 2)
    m.configure(store.path)
    try:
        out = json.loads(m.fundus_review("share", project="contoso", slides=[str(s[0]["exhibit_id"])], people=["Keller, Anna"]))
        assert out["reviewers"][0]["name"] == "Anna Keller"
        assert "error" in json.loads(m.fundus_review("share", project="contoso", slides=[str(s[0]["exhibit_id"])]))
        g = json.loads(m.fundus_review("gemini", project="contoso", set="main"))
        assert g["gemini"] == "queued" and g["reviewers"] == []
        rv.add(store, s[1]["exhibit_id"], "Achse beschriften", source="ai", review_id=g["id"])
        box = json.loads(m.fundus_feedback(project="contoso", by="contoso"))
        ref = box["slides"][0]["items"][0]["ref"]
        assert box["slides"][0]["items"][0]["status"] == "in_progress"
        assert "error" in json.loads(m.fundus_feedback_resolve([ref], "incorporated", "Achse beschriftet"))
        assert json.loads(m.fundus_feedback_resolve([ref], "answered", "Achse ist im Titel benannt"))["outcome"] == "answered"
        assert json.loads(m.fundus_review("people"))["reviewers"]
        ex_out = json.loads(m.fundus_exhibit(str(s[1]["exhibit_id"]), script=False))
        assert ex_out["changelog"][0]["outcome"] == "answered"
    finally:
        m.configure(m.DEFAULT_DB)


def call(m, fn, method="GET", path="/", body=None, query=b"", path_params=None, origin="http://127.0.0.1:8010"):
    from starlette.requests import Request
    raw = json.dumps(body).encode() if body is not None else b""
    hdr = [(b"host", b"127.0.0.1:8010"), (b"content-type", b"application/json")] + ([(b"origin", origin.encode())] if origin else [])
    scope = {"type": "http", "method": method, "path": path, "path_params": path_params or {}, "query_string": query, "headers": hdr}
    sent = []

    async def receive():
        if sent:
            return {"type": "http.disconnect"}
        sent.append(1)
        return {"type": "http.request", "body": raw, "more_body": False}
    r = asyncio.run(fn(Request(scope, receive)))
    return r.status_code, json.loads(r.body)


def test_portal_api(store, projects_root, monkeypatch):
    from fundus import mcp_server as m
    doc, obs = evidence(store)
    s = slides(store, projects_root, 2, refs=f"document:{doc}")
    monkeypatch.setattr(m, "_check_links_later", lambda rid: None)
    m.configure(store.path)
    try:
        code, d = call(m, m.reviewers_api, "POST", "/api/reviewers", {"name": "Keller, Anna", "org": "HSG"})
        assert code == 200 and d["org"] == "HSG"
        code, d = call(m, m.reviews_api, "POST", "/api/reviews", {"project": "contoso", "slides": [s[0]["exhibit_id"], s[1]["exhibit_id"]],
                                                                  "people": ["Keller, Anna", "Meier, Ben"], "gemini": True})
        assert code == 200 and len(d["reviewers"]) == 2 and d["gemini"] == "queued"
        rid = d["id"]
        assert call(m, m.reviews_api, "POST", "/api/reviews", {"project": "contoso", "slides": [s[0]["exhibit_id"]]})[0] == 400
        assert call(m, m.reviews_api, "POST", "/api/reviews", {"project": "contoso", "people": "x"})[0] == 400
        code, d = call(m, m.reviews_api, "GET", "/api/reviews", query=b"project=contoso")
        assert code == 200 and d["reviews"][0]["id"] == rid and {r["name"] for r in d["reviewers"]} == {"Anna Keller", "Ben Meier"}
        code, d = call(m, m.reviews_api, "POST", f"/api/reviews/{rid}", {"action": "revoke", "person": "Meier, Ben"}, path_params={"rid": rid})
        assert code == 200 and sum(1 for p in d["reviewers"] if p["link"]) == 1
        assert call(m, m.reviews_api, "GET", "/api/reviews/999", path_params={"rid": 999})[0] == 404
        code, d = call(m, m.feedback_api, "POST", "/api/feedback", {"exhibit": s[0]["exhibit_id"], "text": "Quelle fehlt", "severity": "high"})
        assert code == 200 and d["ref"].startswith("F-")
        assert call(m, m.feedback_api, "POST", "/api/feedback", {"exhibit": s[0]["exhibit_id"], "text": " "})[0] == 400
        assert call(m, m.feedback_api, "POST", "/api/feedback", {"exhibit": s[0]["exhibit_id"], "text": "x"}, origin="http://evil.test")[0] == 403
        code, d = call(m, m.feedback_api, "GET", "/api/feedback", query=b"project=contoso")
        assert code == 200 and d["slides"][0]["items"][0]["status"] == "open"          # the portal never claims
        code, d = call(m, m.feedback_api, "GET", "/api/feedback/counts", query=b"project=contoso")
        assert code == 200 and d["open"] == 1
        code, d = call(m, m.slide_review_api, "GET", f"/api/slide-review/{s[0]['exhibit_id']}", path_params={"ref": str(s[0]["exhibit_id"])})
        assert code == 200 and d["sources"][0]["id"] == doc and d["feedback"][0]["items"][0]["text"] == "Quelle fehlt"
        fid = d["feedback"][0]["items"][0]["id"]
        rv.resolve(store, [fid], "decision", "Welche Quelle?")
        code, d = call(m, m.feedback_api, "POST", "/api/feedback", {"id": fid, "action": "reopen", "instruction": "BFS nehmen"})
        assert code == 200 and d["status"] == "open" and d["instruction"] == "BFS nehmen"
    finally:
        m.configure(m.DEFAULT_DB)


def test_cli_review(store, projects_root, capsys):
    from fundus import cli
    s = slides(store, projects_root, 1)
    db = ["--db", str(store.path), "review"]
    assert cli.main(db + ["gemini", "contoso", "--no-run", "--slides", str(s[0]["exhibit_id"])]) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(db + ["share", "contoso", "--set", "main", "--person", "Keller, Anna"]) == 0
    assert json.loads(capsys.readouterr().out)["reviewers"][0]["label"] == "Keller, Anna"
    rv.add(store, s[0]["exhibit_id"], "Farbe prüfen", source="ai", review_id=rid)
    assert cli.main(db + ["feedback", "--project", "contoso"]) == 0
    ref = json.loads(capsys.readouterr().out)["slides"][0]["items"][0]["ref"]
    assert cli.main(db + ["resolve", ref, "--outcome", "rejected", "--note", "Farbe ist Markenfarbe"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "rejected"


def test_gemini_model_choice(store, projects_root):
    from fundus import gemini as gm
    s = slides(store, projects_root, 1)
    g = rv.create(store, "contoso", slides=[s[0]["exhibit_id"]], gemini=True)
    assert g["gemini_model"] == gm.DEFAULT == "gemini-4-argon"
    g2 = rv.create(store, "contoso", slides=[s[0]["exhibit_id"]], gemini=True, model="gemini-3.8-flash")
    assert g2["gemini_model"] == "gemini-3.8-flash"
    with pytest.raises(ValueError):
        rv.create(store, "contoso", slides=[s[0]["exhibit_id"]], gemini=True, model="gpt-5")
    p = rv.create(store, "contoso", slides=[s[0]["exhibit_id"]], people=["Keller, Anna"], model="gemini-3.8-flash")
    assert p["gemini_model"] is None                                   # no AI check, no model
    assert gm.models()["location"] == "global" and gm.models()["models"][0]["id"] == gm.DEFAULT


def test_review_table_gets_model_column(tmp_path):
    import sqlite3
    from fundus import Store
    path = tmp_path / "old.sqlite"
    Store(path).close()
    db = sqlite3.connect(path)
    db.executescript("DROP TABLE review_slide; DROP TABLE review_access; DROP TABLE review; "
                     "CREATE TABLE review (id INTEGER PRIMARY KEY, project TEXT, title TEXT, scope TEXT, set_id INTEGER, sequence_id INTEGER, "
                     "round INTEGER DEFAULT 1, parent_id INTEGER, status TEXT DEFAULT 'draft', gemini_state TEXT, focus TEXT, expires_at TEXT, "
                     "created_by TEXT DEFAULT 'portal', created_at TEXT DEFAULT CURRENT_TIMESTAMP, published_at TEXT, closed_at TEXT);")
    db.close()
    st = Store(path)
    assert "gemini_model" in [r[1] for r in st.db.execute("PRAGMA table_info(review)")]
    st.close()


def test_deep_link_and_evidence(store, projects_root):
    assert rv.deep_link("https://x.org/r.pdf", "Tabelle 3, S. 14") == "https://x.org/r.pdf#page=14"
    assert rv.deep_link("https://x.org/r.pdf", None, "p. 7") == "https://x.org/r.pdf#page=7"
    assert rv.deep_link("https://x.org/p#top", "„82% of dealers report using AI in some form,“ …") == \
        "https://x.org/p#:~:text=82%25%20of%20dealers%20report%20using%20AI%20in%20some%20form"
    assert rv.deep_link("https://x.org/p", "zu kurz") is None and rv.deep_link(None, "x y z") is None
    doc, obs = evidence(store)
    store.db.execute("UPDATE observation SET locator = 'Tabelle 3', quote_context = 'Health spending reached CHF 91.5 billion in 2022' WHERE id = ?", (obs,))
    store.db.commit()
    s = slides(store, projects_root, 1, refs=f"observation:{obs} document:{doc}")
    rv.add(store, s[0]["exhibit_id"], "", source="ai", kind="check", target=("document", doc), verdict="ok",
           evidence="Gesundheitsausgaben 2022: 91,5 Mrd. Franken insgesamt")
    ev = rv.slide_evidence(store, s[0]["exhibit_id"])
    assert ev["sources"][0]["evidence"]["by"] == "Gemini" and "#:~:text=Gesundheitsausgaben" in ev["sources"][0]["deep_url"]
    d = ev["data"][0]
    assert d["locator"] == "Tabelle 3" and d["quote"].startswith("Health spending") and "#:~:text=Health%20spending" in d["deep_url"]


def test_fundus_sources_in_the_file_become_linked_documents(store, projects_root):
    import json as _j
    known = store.document("Cox AI tracker", url="https://www.coxautoinc.com/tracker", publisher="Cox Automotive")
    store.db.commit()
    srcs = [{"title": "Cox AI tracker (Seite)", "url": "https://coxautoinc.com/tracker/"},            # known URL: reused, not renamed
            {"title": "AUTOHAUS Panel Aug 2026", "url": "https://www.autohaus.de/panel-2026", "publisher": "AUTOHAUS", "published": "2026-09"},
            {"title": "Interne Händlerbefragung"},                                                     # no URL, no match: not linked
            {"title": "Digital 2019-2024", "url": "https://datareportal.com/r/digital-2024 (and https://datareportal.com/r/digital-2019)"}]
    html = page("Dealers").replace("<head>", '<head><script type="application/json" id="fundus-sources">' + _j.dumps(srcs) + "</script>", 1)
    s = reg(store, write(projects_root, "Acme-Dealerships/exhibits/kpi-9", html, png=png_bytes(rgb=(1, 99, 1))))
    ev = rv.slide_evidence(store, s["exhibit_id"])
    ids = {x["id"] for x in ev["sources"]}
    new = store.db.execute("SELECT id, title, published FROM document WHERE url LIKE '%autohaus.de%'").fetchone()
    assert known in ids and new["id"] in ids and new["title"] == "AUTOHAUS Panel Aug 2026" and new["published"] == "2026-09"
    assert store.db.execute("SELECT title FROM document WHERE id = ?", (known,)).fetchone()[0] == "Cox AI tracker"
    dr = {r[0] for r in store.db.execute("SELECT url FROM document WHERE url LIKE '%datareportal%'")}
    assert dr == {"https://datareportal.com/r/digital-2024", "https://datareportal.com/r/digital-2019"}   # one document per URL


def test_slide_status_stages(store, projects_root):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 2, refs=f"document:{doc}")
    e0, e1 = s[0]["exhibit_id"], s[1]["exhibit_id"]
    e1 = reg(store, write(projects_root, "Contoso/exhibits/slide-1", page("82% der Händler, 41% sehen Nutzen"), png=png_bytes(rgb=(3, 3, 3))))["exhibit_id"]
    st = rv.slide_status(store, "contoso")
    assert st[e0]["stage"] == "unchecked" and st[e0]["level"] == 0 and st[e0]["sources"] == 1
    assert st[e1]["stage"] == "no_sources"                              # numbers on the slide, no source at all
    f = rv.add(store, e0, "Zahl prüfen", source="ai")
    assert rv.slide_status(store, ids=[e0])[e0]["stage"] == "open"
    reg(store, write(projects_root, "Contoso/exhibits/slide-0", page("Slide 0 korrigiert").replace(
        "<head>", f'<head><meta name="fundus-refs" content="document:{doc}">', 1), png=png_bytes(rgb=(7, 7, 7))))
    rv.resolve(store, [f["ref"]], "incorporated", "Zahl korrigiert")
    st = rv.slide_status(store, ids=[e0])[e0]
    assert st["stage"] == "incorporated" and st["level"] == 2 and st["label"] == "eingearbeitet"
    rv.add_reviewer(store, "Keller, Anna")
    rv.add(store, e0, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("document", doc), verdict="ok")
    st = rv.slide_status(store, ids=[e0])[e0]
    assert st["stage"] == "confirmed" and st["level"] == 3 and "Anna Keller" in st["reviewers"]
    # a clean check, then a new render without feedback: no longer checked
    v1 = store.db.execute("SELECT current_version FROM exhibit WHERE id = ?", (e1,)).fetchone()[0]
    store.db.execute("INSERT INTO exhibit_number(exhibit_id, version, number, verdict) VALUES (?, ?, '91.5', 'sourced')", (e1, v1))
    store.db.commit()
    assert rv.slide_status(store, ids=[e1])[e1]["stage"] == "checked"
    new_version(store, projects_root, 1, "Slide 1 neu")
    assert rv.slide_status(store, ids=[e1])[e1]["stage"] == "changed"
    hit = ex.summary_fields(store, store.db.execute("SELECT * FROM exhibit WHERE id = ?", (e0,)).fetchone())
    assert hit["review_stage"] == "confirmed" and hit["confidence"] == 3


def test_slide_marked_as_needing_no_sources(store, projects_root):
    html = page("Agenda 09:00–17:00, 90 min").replace("<head>", '<head><meta name="fundus-sources" content="none: Agenda">', 1)
    e = reg(store, write(projects_root, "Contoso/exhibits/agenda", html, png=png_bytes(rgb=(5, 50, 5))))["exhibit_id"]
    st = rv.slide_status(store, ids=[e])[e]
    assert st["stage"] == "not_needed" and st["level"] == 2 and st["no_sources"] == "Agenda" and st["label"] == "keine Quellen nötig"


def test_verdict_counts_for_its_slide_and_source_itself_everywhere(store, projects_root):
    doc, obs = evidence(store)
    s = slides(store, projects_root, 2, refs=f"document:{doc}")
    s1 = reg(store, write(projects_root, "Contoso/exhibits/slide-1", page("Slide 1 b").replace(
        "<head>", f'<head><meta name="fundus-refs" content="document:{doc}">', 1), png=png_bytes(rgb=(4, 4, 4))))
    e0, e1 = s[0]["exhibit_id"], s1["exhibit_id"]
    rv.add_reviewer(store, "Keller, Anna")
    rv.add(store, e0, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("document", doc), verdict="ok")
    ev0, ev1 = rv.slide_evidence(store, e0)["sources"][0], rv.slide_evidence(store, e1)["sources"][0]
    assert ev0["verification"]["stage"] == "verified" and ev1["verification"]["stage"] == "unchecked"   # only the slide it was given on
    assert rv.slide_status(store, ids=[e1])[e1]["confirmations"] == 0
    # "the source itself is not genuine" disputes every use
    rv.add(store, e1, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("document", doc), verdict="wrong",
           note="falsche Ausgabe", aspect="source")
    assert rv.slide_evidence(store, e0)["sources"][0]["verification"]["stage"] == "disputed"
    assert rv.verification(store, "document", doc)["source"]["stage"] == "disputed"
    with pytest.raises(ValueError):
        rv.record_check(store, "observation", obs, rv.reviewer(store, "Keller, Anna"), "ok", aspect="source")


def test_people_confirm_single_numbers(store, projects_root):
    s = slides(store, projects_root, 1)
    e = s[0]["exhibit_id"]
    store.db.execute("INSERT INTO exhibit_number(exhibit_id, version, number, claim, verdict) VALUES (?, 1, '82 %', 'US dealers', 'sourced')", (e,))
    store.db.commit()
    nid = rv.slide_numbers(store, e)[0]["id"]
    rv.add_reviewer(store, "Keller, Anna")
    out = rv.add(store, e, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("number", nid), verdict="ok")
    assert out["verification"]["stage"] == "verified"
    n = rv.slide_numbers(store, e)[0]
    assert n["people"]["stage"] == "verified" and n["people"]["ok"][0]["reviewer"] == "Anna Keller"
    w = rv.add(store, e, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("number", nid), verdict="wrong", note="81 %")
    assert w["ref"] and rv.inbox(store, exhibit=e)["slides"][0]["items"][0]["target"]["label"].startswith("82 %")


def test_unmodified_copy_in_another_set_shares_the_stage(store, projects_root):
    doc, _ = evidence(store)
    html = page("Kosten 91,5 Mrd. CHF, +3,4% p.a.").replace("<head>", f'<head><meta name="fundus-refs" content="document:{doc}">', 1)
    main = reg(store, write(projects_root, "Contoso/exhibits/cost", html, png=png_bytes(rgb=(1, 2, 3))))["exhibit_id"]
    ex.create_set(store, "contoso", "Präsentation", folders=["praes"])
    copy = reg(store, write(projects_root, "Contoso/praes/cost", html, png=png_bytes(rgb=(1, 2, 3))))["exhibit_id"]
    assert copy != main
    st = rv.slide_status(store, ids=[main, copy])
    assert st[copy]["stage"] == st[main]["stage"] == "unchecked"
    assert st[copy]["shared_with"][0]["exhibit"] == main and st[main]["shared_with"][0]["exhibit"] == copy
    # a check on the original counts for the copy, and a check on the copy for the original
    f = rv.add(store, main, "Quelle fehlt für +3,4%", source="ai")
    st = rv.slide_status(store, ids=[main, copy])
    assert st[copy]["stage"] == st[main]["stage"] == "open" and st[copy]["feedback"]["open"] == 1
    rv.resolve(store, [f["ref"]], "answered", "steht in der Quelle")
    st = rv.slide_status(store, ids=[main, copy])
    assert st[copy]["stage"] == st[main]["stage"] == "incorporated"
    rv.add_reviewer(store, "Keller, Anna")
    rv.add(store, copy, "", source="person", reviewer_ref="Keller, Anna", kind="check", target=("document", doc), verdict="ok")
    st = rv.slide_status(store, ids=[main, copy])
    assert st[copy]["stage"] == st[main]["stage"] == "confirmed" and "Anna Keller" in st[main]["reviewers"]
    # the copy is changed: it stands on its own, the original keeps its stage
    g = rv.add(store, copy, "Achse beschriften", source="ai")
    reg(store, write(projects_root, "Contoso/praes/cost", page("Kosten 91,5 Mrd. CHF (neu)"), png=png_bytes(rgb=(4, 5, 6))))
    rv.resolve(store, [g["ref"]], "incorporated", "Achse beschriftet")
    st = rv.slide_status(store, ids=[main, copy])
    assert st[copy]["stage"] == "confirmed" and st[copy]["version"] == 2       # its own fix, the confirmation came with it
    assert st[main]["stage"] == "open" and st[main]["feedback"]["open"] == 1  # fixed only in the copy: still in the original
    assert st[main]["shared_with"][0]["is_current"] is False
    # a new version of the original: nothing of it reaches the copy
    h = rv.add(store, main, "Titel zu lang", source="ai")
    st2 = rv.slide_status(store, ids=[copy])[copy]
    assert st2["feedback"] == st[copy]["feedback"]
    reg(store, write(projects_root, "Contoso/exhibits/cost", page("Kosten (Original neu)"), png=png_bytes(rgb=(7, 8, 9))))
    rv.resolve(store, [h["ref"]], "incorporated", "gekürzt")
    assert rv.slide_status(store, ids=[copy])[copy]["stage"] == "confirmed"
    assert rv.slide_status(store, ids=[main])[main]["stage"] in ("incorporated", "confirmed")
