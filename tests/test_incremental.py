def _obs(store, metric, key, value=54, unit="%", geo=None):
    d = store.document("Dealer survey 2026", url="https://s.org/dealers", published="2026-03")
    return store.add("observation", {"document_id": d, "metric": metric, "value": value, "unit": unit, "period": "2026",
                                     "geo": geo}, tags={"industry": ["automotive-retail"], "topic": ["ai-adoption"]},
                     origin=("p", "f.json", key))


def test_incremental_dedupe_finds_new_duplicate_of_old_item(store):
    with store.batch("importer:p"):
        _obs(store, "Share of dealers using generative AI tools weekly", "old")
    with store.batch("importer:maintenance"):
        store.dedupe(); store.set_maintenance_marker()
    since = store.maintenance_marker()
    with store.batch("importer:p"):
        _obs(store, "Dealers using generative AI tools weekly", "new")
    with store.batch("importer:maintenance"):
        merges = store.dedupe(since=since)
    assert len(merges) == 1     # the old item is outside the change set but in the same source group


def test_incremental_normalise_only_touches_changed_items(store):
    with store.batch("importer:p"):
        a = _obs(store, "Metric A", "a", geo="Singapore")
    store.db.execute("UPDATE observation SET unit = 'USD trillion' WHERE id = ?", (a,))   # simulate old raw data
    with store.batch("importer:maintenance"):
        store.set_maintenance_marker()
    since = store.maintenance_marker()
    with store.batch("importer:p"):
        b = _obs(store, "Metric B", "b", unit="percent")
    store.db.execute("UPDATE observation SET unit = 'percent' WHERE id = ?", (b,))
    store.log("observation", b, "update", {"unit": {"old": "%", "new": "percent"}})
    with store.batch("importer:maintenance"):
        res = store.normalise(since=since)
    units = dict(store.db.execute("SELECT id, unit FROM observation").fetchall())
    assert res["units"] == 1 and units[b] == "%" and units[a] == "USD trillion"   # a untouched in the incremental run
    with store.batch("importer:maintenance"):
        store.normalise()
    assert dict(store.db.execute("SELECT id, unit FROM observation").fetchall())[a] == "USD tn"
