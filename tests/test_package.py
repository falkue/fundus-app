"""Slide packages (fundus.package): export → receive → accept between two Fundus installations, global-id matching in
both directions, path smoothing, the 25 MB rule, the sender's stage note ("geprüft bei …"), internal report, skill install."""
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from fundus import Store, decks, locks
from fundus import exhibits as ex
from fundus import package as pk
from fundus import reviews as rv
from test_exhibits import page, png_bytes, write


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    root.mkdir()
    monkeypatch.setattr(ex, "PROJECTS_ROOT", root)
    monkeypatch.setattr(ex, "PUBLIC_URL", "http://fundus.test")
    skill = tmp_path / "skills-a" / "mck-exhibit"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "mck.py").write_text("VERSION = 1\n")
    _git(skill, "init", "-q")
    _git(skill, "add", "-A")
    _git(skill, "commit", "-q", "-m", "v1")
    monkeypatch.setattr(ex, "SKILL_DIR", skill)
    monkeypatch.setenv("FUNDUS_SENDER_NAME", "Falk")
    a = Store(tmp_path / "a" / "fundus.sqlite")
    b = Store(tmp_path / "b" / "fundus.sqlite")
    yield {"root": root, "a": a, "b": b, "tmp": tmp_path, "skill": skill}
    a.close()
    b.close()


def _git(d, *args):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=T", "-c", "user.email=t@example.org", *args], check=True,
                   capture_output=True)


def slide(store, root, rel, title, rgb, script_text=None, refs=None, **kw):
    html = page(title, **kw)
    if refs:
        html = html.replace("<head>", f'<head><meta name="fundus-refs" content="{refs}">')
    files = write(root, rel, html, png=png_bytes(rgb=rgb))
    script = None
    if script_text is not None:
        script = root / Path(rel).parts[0] / "skripte" / f"{Path(rel).name}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(script_text)
    return ex.register(store, files["html"], files["png"], script=script, find=False)


def sender_setup(env):
    a, root = env["a"], env["root"]
    proj = root / "Lehre"
    (proj / "data").mkdir(parents=True)
    (proj / "data" / "input.csv").write_text("x,y\n1,2\n")
    ext = env["tmp"] / "elsewhere" / "logo.svg"
    ext.parent.mkdir(parents=True)
    ext.write_text("<svg/>")
    with a.batch("test"):
        doc = a.document("CMO Survey 2026", url="https://cmosurvey.org/2026", publisher="Duke University", published="2026")
        obs = a.add("observation", {"metric": "Marketing budget share for AI", "value": 12.5, "unit": "%", "period": "2026",
                                    "document_id": doc, "status": "verified"})
    (proj / "skripte").mkdir(parents=True)
    (proj / "skripte" / "common.py").write_text(f'BASE = "{proj}/data"\nSKILL = "{env["skill"]}/render.sh"\n')
    script = (f'import sys\nimport common\nsys.path.insert(0, "{env["skill"]}/scripts")\nROOT = "{proj}"\n'
              f'DATA = "{proj}/data/input.csv"\nLOGO = "{ext}"\nMORE = "../data/input.csv"\n'
              f'MISSING = "/srv/somewhere/else.csv"\nREFS = ["observation:{obs}"]\nOUT = ROOT + "/board/cost.html"\n')
    ex.create_set(a, "lehre", "Board März", folders=["board"], slug="board")
    r1 = slide(a, root, "Lehre/board/cost", "Kosten steigen", (1, 2, 3), script_text=script, refs=f"observation:{obs}")
    r2 = slide(a, root, "Lehre/board/plan", "Plan 2027", (4, 5, 6))
    decks.set_sequence(a, "lehre", "board-deck", [str(r2["exhibit_id"]), str(r1["exhibit_id"])], set_ref="board", title="Board-Deck")
    return r1, r2, obs


def mark_checked(store, eid):
    """An independent clean check of the current version (Gemini) → stage 'checked'."""
    rid = rv.ai_reviewer(store, "Gemini")["id"]
    v = store.db.execute("SELECT current_version FROM exhibit WHERE id = ?", (eid,)).fetchone()[0]
    store.db.execute("INSERT INTO exhibit_check(exhibit_id, version, reviewer_id, mode, result) VALUES (?,?,?,?,?)",
                     (eid, v, rid, "full", "clean"))
    store.db.commit()


def roundtrip(env, refs=("set:lehre/board",), project="Neues Projekt", **kw):
    out = pk.export(env["a"], list(refs), env["tmp"] / "out", actor="test")
    rec = pk.receive(env["b"], out["path"])
    rep = pk.accept(env["b"], rec["received_id"], project, actor="test", **kw)
    return out, rec, rep


# ------------------------------------------------------------------ round trip
def test_export_receive_accept_round_trip(env):
    a, b = env["a"], env["b"]
    r1, r2, obs = sender_setup(env)
    mark_checked(a, r1["exhibit_id"])
    out = pk.export(a, ["set:lehre/board"], env["tmp"] / "out", actor="test")
    man = out["manifest"]
    assert Path(out["path"]).is_file() and out["path"].endswith(".fundus.zip")
    assert len(man["slides"]) == 2 and man["sets"][0]["title"] == "Board März"
    assert man["decks"][0]["name"] == "board-deck" and len(man["decks"][0]["slides"]) == 2
    assert man["skill"] and man["skill"]["version"] and "skill/mck-exhibit.tar" in zipfile.ZipFile(out["path"]).namelist()
    s1 = next(s for s in man["slides"] if s["slug"] == "cost")
    gid = a.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (r1["exhibit_id"],)).fetchone()[0]
    assert s1["global_id"] == gid and len(gid) == 36
    assert s1["observations"][0]["value"] == 12.5 and s1["documents"][0]["title"] == "CMO Survey 2026"
    assert s1["review_notes"][0]["stage"] == "checked"
    roles = {Path(f["path"]).name: (f["role"], f["included"]) for f in s1["script_files"]}
    assert roles["input.csv"] == ("project", True) and roles["logo.svg"] == ("extern", True)
    assert roles["scripts"][0] == "skill"

    rec = pk.receive(b, out["path"])
    rid = rec["received_id"]
    assert rec["slides"] == 2 and rec["cases"] == {"new": 2}
    orig = pk.received_dir(b) / str(rid)
    assert (orig / "manifest.json").is_file() and (orig / "package.zip").is_file()
    det = pk.detail(b, rid)
    assert det["status"] == "new" and det["slides"][0]["current"]["png"].startswith(f"/api/received/{rid}/file/")
    p, ctype = pk.received_file(b, rid, det["slides"][0]["current"]["png"].rsplit("/", 1)[1])
    assert p.is_file() and ctype == "image/png"

    rep = pk.accept(b, rid, "Neues Projekt", actor="test")
    assert rep["status"] == "accepted" and rep["project"] == "neues-projekt" and rep["taken"] == 2
    row = b.db.execute("SELECT * FROM exhibit WHERE global_id = ?", (gid,)).fetchone()
    assert row["project"] == "neues-projekt"
    assert b.db.execute("SELECT title FROM project WHERE slug = 'neues-projekt'").fetchone()[0] == "Neues Projekt"
    st = b.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (row["set_id"],)).fetchone()
    assert st["title"] == "Board März – von Falk"
    # the project has no folder at the recipient: files under <data dir>/received-files/<project>/<set>/
    base = env["tmp"] / "b" / "received-files" / "neues-projekt" / st["slug"]
    script = base / "skripte" / "cost.py"
    text = script.read_text()
    assert str(env["root"] / "Lehre") not in text and f'ROOT = "{base}"' in text
    assert f'DATA = "{base}/data/input.csv"' in text and (base / "data" / "input.csv").read_text() == "x,y\n1,2\n"
    assert f'LOGO = "{base}/_extern/logo.svg"' in text and (base / "_extern" / "logo.svg").is_file()
    assert f'"{ex.SKILL_DIR}/scripts"' in text
    helper = (base / "skripte" / "common.py").read_text()      # sibling helper module: travelled and smoothed too
    assert helper == f'BASE = "{base}/data"\nSKILL = "{ex.SKILL_DIR}/render.sh"\n'
    new_obs = b.db.execute("SELECT * FROM observation").fetchone()
    assert new_obs["value"] == 12.5 and new_obs["status"] == "unverified" and f'REFS = ["observation:{new_obs["id"]}"]' in text
    assert any(u["path"] == "/srv/somewhere/else.csv" for u in rep["unresolved"])
    links = {(r[0], r[1]) for r in b.db.execute("SELECT target_kind, target_id FROM exhibit_link WHERE exhibit_id = ? "
                                                  "AND target_kind IN ('observation','document')", (row["id"],))}
    assert ("observation", str(new_obs["id"])) in links and ("observation", str(obs)) not in links or obs == new_obs["id"]
    dk = b.db.execute("SELECT * FROM exhibit_sequence WHERE project = 'neues-projekt'").fetchone()
    assert dk["name"] == "board-deck" and dk["title"] == "Board-Deck"
    order = [r[0] for r in b.db.execute("SELECT e.slug FROM exhibit_sequence_item i JOIN exhibit e ON e.id = i.exhibit_id "
                                        "WHERE i.sequence_id = ? ORDER BY i.position", (dk["id"],))]
    assert order == ["plan", "cost"]
    # the received original stays unchanged and accepting it again does nothing
    assert pk.plan(b, rid)["slides"][0]["case"] == "same"
    det = pk.detail(b, rid)
    assert det["status"] == "accepted" and all(s["own"] and not s["own"]["changed"] for s in det["slides"])
    assert pk.received_list(b)["items"][0]["slides"] == 2


def test_internal_slide_stays_internal_and_data_keeps_visibility(env):
    a, b, root = env["a"], env["b"], env["root"]
    with a.batch("test"):
        obs = a.add("observation", {"metric": "Interne Kennzahl", "value": 3, "unit": "%", "visibility": "internal"})
    r = slide(a, root, "Lehre/exhibits/intern", "Intern", (9, 9, 9), refs=f"observation:{obs}")
    ex.set_sharing(a, r["exhibit_id"], "project")
    out, rec, rep = roundtrip(env, refs=[str(r["exhibit_id"])], project="ziel")
    row = b.db.execute("SELECT * FROM exhibit WHERE project = 'ziel'").fetchone()
    assert row["sharing"] == "project" and row["visibility"] == "internal"
    assert b.db.execute("SELECT visibility, status FROM observation").fetchone()[:] == ("internal", "unverified")


# ------------------------------------------------------------------ matching
def test_matching_cases_in_both_directions(env, monkeypatch):
    a, b, root = env["a"], env["b"], env["root"]
    r1, r2, _ = sender_setup(env)
    out, rec, rep = roundtrip(env)
    gid = a.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (r1["exhibit_id"],)).fetchone()[0]
    mine = b.db.execute("SELECT * FROM exhibit WHERE global_id = ?", (gid,)).fetchone()
    # the recipient edits the slide (new render in its received folder) and sends it back
    v1 = b.db.execute("SELECT png_path FROM exhibit_version WHERE exhibit_id = ?", (mine["id"],)).fetchone()[0]
    p = Path(v1)
    p.with_suffix(".html").write_text(page("Kosten steigen stärker"))
    p.write_bytes(png_bytes(rgb=(10, 20, 30)))
    reg = ex.register(b, p.with_suffix(".html"), p, project="neues-projekt", set_ref=str(mine["set_id"]), find=False)
    assert reg["status"] == "new_version" and reg["exhibit_id"] == mine["id"]
    back = pk.export(b, [str(mine["id"])], env["tmp"] / "back", actor="test")
    assert back["manifest"]["slides"][0]["global_id"] == gid
    got = pk.receive(a, back["path"], sender="Anna")
    pl = pk.plan(a, got["received_id"])["slides"][0]
    assert pl["case"] == "update" and pl["local"]["exhibit_id"] == r1["exhibit_id"] and pl["new_versions"] == 1
    res = pk.accept(a, got["received_id"], "lehre", actor="test")
    assert res["slides"][0]["action"] == "new_version"
    row = a.db.execute("SELECT * FROM exhibit WHERE id = ?", (r1["exhibit_id"],)).fetchone()
    assert row["current_version"] == 2 and row["title"] == "Kosten steigen stärker"
    assert a.db.execute("SELECT count(*) FROM exhibit WHERE global_id = ?", (gid,)).fetchone()[0] == 1
    # the same package again: nothing to do
    again = pk.receive(a, back["path"], sender="Anna")
    assert pk.plan(a, again["received_id"])["slides"][0]["case"] == "same"
    # both change: the sender edits too, the recipient sends another edit → new version, variant or skip; never overwrite
    files = write(root, "Lehre/board/cost", page("Kosten – Fassung Falk"), png=png_bytes(rgb=(11, 22, 33)))
    ex.register(a, files["html"], files["png"], find=False)
    p.with_suffix(".html").write_text(page("Kosten – Fassung Anna"))
    p.write_bytes(png_bytes(rgb=(12, 24, 36)))
    ex.register(b, p.with_suffix(".html"), p, project="neues-projekt", set_ref=str(mine["set_id"]), find=False)
    third = pk.export(b, [str(mine["id"])], env["tmp"] / "third", actor="test")
    got3 = pk.receive(a, third["path"], sender="Anna")
    pl = pk.plan(a, got3["received_id"])["slides"][0]
    assert pl["case"] == "conflict" and pl["options"] == ["new_version", "variant", "skip"] and pl["default"] == "variant"
    with pytest.raises(ValueError):
        pk.accept(a, got3["received_id"], "lehre", decisions={gid: "accept"})
    res = pk.accept(a, got3["received_id"], "lehre", actor="test")
    var = res["slides"][0]
    assert var["action"] == "variant" and var["exhibit_id"] != r1["exhibit_id"]
    assert a.db.execute("SELECT title FROM exhibit WHERE id = ?", (r1["exhibit_id"],)).fetchone()[0] == "Kosten – Fassung Falk"
    assert a.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (var["exhibit_id"],)).fetchone()[0] is None
    # locked at the recipient: only a copy
    locks.lock(a, exhibit=str(r1["exhibit_id"]))
    got4 = pk.receive(a, third["path"], sender="Anna")
    pl = pk.plan(a, got4["received_id"])["slides"][0]
    assert pl["case"] == "locked" and pl["options"] == ["variant", "skip"]


def test_unknown_slide_is_new_and_partial_accept(env):
    a, b = env["a"], env["b"]
    r1, r2, _ = sender_setup(env)
    out = pk.export(a, ["deck:lehre/board-deck"], env["tmp"] / "out")
    rec = pk.receive(b, out["path"])
    gid2 = a.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (r2["exhibit_id"],)).fetchone()[0]
    rep = pk.accept(b, rec["received_id"], "teil", slides=[gid2])
    assert rep["status"] == "partly" and rep["taken"] == 1
    assert [x["action"] for x in rep["slides"]].count("not_selected") == 1
    rep = pk.accept(b, rec["received_id"], "teil")
    assert rep["status"] == "accepted"
    # a re-sent package: slides already here count as taken even when not selected, a later check at the sender arrives
    mark_checked(a, r2["exhibit_id"])
    again = pk.receive(b, pk.export(a, ["deck:lehre/board-deck"], env["tmp"] / "again")["path"])
    rep = pk.accept(b, again["received_id"], "teil", slides=["nothing"])
    assert rep["status"] == "accepted" and {x["case"] for x in rep["slides"]} == {"same"}
    mine = b.db.execute("SELECT id FROM exhibit WHERE global_id = ?", (gid2,)).fetchone()[0]
    assert rv.slide_status(b, ids=[mine])[mine]["label"] == "geprüft bei Falk"
    assert b.db.execute("SELECT count(*) FROM exhibit WHERE project = 'teil'").fetchone()[0] == 2
    assert pk.reject is not None
    with pytest.raises(ValueError):
        pk.reject(b, rec["received_id"])


def test_accept_into_project_with_folder_places_files_there(env):
    a, b, root = env["a"], env["b"], env["root"]
    sender_setup(env)
    own = write(root, "Zielprojekt/exhibits/eigen", page("Eigen"), png=png_bytes(rgb=(50, 50, 50)))
    ex.register(b, own["html"], own["png"], find=False)
    out, rec, rep = roundtrip(env, project="zielprojekt")
    row = b.db.execute("SELECT e.*, s.slug set_slug FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id WHERE e.slug = 'cost' "
                       "AND e.project = 'zielprojekt'").fetchone()
    png = b.db.execute("SELECT png_path FROM exhibit_version WHERE exhibit_id = ?", (row["id"],)).fetchone()[0]
    assert png.startswith(str(root / "Zielprojekt" / "received" / row["set_slug"]))
    folders = [r[0] for r in b.db.execute("SELECT folder FROM exhibit_set_folder WHERE set_id = ?", (row["set_id"],))]
    assert folders == [f"received/{row['set_slug']}"]


# ------------------------------------------------------------------ path smoothing
def test_rewrite_paths_boundaries_and_one_pass():
    text = 'A = "/home/x/Projects/Lehre"\nB = "/home/x/Projects/Lehre/data/a.csv"\nC = "/home/x/Projects/LehreOld/b"\nD = \'~/.claude/skills/mck-exhibit/scripts\'\n'
    out = pk.rewrite_paths(text, [("/home/x/Projects/Lehre", "/home/x/Projects/Lehre/received/s"),
                                  ("~/.claude/skills/mck-exhibit", "/opt/skills/mck-exhibit")])
    assert 'A = "/home/x/Projects/Lehre/received/s"' in out
    assert 'B = "/home/x/Projects/Lehre/received/s/data/a.csv"' in out
    assert 'C = "/home/x/Projects/LehreOld/b"' in out           # only at a path boundary
    assert "'/opt/skills/mck-exhibit/scripts'" in out
    assert pk.rewrite_ids('fundus=["observation:12", "document:4", "observation:99"]',
                          {"observation:12": "observation:7", "document:4": "document:1"}) == \
        'fundus=["observation:7", "document:1", "observation:99"]'


def test_script_paths_and_files_classified(env):
    a, root = env["a"], env["root"]
    proj = root / "Lehre"
    (proj / "skripte").mkdir(parents=True)
    (proj / "skripte" / "helpers.py").write_text("X = 1\n")
    (proj / "skripte" / "local.json").write_text("{}")
    text = 'import helpers\nP = "local.json"\nQ = "/data/big.parquet"\nlabel = "Kosten/Nutzen"\nurl = "https://x.org/a.csv"\n'
    fs = pk._script_files(a, text, str(proj / "skripte" / "s.py"), proj, set())
    by = {Path(f["path"]).name: f for f in fs}
    assert by["helpers.py"]["included"] and by["helpers.py"]["rel"] == "skripte/helpers.py"
    assert by["local.json"]["included"]
    assert by["big.parquet"]["role"] == "data" and not by["big.parquet"]["included"]
    assert "Nutzen" not in by and "a.csv" not in by


# ------------------------------------------------------------------ 25 MB
def test_files_above_25_mb_become_references(env, monkeypatch):
    a, b = env["a"], env["b"]
    sender_setup(env)
    real = pk._size
    monkeypatch.setattr(pk, "_size", lambda p: 26 * 1024 * 1024 if Path(p).name == "input.csv" else real(p))
    out = pk.export(a, ["set:lehre/board"], env["tmp"] / "out")
    s1 = next(s for s in out["manifest"]["slides"] if s["slug"] == "cost")
    f = next(f for f in s1["script_files"] if Path(f["path"]).name == "input.csv")
    assert not f["included"] and "25 MB" in f["reason"]
    assert any(r.get("file", "").endswith("input.csv") and "25 MB" in r["reason"] for r in out["manifest"]["references"])
    names = zipfile.ZipFile(out["path"]).namelist()
    assert not any(n.startswith("assets/") and n.endswith(f.get("sha") or "-") for n in names)
    monkeypatch.setattr(pk, "MAX_FILE_BYTES", 10)          # now even the slide PNG is too big: a reference, accept skips it
    out2 = pk.export(a, ["set:lehre/board"], env["tmp"] / "out2")
    assert not any(n.endswith(".png") for n in zipfile.ZipFile(out2["path"]).namelist())
    assert any("larger than" in r["reason"] for r in out2["manifest"]["references"])


# ------------------------------------------------------------------ the sender's stage note
def test_foreign_review_note_counts_until_the_content_changes(env):
    a, b = env["a"], env["b"]
    r1, r2, _ = sender_setup(env)
    mark_checked(a, r1["exhibit_id"])
    assert rv.slide_status(a, ids=[r1["exhibit_id"]])[r1["exhibit_id"]]["stage"] == "checked"
    out, rec, rep = roundtrip(env)
    gid = a.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (r1["exhibit_id"],)).fetchone()[0]
    mine = b.db.execute("SELECT * FROM exhibit WHERE global_id = ?", (gid,)).fetchone()
    st = rv.slide_status(b, ids=[mine["id"]])[mine["id"]]
    assert st["stage"] == "checked" and st["level"] == 2 and st["label"] == "geprüft bei Falk"
    assert st["foreign"]["origin"] == "Falk"
    # the unchecked second slide stays unchecked
    other = b.db.execute("SELECT id FROM exhibit WHERE project = 'neues-projekt' AND slug = 'plan'").fetchone()[0]
    assert rv.slide_status(b, ids=[other])[other]["stage"] == "unchecked"
    # the stage note travels on with its origin when the recipient sends the slide on
    fwd = pk.export(b, [str(mine["id"])], env["tmp"] / "fwd")
    note = fwd["manifest"]["slides"][0]["review_notes"][0]
    assert note["origin"] == "Falk" and note["stage"] == "checked"
    # the first change lapses it
    p = Path(b.db.execute("SELECT png_path FROM exhibit_version WHERE exhibit_id = ?", (mine["id"],)).fetchone()[0])
    p.with_suffix(".html").write_text(page("Kosten geändert"))
    p.write_bytes(png_bytes(rgb=(77, 77, 77)))
    ex.register(b, p.with_suffix(".html"), p, project="neues-projekt", set_ref=str(mine["set_id"]), find=False)
    st = rv.slide_status(b, ids=[mine["id"]])[mine["id"]]
    assert st["stage"] == "changed" and "Falk" in st["label"] and "foreign" not in st and st["foreign_lapsed"]["origin"] == "Falk"


# ------------------------------------------------------------------ internal report
def test_internal_report(env):
    a, root = env["a"], env["root"]
    r1, r2, _ = sender_setup(env)
    c = slide(a, root, "Northwind/exhibits/gov", "Governance", (20, 30, 40))
    ex.set_sharing(a, r2["exhibit_id"], "project")
    rep = pk.internal_report(a, ["set:lehre/board", str(c["exhibit_id"])])
    by = {x["ref"]: x for x in rep["internal"]}
    assert rep["slides"] == 3 and rep["count"] == 2
    assert "client_project" in by["northwind/gov"]["reasons"]
    assert set(by["lehre/plan"]["reasons"]) >= {"sharing_project", "visibility_internal"}
    assert by["lehre/plan"]["labels"][0]
    assert "lehre/cost" not in by


# ------------------------------------------------------------------ skill
def test_install_skill_refuses_local_changes_and_newer_versions(env, monkeypatch):
    a, b, tmp = env["a"], env["b"], env["tmp"]
    sender_setup(env)
    out = pk.export(a, ["set:lehre/board"], tmp / "out")
    rec = pk.receive(b, out["path"])
    rid = rec["received_id"]
    # recipient has a git checkout of the skill with uncommitted changes
    target = tmp / "skills-b" / "mck-exhibit"
    (target / "scripts").mkdir(parents=True)
    (target / "scripts" / "mck.py").write_text("VERSION = 0\n")
    _git(target, "init", "-q")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "old")
    (target / "scripts" / "mck.py").write_text("VERSION = 'local edit'\n")
    monkeypatch.setattr(ex, "SKILL_DIR", target)
    with pytest.raises(PermissionError, match="uncommitted"):
        pk.install_skill(b, rid)
    assert "local edit" in (target / "scripts" / "mck.py").read_text()
    res = pk.install_skill(b, rid, force=True)
    assert res["installed"] and Path(res["backup"]).is_dir()
    assert (target / "scripts" / "mck.py").read_text() == "VERSION = 1\n"
    meta = json.loads((target / ".fundus-skill.json").read_text())
    assert meta["version"] == out["manifest"]["skill"]["version"]
    assert pk.install_skill(b, rid)["reason"] == "already installed"
    # an installed snapshot that is newer than the package: refused without force
    meta["version"], meta["date"] = "f" * 40, "2999-01-01T00:00:00+00:00"
    (target / ".fundus-skill.json").write_text(json.dumps(meta))
    with pytest.raises(PermissionError, match="newer"):
        pk.install_skill(b, rid)
    # nothing installs on receive or accept by itself
    other = tmp / "skills-c" / "mck-exhibit"
    monkeypatch.setattr(ex, "SKILL_DIR", other)
    pk.accept(b, rid, "ziel")
    assert not other.exists()


def test_receive_refuses_foreign_entries_and_damaged_files(env, tmp_path):
    a, b = env["a"], env["b"]
    sender_setup(env)
    out = pk.export(a, ["set:lehre/board"], tmp_path / "out", include_skill=False)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out["path"]) as zin, zipfile.ZipFile(bad, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, zin.read(n))
        zout.writestr("../evil.sh", "rm -rf /")
    with pytest.raises(ValueError, match="unexpected"):
        pk.receive(b, bad)
    dam = tmp_path / "damaged.zip"
    with zipfile.ZipFile(out["path"]) as zin, zipfile.ZipFile(dam, "w") as zout:
        for n in zin.namelist():
            zout.writestr(n, b"x" if n.endswith(".html") else zin.read(n))
    with pytest.raises(ValueError, match="damaged"):
        pk.receive(b, dam)
    assert b.db.execute("SELECT count(*) FROM received_package").fetchone()[0] == 0


def test_record_sent_and_list(env):
    a = env["a"]
    sender_setup(env)
    out = pk.export(a, ["set:lehre/board"], env["tmp"] / "out", include_skill=False)
    sid = pk.record_sent(a, out["manifest"], ["Anna Keller"], transport="file")
    lst = pk.sent_list(a)
    assert lst["items"][0]["id"] == sid and lst["items"][0]["recipients"] == ["Anna Keller"] and lst["items"][0]["slides"] == 2


# ------------------------------------------------------------------ REST (portal)
def test_portal_api(env):
    import asyncio
    from starlette.requests import Request
    from fundus import mcp_server as m
    from test_reviews import call
    a, b = env["a"], env["b"]
    sender_setup(env)
    out = pk.export(a, ["set:lehre/board"], env["tmp"] / "out", include_skill=False)
    pk.record_sent(a, out["manifest"], ["Anna"])
    rid = pk.receive(b, out["path"])["received_id"]
    m.configure(b.path)
    try:
        code, lst = call(m, m.received_api, path="/api/received")
        assert code == 200 and lst["new"] == 1 and lst["items"][0]["slides"] == 2
        code, det = call(m, m.received_api, path=f"/api/received/{rid}", path_params={"rid": rid})
        assert code == 200 and len(det["slides"]) == 2 and det["slides"][0]["plan"]["case"] == "new"
        name = det["slides"][0]["current"]["html"].rsplit("/", 1)[1]
        scope = {"type": "http", "method": "GET", "path": "/x", "path_params": {"rid": rid, "name": name}, "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8010")]}
        r = asyncio.run(m.received_api(Request(scope)))
        assert r.status_code == 200 and r.headers["content-security-policy"] == "sandbox allow-scripts"
        code, _ = call(m, m.received_api, "POST", f"/api/received/{rid}", body={"action": "accept", "project": "x"},
                       path_params={"rid": rid}, origin="http://evil.example")
        assert code == 403
        code, rep = call(m, m.received_api, "POST", f"/api/received/{rid}", body={"action": "accept", "project": "Von Falk"},
                         path_params={"rid": rid})
        assert code == 200 and rep["status"] == "accepted" and rep["project"] == "von-falk"
        code, _ = call(m, m.received_api, "POST", f"/api/received/{rid}", body={"action": "reject"}, path_params={"rid": rid})
        assert code == 400
        code, _ = call(m, m.received_api, "POST", f"/api/received/{rid}", body={"action": "install_skill"}, path_params={"rid": rid})
        assert code == 404                       # no skill in this package
        m.configure(a.path)
        code, sent = call(m, m.sent_api, path="/api/sent")
        assert code == 200 and sent["items"][0]["recipients"] == ["Anna"]
    finally:
        m.configure(m.DEFAULT_DB)
