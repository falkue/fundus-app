from datetime import date

import pytest

from fundus import query as q


@pytest.fixture
def filled(store):
    with store.batch("t"):
        d1 = store.document("Dealer AI survey 2023", url="https://cdk.example/2023", publisher="CDK Global",
                            kind="survey_report", published="2023-11")
        d2 = store.document("Dealer AI survey 2024", url="https://cdk.example/2024", publisher="CDK Global",
                            published="2024-11")
        store.series("cdk-dealer-ai", "Share of dealers using AI", unit="%", publisher="CDK Global",
                     frequency="annual", update_hint="cdk.example/research, November")
        tags = {"industry": ["automotive-retail"], "topic": ["ai-adoption"], "region": ["us"]}
        o1 = store.add("observation", {"series_id": "cdk-dealer-ai", "document_id": d1, "metric": "Dealers using AI",
                                       "value": 40, "unit": "%", "period": "2023", "status": "verified"},
                       tags=tags, origin=("acme", "a.json", "1"))
        o2 = store.add("observation", {"series_id": "cdk-dealer-ai", "document_id": d2, "metric": "Dealers using AI",
                                       "value": 55, "unit": "%", "period": "2024"},
                       tags=tags, origin=("acme", "a.json", "2"))
        store.add("observation", {"metric": "Hospitals using ambient scribes", "value": 22, "unit": "%", "period": "2025"},
                  tags={"industry": ["healthcare"], "topic": ["genai"], "region": ["de"]})
        internal = store.add("quote", {"text": "Our dealers fear AI scribes replace them", "speaker": "Client CEO",
                                       "visibility": "internal"},
                             tags={"industry": ["automotive-retail"], "topic": ["workforce"]},
                             origin=("acme-interviews", "i.json", "q1"))
        f = store.add("forecast", {"statement": "By 2026 70% of dealers will use AI", "value": 70, "unit": "%",
                                   "target_period": "2026", "series_id": "cdk-dealer-ai", "document_id": d2},
                      tags=tags)
        store.series("once-series", "A one-off", frequency="once")
        store.series("future-series", "Checked recently", next_expected="2099-01")
    return {"o1": o1, "o2": o2, "internal": internal, "forecast": f}


def test_fts_query_quotes_words():
    assert q.fts_query("AI-adoption dealers") == '"AI" "adoption"* "dealers"*'
    assert q.fts_query("a b", "any") == '"a" OR "b"'


def test_period_start():
    assert q.period_start("2026") == date(2026, 1, 1)
    assert q.period_start("2026-08") == date(2026, 8, 1)
    assert q.period_start("2025-Q3") == date(2025, 7, 1)
    assert q.period_start("2025-H2") == date(2025, 7, 1)
    assert q.period_start("by 2030") == date(2030, 1, 1)
    assert q.period_start("soon") is None
    assert q.parse_period("2026-2030") == q.Period("year", date(2026, 1, 1), date(2030, 12, 31), date(2030, 1, 1))
    assert q.period_end("2025-11/12") == date(2025, 12, 31) and q.parse_period("2025-11/12").gran == "month"
    assert q.period_end("2025/26") == date(2026, 12, 31)
    assert q.parse_period("FY2019").end == date(2019, 12, 31)
    assert q.parse_period("Q2 2025") == q.parse_period("2025-Q2")
    assert q.parse_period("H1 2024").end == date(2024, 6, 30)
    assert q.period_end("2026-02") == date(2026, 2, 28)


def test_search_ranks_and_returns_source(filled, store):
    r = q.search(store, "dealers AI")
    kinds = [x["kind"] for x in r["results"]]
    assert "observation" in kinds and "forecast" in kinds
    obs = next(x for x in r["results"] if x["kind"] == "observation")
    assert obs["source"]["publisher"] == "CDK Global" and obs["source"]["url"].startswith("https://cdk")


def test_search_prefix_and_any_fallback(filled, store):
    assert q.search(store, "scribe")["count"] == 1               # prefix: scribe* → scribes
    r = q.search(store, "scribes zzzzunknown")
    assert r["match"] == "any" and r["count"] == 1


def test_tag_filter_includes_children(filled, store):
    r = q.search(store, "AI", industry="automotive", kind="observation")
    assert {x["id"] for x in r["results"]} == {filled["o1"], filled["o2"]}
    r = q.search(store, "", region="europe")                     # europe ⊃ dach ⊃ de
    assert [x["metric"] for x in r["results"]] == ["Hospitals using ambient scribes"]


def test_internal_hidden_unless_requested_or_project(filled, store):
    assert q.search(store, "scribes replace", kind="quote")["count"] == 0
    assert q.search(store, "scribes replace", kind="quote", include_internal=True)["count"] == 1
    assert q.search(store, "scribes replace", kind="quote", project="acme-interviews")["count"] == 1
    assert q.search(store, "scribes replace", kind="quote", project="acme")["count"] == 0
    assert q.search(store, "scribes replace")["match"] == "any"   # only the public hospital item via fallback
    with pytest.raises(PermissionError):
        q.get(store, "quote", filled["internal"])
    assert q.get(store, "quote", filled["internal"], project="acme-interviews")["speaker"] == "Client CEO"


def test_status_and_since_filters(filled, store):
    r = q.search(store, "dealers", status="verified")
    assert [x["id"] for x in r["results"]] == [filled["o1"]]
    r = q.search(store, "dealers", kind="observation", since="2024")
    assert [x["id"] for x in r["results"]] == [filled["o2"]]


def test_get_observation_includes_series_and_origin(filled, store):
    g = q.get(store, "observation", filled["o1"])
    assert g["document"]["publisher"]["name"] == "CDK Global"
    assert g["origin"][0]["project"] == "acme"
    assert [o["period"] for o in g["series"]["observations"]] == ["2023", "2024"]
    assert g["tags"]["industry"] == ["automotive-retail"]


def test_series_sorted_with_sources_and_forecasts(filled, store):
    s = q.series(store, "cdk-dealer-ai")
    assert [(o["period"], o["value"]) for o in s["observations"]] == [("2023", 40), ("2024", 55)]
    assert s["observations"][1]["source"]["title"] == "Dealer AI survey 2024"
    assert s["forecasts"][0]["target_period"] == "2026"
    assert s["n_periods"] == 2 and s["last_period"] == "2024"


def test_find_series(filled, store):
    assert q.find_series(store, "dealers")[0]["id"] == "cdk-dealer-ai"
    assert q.find_series(store, "cdk-dealer")[0]["id"] == "cdk-dealer-ai"   # substring fallback on the id


def test_overview(filled, store):
    o = q.overview(store, industry="automotive")
    assert o["counts"]["observation"] == 2 and o["counts"]["quote"] == 0 and o["counts"]["forecast"] == 1
    assert o["top_series"][0]["id"] == "cdk-dealer-ai" and o["top_series"][0]["n_periods"] == 2
    assert o["forecasts_open_or_due"][0]["id"] == filled["forecast"]
    assert "ai-adoption" in o["by_topic"]
    assert q.overview(store, industry="automotive", include_internal=True)["counts"]["quote"] == 1


def test_update_targets(filled, store):
    t = q.update_targets(store, today=date(2026, 10, 24))
    ids = [x["id"] for x in t["targets"]]
    assert "cdk-dealer-ai" in ids and "once-series" not in ids and "future-series" not in ids
    x = next(x for x in t["targets"] if x["id"] == "cdk-dealer-ai")
    assert x["reason"] == "never_checked" and x["last_period"] == "2024" and "November" in x["update_hint"]
    with store.batch("t"):
        store.update("series", "cdk-dealer-ai", {"last_checked": "2026-10-01"})
    assert "cdk-dealer-ai" not in [x["id"] for x in q.update_targets(store, today=date(2026, 10, 24))["targets"]]
    with store.batch("t"):
        store.update("series", "cdk-dealer-ai", {"next_expected": "2026-09"})
    t = q.update_targets(store, today=date(2026, 10, 24), industry="automotive")
    assert t["targets"][0] == {**t["targets"][0], "id": "cdk-dealer-ai", "reason": "overdue"}


def test_vocab_counts(filled, store):
    v = q.vocab(store)
    ar = next(x for x in v["industry"] if x["value"] == "automotive-retail")
    assert ar["parent"] == "automotive" and ar["n"] >= 3
    assert {"value": "acme", "n": 2} in v["project"]


def test_cli_search_export_and_due(filled, store, tmp_path, capsys):
    import json as _json
    from fundus import cli
    db = str(store.path)
    store.db.commit()
    assert cli.main(["--db", db, "search", "dealers", "--kind", "observation"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert cli.main(["--db", db, "export", "--format", "jsonl"]) == 0
    lines = [_json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert all(x.get("visibility") != "internal" for x in lines) and len(lines) == 4
    csv_path = tmp_path / "e.csv"
    assert cli.main(["--db", db, "export", "--format", "csv", "--out", str(csv_path), "--include-internal"]) == 0
    assert len(csv_path.read_text().splitlines()) == 6
    assert cli.main(["--db", db, "due"]) == 0
    assert "cdk-dealer-ai" in capsys.readouterr().out
    assert cli.main(["--db", db, "forecasts", "--due", "--today", "2027-02-01"]) == 0
    assert _json.loads(capsys.readouterr().out)[0]["forecast"]["target_period"] == "2026"
    assert cli.main(["--db", db, "get", "quote", str(filled["internal"])]) == 1


def test_cli_snapshot_writes_consistent_copy(store, tmp_path):
    import sqlite3
    from fundus.cli import snapshot
    with store.batch("t"):
        store.add("quote", {"text": "Snapshot me"}, tags={"industry": ["luxury"], "topic": ["future"]})
    res = snapshot(store, str(tmp_path / "b" / "copy.sqlite"))
    assert sqlite3.connect(res["snapshot"]).execute("SELECT text FROM quote").fetchone()[0] == "Snapshot me"


# ---------------------------------------------------------------- regression tests (review findings)
@pytest.fixture
def mixed(store):
    """Public series s1 with one public and one internal observation (project 'client-a') plus an internal forecast."""
    tags = {"industry": ["retail"], "topic": ["market"]}
    with store.batch("t"):
        d_int = store.document("Client deck", url="https://client.example/deck")
        store.series("s1", "Store traffic index")
        pub = store.add("observation", {"series_id": "s1", "metric": "traffic", "value": 1, "period": "2024"}, tags=tags)
        intern = store.add("observation", {"series_id": "s1", "metric": "traffic", "value": 999, "period": "2025",
                                           "document_id": d_int, "visibility": "internal"},
                           tags=tags, origin=("client-a", "c.json", "1"))
        fint = store.add("forecast", {"statement": "traffic secret", "series_id": "s1", "target_period": "2027",
                                      "visibility": "internal"}, tags=tags, origin=("client-a", "c.json", "f"))
    return {"pub": pub, "intern": intern, "fint": fint, "doc": d_int}


def test_unknown_project_does_not_unhide_internal(mixed, store):
    s = q.series(store, "s1", project="nonexistent")
    assert [o["value"] for o in s["observations"]] == [1] and s["forecasts"] == [] and s["n_periods"] == 1
    g = q.get(store, "observation", mixed["pub"], project="nonexistent")
    assert [o["value"] for o in g["series"]["observations"]] == [1]
    hit = q.search(store, "traffic index", kind="series", visible_project="nonexistent")["results"][0]
    assert hit["n_obs"] == 1 and hit["last_period"] == "2024"
    o = q.overview(store, industry="retail")
    assert o["top_series"][0]["n_periods"] == 1
    s = q.series(store, "s1", project="client-a")
    assert [o["value"] for o in s["observations"]] == [1, 999] and len(s["forecasts"]) == 1


def test_breakdown_vocab_and_document_respect_visibility(mixed, store):
    o = q.overview(store, topic="market")
    assert o["by_industry"] == {"retail": 1}                 # only the public observation, not the internal ones
    v = q.vocab(store)
    assert all(p["value"] != "client-a" for p in v["project"])
    assert next(x for x in v["industry"] if x["value"] == "retail")["n"] == 1
    assert {"value": "client-a", "n": 2} in q.vocab(store, project="client-a")["project"]
    with pytest.raises(PermissionError):
        q.get(store, "document", mixed["doc"])
    assert q.get(store, "document", mixed["doc"], project="client-a")["n_items"] == 1


def test_since_uses_parsed_periods(store):
    tags = {"industry": ["retail"], "topic": ["market"]}
    with store.batch("t"):
        old = store.add("observation", {"metric": "old fy", "value": 1, "period": "FY2019"}, tags=tags)
        new = store.add("observation", {"metric": "new q", "value": 2, "period": "Q2 2025"}, tags=tags)
        h = store.add("observation", {"metric": "half", "value": 3, "period": "H1 2024"}, tags=tags)
    got = {x["id"] for x in q.search(store, "", kind="observation", since="2024-03")["results"]}
    assert got == {new, h} and old not in got
    with pytest.raises(ValueError):
        q.search(store, "", since="recently")
