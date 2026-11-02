"""Own display names for projects and slide sets: the slug and the set id stay the identity; CLI and POST /api/names."""
import asyncio
import json

import pytest

from fundus import exhibits as ex
from fundus import projects
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)


def post(m, body, origin="http://127.0.0.1:8010", ctype=b"application/json"):
    from starlette.requests import Request
    raw = json.dumps(body).encode()
    hdr = [(b"host", b"127.0.0.1:8010"), (b"content-type", ctype)] + ([(b"origin", origin.encode())] if origin else [])
    scope = {"type": "http", "method": "POST", "path": "/api/names", "path_params": {}, "query_string": b"", "headers": hdr}
    sent = []

    async def receive():
        if sent:
            return {"type": "http.disconnect"}
        sent.append(1)
        return {"type": "http.request", "body": raw, "more_body": False}
    return asyncio.run(m.names(Request(scope, receive)))


def test_project_title_set_change_remove(store, projects_root):
    reg(store, write(projects_root, "Northwind/exhibits/gov", page("Board governance")))
    assert projects.list_projects(store) == [{"project": "northwind", "title": None}]
    r = projects.set_title(store, "northwind", "  Northwind   KI-Strategie ")
    assert r == {"project": "northwind", "title": "Northwind KI-Strategie", "changed": True}
    assert projects.set_title(store, "Northwind", "Northwind KI-Strategie")["changed"] is False
    assert ex.gallery(store)["projects"][0]["title"] == "Northwind KI-Strategie"
    assert ex.gallery(store, "northwind")["title"] == "Northwind KI-Strategie"
    # the identity does not move
    assert store.db.execute("SELECT DISTINCT project FROM exhibit").fetchall()[0][0] == "northwind"
    assert projects.set_title(store, "northwind", "")["title"] is None
    assert projects.titles(store) == {}
    log = [json.loads(d) for (d,) in store.db.execute("SELECT detail FROM changelog WHERE item_kind = 'project'")]
    assert log[0]["title"] == {"old": None, "new": "Northwind KI-Strategie"} and log[-1]["title"]["new"] is None
    with pytest.raises(LookupError):
        projects.set_title(store, "nope", "Nope")
    with pytest.raises(ValueError):
        projects.set_title(store, "northwind", "x" * 200)


def test_project_known_from_tags(store):
    store.db.execute("INSERT INTO tag(item_kind, item_id, facet, value) VALUES ('fact', '1', 'project', 'fhnw-marketing')")
    assert projects.set_title(store, "fhnw-marketing", "FHNW Marketingstudien")["title"] == "FHNW Marketingstudien"


def test_post_names_project_and_set(store, projects_root):
    from fundus import mcp_server as m
    m.configure(store.path)
    try:
        reg(store, write(projects_root, "Northwind/exhibits/gov", page("Board governance")))
        board = ex.create_set(store, "northwind", "Board", folders=["board"])
        r = post(m, {"project": "northwind", "title": "Northwind"})
        assert r.status_code == 200 and json.loads(r.body)["title"] == "Northwind"
        r = post(m, {"set": board["id"], "title": "Board-Workshop März"})
        d = json.loads(r.body)
        assert r.status_code == 200 and d["title"] == "Board-Workshop März" and d["slug"] == "board" and d["changed"]
        assert json.loads(post(m, {"set": board["id"], "title": "Board-Workshop März"}).body)["changed"] is False
        assert store.db.execute("SELECT slug, title FROM exhibit_set WHERE id = ?", (board["id"],)).fetchone()[:] == \
            ("board", "Board-Workshop März")
        assert post(m, {"set": board["id"], "title": "  "}).status_code == 400
        assert post(m, {"set": "board", "title": "X"}).status_code == 400
        assert post(m, {"set": 99999, "title": "X"}).status_code == 404
        assert post(m, {"project": "nope", "title": "X"}).status_code == 404
        assert post(m, {"project": "northwind", "title": 5}).status_code == 400
        assert post(m, {"title": "X"}).status_code == 400
        assert post(m, {"project": "northwind", "title": "X"}, origin="http://evil.test").status_code == 403
        assert post(m, {"project": "northwind", "title": "X"}, ctype=b"text/plain").status_code == 415
        assert post(m, {"project": "northwind", "title": ""}).status_code == 200 and projects.titles(store) == {}
    finally:
        m.configure(m.DEFAULT_DB)


def test_cli_project(store, projects_root, capsys):
    from fundus import cli
    reg(store, write(projects_root, "Contoso/exhibits/market", page("Coffee market"), png=png_bytes(rgb=(2, 7, 7))))
    db = ["--db", str(store.path), "project"]
    assert cli.main(db + ["title", "contoso", "Contoso Group"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Contoso Group"
    assert cli.main(db + ["list"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"project": "contoso", "title": "Contoso Group"}]
    assert cli.main(db + ["title", "unknown", "X"]) != 0


def test_project_sort_setting_and_created(store, projects_root):
    from fundus import mcp_server as m
    reg(store, write(projects_root, "Contoso/exhibits/market", page("Coffee market"), png=png_bytes(rgb=(2, 7, 7))))
    assert projects.get_sort(store) == "name_asc" and ex.gallery(store)["project_sort"] == "name_asc"
    assert ex.gallery(store)["projects"][0]["created_at"]
    ex.create_set(store, "fabrikam", "Kick-off", folders=["kickoff"])          # no slides: date of its first set
    assert projects.created(store)["fabrikam"]
    with pytest.raises(ValueError):
        projects.set_sort(store, "random")
    m.configure(store.path)
    try:
        from starlette.requests import Request
        raw = json.dumps({"project_sort": "created_desc"}).encode()
        scope = {"type": "http", "method": "POST", "path": "/api/portal/settings", "path_params": {}, "query_string": b"",
                 "headers": [(b"host", b"h:1"), (b"content-type", b"application/json"), (b"origin", b"http://h:1")]}
        sent = []

        async def receive():
            if sent:
                return {"type": "http.disconnect"}
            sent.append(1)
            return {"type": "http.request", "body": raw, "more_body": False}
        r = asyncio.run(m.portal_settings(Request(scope, receive)))
        assert r.status_code == 200 and json.loads(r.body) == {"project_sort": "created_desc"}
        assert ex.gallery(store)["project_sort"] == "created_desc"
    finally:
        m.configure(m.DEFAULT_DB)
