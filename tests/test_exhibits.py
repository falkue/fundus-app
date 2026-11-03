"""Exhibits (slides/charts): registration, identity, versions, decks, text extraction, search, sharing policy, entity graph,
file serving and the render.sh hook."""
import asyncio
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from fundus import embed
from fundus import exhibits as ex
from fundus import query as q

NAVY_BLUE, NAVY_HSG = "#051C2C", "#0A5F2D"


def png_bytes(w=640, h=360, rgb=(255, 255, 255)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def page(title, eyebrow="Northwind · Governance · Exhibit G1", so_what="Boards need one AI owner",
         sources=("Kaiser Permanente annual report 2025", "BAG Spitalstatistik 2024"), color=NAVY_BLUE, chart=True,
         note="Values rounded", extra=""):
    srcs = "".join(f'<span class="sn">{i + 1}</span>{s}' + ('<span class=sd>·</span>' if i < len(sources) - 1 else "")
                   for i, s in enumerate(sources))
    svg = (f'<svg data-mck-chart="hbar" data-dw="900" data-dh="400" viewBox="0 0 900 400"><text x="1">Zurich</text>'
           f'<text x="2">Geneva</text></svg>') if chart else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><style>.x{{color:{color}}}</style></head>
<body style="position:relative;"><div class="page"><div class="eyebrow">{eyebrow}</div><h1>{title}</h1>
<div class="meta"><b>Survey of hospital boards</b><span class="dot">·</span>n = 40</div>
<div class="body"><div class="chead"><b>Governance structures</b> by hospital group</div>{svg}
<div class="lab">Share of hospitals</div><p>Most hospital groups govern AI through the IT department. {extra}</p>
<div class="conclusion"><div class="k">So what</div><div class="h">{so_what}</div><div class="x">Start with a mandate.</div></div></div>
<div class="src"><span class="srck">Sources</span>{srcs}<div class="srcm">Further sources: OECD Health at a Glance 2025 · Note: {note}</div></div>
<div class="copy">© 2026 Jane Doe</div></div><script>var a=1;</script></body></html>"""


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "Projects"
    root.mkdir()
    monkeypatch.setattr(ex, "PROJECTS_ROOT", root)
    monkeypatch.setattr(ex, "PUBLIC_URL", "http://fundus.test")
    return root


def write(root: Path, rel: str, html: str | None = None, png: bytes | None = None, mtime: float | None = None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {}
    if html is not None:
        p.with_suffix(".html").write_text(html)
        out["html"] = p.with_suffix(".html")
    p.with_suffix(".png").write_bytes(png if png is not None else png_bytes())
    out["png"] = p.with_suffix(".png")
    if mtime:
        for f in out.values():
            os.utime(f, (mtime, mtime))
    return out


def reg(store, files, **kw):
    return ex.register(store, files.get("html"), files["png"], **kw)


# ------------------------------------------------------------------ identity
def test_slug_rules():
    assert ex.exhibit_slug("04-ai-c1-funnel-search-to-agent") == "ai-c1-funnel-search-to-agent"
    assert ex.exhibit_slug("B03-kpi-4-turnover") == "kpi-4-turnover"
    assert ex.exhibit_slug("00_title") == "title"
    assert ex.exhibit_slug("11-divider") == "11-divider"            # generic deck element keeps its number
    assert ex.exhibit_slug("01-00-section") == "01-00-section"      # what remains starts with a digit
    assert ex.exhibit_slug("demo-hsg-section-hsg") == "demo-hsg-section"
    assert ex.exhibit_slug("models-2-cost-v1") == "models-2-cost"
    assert ex.exhibit_slug("ex01_adoption") == "ex01-adoption"
    assert ex.exhibit_slug("s-1-stack-layers") == "s-1-stack-layers"
    assert ex.exhibit_slug("b2b-1a-ai-manufacturing") == "b2b-1a-ai-manufacturing"
    assert ex.order_prefix("B03-x.png") == (3, "B03")
    assert ex.order_prefix("x.png") == (None, None)


def test_project_detection(projects, tmp_path):
    (projects / "Fabrikam" / "exhibits").mkdir(parents=True)
    os.symlink(projects / "Fabrikam", projects / "Fabrikan")
    assert ex.detect_project(projects / "Fabrikan" / "exhibits" / "a.png")[0] == "fabrikam"
    assert ex.detect_project(projects / "Fabrikam" / "exhibits" / "a.png")[0] == "fabrikam"
    assert ex.detect_project(projects / "Acme-Dealerships" / "exhibits-hsg" / "a.png")[0] == "acme-dealerships"
    assert ex.detect_project(projects / "New Client" / "x" / "a.png")[0] == "new-client"
    assert ex.detect_project(tmp_path / "elsewhere" / "a.png")[0] == "elsewhere"


# ------------------------------------------------------------------ extraction
def test_text_extraction():
    e = ex.extract(page("Only 3 of 40 hospital groups have an AI board, while 25 delegate it to IT", color=NAVY_HSG))
    assert e["title"].startswith("Only 3 of 40") and e["eyebrow"].startswith("Northwind")
    assert "Survey of hospital boards" in e["meta"] and "n = 40" in e["meta"]
    assert e["chart_heads"] == ["Governance structures by hospital group"]
    assert {"Zurich", "Geneva", "Share of hospitals"} <= set(e["labels"])
    assert e["so_what"].startswith("Boards need one AI owner") and "So what" not in e["so_what"]
    assert e["sources"] == ["Kaiser Permanente annual report 2025", "BAG Spitalstatistik 2024"]
    assert e["sources_more"] == ["OECD Health at a Glance 2025"] and e["notes"] == ["Values rounded"]
    assert e["chart_types"] == ["hbar"] and e["palette"] == "hsg" and e["language"] == "en"
    assert "var a" not in e["body"] and "Jane Doe" not in e["body"]
    de = ex.extract(page("Die meisten Spitäler steuern KI über die IT und nicht über die Geschäftsleitung", chart=False,
                         extra="Das ist für die Spitäler nicht neu und wird sich mit der Regulierung auch ändern."))
    assert de["language"] == "de" and de["chart_types"] == [] and de["palette"] == "blue"


def test_kind_classification():
    assert ex.classify("x", 3840, 2160, {"chart_types": ["hbar"], "is_html": True, "has_h1": True}) == "chart"
    assert ex.classify("x", 3840, 2160, {"chart_types": [], "is_html": True, "has_h1": True}) == "slide"
    assert ex.classify("sec-2", 3840, 2160, None) == "section"
    assert ex.classify("x", 3840, 2160, {"chart_types": [], "is_html": True, "has_h1": False}) == "section"
    assert ex.classify("journey", 4800, 1500, None) == "poster"


# ------------------------------------------------------------------ registration, versions, dedupe, decks
def test_register_versions_dedupe_variants(store, projects):
    (projects / "Northwind" / "skripte").mkdir(parents=True)
    (projects / "Northwind" / "skripte" / "governance.py").write_text("write(html, OUT / 'governance-1a.html')\n")
    f1 = write(projects, "Northwind/exhibits/governance-1a", page("Only 3 of 40 groups have an AI board"), mtime=1_700_000_000)
    r1 = reg(store, f1)
    assert r1["status"] == "new_exhibit" and r1["version"] == 1 and r1["project"] == "northwind"
    assert r1["slug"] == "governance-1a" and r1["kind"] == "chart" and r1["script"].endswith("governance.py")
    n_files = len(list(ex.files_dir(store).rglob("*.*")))
    assert reg(store, f1)["status"] == "duplicate"                       # identical content: no new version, no new file
    assert len(list(ex.files_dir(store).rglob("*.*"))) == n_files
    f1["html"].write_text(page("Only 4 of 40 groups have an AI board"))   # HTML changed -> new version
    f1["png"].write_bytes(png_bytes(rgb=(250, 250, 250)))
    r2 = reg(store, f1)
    assert r2["status"] == "new_version" and r2["version"] == 2
    fh = write(projects, "Northwind/exhibits-hsg/governance-1a", page("Only 4 of 40 groups have an AI board", color=NAVY_HSG),
               png=png_bytes(rgb=(240, 250, 240)))
    r3 = reg(store, fh)                                                    # palette variant: same logical slide
    assert r3["exhibit_id"] == r1["exhibit_id"] and r3["version"] == 3 and r3["palette"] == "hsg"
    (projects / "Northwind" / "skripte" / "governance.py").write_text("write(html, OUT / 'governance-1a.html')  # v2\n")
    r4 = reg(store, fh)                                  # only the script changed: same version, which now points to it
    assert r4["status"] == "duplicate" and r4["version"] == 3
    g = ex.get(store, "northwind/governance-1a")
    assert g["title"] == "Only 4 of 40 groups have an AI board" and g["current_version"] == 3
    assert g["variants"] == {"blue": 2, "hsg": 3} and len(g["versions"]) == 3
    assert g["urls"]["preview"].startswith("http://fundus.test/exhibits/") and g["urls"]["preview"].endswith(".preview.png")
    assert "# v2" in g["script"]["text"]
    stored = ex.file_path(store, store.db.execute("SELECT html_sha FROM exhibit_version WHERE n = 1").fetchone()[0], "html")
    assert stored.is_file() and stored.parent.name == stored.name[:2]
    assert store.db.execute("SELECT count(*) FROM search_index WHERE item_kind = 'exhibit'").fetchone()[0] == 1


def test_deck_copies_are_memberships(store, projects):
    f = write(projects, "Contoso/research/exhibits/ex01_adoption", page("One in six manufacturers used AI"))
    r = reg(store, f)
    deck_png = projects / "Contoso" / "research" / "exhibits" / "deck" / "04-ex01_adoption.png"
    deck_png.parent.mkdir(parents=True)
    shutil.copy(f["png"], deck_png)
    d = ex.register(store, None, deck_png)
    assert d["status"] == "deck_copy" and d["exhibit_id"] == r["exhibit_id"] and d["version"] == 1
    assert d["deck"] == "research/exhibits/deck"
    row = store.db.execute("SELECT * FROM exhibit_deck").fetchone()
    assert (row["position"], row["slot"], row["file"]) == (4, "04", "04-ex01_adoption.png")
    e = store.db.execute("SELECT * FROM exhibit").fetchone()
    assert e["status"] == "final" and e["deck"] == "research/exhibits/deck"
    assert store.db.execute("SELECT count(*) FROM exhibit_version").fetchone()[0] == 1
    # a PNG-only deck slide that matches nothing is its own exhibit; a later one keeps the text of the HTML version
    other = projects / "Contoso" / "research" / "exhibits" / "deck" / "05-section_1.png"
    other.write_bytes(png_bytes(rgb=(1, 2, 3)))
    o = ex.register(store, None, other)
    assert o["status"] == "new_exhibit" and o["kind"] == "section" and o["slug"] == "section-1"
    late = projects / "Contoso" / "research" / "exhibits" / "ex01_adoption-v2.png"
    late.write_bytes(png_bytes(rgb=(9, 9, 9)))
    lv = ex.register(store, None, late)
    assert lv["exhibit_id"] == r["exhibit_id"] and lv["version"] == 2
    assert store.db.execute("SELECT title FROM exhibit WHERE id = ?", (r["exhibit_id"],)).fetchone()[0].startswith("One in six")


def test_import_dir_orders_by_mtime_and_skips(store, projects):
    base = projects / "Acme-Dealerships"
    write(base.parent, "Acme-Dealerships/versions/v1-blue/exhibits/kpi-1-gap", page("Old title"), png=png_bytes(rgb=(1, 1, 1)),
          mtime=1_700_000_000)
    new = write(base.parent, "Acme-Dealerships/exhibits/kpi-1-gap", page("New title"), png=png_bytes(rgb=(2, 2, 2)),
                mtime=1_700_100_000)
    deck = base / "deck" / "main" / "07-kpi-1-gap.png"
    deck.parent.mkdir(parents=True)
    shutil.copy(new["png"], deck)
    os.utime(deck, (1_700_000_500, 1_700_000_500))                       # copy dated between the two renders
    write(base.parent, "Acme-Dealerships/exhibits/logos/acme", png=png_bytes(w=800, h=400))
    (base / "exhibits" / "poster-source.html").write_text(page("No PNG"))
    write(base.parent, "Acme-Dealerships/deck/overview-main", png=png_bytes(w=800, h=450))
    res = ex.import_dir(store, base)
    assert res["counts"]["new_exhibit"] == 1 and res["counts"]["new_version"] == 1 and res["counts"]["deck_copy"] == 1
    assert res["projects"]["acme-dealerships"] == {"logical": 1, "versions": 2, "deck_memberships": 1, "by_kind": {"chart": 1}}
    reasons = {s["reason"] for s in res["skipped"]}
    assert {"HTML without PNG", "contact sheet/preview/test"} <= reasons
    g = ex.get(store, "kpi-1-gap")
    assert g["title"] == "New title" and [v["n"] for v in g["versions"]] == [1, 2]
    assert g["versions"][0]["title"] == "Old title" and g["decks"][0]["version"] == 2
    assert ex.import_dir(store, base)["counts"]["new_version"] == 0      # idempotent


# ------------------------------------------------------------------ search
def _evidence(store):
    with store.batch("t"):
        doc = store.document("Kaiser Permanente annual report 2025", url="https://kp.example/ar25", publisher="Kaiser Permanente",
                             published="2025")
        store.add("observation", {"metric": "Hospital groups with an AI board", "value": 3, "unit": "count", "period": "2025",
                                  "document_id": doc}, tags={"industry": ["healthcare"], "topic": ["ai-adoption"]})
    return doc


def test_search_with_kind_filter(store, projects):
    _evidence(store)
    reg(store, write(projects, "Northwind/exhibits/governance-1a", page("Only 3 of 40 hospital groups have an AI board")))
    reg(store, write(projects, "Northwind/exhibits/close-2", page("Next steps for the board", chart=False),
                     png=png_bytes(rgb=(5, 5, 5))))
    allr = q.search(store, "AI board", mode="lexical")["results"]
    assert {r["kind"] for r in allr} == {"exhibit", "observation"}
    exr = q.search(store, "AI board", kind="exhibit", mode="lexical")["results"]
    assert {r["kind"] for r in exr} == {"exhibit"}
    hit = next(r for r in exr if r["slug"] == "governance-1a")
    assert hit["exhibit_type"] == "chart" and hit["project"] == "northwind" and hit["version"] == 1
    assert hit["preview_url"].endswith(".preview.png") and hit["png_url"].endswith(".png")
    charts = q.search(store, "board", kind="chart", mode="lexical")["results"]
    assert [r["slug"] for r in charts] == ["governance-1a"]
    assert [r["slug"] for r in q.search(store, "board", kind="slide", mode="lexical")["results"]] == ["close-2"]
    assert q.search(store, "board", kind="exhibit", industry="healthcare", mode="lexical")["results"] == []
    g = q.get(store, "exhibit", hit["id"])
    assert g["slug"] == "governance-1a"
    assert any(l["kind"] == "document" and l["role"] == "source" for l in g["links"])   # source line -> Fundus document


# ------------------------------------------------------------------ sharing policy
def _two_projects(store, projects):
    a = reg(store, write(projects, "Northwind/exhibits/gov", page("Hospital board governance in Switzerland",
                                                                   sources=("Northwind internal interviews 2026",))))
    b = reg(store, write(projects, "Contoso/exhibits/gov-peers", page("Hospital board governance among peers",
                                                                    sources=("OECD Health at a Glance 2025",)),
                         png=png_bytes(rgb=(7, 7, 7))))
    return a["exhibit_id"], b["exhibit_id"]


def _ids(res):
    return {r["id"] for r in res["results"] if r["kind"] == "exhibit"}


def test_policy_share_everything_and_internal_default(store, projects):
    a, b = _two_projects(store, projects)
    assert ex.policy(store) == "share_everything"
    assert _ids(q.search(store, "governance", mode="lexical")) == {a, b}
    res = ex.set_policy(store, "internal_default")
    assert res["internal"] == 2 and res["changed"] == 2
    assert _ids(q.search(store, "governance", mode="lexical")) == set()
    assert _ids(q.search(store, "governance", mode="lexical", visible_project="northwind")) == {a}   # no leak of contoso's
    assert _ids(q.search(store, "governance", mode="lexical", project="contoso")) == {b}
    assert _ids(q.search(store, "governance", mode="lexical", include_internal=True)) == {a, b}
    assert _ids(q.search(store, "", kind="exhibit")) == set()
    with pytest.raises(PermissionError):
        ex.get(store, a)
    with pytest.raises(PermissionError):
        ex.get(store, a, project="contoso")
    assert ex.get(store, a, project="northwind")["urls"]["png"].endswith("?project=northwind")
    assert ex.listing(store) == [] and len(ex.listing(store, project="northwind")) == 1
    s = ex.share(store, a)
    assert s["visibility"] == "public"
    assert _ids(q.search(store, "governance", mode="lexical")) == {a}
    assert ex.set_policy(store, "share_everything")["public"] == 2


def test_policy_auto_sources(store, projects):
    a, b = _two_projects(store, projects)
    ex.set_policy(store, "auto_sources")
    vis = dict(store.db.execute("SELECT id, visibility FROM exhibit").fetchall())
    assert vis == {a: "internal", b: "public"}           # 'internal interviews' source vs public OECD source
    # a linked internal Fundus item makes an exhibit internal too
    with store.batch("t"):
        oid = store.add("observation", {"metric": "Board survey", "value": 1, "period": "2026", "visibility": "internal"},
                        origin=("contoso", "f", "1"))
        store.db.execute("INSERT INTO exhibit_link(exhibit_id, target_kind, target_id, role) VALUES (?, 'observation', ?, 'data')",
                         (b, str(oid)))
        ex.recompute(store, [b])
    assert tuple(store.db.execute("SELECT visibility, internal_links FROM exhibit WHERE id = ?", (b,)).fetchone()) == ("internal", 1)
    assert _ids(q.search(store, "governance", mode="lexical")) == set()
    ex.share(store, b)
    assert _ids(q.search(store, "governance", mode="lexical")) == {b}


def test_data_refs_and_new_exhibits_follow_policy(store, projects):
    assert ex.data_refs("Data: Fundus obs 4871, 15391 and obs 344–346; series bfs-genai-use-ch") == [
        ("observation", "4871"), ("observation", "15391"), ("observation", "344"), ("observation", "345"),
        ("observation", "346"), ("series", "bfs-genai-use-ch")]
    assert ex.data_refs("obs 12 without the archive name") == []
    ex.set_policy(store, "internal_default")
    r = reg(store, write(projects, "Contoso/exhibits/new", page("A new slide")))
    assert store.db.execute("SELECT visibility FROM exhibit WHERE id = ?", (r["exhibit_id"],)).fetchone()[0] == "internal"


# ------------------------------------------------------------------ entity graph
class WordEmbedder:
    model, dim = "fake-words", 128

    def _v(self, t):
        v = np.zeros(self.dim, dtype=np.float32)
        for w in __import__("re").findall(r"\w+", t.lower()):
            v[hash(w) % self.dim] += 1
        return v

    def embed_documents(self, texts):
        return embed.normalise([self._v(t) for t in texts], self.dim)

    def embed_query(self, text, timeout=4.0):
        return embed.normalise(self._v(text), self.dim)[0]


def test_entity_graph(store, projects):
    from fundus import entities as ent
    with store.batch("t"):
        kp = ent.create_entity(store, "organisation", "Kaiser Permanente", "manual")
    r = reg(store, write(projects, "Northwind/exhibits/cases-kp", page("Kaiser Permanente runs ambient AI scribes in 40 hospitals",
                                                                         sources=("OECD Health at a Glance 2025",))))
    links = {(e, role) for e, role in store.db.execute("SELECT entity_id, role FROM entity_link WHERE item_kind = 'exhibit'")}
    client = ent.find(store, "Northwind")
    assert (kp, "subject") in links and client and (client[0], "subject") in links    # client created and linked
    prof = q.entity_profile(store, "Kaiser Permanente")
    assert prof["counts"]["by_kind"]["exhibit"] == 1 and prof["exhibits"]["items"][0]["id"] == r["exhibit_id"]
    assert prof["exhibits"]["items"][0]["role"] == "subject"
    res = q.search(store, "scribes", mode="lexical", entity="Kaiser Permanente")
    assert _ids(res) == {r["exhibit_id"]}
    embed.set_embedder(WordEmbedder())
    embed.embed_pending(store)
    assert store.db.execute("SELECT count(*) FROM embedding WHERE item_kind = 'exhibit'").fetchone()[0] == 1
    hy = q.search(store, "Kaiser Permanente ambient AI", mode="hybrid")
    assert hy["mode"] == "hybrid" and "Kaiser Permanente" in hy.get("entities_detected", [])
    assert hy["results"][0]["kind"] == "exhibit" and "Kaiser Permanente" in hy["results"][0]["entities"]
    # a full entity relink keeps the slides in the graph
    ent.link(store)
    assert store.db.execute("SELECT count(*) FROM entity_link WHERE item_kind = 'exhibit' AND entity_id = ?", (kp,)).fetchone()[0] == 1
    # visibility applies: internal exhibits drop out of the profile and the entity search
    ex.set_policy(store, "internal_default")
    try:
        assert "exhibits" not in q.entity_profile(store, "Kaiser Permanente")
    except LookupError:   # nothing else is linked to the entity: the profile is hidden as a whole
        pass
    assert _ids(q.search(store, "scribes", mode="lexical", entity="Kaiser Permanente")) == set()
    assert _ids(q.search(store, "Kaiser Permanente ambient AI", mode="hybrid")) == set()
    assert q.entity_profile(store, "Kaiser Permanente", project="northwind")["exhibits"]["count"] == 1
    # new version with changed text -> re-embedded (text_hash)
    embed.embed_pending(store)
    h0 = store.db.execute("SELECT text_hash FROM embedding WHERE item_kind = 'exhibit'").fetchone()[0]
    f = projects / "Northwind" / "exhibits" / "cases-kp.html"
    f.write_text(page("Kaiser Permanente runs ambient AI scribes in 45 hospitals"))
    reg(store, {"html": f, "png": f.with_suffix(".png")})
    assert embed.embed_pending(store)["embedded"] == 1
    assert store.db.execute("SELECT text_hash FROM embedding WHERE item_kind = 'exhibit'").fetchone()[0] != h0


def test_entity_link_migration(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE entity_link (
      entity_id  INTEGER NOT NULL,
      item_kind  TEXT NOT NULL CHECK (item_kind IN ('observation','forecast','study','quote','practice','fact','series')),
      item_id    TEXT NOT NULL,
      role       TEXT NOT NULL CHECK (role IN ('publisher','organisation','speaker','subject','mentioned','via')),
      source     TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','llm','manual')),
      confidence REAL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (entity_id, item_kind, item_id, role)) WITHOUT ROWID;
      INSERT INTO entity_link(entity_id, item_kind, item_id, role) VALUES (1, 'fact', '7', 'subject');""")
    db.commit()
    db.close()
    from fundus import Store
    st = Store(path)
    assert "'exhibit'" in st.db.execute("SELECT sql FROM sqlite_master WHERE name = 'entity_link'").fetchone()[0]
    assert [tuple(r) for r in st.db.execute("SELECT entity_id, item_kind, item_id FROM entity_link")] == [(1, "fact", "7")]
    st.db.execute("INSERT INTO entity_link(entity_id, item_kind, item_id, role) VALUES (1, 'exhibit', '1', 'mentioned')")
    st.close()


# ------------------------------------------------------------------ file serving
def test_file_serving_validation(store, projects):
    r = reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
    v = store.db.execute("SELECT png_sha, html_sha FROM exhibit_version").fetchone()
    p, ctype, eid = ex.resolve_file(store, f"{v[0]}.png")
    assert p.is_file() and ctype == "image/png" and eid == r["exhibit_id"]
    assert ex.resolve_file(store, f"{v[1]}.html")[1].startswith("text/html")
    for bad in ("../fundus.sqlite", f"{v[0]}.png/../x", f"{v[0].upper()}.png", f"{v[0]}.exe", f"{v[0][:10]}.png",
                f"{v[0]}.png.png", "", f"/{v[0]}.png", f"{v[0]}.PNG"):
        with pytest.raises(ValueError):
            ex.resolve_file(store, bad)
    with pytest.raises(LookupError):
        ex.resolve_file(store, "0" * 64 + ".png")
    with pytest.raises(LookupError):                       # a known PNG hash is not a known script hash
        ex.resolve_file(store, f"{v[0]}.py")
    ex.set_policy(store, "internal_default")
    with pytest.raises(PermissionError):
        ex.resolve_file(store, f"{v[0]}.png")
    assert ex.resolve_file(store, f"{v[0]}.png", project="northwind")[0] == p


def test_http_route(store, projects, monkeypatch):
    from starlette.requests import Request
    from fundus import mcp_server as m
    m.configure(store.path)
    try:
        reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
        sha = store.db.execute("SELECT png_sha FROM exhibit_version").fetchone()[0]

        def get(name, qs=""):
            scope = {"type": "http", "method": "GET", "path": f"/exhibits/{name}", "path_params": {"name": name},
                     "query_string": qs.encode(), "headers": []}
            return asyncio.run(m.exhibit_file(Request(scope)))
        ok = get(f"{sha}.png")
        assert ok.status_code == 200 and ok.media_type == "image/png" and ok.headers["x-content-type-options"] == "nosniff"
        assert get("..%2Fdata.sqlite").status_code == 400
        assert get("f" * 64 + ".png").status_code == 404
        ex.set_policy(store, "internal_default")
        assert get(f"{sha}.png").status_code == 403
        assert get(f"{sha}.png", "project=northwind").status_code == 200
        assert store.db.execute("SELECT sum(n) FROM exhibit_view WHERE via = 'http'").fetchone()[0] == 2
        assert any(getattr(r, "path", "") == "/exhibits/{name}" for r in m.build_server()._custom_starlette_routes)
    finally:
        m.configure(m.DEFAULT_DB)


def test_gallery_api(store, projects):
    from starlette.requests import Request
    from fundus import mcp_server as m
    m.configure(store.path)
    try:
        reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
        reg(store, write(projects, "Northwind/deck/02-gov", page("Board governance")))
        reg(store, write(projects, "Contoso/exhibits/market", page("Coffee market")))
        ex.set_policy(store, "internal_default")
        sha = store.db.execute("SELECT png_sha FROM exhibit_version LIMIT 1").fetchone()[0]

        def call(fn, path, qs="", params=None, origin="http://fundus.example.ts.net:8087"):
            scope = {"type": "http", "method": "GET", "path": path, "path_params": params or {}, "query_string": qs.encode(),
                     "headers": [(b"origin", origin.encode())]}
            return asyncio.run(fn(Request(scope)))
        r = call(m.exhibit_api, "/api/exhibits")
        d = json.loads(r.body)
        assert r.status_code == 200 and "access-control-allow-origin" not in r.headers   # portal is same-origin
        assert {p["project"]: p["exhibits"] for p in d["projects"]} == {"northwind": 1, "contoso": 1}
        assert d["projects"][0]["decks"] in (0, 1) and d["policy"] == "internal_default"
        g = json.loads(call(m.exhibit_api, "/api/exhibits", "project=northwind").body)
        it = g["exhibits"][0]
        assert it["visibility"] == "internal" and it["preview"].endswith(".preview.png?project=northwind")
        assert g["decks"] and g["decks"][0]["slides"][0]["id"] == it["id"]
        one = json.loads(call(m.exhibit_api, f"/api/exhibits/{it['id']}", params={"ref": str(it["id"])}).body)
        assert one["id"] == it["id"] and "script" not in one
        assert call(m.exhibit_api, "/api/exhibits", "project=nope").status_code == 404
        # browsing in the gallery's lightbox does not count as a view
        before = store.db.execute("SELECT coalesce(sum(n), 0) FROM exhibit_view").fetchone()[0]
        scope = {"type": "http", "method": "GET", "path": f"/exhibits/{sha}.png", "path_params": {"name": f"{sha}.png"},
                 "query_string": b"project=northwind&src=gallery", "headers": []}
        assert asyncio.run(m.exhibit_file(Request(scope))).status_code == 200
        assert store.db.execute("SELECT coalesce(sum(n), 0) FROM exhibit_view").fetchone()[0] == before
        paths = {getattr(r, "path", "") for r in m.build_server()._custom_starlette_routes}
        assert {"/api/exhibits", "/api/exhibits/{ref:path}"} <= paths
    finally:
        m.configure(m.DEFAULT_DB)


def test_sharing_levels(store, projects):
    """Slide setting > project setting > global policy; project setting applies to all slides without an own one."""
    a = reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
    b = reg(store, write(projects, "Northwind/exhibits/costs", page("Cost structure")))
    c = reg(store, write(projects, "Contoso/exhibits/market", page("Coffee market")))
    vis = lambda: dict(store.db.execute("SELECT slug, visibility FROM exhibit").fetchall())
    assert set(vis().values()) == {"public"}                       # share_everything
    r = ex.set_project_sharing(store, "northwind", "project")
    assert r["changed"] == 2 and vis() == {"gov": "internal", "costs": "internal", "market": "public"}
    ex.set_sharing(store, "northwind/gov", "shared")              # exempt one slide
    assert vis()["gov"] == "public" and vis()["costs"] == "internal"
    ex.set_policy(store, "share_everything")                       # a policy recompute keeps both levels
    assert vis()["gov"] == "public" and vis()["costs"] == "internal"
    ex.set_sharing(store, "northwind/gov", None)                  # back to the project setting
    assert vis()["gov"] == "internal"
    ex.set_project_sharing(store, "northwind", None)              # back to the policy
    assert set(vis().values()) == {"public"}
    ex.set_sharing(store, "contoso/market", "project")               # slide-level only, under share_everything
    assert vis()["market"] == "internal"
    new = reg(store, write(projects, "Contoso/exhibits/beans", page("Bean prices")))
    assert vis()["beans"] == "public"
    ex.set_project_sharing(store, "contoso", "project")
    reg(store, write(projects, "Contoso/exhibits/roast", page("Roast levels")))   # new slides follow the project setting
    assert vis()["roast"] == "internal"
    with pytest.raises(ValueError):
        ex.set_sharing(store, "contoso/market", "everyone")
    log = [json.loads(d) for (d,) in store.db.execute("SELECT detail FROM changelog WHERE item_kind = 'exhibit_project'")]
    assert log[0]["sharing"] == {"old": None, "new": "project"}
    assert a and b and c and new


def test_sharing_endpoint_and_portal(store, projects, tmp_path, monkeypatch):
    from starlette.requests import Request
    from fundus import mcp_server as m
    m.configure(store.path)
    portal = tmp_path / "portal"
    (portal / "js").mkdir(parents=True)
    (portal / "index.html").write_text("<!doctype html><title>Fundus</title>")
    (portal / "js" / "main.js").write_text("export {};")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setattr(m, "PORTAL_DIR", portal)
    try:
        reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))

        def post(body, origin="http://127.0.0.1:8010", ctype="application/json"):
            raw = json.dumps(body).encode()
            hdr = [(b"host", b"127.0.0.1:8010"), (b"content-type", ctype.encode())] + ([(b"origin", origin.encode())] if origin else [])
            scope = {"type": "http", "method": "POST", "path": "/api/exhibits/sharing", "path_params": {},
                     "query_string": b"", "headers": hdr}
            sent = []

            async def receive():
                if sent:
                    return {"type": "http.disconnect"}
                sent.append(1)
                return {"type": "http.request", "body": raw, "more_body": False}
            return asyncio.run(m.exhibit_sharing(Request(scope, receive)))
        r = post({"project": "northwind", "sharing": "project"})
        assert r.status_code == 200 and json.loads(r.body)["internal"] == 1
        assert post({"id": 1, "sharing": "shared"}, origin=None).status_code == 200      # no Origin: script in the Tailnet
        assert store.db.execute("SELECT visibility FROM exhibit").fetchone()[0] == "public"
        assert post({"project": "northwind", "sharing": "shared"}, origin="http://evil.test").status_code == 403
        assert post({"project": "northwind", "sharing": "shared"}, ctype="text/plain").status_code == 415
        assert post({"project": "nope", "sharing": "shared"}).status_code == 404
        assert post({"id": 1, "sharing": "everyone"}).status_code == 400

        def get(path, params):
            scope = {"type": "http", "method": "GET", "path": path, "path_params": params, "query_string": b"", "headers": []}
            return asyncio.run(m.portal_file(Request(scope)))
        assert get("/", {}).status_code == 307
        ok = get("/portal/", {"path": ""})
        assert ok.status_code == 200 and ok.media_type.startswith("text/html")
        assert get("/portal/js/main.js", {"path": "js/main.js"}).media_type.startswith("text/javascript")
        assert get("/portal/../secret.txt", {"path": "../secret.txt"}).status_code == 404
        assert get("/portal/nope.html", {"path": "nope.html"}).status_code == 404
    finally:
        m.configure(m.DEFAULT_DB)


def test_mcp_tools(store, projects):
    from fundus import mcp_server as m
    m.configure(store.path)
    try:
        f = write(projects, "Northwind/exhibits/gov", page("Board governance"))
        r = json.loads(m.fundus_exhibit_add(str(f["html"]), str(f["png"]), note="manual"))
        assert r["status"] == "new_exhibit"
        g = json.loads(m.fundus_exhibit("northwind/gov"))
        assert g["id"] == r["exhibit_id"] and g["versions"][0]["note"] == "manual"
        s = json.loads(m.fundus_search("Board governance", kind="exhibit", mode="lexical"))
        assert s["results"][0]["kind"] == "exhibit"
        assert "error" in json.loads(m.fundus_exhibit("does-not-exist"))
        assert "error" in json.loads(m.fundus_exhibit_add(str(f["html"]), "/nope.png"))
    finally:
        m.configure(m.DEFAULT_DB)


# ------------------------------------------------------------------ metrics, CLI hook
def test_metrics_block(store, projects):
    from fundus import metrics
    reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")),
        density={"v": 1, "fit": 0.9, "fmed": 15.0, "words": 120, "units": 10, "claims": 1})
    out = metrics.compute(store)["exhibits"]
    assert out["logical"] == 1 and out["versions"] == 1 and out["byKind"] == {"chart": 1}
    assert out["policy"] == "share_everything" and out["shared"] == 1 and out["byProject"][0]["project"] == "northwind"
    assert len(out["versionsByDay"]) == 30 and len(out["usage"]["exhibitCallsByDay"]) == 30
    assert out["topChartTypes"] == [{"type": "hbar", "n": 1}]
    assert out["density"]["measured"] in (0, 1)        # verdict needs the skill's density.py (present on the Spark)


def test_cli_hook(store, projects, tmp_path, capsys, monkeypatch):
    from fundus import cli
    f = write(tmp_path, "scratch/x", page("Outside the projects"))
    assert cli.main(["--db", str(store.path), "exhibits", "add", str(f["html"]), str(f["png"]), "--hook"]) == 0
    assert "skipped" in capsys.readouterr().out
    g = write(projects, "Contoso/exhibits/y", page("Inside a project"))
    dom = tmp_path / "dom.html"
    dom.write_text('<body data-fit="0.912">x</body>')
    assert cli.main(["--db", str(store.path), "exhibits", "add", str(g["html"]), str(g["png"]), "--hook", "--dom", str(dom)]) == 0
    line = json.loads(capsys.readouterr().out.strip())
    assert line["status"] == "new_exhibit" and not dom.exists()
    assert store.db.execute("SELECT fit FROM exhibit_version").fetchone()[0] == pytest.approx(0.912)
    assert cli.main(["--db", str(store.path), "exhibits", "add", "/nope.html", "/nope.png", "--hook"]) == 0  # outside: skipped
    monkeypatch.setenv("FUNDUS_EXHIBIT_FORCE", "1")
    assert cli.main(["--db", str(store.path), "exhibits", "add", "/nope.html", "/nope.png", "--hook"]) == 1  # error, no crash


RENDER = Path.home() / ".claude" / "skills" / "mck-exhibit" / "render.sh"


@pytest.mark.skipif(not RENDER.exists() or "fundus_exhibit_hook" not in RENDER.read_text(), reason="skill hook not installed")
def test_render_hook_never_blocks_or_fails(tmp_path):
    html = tmp_path / "slide.html"
    html.write_text(page("Hook test"))
    slow = tmp_path / "slowpy"
    marker = tmp_path / "hook-ran"
    slow.write_text(f"#!/bin/sh\nsleep 4\ntouch {marker}\nexit 3\n")
    slow.chmod(0o755)
    env = {**os.environ, "FUNDUS_EXHIBIT_FORCE": "1", "FUNDUS_EXHIBIT_PY": str(slow)}
    t0 = time.monotonic()
    r = subprocess.run([str(RENDER), str(html), str(tmp_path / "slide.png")], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0 and "fit=" in r.stdout and not marker.exists()      # returned before the hook finished
    assert "fundus" not in (r.stdout + r.stderr).lower()                         # output unchanged
    env["FUNDUS_EXHIBIT_PY"] = str(tmp_path / "does-not-exist")
    r2 = subprocess.run([str(RENDER), str(html), str(tmp_path / "slide2.png")], capture_output=True, text=True, env=env, timeout=120)
    assert r2.returncode == 0 and "fit=" in r2.stdout
    assert time.monotonic() - t0 < 100


# ------------------------------------------------------------------ slide sets (Foliensätze)
OLD_EXHIBIT = """CREATE TABLE exhibit (
  id              INTEGER PRIMARY KEY,
  project         TEXT NOT NULL,
  slug            TEXT NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'slide' CHECK (kind IN ('slide','chart','poster','section')),
  title           TEXT,
  eyebrow         TEXT,
  meta            TEXT,
  so_what         TEXT,
  chart_types     TEXT,
  chart_heads     TEXT,
  labels          TEXT,
  sources         TEXT,
  notes           TEXT,
  body_text       TEXT,
  palette         TEXT,
  language        TEXT,
  deck            TEXT,
  status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final','presented')),
  released        INTEGER NOT NULL DEFAULT 0,
  sources_public  INTEGER,
  internal_links  INTEGER NOT NULL DEFAULT 0,
  visibility      TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  current_version INTEGER,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT,
  UNIQUE (project, slug)
)"""


def _snapshot(db):
    return {t: sorted(tuple(r) for r in db.execute(f"SELECT * FROM {t}"))
            for t in ("exhibit_version", "exhibit_deck", "exhibit_link", "exhibit_view", "search_index")} | {
        "entity_link": sorted(tuple(r) for r in db.execute("SELECT * FROM entity_link WHERE item_kind = 'exhibit'"))}


def test_set_migration_of_old_schema(tmp_path, projects):
    """A database from before slide sets (exhibit without set_id, sharing added by ALTER) is rebuilt in place: one default
    set per project, every exhibit in it, ids, columns, versions, decks, links, views and index rows unchanged."""
    from fundus import Store
    path = tmp_path / "old.sqlite"
    st = Store(path)
    _evidence(st)
    a = reg(st, write(projects, "Northwind/exhibits/gov", page("Only 3 of 40 groups have an AI board")))
    f = write(projects, "Northwind/exhibits/costs", page("Cost structure"), png=png_bytes(rgb=(3, 3, 3)))
    reg(st, f)
    f["png"].write_bytes(png_bytes(rgb=(4, 4, 4)))
    reg(st, f)                                                               # second version
    deck = projects / "Northwind" / "exhibits" / "deck" / "02-gov.png"
    deck.parent.mkdir(parents=True)
    shutil.copy(a and projects / "Northwind/exhibits/gov.png", deck)
    ex.register(st, None, deck)
    reg(st, write(projects, "Contoso/exhibits/market", page("Coffee market"), png=png_bytes(rgb=(5, 5, 5))))
    ex.set_sharing(st, "contoso/market", "project")
    ex.record_view(st, a["exhibit_id"], "mcp")
    st.close()
    # turn it into the old schema: exhibit without set_id (sharing as an ALTER-added column), no set tables
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = OFF")
    cols = "id, project, slug, kind, title, eyebrow, meta, so_what, chart_types, chart_heads, labels, sources, notes, body_text, " \
           "palette, language, deck, status, released, sources_public, internal_links, visibility, current_version, " \
           "created_at, updated_at"
    db.executescript(f"""BEGIN; {OLD_EXHIBIT.replace('CREATE TABLE exhibit', 'CREATE TABLE exhibit_old')};
        INSERT INTO exhibit_old SELECT {cols} FROM exhibit; ALTER TABLE exhibit_old ADD COLUMN sharing TEXT
        CHECK (sharing IN ('shared','project')); UPDATE exhibit_old SET sharing = (SELECT sharing FROM exhibit e
        WHERE e.id = exhibit_old.id); DROP TABLE exhibit; ALTER TABLE exhibit_old RENAME TO exhibit;
        DROP TABLE exhibit_set_folder; DROP TABLE exhibit_set; COMMIT;""")
    before_rows = sorted(tuple(r) for r in db.execute(f"SELECT {cols}, sharing FROM exhibit"))
    before = _snapshot(db)
    db.close()
    st = Store(path)                                                         # migrates
    try:
        info = {r[1] for r in st.db.execute("PRAGMA table_info(exhibit)")}
        assert "set_id" in info
        ddl = st.db.execute("SELECT sql FROM sqlite_master WHERE name = 'exhibit'").fetchone()[0]
        assert "UNIQUE (project, set_id, slug)" in ddl and "set_id          INTEGER NOT NULL" in ddl
        assert sorted(tuple(r) for r in st.db.execute(f"SELECT {cols}, sharing FROM exhibit")) == before_rows
        assert _snapshot(st.db) == before
        sets = st.db.execute("SELECT project, slug, title, is_default FROM exhibit_set ORDER BY project").fetchall()
        assert [tuple(r) for r in sets] == [("contoso", "main", "Hauptfoliensatz", 1), ("northwind", "main", "Hauptfoliensatz", 1)]
        assert st.db.execute("SELECT count(*) FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id AND s.project = e.project "
                             "AND s.is_default = 1").fetchone()[0] == 3
        assert {r[0] for r in st.db.execute("SELECT folder FROM exhibit_set_folder")} == {""}
        assert st.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert st.db.execute("PRAGMA foreign_key_check(exhibit_version)").fetchall() == []
        g = ex.get(st, "northwind/costs")
        assert g["set"]["slug"] == "main" and [v["n"] for v in g["versions"]] == [1, 2]
        assert ex.get(st, a["exhibit_id"])["decks"][0]["file"] == "02-gov.png"
        assert ex.get(st, "contoso/main/market", include_internal=True)["visibility"] == "internal"   # slide setting kept
        log = st.db.execute("SELECT detail FROM changelog WHERE item_kind = 'exhibit_set' AND action = 'migrate'").fetchone()
        assert json.loads(log[0])["exhibits"] == 3
        # new registrations work on the migrated database; the default set takes them
        r = reg(st, write(projects, "Northwind/exhibits/new-one", page("A new slide"), png=png_bytes(rgb=(6, 6, 6))))
        assert r["status"] == "new_exhibit" and r["set"] == "main"
    finally:
        st.close()
    st = Store(path)                                                         # idempotent
    assert st.db.execute("SELECT count(*) FROM exhibit_set").fetchone()[0] == 2
    assert st.db.execute("SELECT count(*) FROM changelog WHERE action = 'migrate'").fetchone()[0] == 1
    st.close()


def test_set_detection_by_path_and_same_slug_in_two_sets(store, projects):
    main = reg(store, write(projects, "Northwind/exhibits/agenda", page("Agenda of the governance review", chart=False)))
    assert main["set"] == "main"
    s = ex.create_set(store, "northwind", "Board-Workshop März", folders=["decks/board"])
    assert s["slug"] == "board-workshop-marz" and s["folders"] == ["decks/board"] and s["moved"] == []
    b = reg(store, write(projects, "Northwind/decks/board/agenda", page("Agenda of the board workshop", chart=False),
                         png=png_bytes(rgb=(9, 9, 9))))
    assert b["status"] == "new_exhibit" and b["set"] == "board-workshop-marz" and b["exhibit_id"] != main["exhibit_id"]
    # a byte-identical deck copy inside the set's folder belongs to the set's slide, not to the default set's
    dk = projects / "Northwind" / "decks" / "board" / "deck" / "01-agenda.png"
    dk.parent.mkdir(parents=True)
    shutil.copy(projects / "Northwind/decks/board/agenda.png", dk)
    d = ex.register(store, None, dk)
    assert d["status"] == "deck_copy" and d["exhibit_id"] == b["exhibit_id"]
    # the main slide's PNG copied into the set's folder is a new slide of that set (sets do not share slides)
    cp = projects / "Northwind" / "decks" / "board" / "overview.png"
    shutil.copy(projects / "Northwind/exhibits/agenda.png", cp)
    assert ex.register(store, None, cp)["set"] == "board-workshop-marz"
    # longest prefix wins; a sibling folder with the same prefix text does not match
    ex.create_set(store, "northwind", "Board Appendix", folders=["decks/board/appendix"], slug="appendix")
    assert reg(store, write(projects, "Northwind/decks/board/appendix/agenda", page("Appendix agenda"),
                            png=png_bytes(rgb=(8, 8, 1))))["set"] == "appendix"
    assert reg(store, write(projects, "Northwind/decks/boardroom/agenda", page("Boardroom agenda"),
                            png=png_bytes(rgb=(8, 8, 2))))["set"] == "main"
    assert store.db.execute("SELECT count(*) FROM exhibit WHERE slug = 'agenda'").fetchone()[0] == 3
    # a new project gets its default set on its first registration
    n = reg(store, write(projects, "New Client/exhibits/title", page("Kick-off"), png=png_bytes(rgb=(7, 1, 1))))
    assert n["set"] == "main" and store.db.execute("SELECT is_default FROM exhibit_set WHERE project = 'new-client'").fetchone()[0] == 1


def test_explicit_set_argument(store, projects, capsys):
    from fundus import cli
    from fundus import mcp_server as m
    ex.create_set(store, "contoso", "Pricing study", folders=["pricing"])
    f = write(projects, "Contoso/exhibits/prices", page("Bean prices"))
    r = reg(store, f, set_ref="pricing-study")
    assert r["set"] == "pricing-study"
    with pytest.raises(LookupError):
        reg(store, write(projects, "Contoso/exhibits/x", page("X"), png=png_bytes(rgb=(1, 9, 1))), set_ref="nope")
    g = write(projects, "Contoso/exhibits/volumes", page("Volumes"), png=png_bytes(rgb=(2, 9, 2)))
    assert cli.main(["--db", str(store.path), "exhibits", "add", str(g["html"]), str(g["png"]), "--set", "pricing-study"]) == 0
    assert json.loads(capsys.readouterr().out)["set"] == "pricing-study"
    m.configure(store.path)
    try:
        h = write(projects, "Contoso/exhibits/margins", page("Margins"), png=png_bytes(rgb=(3, 9, 3)))
        assert json.loads(m.fundus_exhibit_add(str(h["html"]), str(h["png"]), set="pricing-study"))["set"] == "pricing-study"
        assert "error" in json.loads(m.fundus_exhibit_add(str(h["html"]), str(h["png"]), set="nope"))
    finally:
        m.configure(m.DEFAULT_DB)
    assert {e["slug"] for e in ex.listing(store, project="contoso", set_ref="pricing-study")} == {"prices", "volumes", "margins"}


def test_set_creation_moves_registered_slides(store, projects):
    intro = reg(store, write(projects, "Acme-Dealerships/workshop/intro", page("Workshop intro")))
    agenda = reg(store, write(projects, "Acme-Dealerships/workshop/agenda", page("Workshop agenda"), png=png_bytes(rgb=(1, 1, 7))))
    split = write(projects, "Acme-Dealerships/workshop/kpi", page("KPI v1"), png=png_bytes(rgb=(1, 2, 7)))
    sp = reg(store, split)
    reg(store, write(projects, "Acme-Dealerships/exhibits/kpi", page("KPI v2"), png=png_bytes(rgb=(1, 3, 7))))  # same slide elsewhere
    other = reg(store, write(projects, "Acme-Dealerships/exhibits/market", page("Market"), png=png_bytes(rgb=(1, 4, 7))))
    res = ex.create_set(store, "acme-dealerships", "Dealer workshop", folders=["workshop"], slug="workshop")
    assert {m["slug"] for m in res["moved"]} == {"intro", "agenda"} and res["exhibits"] == 2
    assert [c["slug"] for c in res["conflicts"]] == ["kpi"] and "several" in res["conflicts"][0]["reason"]
    sid = lambda eid: store.db.execute("SELECT s.slug FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id WHERE e.id = ?",
                                       (eid,)).fetchone()[0]
    assert sid(intro["exhibit_id"]) == sid(agenda["exhibit_id"]) == "workshop"
    assert sid(sp["exhibit_id"]) == sid(other["exhibit_id"]) == "main"
    moves = [json.loads(d) for (d,) in store.db.execute("SELECT detail FROM changelog WHERE item_kind = 'exhibit' AND detail LIKE '%\"set\"%'")]
    assert len(moves) == 2 and all("created" in m["reason"] for m in moves)
    # new versions of a moved slide land in the set (path) – no duplicate in main
    assert reg(store, write(projects, "Acme-Dealerships/workshop/intro", page("Workshop intro, revised"),
                            png=png_bytes(rgb=(2, 2, 9))))["exhibit_id"] == intro["exhibit_id"]
    # slug conflict: the target set has the slug already
    reg(store, write(projects, "Acme-Dealerships/exhibits/summary", page("Summary for the workshop"), png=png_bytes(rgb=(5, 1, 7))),
        set_ref="workshop")
    late = reg(store, write(projects, "Acme-Dealerships/workshop2/summary", page("Summary 2"), png=png_bytes(rgb=(6, 1, 7))))
    assert late["set"] == "main"
    r2 = ex.add_set_folder(store, "acme-dealerships/workshop", "workshop2")
    cf = {c["slug"]: c["reason"] for c in r2["conflicts"]}
    assert r2["moved"] == [] and "already exists in set 'workshop'" in cf["summary"] and "kpi" in cf
    assert "workshop2" in r2["folders"]
    # removing a folder gives its slides back to the set their path resolves to
    r3 = ex.remove_set_folder(store, "acme-dealerships/workshop", "workshop")
    assert {m["slug"] for m in r3["moved"]} == {"intro", "agenda"} and all(m["to"] == "main" for m in r3["moved"])
    with pytest.raises(ValueError):
        ex.create_set(store, "acme-dealerships", "Again", folders=["workshop2"])     # folder owned by another set
    with pytest.raises(ValueError):
        ex.create_set(store, "acme-dealerships", "Dealer workshop", folders=["x"], slug="workshop")   # slug exists
    with pytest.raises(ValueError):
        ex.create_set(store, "acme-dealerships", "Escape", folders=["../other"])
    with pytest.raises(ValueError):
        ex.remove_set_folder(store, "acme-dealerships/main", "")


def test_set_refs(store, projects):
    a = reg(store, write(projects, "Northwind/exhibits/agenda", page("Main agenda")))
    ex.create_set(store, "northwind", "Board", folders=["board"])
    ex.create_set(store, "northwind", "Strategy", folders=["strategy"])
    b = reg(store, write(projects, "Northwind/board/agenda", page("Board agenda"), png=png_bytes(rgb=(1, 5, 5))))
    c = reg(store, write(projects, "Northwind/board/intro", page("Board intro"), png=png_bytes(rgb=(2, 5, 5))))
    d = reg(store, write(projects, "Northwind/strategy/intro", page("Strategy intro"), png=png_bytes(rgb=(3, 5, 5))))
    e = reg(store, write(projects, "Northwind/strategy/vision", page("Strategy vision"), png=png_bytes(rgb=(4, 5, 5))))
    assert ex.get(store, "northwind/agenda")["id"] == a["exhibit_id"]                # default set wins
    assert ex.get(store, "northwind/board/agenda")["id"] == b["exhibit_id"]
    assert ex.get(store, "northwind/strategy/vision")["id"] == e["exhibit_id"]
    assert ex.get(store, "northwind/vision")["id"] == e["exhibit_id"]                # unique across the sets
    assert ex.get(store, "agenda", project="northwind")["id"] == a["exhibit_id"]
    with pytest.raises(LookupError) as err:
        ex.get(store, "northwind/intro")                                              # two non-default sets
    assert "northwind/board/intro" in str(err.value) and "northwind/strategy/intro" in str(err.value)
    assert ex.get(store, f"northwind/board/intro")["id"] == c["exhibit_id"]
    g = ex.get(store, d["exhibit_id"])
    assert g["set"]["slug"] == "strategy" and g["set"]["title"] == "Strategy" and g["set"]["is_default"] is False
    assert ex.versions(store, "northwind/strategy/intro")["set"]["slug"] == "strategy"
    hit = next(r for r in q.search(store, "Board agenda", kind="exhibit", mode="lexical")["results"] if r["id"] == b["exhibit_id"])
    assert hit["set"] == "board" and hit["set_title"] == "Board"
    lst = {(x["set"], x["slug"]) for x in ex.listing(store, project="northwind")}
    assert ("board", "agenda") in lst and ("main", "agenda") in lst
    with pytest.raises(LookupError):
        ex.get(store, "northwind/nope/agenda")


def test_sharing_chain_with_set_level(store, projects):
    """slide > set > project > policy."""
    ex.create_set(store, "northwind", "Board", folders=["board"])
    reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
    reg(store, write(projects, "Northwind/board/agenda", page("Board agenda"), png=png_bytes(rgb=(1, 6, 6))))
    reg(store, write(projects, "Northwind/board/costs", page("Board costs"), png=png_bytes(rgb=(2, 6, 6))))
    vis = lambda: dict(store.db.execute("SELECT slug, visibility FROM exhibit").fetchall())
    r = ex.set_set_sharing(store, "northwind/board", "project")
    assert r["changed"] == 2 and vis() == {"gov": "public", "agenda": "internal", "costs": "internal"}
    ex.set_sharing(store, "northwind/board/agenda", "shared")                 # slide beats set
    assert vis()["agenda"] == "public" and vis()["costs"] == "internal"
    ex.set_project_sharing(store, "northwind", "project")                     # project applies where set has none
    assert vis() == {"gov": "internal", "agenda": "public", "costs": "internal"}
    ex.set_set_sharing(store, "northwind/board", "shared")                    # set beats project
    assert vis() == {"gov": "internal", "agenda": "public", "costs": "public"}
    ex.set_policy(store, "internal_default")                                   # recompute keeps all levels
    assert vis() == {"gov": "internal", "agenda": "public", "costs": "public"}
    ex.set_set_sharing(store, "northwind/board", None)                        # inherit: back to the project setting
    assert vis()["costs"] == "internal"
    new = reg(store, write(projects, "Northwind/board/next", page("Next steps"), png=png_bytes(rgb=(3, 6, 6))))
    assert vis()["next"] == "internal" and new
    ex.set_set_sharing(store, "northwind/board", "shared")
    assert reg(store, write(projects, "Northwind/board/more", page("More"), png=png_bytes(rgb=(4, 6, 6))))  # new slides follow the set
    assert vis()["more"] == "public"
    with pytest.raises(ValueError):
        ex.set_set_sharing(store, "northwind/board", "everyone")
    log = [json.loads(d) for (d,) in store.db.execute("SELECT detail FROM changelog WHERE item_kind = 'exhibit_set' "
                                                      "AND action = 'update'")]
    assert log[0]["sharing"] == {"old": None, "new": "project"}


def test_gallery_shape_with_sets_and_post_with_set(store, projects):
    from starlette.requests import Request
    from fundus import mcp_server as m
    m.configure(store.path)
    try:
        reg(store, write(projects, "Northwind/exhibits/gov", page("Board governance")))
        reg(store, write(projects, "Northwind/deck/02-gov", page("Board governance")))
        board = ex.create_set(store, "northwind", "Board", folders=["board"])
        b1 = reg(store, write(projects, "Northwind/board/agenda", page("Board agenda"), png=png_bytes(rgb=(1, 7, 7))))
        reg(store, write(projects, "Northwind/board/deck/01-agenda", page("Board agenda"), png=png_bytes(rgb=(1, 7, 7))))
        reg(store, write(projects, "Contoso/exhibits/market", page("Coffee market"), png=png_bytes(rgb=(2, 7, 7))))

        def call(qs="", params=None, path="/api/exhibits"):
            scope = {"type": "http", "method": "GET", "path": path, "path_params": params or {}, "query_string": qs.encode(),
                     "headers": []}
            return asyncio.run(m.exhibit_api(Request(scope)))
        d = json.loads(call().body)
        assert set(d) == {"policy", "project_sort", "projects"}
        h = next(p for p in d["projects"] if p["project"] == "northwind")
        assert set(h) == {"project", "title", "sharing", "exhibits", "versions", "decks", "internal", "released", "updated_at", "created_at", "sets",
                          "archived", "favorites"}
        assert h["exhibits"] == 2 and [s["slug"] for s in h["sets"]] == ["main", "board"]
        s0 = h["sets"][1]
        assert set(s0) == {"id", "slug", "title", "sharing", "is_default", "exhibits", "versions", "folders", "updated_at", "archived",
                          "favorites"}
        assert s0 == {**s0, "id": board["id"], "title": "Board", "sharing": None, "is_default": False, "exhibits": 1,
                      "folders": ["board"]}
        assert h["sets"][0]["is_default"] is True and h["sets"][0]["folders"] == [""]
        g = json.loads(call("project=northwind").body)
        assert set(g) == {"policy", "project", "title", "sharing", "sets", "sequences", "set", "exhibits", "decks"}
        assert g["set"] is None and len(g["exhibits"]) == 2 and all("set_id" in e for e in g["exhibits"])
        assert {dk["deck"]: dk["set_id"] for dk in g["decks"]} == {"deck": h["sets"][0]["id"], "board/deck": board["id"]}
        assert set(g["decks"][0]) == {"deck", "set_id", "updated_at", "slides"}
        for sel in (str(board["id"]), "board"):
            gs = json.loads(call(f"project=northwind&set={sel}").body)
            assert gs["set"] == board["id"] and [e["id"] for e in gs["exhibits"]] == [b1["exhibit_id"]]
            assert [dk["deck"] for dk in gs["decks"]] == ["board/deck"] and len(gs["sets"]) == 2
        assert call("project=northwind&set=nope").status_code == 404
        one = json.loads(call(params={"ref": "northwind/board/agenda"}, path="/api/exhibits/northwind/board/agenda").body)
        assert one["id"] == b1["exhibit_id"] and one["set"]["slug"] == "board"
        # an empty new set is visible in the overview and the project view
        ex.create_set(store, "fabrikam", "Kick-off", folders=["kickoff"])
        d2 = json.loads(call().body)
        gb = next(p for p in d2["projects"] if p["project"] == "fabrikam")
        assert gb["exhibits"] == 0 and [s["slug"] for s in gb["sets"]] == ["main", "kick-off"]
        assert json.loads(call("project=fabrikam&set=kick-off").body)["exhibits"] == []

        def post(body, origin="http://127.0.0.1:8010"):
            raw = json.dumps(body).encode()
            hdr = [(b"host", b"127.0.0.1:8010"), (b"content-type", b"application/json")] + ([(b"origin", origin.encode())] if origin else [])
            scope = {"type": "http", "method": "POST", "path": "/api/exhibits/sharing", "path_params": {}, "query_string": b"",
                     "headers": hdr}
            sent = []

            async def receive():
                if sent:
                    return {"type": "http.disconnect"}
                sent.append(1)
                return {"type": "http.request", "body": raw, "more_body": False}
            return asyncio.run(m.exhibit_sharing(Request(scope, receive)))
        r = post({"set": board["id"], "sharing": "project"})
        assert r.status_code == 200 and json.loads(r.body)["internal"] == 1 and json.loads(r.body)["set"]["slug"] == "board"
        assert dict(store.db.execute("SELECT slug, visibility FROM exhibit WHERE project = 'northwind'").fetchall()) == \
            {"gov": "public", "agenda": "internal"}
        assert json.loads(call().body)["projects"][0]["sets"][1]["sharing"] == "project"
        assert post({"set": board["id"], "sharing": "inherit"}).status_code == 200
        assert store.db.execute("SELECT sharing FROM exhibit_set WHERE id = ?", (board["id"],)).fetchone()[0] is None
        assert post({"set": "board", "sharing": "shared"}).status_code == 400
        assert post({"set": 99999, "sharing": "shared"}).status_code == 404
        assert post({"set": board["id"], "sharing": "everyone"}).status_code == 400
        assert post({"set": board["id"], "sharing": "shared"}, origin="http://evil.test").status_code == 403
        assert post({"sharing": "shared"}).status_code == 400
    finally:
        m.configure(m.DEFAULT_DB)


def test_set_cli_mcp_and_metrics(store, projects, capsys):
    from fundus import cli, metrics
    from fundus import mcp_server as m
    db = ["--db", str(store.path), "exhibits", "set"]
    reg(store, write(projects, "Contoso/talks/opening", page("Opening")))
    assert cli.main(db + ["create", "contoso", "Investor talk", "--folder", "talks", "--slug", "investor"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["slug"] == "investor" and [x["slug"] for x in out["moved"]] == ["opening"]
    absf = str(projects / "Contoso" / "talks-2")
    assert cli.main(db + ["folder", "add", "contoso/investor", absf]) == 0               # absolute folder is made relative
    assert json.loads(capsys.readouterr().out)["folders"] == ["talks", "talks-2"]
    assert cli.main(db + ["rename", "contoso/investor", "Investor talk 2027"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Investor talk 2027"
    assert cli.main(db + ["list", "contoso"]) == 0
    assert [s["slug"] for s in json.loads(capsys.readouterr().out)] == ["main", "investor"]
    assert cli.main(db + ["show", "contoso/investor"]) == 0
    sh = json.loads(capsys.readouterr().out)
    assert [e["slug"] for e in sh["exhibits_list"]] == ["opening"] and sh["exhibits"] == 1
    assert cli.main(db + ["sharing", "contoso/investor", "project"]) == 0
    assert json.loads(capsys.readouterr().out)["internal"] == 1
    assert cli.main(db + ["folder", "remove", "contoso/investor", "talks-2"]) == 0
    capsys.readouterr()
    assert cli.main(db + ["create", "contoso"]) == 1                                       # usage error, no crash
    capsys.readouterr()
    m.configure(store.path)
    try:
        c = json.loads(m.fundus_exhibit_set("create", project="contoso", title="Board deck", folders=["board"]))
        assert c["slug"] == "board-deck" and c["folders"] == ["board"]
        lst = json.loads(m.fundus_exhibit_set("list", project="contoso"))["sets"]
        assert [s["slug"] for s in lst] == ["main", "investor", "board-deck"]
        assert json.loads(m.fundus_exhibit_set("show", project="contoso", slug="board-deck"))["id"] == c["id"]
        assert json.loads(m.fundus_exhibit_set("show", slug="contoso/investor"))["slug"] == "investor"
        assert "error" in json.loads(m.fundus_exhibit_set("create", project="contoso"))
        assert "error" in json.loads(m.fundus_exhibit_set("delete", project="contoso"))
        assert m.fundus_exhibit_set in m.TOOLS
    finally:
        m.configure(m.DEFAULT_DB)
    out = metrics.compute(store, use_cache=False)["exhibits"]
    assert out["sets"] == 3 and out["byProject"][0]["sets"] == 3
    assert store.stats()["exhibits"]["sets"] == 3


def test_sync_projects_registers_copies_outside_render_hook(store, projects):
    reg(store, write(projects, "Contoso/exhibits/market", page("Coffee market"), png=png_bytes(rgb=(2, 7, 7))))
    write(projects, "Contoso/exhibits/deck/01-market", page("Coffee market"), png=png_bytes(rgb=(2, 7, 7)))   # plain copy
    write(projects, "Contoso/exhibits/share", page("Market share"), png=png_bytes(rgb=(3, 7, 7)))           # never hooked
    (projects / "Elsewhere").mkdir()
    write(projects, "Elsewhere/x/foo", page("Unrelated"), png=png_bytes(rgb=(9, 7, 7)))                   # no exhibit yet
    assert ex.project_roots(store) == [projects / "Contoso"]
    out = ex.sync_projects(store)
    assert set(out) == {"Contoso", "_pdf_decks"} and out["Contoso"]["new_exhibit"] == 1 and out["Contoso"]["deck_copy"] == 1
    assert ex.sync_projects(store)["Contoso"]["new_exhibit"] == 0                                         # idempotent
    assert {r[0] for r in store.db.execute("SELECT project FROM exhibit")} == {"contoso"}


def test_older_file_found_later_is_sorted_in_by_content_date(store, projects):
    t0 = time.time() - 86400 * 3
    a = reg(store, write(projects, "Contoso/exhibits/market", page("Market v2"), png=png_bytes(rgb=(2, 7, 7)), mtime=t0 + 86400))
    b = reg(store, write(projects, "Contoso/exhibits/market", page("Market v3"), png=png_bytes(rgb=(3, 7, 7)), mtime=t0 + 2 * 86400))
    old = reg(store, write(projects, "Contoso/archive/market", page("Market v1"), png=png_bytes(rgb=(1, 7, 7)), mtime=t0))
    assert (a["version"], b["version"]) == (1, 2) and old["status"] == "new_version" and old["version"] == 1
    g = ex.get(store, a["exhibit_id"])
    assert g["current_version"] == 3 and g["title"] == "Market v3"
    assert [v["n"] for v in g["versions"]] == [1, 2, 3]


def test_fold_script_versions_cleans_old_rule(store, projects):
    f = write(projects, "Contoso/exhibits/market", page("Market"), png=png_bytes(rgb=(2, 7, 7)), mtime=time.time() - 300)
    r1 = reg(store, f)
    write(projects, "Contoso/deck/01-market", page("Market"), png=png_bytes(rgb=(2, 7, 7)))
    eid = r1["exhibit_id"]
    # what the old rule produced: the same PNG and HTML again as v2 (script edit only), then real new content as v3
    v1 = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ?", (eid,)).fetchone()
    cols = [c for c in v1.keys() if c not in ("id", "n", "script_sha", "created_at")]
    store.db.execute(f"INSERT INTO exhibit_version(n, script_sha, created_at, {', '.join(cols)}) SELECT 2, 'abc', "
                     f"datetime(created_at, '+1 minute'), {', '.join(cols)} FROM exhibit_version WHERE id = ?", (v1["id"],))
    store.db.commit()
    reg(store, write(projects, "Contoso/exhibits/market", page("Market new"), png=png_bytes(rgb=(5, 7, 7))))
    store.db.execute("INSERT INTO exhibit_deck(project, deck, file, exhibit_id, version) VALUES ('contoso', 'deck', 'x.png', ?, 3)", (eid,))
    store.db.commit()
    dry = ex.fold_script_versions(store)
    assert dry["versions_removed"] == 1 and not dry["applied"]
    assert store.db.execute("SELECT count(*) FROM exhibit_version WHERE exhibit_id = ?", (eid,)).fetchone()[0] == 3
    res = ex.fold_script_versions(store, apply=True)
    assert res["versions_removed"] == 1
    vs = store.db.execute("SELECT n, script_sha FROM exhibit_version WHERE exhibit_id = ? ORDER BY n", (eid,)).fetchall()
    assert [v[0] for v in vs] == [1, 2] and vs[0][1] == "abc"                 # v1 now carries the newer script
    assert store.db.execute("SELECT version FROM exhibit_deck WHERE file = 'x.png'").fetchone()[0] == 2
    assert ex.get(store, eid)["current_version"] == 2
    assert ex.fold_script_versions(store, apply=True)["versions_removed"] == 0


def test_render_outside_projects_is_not_given_a_guessed_project(store, projects, tmp_path):
    f = write(projects, "Acme-Dealerships/exhibits/kpi-1", page("Dealers"), png=png_bytes(rgb=(9, 9, 90)))
    r = reg(store, f)
    scratch = tmp_path / "claude-1000" / "-home-user-Projects-P" / "abc" / "scratchpad"
    scratch.mkdir(parents=True)
    copy = scratch / "new-exhibits-hsg-kpi-1.png"
    copy.write_bytes(f["png"].read_bytes())
    (scratch / "new-exhibits-hsg-kpi-1.html").write_text(f["html"].read_text())
    out = ex.register(store, str(scratch / "new-exhibits-hsg-kpi-1.html"), str(copy))
    assert out["status"] == "duplicate" and out["project"] == "acme-dealerships" and out["exhibit_id"] == r["exhibit_id"]
    other = scratch / "fresh.png"
    other.write_bytes(png_bytes(rgb=(1, 200, 1)))
    with pytest.raises(ValueError, match="outside ~/Projects"):
        ex.register(store, None, str(other))
    assert ex.register(store, None, str(other), project="acme-dealerships")["project"] == "acme-dealerships"
    assert not store.db.execute("SELECT 1 FROM exhibit WHERE project = 'scratchpad'").fetchone()


def test_zhaw_palette_is_a_variant_of_the_same_slide():
    assert ex.exhibit_slug("kpi-1-turnover-zhaw") == "kpi-1-turnover"
    html = page("Turnover", color="#051C2C").replace("</body>", '<svg id="ZHAW_Logo"></svg></body>', 1)
    assert ex._palette_from_text(html) == "zhaw"
    assert ex._palette_from_text(page("Turnover", color="#051C2C")) == "blue"


def test_find_script_prefers_slide_named_script(tmp_path):
    from fundus.exhibits import find_script
    root = tmp_path / "proj"
    out = root / "30_folien" / "exhibits"
    sk = root / "30_folien" / "skripte"
    out.mkdir(parents=True), sk.mkdir(parents=True)
    png = out / "fw-01b-canvas-origin.png"
    png.write_bytes(b"x")
    # the deck order file names every slug and sorts first; the slide's own script must still win
    (sk / "deck_structure.py").write_text('ORDER = ["fw-01b-canvas-origin.html", "fw-02.html"]\n')
    (sk / "fw-01b-canvas-origin.py").write_text('import mck\nmck.page(out="fw-01b-canvas-origin")\n')
    assert find_script(png, root, png.stem).name == "fw-01b-canvas-origin.py"
    # a slide script that only calls a shared frame module still wins over the helper that lists all slugs
    (sk / "aibm-05-seat.py").write_text("from aibm_frame import *\nbuild()\n")
    (sk / "ai_icons.py").write_text('SLUGS = {"aibm-05-seat": 1}\n')
    png3 = out / "aibm-05-seat.png"
    png3.write_bytes(b"x")
    assert find_script(png3, root, png3.stem).name == "aibm-05-seat.py"
    # an analysis script with underscores in the name is not the slide's script
    (sk / "fw_02.py").write_text("import duckdb\nprint(1)\n")
    png2 = out / "fw-02.png"
    png2.write_bytes(b"x")
    assert find_script(png2, root, png2.stem).name == "deck_structure.py"


def test_register_takes_the_calling_script(store, projects):
    # the script that ran render.sh (in a sub-folder skripte/s3/) is the slide's script, even if another project has a
    # same-named one; a runner that renders prebuilt HTML is not taken
    s3 = projects / "Workbench" / "blue" / "skripte" / "s3"
    s3.mkdir(parents=True)
    own = s3 / "s3-a2-pricing.py"
    own.write_text("from frame import *\nbuild()\n")
    f = write(projects, "Workbench/blue/s3-a2-pricing", page("Pricing differs"))
    r = reg(store, f, caller=str(own))
    assert r["script"] == str(own.resolve())
    # without the caller the search also looks one level into skripte/ and finds it by name
    f2 = write(projects, "Workbench/blue/s3-a2-pricing", page("Pricing differs a lot"))
    assert reg(store, f2, caller=None)["script"] == str(own.resolve())
    runner = projects / "Workbench" / "render_all.py"
    runner.write_text("import subprocess\nfor h in files: subprocess.run(['render.sh', h])\n")
    f3 = write(projects, "Workbench/blue/s3-a2-pricing", page("Pricing differs most"))
    assert reg(store, f3, caller=str(runner))["script"] == str(own.resolve())


def test_scan_skips_pictures_without_source(tmp_path):
    """Website pictures and page renders without HTML/SVG are not slides; slide-shaped PNGs and copies of renders are."""
    write(tmp_path, "site/media/originals/team-photo", png=png_bytes(w=1000, h=1000, rgb=(1, 2, 3)))
    write(tmp_path, "site/pages/p-001", png=png_bytes(w=1000, h=563, rgb=(4, 5, 6)))
    write(tmp_path, "site/deck/export-01", png=png_bytes(w=1920, h=1080, rgb=(7, 8, 9)))
    write(tmp_path, "site/exhibits/chart", html=page("Chart"), png=png_bytes(w=640, h=360, rgb=(10, 11, 12)))
    write(tmp_path, "site/deck/chart-copy", png=png_bytes(w=640, h=360, rgb=(10, 11, 12)))
    items, skipped = ex.scan(tmp_path)
    names = sorted(Path(i["png"]).stem for i in items)
    assert names == ["chart", "chart-copy", "export-01"], names
    assert sum("not slide-shaped" in s["reason"] for s in skipped) == 2


def test_scan_skips_a_fundus_data_folder(tmp_path):
    """The nightly sync must never read Fundus' own content-addressed store as renders (3 Nov 2026: 3,698 stored files
    became a project 'fundus' after a received package put a render path inside the Fundus folder)."""
    from fundus import exhibits as ex
    proj = tmp_path / "Fundus"
    data = proj / "data"
    (data / "exhibits" / "ab").mkdir(parents=True)
    (data / "fundus.sqlite").write_bytes(b"")
    (data / "exhibits" / "ab" / ("ab" * 32 + ".png")).write_bytes(b"\x89PNG")
    (proj / "slides").mkdir()
    (proj / "slides" / "real.png").write_bytes(b"\x89PNG")
    items, skipped = ex.scan(proj)
    names = [str(x) for x in items] + [str(x) for x in skipped]
    assert not any("/data/" in n for n in names)
    assert ex._fundus_data(data / "exhibits") and not ex._fundus_data(proj / "slides")
