"""Archive locks (fundus.locks): a locked slide set or slide is frozen – no new version, no move, no new slide, no folder,
name or deck-order change; copies are free and unlocked; unlocking only for Falk (portal, interactive terminal)."""
import json

import pytest

from fundus import decks
from fundus import exhibits as ex
from fundus import locks
from fundus import mcp_server as m
from fundus import reviews as rv
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _mcp_back():
    yield
    m.configure(m.DEFAULT_DB)


def deck_set(store, root):
    a = reg(store, write(root, "Contoso/exhibits/cost", page("Kosten"), png=png_bytes(rgb=(1, 2, 3))))
    ex.create_set(store, "contoso", "Präsentation Board", folders=["praes"], slug="praesentation-board")
    b = reg(store, write(root, "Contoso/praes/intro", page("Intro"), png=png_bytes(rgb=(4, 5, 6))))
    c = reg(store, write(root, "Contoso/praes/plan", page("Plan"), png=png_bytes(rgb=(7, 8, 9))))
    decks.set_sequence(store, "contoso", "board-deck", [str(b["exhibit_id"]), str(c["exhibit_id"])], set_ref="praesentation-board")
    return a, b, c


def test_locked_set_freezes_slides_folders_and_deck_order(store, projects_root):
    a, b, c = deck_set(store, projects_root)
    res = locks.lock(store, set_ref="contoso/praesentation-board", note="gehalten am 3.11.")
    assert res["locked"] and res["slides"] == 2
    assert locks.lock(store, set_ref="contoso/praesentation-board")["already"]
    # a re-render with changed content is refused, nothing written
    r = reg(store, write(projects_root, "Contoso/praes/intro", page("Intro geändert"), png=png_bytes(rgb=(40, 5, 6))))
    assert r["status"] == "locked" and "archived" in r["note"]
    assert store.db.execute("SELECT count(*) FROM exhibit_version WHERE exhibit_id = ?", (b["exhibit_id"],)).fetchone()[0] == 1
    # the unchanged file is still known (duplicate), a new slide in the set is refused
    write(projects_root, "Contoso/praes/intro", page("Intro"), png=png_bytes(rgb=(4, 5, 6)))
    assert reg(store, {"html": projects_root / "Contoso/praes/intro.html", "png": projects_root / "Contoso/praes/intro.png"})["status"] == "duplicate"
    assert reg(store, write(projects_root, "Contoso/praes/new", page("Neu"), png=png_bytes(rgb=(9, 9, 9))))["status"] == "locked"
    # folders, name and deck order are frozen
    with pytest.raises(locks.Locked):
        ex.add_set_folder(store, "contoso/praesentation-board", "praes2")
    with pytest.raises(locks.Locked):
        ex.remove_set_folder(store, "contoso/praesentation-board", "praes")
    with pytest.raises(locks.Locked):
        ex.rename_set(store, "contoso/praesentation-board", title="Anders")
    with pytest.raises(locks.Locked):
        decks.set_sequence(store, "contoso", "board-deck", [str(c["exhibit_id"]), str(b["exhibit_id"])])
    with pytest.raises(locks.Locked):
        decks.delete_sequence(store, "contoso/board-deck")
    with pytest.raises(locks.Locked):
        rv.link_items(store, b["exhibit_id"], ["document:1"])
    # reviews and feedback still work on archived slides
    rv.add(store, b["exhibit_id"], "Quelle ergänzen", source="ai")
    # the set and its slides show the lock
    g = ex.gallery(store, "contoso")
    assert next(s for s in g["sets"] if s["slug"] == "praesentation-board")["locked"]["note"] == "gehalten am 3.11."
    assert {x["id"]: bool(x.get("locked")) for x in g["exhibits"]} == {a["exhibit_id"]: False, b["exhibit_id"]: True, c["exhibit_id"]: True}
    assert ex.get(store, b["exhibit_id"])["locked"]["target"] == "set"


def test_locked_slide_stays_in_its_set_and_takes_no_version(store, projects_root):
    a = reg(store, write(projects_root, "Contoso/board/cost", page("Kosten"), png=png_bytes(rgb=(1, 2, 3))))
    locks.lock(store, exhibit=a["exhibit_id"])
    assert reg(store, write(projects_root, "Contoso/board/cost", page("Kosten neu"), png=png_bytes(rgb=(2, 2, 2))))["status"] == "locked"
    # a new set claiming its folder does not take the locked slide along
    s = ex.create_set(store, "contoso", "Board", folders=["board"])
    assert s["moved"] == [] and any("locked" in c["reason"] for c in s["conflicts"])
    assert store.db.execute("SELECT set_id FROM exhibit WHERE id = ?", (a["exhibit_id"],)).fetchone()[0] == a["set_id"]
    # other slides of the (unlocked) set stay free
    assert reg(store, write(projects_root, "Contoso/exhibits/other", page("Andere"), png=png_bytes(rgb=(3, 3, 3))))["status"] == "new_exhibit"
    assert ex.fold_script_versions(store)["exhibits"] == 0


def test_copies_are_unlocked_and_share_the_stage(store, projects_root):
    a, b, c = deck_set(store, projects_root)
    locks.lock(store, set_ref="contoso/praesentation-board")
    rv.add(store, b["exhibit_id"], "Zahl prüfen", source="ai")
    # one slide into the (free) default set
    cp = locks.copy_exhibit(store, b["exhibit_id"], to_set="contoso/main")
    assert cp["status"] == "new_exhibit" and cp["set"] == "main" and cp["copy_of"]["exhibit"] == b["exhibit_id"]
    assert (projects_root / "Contoso/exhibits/intro.html").is_file()
    assert locks.exhibit_lock(store, cp["exhibit_id"]) is None
    st = rv.slide_status(store, ids=[cp["exhibit_id"], b["exhibit_id"]])
    assert st[cp["exhibit_id"]]["stage"] == st[b["exhibit_id"]]["stage"] == "open"
    # the copy takes new versions, the original stays
    assert reg(store, write(projects_root, "Contoso/exhibits/intro", page("Intro neu"), png=png_bytes(rgb=(50, 5, 6))))["status"] == "new_version"
    assert store.db.execute("SELECT current_version FROM exhibit WHERE id = ?", (b["exhibit_id"],)).fetchone()[0] == 1
    # into the locked set itself: refused
    with pytest.raises(locks.Locked):
        locks.copy_exhibit(store, b["exhibit_id"], slug="intro-2")
    # a slide locked on its own can be copied into its own set under a new name
    locks.lock(store, exhibit=a["exhibit_id"])
    same = locks.copy_exhibit(store, a["exhibit_id"], slug="cost-copy")
    assert same["status"] == "new_exhibit" and same["slug"] == "cost-copy" and same["set"] == "main"
    # the whole set with its deck order
    out = locks.copy_set(store, "contoso/praesentation-board", "Board Überarbeitung", "praes-v2")
    assert len(out["slides"]) == 2 and out["decks"][0]["to"] == "board-deck (board-uberarbeitung)"
    assert locks.set_lock(store, out["set"]["id"]) is None
    seq = decks.get_sequence(store, "contoso/board-deck (board-uberarbeitung)")
    assert [s["slug"] for s in seq["slides"]] == ["intro", "plan"]


def test_unlock_only_for_falk(store, projects_root):
    a, b, c = deck_set(store, projects_root)
    sid = b["set_id"]
    locks.lock(store, set_ref=str(sid))
    m.configure(store.path)
    # agents: the MCP tool refuses, the CLI refuses without a terminal
    out = json.loads(m._run(m.exhibit_lock, "unlock", set=str(sid)))
    assert "Falk" in out["error"]
    with pytest.raises(LookupError):
        locks.unlock(store, exhibit=b["exhibit_id"])           # frozen by the set: no own lock
    # Falk (portal / terminal) unlocks; the history keeps both
    res = locks.unlock(store, set_ref=str(sid), note="Korrektur nach Board", actor="falk:portal")
    assert res["locked"] is False and locks.set_lock(store, sid) is None
    h = locks.history(store, "contoso")
    assert h[0]["unlocked_by"] == "falk:portal" and h[0]["unlock_note"] == "Korrektur nach Board"
    assert reg(store, write(projects_root, "Contoso/praes/intro", page("Intro v2"), png=png_bytes(rgb=(60, 5, 6))))["status"] == "new_version"


def test_mcp_lock_and_copy_tools(store, projects_root):
    a, b, c = deck_set(store, projects_root)
    m.configure(store.path)
    out = json.loads(m._run(m.exhibit_lock, "lock", set="contoso/praesentation-board", note="Archiv"))
    assert out["locked"] and out["note"] == "Archiv"
    assert json.loads(m._run(m.exhibit_lock, "status", exhibit=str(b["exhibit_id"])))["locked"]["target"] == "set"
    err = json.loads(m._run(m.exhibit_add, str(projects_root / "Contoso/praes/plan.html"), str(write(
        projects_root, "Contoso/praes/plan", page("Plan neu"), png=png_bytes(rgb=(70, 8, 9)))["png"])))
    assert err["status"] == "locked"
    cp = json.loads(m._run(m.exhibit_copy, exhibit=str(c["exhibit_id"]), to_set="contoso/main"))
    assert cp["status"] == "new_exhibit"
    assert "title" in json.loads(m._run(m.exhibit_copy, set="contoso/praesentation-board"))["error"]


def test_slides_are_frozen_while_a_review_runs(store, projects_root):
    a, b, c = deck_set(store, projects_root)
    rv.add_reviewer(store, "Keller, Anna")
    r = rv.create(store, "contoso", slides=[str(b["exhibit_id"])], people=["Keller, Anna"], title="Board-Check")
    change = lambda rgb: reg(store, write(projects_root, "Contoso/praes/intro", page("Intro neu"), png=png_bytes(rgb=rgb)))
    # a draft (not yet sent to the person) does not freeze
    assert change((90, 1, 1))["status"] == "new_version"
    store.db.execute("UPDATE review SET status = 'open' WHERE id = ?", (r["id"],)); store.db.commit()   # published
    out = change((91, 1, 1))
    assert out["status"] == "locked" and "under review" in out["note"] and "Anna Keller" in out["note"]
    assert reg(store, write(projects_root, "Contoso/praes/plan", page("Plan neu"), png=png_bytes(rgb=(92, 1, 1))))["status"] == "new_version"
    g = ex.gallery(store, "contoso")
    assert {x["id"]: bool(x.get("in_review")) for x in g["exhibits"]}[b["exhibit_id"]] is True
    # withdrawn assignment → free again; a new invitation freezes again; expiry frees
    rv.revoke(store, r["id"], "Keller, Anna")
    assert change((93, 1, 1))["status"] == "new_version"
    rv.invite(store, r["id"], ["Keller, Anna"])
    assert change((94, 1, 1))["status"] == "locked"
    store.db.execute("UPDATE review SET expires_at = '2000-01-01 00:00:00' WHERE id = ?", (r["id"],)); store.db.commit()
    assert change((95, 1, 1))["status"] == "new_version"
    # Gemini: frozen while queued or running, free when done
    gr = rv.create(store, "contoso", slides=[str(c["exhibit_id"])], gemini=True, title="Gemini")
    chg = lambda rgb: reg(store, write(projects_root, "Contoso/praes/plan", page("Plan v3"), png=png_bytes(rgb=rgb)))
    assert chg((96, 1, 1))["status"] == "locked"
    store.db.execute("UPDATE review SET gemini_state = 'done' WHERE id = ?", (gr["id"],)); store.db.commit()
    assert chg((97, 1, 1))["status"] == "new_version"
    # a finished person frees the slide too; copying is always possible
    r2 = rv.create(store, "contoso", slides=[str(b["exhibit_id"])], people=["Keller, Anna"], title="Zweiter Blick")
    store.db.execute("UPDATE review SET status = 'open' WHERE id = ?", (r2["id"],)); store.db.commit()
    assert change((98, 1, 1))["status"] == "locked"
    assert locks.copy_exhibit(store, b["exhibit_id"], to_set="contoso/main")["status"] == "new_exhibit"
    store.db.execute("UPDATE review_access SET finished_at = '2026-10-27 08:00:00' WHERE review_id = ?", (r2["id"],)); store.db.commit()
    assert change((99, 1, 1))["status"] == "new_version"
