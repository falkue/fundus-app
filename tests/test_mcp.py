import asyncio
import json

import pytest

from fundus import mcp_server as m

DOC = {"title": "Dealer AI survey 2024", "url": "https://cdk.example/2024?utm_source=x", "publisher": "CDK Global",
       "publisher_type": "vendor", "kind": "survey_report", "published": "2024-11"}
TAGS = {"industry": ["automotive-retail"], "topic": ["ai-adoption"], "region": ["us"]}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "mcp.sqlite"
    m.configure(path)
    yield path
    m.configure(m.DEFAULT_DB)


def call(fn, *a, **kw):
    return json.loads(fn(*a, **kw))


def test_add_observation_with_series_and_duplicates(db):
    r = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 55, "unit": "%", "period": "2024",
                                           "status": "verified"},
             document=DOC, tags=TAGS, project="acme",
             series={"id": "cdk-dealer-ai", "name": "Share of dealers using AI", "unit": "%", "publisher": "CDK Global",
                     "frequency": "annual"})
    assert r["created"] and r["status"] == "unverified" and r["series_id"] == "cdk-dealer-ai"
    assert "warnings" not in r
    # same figure again → merged, no duplicate
    r2 = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 55, "unit": "%", "period": "2024",
                                            "series_id": "cdk-dealer-ai"}, document=DOC, tags=TAGS, project="acme")
    assert r2["id"] == r["id"] and not r2["created"]
    # a near match is reported as possible duplicate
    r3 = call(m.fundus_add, "observation", {"metric": "Dealers using AI tools", "value": 57, "unit": "%",
                                            "period": "2024"}, document=DOC, tags=TAGS)
    assert r3["created"] and r["id"] in [d["id"] for d in r3["possible_duplicates"]]
    s = call(m.fundus_series, "cdk-dealer-ai")
    assert s["observations"][0]["source"]["publisher"] == "CDK Global"
    g = call(m.fundus_get, "observation", str(r["id"]))
    assert g["origin"][0]["project"] == "acme" and g["tags"]["project"] == ["acme"]
    assert g["document"]["publisher"]["type"] == "vendor"


def test_add_requires_tags_and_note_for_verified(db):
    e = call(m.fundus_add, "quote", {"text": "AI will not replace the advisor."}, document=DOC, tags={"industry": ["retail"]})
    assert "topic" in e["error"]
    e = call(m.fundus_add, "quote", {"text": "x"}, document=DOC, tags={"industry": ["space"], "topic": ["ai"]})
    assert "unknown vocabulary" in e["error"]
    e = call(m.fundus_add, "quote", {"text": "x"}, document=DOC, tags=TAGS, verified=True)
    assert "verification_note" in e["error"]
    ok = call(m.fundus_add, "quote", {"text": "AI will not replace the advisor.", "speaker": "Jane"}, document=DOC,
              tags=TAGS, verified=True, verification_note="checked p. 4 of the PDF")
    assert ok["status"] == "verified"
    g = call(m.fundus_get, "quote", str(ok["id"]))
    assert "Verified: checked p. 4" in g["notes"] and any(h["action"] == "verify" for h in g["history"])
    # re-adding unverified does not downgrade
    again = call(m.fundus_add, "quote", {"text": "AI will not replace the advisor.", "speaker": "Jane"}, document=DOC,
                 tags=TAGS)
    assert again["id"] == ok["id"] and again["status"] == "verified"


def test_add_without_document_warns(db):
    r = call(m.fundus_add, "fact", {"kind": "regulation", "title": "EU AI Act", "attributes": {"article": 50}},
             tags={"industry": ["cross-industry"], "topic": ["regulation"], "region": ["europe"]})
    assert r["status"] == "unverified" and "no document" in r["warnings"][0]
    assert call(m.fundus_search, "AI Act", region="europe")["count"] == 1


def test_document_title_fallback_and_unknown_fields(db):
    r = call(m.fundus_add, "observation", {"metric": "m", "value": 1, "period": "2025"},
             document={"url": "https://www.example.org/x"}, tags=TAGS)
    assert call(m.fundus_get, "observation", str(r["id"]))["document"]["title"] == "example.org"
    e = call(m.fundus_add, "observation", {"metric": "m", "value": 1}, document={"title": "t", "bogus": 1}, tags=TAGS)
    assert "bogus" in e["error"]
    e = call(m.fundus_add, "observation", {"metric": "m", "value": 1, "series_id": "nope"}, tags=TAGS)
    assert "does not exist" in e["error"]


def test_update_verify_and_mark_checked(db):
    r = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 55, "unit": "%", "period": "2024"},
             document=DOC, tags=TAGS, series={"id": "s1", "name": "Dealers using AI", "frequency": "annual"})
    e = call(m.fundus_update, "observation", str(r["id"]), {"status": "verified"})
    assert "note" in e["error"]
    u = call(m.fundus_update, "observation", str(r["id"]), {"status": "verified", "tags": {"region": ["de"]}},
             note="matches chart 3")
    assert u["changed"]["status"] == "verified"
    g = call(m.fundus_get, "observation", str(r["id"]))
    assert "de" in g["tags"]["region"] and "Verified: matches chart 3" in g["notes"]
    t = call(m.fundus_update_targets)
    assert t["targets"][0]["id"] == "s1" and t["targets"][0]["reason"] == "never_checked"
    c = call(m.fundus_mark_checked, "s1", next_expected="2027-11", update_hint="cdk.example, November")
    assert c["next_expected"] == "2027-11"
    assert call(m.fundus_update_targets)["total"] == 0
    assert call(m.fundus_mark_checked, "nope")["error"]


def test_forecast_flow(db):
    call(m.fundus_add_series, "s-ai", "Dealers using AI", unit="%", frequency="annual", tags=TAGS)
    f = call(m.fundus_add, "forecast", {"statement": "By 2025, 60% of dealers will use AI", "value": 60, "unit": "%",
                                        "target_period": "2025", "made_on": "2023-05", "series_id": "s-ai"},
             document=DOC, tags=TAGS)
    o = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 61, "unit": "%", "period": "2025",
                                           "series_id": "s-ai"}, document=DOC, tags=TAGS)
    lst = call(m.fundus_forecasts)
    assert lst["total"] == 1 and lst["forecasts"][0]["outcome"] == "due"
    assert lst["forecasts"][0]["proposal"]["outcome"] == "hit"
    res = call(m.fundus_resolve_forecast, f["id"], "hit", observation_id=o["id"], note="CDK 2025 edition")
    assert res["outcome"] == "hit" and res["actual_value"] == 61
    assert call(m.fundus_forecasts)["total"] == 0
    assert call(m.fundus_forecasts, state="resolved")["forecasts"][0]["resolution_note"] == "CDK 2025 edition"


def test_overview_vocab_and_add_vocab(db):
    call(m.fundus_add, "study", {"finding": "AI scribes cut documentation time by 20%", "design": "field study", "n": 120},
         document={"title": "Scribe study", "url": "https://doi.org/10.1/x", "publisher": "NEJM AI", "kind": "paper"},
         tags={"industry": ["healthcare"], "topic": ["genai"]})
    o = call(m.fundus_overview, industry="healthcare")
    assert o["counts"]["study"] == 1 and o["newest"][0]["kind"] == "study"
    v = call(m.fundus_vocab, "topic")
    assert any(x["value"] == "genai" and x["n"] == 1 for x in v["topic"])
    assert call(m.fundus_add_vocab, "topic", "Ambient Scribes", "Ambient scribes", parent="genai")["value"] == "ambient-scribes"
    assert call(m.fundus_add, "quote", {"text": "q"}, tags={"industry": ["healthcare"], "topic": ["ambient-scribes"]})["id"]
    assert "error" in call(m.fundus_add_vocab, "topic", "x", "X", parent="nope")


def test_internal_items_hidden_from_tools(db):
    call(m.fundus_add, "quote", {"text": "Client says AI scares staff", "visibility": "internal"},
         tags={"industry": ["luxury"], "topic": ["workforce"]}, project="client-x")
    assert call(m.fundus_search, "scares")["count"] == 0
    assert call(m.fundus_search, "scares", project="client-x")["count"] == 1


def test_long_text_is_clipped(db):
    call(m.fundus_add, "quote", {"text": "word " * 400}, tags=TAGS)
    hit = call(m.fundus_search, "word")["results"][0]
    assert len(hit["text"]) <= 300 and hit["text"].endswith("…")


def test_server_lists_and_calls_tools(db):
    server = m.build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {f.__name__ for f in m.TOOLS} and all(n.startswith("fundus_") for n in names)
    add = next(t for t in tools if t.name == "fundus_add")
    assert "fields" in add.input_schema["properties"]
    assert "url + publisher" in add.description
    res = asyncio.run(server.call_tool("fundus_vocab", {"facet": "region"}))
    assert "dach" in res.content[0].text
    q = json.loads(asyncio.run(server.call_tool("fundus_add", {
        "kind": "quote", "fields": {"text": "Advisors stay"}, "tags": TAGS})).content[0].text)
    got = asyncio.run(server.call_tool("fundus_get", {"kind": "quote", "id": q["id"]}))   # int id accepted
    assert json.loads(got.content[0].text)["text"] == "Advisors stay"
    assert "fundus_overview" in m.INSTRUCTIONS


def _post(port, body, host=None, session=None):
    import urllib.error
    import urllib.request
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    if host:
        headers["Host"] = host
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), r.headers.get("mcp-session-id")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), None


def test_http_server_initialize_and_host_protection(db):
    """Start the real server on a free local port and talk MCP over HTTP (Host-header check included)."""
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    root = Path(__file__).resolve().parent.parent
    import os
    cfg = Path(str(db)).parent / "config-empty.json"   # never the live data/config.json of this machine
    cfg.write_text("{}")
    env = {**os.environ, "FUNDUS_ALLOWED_HOSTS": "fundus.example.ts.net:8010", "FUNDUS_SCHEDULER": "0", "FUNDUS_CONFIG": str(cfg)}
    env.pop("FUNDUS_PUBLIC_URL", None)
    proc = subprocess.Popen([sys.executable, "-m", "fundus.mcp_server", "--db", str(db), "--host", "127.0.0.1",
                             "--port", str(port)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        status, text, sid = _post(port, init)
        assert status == 200 and '"name":"fundus"' in text.replace(" ", "") and "fundus_overview" in text
        status, _, _ = _post(port, init, host="fundus.example.ts.net:8010")      # config/env allowed_hosts
        assert status == 200
        info = json.loads((Path(db).parent / "server.json").read_text())        # next to the server's database
        assert info["public_url"] == f"http://127.0.0.1:{port}"
        status, _, _ = _post(port, init, host="evil.example:8010")
        assert status in (400, 403, 421)
        if sid:
            _post(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session=sid)
        status, text, _ = _post(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session=sid)
        assert status == 200 and "fundus_add_series" in text
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------- regression tests (review findings)
def test_duplicates_and_similar_series_hide_other_projects_internal_items(db):
    secret = call(m.fundus_add, "observation", {"metric": "Dealer AI budget share", "value": 13.7, "unit": "%",
                                                "period": "2025", "visibility": "internal"},
                  tags=TAGS, project="client-x",
                  series={"id": "client-x-ai-budget", "name": "Dealer AI budget share client panel"})
    r = call(m.fundus_add, "observation", {"metric": "Dealer AI budget share", "value": 9, "unit": "%",
                                           "period": "2024"}, document=DOC, tags=TAGS, project="session")
    assert secret["id"] not in [d["id"] for d in r["possible_duplicates"]]
    r2 = call(m.fundus_add, "observation", {"metric": "Dealer AI budget share", "value": 14, "unit": "%",
                                            "period": "2026", "visibility": "internal"}, tags=TAGS, project="client-x")
    assert secret["id"] in [d["id"] for d in r2["possible_duplicates"]]       # own project may see it
    s = call(m.fundus_add_series, "public-ai-budget", "Dealer AI budget share client panel")
    assert "client-x-ai-budget" not in [x["id"] for x in s["similar_series"]]
    s = call(m.fundus_add_series, "public-ai-budget", "Dealer AI budget share client panel", project="client-x")
    assert "client-x-ai-budget" in [x["id"] for x in s["similar_series"]]


def test_add_warns_when_existing_series_definition_differs(db):
    call(m.fundus_add_series, "s-share", "Share of dealers using AI", unit="%", publisher="CDK Global")
    r = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 3, "period": "2025"}, document=DOC,
             tags=TAGS, series={"id": "s-share", "name": "Number of AI tools per dealer", "unit": "count"})
    w = " ".join(r.get("warnings", []))
    assert "already exists" in w and "name" in w and "unit" in w and "Share of dealers using AI" in w
    r = call(m.fundus_add, "observation", {"metric": "Dealers using AI", "value": 4, "period": "2026"}, document=DOC,
             tags=TAGS, series={"id": "s-share", "name": "share of dealers  using AI", "unit": "%"})
    assert "warnings" not in r
    s = call(m.fundus_add_series, "s-share", "Something else", unit="%")
    assert "already exists" in s["warning"]


def test_update_and_resolve_refuse_internal_items(db):
    q_ = call(m.fundus_add, "quote", {"text": "secret", "visibility": "internal"}, tags=TAGS, project="client-x")
    e = call(m.fundus_update, "quote", q_["id"], {"status": "disputed"}, note="x")
    assert "internal" in e["error"]
    assert "internal" in call(m.fundus_update, "quote", q_["id"], {"status": "disputed"}, note="x",
                              project="other")["error"]
    ok = call(m.fundus_update, "quote", q_["id"], {"status": "disputed"}, note="x", project="client-x")
    assert ok["changed"]["status"] == "disputed"
    f = call(m.fundus_add, "forecast", {"statement": "secret plan", "target_period": "2025", "visibility": "internal"},
             tags=TAGS, project="client-x")
    assert "internal" in call(m.fundus_resolve_forecast, f["id"], "miss")["error"]
    assert call(m.fundus_resolve_forecast, f["id"], "miss", project="client-x")["outcome"] == "miss"


def test_vocab_tool_hides_internal_projects(db):
    call(m.fundus_add, "quote", {"text": "secret", "visibility": "internal"}, tags=TAGS, project="client-x")
    assert all(p["value"] != "client-x" for p in call(m.fundus_vocab)["project"])
    assert any(p["value"] == "client-x" for p in call(m.fundus_vocab, project="client-x")["project"])


def test_journal_from_crossref_and_document_publisher_correction(db, monkeypatch):
    from fundus import crossref
    seen = []

    def fake(doi, timeout=6.0):
        seen.append(doi)
        return {"journal": {"10.1016/j.lrp.2009.07.003": "Long Range Planning",
                            "10.1016/j.clsr.2020.1": "Computer Law & Security Review"}.get(doi), "publisher": "Elsevier BV"}
    monkeypatch.setattr(crossref, "lookup", fake)
    fact = {"kind": "definition", "title": "Business models are templates of value creation", "what": "x"}
    tags = {"industry": ["cross-industry"], "topic": ["ai"]}
    a = call(m.fundus_add, "fact", {**fact, "title": "First paper"}, tags=tags,
             document={"title": "CLSR paper", "url": "https://doi.org/10.1016/j.clsr.2020.1", "doi": "10.1016/j.clsr.2020.1",
                       "publisher": "Elsevier (Computer Law & Security Review)", "kind": "article"})
    b = call(m.fundus_add, "fact", fact, tags=tags,
             document={"title": "Business models, business strategy and innovation", "doi": "10.1016/j.lrp.2009.07.003",
                       "url": "https://doi.org/10.1016/j.lrp.2009.07.003", "publisher": "Elsevier", "kind": "article"})
    ga, gb = call(m.fundus_get, "fact", str(a["id"])), call(m.fundus_get, "fact", str(b["id"]))
    # same house, each paper keeps its own journal
    assert gb["document"]["journal"] == "Long Range Planning"
    assert ga["document"]["journal"] == "Computer Law & Security Review"
    assert ga["document"]["publisher"]["name"] == "Elsevier"   # house without the journal in brackets
    doc = b["document_id"] if "document_id" in b else gb["document_id"]
    # adding again does not ask Crossref a second time
    n = len(seen)
    call(m.fundus_add, "fact", {**fact, "title": "Another fact"}, tags=tags,
         document={"title": "Business models, business strategy and innovation", "doi": "10.1016/j.lrp.2009.07.003",
                   "url": "https://doi.org/10.1016/j.lrp.2009.07.003", "publisher": "Elsevier"})
    assert len(seen) == n
    # publisher and journal can be corrected
    u = call(m.fundus_update, "document", doc, {"publisher": "SAGE", "journal": "Journal of Management"})
    assert u["changed"]["journal"] == "Journal of Management" and u["changed"]["publisher_id"] == "sage"
    g = call(m.fundus_get, "fact", str(b["id"]))
    assert g["document"]["publisher"]["name"] == "SAGE" and g["document"]["journal"] == "Journal of Management"
    assert "unknown" in call(m.fundus_update, "document", doc, {"venue": "x"})["error"]


def test_document_brackets_stay_with_the_document(db):
    tags = {"industry": ["cross-industry"], "topic": ["ai"]}
    a = call(m.fundus_add, "fact", {"kind": "definition", "title": "Thesis fact", "what": "x"}, tags=tags,
             document={"title": "A thesis", "publisher": "University of St.Gallen (master's thesis)"})
    b = call(m.fundus_add, "fact", {"kind": "definition", "title": "Paper fact", "what": "y"}, tags=tags,
             document={"title": "A conference paper", "publisher": "University of St.Gallen (R&D Management Conference)"})
    ga, gb = call(m.fundus_get, "fact", str(a["id"])), call(m.fundus_get, "fact", str(b["id"]))
    assert ga["document"]["publisher"]["name"] == gb["document"]["publisher"]["name"] == "University of St.Gallen"
    assert "master's thesis" in ga["document"]["notes"] and "R&D Management Conference" in gb["document"]["notes"]
    c = call(m.fundus_add, "fact", {"kind": "definition", "title": "BFS fact", "what": "z"}, tags=tags,
             document={"title": "Statistik", "publisher": "Bundesamt für Statistik (BFS)"})
    assert call(m.fundus_get, "fact", str(c["id"]))["document"]["publisher"]["name"] == "Bundesamt für Statistik (BFS)"


def test_api_call_retries_locked_write(db, monkeypatch):
    """A write that collides with another process ('database is locked') is retried before the portal sees an error."""
    import sqlite3
    from fundus import mcp_server as m
    m.configure(db)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    calls = []

    def fn(st):
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return {"ok": True}

    class Req:
        method = "POST"
        query_params = {}
        headers = {}
        class url:
            path = "/api/trash"
    r = m._api_call(Req(), fn)
    assert len(calls) == 3 and r.status_code == 200
