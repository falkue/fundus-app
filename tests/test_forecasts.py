from datetime import date

import pytest

from fundus import forecasts as fc

TAGS = {"industry": ["retail"], "topic": ["market"]}


def _setup(store, forecast: dict, obs: list[tuple] = ()):
    with store.batch("t"):
        store.series("s", "Some metric", unit=forecast.get("unit"))
        for period, value, *cat in obs:
            store.add("observation", {"series_id": "s", "metric": "m", "period": period, "value": value,
                                      "unit": forecast.get("unit"), "category": cat[0] if cat else None}, tags=TAGS)
        return store.add("forecast", {"statement": "x", "series_id": "s", **forecast}, tags=TAGS)


def test_due_marks_reached_forecasts(store):
    with store.batch("t"):
        a = store.add("forecast", {"statement": "a", "target_period": "2025"}, tags=TAGS)
        b = store.add("forecast", {"statement": "b", "target_period": "2030"}, tags=TAGS)
        c = store.add("forecast", {"statement": "c", "target_period": "2026-Q3"}, tags=TAGS)
    res = fc.due(store, today=date(2026, 10, 24))                  # 2026-Q3 ended 30 Sep
    assert [r["id"] for r in res] == [a, c]
    outcomes = dict(store.db.execute("SELECT id, outcome FROM forecast").fetchall())
    assert outcomes == {a: "due", b: "open", c: "due"}
    assert store.db.execute("SELECT count(*) FROM changelog WHERE action='update'").fetchone()[0] == 2


def test_evaluate_point_value_relative(store):
    f = _setup(store, {"value": 100, "unit": "EUR bn", "target_period": "2025"}, [("2024", 80), ("2025", 108)])
    p = fc.evaluate(store, f)
    assert p["outcome"] == "hit" and p["actual_value"] == 108 and p["actual_period"] == "2025"


def test_evaluate_partly_and_miss(store):
    f = _setup(store, {"value": 100, "unit": "units", "target_period": "2025"}, [("2025", 120)])
    assert fc.evaluate(store, f)["outcome"] == "partly"
    with store.batch("t"):
        store.update("forecast", f, {"value": 200})
    assert fc.evaluate(store, f)["outcome"] == "miss"


def test_evaluate_percentage_points(store):
    f = _setup(store, {"value": 70, "unit": "%", "target_period": "2026"}, [("2026", 67.5)])
    p = fc.evaluate(store, f)
    assert p["outcome"] == "partly" and "percentage points" in p["note"]
    with store.batch("t"):
        store.update("forecast", f, {"value": 69})
    assert fc.evaluate(store, f)["outcome"] == "hit"


def test_evaluate_range_exact(store):
    f = _setup(store, {"value_low": 30, "value_high": 40, "unit": "%", "target_period": "2025"},
               [("2024", 35), ("2025", 38), ("2026", 90)])
    p = fc.evaluate(store, f)
    assert p["outcome"] == "hit" and p["actual_period"] == "2025" and p["auto_apply"] is True


def test_nearest_later_period_same_granularity_is_flagged(store):
    f = _setup(store, {"value_low": 30, "value_high": 40, "unit": "%", "target_period": "2025"},
               [("2024", 35), ("2026-03", 38), ("2027", 39)])
    p = fc.evaluate(store, f)
    assert p["actual_period"] == "2027" and p["auto_apply"] is False
    assert p["flags"][0].startswith("nearest_later_period")


def test_evaluate_prefers_uncategorised_value(store):
    f = _setup(store, {"value": 50, "unit": "%", "target_period": "2025"}, [("2025", 10, "small firms"), ("2025", 51)])
    assert fc.evaluate(store, f)["actual_value"] == 51


def test_evaluate_without_actual_is_none(store):
    f = _setup(store, {"value": 50, "unit": "%", "target_period": "2030"}, [("2025", 40)])
    assert fc.evaluate(store, f) is None
    with store.batch("t"):
        g = store.add("forecast", {"statement": "no series", "value": 1, "target_period": "2020"}, tags=TAGS)
    assert fc.evaluate(store, g) is None


def test_resolve_writes_and_logs(store):
    f = _setup(store, {"value": 70, "unit": "%", "target_period": "2026"}, [("2026", 60)])
    oid = store.db.execute("SELECT id FROM observation").fetchone()[0]
    fc.resolve(store, f, "miss", observation_id=oid, note="checked", today=date(2026, 12, 1))
    r = store.db.execute("SELECT * FROM forecast WHERE id=?", (f,)).fetchone()
    assert (r["outcome"], r["actual_value"], r["actual_observation_id"], r["resolved_at"], r["resolution_note"]) == \
        ("miss", 60, oid, "2026-12-01", "checked")
    assert store.db.execute("SELECT count(*) FROM changelog WHERE action='resolve' AND item_id=?", (str(f),)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        fc.resolve(store, f, "great")


def test_review_proposes_but_applies_only_with_flag(store):
    f = _setup(store, {"value": 70, "unit": "%", "target_period": "2026"}, [("2026", 71)])
    assert fc.review(store, today=date(2026, 10, 24)) == []          # 2026 not over yet
    res = fc.review(store, today=date(2027, 1, 15))
    assert res[0]["proposal"]["outcome"] == "hit" and "applied" not in res[0]
    assert store.db.execute("SELECT outcome FROM forecast WHERE id=?", (f,)).fetchone()[0] == "due"
    res = fc.review(store, today=date(2027, 1, 15), apply=True)
    assert res[0]["applied"] is True
    assert store.db.execute("SELECT outcome FROM forecast WHERE id=?", (f,)).fetchone()[0] == "hit"


# ---------------------------------------------------------------- regression tests (review findings)
def test_year_forecast_not_due_before_year_end_and_ignores_january_value(store):
    f = _setup(store, {"value": 70, "unit": "%", "target_period": "2026"}, [("2026-01", 70)])
    assert fc.due(store, today=date(2026, 6, 1)) == []
    assert fc.due(store, today=date(2026, 12, 31)) == []
    assert [r["id"] for r in fc.due(store, today=date(2027, 1, 1))] == [f]
    assert fc.evaluate(store, f) is None                            # a January value is no annual actual


def test_last_subperiod_value_is_flagged_not_applied(store):
    f = _setup(store, {"value": 70, "unit": "%", "target_period": "2026"}, [("2026-01", 10), ("2026-12", 71)])
    p = fc.evaluate(store, f)
    assert p["actual_period"] == "2026-12" and p["auto_apply"] is False and p["flags"][0].startswith("sub_period")
    res = fc.review(store, today=date(2027, 2, 1), apply=True)
    assert res[0]["applied"] is False
    assert store.db.execute("SELECT outcome, actual_value FROM forecast WHERE id=?", (f,)).fetchone()[:] == ("due", None)


def test_range_targets_count_by_their_end(store):
    with store.batch("t"):
        a = store.add("forecast", {"statement": "a", "target_period": "2026-2030"}, tags=TAGS)
        b = store.add("forecast", {"statement": "b", "target_period": "2025-11/12"}, tags=TAGS)
    assert [r["id"] for r in fc.due(store, today=date(2026, 1, 5))] == [b]
    assert [r["id"] for r in fc.due(store, today=date(2031, 1, 1))] == [b, a]
    g = _setup(store, {"statement": "range", "value": 50, "unit": "%", "target_period": "2026-2030"}, [("2026", 10), ("2030", 51)])
    assert fc.evaluate(store, g)["actual_period"] == "2030"


def test_unit_mismatch_and_category_only_not_auto_applied(store):
    f = _setup(store, {"value": 100, "unit": "EUR bn", "target_period": "2025"}, [("2025", 101)])
    with store.batch("t"):
        store.update("observation", 1, {"unit": "USD bn"})
    p = fc.evaluate(store, f)
    assert p["auto_apply"] is False and any(x.startswith("unit_mismatch") for x in p["flags"])
    g = _setup(store, {"statement": "second", "value": 50, "unit": "%", "target_period": "2024"},
               [("2024", 49, "SMEs")])
    p = fc.evaluate(store, g)
    assert p["auto_apply"] is False and any(x.startswith("category_only") for x in p["flags"])
    res = fc.review(store, today=date(2026, 10, 24), apply=True)
    assert not any(e.get("applied") for e in res)


def test_internal_observation_never_resolves_public_forecast(store):
    with store.batch("t"):
        store.series("s", "Metric", unit="%")
        store.add("observation", {"series_id": "s", "metric": "m", "period": "2025", "value": 60, "unit": "%",
                                  "visibility": "internal"}, tags=TAGS, origin=("client", "x.json", "1"))
        pub = store.add("forecast", {"statement": "public", "series_id": "s", "value": 60, "unit": "%",
                                     "target_period": "2025"}, tags=TAGS)
        intern = store.add("forecast", {"statement": "internal", "series_id": "s", "value": 60, "unit": "%",
                                        "target_period": "2025", "visibility": "internal"},
                           tags=TAGS, origin=("client", "x.json", "f1"))
    assert fc.evaluate(store, pub) is None
    assert fc.evaluate(store, intern)["actual_value"] == 60          # same project → usable
    res = fc.review(store, today=date(2026, 10, 24), apply=True)
    applied = {e["forecast"]["id"]: e.get("applied") for e in res}
    assert applied == {pub: None, intern: True}
    assert store.db.execute("SELECT actual_value FROM forecast WHERE id=?", (pub,)).fetchone()[0] is None
    with pytest.raises(PermissionError):
        fc.resolve(store, pub, "hit", observation_id=1)


def test_resolve_refuses_internal_forecast_and_reopen_clears(store):
    with store.batch("t"):
        f = store.add("forecast", {"statement": "x", "target_period": "2025", "visibility": "internal"},
                      tags=TAGS, origin=("client", "f.json", "1"))
    with pytest.raises(PermissionError):
        fc.resolve(store, f, "miss", actual_value=3)
    with pytest.raises(PermissionError):
        fc.resolve(store, f, "miss", actual_value=3, project="other")
    fc.resolve(store, f, "miss", actual_value=3, note="n", project="client")
    fc.resolve(store, f, "open", project="client")
    r = store.db.execute("SELECT * FROM forecast WHERE id=?", (f,)).fetchone()
    assert (r["outcome"], r["actual_value"], r["actual_observation_id"], r["resolution_note"], r["resolved_at"]) == \
        ("open", None, None, None, None)
