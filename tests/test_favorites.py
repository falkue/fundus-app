"""Favourites (fundus.favorites): mark/unmark slides, flags in gallery, set counts, show and search hits, slide search
with favorites=True (also without a question), fundus_slides(['favorites']), MCP tool and portal API."""
import json

import pytest

from fundus import exhibits as ex
from fundus import favorites as fav
from fundus import mcp_server as m
from fundus import slide_search as ss
from test_archive import setup
from test_exhibits import projects as projects_root  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _mcp_back():
    yield
    m.configure(m.DEFAULT_DB)


def test_mark_flags_and_lists(store, projects_root):
    a, b = setup(store, projects_root)
    r = fav.mark(store, [a["exhibit_id"]], actor="test")
    assert r["marked"] == 1 and fav.mark(store, [a["exhibit_id"]])["unchanged"] == 1
    items = {x["id"]: x for x in ex.gallery(store, "contoso")["exhibits"]}
    assert items[a["exhibit_id"]]["favorite"] is True and "favorite" not in items[b["exhibit_id"]]
    p = next(x for x in ex.gallery(store)["projects"] if x["project"] == "contoso")
    assert p["favorites"] == 1 and p["sets"][0]["favorites"] == 1
    assert ex.get(store, a["exhibit_id"], include_internal=True, script=False)["favorite"]["by"] == "test"
    assert fav.ids(store) == [a["exhibit_id"]] and fav.ids(store, "contoso", "main") == [a["exhibit_id"]]
    lst = fav.listing(store)
    assert lst["count"] == 1 and lst["by_project"] == {"contoso": 1} and lst["slides"][0]["favorite"]
    assert [s["id"] for s in ss.resolve_refs(store, ["favorites"])["slides"]] == [a["exhibit_id"]]
    assert [s["id"] for s in ss.resolve_refs(store, ["favorites:contoso/main"])["slides"]] == [a["exhibit_id"]]
    res = ss.search(store, "", favorites=True, aspects=False)
    assert [s["id"] for s in res["slides"]] == [a["exhibit_id"]]
    res = ss.search(store, "Grinder Plan", favorites=True, aspects=False)
    assert [s["id"] for s in res["slides"]] == [a["exhibit_id"]]       # only favourites, even if others rank higher
    with pytest.raises(ValueError):
        ss.search(store, "", aspects=False)
    assert fav.mark(store, [a["exhibit_id"]], on=False)["unmarked"] == 1 and fav.ids(store) == []


def test_mcp_and_portal_api(store, projects_root):
    from test_reviews import call
    a, b = setup(store, projects_root)
    m.configure(store.path)
    assert json.loads(m.fundus_exhibit_favorite(action="add", refs=[str(b["exhibit_id"])]))["marked"] == 1
    assert json.loads(m.fundus_exhibit_favorite())["count"] == 1
    assert "error" in json.loads(m.fundus_exhibit_favorite(action="add"))
    out = json.loads(m.fundus_slide_search(favorites=True, aspects=False))
    assert [s["id"] for s in out["slides"]] == [b["exhibit_id"]]
    code, lst = call(m, m.favorites_api, path="/api/favorites")
    assert code == 200 and lst["slides"][0]["id"] == b["exhibit_id"]
    code, r = call(m, m.favorites_api, "POST", "/api/favorites", body={"refs": [str(b["exhibit_id"])], "on": False})
    assert code == 200 and r["unmarked"] == 1
    code, _ = call(m, m.favorites_api, "POST", "/api/favorites", body={"refs": ["1"]}, origin="http://evil.example")
    assert code == 403
