import pytest

from fundus import norm_url


def test_norm_url_ignores_scheme_www_tracking_and_slash():
    assert norm_url("https://www.example.com/a/?utm_source=x&id=3#top") == norm_url("http://example.com/a?id=3")


def test_document_dedup_by_url_and_fill(store):
    with store.batch("t"):
        a = store.document("Report", url="https://www.x.org/r/", publisher="X Org")
        b = store.document("Report (again)", url="http://x.org/r", published="2024-05")
    assert a != b   # dated and undated citations of one page stay separate documents (order-independent)
    assert store.db.execute("SELECT count(DISTINCT url_key) FROM document").fetchone()[0] == 1


def test_add_is_idempotent_with_origin_and_logs_value_change(store):
    with store.batch("t"):
        d = store.document("S", url="https://s.org")
        store.series("s-adoption", "Share using AI", unit="%", publisher="S")
        o = dict(series_id="s-adoption", document_id=d, metric="Share using AI", value=40, unit="%", period="2024")
        i = store.add("observation", o, tags={"industry": ["healthcare"], "topic": ["ai-adoption"]},
                      origin=("proj", "f.json", "1"))
        j = store.add("observation", {**o, "value": 42}, origin=("proj", "f.json", "1"))
    assert i == j
    assert store.db.execute("SELECT value FROM observation WHERE id=?", (i,)).fetchone()[0] == 42
    assert store.db.execute("SELECT count(*) FROM changelog WHERE action='update'").fetchone()[0] == 1
    tags = {tuple(r) for r in store.db.execute("SELECT facet, value FROM tag WHERE item_id=?", (str(i),))}
    assert tags == {("industry", "healthcare"), ("topic", "ai-adoption"), ("project", "proj")}


def test_identical_item_from_other_project_is_reused(store):
    with store.batch("t"):
        q = dict(text="AI will not replace the advisor.", speaker="Jane Doe")
        a = store.add("quote", q, origin=("p1", "a.json", "q1"))
        b = store.add("quote", {**q, "text": "AI will not replace  the advisor. "}, origin=("p2", "b.json", "7"))
    assert a == b
    assert store.db.execute("SELECT count(*) FROM origin").fetchone()[0] == 2


def test_unknown_tag_value_is_rejected_until_registered(store):
    with store.batch("t"):
        with pytest.raises(ValueError):
            store.add("fact", {"kind": "tool", "title": "X"}, tags={"industry": ["space-mining"]})
        store.add_vocab("industry", "space-mining", "Space mining")
        store.add("fact", {"kind": "tool", "title": "X"}, tags={"industry": ["space-mining"]})


def test_search_index_finds_items_by_text_and_publisher(store):
    with store.batch("t"):
        d = store.document("Hospital AI survey", url="https://h.org", publisher="Health Org")
        store.add("observation", {"document_id": d, "metric": "Hospitals using ambient scribes", "value": 22, "unit": "%"})
    hits = store.db.execute("SELECT item_kind FROM search_index WHERE search_index MATCH 'scribes AND health'").fetchall()
    assert [h[0] for h in hits] == ["observation"]


def test_editions_on_one_landing_page_stay_separate(store):
    with store.batch("t"):
        a = store.document("CMO Survey Spring 2024", url="https://cmosurvey.org/results", published="2024-03")
        b = store.document("CMO Survey Spring 2025", url="https://cmosurvey.org/results", published="2025-03")
        c = store.document("CMO Survey results", url="https://cmosurvey.org/results/")
    assert a != b and c not in (a, b)   # undated, several editions: no guessing


def test_publisher_slug_drops_legal_suffix_and_parentheses(store):
    from fundus.store import publisher_slug
    assert publisher_slug("McKinsey & Company") == publisher_slug("McKinsey") == "mckinsey"
    assert publisher_slug("CDK Global (via MOTOR)") == "cdk-global"
    assert publisher_slug("Boston Consulting Group") == "boston-consulting-group"


def test_series_refresh_overwrites_and_logs(store):
    with store.batch("t"):
        store.series("s1", "Old name", update_hint="old hint")
        store.series("s1", "New name", update_hint="new hint")
        assert store.db.execute("SELECT update_hint FROM series").fetchone()[0] == "old hint"
        store.series("s1", "New name", refresh=True, update_hint="new hint")
    assert tuple(store.db.execute("SELECT name, update_hint FROM series").fetchone()) == ("New name", "new hint")
    assert store.db.execute("SELECT count(*) FROM changelog WHERE item_kind='series'").fetchone()[0] == 1


def test_merge_publisher_moves_documents_and_keeps_alias(store):
    with store.batch("t"):
        d = store.document("Pew report", url="https://pew.org/a", publisher="Pew")
        store.document("Pew report 2", url="https://pew.org/b", publisher="Pew Research Center")
        store.merge_publisher("pew", "pew-research-center")
        d3 = store.document("Pew report 3", url="https://pew.org/c", publisher="Pew")
    pubs = {r[0] for r in store.db.execute("SELECT DISTINCT publisher_id FROM document")}
    assert pubs == {"pew-research-center"} and d != d3


def test_facts_with_same_title_from_different_documents_stay_apart(store):
    with store.batch("t"):
        a = store.document("A", url="https://a.org")
        b = store.document("B", url="https://b.org")
        f1 = store.add("fact", {"kind": "event", "title": "Data breach", "document_id": a}, origin=("p", "x", "1"))
        f2 = store.add("fact", {"kind": "event", "title": "Data breach", "document_id": b}, origin=("p", "x", "2"))
    assert f1 != f2


def test_vendor_flag_upgrades_existing_document(store):
    with store.batch("t"):
        d = store.document("Vendor study", url="https://v.com/s")
        store.document("Vendor study", url="https://v.com/s", is_vendor=1)
    assert store.db.execute("SELECT is_vendor FROM document WHERE id=?", (d,)).fetchone()[0] == 1


def test_registered_alias_lands_on_canonical_publisher(store):
    with store.batch("t"):
        store.document("Press release", url="https://newsroom.acme.com/x", publisher="Acme Newsroom")
    assert tuple(store.db.execute("SELECT id, name FROM publisher").fetchone()) == ("acme", "Acme")


def test_reimport_does_not_downgrade_verified_status(store):
    with store.batch("importer:p"):
        i = store.add("quote", {"text": "Q", "status": "unverified"}, origin=("p", "f", "1"))
    with store.batch("mcp"):
        store.update("quote", i, {"status": "verified"})
    with store.batch("importer:p"):
        store.add("quote", {"text": "Q", "status": "unverified"}, origin=("p", "f", "1"))
    assert store.db.execute("SELECT status FROM quote").fetchone()[0] == "verified"


def test_manual_unverify_is_applied(store):
    with store.batch("mcp"):
        i = store.add("quote", {"text": "Q", "status": "verified"})
        store.update("quote", i, {"status": "unverified"})
    assert store.db.execute("SELECT status FROM quote").fetchone()[0] == "unverified"


def test_reimport_keeps_curated_dispute_and_internal(store):
    o = {"metric": "M", "value": 5, "status": "verified", "visibility": "public", "notes": "imp"}
    with store.batch("importer:p"):
        i = store.add("observation", o, origin=("p", "f", "1"))
    with store.batch("mcp"):
        store.update("observation", i, {"status": "disputed", "visibility": "internal", "notes": "50 not 5, p.12"})
    with store.batch("importer:p"):
        store.add("observation", {**o, "notes": "imp 2"}, origin=("p", "f", "1"))
    r = store.db.execute("SELECT status, visibility, notes FROM observation").fetchone()
    assert tuple(r) == ("disputed", "internal", "50 not 5, p.12")


def test_same_quote_from_other_source_does_not_rewrite_verified_source(store):
    with store.batch("t"):
        hb = store.document("Handelsblatt", url="https://hb.de/a")
        blog = store.document("Blog", url="https://blog.example/b")
        i = store.add("quote", {"text": "KI ersetzt den Berater nicht.", "speaker": "Anna Muster", "document_id": hb,
                                "status": "verified", "said_on": "2025-03"}, origin=("p", "f", "1"))
        j = store.add("quote", {"text": "KI ersetzt den  Berater nicht.", "speaker": "ANNA MUSTER", "document_id": blog,
                                "said_on": "2019", "notes": "seen on a blog"})
    assert i == j
    r = store.db.execute("SELECT document_id, said_on, status, notes FROM quote").fetchone()
    assert tuple(r) == (hb, "2025-03", "verified", "seen on a blog")


def test_observations_differing_in_population_stay_apart(store):
    with store.batch("t"):
        a = store.add("observation", {"metric": "Use AI", "value": 45, "period": "2025", "population": "dealers"})
        b = store.add("observation", {"metric": "Use AI", "value": 45, "period": "2025", "population": "consumers"})
    assert a != b


def test_editions_without_url_stay_apart_by_date(store):
    with store.batch("t"):
        a = store.document("CMO Survey", publisher="Duke", published="2023-02")
        b = store.document("CMO Survey", publisher="Duke", published="2024-02")
    assert a != b


def test_undated_document_on_shared_page_does_not_attach_to_oldest_edition(store):
    with store.batch("t"):
        a = store.document("CMO Survey Feb 2021", url="https://cmo.org/r", published="2021-02")
        store.document("CMO Survey Feb 2024", url="https://cmo.org/r", published="2024-02")
        c = store.document("CMO Survey Feb 2024 (summary)", url="https://cmo.org/r")
    assert c != a


def test_search_index_follows_document_changes(store):
    with store.batch("t"):
        d = store.document("Survey", url="https://x.org/s")
        store.add("quote", {"text": "Something", "document_id": d})
    with store.batch("t"):
        store.document("Survey", url="https://x.org/s", publisher="CDK Global")
    assert store.db.execute("SELECT count(*) FROM search_index WHERE search_index MATCH 'CDK'").fetchone()[0] == 1


def test_acme_se_and_acme_ag_are_different_publishers():
    from fundus.store import publisher_slug
    assert publisher_slug("Acme SE") != publisher_slug("Acme AG")


def test_same_edition_at_different_date_precision_is_one_document(store):
    with store.batch("t"):
        a = store.document("NADA Data 2022", url="https://x.com/nada.pdf", published="2023-05")
        b = store.document("NADA Data 2022", url="https://x.com/nada.pdf", published="2023")
    assert a == b


def test_document_matching_does_not_depend_on_order(store, tmp_path):
    from fundus import Store
    calls = [("T", "2024-01"), ("T", None), ("T2", "2025-01"), ("T", None)]
    results = []
    for order in (calls, list(reversed(calls))):
        st = Store(tmp_path / f"o{len(results)}.sqlite")
        with st.batch("t"):
            ids = {}
            for t, pub in order:
                ids[(t, pub)] = st.document(t, url="https://p.org/r", **({"published": pub} if pub else {}))
            again = {k: st.document(k[0], url="https://p.org/r", **({"published": k[1]} if k[1] else {})) for k in ids}
        assert again == ids
        results.append(st.db.execute("SELECT count(*) FROM document").fetchone()[0])
        st.close()
    assert results[0] == results[1]
