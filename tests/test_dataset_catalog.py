"""Dataset catalog & archive (fundus.dataset_catalog): column counts, publisher groups, usage log, lifecycle, reference
datasets, fixity and orphans, citation, catalog endpoint, version changes, lineage."""
import json
import os
import time

import pytest

from fundus import dataset_catalog as cat
from fundus import datasets as ds
from fundus import mcp_server as m


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_DATASET_DIR", str(tmp_path / "datasets"))
    yield
    ds.set_llm(None)
    m.configure(m.DEFAULT_DB)


def _csv(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def _two_tables(tmp_path):
    d = tmp_path / "two"
    d.mkdir()
    (d / "big.csv").write_text("a,b,c\n" + "".join(f"{i},{i},{i}\n" for i in range(10)))
    (d / "small.csv").write_text("x,y,z,w,v\n1,2,3,4,5\n")
    return d


def test_counts_main_table_max_and_total(store, tmp_path):
    r = ds.add(store, [_two_tables(tmp_path)], title="Zwei Tabellen")
    b = ds.get(store, r["dataset"])
    assert b["tables"] == 2 and b["rows"] == 11
    assert b["columns"] == 3 and b["columns_max"] == 5 and b["columns_total"] == 8      # main = largest table
    assert store.db.execute("SELECT count(*) FROM dataset_file WHERE columns_n IS NULL AND role = 'table'").fetchone()[0] == 0
    hit = ds.search(store, "Zwei")["datasets"][0]
    assert hit["columns"] == 3 and hit["columns_total"] == 8


def test_publisher_group(store):
    eid = store.db.execute("INSERT INTO entity(kind, name) VALUES ('organisation', 'Federal Statistical Office')").lastrowid
    from fundus.entities import norm
    for a in ("Bundesamt für Statistik", "BFS"):
        store.db.execute("INSERT INTO entity_alias(entity_id, alias, alias_norm, source) VALUES (?,?,?, 'manual')", (eid, a, norm(a)))
    store.db.commit()
    g = cat.publisher_groups(store, ["Jane Doe (HSG), own analysis (Series 5)", "Jane Doe (HSG) / Fundus analysis",
                                     "Bundesamt für Statistik (BFS / OFS)", "OECD, Directorate for Science", "Eurostat",
                                     "Y Combinator (Verzeichnis) / eigene Analyse", "European Commission – CORDIS"])
    assert g["Jane Doe (HSG), own analysis (Series 5)"] == cat.OWN_GROUP == g["Jane Doe (HSG) / Fundus analysis"]
    assert g["Bundesamt für Statistik (BFS / OFS)"] == "Federal Statistical Office"
    assert g["OECD, Directorate for Science"] == "OECD" and g["Eurostat"] == "Eurostat"
    assert g["Y Combinator (Verzeichnis) / eigene Analyse"] == "Y Combinator"
    assert g["European Commission – CORDIS"] == "European Commission"


def test_usage_log_and_derived_numbers(store, tmp_path):
    m.configure(store.path)
    r = ds.add(store, [_csv(tmp_path, "a.csv", "k,v\n1,2\n")], title="Nutzung Test")
    ref = r["ref"]
    json.loads(m.fundus_dataset(ref))
    json.loads(m.fundus_dataset(ref, action="preview"))
    json.loads(m.fundus_dataset(ref, action="query", sql="SELECT * FROM t"))
    json.loads(m.fundus_dataset_search("Nutzung"))
    tools = [t for (t,) in m._store().db.execute("SELECT tool FROM dataset_use ORDER BY id")]
    assert tools == ["show", "preview", "query", "search_hit"]
    st = m._store()
    st.db.execute("INSERT INTO dataset_use(ts, dataset_id, version, tool) VALUES ('2020-01-01 00:00:00', ?, 1, 'show')", (r["dataset"],))
    st.db.commit()
    u = cat.usage_counts(st)[r["dataset"]]
    assert u["uses_total"] == 4 and u["uses_30d"] == 3 and u["seen_30d"] == 1 and u["last_used"] >= "2026"
    g = ds.get(st, ref)
    assert g["uses"]["total"] == 4 and g["uses"]["by_tool"]["show"] == 2 and len(g["uses"]["recent"]) == 5
    assert g["uses"]["daily_90"] and g["uses"]["daily_90"][-1][1] == 3
    cat.log_use(st, "dataset:99999999", "show")           # never raises
    cat.log_use(st, "no-such-slug", "show")


def test_dormant(store, tmp_path):
    r = ds.add(store, [_csv(tmp_path, "a.csv", "k,v\n1,2\n")], title="Alt")
    store.db.execute("UPDATE dataset SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (r["dataset"],))
    store.db.commit()
    c = cat.catalog(store)
    assert c["datasets"][0]["dormant"] is True and c["stats"]["dormant"] == 1
    cat.log_use(store, r["dataset"], "search_hit")         # seen does not count as use
    assert cat.catalog(store)["datasets"][0]["dormant"] is True
    cat.log_use(store, r["dataset"], "query")
    assert cat.catalog(store)["datasets"][0]["dormant"] is False


def test_lifecycle_validation_and_search(store, tmp_path):
    a = ds.add(store, [_csv(tmp_path, "a.csv", "k,v\n1,2\n")], title="Lebenszyklus Alt")
    b = ds.add(store, [_csv(tmp_path, "b.csv", "k,v\n1,3\n")], title="Lebenszyklus Neu")
    with pytest.raises(ValueError):
        ds.update(store, a["dataset"], {"lifecycle": "superseded"})                 # needs a successor
    with pytest.raises(ValueError):
        ds.update(store, a["dataset"], {"superseded_by": a["ref"]})                  # not itself
    with pytest.raises(ValueError):
        ds.update(store, a["dataset"], {"superseded_by": "dataset:99999"})
    with pytest.raises(ValueError):
        ds.update(store, a["dataset"], {"lifecycle": "gone"})
    g = ds.update(store, a["dataset"], {"superseded_by": b["ref"], "lifecycle_note": "neue Ausgabe"})
    assert g["lifecycle"] == "superseded" and g["superseded_by"] == b["dataset"] and g["lifecycle_at"]
    assert list(g)[0] == "notice" and "ERSETZT" in g["notice"] and g["successor"]["ref"] == b["ref"]
    assert ds.get(store, b["dataset"])["predecessors"][0]["ref"] == a["ref"]
    hit = [x for x in ds.search(store, "Lebenszyklus")["datasets"] if x["id"] == a["dataset"]][0]
    assert hit["successor"] == b["ref"]
    ds.update(store, a["dataset"], {"lifecycle": "withdrawn", "lifecycle_note": "fehlerhaft"})
    assert ds.get(store, a["dataset"])["superseded_by"] is None
    assert [x["id"] for x in ds.search(store, "Lebenszyklus")["datasets"]] == [b["dataset"]]
    assert len(ds.search(store, "Lebenszyklus", lifecycle="all")["datasets"]) == 2
    assert "ZURÜCKGEZOGEN" in ds.get(store, a["dataset"])["notice"]
    m.configure(store.path)
    assert json.loads(m.fundus_dataset_search("Lebenszyklus", lifecycle="all"))["total"] == 2
    assert json.loads(m.fundus_dataset_search("Lebenszyklus"))["total"] == 1
    g = ds.update(store, a["dataset"], {"lifecycle": "active", "retrieved_at": "2026-10-01", "script": "fetch.py"})
    assert g["lifecycle"] == "active" and g["retrieved_at"] == "2026-10-01" and g["script"] == "fetch.py"
    with pytest.raises(ValueError):
        ds.update(store, a["dataset"], {"retrieved_at": "gestern"})


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        return {"summary_en": "A dump of logs.", "summary_de": "Ein Log-Dump.", "keywords": ["logs"], "columns": {}}


def test_reference_dataset_and_locations(store, tmp_path):
    folder = tmp_path / "dump"
    (folder / "sub").mkdir(parents=True)
    (folder / "README.md").write_text("# Dump\nGitHub events 2024, one JSON per day.\n")
    (folder / "sub" / "2024-01-01.json").write_text('{"secret_value": 4711}\n')
    r = cat.add_reference(store, folder, title="Event-Dump", publisher="GH Archive", project="AIDev", retrieved_at="2026-11-01")
    assert r["holding"] == "reference" and r["files"] == 2
    assert not list((tmp_path / "datasets").rglob("*.json"))                     # nothing copied
    g = ds.get(store, r["ref"])
    assert g["holding"] == "reference" and g["location"]["path"] == str(folder.resolve()) and "REFERENZ" in g["notice"]
    assert {e["name"] for e in g["location"]["manifest"]} == {"README.md", "sub/2024-01-01.json"}
    assert g["bytes"] == r["bytes"] and g["tables"] == 0
    with pytest.raises(ValueError, match="Referenz"):
        ds.preview(store, r["ref"])
    with pytest.raises(ValueError, match="Referenz"):
        ds.query(store, r["ref"], "SELECT 1")
    with pytest.raises(ValueError):
        cat.add_reference(store, folder)                                         # already registered
    llm = FakeLLM()
    ds.describe_one(store, r["dataset"], llm)
    assert "GitHub events 2024" in llm.prompts[0] and "sub/2024-01-01.json" in llm.prompts[0]
    assert "4711" not in llm.prompts[0]                                          # never the data
    assert ds.search(store, "Dump")["datasets"][0]["id"] == r["dataset"]
    assert cat.refresh_locations(store)["ok"] == 1
    (folder / "sub" / "2024-01-02.json").write_text("{}\n")
    res = cat.refresh_locations(store)
    assert res["changed"] == [r["ref"]] and ds.get(store, r["ref"])["location"]["files"] == 3
    import shutil
    shutil.rmtree(folder)
    assert cat.refresh_locations(store)["missing"] == [r["ref"]]
    c = cat.catalog(store)
    assert c["stats"]["references"] == 1 and c["stats"]["bytes_reference"] > 0 and c["datasets"][0]["holding"] == "reference"
    with pytest.raises(FileNotFoundError):
        cat.add_reference(store, tmp_path / "nope")


def test_mcp_reference_and_portal_route(store, tmp_path, monkeypatch):
    from test_reviews import call
    m.configure(store.path)
    folder = tmp_path / "ref"
    folder.mkdir()
    (folder / "a.parquet").write_bytes(b"x" * 10)
    out = json.loads(m.fundus_dataset_add([str(folder)], title="Ref", reference=True))
    assert out["holding"] == "reference" and out["portal"].endswith(out["slug"])
    assert "error" in json.loads(m.fundus_dataset_add([str(folder), str(folder)], reference=True))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda c: tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    code, r = call(m, m.datasets_api, "POST", "/api/datasets/reference", body={"path": str(other), "title": "Portal-Ref"})
    assert code == 200 and r["holding"] == "reference"
    code, _ = call(m, m.datasets_api, "POST", "/api/datasets/reference", body={"path": "/etc", "title": "x"})
    assert code == 400
    code, _ = call(m, m.datasets_api, "POST", "/api/datasets/reference", body={"path": str(tmp_path / "missing")})
    assert code == 400


def test_fixity_and_orphans(store, tmp_path):
    r = ds.add(store, [_two_tables(tmp_path)], title="Fixity")
    res = cat.fixity(store)
    assert res["checked"] == 2 and res["ok"] == 2 and not res["failed"] and res["orphans"]["files"] == 0
    f = store.db.execute("SELECT sha, ext FROM dataset_file ORDER BY id LIMIT 1").fetchone()
    p = ds.file_path(store, f["sha"], f["ext"])
    os.chmod(p, 0o644)
    p.write_text("corrupted")
    res = cat.fixity(store)
    assert len(res["failed"]) == 1 and res["failed"][0]["error"] == "checksum differs"
    assert store.db.execute("SELECT count(*) FROM changelog WHERE action = 'fixity'").fetchone()[0] == 1
    g = ds.get(store, r["dataset"])
    assert sorted(t["verified_ok"] for t in g["tables_detail"]) == [0, 1] and all(t["verified_at"] for t in g["tables_detail"])
    fx = cat.catalog(store)["stats"]["fixity"]
    assert fx["failed"] == 1 and fx["failures"][0]["dataset"] == r["ref"] and fx["failures"][0]["file"].endswith(".csv")
    res = cat.fixity(store, budget_gb=0)                                       # budget: at least one file, then stop
    assert res["checked"] == 1 and res["left"] == 1
    # orphans: unreferenced and tmp files older than 24 h, stale incoming folders; fresh ones are left alone
    root = ds.files_dir(store)
    old = time.time() - 2 * 86400
    (root / "ab").mkdir(exist_ok=True)
    for name in ("ab" + "0" * 62 + ".csv", "ab" + "1" * 62 + ".csv.tmp123"):
        (root / "ab" / name).write_text("orphan")
        os.utime(root / "ab" / name, (old, old))
    (root / "ab" / ("ab" + "2" * 62 + ".csv")).write_text("fresh")
    inc = root / "incoming" / "w1"
    inc.mkdir(parents=True)
    (inc / "x.csv").write_text("123")
    os.utime(inc / "x.csv", (old, old))
    os.utime(inc, (old, old))
    o = cat.orphans(store)
    assert o["files"] == 3 and {x["kind"] for x in o["list"]} == {"file", "tmp", "incoming"}
    o2 = cat.catalog(store)["stats"]["orphans"]
    assert o2["files"] == 3 and len(o2["list"]) == 3 and o2["bytes"] > 0
    assert cat.purge_orphans(store)["deleted"] == 3 and cat.orphans(store)["files"] == 0
    assert (root / "ab" / ("ab" + "2" * 62 + ".csv")).exists() and ds.get(store, r["dataset"])["tables"] == 2


def test_citation(store, tmp_path):
    r = ds.add(store, [_csv(tmp_path, "a.csv", "k,v\n1,2\n")], title="Arbeitsmarkt", publisher="Bundesagentur für Arbeit (Statistik)",
               source_title="Eckwerte", url_="https://statistik.example", published="2026-10-30", retrieved_at="2026-11-02")
    g = ds.get(store, r["dataset"])
    assert g["citation"] == (f"Bundesagentur für Arbeit (2026): Eckwerte. https://statistik.example. Abgerufen 2026-11-02. "
                             f"Fundus dataset:{r['dataset']} v1.")
    assert g["citation_bibtex"].startswith(f"@misc{{fundus_dataset_{r['dataset']},") and "year = {2026}" in g["citation_bibtex"]
    r2 = ds.add(store, [_csv(tmp_path, "b.csv", "k\n1\n")], title="Ohne alles")
    assert "o. A. (o. J.): Ohne alles." in ds.get(store, r2["dataset"])["citation"]


def test_catalog_shape_and_facets(store, tmp_path):
    from test_reviews import call
    store.db.execute("INSERT INTO project(slug, title) VALUES ('northwind', 'Northwind KI')")
    store.db.commit()
    a = ds.add(store, [_two_tables(tmp_path)], title="Katalog A", project="northwind", visibility="internal",
               publisher="Eurostat", frequency="annual")
    ds.update(store, a["dataset"], {"tags": {"topic": ["ai-adoption"], "region": ["ch"]}, "period": "2009-H1 – 2026-H1"})
    b = ds.add(store, [_csv(tmp_path, "b.csv", "k,v\n1,2\n")], title="Katalog B", inputs=[a["ref"]])
    gone = ds.add(store, [_csv(tmp_path, "c.csv", "k,v\n1,2\n")], title="Katalog C")
    ds.remove(store, gone["dataset"])
    m.configure(store.path)
    code, c = call(m, m.datasets_api, path="/api/datasets/catalog")
    assert code == 200 and set(c) == {"stats", "facets", "datasets"}
    st = c["stats"]
    for k in ("datasets", "tables", "rows", "bytes_original", "bytes_parquet", "bytes_reference", "internal", "update_due", "dormant",
              "superseded", "withdrawn", "references", "fixity", "uses_30d", "orphans"):
        assert k in st, k
    assert st["datasets"] == 2 and st["tables"] == 3 and st["internal"] == 2 and st["bytes_parquet"] > 0   # B inherits internal
    assert set(st["fixity"]) == {"ok", "failed", "unchecked", "oldest_verified", "failures"} and st["orphans"]["list"] == []
    f = c["facets"]
    assert set(f) == {"project", "industry", "topic", "region", "publisher_group", "frequency", "visibility", "lifecycle", "holding",
                      "kind", "jurisdiction", "act_type", "size", "status"}
    assert {"value": "northwind", "label": "Northwind KI", "count": 1} in f["project"]
    assert f["topic"][0]["value"] == "ai-adoption" and f["kind"] == [{"value": "data", "label": "Daten", "count": 2}]
    assert f["size"][0]["value"] == "lt10mb"
    x = {d["id"]: d for d in c["datasets"]}
    da = x[a["dataset"]]
    for k in ("period_from", "period_to", "size_class", "bytes_disk", "uses_30d", "seen_30d", "last_used", "lifecycle", "superseded_by",
              "holding", "dormant", "publisher_group", "inputs_n", "used_by_n", "slides_n", "columns", "columns_max", "columns_total"):
        assert k in da, k
    assert (da["period_from"], da["period_to"]) == (2009, 2026) and da["used_by_n"] == 1 and x[b["dataset"]]["inputs_n"] == 1
    assert da["bytes_disk"] > 0 and da["publisher_group"] == "Eurostat" and da["columns"] == 3
    code, rem = call(m, m.datasets_api, query=b"removed=1", path="/api/datasets/catalog")
    assert [d["id"] for d in rem["datasets"]] == [gone["dataset"]]
    code, _ = call(m, m.datasets_api, path=f"/api/datasets/{a['dataset']}", path_params={"did": a["dataset"]})
    assert m._store().db.execute("SELECT tool, actor FROM dataset_use").fetchall()[-1][:] == ("portal_view", "portal")
    code, u = call(m, m.datasets_api, "POST", f"/api/datasets/{b['dataset']}", path_params={"did": b["dataset"]},
                   body={"action": "update", "changes": {"lifecycle": "superseded", "superseded_by": a["ref"], "script": "s.py"}})
    assert code == 200 and u["lifecycle"] == "superseded" and u["script"] == "s.py"


def test_version_changes(store, tmp_path):
    d = tmp_path / "v1"
    d.mkdir()
    (d / "main.csv").write_text("a,b\n1,2\n3,4\n")
    (d / "old.csv").write_text("q\n1\n")
    r = ds.add(store, [d], title="Versionen")
    d2 = tmp_path / "v2"
    d2.mkdir()
    (d2 / "main.csv").write_text("a,c\n1,2\n3,4\n5,6\n")
    (d2 / "new.csv").write_text("n\n1\n")
    ds.add(store, [d2], dataset=r["dataset"])
    v = {x["n"]: x for x in ds.get(store, r["dataset"])["versions"]}
    ch = v[2]["changes"]
    assert ch["tables_added"] == ["new"] and ch["tables_removed"] == ["old"]
    assert ch["columns_added"] == {"main": ["c"]} and ch["columns_removed"] == {"main": ["b"]} and ch["rows_delta"] == 1
    assert "changes" not in v[1]


def test_lineage_two_levels(store, tmp_path):
    mk = lambda n, inputs=None: ds.add(store, [_csv(tmp_path, f"{n}.csv", "k\n1\n")], title=n, inputs=inputs)
    a = mk("a")
    b = mk("b", [a["ref"]])
    c = mk("c", [b["ref"]])
    d = mk("d", [c["ref"]])
    e = mk("e", [d["ref"]])
    lin = ds.get(store, c["dataset"])["lineage"]
    depth = {n["ref"]: n["depth"] for n in lin["nodes"]}
    assert depth == {a["ref"]: -2, b["ref"]: -1, c["ref"]: 0, d["ref"]: 1, e["ref"]: 2}
    assert {"from": b["ref"], "to": c["ref"]} in lin["edges"] and len(lin["edges"]) == 4
    lin = ds.get(store, a["dataset"])["lineage"]
    assert {n["depth"] for n in lin["nodes"]} == {0, 1, 2}


def test_period_years_and_size_class():
    assert cat.period_years("1999-2026") == (1999, 2026)
    assert cat.period_years("2009-H1 – 2026-H1") == (2009, 2026)
    assert cat.period_years("2025") == (2025, 2025) and cat.period_years("Q3") == (None, None)
    assert cat.size_class(5) == "lt10mb" and cat.size_class(50 * 1024 ** 2) == "lt100mb" and cat.size_class(2 * 1024 ** 3) == "ge1gb"
