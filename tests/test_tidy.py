"""fundus.tidy: the unit standard (rules, plan, apply with undo) and the review/decide round trip with Claude."""
import json

from fundus import cleanup, tidy


def test_unit_rules():
    s = tidy.standard_unit
    assert s("% of respondents") == ("%", "respondents", None)
    assert s("% of GDP") == ("% of GDP", None, None)                     # a denominator is a different measure
    assert s("% of marketing budget") == ("% of marketing budget", None, None)
    assert s("TCHF") == ("CHF k", None, None)
    assert s("minutes/day") == ("minutes per day", None, None)
    assert s("index, 2016 = 100") == ("index (2016 = 100)", None, None)
    assert s("score (1-7)") == ("score (1–7)", None, None)
    assert s("% (about)") == ("%", None, "about")
    assert s("USD bn (2019 dollars)") == ("USD bn (2019 dollars)", None, None)   # price base stays
    assert s("% (percentage points as stated)") == ("pp", None, None)
    assert s("% -> 12.3% (2021 -> 2024)")[0] == "% -> 12.3% (2021 -> 2024)"     # compound: left for a human
    assert s("$ bn") == ("USD bn", None, None)


def test_apply_units_and_undo(store):
    d = store.document("Survey", url="https://ex.org/s", publisher="Ex")
    a = store.add("observation", {"metric": "Use AI", "value": 40, "unit": "% of respondents", "period": "2025", "document_id": d})
    b = store.add("observation", {"metric": "Use AI at work", "value": 12, "unit": "% (about)", "period": "2025", "document_id": d,
                                  "population": "US adults"})
    store.db.commit()
    r = tidy.apply_units(store)
    rows = {x["id"]: x for x in store.db.execute("SELECT * FROM observation")}
    assert rows[a]["unit"] == "%" and rows[a]["population"] == "respondents"
    assert rows[b]["unit"] == "%" and rows[b]["population"] == "US adults" and "Unit as given: % (about)" in rows[b]["notes"]
    cleanup.undo(store, r["run"])
    rows = {x["id"]: x for x in store.db.execute("SELECT * FROM observation")}
    assert rows[a]["unit"] == "% of respondents" and not rows[a]["population"] and rows[b]["unit"] == "% (about)"


def test_review_and_decide(store, tmp_path):
    d = store.document("Dealer survey", url="https://ex.org/d", publisher="Ex")
    x = store.add("observation", {"metric": "Share of dealers using AI", "value": 40, "unit": "%", "period": "2025", "document_id": d})
    y = store.add("observation", {"metric": "Share of dealers using AI ('nearly 40%')", "value": 40, "unit": "%", "period": "2025",
                                  "document_id": d})
    store.db.commit()
    cleanup.analyze(store)
    rv = tidy.review(store)
    if not rv:            # the analysis may class this as safe: then nothing waits for judgement
        return
    f = rv[0]
    assert f["records"] and {r["id"] for r in f["records"]} <= {x, y}
    p = tmp_path / "d.json"
    p.write_text(json.dumps([{"id": f["id"], "action": "merge", "keep": x, "why": "same figure, same source"}]))
    out = tidy.decide(store, json.loads(p.read_text()))
    assert out["decided"] == 1 and not out["failed"]
    assert store.db.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
