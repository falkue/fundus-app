"""Datasets (fundus.datasets): import of CSV (decimal commas), Excel (every sheet), JSON and documents; profile; versions;
description by a (fake) local model with own fields winning; search; preview; read-only SQL with file access blocked;
edit, remove/restore/purge; MCP tools; portal API, upload and download."""
import json

import pytest

from fundus import datasets as ds
from fundus import mcp_server as m


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_DATASET_DIR", str(tmp_path / "datasets"))
    yield
    ds.set_llm(None)
    m.configure(m.DEFAULT_DB)


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        return {"summary_en": "Hospitals per canton with their AI share.", "summary_de": "Spitäler je Kanton mit KI-Anteil.",
                "keywords": ["hospitals", "AI share"], "period": "2024-2025", "geo": "Switzerland",
                "unit_of_observation": "canton and year", "columns": {"ki_anteil": "share of hospitals using AI, %"},
                "industry": ["healthcare", "no-such"], "topic": [], "region": ["ch"]}


def files(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "spitaeler.csv").write_text("kanton;jahr;spitaeler;ki_anteil\nZH;2024;24;12,5\nBE;2024;17;8,25\nGE;2025;9;\n")
    (d / "codebuch.md").write_text("# Codebuch\nki_anteil in %\n")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Umsatz"
    ws.append(["firma", "umsatz"])
    ws.append(["A", 10.5])
    ws.append(["B", 20])
    ws2 = wb.create_sheet("Leer")
    ws3 = wb.create_sheet("Personal")
    ws3.append(["firma", "fte"])
    ws3.append(["A", 3])
    wb.save(d / "firmen.xlsx")
    (d / "events.jsonl").write_text('{"id": 1, "typ": "a"}\n{"id": 2, "typ": "b"}\n')
    return d


def test_import_profile_version_and_files(store, tmp_path):
    d = files(tmp_path)
    r = ds.add(store, [d], title="KI in Spitälern", publisher="Test", project="Northwind", actor="test")
    assert r["version"] == 1 and r["documents"] == ["codebuch.md"] and not r["skipped"]
    tabs = {t["table"]: t for t in r["tables"]}
    assert set(tabs) == {"spitaeler", "Umsatz", "Personal", "events"}                       # empty sheet left out
    assert tabs["spitaeler"]["rows"] == 3 and tabs["spitaeler"]["columns"] == 4
    g = ds.get(store, r["slug"])
    col = {c["name"]: c for t in g["tables_detail"] if t["table"] == "spitaeler" for c in t["columns"]}
    assert col["ki_anteil"]["type"] == "DOUBLE" and col["ki_anteil"]["max"] == "12.5" and col["ki_anteil"]["nulls_pct"] > 30
    assert all(__import__("os").path.exists(t["path"]) for t in g["tables_detail"]) and g["project"] == "northwind"
    assert g["documents"][0]["file"] == "codebuch.md"
    v2 = ds.add(store, [d / "spitaeler.csv"], dataset=r["slug"], note="update")
    assert v2["version"] == 2 and ds.get(store, r["dataset"])["tables"] == 1 and len(ds.get(store, r["dataset"])["versions"]) == 2
    with pytest.raises(ValueError):
        ds.add(store, [tmp_path / "in" / "codebuch.md"], visibility="secret")


def test_describe_own_fields_win_and_search(store, tmp_path):
    r = ds.add(store, [files(tmp_path)], title="KI in Spitälern", keywords=["eigene"])
    ds.update(store, r["dataset"], {"tags": {"topic": ["ai-adoption"]}, "period": "2023-2025"})
    llm = FakeLLM()
    ds.describe_one(store, r["dataset"], llm)
    assert "ki_anteil" in llm.prompts[0] and "ZH | 2024" in llm.prompts[0]
    g = ds.get(store, r["dataset"])
    assert g["status"] == "described" and g["summary_de"].startswith("Spitäler") and g["geo"] == "CH"
    assert g["period"] == "2023-2025"                                                       # own value kept
    assert g["keywords"][0] == "eigene" and "hospitals" in g["keywords"]
    assert g["tags"]["topic"] == ["ai-adoption"] and g["tags"]["industry"] == ["healthcare"]  # unknown vocab dropped
    meaning = {c["name"]: c.get("meaning") for t in g["tables_detail"] for c in t["columns"]}
    assert meaning["ki_anteil"].startswith("share")
    assert ds.search(store, "KI-Anteil Kanton")["total"] == 1 and ds.search(store, "share of hospitals")["total"] == 1
    assert ds.search(store, "Acme")["total"] == 0 and ds.search(store, "")["total"] == 1
    ds.update(store, r["dataset"], {"visibility": "internal"})
    assert ds.search(store, "Kanton")["total"] == 0 and ds.search(store, "Kanton", include_internal=True)["total"] == 1


def test_period_comes_from_the_data(store, tmp_path):
    r = ds.add(store, [files(tmp_path)], title="T")
    ds.describe_one(store, r["dataset"], FakeLLM())                      # the model says 2024-2025 too, but
    store.db.execute("UPDATE dataset_file SET profile = replace(profile, '\"max\": \"2025\"', '\"max\": \"2026\"')")
    store.db.execute("UPDATE dataset SET period = NULL")
    store.db.commit()
    ds.describe_one(store, r["dataset"], FakeLLM())
    assert ds.get(store, r["dataset"])["period"] == "2024-2026"           # min/max of the year column win


def test_preview_and_safe_query(store, tmp_path):
    r = ds.add(store, [files(tmp_path)], title="T")
    p = ds.preview(store, r["dataset"], table="spitaeler", order="spitaeler:desc", limit=2)
    assert p["total"] == 3 and p["rows"][0][0] == "ZH" and len(p["rows"]) == 2
    q = ds.query(store, r["dataset"], "SELECT kanton FROM spitaeler WHERE ki_anteil > 10")
    assert q["rows"] == [["ZH"]] and "spitaeler" in q["views"]
    assert ds.query(store, r["dataset"], "SELECT sum(fte) s FROM personal")["rows"] == [[3]]
    for bad in ("SELECT * FROM read_csv('/etc/passwd')", "COPY t TO '/tmp/x.csv'", "SELECT 1; SELECT 2", "DROP VIEW t",
                "SELECT * FROM 'x.parquet'", "ATTACH 'x.db'"):
        with pytest.raises(ValueError):
            ds.query(store, r["dataset"], bad)


def test_remove_restore_purge(store, tmp_path):
    r = ds.add(store, [files(tmp_path)], title="T")
    pq = ds.get(store, r["dataset"])["tables_detail"][0]["path"]
    ds.remove(store, r["dataset"])
    assert ds.search(store)["total"] == 0 and ds.search(store, removed=True)["total"] == 1
    ds.remove(store, r["dataset"], restore=True)
    assert ds.search(store)["total"] == 1
    ds.remove(store, r["dataset"])
    assert ds.purge_removed(store)["purged"] == 0
    store.db.execute("UPDATE dataset SET removed_at = '2000-01-01 00:00:00'")
    store.db.commit()
    res = ds.purge_removed(store)
    assert res["purged"] == 1 and res["files_deleted"] > 0 and not __import__("os").path.exists(pq)


def test_mcp_and_portal(store, tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from test_reviews import call
    m.configure(store.path)
    d = files(tmp_path)
    out = json.loads(m.fundus_dataset_add([str(d)], title="KI in Spitälern", visibility="internal", project="northwind"))
    assert out["portal"].endswith(f"#daten?d={out['slug']}") and len(out["tables"]) == 4
    assert json.loads(m.fundus_dataset_search("Spitälern"))["total"] == 0                     # internal
    assert json.loads(m.fundus_dataset_search("Spitälern", project="northwind"))["total"] == 1
    g = json.loads(m.fundus_dataset(out["ref"], include_internal=True))
    assert g["tables_detail"][0]["path"].endswith(".parquet") and "how_to_use" in g
    q = json.loads(m.fundus_dataset(out["ref"], action="query", sql="SELECT count(*) n FROM t", include_internal=True))
    assert q["rows"] == [[3]]
    assert "error" in json.loads(m.fundus_dataset(out["ref"]))                               # internal, no project
    code, lst = call(m, m.datasets_api, path="/api/datasets")
    assert code == 200 and lst["total"] == 1 and lst["stats"]["datasets"] == 1
    code, one = call(m, m.datasets_api, query=f"ref={out['slug']}".encode(), path="/api/datasets")
    assert code == 200 and one["id"] == out["dataset"]
    did = out["dataset"]
    code, p = call(m, m.datasets_api, query=b"limit=2", path=f"/api/datasets/{did}/preview", path_params={"did": did, "sub": "preview"})
    assert code == 200 and len(p["rows"]) == 2
    code, q = call(m, m.datasets_api, "POST", f"/api/datasets/{did}/query", path_params={"did": did, "sub": "query"},
                   body={"sql": "SELECT kanton FROM t ORDER BY kanton"})
    assert code == 200 and q["rows"][0] == ["BE"]
    code, u = call(m, m.datasets_api, "POST", f"/api/datasets/{did}", path_params={"did": did},
                   body={"action": "update", "changes": {"description": "Eigene Beschreibung"}})
    assert code == 200 and u["description"] == "Eigene Beschreibung"
    code, _ = call(m, m.datasets_api, "POST", f"/api/datasets/{did}", path_params={"did": did}, body={"action": "update", "changes": {"x": 1}})
    assert code == 400
    app = Starlette(routes=[Route("/datasets/{name}", m.dataset_file), Route("/api/datasets/upload", m.dataset_upload, methods=["POST"])])
    with TestClient(app) as c:
        f = g["tables_detail"][0]["download_url"].split("/datasets/")[1]
        r = c.get(f"/datasets/{f}")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert c.get(f"/datasets/{'0' * 64}.csv").status_code == 404
        up = c.post("/api/datasets/upload", files=[("files", ("neu.csv", b"a,b\n1,2\n3,4\n", "text/csv"))], data={"title": "Neu"})
        assert up.status_code == 200 and up.json()["tables"][0]["rows"] == 2


def test_metrics_for_statistics(store, tmp_path):
    r = ds.add(store, [files(tmp_path)], title="KI in Spitälern", visibility="internal", project="northwind")
    m_ = ds.metrics(store)
    assert (m_["datasets"], m_["tables"], m_["documents"], m_["internal"], m_["pending"]) == (1, 4, 1, 1, 1)
    assert m_["rows"] == 3 + 2 + 1 + 2 and m_["columns"] == 4 + 2 + 2 + 2 and m_["bytes_stored"] > m_["bytes_current"] > 0
    assert {f["format"] for f in m_["formats"]} == {"csv", "xlsx", "jsonl", "md"} and m_["byProject"][0]["project"] == "northwind"
    assert m_["largest"][0]["slug"] == r["slug"] and len(m_["callsByDay"]) == 30 and sum(x["n"] for x in m_["addedByDay"]) == 1
    from fundus import metrics
    assert metrics.compute(store, use_cache=False)["datasets"]["datasets"] == 1


def test_edition_reminders(store, tmp_path):
    from datetime import date
    from fundus import query
    r = ds.add(store, [files(tmp_path)], title="WIPO GII", frequency="annual", next_expected="2026-09",
               update_hint="https://wipo.int/gii – rebuild: skripte/gii.py")
    store.series("wipo-gii-overall-score", "GII overall score", unit="index", frequency="annual",
                 update_hint=f"tracker for dataset:{r['dataset']} – download from wipo.int")
    store.series("other-series", "Other", unit="%", frequency="annual")
    store.db.commit()
    today = date(2026, 10, 31)
    assert ds.due(store, today=today)[0]["reason"] == "overdue" and ds.get(store, r["dataset"])["tracker_series"] == ["wipo-gii-overall-score"]
    t = query.update_targets(store, today=today)
    assert t["datasets"][0]["ref"] == f"dataset:{r['dataset']}"
    assert "wipo-gii-overall-score" not in [x["id"] for x in t["targets"]] and "other-series" in [x["id"] for x in t["targets"]]
    out = ds.mark_checked(store, r["dataset"], today=today)
    assert out["next_expected"] == "2027-09" and out["tracker_series_checked"] == ["wipo-gii-overall-score"]
    assert store.db.execute("SELECT last_checked, next_expected FROM series WHERE id = 'wipo-gii-overall-score'").fetchone()[:] == ("2026-10-31", "2027-09")
    assert ds.due(store, today=today) == [] and ds.metrics(store)["tracked"] == 1 and ds.metrics(store)["updateDue"] == []
    assert ds.due(store, today=date(2027, 9, 1))[0]["reason"] == "overdue"
    with pytest.raises(ValueError):
        ds.update(store, r["dataset"], {"frequency": "weekly"})
    with pytest.raises(ValueError):
        ds.update(store, r["dataset"], {"next_expected": "Sept 2027"})
    v2 = ds.add(store, [tmp_path / "in" / "spitaeler.csv"], dataset=r["dataset"])         # a new edition counts as checked
    assert ds.get(store, r["dataset"])["last_checked"] == date.today().isoformat() and v2["version"] == 2
    m.configure(store.path)
    res = json.loads(m.fundus_mark_checked(f"dataset:{r['dataset']}", next_expected="2028-09"))
    assert res["next_expected"] == "2028-09"
    upd = json.loads(m.fundus_dataset(f"dataset:{r['dataset']}", action="update", changes={"frequency": "quarterly"}))
    assert upd["frequency"] == "quarterly"


def test_update_via_fundus_update_links_to_slides_dat_files(store, tmp_path, monkeypatch):
    from fundus import exhibits as ex, reviews as rv
    from test_exhibits import page, png_bytes, reg, write
    d = files(tmp_path)
    (d / "census.dat").write_text("geo|year|value\nCA|2019|1.5\nNY|2020|2.5\n")
    (d / "structure.xml").write_text("<Structure/>")
    r = ds.add(store, [d], title="T")
    assert "census" in [t["table"] for t in r["tables"]] and "structure.xml" in r["documents"]
    m.configure(store.path)
    out = json.loads(m.fundus_update("dataset", r["dataset"], {"period": "2008-Q1 – 2026-Q3", "frequency": "quarterly"}, note="fixed"))
    assert out["period"] == "2008-Q1 – 2026-Q3" and out["frequency"] == "quarterly"
    log = store.db.execute("SELECT detail FROM changelog WHERE item_kind = 'dataset' AND action = 'update' ORDER BY id DESC").fetchone()[0]
    assert '"old"' in log and "2008-Q1" in log
    root = tmp_path / "Projects"
    monkeypatch.setattr(ex, "PROJECTS_ROOT", root)
    e = reg(store, write(root, "Contoso/exhibits/funding", page("Funding by round", extra=""), png=png_bytes()))
    res = rv.link_items(store, e["exhibit_id"], [f"dataset:{r['dataset']}"])
    assert res["datasets"][0]["ref"] == f"dataset:{r['dataset']}"
    assert ds.get(store, r["dataset"])["used_on_slides"][0]["id"] == e["exhibit_id"]
    html = page("Funding by round 2", extra="").replace("<head>", f'<head><meta name="fundus-refs" content="dataset:{r["dataset"]}">', 1)
    e2 = reg(store, write(root, "Contoso/exhibits/funding2", html, png=png_bytes(rgb=(3, 3, 3))))
    assert any(x["id"] == e2["exhibit_id"] for x in ds.get(store, r["dataset"])["used_on_slides"])


def test_period_spans_all_tables(store, tmp_path):
    d = tmp_path / "q"
    d.mkdir()
    (d / "2008q1.csv").write_text("year,x\n2008,1\n2008,2\n")
    (d / "2026q3.csv").write_text("year,x\n2026,1\n")
    (d / "2021q2.csv").write_text("year,x\n" + "".join(f"2021,{i}\n" for i in range(50)))
    r = ds.add(store, [d], title="Quarterly packages")
    ds.describe_one(store, r["dataset"], FakeLLM())
    assert ds.get(store, r["dataset"])["period"] == "2008-2026"


# ------------------------------------------------------------------ linked data (1 Nov 2026)
def _csv(tmp_path, name, text):
    d = tmp_path / name.split(".")[0]
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text)
    return p


def test_code_systems_and_crosswalk_lookup(store, tmp_path):
    data = ds.add(store, [_csv(tmp_path, "firms.csv", "sic,year,ai\n7372,2024,0.4\n2834,2024,0.1\n")], title="10-K panel",
                  code_systems={"sic": "SIC-SEC"})
    g = ds.get(store, data["dataset"])
    assert g["tables_detail"][0]["columns"][0]["code_system"] == "SIC-SEC"
    with pytest.raises(ValueError, match="no column"):
        ds.update(store, data["dataset"], {"code_systems": {"nope": "NAICS-2022"}})
    # one crosswalk table with several system pairs, valid dates and weights
    xw = ds.add(store, [_csv(tmp_path, "xwalk.csv",
                             "from_system,from_code,from_label,to_system,to_code,to_label,weight,relation,quality,valid_from,valid_to\n"
                             "SIC-SEC,7372,Prepackaged software,NAICS-2022,513210,Software publishers,1,1:1,A,2017-01-01,\n"
                             "SIC-SEC,2834,Pharmaceutical preparations,NAICS-2022,325412,Pharma,1,1:1,A,2017-01-01,\n"
                             "NAICS-2022,513210,Software publishers,NACE-2,J58.29,Other software publishing,0.8,1:n,B,2022-01-01,\n"
                             "NAICS-2022,513210,Software publishers,NACE-2,J62.01,Computer programming,0.2,1:n,B,2022-01-01,2025-12-31\n")],
                title="Industry crosswalk", kind="crosswalk", crosswalk={"method": "official", "quality": "A"})
    meta = ds.get(store, xw["dataset"])["crosswalk"]
    assert {(p["from_system"], p["to_system"]) for p in meta["pairs"]} == {("SIC-SEC", "NAICS-2022"), ("NAICS-2022", "NACE-2")}
    r = ds.crosswalk_lookup(store, "sic sec", "NAICS_2022", codes="7372")
    assert [x["to_code"] for x in r["mappings"]] == ["513210"] or [x["to_code"] for x in r["mappings"]] == [513210]
    r = ds.crosswalk_lookup(store, "NAICS-2022", "NACE-2", codes=["513210"], at="2026-06-01")
    assert [x["to_code"] for x in r["mappings"]] == ["J58.29"]            # the second row expired end of 2025
    back = ds.crosswalk_lookup(store, "NACE-2", "NAICS-2022", codes="J58.29")
    assert back["mappings"][0]["direction"] == "reverse" and str(back["mappings"][0]["to_code"]) == "513210"
    assert ds.crosswalk_lookup(store, "ISIC-4", "NACE-2")["hint"]
    reg = {x["system"]: x for x in ds.code_system_registry(store, include_internal=True)}
    assert "SIC-SEC" in reg and reg["SIC-SEC"]["columns"] and reg["SIC-SEC"]["crosswalks"]
    assert [d["ref"] for d in ds.search(store, code_system="naics-2022", include_internal=True)["datasets"]] == [xw["ref"]]
    assert ds.search(store, kind="crosswalk", include_internal=True)["total"] == 1
    # a crosswalk needs its code columns
    with pytest.raises(ValueError, match="from_code"):
        ds.update(store, data["dataset"], {"kind": "crosswalk", "crosswalk": {"from_system": "A", "to_system": "B"}})


def test_lineage_stale_and_visibility(store, tmp_path):
    a = ds.add(store, [_csv(tmp_path, "a.csv", "x,y\n1,2\n")], title="Input A")
    b = ds.add(store, [_csv(tmp_path, "b.csv", "x,z\n1,3\n")], title="Input B (client)", visibility="internal")
    p = ds.add(store, [_csv(tmp_path, "p.csv", "x,y\n1,2\n")], title="Panel", inputs=[a["ref"]])
    assert ds.get(store, p["dataset"])["inputs"][0]["ref"] == a["ref"]
    assert ds.get(store, a["dataset"])["used_by"][0]["ref"] == p["ref"]
    # a new version of the input makes the panel stale (get, search, update reminders)
    ds.add(store, [_csv(tmp_path, "a2.csv", "x,y\n1,5\n")], dataset=a["ref"])
    g = ds.get(store, p["dataset"])
    assert g["inputs"][0]["stale"] and g["stale"]
    assert [d["ref"] for d in ds.search(store, stale=True, include_internal=True)["datasets"]] == [p["ref"]]
    assert [x["reason"] for x in ds.due(store, include_internal=True) if x["ref"] == p["ref"]] == ["inputs_updated"]
    # rebuilt as a new version: same inputs, now current – no longer stale
    ds.add(store, [_csv(tmp_path, "p2.csv", "x,y\n1,5\n")], dataset=p["ref"])
    assert "stale" not in ds.get(store, p["dataset"])
    # an internal input makes the panel internal; publishing it again needs a confirmation
    r = ds.update(store, p["dataset"], {"inputs": [a["ref"], b["ref"]]})
    assert r["visibility"] == "internal" and r["warnings"]
    with pytest.raises(ValueError, match="confirm_public"):
        ds.update(store, p["dataset"], {"visibility": "public"})
    assert ds.update(store, p["dataset"], {"visibility": "public", "confirm_public": True})["visibility"] == "public"
    # an input turning internal later carries over to what is built from it
    r = ds.update(store, a["dataset"], {"visibility": "internal"})
    assert ds.get(store, p["dataset"])["visibility"] == "internal" and r["warnings"]
    with pytest.raises(ValueError, match="cycle"):
        ds.update(store, a["dataset"], {"inputs": [p["ref"]]})


def test_tables_across_versions(store, tmp_path):
    d = tmp_path / "v1"
    d.mkdir()
    (d / "pass0.csv").write_text("id,label\n1,a\n")
    (d / "pass1.csv").write_text("id,extra\n1,x\n2,y\n")
    r = ds.add(store, [d], title="YC classification")
    ds.add(store, [_csv(tmp_path, "pass0.csv", "id,label\n1,b\n3,c\n")], dataset=r["ref"])
    assert [t["table"] for t in ds.get(store, r["dataset"])["tables_detail"]] == ["pass0"]
    g = ds.update(store, r["dataset"], {"table_scope": "merged"})
    tabs = {t["table"]: t for t in g["tables_detail"]}
    assert set(tabs) == {"pass0", "pass1"} and tabs["pass1"]["version"] == 1 and "version" not in tabs["pass0"]
    q = ds.query(store, r["dataset"], "SELECT count(*) FROM pass1")
    assert q["rows"] == [[2]]
    assert ds.query(store, r["dataset"], "SELECT label FROM pass0 ORDER BY id")["rows"] == [["b"], ["c"]]


def test_mcp_crosswalk_tool(store, tmp_path):
    m.configure(store.path)
    ds.add(store, [_csv(tmp_path, "x.csv", "from_code,to_code\nA1,B1\n")], title="Mini crosswalk",
           kind="crosswalk", crosswalk={"from_system": "SYS-A", "to_system": "SYS-B"})
    store.db.commit()
    out = json.loads(m.fundus_crosswalk())
    assert {x["system"] for x in out["code_systems"]} == {"SYS-A", "SYS-B"}
    out = json.loads(m.fundus_crosswalk("SYS-A", "SYS-B", codes="A1"))
    assert out["mappings"][0]["to_code"] == "B1"


def test_timed_out_descriptions_are_retried(store, tmp_path):
    class Slow:
        model = "slow"

        def complete(self, system, user):
            raise TimeoutError("timed out")
    r = ds.add(store, [_csv(tmp_path, "big.csv", "a,b\n1,2\n")], title="Big")
    with pytest.raises(TimeoutError):
        ds.describe_one(store, r["dataset"], llm=Slow())
    row = store.db.execute("SELECT status, error FROM dataset WHERE id = ?", (r["dataset"],)).fetchone()
    assert row["status"] == "failed" and "[attempt 1 " in row["error"]
    assert r["dataset"] not in ds.pending(store, retry_timeouts=True)        # too early
    store.db.execute("UPDATE dataset SET error = 'TimeoutError: timed out [attempt 1 2020-01-01 00:00]' WHERE id = ?", (r["dataset"],))
    assert r["dataset"] in ds.pending(store, retry_timeouts=True)
    store.db.execute("UPDATE dataset SET error = 'TimeoutError: timed out [attempt 3 2020-01-01 00:00]' WHERE id = ?", (r["dataset"],))
    assert r["dataset"] not in ds.pending(store, retry_timeouts=True)        # gave up after three attempts
