"""Free-text questions to the dataset catalogue (fundus.dataset_ask) with a fake local model: two stages, candidates
from the model plus keyword search, validation (refs, columns, related, answer), visibility, cache, MCP, CLI and API."""
import json

import pytest

from fundus import dataset_ask as da
from fundus import datasets as ds
from fundus import mcp_server as m


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_DATASET_DIR", str(tmp_path / "datasets"))
    yield
    da.set_llm(None)
    m.configure(m.DEFAULT_DB)


class FakeLLM:
    model = "fake-gemma"

    def __init__(self, stage1, stage2):
        self.stage1, self.stage2, self.calls = stage1, stage2, []

    def complete(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        return self.stage1 if "terms" in schema["properties"] else self.stage2


def make(store, tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "postings.csv").write_text("date,jobcountry,AI_share_postings\n2025-01-01,DE,1.2\n")
    (d / "oews.csv").write_text("OCC_CODE,OCC_TITLE,A_MEAN\n11-2021,Marketing Managers,160000\n")
    (d / "kunde.csv").write_text("firma,umsatz\nA,1\n")
    a = ds.add(store, [d / "postings.csv"], title="Indeed AI tracker", description="Share of job postings mentioning AI")
    b = ds.add(store, [d / "oews.csv"], title="BLS OEWS wages by occupation", description="Employment and wages by occupation")
    c = ds.add(store, [d / "kunde.csv"], title="Kundenumsatz", description="internal client data", visibility="internal",
               project="Northwind")
    return a["dataset"], b["dataset"], c["dataset"]


def test_two_stages_validation_and_cache(store, tmp_path):
    a, b, c = make(store, tmp_path)
    fake = FakeLLM({"terms": ["Stellenanzeigen", "occupation"], "candidates": [f"dataset:{b}", "dataset:999", f"dataset:{c}"]},
                   {"answer": "ja", "summary": "Nur KI-Anteil der Anzeigen.",
                    "hits": [{"ref": f"dataset:{a}", "fit": "teilweise", "why": "nur KI-Anteil",
                              "columns": ["AI_share_postings", "postings: date", "no_such_col"]},
                             {"ref": "dataset:999", "fit": "direkt", "why": "erfunden", "columns": []},
                             {"ref": f"dataset:{c}", "fit": "direkt", "why": "intern", "columns": []}],
                    "related": [{"ref": f"dataset:{b}", "why": "Löhne für Marketing-Berufe"},
                                {"ref": f"dataset:{a}", "why": "doppelt"}]})
    da.set_llm(fake)
    r = da.ask(store, "Haben wir job postings zum Marketing?")
    # stage 1 saw the public catalogue only (no internal dataset), stage 2 the candidates incl. keyword hits
    assert "Kundenumsatz" not in fake.calls[0][0] and "Indeed AI tracker" in fake.calls[0][0]
    assert f"dataset:{a}" in fake.calls[1][1] and f"dataset:{b}" in fake.calls[1][1] and "Kundenumsatz" not in fake.calls[1][1]
    assert [h["id"] for h in r["hits"]] == [a]                         # invented and invisible refs dropped
    assert r["hits"][0]["columns"] == ["AI_share_postings", "date"]    # only real columns, "table: col" unwrapped
    assert r["answer"] == "teilweise"                                  # follows the surviving hits, not the model's "ja"
    assert [x["id"] for x in r["related"]] == [b] and r["related"][0]["title"].startswith("BLS")
    assert r["candidates"] == 2 and r["model"] == "fake-gemma" and not r["cached"]
    n = len(fake.calls)
    assert da.ask(store, "  haben wir JOB postings zum marketing? ")["cached"] and len(fake.calls) == n
    ri = da.ask(store, "Haben wir job postings zum Marketing?", include_internal=True)
    assert not ri["cached"] and [h["id"] for h in ri["hits"]] == [a, c]
    with pytest.raises(ValueError):
        da.ask(store, " ")


def test_no_candidates_and_bad_answers(store, tmp_path):
    make(store, tmp_path)
    fake = FakeLLM({"terms": [], "candidates": []}, {"answer": "ja", "summary": "x", "hits": [], "related": []})
    da.set_llm(fake)
    r = da.ask(store, "Zuckerkonsum qqq")
    assert r["answer"] == "nein" and r["hits"] == [] and len(fake.calls) == 1     # no stage 2 without candidates
    fake2 = FakeLLM({"terms": "kaputt", "candidates": None}, "not a dict")
    da.set_llm(fake2)
    r = da.ask(store, "Indeed postings")                                  # keyword search still finds a candidate
    assert r["candidates"] >= 1 and r["answer"] == "nein" and r["hits"] == []


def test_mcp_cli_and_api(store, tmp_path, capsys):
    from test_reviews import call
    a, b, c = make(store, tmp_path)
    da.set_llm(FakeLLM({"terms": [], "candidates": [f"dataset:{a}"]},
                       {"answer": "ja", "summary": "s", "hits": [{"ref": f"dataset:{a}", "fit": "direkt", "why": "w",
                                                                   "columns": ["date"]}], "related": []}))
    m.configure(store.path)
    out = json.loads(m.fundus_dataset_search("job postings", ai=True))
    assert out["answer"] == "ja" and out["hits"][0]["ref"] == f"dataset:{a}"
    assert "datasets" in json.loads(m.fundus_dataset_search("job postings"))       # plain word search unchanged
    code, r = call(m, m.datasets_api, "POST", "/api/datasets/ask", body={"question": "job postings"})
    assert code == 200 and r["hits"][0]["id"] == a
    code, r = call(m, m.datasets_api, "POST", "/api/datasets/ask", body={"question": ""})
    assert code == 400
    from fundus import cli
    assert cli.main(["--db", str(store.path), "datasets", "ask", "job", "postings"]) in (0, None)
    assert json.loads(capsys.readouterr().out)["answer"] == "ja"
