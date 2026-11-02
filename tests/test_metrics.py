import datetime as dt
import json


def test_metrics_cover_all_sections(store):
    from fundus import metrics
    with store.batch("importer:p"):
        store.series("s", "Share using AI", unit="%")
        d = store.document("Survey", url="https://s.org", publisher="Survey Org", published="2025-05")
        for per, v in (("2024", 40), ("2025", 45)):
            store.add("observation", {"series_id": "s", "document_id": d, "metric": "Share using AI", "value": v,
                                      "unit": "%", "period": per, "status": "verified"},
                      tags={"industry": ["healthcare"], "topic": ["ai-adoption"]}, origin=("p", "f", per))
        store.add("forecast", {"statement": "Half will use AI", "target_period": "2024", "outcome": "due",
                               "document_id": d}, tags={"industry": ["healthcare"], "topic": ["future"]},
                  origin=("p", "f", "fc"))
        store.add("quote", {"text": "No speaker here"}, tags={"industry": ["healthcare"], "topic": ["ai"]})
    store.db.execute("INSERT INTO usage(day, tool, calls) VALUES (date('now','localtime'), 'fundus_search', 3)")
    m = metrics.compute(store, today=dt.date(2026, 10, 24))
    json.dumps(m)
    assert m["itemsTotal"] == 4 and m["totals"]["series"] == 1
    assert m["series"]["multiPeriod"] == 1 and m["series"]["periodsHistogram"] == [{"periods": "2", "series": 1}]
    assert m["forecasts"]["byOutcome"]["due"] == 1 and m["forecasts"]["pastTargetUnresolved"] == 1
    assert m["quality"]["quotesWithoutSpeaker"] == 1 and m["quality"]["itemsWithoutSource"] == 1
    assert m["byProject"][0]["project"] == "p" and m["byProject"][0]["verifiedShare"] == round(2 / 3, 3)
    assert m["matrix"]["rows"][0]["value"] == "healthcare"
    assert m["activity"]["toolCalls30d"] == [{"tool": "fundus_search", "calls": 3}]


def test_usage_is_counted_per_tool(tmp_path):
    from fundus import mcp_server
    mcp_server.configure(tmp_path / "u.sqlite")
    mcp_server.record_usage("fundus_search")
    mcp_server.record_usage("fundus_search")
    st = mcp_server._store()
    assert st.db.execute("SELECT calls FROM usage WHERE tool='fundus_search'").fetchone()[0] == 2
