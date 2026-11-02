"""Semantic / hybrid search, embedding storage, logging and the eval instrument – with a fake embedder."""
import hashlib
import json
import re
import time

import numpy as np
import pytest

from fundus import embed
from fundus import eval_search as ev
from fundus import query as q

SYN = {"spital": "hospital", "spitäler": "hospital", "spitaeler": "hospital", "krankenhaus": "hospital",
       "hospitals": "hospital", "sparen": "save", "saves": "save", "savings": "save", "cut": "save",
       "costs": "cost", "kosten": "cost", "ki": "ai", "genai": "generative", "händler": "dealer",
       "dealers": "dealer", "autohändler": "dealer", "money": "cost"}
STOP = {"figure", "value", "period", "source", "publisher", "tags", "quote", "speaker", "study", "finding", "fact",
        "what", "kind", "time", "series", "unit", "the", "of", "with", "by", "a", "in", "wie", "viel", "mit", "our"}


class FakeEmbedder:
    """Bag of (synonym-mapped) words hashed into `dim` buckets – 'multilingual' through SYN."""

    def __init__(self, model="fake", dim=256, delay=0.0, fail_query=False):
        self.model, self.dim, self.delay, self.fail_query = model, dim, delay, fail_query
        self.doc_calls = 0

    def _vec(self, text):
        v = np.zeros(self.dim, dtype=np.float32)
        for t in re.findall(r"\w+", text.lower()):
            t = SYN.get(t, t)
            if t in STOP or t.isdigit():
                continue
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % self.dim] += 1
        return v

    def embed_documents(self, texts):
        self.doc_calls += 1
        return embed.normalise([self._vec(t) for t in texts], self.dim)

    def embed_query(self, text, timeout=4.0):
        if self.fail_query:
            raise embed.EmbedUnavailable("ollama not reachable: connection refused")
        if self.delay:
            time.sleep(self.delay)
        return embed.normalise(self._vec(text), self.dim)[0]


@pytest.fixture
def fake():
    f = FakeEmbedder()
    embed.set_embedder(f)
    return f


@pytest.fixture
def items(store, fake):
    hc = {"industry": ["healthcare"], "topic": ["genai"], "region": ["us"]}
    with store.batch("t"):
        d = store.document("AI in hospital operations 2025", url="https://hc.example/r", publisher="Health Analytics Inc",
                           published="2025-03")
        hosp = store.add("observation", {"metric": "Hospitals cut costs with AI scheduling", "value": 12, "unit": "%",
                                         "period": "2025", "document_id": d, "status": "verified"}, tags=hc)
        dealer = store.add("observation", {"metric": "Dealers using AI", "value": 55, "unit": "%", "period": "2024"},
                           tags={"industry": ["automotive-retail"], "topic": ["ai-adoption"]})
        secret = store.add("quote", {"text": "Our hospital saves money with AI every day", "speaker": "Client CFO",
                                     "visibility": "internal"},
                           tags={"industry": ["healthcare"], "topic": ["genai"]}, origin=("client-x", "i.json", "1"))
        study = store.add("study", {"finding": "Generative AI raises productivity of hospital support staff",
                                    "design": "field study"}, tags=hc)
        fact = store.add("fact", {"kind": "regulation", "title": "EU AI Act", "what": "Risk-based AI regulation"},
                         tags={"industry": ["cross-industry"], "topic": ["regulation"], "region": ["europe"]})
        store.series("hosp-ai-cost", "Hospital cost savings from AI", unit="%")
    res = embed.embed_pending(store)
    assert res["embedded"] == 6 and res["activated"]
    return {"hosp": hosp, "dealer": dealer, "secret": secret, "study": study, "fact": fact}


def keys(res):
    return [f"{r['kind']}:{r['id']}" for r in res["results"]]


# ---------------------------------------------------------------- vectors / storage
def test_quantisation_roundtrip():
    v = embed.normalise(np.random.default_rng(0).standard_normal((3, 64)), 64)
    for quant in embed.QUANTS:
        back = embed.decode(embed.encode(v, quant), quant, 64)
        assert np.all(np.sum(back * v, axis=1) > 0.999)
    assert len(embed.bits(v)[0]) == 8
    assert embed.normalise(np.ones((1, 2048)), 1024).shape == (1, 1024)


def test_capacity_rule():
    assert embed.capacity_ok(["gemma4:26b"], "qwen3-embedding:8b", 2)
    assert embed.capacity_ok(["gemma4:26b", "qwen3-embedding:8b"], "qwen3-embedding:8b", 2)
    assert not embed.capacity_ok(["gemma4:26b", "llama3.1:70b"], "qwen3-embedding:8b", 2)
    assert embed.capacity_ok(["gemma4:26b", "llama3.1:70b"], "qwen3-embedding:8b")      # default: no limit


def test_schema_migrates_existing_db(tmp_path):
    from fundus import Store
    st = Store(tmp_path / "old.sqlite")
    st.db.executescript("DROP TABLE embedding; DROP TABLE embedding_space; DROP TABLE search_log;")
    st.close()
    st = Store(tmp_path / "old.sqlite")
    names = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master")}
    assert {"embedding", "embedding_space", "search_log", "ix_search_log_ts"} <= names
    st.close()


def test_item_text_has_context(items, store):
    ctx = embed._Context(store)
    row = store.db.execute("SELECT * FROM observation WHERE id = ?", (items["hosp"],)).fetchone()
    t = embed.item_text("observation", row, ctx)
    for part in ("Hospitals cut costs with AI scheduling", "12 %", "2025", "AI in hospital operations 2025",
                 "Health Analytics Inc", "Health care", "Generative AI"):
        assert part in t, part


def test_incremental_embedding_and_orphans(items, store, fake):
    space = embed.active_space(store)
    assert space.key == "fake|256|int8"
    assert embed.embed_pending(store)["embedded"] == 0
    with store.batch("t"):
        store.update("observation", items["dealer"], {"value": 60})                    # text changes → re-embed
        store.update("quote", items["secret"], {"notes": "not part of the embedded text"})
    r = embed.embed_pending(store)
    assert r["pending"] == 1 and r["embedded"] == 1
    with store.batch("t"):
        store.update("study", items["study"], {"visibility": "internal"})               # visibility → metadata refresh
    assert embed.embed_pending(store)["embedded"] == 1
    assert store.db.execute(f"SELECT internal FROM {space.vec_table} v JOIN embedding e ON e.id = v.rowid "
                            "WHERE e.item_kind = 'study'").fetchone()[0] == 1
    store.db.execute("DELETE FROM fact WHERE id = ?", (items["fact"],))
    store.db.commit()
    r = embed.embed_pending(store)
    assert r["removed"] == 1 and r["total_in_space"] == 5
    assert store.db.execute(f"SELECT count(*) FROM {space.vec_table}").fetchone()[0] == 5


def test_full_reembed_is_resumable(items, store, fake):
    r = embed.embed_pending(store, all_items=True, limit=2)
    assert r["embedded"] == 2 and r["pending"] == 6
    assert store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'embedding.reembed.%'").fetchone()
    r = embed.embed_pending(store)                                       # plain run continues the full re-embed
    assert r["pending"] == 4 and r["embedded"] == 4
    assert not store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'embedding.reembed.%'").fetchone()


def test_capacity_rule_stops_batches(store):
    class Busy(FakeEmbedder):
        def check_capacity(self):
            raise embed.EmbedUnavailable("ollama busy: gemma4:26b, llama3.1:70b loaded")
    with store.batch("t"):
        store.add("quote", {"text": "x"}, tags={"industry": ["retail"], "topic": ["ai"]})
    b = Busy()
    r = embed.embed_pending(store, embedder=b)
    assert r["embedded"] == 0 and "busy" in r["stopped"] and b.doc_calls == 0


def test_space_switch_and_drop(items, store):
    old = embed.active_space(store)
    r = embed.embed_pending(store, embedder=FakeEmbedder(model="fake2", dim=128))
    assert r["space"] == "fake2|128|int8" and r["embedded"] == 6 and "activated" not in r
    assert embed.active_space(store) == old                                # computed in parallel, not switched
    new = embed.Space("fake2", 128, "int8")
    embed.set_active(store, new)
    assert embed.active_space(store) == new
    with pytest.raises(ValueError):
        embed.drop_space(store, new)
    assert embed.drop_space(store, old)["vectors"] == 6
    st = embed.status(store)
    assert st["active"] == new.key and [s["space"] for s in st["spaces"]] == [new.key]
    with pytest.raises(LookupError):
        embed.set_active(store, old)


def test_rebuild_index(items, store):
    space = embed.active_space(store)
    store.db.execute(f"DELETE FROM {space.vec_table}")
    store.db.commit()
    assert embed.rebuild_index(store)["indexed"] == 6
    assert store.db.execute(f"SELECT count(*) FROM {space.vec_table}").fetchone()[0] == 6


# ---------------------------------------------------------------- search
def test_rrf():
    fused = q.rrf([["a", "b", "c"], ["c", "a"]])
    assert [k for k, _ in fused] == ["a", "c", "b"]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert [k for k, _ in q.rrf([["x"], ["y"]])] == ["x", "y"]            # tie → earlier ranking first
    assert [k for k, _ in q.rrf([["x", "y"], ["y"]], weights=[0.1, 1])] == ["y", "x"]


def test_semantic_finds_german_query(items, store):
    text = "Wie viel sparen Spitäler mit KI?"
    lex = q.search(store, text, mode="lexical")
    assert f"observation:{items['hosp']}" not in keys(lex)
    sem = q.search(store, text, mode="semantic")
    assert sem["mode"] == "semantic" and f"observation:{items['hosp']}" in keys(sem)[:2]
    hyb = q.search(store, text)
    assert hyb["mode"] == "hybrid" and f"observation:{items['hosp']}" in keys(hyb)[:2]
    hit = next(r for r in hyb["results"] if r["kind"] == "observation")
    assert hit["source"]["publisher"] == "Health Analytics Inc" and hit["value"] == 12        # same result format


def test_internal_never_leaks_through_semantic(items, store):
    secret = f"quote:{items['secret']}"
    for mode in q.SEARCH_MODES:
        for kw in ({}, {"kind": "quote"}, {"industry": "healthcare"}, {"project": "acme"},
                   {"visible_project": "other"}):
            assert secret not in keys(q.search(store, "hospital saves money AI", mode=mode, **kw)), (mode, kw)
    for mode in ("semantic", "hybrid"):
        assert secret in keys(q.search(store, "hospital saves money AI", mode=mode, project="client-x"))
        assert secret in keys(q.search(store, "hospital saves money AI", mode=mode, include_internal=True))
        assert secret in keys(q.search(store, "hospital saves money AI", mode=mode, visible_project="client-x"))


def test_internal_never_leaks_through_coarse_index(items, store, monkeypatch):
    monkeypatch.setattr(q, "EXACT_MAX", 0)                  # force the vec0 coarse path
    secret = f"quote:{items['secret']}"
    res = q.search(store, "hospital saves money AI", mode="semantic")
    assert res["count"] > 0 and secret not in keys(res)
    assert secret in keys(q.search(store, "hospital saves money AI", mode="semantic", project="client-x"))
    # a stale index entry (item turned internal after embedding) is still removed by the SQL post-filter
    with store.batch("t"):
        store.update("observation", items["hosp"], {"visibility": "internal"})
    assert f"observation:{items['hosp']}" not in keys(q.search(store, "hospital cost savings", mode="semantic"))


@pytest.mark.parametrize("exact_max", [20000, 0])
def test_filters_apply_to_semantic(items, store, monkeypatch, exact_max):
    monkeypatch.setattr(q, "EXACT_MAX", exact_max)
    t = "hospital AI costs"
    assert {r["kind"] for r in q.search(store, t, mode="semantic", kind="study")["results"]} == {"study"}
    assert keys(q.search(store, t, mode="semantic", industry="automotive")) == [f"observation:{items['dealer']}"]
    assert keys(q.search(store, t, mode="semantic", status="verified")) == [f"observation:{items['hosp']}"]
    assert keys(q.search(store, t, mode="semantic", region="europe")) == [f"fact:{items['fact']}"]
    got = keys(q.search(store, t, mode="semantic", kind="observation", since="2025"))
    assert got == [f"observation:{items['hosp']}"]
    assert q.search(store, t, mode="hybrid", kind="series")["results"][0]["id"] == "hosp-ai-cost"


def test_fallbacks(items, store, monkeypatch):
    embed.set_embedder(FakeEmbedder(fail_query=True))
    r = q.search(store, "dealers")
    assert r["mode"] == "lexical" and r["fallback"].startswith("error:") and r["count"] == 1
    assert q.search(store, "dealers", mode="semantic")["fallback"].startswith("error:")
    embed.set_embedder(FakeEmbedder(delay=0.2))
    monkeypatch.setattr(q, "SEM_TIMEOUT", 0.05)
    r = q.search(store, "dealers")
    assert r["fallback"] == "timeout" and r["mode"] == "lexical" and r["count"] == 1


def test_no_embeddings_is_plain_lexical(store, fake):
    with store.batch("t"):
        store.add("quote", {"text": "Scribes help"}, tags={"industry": ["healthcare"], "topic": ["ai"]})
    r = q.search(store, "scribes")
    assert r["fallback"] == "no_embeddings" and r["mode"] == "lexical" and r["count"] == 1


def test_empty_query_lists_newest_in_every_mode(items, store):
    for mode in q.SEARCH_MODES:
        r = q.search(store, "", mode=mode, kind="observation")
        assert r["match"] == "newest" and r["count"] == 2 and "fallback" not in r
    with pytest.raises(ValueError):
        q.search(store, "x", mode="fuzzy")


def test_search_log_from_mcp_and_cli(items, store, capsys):
    from fundus import cli
    from fundus import mcp_server as m
    store.db.commit()
    m.configure(store.path)
    try:
        out = json.loads(m.fundus_search("Spitäler sparen KI", mode="hybrid", industry="healthcare"))
        assert out["mode"] == "hybrid"
        json.loads(m.fundus_search("dealers", mode="lexical"))
    finally:
        m.configure(m.DEFAULT_DB)
    assert cli.main(["--db", str(store.path), "search", "EU", "AI", "Act", "--mode", "semantic"]) == 0
    capsys.readouterr()
    rows = store.db.execute("SELECT actor, query, mode, filters, n_results, top_ids, ms, fallback FROM search_log "
                            "ORDER BY id").fetchall()
    assert [(r[0], r[2]) for r in rows] == [("mcp", "hybrid"), ("mcp", "lexical"), ("cli", "semantic")]
    first = rows[0]
    assert first[1] == "Spitäler sparen KI" and json.loads(first[3]) == {"industry": "healthcare", "limit": 20}
    assert json.loads(first[5])[0] == out["results"][0]["kind"] + ":" + str(out["results"][0]["id"])
    assert first[4] == out["count"] and first[6] >= 0 and first[7] is None
    assert json.loads(rows[2][5])[0] == f"fact:{items['fact']}"
    store.db.execute("UPDATE search_log SET ts = '2020-01-01 00:00:00' WHERE id = 1")
    store.db.commit()
    assert embed.prune_search_log(store, 180) == 1


def test_internal_callers_stay_lexical(items, store, fake):
    calls = []
    orig = fake.embed_query
    fake.embed_query = lambda *a, **kw: calls.append(1) or orig(*a, **kw)
    q.find_series(store, "hospital")
    from fundus import mcp_server as m
    m.add_item(store, "quote", {"text": "AI in hospitals"}, tags={"industry": ["healthcare"], "topic": ["ai"]})
    assert calls == []


# ---------------------------------------------------------------- eval instrument
def test_metric_functions():
    rel = {"a": 2, "b": 1, "z": 0}
    ranked = ["x", "a", "y", "b"]
    assert ev.recall_at(ranked, rel, 2) == 0.5 and ev.recall_at(ranked, rel, 10) == 1.0
    assert ev.rr_at(ranked, rel, 10) == 0.5 and ev.rr_at(["x"], rel, 10) == 0.0
    ideal = 3 + 1 / np.log2(3)
    assert ev.ndcg_at(ranked, rel, 10) == pytest.approx((3 / np.log2(3) + 1 / np.log2(5)) / ideal)
    assert ev.ndcg_at(["a", "b"], rel, 10) == pytest.approx(1.0)
    assert ev.recall_at(ranked, {"z": 0}, 5) is None


def test_eval_report_and_unjudged(items, store, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"queries": [
        {"id": "de1", "lang": "de", "text": "Wie viel sparen Spitäler mit KI?", "intent": "hospital AI savings",
         "relevant": {f"observation:{items['hosp']}": 2, f"series:hosp-ai-cost": 1}},
        {"id": "en1", "lang": "en", "text": "dealers using AI", "filters": {"kind": "observation"},
         "relevant": {f"observation:{items['dealer']}": 2}},
        {"id": "en2", "lang": "en", "text": "zzzz nothing", "relevant": {}}]}))
    data = ev.load(f)
    res = ev.evaluate(store, data, ["lexical", "semantic", "hybrid"])
    lex, sem = res["modes"]["lexical"], res["modes"]["semantic"]
    assert lex["queries"] == 3 and lex["judged"] == 2
    assert lex["by_lang"]["de"]["recall@10"] == 0.0 and sem["by_lang"]["de"]["recall@10"] == 1.0
    assert lex["by_lang"]["en"]["mrr"] == 1.0
    assert lex["zero_hit_rate"] == pytest.approx(2 / 3, abs=1e-3)                     # German query + nonsense query
    assert {r["id"] for r in res["per_query"]} == {"de1", "en1", "en2"}
    text = ev.format_report(res)
    assert "hybrid" in text and "de1" in text
    un = ev.unjudged(store, data, ["semantic"])
    de = next(x for x in un["queries"] if x["id"] == "de1")
    assert f"observation:{items['hosp']}" not in [e["key"] for e in de["unjudged"]]
    assert de["unjudged"] and all(e["ranks"]["semantic"] >= 1 for e in de["unjudged"])
    assert "[de1]" in ev.format_unjudged(un)
    # search_log stays untouched by evaluation runs
    assert store.db.execute("SELECT count(*) FROM search_log").fetchone()[0] == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"queries": [{"id": "x", "text": "t", "relevant": {"quote:1": 3}}]}))
    with pytest.raises(ValueError):
        ev.load(bad)


def test_derive_space_and_eval_against_it(items, store):
    src = embed.active_space(store)
    dst = embed.Space(src.model, 128, "float16")
    r = embed.derive_space(store, src, dst)
    assert r["vectors"] == 6 and embed.active_space(store) == src
    res = q.search(store, "Spitäler sparen KI", mode="semantic", space=dst)
    assert res["mode"] == "semantic" and res["count"] > 0
    assert embed.embed_pending(store, space=dst, embedder=FakeEmbedder(dim=128))["embedded"] == 0   # hashes carried over
    with pytest.raises(ValueError):
        embed.derive_space(store, dst, embed.Space(src.model, 256, "int8"))
