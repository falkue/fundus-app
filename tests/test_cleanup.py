"""Data hygiene (fundus.cleanup): findings per class, automatic run with backup, redirects for merged records (also on
slides), decisions (merge with a chosen record, no duplicate, contradictions), undo back to the identical state, score,
geo codes on write, portal API."""
import hashlib
import json

import pytest

from fundus import cleanup
from fundus import mcp_server as m
from fundus import query
from fundus.geo import canonical_geo

TABLES = ["observation", "forecast", "quote", "document", "publisher", "series", "tag", "origin", "exhibit_link",
          "entity_link", "item_check", "publisher_alias"]


def digest(store):
    out = {}
    for t in TABLES:
        rows = sorted(json.dumps([r[k] for k in r.keys() if k != "updated_at"], default=str)
                      for r in store.db.execute(f"SELECT * FROM {t}"))
        out[t] = (len(rows), hashlib.sha1("\n".join(rows).encode()).hexdigest())
    return out


def raw_doc(store, title, url=None, publisher=None, doi=None, published=None):
    from fundus.store import norm_url
    cur = store.db.execute("INSERT INTO document(title, url, url_key, publisher_id, doi, published) VALUES (?,?,?,?,?,?)",
                           (title, url, norm_url(url) if url else None, publisher, doi, published))
    return cur.lastrowid


def clone(store, oid, **changes):
    """A second row with the same content, as older imports left them (store.add itself reuses identical items)."""
    row = dict(store.db.execute("SELECT * FROM observation WHERE id = ?", (oid,)).fetchone())
    row.pop("id")
    row.update(changes)
    cur = store.db.execute(f"INSERT INTO observation({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    return cur.lastrowid


def build(store):
    """A small archive with one example of every finding."""
    with store.batch("test"):
        pub = store.publisher("Gartner", type="analyst")
        store.db.execute("INSERT INTO publisher(id, name) VALUES ('gartner-inc', 'gartner')")      # same name: safe
        store.db.execute("INSERT INTO publisher(id, name) VALUES ('lonely-pub', 'Lonely Pub')")    # orphan
        d1 = raw_doc(store, "AI survey 2026", "https://g.com/ai-2026", pub, doi="10.1/x")
        d2 = raw_doc(store, "AI survey 2026 (copy)", "https://other.com/ai", "gartner-inc", doi="10.1/X")   # same DOI
        d3 = raw_doc(store, "Hospital AI report", "https://a.org/h1", pub)
        d4 = raw_doc(store, "Hospital AI report", None, pub)                                       # probable
        base = {"document_id": d3, "metric": "Hospitals using AI", "value": 40, "unit": "%", "period": "2025"}
        o1 = store.add("observation", {**base, "status": "verified"}, tags={"topic": ["ai-adoption"]}, origin=("p", "a", "1"))
        o2 = clone(store, o1, status="unverified")                     # identical row (store.add would reuse o1) → safe
        o3 = store.add("observation", {**base, "metric": "Hospitals deploying AI"}, origin=("p", "c", "3"))
        o4 = store.add("observation", {**base, "metric": "Nurses trust AI", "value": 30}, origin=("p", "d", "4"))
        o5 = store.add("observation", {**base, "metric": "Nurses trust AI", "value": 35, "category": None}, origin=("p", "e", "5"))
        store.db.execute("UPDATE observation SET geo = 'United States' WHERE id = ?", (o4,))         # old spelling
        store.db.execute("UPDATE observation SET geo = 'United States' WHERE id = ?", (o5,))
        store.series("s-ai", "AI use in hospitals", publisher=pub, unit="%")
        store.series("s-ai-2", "AI use in hospitals", publisher=pub, unit="%")                      # same definition
        store.db.execute("INSERT INTO exhibit_set(id, project, slug, title) VALUES (900, 'p', 'main', 'Main')")
        store.db.execute("INSERT INTO exhibit(id, project, set_id, slug, title) VALUES (77, 'p', 900, 'slide', 'Slide')")
        store.db.execute("INSERT INTO exhibit_link(exhibit_id, target_kind, target_id, role) VALUES (77, 'observation', ?, 'data')",
                         (str(o2),))
    return {"d": [d1, d2, d3, d4], "o": [o1, o2, o3, o4, o5], "pub": pub}


def test_geo_codes():
    assert [canonical_geo(g) for g in ("United States", "us", "World", "global", "Deutschland", "Schweiz",
                                       "United States – Texas", "Europe", "Zurich")] == \
        ["US", "US", "WORLD", "WORLD", "DE", "CH", "US – Texas", "Europe", "Zurich"]


def test_geo_code_on_write(store):
    with store.batch("test"):
        d = store.document("X", url="https://x.org")
        i = store.add("observation", {"document_id": d, "metric": "m", "value": 1, "unit": "%", "period": "2025",
                                      "geo": "United States"})
        store.update("observation", i, {"geo": "Weltweit"})
    assert store.db.execute("SELECT geo FROM observation WHERE id = ?", (i,)).fetchone()[0] == "WORLD"


def test_analyze_finds_each_class(store):
    ids = build(store)
    r = cleanup.analyze(store)
    c = r["classes"]
    assert c["variant"]["records"] == 2 and c["orphan"]["groups"] == 1 and c["contradiction"]["groups"] == 1
    safe = cleanup.candidates(store, "safe")["items"]
    kinds = sorted(x["kind"] for x in safe)
    assert kinds == ["document", "observation", "publisher"]
    prob = cleanup.candidates(store, "probable")["items"]
    assert {x["kind"] for x in prob} == {"document", "observation", "series"}
    obs_prob = next(x for x in prob if x["kind"] == "observation")
    assert set(int(r["id"]) for r in obs_prob["records"]) == {ids["o"][0], ids["o"][2]}    # similar wording
    assert 0 < r["score"] < 1 and r["auto_records"] == c["variant"]["records"] + c["safe"]["records"] + 1


def test_apply_redirects_and_undo_restores(store):
    ids = build(store)
    before = digest(store)
    r = cleanup.apply(store)
    assert not r["failed"] and r["backup"] and r["score"] > 0
    o1, o2 = ids["o"][0], ids["o"][1]
    assert store.db.execute("SELECT count(*) FROM observation WHERE id = ?", (o2,)).fetchone()[0] == 0
    assert store.follow("observation", o2) == str(o1)                       # verified one kept, redirect left
    got = query.get(store, "observation", o2)
    assert got["id"] == o1 and got["redirected_from"] == f"observation:{o2}"
    assert store.db.execute("SELECT target_id FROM exhibit_link WHERE exhibit_id = 77").fetchone()[0] == str(o1)
    assert store.db.execute("SELECT count(*) FROM observation WHERE geo = 'United States'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM publisher WHERE id IN ('lonely-pub', 'gartner-inc')").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM document WHERE id = ?", (ids["d"][1],)).fetchone()[0] == 0
    assert cleanup.candidates(store, "safe")["total"] == 0 and cleanup.candidates(store, "variant")["total"] == 0
    u = cleanup.undo(store, r["run"])
    assert u["undone"] == r["run"]
    assert digest(store) == before                                          # exactly the state before the run
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert cleanup.candidates(store, "safe")["total"] > 0                   # findings open again
    with pytest.raises(ValueError):
        cleanup.undo(store, r["run"])                                       # only once


def test_decide_merge_keep_reject_dispute(store):
    ids = build(store)
    cleanup.analyze(store)
    prob = {x["kind"]: x for x in cleanup.candidates(store, "probable")["items"] if not x.get("action")}
    doc = prob["document"]
    other = [r["id"] for r in doc["records"] if str(r["id"]) != doc["keep"]][0]
    res = cleanup.decide(store, [doc["id"]], "merge", keep=other)          # Falk keeps the other one
    assert res["records"] == 1 and store.follow("document", doc["keep"]) == str(other)
    with pytest.raises(ValueError):
        cleanup.decide(store, [prob["series"]["id"]], "merge", keep="nope")
    cleanup.decide(store, [prob["series"]["id"]], "reject")                 # not a duplicate
    cleanup.analyze(store)
    assert all(x["kind"] != "series" or x.get("action") for x in cleanup.candidates(store, "probable")["items"])
    assert cleanup.candidates(store, state="rejected")["total"] == 1
    con = cleanup.candidates(store, "contradiction")["items"][0]
    cleanup.decide(store, [con["id"]], "dispute")
    st = {r[0] for r in store.db.execute("SELECT status FROM observation WHERE id IN (?, ?)", (ids["o"][3], ids["o"][4]))}
    assert st == {"disputed"}
    runs = cleanup.runs(store)
    assert {r["kind"] for r in runs} >= {"analyze", "decide"}


def test_public_and_internal_never_merged(store):
    with store.batch("test"):
        d = store.document("Doc", url="https://d.org")
        a = store.add("observation", {"document_id": d, "metric": "m", "value": 1, "unit": "%", "period": "2025"}, origin=("p", "a", "1"))
        b = clone(store, a, visibility="internal")
    assert a != b
    cleanup.analyze(store)
    assert cleanup.candidates(store, "safe")["total"] == 0


def test_status_trend_and_metrics(store):
    build(store)
    cleanup.analyze(store)
    s = cleanup.status(store)
    assert s["score"] is not None and len(s["trend"]) == 1 and s["classes"]["variant"]["groups"] == 1
    from fundus import metrics
    out = metrics.compute(store, use_cache=False)
    assert out["cleanup"]["score"] == s["score"]


def test_portal_api(store):
    import time
    from test_reviews import call
    build(store)
    m.configure(store.path)
    code, s = call(m, m.cleanup_api, path="/api/cleanup")
    assert code == 200 and "job" in s
    code, j = call(m, m.cleanup_api, "POST", "/api/cleanup", body={"action": "analyze"})
    assert code == 202 and j["job"]["state"] == "running"
    for _ in range(100):
        code, s = call(m, m.cleanup_api, path="/api/cleanup")
        if s["job"].get("state") != "running":
            break
        time.sleep(0.1)
    assert s["job"]["state"] == "done" and s["score"] is not None
    code, c = call(m, m.cleanup_api, query=b"view=candidates&class=probable", path="/api/cleanup")
    assert code == 200 and c["total"] >= 1 and c["items"][0]["records"]
    code, d = call(m, m.cleanup_api, "POST", "/api/cleanup", body={"action": "decide", "ids": [c["items"][0]["id"]], "decision": "later"})
    assert code == 200 and d["state"] == "later"
    code, _ = call(m, m.cleanup_api, "POST", "/api/cleanup", body={"action": "nonsense"})
    assert code == 400
    code, _ = call(m, m.cleanup_api, "POST", "/api/cleanup", body={"action": "analyze"}, origin="http://evil.example")
    assert code == 403
    m.configure(m.DEFAULT_DB)
