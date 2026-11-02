"""Pooling pass: `nonrelevant` keeps judged-not-relevant items out of --unjudged without touching the metrics."""
import json

import pytest

from fundus import eval_search as ev


@pytest.fixture
def three(store):
    with store.batch("t"):
        a = store.add("observation", {"metric": "Hospitals saving costs with AI", "value": 12, "unit": "%", "period": "2025"})
        b = store.add("observation", {"metric": "Hospital AI cost projects", "value": 3, "unit": "%", "period": "2024"})
        c = store.add("observation", {"metric": "Hospital AI cost anecdotes", "value": 1, "unit": "%", "period": "2023"})
    return a, b, c


def _data(tmp_path, **extra):
    f = tmp_path / "e.json"
    qu = {"id": "q1", "lang": "en", "text": "hospital AI cost", "relevant": {}} | extra
    f.write_text(json.dumps({"queries": [qu]}))
    return ev.load(f)


def _unjudged_keys(store, data):
    un = ev.unjudged(store, data, ["lexical"])
    return {e["key"] for e in un["queries"][0]["unjudged"]}


def test_nonrelevant_skipped_in_unjudged_metrics_unchanged(store, three, tmp_path):
    a, b, c = three
    ka, kb, kc = (f"observation:{x}" for x in (a, b, c))
    base = _data(tmp_path, relevant={ka: 2})
    assert _unjudged_keys(store, base) == {kb, kc}

    with_nr = _data(tmp_path, relevant={ka: 2}, nonrelevant=[kb])
    assert _unjudged_keys(store, with_nr) == {kc}
    assert _unjudged_keys(store, _data(tmp_path, relevant={ka: 2}, nonrelevant=[kb, kc])) == set()

    # nonrelevant counts as grade 0: metrics are identical with and without the list
    m1 = ev.evaluate(store, base, ["lexical"])["modes"]["lexical"]
    m2 = ev.evaluate(store, with_nr, ["lexical"])["modes"]["lexical"]
    timing = {"mean_ms", "p95_ms"}
    strip = lambda m: {k: v for k, v in m.items() if k not in timing and k != "by_lang"}
    assert strip(m1) == strip(m2) and m1["ndcg"] is not None   # latency differs run to run
    # a query judged only through `nonrelevant` has no positives and stays out of the quality metrics
    only_nr = _data(tmp_path, nonrelevant=[ka])
    assert ev.evaluate(store, only_nr, ["lexical"])["modes"]["lexical"]["judged"] == 0


def test_nonrelevant_validation(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"queries": [{"id": "x", "text": "t", "nonrelevant": ["no-colon"]}]}))
    with pytest.raises(ValueError):
        ev.load(f)
    f.write_text(json.dumps({"queries": [{"id": "x", "text": "t", "nonrelevant": "observation:1"}]}))
    with pytest.raises(ValueError):
        ev.load(f)
