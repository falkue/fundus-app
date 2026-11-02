"""Entity layer: normalisation, gazetteer, linking roles, LLM enrichment (fake), query detection, fusion with
filters/visibility, incremental linking, merge/unmerge, context in get, the MCP tool and the eval mode."""
import json

import pytest

from fundus import embed
from fundus import entities as E
from fundus import query as q
from test_semantic import FakeEmbedder

HC = {"industry": ["healthcare"], "topic": ["genai"], "region": ["ch"]}


def ent(store, name, kind="organisation", aliases=(), source="manual"):
    """Entity with aliases, marked as already enriched."""
    eid = E.create_entity(store, kind, name, source, aliases=[(a, "abbrev" if E.is_abbrev(a) else "llm") for a in aliases])
    store.db.execute("UPDATE entity SET enriched_at = CURRENT_TIMESTAMP WHERE id = ?", (eid,))
    return eid


def links(store, kind, iid):
    return {(store.db.execute("SELECT name FROM entity WHERE id = ?", (e,)).fetchone()[0], r)
            for e, r in store.db.execute("SELECT entity_id, role FROM entity_link WHERE item_kind = ? AND item_id = ?",
                                         (kind, str(iid)))}


# ---------------------------------------------------------------- normalisation
def test_alias_normalisation():
    assert E.norm("Universitätsspital  Zürich") == "universitatsspital zurich"
    assert E.norm("McKinsey & Company") == "mckinsey and company"
    assert E.norm("Straße") == "strasse" and E.norm("L'Oréal") == "l oreal"
    assert E.norm("St. Gallen") == "st gallen"
    assert E.umlaut_variant("Universitätsspital Zürich") == "Universitaetsspital Zuerich"
    assert E.umlaut_variant("Basel") is None
    assert E.is_abbrev("USZ") and E.is_abbrev("3M") and not E.is_abbrev("Bain") and not E.is_abbrev("BoF")
    assert E.clean_name("AJMC (reporting CAQH Index 2025, released 19 Feb 2026)") == "AJMC"
    assert E.clean_name("Pinewood.AI: AI products (Connect, Assist)", cut_title=True) == "Pinewood.AI"
    assert E.initials_match("USZ", "Universitaetsspital Zuerich") and E.initials_match("USZ", "University Hospital Zurich")
    assert not E.initials_match("TPMG", "Kaiser Permanente")
    assert E.paren_abbrevs("Universitätsspital Basel (USB)") == ["USB"] and E.paren_abbrevs("Kaiser Permanente (TPMG)") == []


def test_acronym_definitions():
    defs = E.acronym_definitions("A trial at the University Hospital Zurich (USZ; co-author) and USZ (Universitaetsspital "
                                 "Zuerich) costs. Large language models (LLM) and Return on investment (ROI).")
    assert ("University Hospital Zurich", "USZ") in defs and ("Universitaetsspital Zuerich", "USZ") in defs
    assert not any(ab in ("LLM", "ROI") for _, ab in defs)


# ---------------------------------------------------------------- gazetteer
def test_gazetteer_word_boundaries_diacritics_abbreviations(store):
    usz = ent(store, "University Hospital Zurich", aliases=["Universitätsspital Zürich", "Unispital Zürich", "USZ"])
    bain = ent(store, "Bain & Company", aliases=["Bain"])
    ent(store, "Zurich Insurance Group", aliases=["Zurich"])
    a = ent(store, "Alpha Health", aliases=["AHX"])
    b = ent(store, "Alpha Housing", aliases=["AHX"])         # shared abbreviation → ambiguous, never used
    ent(store, "Jane Doe", kind="person", aliases=["Doe"])
    r = E.Resolver(store)

    def found(text, **kw):
        return [r.name[e] for e, _ in r.mentions(text, **kw)]
    assert found("Study at the Universitaetsspital Zuerich and the USZ.") == ["University Hospital Zurich"] * 2
    assert found("Projekte am Unispital Zürich") == ["University Hospital Zurich"]
    assert found("the usz said") == []                          # abbreviations only in upper case
    assert found("USZA and XUSZ") == []                         # word boundaries
    assert found("Bainbridge island; Bain says") == ["Bain & Company"]
    assert found("bain-marie") == []                            # single words need a capital letter
    assert found("hospitals in Zurich") == []                   # place names are never aliases on their own
    assert found("AHX reported") == []                          # ambiguous alias
    assert found("Doe said, Jane Doe said") == ["Jane Doe"]     # persons: full names only
    # longest match first: 'Bain & Company' as one entity, not 'Bain' + 'Company'
    assert r.mentions("Bain & Company") == [(bain, "bain and company")]
    assert {usz, a, b}


def test_query_detection(store):
    ent(store, "University Hospital Zurich", aliases=["Universitätsspital Zürich", "Unispital Zürich", "USZ"])
    ent(store, "Gartner")
    ent(store, "McKinsey & Company", aliases=["McKinsey"])
    store.db.commit()
    names = lambda t: [store.db.execute("SELECT name FROM entity WHERE id = ?", (e,)).fetchone()[0]
                       for e in q.detect_entities(store, t)]
    assert names("KI-Projekte am Unispital Zürich") == ["University Hospital Zurich"]
    assert names("ki-projekte am unispital zürich") == ["University Hospital Zurich"]      # lower-case multi-word
    assert names("What does McKinsey's State of AI survey say?") == ["McKinsey & Company"]
    assert names("Was sagt Gartners CMO-Umfrage?") == ["Gartner"]                           # genitive s
    assert names("private AI investment China 2024") == []


# ---------------------------------------------------------------- seeding + linking
@pytest.fixture
def archive(store):
    with store.batch("t"):
        d1 = store.document("State of AI 2025", url="https://mck.example/ai", publisher="McKinsey & Company", published="2025")
        d2 = store.document("Zurich hospital report", url="https://zh.example/r", publisher="NZZ", published="2025")
        obs = store.add("observation", {"metric": "Organisations using AI", "value": 88, "unit": "%", "period": "2025",
                                        "document_id": d1, "notes": "Underlying source: Stanford HAI"}, tags=HC)
        study = store.add("study", {"finding": "Ambient documentation trial at the University Hospital Zurich (USZ) cut "
                                    "note time", "document_id": d2}, tags=HC)
        quote = store.add("quote", {"text": "AI will change care", "speaker": "Dr. Anna Muster",
                                    "organisation": "Universitätsspital Zürich", "document_id": d2}, tags=HC)
        practice = store.add("practice", {"organisation": "Universitätsspital Zürich", "what": "Epic rollout with AI "
                                          "documentation (via NZZ)"}, tags=HC)
        tool = store.add("fact", {"kind": "tool", "title": "Pinewood.AI: AI products for dealers", "what": "DMS"},
                         tags={"industry": ["automotive-retail"], "topic": ["genai"]})
        secret = store.add("quote", {"text": "Internal: USZ pays too much", "speaker": "Client CFO",
                                     "organisation": "Secret Client AG", "visibility": "internal"},
                           tags=HC, origin=("client-x", "i.json", "1"))
        store.series("mck-ai", "Organisations using AI (McKinsey)", publisher="McKinsey & Company", unit="%")
    return {"obs": obs, "study": study, "quote": quote, "practice": practice, "tool": tool, "secret": secret}


def test_seed_and_link_roles(store, archive):
    res = E.build(store, use_llm=False)
    assert res["seed"]["publisher"] >= 2 and res["patterns"]["pending_created"] >= 1
    usz = E.find(store, "Universitätsspital Zürich")
    assert len(usz) == 1
    # the acronym definition 'University Hospital Zurich (USZ)' is pending until the LLM confirms it
    pending = store.db.execute("SELECT id FROM entity WHERE name = 'University Hospital Zurich'").fetchone()[0]
    assert pending in E.Resolver(store).pending
    assert ("McKinsey & Company", "publisher") in links(store, "observation", archive["obs"])
    assert ("McKinsey & Company", "publisher") in links(store, "series", "mck-ai")
    assert ("Stanford HAI", "via") not in links(store, "observation", archive["obs"])   # no such entity yet
    ql = links(store, "quote", archive["quote"])
    assert ("Universitätsspital Zürich", "organisation") in ql and ("Dr. Anna Muster", "speaker") in ql
    assert ("NZZ", "publisher") in ql
    assert ("Universitätsspital Zürich", "organisation") in links(store, "practice", archive["practice"])
    assert ("NZZ", "via") in links(store, "practice", archive["practice"])
    assert ("Pinewood.AI", "subject") in links(store, "fact", archive["tool"])
    person = store.db.execute("SELECT parent_id, description FROM entity WHERE name = 'Dr. Anna Muster'").fetchone()
    assert person[0] == usz[0]                               # affiliation from the quote's organisation


class FakeLLM:
    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    def complete(self, system, user):
        self.calls += 1
        out = []
        for inp in json.loads(user):
            a = self.answers.get(inp["name"], {"kind": "organisation", "canonical": inp["name"]})
            out.append({"i": inp["i"], "name": inp["name"], **a})
        return {"entities": out}


ANSWERS = {
    "Universitätsspital Zürich": {"kind": "organisation", "canonical": "University Hospital Zurich",
                                  "aliases": ["Universitätsspital Zürich", "Unispital Zürich"], "abbreviations": ["USZ"],
                                  "country": "CH"},
    "University Hospital Zurich": {"kind": "organisation", "canonical": "University Hospital Zurich",
                                   "abbreviations": ["USZ"]},
    "Dr. Anna Muster": {"kind": "person", "canonical": "Anna Muster"},
    "Client CFO": {"kind": "none"},
    "McKinsey & Company": {"kind": "organisation", "canonical": "McKinsey & Company", "aliases": ["McKinsey"]},
    "Secret Client AG": {"kind": "organisation", "canonical": "Secret Client"},
    "Pinewood.AI": {"kind": "product", "canonical": "Pinewood.AI", "parent": "Pinewood Technologies"},
}


def test_llm_enrichment_merge_and_cache(store, archive, tmp_path, monkeypatch):
    import sqlite3
    from fundus.store import Store
    monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "cache.jsonl"))
    with sqlite3.connect(tmp_path / "copy.sqlite") as c:      # same archive, for the rebuild below
        store.db.backup(c)
    llm = FakeLLM(ANSWERS)
    res = E.build(store, llm=llm)
    assert res["llm"]["llm_batches"] >= 1 and res["merged"] >= 1
    usz = E.find(store, "USZ")
    assert len(usz) == 1 and E.find(store, "Unispital Zürich") == usz == E.find(store, "Universitaetsspital Zuerich")
    name = store.db.execute("SELECT name FROM entity WHERE id = ?", (usz[0],)).fetchone()[0]
    assert name == "University Hospital Zurich"
    # the pending acronym entity was merged into the organisation; the merge is logged
    assert store.db.execute("SELECT count(*) FROM changelog WHERE item_kind = 'entity' AND action = 'merge'").fetchone()[0] >= 1
    # the study mentions 'University Hospital Zurich (USZ)' and is now linked
    assert ("University Hospital Zurich", "mentioned") in links(store, "study", archive["study"])
    assert not E.find(store, "Client CFO") and E.is_rejected(store, "Client CFO")
    parent = store.db.execute("SELECT p.name FROM entity e JOIN entity p ON p.id = e.parent_id WHERE e.name = 'Pinewood.AI'").fetchone()
    assert parent[0] == "Pinewood Technologies"
    assert not E.build(store, llm=FakeLLM(ANSWERS))["llm"].get("llm_batches")   # nothing pending, nothing re-seeded
    assert not E.find(store, "Client CFO")
    # rebuilding the same archive from scratch asks the LLM nothing (answers come from the cache)
    copy = Store(tmp_path / "copy.sqlite")
    llm2 = FakeLLM(ANSWERS)
    E.build(copy, llm=llm2)
    assert llm2.calls == 0 and E.find(copy, "Unispital Zürich") and not E.find(copy, "Client CFO")
    copy.close()


def test_llm_split_of_co_publication(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "cache.jsonl"))
    with store.batch("t"):
        d = store.document("Luxury study", url="https://bain.example/l", publisher="Bain & Company / Comité Colbert")
        o = store.add("observation", {"metric": "Luxury market", "value": 364, "unit": "EUR bn", "period": "2025",
                                      "document_id": d}, tags=HC)
    llm = FakeLLM({"Bain & Company / Comité Colbert": {"kind": "organisation", "canonical": None,
                                                        "parts": ["Bain & Company", "Comité Colbert"]}})
    E.build(store, llm=llm)
    assert links(store, "observation", o) == {("Bain & Company", "publisher"), ("Comité Colbert", "publisher")}
    # seeding again does not bring the co-publication back as an entity of its own
    with store.batch("t"):
        assert E.seed(store)["created"] == 0
    assert E.update(store)["pending_llm"] == 0


def test_capacity_rule_stops_enrichment(store, archive, tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "cache.jsonl"))

    class Busy(FakeLLM):
        def check_capacity(self):
            raise embed.EmbedUnavailable("ollama busy: a, b loaded")
    llm = Busy(ANSWERS)
    with store.batch("t"):
        E.seed(store)
    res = E.enrich(store, llm=llm)
    assert "busy" in res["stopped"] and llm.calls == 0


def test_merge_and_unmerge(store, archive):
    E.build(store, use_llm=False)
    a, b = E.find(store, "McKinsey & Company")[0], ent(store, "McK Research", aliases=["McK"])
    with store.batch("t"):
        store.db.execute("INSERT INTO entity_link(entity_id, item_kind, item_id, role, source) VALUES (?, 'study', ?, "
                         "'subject', 'manual')", (b, str(archive["study"])))
        E.merge(store, b, a)
    assert E.find(store, "McK") == [a] and ("McKinsey & Company", "subject") in links(store, "study", archive["study"])
    cid = store.db.execute("SELECT max(id) FROM changelog WHERE item_kind = 'entity' AND action = 'merge'").fetchone()[0]
    with store.batch("t"):
        E.unmerge(store, cid)
    assert E.find(store, "McK") == [b] and ("McK Research", "subject") in links(store, "study", archive["study"])
    assert ("McKinsey & Company", "subject") not in links(store, "study", archive["study"])


def test_incremental_linking(store, archive):
    first = E.update(store)
    assert first["link"]["items"] > 5
    with store.batch("t"):
        new = store.add("observation", {"metric": "McKinsey & Company finds 88% use AI", "value": 88, "unit": "%",
                                        "period": "2025"}, tags=HC)
    res = E.update(store)
    assert res["changed_items"] >= 1 and res["link"]["items"] <= 3
    assert ("McKinsey & Company", "mentioned") in links(store, "observation", new)
    # a new alias: items containing it (found through the full-text index) are linked without touching the others
    with store.batch("t"):
        eid = ent(store, "Epic Systems", aliases=["Epic"])
    res = E.update(store)
    assert res["alias_candidates"] >= 1 and ("Epic Systems", "mentioned") in links(store, "practice", archive["practice"])
    # a deduplicated (deleted) item loses its links
    with store.batch("t"):
        store._merge_item("observation", archive["obs"], new)
    E.update(store)
    assert not links(store, "observation", new)
    assert eid


# ---------------------------------------------------------------- search integration
@pytest.fixture
def searchable(store, archive, tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "cache.jsonl"))
    f = FakeEmbedder()
    embed.set_embedder(f)
    E.build(store, llm=FakeLLM(ANSWERS))
    embed.embed_pending(store)
    return archive


def keys(res):
    return [f"{r['kind']}:{r['id']}" for r in res["results"]]


def test_entity_ranking_in_hybrid_search(store, searchable):
    res = q.search(store, "KI-Projekte am Unispital Zürich")
    assert res["entities_detected"] == ["University Hospital Zurich"]
    top = keys(res)[:4]
    for k in ("study", "quote", "practice"):
        assert f"{k}:{searchable[k]}" in top
    assert any("University Hospital Zurich" in r.get("entities", []) for r in res["results"])
    off = q.search(store, "KI-Projekte am Unispital Zürich", entities=False)
    assert "entities_detected" not in off


def test_entity_ranking_respects_filters_and_visibility(store, searchable):
    secret = f"quote:{searchable['secret']}"
    for mode in ("hybrid", "semantic", "lexical"):
        assert secret not in keys(q.search(store, "USZ Universitätsspital Zürich pays", mode=mode, limit=50))
        assert secret not in keys(q.search(store, "", entity="USZ", mode=mode, limit=50))
    assert secret in keys(q.search(store, "USZ pays too much", include_internal=True, limit=50))
    # the internal-only client organisation is not detectable / profiled for normal callers
    with pytest.raises(LookupError):
        q.entity_profile(store, "Secret Client")
    assert q.entity_profile(store, "Secret Client", include_internal=True)["name"] == "Secret Client"
    # tag filters apply to the entity ranking as well
    res = q.search(store, "Unispital Zürich documentation", industry="automotive-retail", limit=50)
    assert all("healthcare" not in r.get("tags", {}).get("industry", []) for r in res["results"])
    # entity= filter in every mode
    for mode in ("hybrid", "semantic", "lexical"):
        got = set(keys(q.search(store, "AI", entity="Unispital Zürich", mode=mode, limit=50)))
        assert got and got <= {f"study:{searchable['study']}", f"quote:{searchable['quote']}",
                               f"practice:{searchable['practice']}"}
    with pytest.raises(ValueError):
        q.search(store, "AI", entity="No Such Org")


def test_get_related_and_profile(store, searchable):
    g = q.get(store, "quote", searchable["quote"])
    roles = {e["name"]: e["role"] for e in g["related"]["entities"]}
    assert roles["University Hospital Zurich"] == "organisation" and roles["Anna Muster"] == "speaker"
    same = g["related"]["same_entity"]
    assert same["entity"] == "University Hospital Zurich"
    assert {f"{i['kind']}:{i['id']}" for i in same["items"]} >= {f"practice:{searchable['practice']}"}
    assert f"quote:{searchable['secret']}" not in {f"{i['kind']}:{i['id']}" for i in same["items"]}
    st_rel = q.get(store, "study", searchable["study"])["related"]      # headline mention beats the publisher
    assert st_rel["same_entity"]["entity"] == "University Hospital Zurich"
    p = q.entity_profile(store, "USZ")
    assert p["name"] == "University Hospital Zurich" and "Unispital Zürich" in p["aliases"]
    assert p["counts"]["by_kind"]["quote"] == 1                  # the internal quote is not counted
    assert {"Anna Muster", "NZZ"} <= {c["name"] for c in p["co_occurring"]}
    assert "Secret Client" not in {c["name"] for c in p["co_occurring"]}


def test_series_revision_in_related(store):
    with store.batch("t"):
        d1 = store.document("Survey 2024", url="https://s.example/24", publisher="Pub", published="2024-05")
        d2 = store.document("Survey 2025", url="https://s.example/25", publisher="Pub", published="2025-05")
        store.series("s1", "Share using AI", unit="%")
        o1 = store.add("observation", {"series_id": "s1", "metric": "Share using AI", "value": 50, "unit": "%",
                                       "period": "2024", "document_id": d1}, tags=HC)
        store.add("observation", {"series_id": "s1", "metric": "Share using AI (revised)", "value": 52, "unit": "%",
                                  "period": "2024", "document_id": d2}, tags=HC)
        store.add("observation", {"series_id": "s1", "metric": "Share using AI", "value": 60, "unit": "%",
                                  "period": "2025", "document_id": d2}, tags=HC)
    rel = q.get(store, "observation", o1)["related"]
    assert rel["series_latest"]["period"] == "2025" and rel["series_latest"]["value"] == 60
    assert rel["revised_by_newer_edition"][0]["value"] == 52


def test_mcp_entity_tool_and_search_param(tmp_path, monkeypatch):
    from fundus import mcp_server as m
    from fundus.store import Store
    path = tmp_path / "mcp.sqlite"
    m.configure(path)
    try:
        doc = {"title": "Report", "url": "https://usz.example/r", "publisher": "Universitätsspital Zürich"}
        m.fundus_add("study", {"finding": "AI triage at the hospital cut waiting times"}, document=doc,
                     tags={"industry": ["healthcare"], "topic": ["genai"]})
        m.fundus_add("quote", {"text": "Secret plan", "organisation": "Universitätsspital Zürich", "visibility": "internal"},
                     tags={"industry": ["healthcare"], "topic": ["genai"]}, project="client-y")
        st = Store(path)
        monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "c.jsonl"))
        E.build(st, llm=FakeLLM(ANSWERS))
        st.close()
        prof = json.loads(m.fundus_entity("Unispital Zürich"))
        assert prof["name"] == "University Hospital Zurich" and prof["counts"]["by_kind"] == {"study": 1}
        assert prof["counts"]["by_role"] == {"publisher": 1}
        assert json.loads(m.fundus_entity("Unispital Zürich", project="client-y"))["counts"]["by_kind"] == {"study": 1, "quote": 1}
        assert "error" in json.loads(m.fundus_entity("Nobody Inc"))
        res = json.loads(m.fundus_search("", entity="USZ", mode="lexical"))
        assert [r["kind"] for r in res["results"]] == ["study"] and res["results"][0]["entities"] == ["University Hospital Zurich"]
        got = json.loads(m.fundus_get("study", res["results"][0]["id"]))
        assert got["related"]["entities"][0]["role"] == "publisher"
        assert m.fundus_entity in m.TOOLS
    finally:
        m.configure(m.DEFAULT_DB)


def test_eval_modes_and_intent_breakdown(store, searchable, tmp_path):
    from fundus import eval_search as ev
    data = {"queries": [{"id": "e1", "lang": "de", "intent": "organisation", "text": "KI-Projekte am Unispital Zürich",
                         "relevant": {f"study:{searchable['study']}": 2}},
                        {"id": "e2", "lang": "en", "intent": "concept", "text": "AI will change care",
                         "relevant": {f"quote:{searchable['quote']}": 2}}]}
    res = ev.evaluate(store, data, ("hybrid", "hybrid-noent"))
    assert set(res["modes"]["hybrid"]["by_intent"]) == {"organisation", "concept"}
    assert res["modes"]["hybrid"]["by_intent"]["organisation"]["ndcg"] is not None
    assert "per intent" in ev.format_report(res)


def test_llm_answers_are_validated(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_ENTITY_CACHE", str(tmp_path / "cache.jsonl"))
    assert E.plausible_rename("Universitätsspital Zürich", "University Hospital Zurich")
    assert E.plausible_rename("USZ", "Universitaetsspital Zuerich") and E.plausible_rename("Gartner Inc.", "Gartner")
    assert not E.plausible_rename("COPE", "ICMJE") and not E.plausible_rename("European Commission", "Swiss Academies")

    class Shifted(FakeLLM):     # answers shifted by one position: every echo mismatches → nothing applied
        def complete(self, system, user):
            self.calls += 1
            inp = json.loads(user)
            return {"entities": [{"i": x["i"], "name": inp[(n + 1) % len(inp)]["name"], "kind": "organisation",
                                  "canonical": inp[(n + 1) % len(inp)]["name"] + " Renamed"} for n, x in enumerate(inp)]}
    with store.batch("t"):
        for n in ("COPE", "ICMJE", "ALLEA"):
            store.publisher(n)
        E.seed(store)
    res = E.enrich(store, llm=Shifted({}))
    assert res["misaligned"] >= 3 and res["gave_up"] == 3
    assert {r[0] for r in store.db.execute("SELECT name FROM entity")} == {"COPE", "ICMJE", "ALLEA"}
    # an implausible canonical name is ignored
    with store.batch("t"):
        store.publisher("European Commission")
        E.seed(store)
    E.enrich(store, llm=FakeLLM({"European Commission": {"kind": "organisation", "canonical": "Swiss Academies"}}))
    assert E.find(store, "European Commission") and not E.find(store, "Swiss Academies")


def test_alias_tiers_and_short_forms(store):
    eid = ent(store, "Bain & Company")
    E.add_alias(store, eid, "Bain", "ambiguous")             # LLM: 'Bain' is also a surname
    j = ent(store, "Radiology Journal")
    E.add_alias(store, j, "Radiology", "common_word")
    usb = ent(store, "Universitätsspital Basel")
    r = E.Resolver(store)
    assert r.mentions("Bain says the market shrinks") == []                # not used for text mentions …
    assert [e for e, _ in r.mentions("Was sagt Bain zum Luxusmarkt?", query=True)] == [eid]   # … but in queries
    assert r.mentions("Radiology AI", query=True) == []
    ent(store, "trend")                                       # lower-case one-word brand: never matched
    r = E.Resolver(store)
    assert r.mentions("Welche Trends gibt es?", query=True) == [] and r.mentions("a new trend") == []
    assert E.short_variant("Universitätsspital Basel") == "Unispital Basel"
    assert E.short_variant("Universität Zürich") == "Uni Zürich" and E.short_variant("Universitätsklinikum Leipzig") == "Uniklinikum Leipzig"
    assert E.short_variant("University of Basel") is None
    assert E.find(store, "Unispital Basel") == [usb] and E.find(store, "Unispital Basel") == E.find(store, "Universitaetsspital Basel")


def test_word_like_abbreviation_spelled_out_otherwise():
    from fundus.entities import spelled_otherwise as f
    nice = "National Institute for Health and Care Excellence"
    # Zott & Amit's design themes are not the health institute
    assert f("Design themes (NICE) N Novelty Adopt L Lock-in Build C Complementarities Bundle E Efficiency Cut", "nice", nice)
    assert f("value drivers: novelty, lock-in, complementarities, efficiency (NICE).", "nice", nice)
    assert f("NICE: Novelty, Lock-in, Complementarities, Efficiency", "nice", nice)
    # the institute itself, plain uses and chance initials stay linked
    assert not f("NICE guidance for pancreatic cancer in the UK", "nice", nice)
    assert not f("the National Institute for Health and Care Excellence (NICE) says", "nice", nice)
    assert not f("WHO estimates with high output today", "who", "World Health Organization")
    assert not f("growth was highest overall WHO said", "who", "World Health Organization")
