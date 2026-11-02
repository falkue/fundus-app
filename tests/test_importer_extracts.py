import json

from importers import extracts


def _write(tmp_path):
    data = {
        "project": "demo",
        "vocab": [{"facet": "topic", "value": "nutrition", "label": "Nutrition"}],
        "items": [
            {"kind": "observation", "key": "o1", "source_file": "r.md",
             "fields": {"metric": "CMO budget share of revenue", "value": 7.7, "unit": "%", "period": "2024",
                        "status": "verified"},
             "document": {"title": "CMO Spend Survey 2024", "url": "https://g.com/cmo24", "publisher": "Gartner",
                          "publisher_type": "analyst", "published": "2024-05"},
             "series": {"id": "gartner-cmo-budget-share", "name": "Marketing budget as share of revenue", "unit": "%",
                        "frequency": "annual"},
             "tags": {"industry": ["marketing"], "topic": ["marketing-practice"]}},
            {"kind": "forecast", "key": "f1", "source_file": "r.md",
             "fields": {"statement": "Half of diets will be app-guided", "target_period": "2030", "value": 50, "unit": "%"},
             "tags": {"industry": ["healthcare"], "topic": ["nutrition"]}},
        ],
    }
    (tmp_path / "demo.json").write_text(json.dumps(data))


def test_extracts_import_is_idempotent_and_builds_series(store, tmp_path):
    _write(tmp_path)
    assert extracts.run(store, tmp_path) == {"observation": 1, "forecast": 1}
    extracts.run(store, tmp_path)
    assert store.db.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM forecast").fetchone()[0] == 1
    row = store.db.execute("SELECT o.series_id, p.type FROM observation o JOIN document d ON d.id = o.document_id "
                           "JOIN publisher p ON p.id = d.publisher_id").fetchone()
    assert tuple(row) == ("gartner-cmo-budget-share", "analyst")
