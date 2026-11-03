"""Research registry (fundus.research) and the importer of the analysis series (fundus.research_import)."""
import json
import sqlite3
from pathlib import Path

import pytest

from fundus import research as rs
from fundus import research_import as ri

FIXTURE = Path(__file__).parent / "fixtures" / "research"
PREREG = {"measures": "share of vertical firms", "data": "dataset:1", "method": "logit with function FE",
          "decision_rule": "supported if the CI excludes 0 upwards; not supported if below 0; else inconclusive"}


def _series(store):
    return rs.create_series(store, "serie-8", "Digital innovation", project="innovation-data",
                            theory_cards=[{"id": "A", "name": "Layered modular architecture", "claim": "layers decouple",
                                           "scholars": [{"name": "Yoo, Henfridsson & Lyytinen",
                                                         "works": [{"title": "ISR 2010", "doi": "10.1287/isre.1100.0322"}]}]}])


def _dataset(store, did=1, versions=1):
    now = "2026-11-01 08:00:00"
    store.db.execute("INSERT INTO dataset(id, slug, title, created_at, updated_at, current_version, visibility) VALUES (?,?,?,?,?,?,?)",
                     (did, f"ds-{did}", f"Dataset {did}", now, now, versions, "public"))
    for n in range(1, versions + 1):
        store.db.execute("INSERT INTO dataset_version(dataset_id, n, created_at) VALUES (?,?,?)", (did, n, f"2026-11-0{n} 08:00:00"))
    store.db.commit()


def test_prereg_required_fields_freeze_and_immutability(store):
    _series(store)
    h = rs.create_hypothesis(store, "Open-weight releases widen the scope of new wrapper firms", series="serie-8",
                             direction="+", theory_refs=["A"])
    assert h["code"] == "H8.1" and h["status"] == "idea" and h["visibility"] == "internal"
    with pytest.raises(ValueError, match="missing: measures, data, method, decision_rule"):
        rs.preregister(store, h["id"])
    h = rs.preregister(store, "H8.1", PREREG)
    p = h["prereg"]
    assert h["status"] == "preregistered" and p["frozen"] and p["sha_ok"] and len(p["sha256"]) == 64
    assert p["data"] == [{"ref": "dataset:1"}] and p["statement"].startswith("Open-weight")
    # nothing frozen can be overwritten – not through the API, not through SQL
    with pytest.raises(PermissionError):
        rs.preregister(store, h["id"], {"method": "OLS"})
    with pytest.raises(PermissionError):
        rs.save_prereg_draft(store, h["id"], {"method": "OLS"})
    with pytest.raises(PermissionError):
        rs.update_hypothesis(store, h["id"], {"statement": "something else"})
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        store.db.execute("UPDATE hypothesis_prereg SET method = 'OLS' WHERE hypothesis_id = ?", (h["id"],))
    with pytest.raises(sqlite3.DatabaseError, match="frozen"):
        store.db.execute("DELETE FROM hypothesis_prereg WHERE hypothesis_id = ?", (h["id"],))
    # changes are deviations: appended, with timing; the frozen fields stay
    with pytest.raises(ValueError):
        rs.add_deviation(store, h["id"], "method", "negative binomial", reason="", timing="before_analysis")
    with pytest.raises(ValueError):
        rs.add_deviation(store, h["id"], "method", "negative binomial", reason="overdispersion", timing="later")
    h = rs.add_deviation(store, h["id"], "method", "negative binomial", reason="overdispersion", timing="before_analysis")
    d = h["deviations"][0]
    assert d["before"] == PREREG["method"] and d["after"] == "negative binomial" and d["timing"] == "before_analysis"
    assert h["prereg"]["method"] == PREREG["method"] and h["prereg"]["effective"]["method"] == "negative binomial"
    assert h["prereg"]["sha_ok"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("UPDATE hypothesis_deviation SET reason = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.execute("DELETE FROM hypothesis_deviation")
    h = rs.add_deviation(store, h["id"], "sample", "batches 2016+", reason="data gap seen", timing="after_data_seen")
    assert "deviation_after_data" in [f["flag"] for f in h["flags"]]
    # editable outside the frozen part
    h = rs.update_hypothesis(store, h["id"], {"title": "Layer opening and scope"})
    assert h["title"] == "Layer opening and scope"


def test_results_verdicts_and_interpretation(store):
    _series(store)
    h = rs.create_hypothesis(store, "Statement", series="serie-8", direction="−")
    with pytest.raises(PermissionError, match="not pre-registered"):
        rs.add_result(store, h["id"], "supported")
    rs.preregister(store, h["id"], PREREG)
    rs.start(store, h["id"])
    with pytest.raises(ValueError):
        rs.add_result(store, h["id"], "maybe")
    h = rs.add_result(store, h["id"], "not supported", effect="b = 0.4", estimate=0.4, ci_low=0.1, ci_high=0.7, n="1200",
                      inputs=["dataset:99"])
    r = h["current_result"]
    assert h["status"] == "result" and r["verdict"] == "not_supported" and r["n"] == 1200
    assert r["inputs"] == [{"ref": "dataset:99", "missing": True}]
    assert h["interpretation_state"] == "missing" and "interpretation_missing" in [f["flag"] for f in h["flags"]]
    # a rerun adds a version; the newest full run is current
    h = rs.add_result(store, h["id"], "partially supported", effect="b = 0.1")
    assert len(h["results"]) == 2 and h["current_result"]["verdict"] == "partly_supported"
    rid = h["current_result"]["id"]
    with pytest.raises(PermissionError):
        rs.set_interpretation(store, "result", rid, {"summary": "x"}, author="gemma4:26b")
    out = rs.set_interpretation(store, "result", rid, {"summary": "Das Ergebnis …", "limits": "kleine Stichprobe"}, author="Falk")
    assert out["state"] == "incomplete"
    assert rs.get_hypothesis(store, h["id"])["interpretation_state"] == "incomplete"
    assert rs.worklist(store)["counts"]["interpretation_missing"] == 1
    out = rs.set_interpretation(store, "result", rid, {"summary": "a", "explanation": "b", "limits": "c", "theory": "d"},
                                author="Falk")
    assert out["state"] == "complete"
    g = rs.get_hypothesis(store, h["id"])
    assert g["interpretation_state"] == "complete" and len(g["current_result"]["interpretation"]["versions"]) == 2
    assert rs.worklist(store)["counts"]["interpretation_missing"] == 0
    for v in ("supported", "inconclusive"):
        hh = rs.create_hypothesis(store, f"Statement {v}", series="serie-8", direction="+")
        rs.preregister(store, hh["id"], PREREG)
        rs.add_result(store, hh["id"], v)
    b = next(x for x in rs.board(store) if x["slug"] == "serie-8")
    assert b["verdicts"] == {"supported": 1, "partly_supported": 1, "not_supported": 0, "inconclusive": 1}
    assert b["counts"]["result"] == 3
    # a sample run alone leaves the hypothesis running
    hs = rs.create_hypothesis(store, "Sample only", series="serie-8", direction="+")
    rs.preregister(store, hs["id"], PREREG)
    hs = rs.add_result(store, hs["id"], "inconclusive", sample_run=True)
    assert hs["status"] == "running" and "sample_only" in [f["flag"] for f in hs["flags"]]


def test_findings_stay_exploratory_and_derive_hypothesis(store):
    _series(store)
    with pytest.raises(ValueError, match="exploratory"):
        rs.create_finding(store, "Nurses keep paper notes", series="serie-8", verdict="supported")
    f = rs.create_finding(store, "Nurses keep paper notes next to the EHR", series="serie-8", strength="recurring_pattern",
                          context={"setting": "Spital", "sample": "12 Interviews"}, method="interviews, coding")
    assert f["code"] == "F8.1" and f["exploratory"] is True and f["visibility"] == "internal"
    with pytest.raises(ValueError, match="speaker_role"):
        rs.add_evidence(store, f["ref"], "quote", quote="Wir schreiben es auf.")
    rs.add_evidence(store, f["ref"], "quote", quote="Wir schreiben es auf.", speaker_role="Pflegefachperson, Spital A")
    rs.add_evidence(store, f["ref"], "photo", ref="photo:12", note="Notizzettel am Bildschirm")
    with pytest.raises(ValueError):
        rs.update_finding(store, f["id"], {"verdict": "supported"})
    with pytest.raises(ValueError):
        rs.update_finding(store, f["id"], {"status": "confirmed"})
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE finding SET exploratory = 0 WHERE id = ?", (f["id"],))
    store.db.rollback()
    h = rs.derive_hypothesis(store, f["ref"], "Paper notes persist where the EHR is slow", direction="+")
    assert h["status"] == "idea" and h["derived_from_finding"]["code"] == "F8.1"
    assert h["derived_from_finding"]["label"] == "entstanden aus Finding F8.1"
    assert any(l["relation"] == "derived_from" and l["other"]["ref"] == f["ref"] for l in h["links"])
    g = rs.get_finding(store, f["id"])
    assert g["status"] == "recorded" and g["derived_hypotheses"][0]["code"] == h["code"] and len(g["evidence"]) == 2
    assert g["interpretation"]["state"] == "missing"
    assert rs.worklist(store)["counts"]["interpretation_missing"] == 1     # the finding
    with pytest.raises(ValueError):
        rs.link(store, f["ref"], h["ref"], "derived_from")                  # a finding never stems from a test


def test_links_revise_withdraw(store):
    _series(store)
    a = rs.create_hypothesis(store, "A", series="serie-8", direction="+")
    b = rs.create_hypothesis(store, "B", series="serie-8", direction="+")
    out = rs.link(store, a["code"], b["ref"], "sharpens", note="narrower outcome")
    assert out["created"] and not rs.link(store, a["code"], b["ref"], "sharpens")["created"]
    with pytest.raises(ValueError):
        rs.link(store, a["ref"], b["ref"], "likes")
    assert rs.get_hypothesis(store, b["id"])["links"][0]["direction"] == "in"
    rs.preregister(store, a["id"], PREREG)
    with pytest.raises(ValueError):
        rs.revise(store, a["id"], statement="A'")
    new = rs.revise(store, a["id"], statement="A prime", reason="better operationalisation")
    assert new["code"] == "H8.1-r2" and new["revises"]["code"] == "H8.1" and new["status"] == "idea"
    old = rs.get_hypothesis(store, a["id"])
    assert old["status"] == "revised" and old["prereg"]["frozen"] and old["revised_by"][0]["code"] == "H8.1-r2"
    w = rs.withdraw(store, "hypothesis", b["id"], "duplicate of H8.1")
    assert w["status"] == "withdrawn" and w["withdrawn_reason"] == "duplicate of H8.1"


def test_integrity_flags_and_dataset_research(store):
    from fundus import datasets
    _series(store)
    _dataset(store, 7, versions=1)
    h = rs.create_hypothesis(store, "Uses dataset 7", series="serie-8", direction="+")
    rs.preregister(store, h["id"], {**PREREG, "data": "dataset:7 v1"})
    h = rs.add_result(store, h["id"], "inconclusive", inputs=["dataset:7"], at="2026-11-02 10:00:00")
    assert h["current_result"]["inputs"] == [{"ref": "dataset:7", "version": 1}]
    assert "data_newer_than_result" not in [f["flag"] for f in h["flags"]]
    store.db.execute("INSERT INTO dataset_version(dataset_id, n, created_at) VALUES (7, 2, '2026-11-03 08:00:00')")
    store.db.execute("UPDATE dataset SET current_version = 2 WHERE id = 7")
    store.db.commit()
    h = rs.get_hypothesis(store, h["id"])
    stale = next(f for f in h["flags"] if f["flag"] == "data_newer_than_result")
    assert stale["detail"][0] == {"ref": "dataset:7", "used": 1, "current": 2, "title": "Dataset 7"}
    assert rs.worklist(store)["counts"]["data_newer_than_result"] == 1
    lin = h["lineage"]
    assert {"dataset:7"} <= {n["id"] for n in lin["nodes"]} and any(n.get("stale") for n in lin["nodes"])
    # usage logged as tool 'hypothesis'
    assert store.db.execute("SELECT count(*) FROM dataset_use WHERE dataset_id = 7 AND tool = 'hypothesis'").fetchone()[0] == 1
    f = rs.create_finding(store, "Pattern in dataset 7", series="serie-8", kind="quantitative", strength="quantified")
    rs.add_evidence(store, f["ref"], "dataset", ref="dataset:7")
    res = rs.research_for_dataset(store, 7)
    assert [(x["type"], x["code"], x["verdict"]) for x in res] == [("hypothesis", "H8.1", "inconclusive"), ("finding", "F8.1", None)]
    assert [x["code"] for x in datasets.get(store, 7)["research"]] == ["H8.1", "F8.1"]
    # imported timestamps out of order → prereg_after_result
    h2 = rs.create_hypothesis(store, "Imported late prereg", series="serie-8", source="imported:x/kurz.md")
    rs.add_result(store, h2["id"], "supported", at="2026-11-01 10:00:00", source="imported:x/verdict.json")
    rs._freeze(store, rs.hyp_row(store, h2["id"]), {"statement": "Imported late prereg"}, "2026-11-01 12:00:00", "stamp",
               "imported:x/kurz.md", None)
    names = [f["flag"] for f in rs.get_hypothesis(store, h2["id"])["flags"]]
    assert "prereg_after_result" in names and "prereg_incomplete" in names


def test_search_filters_and_equal_ranking(store):
    _series(store)
    for v, stmt in (("supported", "Layer opening widens wrapper scope"), ("not_supported", "Layer opening shrinks wrapper scope")):
        h = rs.create_hypothesis(store, stmt, series="serie-8", direction="+", theory_refs=["A"])
        rs.preregister(store, h["id"], PREREG)
        rs.add_result(store, h["id"], v)
    rs.create_finding(store, "Wrapper founders talk about layers", series="serie-8")
    res = rs.search(store, "wrapper", include_internal=True)
    assert res["total"] == 3 and res["hypotheses"] == 2 and res["findings"] == 1
    assert rs.search(store, "wrapper")["total"] == 0                                  # internal by default
    assert rs.search(store, "Henfridsson", include_internal=True)["hypotheses"] == 2  # scholar of the theory card
    assert rs.search(store, "", theory="Layered", include_internal=True)["total"] == 2
    assert rs.search(store, "logit", include_internal=True)["total"] == 2             # pre-registration text
    nots = rs.search(store, "", verdict="not supported", include_internal=True)
    assert [b["verdict"] for b in nots["items"]] == ["not_supported"] and nots["findings"] == 0
    assert rs.search(store, "", type="finding", include_internal=True)["items"][0]["exploratory"] is True
    assert rs.search(store, "", exploratory=False, include_internal=True)["findings"] == 0
    em = rs.evidence_map(store)
    assert em[0]["theory"] == "A" and em[0]["verdicts"]["supported"] == 1 and em[0]["verdicts"]["not_supported"] == 1


def test_mcp_tools_and_portal_api(store):
    from fundus import mcp_server as m
    from test_reviews import call
    m.configure(store.path)
    try:
        j = lambda s: json.loads(s)
        s = j(m.fundus_research_series("create", slug="serie-9", title="Methods view", project="innovation-data"))
        assert s["slug"] == "serie-9" and s["visibility"] == "internal"
        h = j(m.fundus_hypothesis("create", series="serie-9", statement="Naive LLM codes overstate the effect", direction="+"))
        assert h["code"] == "H9.1"
        err = j(m.fundus_hypothesis("prereg", ref="H9.1", prereg={"measures": "x"}))
        assert "missing" in err["error"]
        h = j(m.fundus_hypothesis("prereg", ref="H9.1", prereg=PREREG))
        assert h["prereg"]["frozen"]
        assert "frozen" in j(m.fundus_hypothesis("draft", ref="H9.1", prereg={"method": "x"}))["error"]
        h = j(m.fundus_hypothesis("deviate", ref="H9.1", field="method", after="DSL", reason="bias correction",
                                  timing="before_analysis"))
        assert len(h["deviations"]) == 1
        h = j(m.fundus_hypothesis("result", ref="H9.1", result={"verdict": "inconclusive", "effect": "b = 0.0", "n": 50}))
        assert h["status"] == "result"
        out = j(m.fundus_hypothesis("interpret", ref="H9.1", interpretation={"summary": "s", "explanation": "e",
                                                                             "limits": "l", "theory": "t"}))
        assert out["state"] == "complete"
        f = j(m.fundus_finding("create", series="serie-9", statement="Two models disagree on vague claims",
                               kind="quantitative", strength="quantified"))
        assert f["code"] == "F9.1"
        d = j(m.fundus_finding("derive_hypothesis", ref="F9.1", statement="Disagreement marks decoupling"))
        assert d["derived_from_finding"]["code"] == "F9.1"
        r = j(m.fundus_research_search("disagree"))
        assert r["total"] == 2
        assert j(m.fundus_research_series("list"))["series"][0]["counts"]["result"] == 1
        # portal API
        code, ov = call(m, m.research_api, path="/api/research")
        assert code == 200 and {"board", "worklist", "evidence_map", "stats", "labels"} <= set(ov)
        code, one = call(m, m.research_api, path="/api/research/hypotheses/H9.1", path_params={"sub": "hypotheses", "rid": "H9.1"})
        assert code == 200 and one["code"] == "H9.1"
        code, lst = call(m, m.research_api, query=b"series=serie-9", path="/api/research/hypotheses",
                         path_params={"sub": "hypotheses"})
        assert code == 200 and lst["total"] == 2
        code, out = call(m, m.research_api, "POST", f"/api/research/hypotheses/{one['id']}",
                         path_params={"sub": "hypotheses", "rid": str(one["id"])}, body={"action": "draft", "prereg": {"method": "x"}})
        assert code == 403                                                       # never edits a frozen prereg
        code, out = call(m, m.research_api, "POST", f"/api/research/hypotheses/{one['id']}",
                         path_params={"sub": "hypotheses", "rid": str(one["id"])},
                         body={"action": "interpret", "interpretation": {"summary": "neu"}, "author": "Falk"})
        assert code == 200 and out["interpretation_state"] == "incomplete"
        code, out = call(m, m.research_api, "POST", "/api/research/findings", path_params={"sub": "findings"},
                         body={"statement": "Beobachtung im Workshop", "series": "serie-9"})
        assert code == 200 and out["code"] == "F9.2"
        code, out = call(m, m.research_api, "POST", "/api/research/findings", path_params={"sub": "findings"},
                         body={"statement": "x"}, origin="http://evil.example")
        assert code == 403
        code, sg = call(m, m.research_api, path="/api/research/series/serie-9", path_params={"sub": "series", "rid": "serie-9"})
        assert code == 200 and len(sg["hypotheses"]) == 2 and len(sg["findings"]) == 2
        assert m.fundus_hypothesis in m.TOOLS and m.fundus_research_search in m.TOOLS
    finally:
        m.configure(m.DEFAULT_DB)


def test_importer_on_fixture_tree(store):
    dry = ri.run(store, FIXTURE, dry_run=True)
    assert dry["dry_run"] and dry["totals"]["hypotheses"] > 0
    assert store.db.execute("SELECT count(*) FROM hypothesis").fetchone()[0] == 0          # nothing written
    # a hypothesis registered by hand before the import is never touched by it
    rs.create_series(store, "serie-12", "own series")
    mine = rs.create_hypothesis(store, "My own wording", code="H12.2", series="serie-12")
    rep = ri.run(store, FIXTURE)
    c, t = rep["changes"], rep["totals"]
    assert c["hypotheses_skipped_registered"] == 1 and c["series_skipped_registered"] == 1
    assert rs.get_hypothesis(store, mine["id"])["statement"] == "My own wording"
    assert {s["slug"] for s in rs.series_list(store)} == {"serie-4", "serie-5", "serie-6", "serie-10", "serie-12"}
    # H6.1: proposal statement + theory card, frozen pre-registration from kurz.md + stamp, full run current, sample run kept
    h = rs.get_hypothesis(store, "H6.1")
    assert h["statement"].startswith("Post-2023 AI-native firms are more often vertical") and h["direction"] == "+"
    assert h["theory_refs"] == ["T1"] and h["theory_cards"][0]["scholars"][0]["works"][0]["doi"] == "10.5465/amr.2001.4378020"
    p = h["prereg"]
    assert p["frozen_at"] == "2026-11-01 09:42:00" and p["time_source"] == "stamp" and p["sha_ok"]
    assert p["decision_rule"].startswith("supported if the main A coefficient") and "logit" in p["method"]
    assert p["data"] == [{"ref": "dataset:100"}, {"ref": "dataset:47", "version": 3}]
    assert h["status"] == "result" and len(h["results"]) == 2
    cur = h["current_result"]
    assert cur["verdict"] == "inconclusive" and not cur["sample_run"] and cur["n"] == 3000
    assert (cur["ci_low"], cur["ci_high"]) == (-0.30, 0.10) and cur["estimate"] == -0.10
    assert cur["scripts"][0]["path"].endswith("H6.1_verticality.py") and len(cur["scripts"][0]["sha256"]) == 64
    assert [e["note"] for e in cur["evidence"]] == ["h6_1_models.csv"]
    assert "## Estimates" in cur["method_doc"] and cur["interpretation"]["state"] == "missing"
    assert h["results"][1]["sample_run"] and h["results"][1]["partial_data"] == "Wayback index partial"
    names = [f["flag"] for f in h["flags"]]
    assert "interpretation_missing" in names and "prereg_after_result" not in names
    # H6.2 only in the proposal; the table in section 3 did not override H6.1
    h2 = rs.get_hypothesis(store, "H6.2")
    assert h2["status"] == "idea" and h2["imported_partial"] and "imported_partial" in [f["flag"] for f in h2["flags"]]
    # series 5: bare codes → H5.n, verdicts from ERGEBNISSE.md, K design pre-registration without timestamp
    h51 = rs.get_hypothesis(store, "H5.1")
    assert h51["aliases"][:2] == ["H1", "C1"] and h51["current_result"]["verdict"] == "not_supported"
    assert "prereg_missing" in [f["flag"] for f in h51["flags"]] and h51["statement"].startswith("Countries form four types: producer:")
    h512 = rs.get_hypothesis(store, "H12", series="serie-5")
    assert h512["code"] == "H5.12" and h512["prereg"]["time_source"] == "unknown"
    assert h512["current_result"]["verdict"] == "supported" and "prereg_time_unknown" in [f["flag"] for f in h512["flags"]]
    assert rs.get_hypothesis(store, "H5.16")["current_result"] is None                    # 'pending' is no verdict
    # series 10: bullet hypothesis, string n, 'partly supported', an 'Interpretation limits' section taken over as written
    h10 = rs.get_hypothesis(store, "H10.1.1")
    r10 = h10["current_result"]
    assert h10["title"].startswith("Perceived compression") and r10["verdict"] == "partly_supported"
    assert r10["n"] is None and r10["n_text"] == "S1 8077, S2 6990" and r10["inputs"][0]["ref"] == "dataset:111"
    assert h10["prereg"]["frozen_at"] == "2026-11-01 19:18:00"
    assert r10["interpretation"]["state"] == "incomplete" and r10["interpretation"]["current"]["limits"].startswith("Survey")
    assert rs.get_hypothesis(store, "H12.1", series="serie-12")["status"] == "idea"
    # series 4: exploratory analysis → finding, statement from ERGEBNISSE.md
    f = rs.get_finding(store, "F4.1")
    assert f["exploratory"] and f["strength"] == "quantified" and f["title"] == "Supply leads adoption"
    assert f["statement"].startswith("The function mix of startup supply") and "crosswalk" in f["method"]
    assert [e["note"] for e in f["evidence"]] == ["lead_profile.csv"] and f["interpretation"]["state"] == "missing"
    assert c["folders_skipped_building_blocks"] == 1                                       # P0_patents
    assert t["results"] == 5 and t["findings"] == 1 and t["preregs_frozen"] == 3
    assert store.db.execute("SELECT count(*) FROM interpretation").fetchone()[0] == 1      # nothing invented
    # idempotent
    again = ri.run(store, FIXTURE)
    assert not [k for k in again["changes"] if k.endswith(("_created", "_frozen", "_updated"))]
    assert again["changes"]["results_unchanged"] == 5 and again["totals"] == t


def test_parse_numbers_only_when_unambiguous():
    p = ri.parse_numbers
    assert p("Poisson log-ratio of scope after open-weight events +0.021 [-0.004, +0.045] (pred. +) (IRR 1.02)", None) == \
        {"ci_low": -0.004, "ci_high": 0.045, "estimate": 0.021}
    assert p("+2.49 pp per log-year [1.39, 3.59]", None) == {"ci_low": 1.39, "ci_high": 3.59}   # text between: no estimate
    assert p("'no need' minus 'did not know how' +29.9 pp [+26.2, +33.3]", "") == \
        {"ci_low": 26.2, "ci_high": 33.3, "estimate": 29.9, "unit": "pp"}
    assert p("log-odds A vs B = -0.10", "[-0.30, 0.10]") == {"ci_low": -0.30, "ci_high": 0.10, "estimate": -0.10}
    # two intervals, a labelled CI field, an estimate outside the interval: nothing / no estimate
    assert p("a 0.1 [0.0, 0.2]; b 0.3 [0.2, 0.4]", None) == {}
    assert p("x = 1", "a [0.1, 0.2]; b [-0.1, 0.3]") == {}
    assert p("DiD = -0.245 [-0.644, 0.153]", "[-0.294, 0.105]") == {"ci_low": -0.294, "ci_high": 0.105}
    assert p("slope = 5.0", "[0.1, 0.2]") == {"ci_low": 0.1, "ci_high": 0.2}
    assert p("no numbers here", None) == {}


def test_freeze_time_from_main_stamp_not_sample(store, tmp_path):
    import shutil
    root = tmp_path / "an"
    shutil.copytree(FIXTURE / "serie-6", root / "serie-6")
    main, sample = root / "serie-6/ergebnisse/H6.1", root / "serie-6/ergebnisse/_sample/H6.1"
    (main / "prereg.stamp").write_text("2026-11-01 14:14")
    (sample / "prereg.stamp").write_text("2026-11-01 14:07")
    (sample / "kurz.md").write_text((main / "kurz.md").read_text().replace("09:42", "14:07"))
    ri.run(store, root)
    h = rs.get_hypothesis(store, "H6.1")
    assert h["prereg"]["frozen_at"] == "2026-11-01 14:14:00" and h["prereg"]["time_source"] == "stamp"
    assert "prereg_after_result" not in [f["flag"] for f in h["flags"]]       # the earlier sample run had its own prereg


def test_sidebar_stats(store):
    from fundus import mcp_server as m
    from test_reviews import call
    _series(store)
    rs.create_hypothesis(store, "A", series="serie-8")
    rs.create_finding(store, "F", series="serie-8")
    m.configure(store.path)
    try:
        code, s = call(m, m.research_api, query=b"stats=1", path="/api/research")
    finally:
        m.configure(m.DEFAULT_DB)
    assert code == 200 and s["hypotheses"] == 1 and s["findings"] == 1 and s["worklist"] == 1
    assert "board" not in s and len(json.dumps(s)) < 600
