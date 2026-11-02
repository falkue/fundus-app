"""Archive (fundus.archive): archived slides leave the project/set lists and counts but stay findable (search, slide
search, fundus_exhibit, decks); new renders keep them archived; unarchive lists them again; MCP tool and portal API."""
import json

import pytest

from fundus import archive
from fundus import decks
from fundus import exhibits as ex
from fundus import mcp_server as m
from fundus import query
from fundus import slide_search as ss
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _mcp_back():
    yield
    m.configure(m.DEFAULT_DB)


def setup(store, root):
    a = reg(store, write(root, "Contoso/exhibits/cost", page("Kosten im Service"), png=png_bytes(rgb=(1, 2, 3))))
    b = reg(store, write(root, "Contoso/exhibits/plan", page("Plan für Grinder"), png=png_bytes(rgb=(7, 8, 9))))
    decks.set_sequence(store, "contoso", "deck", [str(a["exhibit_id"]), str(b["exhibit_id"])])
    return a, b


def listed(store, project="contoso"):
    return {x["id"] for x in ex.gallery(store, project)["exhibits"]}


def test_archive_hides_from_lists_keeps_findable(store, projects_root):
    a, b = setup(store, projects_root)
    r = archive.archive(store, "slide", [a["exhibit_id"]], actor="test")
    assert r["archived"] == 1
    assert listed(store) == {b["exhibit_id"]}
    p = next(x for x in ex.gallery(store)["projects"] if x["project"] == "contoso")
    assert p["exhibits"] == 1 and p["archived"] == 1 and p["sets"][0]["exhibits"] == 1 and p["sets"][0]["archived"] == 1
    hits = query.search(store, "Kosten Service", kind="exhibit")["results"]
    assert any(h.get("id") == a["exhibit_id"] and h.get("archived") for h in hits)            # findable, marked
    assert ss._brief(store, a["exhibit_id"], {}, {}, True, None)["archived"]
    got = ex.get(store, a["exhibit_id"], include_internal=True, script=False)
    assert got["archived"]["by"] == "test"
    assert [s["id"] for s in decks.get_sequence(store, "deck", project="contoso")["slides"]] == [a["exhibit_id"], b["exhibit_id"]]
    lst = archive.listing(store)
    assert lst["total"] == 1 and lst["items"][0]["ref"] == "contoso/main/cost" and lst["projects"][0]["n"] == 1
    assert archive.listing(store, q="Grinder")["total"] == 0 and archive.listing(store, q="kosten")["total"] == 1
    # a new render adds the version, the slide stays archived
    v2 = reg(store, write(projects_root, "Contoso/exhibits/cost", page("Kosten im Service, neu"), png=png_bytes(rgb=(1, 2, 4))))
    assert v2["exhibit_id"] == a["exhibit_id"] and v2["version"] == 2 and listed(store) == {b["exhibit_id"]}
    assert archive.unarchive(store, "slide", [a["exhibit_id"]])["unarchived"] == 1
    assert listed(store) == {a["exhibit_id"], b["exhibit_id"]} and archive.count(store) == 0


def test_archive_set_and_project(store, projects_root):
    a, b = setup(store, projects_root)
    assert archive.archive(store, "set", ["contoso/main"])["archived"] == 2
    assert listed(store) == set() and "contoso" in [p["project"] for p in ex.gallery(store)["projects"]]
    assert archive.archive(store, "project", ["contoso"])["already"] == 2
    assert archive.unarchive(store, "project", ["contoso"])["unarchived"] == 2
    with pytest.raises(ValueError):
        archive.archive(store, "folder", ["x"])


def test_mcp_and_portal_api(store, projects_root):
    from test_reviews import call
    a, b = setup(store, projects_root)
    m.configure(store.path)
    out = json.loads(m.fundus_exhibit_archive(action="archive", refs=[str(a["exhibit_id"])]))
    assert out["archived"] == 1
    assert json.loads(m.fundus_exhibit_archive())["total"] == 1
    assert "error" in json.loads(m.fundus_exhibit_archive(action="archive"))
    code, lst = call(m, m.archive_api, path="/api/archive")
    assert code == 200 and lst["items"][0]["id"] == a["exhibit_id"] and lst["items"][0]["preview"]
    code, r = call(m, m.archive_api, "POST", "/api/archive", body={"action": "unarchive", "refs": [str(a["exhibit_id"])]})
    assert code == 200 and r["unarchived"] == 1
    code, _ = call(m, m.archive_api, "POST", "/api/archive", body={"action": "archive", "refs": ["1"]}, origin="http://evil.example")
    assert code == 403
