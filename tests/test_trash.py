"""Trash and move (fundus.trash): slides, sets and projects go to the trash (out of gallery, search, decks, saved
searches), come back exactly, are purged after 30 days or by hand (files no other slide uses go too); renders of trashed
or purged slides are skipped; moved slides keep getting their new versions at the new place; locks and reviews protect."""
import json
import os

import pytest

from fundus import decks
from fundus import exhibits as ex
from fundus import locks
from fundus import mcp_server as m
from fundus import query
from fundus import slide_search as ss
from fundus import trash
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _mcp_back():
    yield
    m.configure(m.DEFAULT_DB)


def setup(store, root):
    a = reg(store, write(root, "Contoso/exhibits/cost", page("Kosten im Service"), png=png_bytes(rgb=(1, 2, 3))))
    ex.create_set(store, "contoso", "Board", folders=["praes"], slug="board")
    b = reg(store, write(root, "Contoso/praes/intro", page("Intro zum Board"), png=png_bytes(rgb=(4, 5, 6))))
    c = reg(store, write(root, "Contoso/praes/plan", page("Plan für Grinder"), png=png_bytes(rgb=(7, 8, 9))))
    d = reg(store, write(root, "Acme/exhibits/dealer", page("Händler mit KI"), png=png_bytes(rgb=(9, 9, 1))))
    decks.set_sequence(store, "contoso", "board-deck", [str(b["exhibit_id"]), str(c["exhibit_id"])], set_ref="board")
    ss.save(store, "Auswahl", [str(a["exhibit_id"]), str(b["exhibit_id"])])
    return a, b, c, d


def projects_listed(store):
    return [p["project"] for p in ex.gallery(store)["projects"]]


def test_slide_to_trash_and_back(store, projects_root):
    a, b, c, d = setup(store, projects_root)
    before = ex.get(store, b["exhibit_id"], include_internal=True, script=False)
    r = trash.trash(store, "slide", [b["exhibit_id"]], actor="test")
    tid = r["trashed"][0]["trash_id"]
    assert {x["slug"] for x in ex.gallery(store, "contoso")["exhibits"]} == {"cost", "plan"}
    seq = decks.get_sequence(store, "board-deck", project="contoso")
    assert [s["id"] for s in seq["slides"]] == [c["exhibit_id"]]                         # out of the deck
    saved = store.db.execute("SELECT exhibit_id FROM slide_collection_item").fetchall()
    assert [r[0] for r in saved] == [a["exhibit_id"]]                                    # out of the saved search
    assert not any(h.get("id") == b["exhibit_id"] for h in query.search(store, "Intro Board", kind="exhibit")["results"])
    got = ex.get(store, b["exhibit_id"], include_internal=True, script=False)
    assert got["trashed"]["trash_id"] == tid and got["trashed"]["from"] == "contoso/board/intro"
    lst = trash.listing(store)
    assert lst["entries"] == 1 and lst["items"][0]["days_left"] == 30 and lst["items"][0]["label"] == "Intro zum Board"
    # the same file rendered again (render.sh, nightly sync): skipped while in the trash
    again = reg(store, write(projects_root, "Contoso/praes/intro", page("Intro zum Board"), png=png_bytes(rgb=(4, 5, 6))))
    assert again["status"] == "trashed"
    changed = reg(store, write(projects_root, "Contoso/praes/intro", page("Intro zum Board, neu"), png=png_bytes(rgb=(4, 5, 7))))
    assert changed["status"] == "trashed"                                             # even changed: restore it first
    out = trash.restore(store, tid, actor="test")
    assert out["to"] == "contoso/intro"
    back = ex.get(store, b["exhibit_id"], include_internal=True, script=False)
    assert (back["project"], back["slug"], back.get("set")) == (before["project"], before["slug"], before.get("set"))
    assert [s["id"] for s in decks.get_sequence(store, "board-deck", project="contoso")["slides"]] == [b["exhibit_id"], c["exhibit_id"]]
    assert any(h.get("id") == b["exhibit_id"] for h in query.search(store, "Intro Board", kind="exhibit")["results"])
    assert len(store.db.execute("SELECT * FROM slide_collection_item").fetchall()) == 2
    with pytest.raises(ValueError):
        trash.restore(store, tid)


def test_set_and_project_to_trash_purge_and_tombstone(store, projects_root):
    a, b, c, d = setup(store, projects_root)
    r = trash.trash_set(store, "contoso/board", actor="test")
    assert r["slides"] == 2 and [s["slug"] for s in ex.gallery(store)["projects"][0]["sets"]] != []
    assert "board" not in {s["slug"] for p in ex.gallery(store)["projects"] for s in p["sets"]}
    assert reg(store, write(projects_root, "Contoso/praes/new", page("Neu im Board"), png=png_bytes(rgb=(3, 3, 3))))["status"] == "trashed"
    trash.restore(store, r["trash_id"])
    assert {s["slug"] for s in ex.gallery(store, "contoso")["sets"]} >= {"board"}
    assert decks.get_sequence(store, "board-deck", project="contoso")["slides"]
    p = trash.trash_project(store, "contoso", actor="test")
    assert p["slides"] == 3 and "contoso" not in projects_listed(store) and "_trash" not in projects_listed(store)
    png = store.db.execute("SELECT png_sha FROM exhibit_version WHERE exhibit_id = ?", (a["exhibit_id"],)).fetchone()[0]
    assert ex.file_path(store, png, "png").exists()
    res = trash.purge(store, everything=True, actor="test")
    assert res["purged"] == 1 and res["slides"] == 3 and res["files_deleted"] > 0
    assert not ex.file_path(store, png, "png").exists()
    assert store.db.execute("SELECT count(*) FROM exhibit WHERE id = ?", (a["exhibit_id"],)).fetchone()[0] == 0
    assert trash.listing(store)["entries"] == 0
    # an unchanged file in the project folder does not come back; a changed render is a new slide
    assert reg(store, write(projects_root, "Contoso/exhibits/cost", page("Kosten im Service"), png=png_bytes(rgb=(1, 2, 3))))["status"] == "purged"
    assert reg(store, write(projects_root, "Contoso/exhibits/cost", page("Kosten im Service 2027"), png=png_bytes(rgb=(1, 2, 4))))["status"] == "new_exhibit"
    assert "acme" in projects_listed(store)


def test_expired_entries_are_purged(store, projects_root):
    a, b, c, d = setup(store, projects_root)
    r = trash.trash(store, "slide", [d["exhibit_id"]])
    tid = r["trashed"][0]["trash_id"]
    assert trash.purge(store, expired=True)["purged"] == 0
    store.db.execute("UPDATE trash SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (tid,))
    store.db.commit()
    assert trash.purge(store, expired=True)["purged"] == 1
    assert trash.listing(store, "purged")["items"][0]["id"] == tid


def test_move_slide_set_and_project(store, projects_root):
    a, b, c, d = setup(store, projects_root)
    r = trash.move(store, "slide", [a["exhibit_id"]], "acme/main")
    assert r["moved"][0]["to"] == "acme/main/cost"
    # the next render of the file in the Contoso folder adds a version to the moved slide
    v2 = reg(store, write(projects_root, "Contoso/exhibits/cost", page("Kosten im Service, neu"), png=png_bytes(rgb=(1, 2, 9))))
    assert (v2["exhibit_id"], v2["project"], v2["version"]) == (a["exhibit_id"], "acme", 2)
    m2 = trash.move(store, "set", ["contoso/board"], "acme")
    assert m2["moved"][0]["to"] == "acme/board"
    assert decks.get_sequence(store, "board-deck", project="acme")["slides"]          # its deck moved along
    v3 = reg(store, write(projects_root, "Contoso/praes/plan", page("Plan für Grinder, neu"), png=png_bytes(rgb=(7, 8, 1))))
    assert (v3["exhibit_id"], v3["project"]) == (c["exhibit_id"], "acme")
    # merge the rest of contoso (its main set) into acme: the main sets join, new renders land in acme
    e = reg(store, write(projects_root, "Contoso/exhibits/team", page("Team"), png=png_bytes(rgb=(2, 2, 2))))
    mp = trash.move(store, "project", ["contoso"], "acme")
    assert mp["moved"][0]["into"] == "acme" and "contoso" not in projects_listed(store)
    assert ex.get(store, e["exhibit_id"], include_internal=True, script=False)["project"] == "acme"
    f = reg(store, write(projects_root, "Contoso/exhibits/later", page("Später"), png=png_bytes(rgb=(5, 5, 5))))
    assert f["project"] == "acme"


def test_locks_and_reviews_protect(store, projects_root):
    a, b, c, d = setup(store, projects_root)
    locks.lock(store, set_ref="contoso/board", note="final", actor="test")
    with pytest.raises(locks.Locked):
        trash.trash(store, "slide", [b["exhibit_id"]])
    with pytest.raises(locks.Locked):
        trash.trash_set(store, "contoso/board")
    with pytest.raises(locks.Locked):
        trash.move(store, "slide", [b["exhibit_id"]], "acme/main")
    with pytest.raises(locks.Locked):
        trash.trash_project(store, "contoso")
    assert trash.listing(store)["entries"] == 0                                         # nothing half done


def test_mcp_tools_and_portal_api(store, projects_root):
    from test_reviews import call
    a, b, c, d = setup(store, projects_root)
    m.configure(store.path)
    out = json.loads(m.fundus_exhibit_trash(action="trash", target="slide", refs=[str(d["exhibit_id"])]))
    tid = out["trashed"][0]["trash_id"]
    assert json.loads(m.fundus_exhibit_trash())["entries"] == 1
    assert "error" in json.loads(m.fundus_exhibit_trash(action="purge"))                # emptying is Falk's
    assert json.loads(m.fundus_exhibit_trash(action="restore", trash_id=tid))["restored"] == tid
    mv = json.loads(m.fundus_exhibit_move(target="slide", refs=[str(a["exhibit_id"])], to="contoso/board"))
    assert mv["moved"][0]["to"] == "contoso/board/cost"
    code, r = call(m, m.trash_api, "POST", "/api/trash", body={"action": "trash", "target": "set", "refs": ["contoso/board"]})
    assert code == 200
    code, lst = call(m, m.trash_api, path="/api/trash")
    assert code == 200 and lst["entries"] == 1 and lst["items"][0]["kind"] == "set"
    code, e = call(m, m.trash_api, "POST", "/api/trash", body={"action": "empty"})
    assert code == 200 and e["purged"] == 1
    code, _ = call(m, m.move_api, "POST", "/api/exhibits/move", body={"target": "project", "refs": ["contoso"], "to": "acme"})
    assert code == 200
    code, _ = call(m, m.trash_api, "POST", "/api/trash", body={"action": "empty"}, origin="http://evil.example")
    assert code == 403
