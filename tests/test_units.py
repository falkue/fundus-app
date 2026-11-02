import pytest

from fundus.units import conversion, normalise_unit


@pytest.mark.parametrize("raw,norm", [
    ("USD trn", "USD tn"), ("USD trillion", "USD tn"), ("USD tn", "USD tn"), ("EUR tn", "EUR tn"),
    ("USD million", "USD m"), ("USD m", "USD m"), ("$bn", "USD bn"), ("US$ m", "USD m"), ("€ billion", "EUR bn"),
    ("CHF million per year", "CHF m per year"), ("Mio. CHF pro Jahr", "CHF m per year"), ("bn EUR", "EUR bn"),
    ("percent", "%"), ("pct", "%"), ("%", "%"), ("percentage points", "pp"), ("Prozentpunkte", "pp"),
    ("million", "m"), ("m users", "m users"), ("million units", "m units"), ("thousand per day", "k per day"),
    ("% of time", "% of time"), ("count", "count"), ("days", "days"), ("index points", "index points"),
    ("USD", "USD"), ("  USD   bn ", "USD bn"), ("score (1-7)", "score (1-7)"), ("minutes", "minutes"),
])
def test_normalise_unit(raw, norm):
    assert normalise_unit(raw) == norm


def test_conversion_only_between_scales_of_the_same_base():
    assert conversion("USD m", "USD bn") == pytest.approx(0.001)
    assert conversion("USD bn", "USD tn") == pytest.approx(0.001)
    assert conversion("m users", "bn users") == pytest.approx(0.001)
    assert conversion("USD m", "EUR m") is None
    assert conversion("%", "pp") is None
    assert conversion("USD m", "USD m") is None


def test_add_normalises_and_converts_into_series_unit(store):
    with store.batch("t"):
        store.series("market", "Market size", unit="USD billion")
        i = store.add("observation", {"series_id": "market", "metric": "Market size", "value": 1500, "unit": "USD million",
                                      "period": "2024"})
        j = store.add("observation", {"series_id": "market", "metric": "Market size", "value": 2, "unit": "EUR bn",
                                      "period": "2025"})
    r = store.db.execute("SELECT value, unit, notes FROM observation WHERE id=?", (i,)).fetchone()
    assert (r["value"], r["unit"]) == (1.5, "USD bn") and "Converted from 1500 USD m" in r["notes"]
    assert store.db.execute("SELECT unit FROM series").fetchone()[0] == "USD bn"
    assert store.db.execute("SELECT unit FROM observation WHERE id=?", (j,)).fetchone()[0] == "EUR bn"
    assert any("differs" in w for w in store.warnings)


def test_conversion_is_idempotent_on_reimport(store):
    o = {"series_id": "m", "metric": "M", "value": 1500, "unit": "USD m", "period": "2024"}
    with store.batch("importer:p"):
        store.series("m", "M", unit="USD bn")
        store.add("observation", o, origin=("p", "f", "1"))
        store.add("observation", o, origin=("p", "f", "1"))
    assert store.db.execute("SELECT count(*), max(value) FROM observation").fetchone()[:] == (1, 1.5)
    assert store.db.execute("SELECT count(*) FROM changelog WHERE action='update'").fetchone()[0] == 0


def test_normalise_fixes_existing_rows(store):
    store.db.execute("INSERT INTO series(id, name, unit) VALUES ('s', 'S', 'USD trillion')")
    store.db.execute("INSERT INTO observation(metric, value, unit, series_id) VALUES ('S', 1200, 'USD bn', 's')")
    with store.batch("importer:maintenance"):
        res = store.normalise()
    assert res["units"] == 1 and res["converted"] == 1
    assert tuple(store.db.execute("SELECT value, unit FROM observation").fetchone()) == (1.2, "USD tn")
    with store.batch("importer:maintenance"):
        assert store.normalise() == {"units": 0, "converted": 0, "publisher_types": 0, "region_tags": 0}
