def _obs(store, metric, key, series_id=None, value=54, **kw):
    d = store.document("Dealer survey 2026", url="https://s.org/dealers", published="2026-03")
    return store.add("observation", {"document_id": d, "metric": metric, "value": value, "unit": "%", "period": "2026-03",
                                     "series_id": series_id, **kw},
                     tags={"industry": ["automotive-retail"], "topic": ["ai-adoption"]}, origin=("p", "f.json", key))


def test_dedupe_folds_reworded_duplicate_and_reimport_stays_merged(store):
    with store.batch("importer:p"):
        a = _obs(store, "Share of dealers using generative AI tools weekly", "a")
        b = _obs(store, "Dealers using generative AI tools weekly", "b", status="verified")
    with store.batch("importer:maintenance"):
        merges = store.dedupe()
    assert len(merges) == 1
    assert store.db.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    kept = merges[0]["keep"]
    assert store.db.execute("SELECT count(*) FROM origin WHERE item_id=? AND merged=1", (str(kept),)).fetchone()[0] == 1
    with store.batch("importer:p"):   # nightly re-import of both source records
        _obs(store, "Share of dealers using generative AI tools weekly", "a")
        _obs(store, "Dealers using generative AI tools weekly", "b", status="verified")
    assert store.db.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    with store.batch("importer:maintenance"):
        assert store.dedupe() == []


def test_dedupe_keeps_genuinely_different_metrics(store):
    with store.batch("importer:p"):
        store.series("ch-outlet", "Market share by channel: outlet", unit="%")
        store.series("ch-specialty", "Market share by channel: specialty", unit="%")
        _obs(store, "Share of NEW-vehicle buyers completing all steps in person", "n")
        _obs(store, "Share of USED-vehicle buyers completing all steps in person", "u")
        _obs(store, "Share of dealers using generative AI tools weekly", "w")
        _obs(store, "Share of dealers using generative AI tools monthly", "m")
        _obs(store, "Channel share outlet", "o", series_id=None)
        _obs(store, "Market share by channel: outlet", "o2", series_id="ch-outlet")
        _obs(store, "Market share by channel: specialty", "s2", series_id="ch-specialty")
    with store.batch("importer:maintenance"):
        merges = store.dedupe()
    pairs = {(m["metric"], m["other"]) for m in merges}
    assert not any("NEW" in a or "USED" in a for a, _ in pairs)
    assert not any("weekly" in a and "monthly" in b or "monthly" in a and "weekly" in b for a, b in pairs)
    assert not any("specialty" in a + b and "outlet" in a + b for a, b in pairs)


def test_dedupe_never_merges_sibling_categories_or_subgroups(store):
    with store.batch("importer:p"):
        _obs(store, "Share of Nephrology physicians currently using AI in clinical practice", "neph", value=49)
        _obs(store, "Share of Gastroenterology physicians currently using AI in clinical practice", "gastro", value=49)
        _obs(store, "HCAHPS top-box score, composite: Communication with Nurses", "nurses", value=80)
        _obs(store, "HCAHPS top-box score, composite: Communication with Doctors", "doctors", value=80)
        _obs(store, "Share of US adults interacting with AI daily", "all", value=15, population="U.S. adults")
        _obs(store, "Share of US adults interacting with AI daily", "white", value=15, population="White")
        _obs(store, "Share of dealers using AI", "d1", value=82, population="US franchise dealership leaders, n=537")
        _obs(store, "Dealers using AI", "d2", value=82, population="Franchise dealership leaders")
    with store.batch("importer:maintenance"):
        merges = store.dedupe()
    assert [(m["metric"], m["other"]) for m in merges] == [("Share of dealers using AI", "Dealers using AI")]


def test_dedupe_keeps_subgroup_cells_of_one_figure(store):
    with store.batch("importer:p"):
        _obs(store, "Share saying regulation will go too far", "all", value=40, population="US adults",
             locator="Fig. 9.3.6, p. 384")
        _obs(store, "Share saying regulation will go too far", "hs", value=40, population="US adults: HS or less",
             locator="Fig. 9.3.6, p. 384")
        _obs(store, "Share saying regulation will go too far", "x", value=40, population="US adults overall",
             locator="Fig. 9.3.6, p. 384")
    with store.batch("importer:maintenance"):
        assert store.dedupe() == []


def test_dedupe_keeps_age_groups_apart(store):
    with store.batch("importer:p"):
        _obs(store, "Share not sure", "a", value=33, population="US adults: Age 30-49", locator="Fig. 9.3.6, p. 384")
        _obs(store, "Share not sure", "b", value=33, population="US adults: Age 65+", locator="Fig. 9.3.6, p. 384")
    with store.batch("importer:maintenance"):
        assert store.dedupe() == []
