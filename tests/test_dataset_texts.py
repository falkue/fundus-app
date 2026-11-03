"""Texts with versions (fundus.dataset_texts): ingest as documents with validity, closing of the previous version,
version_at, explicit valid_to kept, description input limited to metadata/beginning/headings, search and catalog facets,
conversion of a stored dataset, CLI and MCP."""
import json

import pytest

from fundus import dataset_catalog as cat
from fundus import dataset_texts as tx
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
        return {"summary_en": "The EU regulation on artificial intelligence.", "summary_de": "Die KI-Verordnung der EU.",
                "keywords": ["AI regulation"], "geo": "EU", "industry": [], "topic": [], "region": []}


BODY = "SECRET-PROVISION-BODY "


def law_html(path, title="Regulation (EU) 2024/1689 laying down harmonised rules on artificial intelligence", articles=60):
    parts = [f"<html><head><title>{title}</title><style>p {{color: red}}</style></head><body>",
             f"<p class='doc-ti'>{title}</p>", "<p>THE EUROPEAN PARLIAMENT AND THE COUNCIL OF THE EUROPEAN UNION,</p>",
             "<p>Whereas:</p>", "<p>(1) The purpose of this Regulation is to improve the functioning of the internal market.</p>"]
    parts += [f"<p>({i}) Recital text number {i} about trustworthy AI and fundamental rights.</p>" for i in range(2, 60)]
    parts.append("<p class='ti-section'>CHAPTER I</p><p>GENERAL PROVISIONS</p>")
    for i in range(1, articles + 1):
        parts.append(f"<p class='ti-art'>Article {i}</p><p class='sti-art'>Subject {i}</p><p>{BODY * 40}</p>")
    parts.append("<p class='ti-grseq'>ANNEX I</p><p>List of Union harmonisation legislation</p></body></html>")
    path.write_text("".join(parts))
    return path


def test_add_text_and_new_version_closes_previous(store, tmp_path):
    v1 = law_html(tmp_path / "32024R1689_en.html")
    r = ds.add(store, [v1], title="EU AI Act", text=True, act_type="regulation", identifier="CELEX 32024R1689",
               jurisdiction="EU", issuer="European Parliament and Council", valid_from="2024-08-01",
               version_label="Originalfassung", published_at="2024-07-12", source_url="https://eur-lex.europa.eu/eli/reg/2024/1689/oj")
    assert r["text"] and r["documents"] == ["32024R1689_en.html"] and r["tables"] == []
    d = ds._row(store, r["dataset"])
    assert d["act_type"] == "regulation" and d["identifier"] == "CELEX 32024R1689" and d["frequency"] == "irregular"
    # a CSV in a text stays a document, never a table
    v2dir = tmp_path / "v2"
    v2dir.mkdir()
    law_html(v2dir / "02024R1689-20260727_en.html")
    (v2dir / "versions.csv").write_text("celex,date\n32024R1689,2024-07-12\n")
    r2 = ds.add(store, [v2dir], dataset=r["dataset"], valid_from="2026-07-27", version_label="konsolidierte Fassung vom 27.07.2026")
    assert r2["version"] == 2 and r2["tables"] == [] and len(r2["documents"]) == 2
    assert r2["closed"] == ["version 1 now valid until 2026-07-26"]
    vs = {v["n"]: v for v in ds.get(store, r["dataset"])["versions"]}
    assert vs[1]["valid_to"] == "2026-07-26" and vs[2]["valid_to"] is None
    assert vs[2]["in_force"] and not vs[1]["in_force"]
    assert [f["file"] for f in vs[1]["files"]] == ["32024R1689_en.html"]
    b = ds.get(store, r["dataset"])
    assert b["kind"] == "text" and b["in_force"]["n"] == 2 and b["jurisdiction"] == "EU"


def test_text_needs_act_type_and_valid_from(store, tmp_path):
    f = law_html(tmp_path / "a.html")
    with pytest.raises(ValueError, match="act_type"):
        ds.add(store, [f], text=True, valid_from="2024-01-01")
    with pytest.raises(ValueError, match="valid_from"):
        ds.add(store, [f], text=True, act_type="law")
    with pytest.raises(ValueError, match="act_type"):
        ds.add(store, [f], text=True, act_type="novel", valid_from="2024-01-01")
    with pytest.raises(ValueError, match="before"):
        ds.add(store, [f], text=True, act_type="law", valid_from="2024-01-01", valid_to="2023-01-01")
    assert not store.db.execute("SELECT count(*) FROM dataset").fetchone()[0]


def test_version_at(store, tmp_path):
    r = ds.add(store, [law_html(tmp_path / "a.html")], text=True, act_type="law", valid_from="2020-01-01")
    ds.add(store, [law_html(tmp_path / "b.html")], dataset=r["dataset"], valid_from="2023-07-01")
    assert tx.version_at(store, r["dataset"], "2019-12-31")["version"] is None
    assert "before the first" in tx.version_at(store, r["dataset"], "2019-12-31")["why"]
    assert tx.version_at(store, r["dataset"], "2020-01-01")["version"] == 1
    assert tx.version_at(store, r["dataset"], "2023-06-30")["version"] == 1
    at = tx.version_at(store, r["dataset"], "2023-07-01")
    assert at["version"] == 2 and at["valid_from"] == "2023-07-01" and at["files"][0]["file"] == "b.html"
    assert tx.version_at(store, r["dataset"], "2030-01-01")["version"] == 2
    with pytest.raises(ValueError):
        tx.version_at(store, r["dataset"], "1.1.2020")


def test_explicit_valid_to_kept_and_historic_version_fits_in(store, tmp_path):
    r = ds.add(store, [law_html(tmp_path / "a.html")], text=True, act_type="law", valid_from="2020-01-01", valid_to="2021-12-31")
    r2 = ds.add(store, [law_html(tmp_path / "b.html")], dataset=r["dataset"], valid_from="2023-01-01")
    vs = {v["n"]: v for v in tx.versions(store, ds._row(store, r["dataset"]))}
    assert vs[1]["valid_to"] == "2021-12-31" and r2["closed"] == []          # explicit valid_to never overwritten
    # a gap: nothing valid in 2022
    assert tx.version_at(store, r["dataset"], "2022-06-01")["why"].startswith("no version covers")
    # a historic version added later ends where the next one starts
    r3 = ds.add(store, [law_html(tmp_path / "c.html")], dataset=r["dataset"], valid_from="2022-01-01")
    assert "version 3 valid until 2022-12-31" in r3["closed"][0]
    assert tx.version_at(store, r["dataset"], "2022-06-01")["version"] == 3
    # correcting a version by hand
    out = tx.set_version(store, r["dataset"], 1, {"valid_to": "", "version_label": "Urfassung"})
    assert out["changed"] == ["valid_to", "version_label"]
    with pytest.raises(ValueError):
        tx.set_version(store, r["dataset"], 1, {"valid_to": "2019-01-01"})


def test_description_input_is_limited(store, tmp_path):
    f = law_html(tmp_path / "law_en.html", articles=400)
    r = ds.add(store, [f], title="EU AI Act", text=True, act_type="regulation", identifier="CELEX 32024R1689",
               jurisdiction="EU", valid_from="2024-08-01", version_label="Originalfassung")
    llm = FakeLLM()
    ds.describe_one(store, r["dataset"], llm=llm)
    p = llm.prompts[0]
    assert "CELEX 32024R1689" in p and "Originalfassung" in p and "Regulation (EU) 2024/1689" in p
    assert "THE EUROPEAN PARLIAMENT" in p                      # preamble
    assert "Article 1 – Subject 1" in p and "CHAPTER I – GENERAL PROVISIONS" in p   # headings with their titles
    assert BODY.strip() not in p                               # never the provisions themselves
    assert "color: red" not in p
    assert len(p) < tx.PREAMBLE_CHARS + tx.TOC_CHARS + 6000     # capped: metadata + beginning + headings + instructions
    assert "… (more headings)" in p
    d = ds._row(store, r["dataset"])
    assert d["status"] == "described" and d["summary_de"] == "Die KI-Verordnung der EU."
    ann = json.loads(d["annotation"])
    assert ann["input"]["file"] == "law_en.html" and ann["input"]["preamble_chars"] <= tx.PREAMBLE_CHARS


def test_main_document_prefers_html_and_english(store, tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    law_html(d / "x_de.xhtml", title="Verordnung (EU) 2024/1689")
    law_html(d / "x_en.xhtml")
    (d / "README.md").write_text("# readme")
    (d / "x_en.pdf").write_bytes(b"%PDF-1.4 not really")
    r = ds.add(store, [d], text=True, act_type="regulation", valid_from="2024-08-01")
    assert tx.main_document(store, ds._row(store, r["dataset"]))["name"] == "x_en.xhtml"


def test_search_and_catalog_facets(store, tmp_path):
    ds.add(store, [law_html(tmp_path / "a.html")], title="EU AI Act", text=True, act_type="regulation",
           identifier="CELEX 32024R1689", jurisdiction="EU", valid_from="2024-08-01")
    ds.add(store, [law_html(tmp_path / "b.html")], title="Datenschutzgesetz", text=True, act_type="law",
           identifier="SR 235.1", jurisdiction="CH", valid_from="2023-09-01")
    csv = tmp_path / "t.csv"
    csv.write_text("a,b\n1,2\n")
    ds.add(store, [csv], title="Eine Tabelle")
    assert [x["title"] for x in ds.search(store, "32024R1689")["datasets"]] == ["EU AI Act"]
    assert [x["title"] for x in ds.search(store, "235.1")["datasets"]] == ["Datenschutzgesetz"]
    assert ds.search(store, kind="text")["total"] == 2
    assert [x["title"] for x in ds.search(store, kind="data")["datasets"]] == ["Eine Tabelle"]
    assert [x["title"] for x in ds.search(store, jurisdiction="ch")["datasets"]] == ["Datenschutzgesetz"]
    assert [x["title"] for x in ds.search(store, act_type="regulation")["datasets"]] == ["EU AI Act"]
    c = cat.catalog(store)
    assert c["stats"]["texts"] == 2
    kinds = {x["value"]: x for x in c["facets"]["kind"]}
    assert kinds["text"]["count"] == 2 and kinds["text"]["label"] == "Text mit Fassungen"
    assert {x["value"]: x["count"] for x in c["facets"]["jurisdiction"]} == {"EU": 1, "CH": 1}
    assert {x["value"]: x["label"] for x in c["facets"]["act_type"]} == {"regulation": "Verordnung (EU-Verordnung)", "law": "Gesetz"}
    item = next(x for x in c["datasets"] if x["title"] == "EU AI Act")
    assert item["kind"] == "text" and item["in_force"]["n"] == 1 and item["identifier"] == "CELEX 32024R1689"


def test_texts_are_tracked_as_irregular(store, tmp_path):
    from datetime import date
    r = ds.add(store, [law_html(tmp_path / "a.html")], text=True, act_type="law", valid_from="2020-01-01",
               update_hint="https://www.fedlex.admin.ch – neue konsolidierte Fassung prüfen")
    due = {x["ref"]: x for x in ds.due(store)}
    assert due[r["ref"]]["reason"] == "never_checked" and "fedlex" in due[r["ref"]]["update_hint"]
    ds.mark_checked(store, r["dataset"])
    assert r["ref"] not in {x["ref"] for x in ds.due(store)}
    assert {x["ref"] for x in ds.due(store, today=date(2030, 1, 1))} == {r["ref"]}     # stale after 180 days


def _stored_dataset(store, tmp_path):
    src = tmp_path / "src"
    (src / "raw").mkdir(parents=True)
    (src / "README.md").write_text("# EU AI Act\nOJ text and consolidated versions\n")
    (src / "versions.csv").write_text("celex,date\n32024R1689,2024-07-12\n02024R1689-20260727,2026-07-27\n")
    law_html(src / "raw" / "32024R1689_en.xhtml")
    (src / "raw" / "32024R1689_en.pdf").write_bytes(b"%PDF-1.4 fake")
    law_html(src / "raw" / "02024R1689-20240712_de.xhtml", title="Verordnung (EU) 2024/1689")
    law_html(src / "raw" / "02024R1689-20260727_en.xhtml")
    r = ds.add(store, [src / "README.md", src / "versions.csv", src / "raw" / "32024R1689_en.pdf"], title="EU AI Act",
               frequency="irregular", update_hint="eu_ai_act_cellar.py")
    return src, r


def spec(src):
    return {"identifier": "CELEX 32024R1689", "jurisdiction": "EU", "act_type": "regulation",
            "issuer": "European Parliament and Council of the European Union", "base": str(src), "tables_as_documents": True,
            "versions": [
                {"version": 1, "files": ["raw/32024R1689_en.xhtml"], "valid_from": "2024-07-12", "valid_to": "2026-07-26",
                 "version_label": "Amtsblatt-Fassung", "published_at": "2024-07-12"},
                {"files": ["raw/02024R1689-20240712_de.xhtml", "README.md"], "valid_from": "2024-07-12",
                 "version_label": "konsolidierte Fassung vom 12.07.2024"},
                {"files": ["raw/02024R1689-20260727_en.xhtml", "README.md"], "valid_from": "2026-07-27",
                 "version_label": "konsolidierte Fassung vom 27.07.2026", "source_url": "https://eur-lex.europa.eu/x"}]}


def test_convert_text(store, tmp_path):
    src, r = _stored_dataset(store, tmp_path)
    plan = tx.convert(store, r["dataset"], spec(src), dry_run=True)
    assert plan["dry_run"] and len(plan["plan"]) == 3
    assert not ds._row(store, r["dataset"])["act_type"]                 # dry run changes nothing
    out = tx.convert(store, r["dataset"], spec(src))
    assert out["fields"]["act_type"] == "regulation" and out["tables_as_documents"] == 1
    assert "v1: 32024R1689_en.xhtml" in out["files_added"]
    vs = {v["n"]: v for v in out["versions"]}
    assert vs[1]["valid_to"] == "2026-07-26" and vs[2]["valid_to"] == "2026-07-26" and vs[3]["valid_to"] is None
    assert out["in_force"]["n"] == 3
    assert tx.version_at(store, r["dataset"], "2025-01-01")["version"] == 2     # consolidated wins over the OJ text
    d = ds.get(store, r["dataset"])
    assert d["tables"] == 0 and d["status"] == "new" and d["kind"] == "text"
    with pytest.raises(ValueError, match="already a text"):
        tx.convert(store, r["dataset"], spec(src))


def test_convert_refuses_missing_files(store, tmp_path):
    src, r = _stored_dataset(store, tmp_path)
    sp = spec(src)
    sp["versions"][2]["files"] = ["raw/nope.xhtml"]
    with pytest.raises(FileNotFoundError):
        tx.convert(store, r["dataset"], sp)
    assert not ds._row(store, r["dataset"])["act_type"]


def test_cli_and_mcp(store, tmp_path, capsys):
    from fundus.cli import main
    db = str(store.path)
    f = law_html(tmp_path / "law.html")
    assert main(["--db", db, "datasets", "add", str(f), "--text", "--title", "DSG", "--act-type", "law", "--identifier", "SR 235.1",
                 "--jurisdiction", "CH", "--issuer", "Bundesversammlung", "--valid-from", "2023-09-01",
                 "--version-label", "Fassung vom 01.09.2023"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["text"] and res["version"] == 1
    assert main(["--db", db, "datasets", "at", res["ref"], "2024-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert main(["--db", db, "datasets", "version", res["ref"], "1", "--source-url", "https://www.fedlex.admin.ch/eli/cc/2022/491/de"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == ["source_url"]
    m.configure(db)
    out = json.loads(m.fundus_dataset_add([str(law_html(tmp_path / "law2.html"))], dataset=res["ref"], valid_from="2025-01-01",
                                          version_label="Fassung vom 01.01.2025"))
    assert out["version"] == 2 and out["closed"] and "headings" in out["description"]
    at = json.loads(m.fundus_dataset(res["ref"], action="at", at="2024-06-01"))
    assert at["version"] == 1 and at["valid_to"] == "2024-12-31"
    hits = json.loads(m.fundus_dataset_search("", kind="text", jurisdiction="CH"))
    assert hits["total"] == 1
    v = json.loads(m.fundus_dataset(res["ref"], action="version", version=2, changes={"published_at": "2024-12-20"}))
    assert v["changed"] == ["published_at"]


def test_portal_upload_of_a_new_version(store, tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    r = ds.add(store, [law_html(tmp_path / "a.html")], text=True, act_type="terms", jurisdiction="global",
               issuer="Example AI Inc.", valid_from="2025-01-01")
    m.configure(store.path)
    app = Starlette(routes=[Route("/api/datasets/upload", m.dataset_upload, methods=["POST"])])
    with TestClient(app) as c:
        bad = c.post("/api/datasets/upload", files=[("files", ("b.html", b"<p>Terms</p>", "text/html"))], data={"dataset": str(r["dataset"])})
        assert bad.status_code == 400 and "valid_from" in bad.json()["error"]
        up = c.post("/api/datasets/upload", files=[("files", ("b.html", b"<p>Terms</p>", "text/html"))],
                    data={"dataset": str(r["dataset"]), "valid_from": "2026-03-01", "version_label": "Terms of 1 March 2026"})
        assert up.status_code == 200 and up.json()["version"] == 2
    assert tx.version_at(store, r["dataset"], "2026-02-28")["version"] == 1
