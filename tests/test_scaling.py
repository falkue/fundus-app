import json
import sqlite3

from importers import extracts


def _extract(tmp_path, value=7.7):
    data = {"project": "demo", "items": [{
        "kind": "observation", "key": "o1", "source_file": "r.md",
        "fields": {"metric": "CMO budget share", "value": value, "unit": "%", "period": "2024"},
        "tags": {"industry": ["marketing"], "topic": ["marketing-practice"]}}]}
    (tmp_path / "demo.json").write_text(json.dumps(data))


def test_extracts_skip_unchanged_files_and_reimport_changed(store, tmp_path, monkeypatch):
    monkeypatch.delenv("FUNDUS_FORCE_IMPORT", raising=False)
    _extract(tmp_path)
    assert extracts.run(store, tmp_path) == {"observation": 1}
    assert extracts.run(store, tmp_path) == {"files_unchanged": 1}
    _extract(tmp_path, value=7.9)
    assert extracts.run(store, tmp_path) == {"observation": 1}
    assert store.db.execute("SELECT value FROM observation").fetchone()[0] == 7.9
    monkeypatch.setenv("FUNDUS_FORCE_IMPORT", "1")
    assert extracts.run(store, tmp_path) == {"observation": 1}


def test_metrics_cache_hit_until_data_changes(store):
    import datetime as dt
    from fundus import metrics
    day = dt.date(2026, 10, 24)
    with store.batch("t"):
        store.add("quote", {"text": "Q1"}, tags={"industry": ["luxury"], "topic": ["future"]})
    assert metrics.compute(store, today=day)["cache"] == "computed"
    m = metrics.compute(store, today=day)
    assert m["cache"] == "hit" and m["itemsTotal"] == 1 and "activity" in m
    with store.batch("t"):
        store.add("quote", {"text": "Q2"}, tags={"industry": ["luxury"], "topic": ["future"]})
    m = metrics.compute(store, today=day)
    assert m["cache"] == "computed" and m["itemsTotal"] == 2


def test_snapshot_signature_ignores_reopen_but_sees_changes(store, tmp_path):
    from fundus.snapshot_job import signature
    from fundus import Store
    con = lambda: sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    s1 = signature(con())
    Store(store.path).close()                       # an open writes meta, but changes no content
    assert signature(con()) == s1
    with store.batch("t"):
        store.add("quote", {"text": "New"}, tags={"industry": ["luxury"], "topic": ["future"]})
    assert signature(con()) != s1


def test_snapshot_signature_sees_every_table(store):
    from fundus.snapshot_job import signature
    con = lambda: sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    s1 = json.loads(signature(con()))
    assert "meta" not in s1 and "changelog" in s1
    assert len(s1) > 15                              # all ordinary tables, not a hand-kept list
