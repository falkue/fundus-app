"""Ordered decks (Reihenfolgen): manifests from sessions, deck PDFs matched page by page, nightly scan, portal data."""
import json
import os
import time

import pytest
from PIL import Image

from fundus import decks
from fundus import exhibits as ex
from test_exhibits import page, png_bytes, reg, write, projects as projects_root  # noqa: F401  (fixture)

COLORS = [(230, 40, 40), (40, 160, 60), (40, 70, 200), (240, 200, 30), (150, 60, 170), (20, 20, 20)]


def build(projects_root, store, n=5):
    out = []
    for i in range(n):
        f = write(projects_root, f"Acme-Dealerships/exhibits-hsg/slide-{i}", page(f"Slide {i}"), png=png_bytes(rgb=COLORS[i]))
        out.append((f, reg(store, f)))
    return out


def make_pdf(path, pngs, extra_blank=0):
    ims = [Image.open(p).convert("RGB").resize((640, 360)) for p in pngs]
    ims += [Image.new("RGB", (640, 360), (255, 255, 255)) for _ in range(extra_blank)]
    path.parent.mkdir(parents=True, exist_ok=True)
    ims[0].save(path, save_all=True, append_images=ims[1:])
    return path


def test_manifest_paths_refs_replace_and_missing(store, projects_root):
    s = build(projects_root, store, 4)
    order = [str(s[2][0]["png"]), "exhibits-hsg/slide-0.png", f"acme-dealerships/slide-3", str(s[1][1]["exhibit_id"]), "nope.png"]
    d = decks.set_sequence(store, "acme-dealerships", "deck-v4", order, title="Deck v4")
    assert [x["slug"] for x in d["slides"]] == ["slide-2", "slide-0", "slide-3", "slide-1"]
    assert d["missing"] == ["nope.png"] and d["source"] == "manifest" and d["title"] == "Deck v4" and d["set_id"]
    # a new render in the manifest is registered first
    new = write(projects_root, "Acme-Dealerships/exhibits-hsg/slide-9", page("Slide 9"), png=png_bytes(rgb=COLORS[5]))
    d2 = decks.set_sequence(store, "acme-dealerships", "deck-v4", [str(new["png"]), str(s[0][0]["png"])])
    assert d2["id"] == d["id"] and [x["slug"] for x in d2["slides"]] == ["slide-9", "slide-0"] and d2["title"] == "Deck v4"
    assert [x["name"] for x in decks.list_sequences(store, "acme-dealerships")] == ["deck-v4"]
    with pytest.raises(LookupError):
        decks.set_sequence(store, "acme-dealerships", "x", ["nope.png"])
    with pytest.raises(ValueError):
        decks.set_sequence(store, "acme-dealerships", "", ["a.png"])
    g = ex.gallery(store, "acme-dealerships")
    assert [q["name"] for q in g["sequences"]] == ["deck-v4"] and g["sequences"][0]["slides"][0]["position"] == 1
    assert decks.delete_sequence(store, "acme-dealerships/deck-v4")["name"] == "deck-v4"
    assert ex.gallery(store, "acme-dealerships")["sequences"] == []


def test_pdf_deck_matched_page_by_page(store, projects_root):
    s = build(projects_root, store, 5)
    pngs = [s[i][0]["png"] for i in (3, 0, 4, 1, 2)]
    pdf = make_pdf(projects_root / "Acme-Dealerships" / "pdf" / "Deck-v5.pdf", pngs, extra_blank=1)
    r = decks.import_pdf(store, pdf)
    assert r["status"] == "deck" and r["pages"] == 6 and r["matched"] == 5 and r["unmatched_pages"] == [6]
    d = decks.get_sequence(store, r["deck"])
    assert [x["slug"] for x in d["slides"]] == ["slide-3", "slide-0", "slide-4", "slide-1", "slide-2"]
    assert d["source"] == "pdf" and d["name"] == "Deck-v5"
    assert decks.import_pdf(store, pdf)["status"] == "unchanged"
    # mostly unknown pages: not a deck
    other = make_pdf(projects_root / "Acme-Dealerships" / "pdf" / "paper.pdf", [pngs[0]], extra_blank=5)
    assert decks.import_pdf(store, other)["status"] == "not_a_deck"


def test_manifest_naming_the_pdf_wins(store, projects_root):
    s = build(projects_root, store, 4)
    pdf = make_pdf(projects_root / "Acme-Dealerships" / "pdf" / "Deck-v6.pdf", [s[i][0]["png"] for i in (0, 1, 2, 3)])
    decks.set_sequence(store, "acme-dealerships", "deck-v6", [str(s[i][0]["png"]) for i in (3, 2, 1, 0)], pdf=str(pdf))
    assert decks.import_pdf(store, pdf)["status"] in ("unchanged", "manifest")
    assert [x["name"] for x in decks.list_sequences(store, "acme-dealerships")] == ["deck-v6"]


def test_sync_pdfs_skips_web_copy_and_research(store, projects_root):
    s = build(projects_root, store, 4)
    pngs = [x[0]["png"] for x in s]
    root = projects_root / "Acme-Dealerships"
    make_pdf(root / "pdf" / "Deck-v7.pdf", pngs)
    make_pdf(root / "pdf" / "Deck-v7_web.pdf", pngs[::-1])
    make_pdf(root / "recherche" / "study.pdf", pngs)
    out = decks.sync_pdfs(store)
    assert out["acme-dealerships"] == {"deck": 1}
    assert [x["name"] for x in decks.list_sequences(store)] == ["Deck-v7"]
    assert decks.sync_pdfs(store)["acme-dealerships"] == {"unchanged": 1}
    assert "_pdf_decks" in ex.sync_projects(store)


def test_renumbered_versions_follow_in_decks(store, projects_root):
    t0 = time.time() - 86400
    a = reg(store, write(projects_root, "Contoso/exhibits/m", page("M2"), png=png_bytes(rgb=COLORS[0]), mtime=t0 + 100))
    d = decks.set_sequence(store, "contoso", "deck", [a["exhibit_id"]])
    assert d["slides"][0]["version"] == 1
    reg(store, write(projects_root, "Contoso/archive/m", page("M1"), png=png_bytes(rgb=COLORS[1]), mtime=t0))   # older, found later
    assert decks.get_sequence(store, d["id"])["slides"][0]["version"] == 2


def test_cli_and_mcp(store, projects_root, capsys):
    from fundus import cli
    from fundus import mcp_server as m
    s = build(projects_root, store, 2)
    db = ["--db", str(store.path), "exhibits", "deck"]
    assert cli.main(db + ["set", "acme-dealerships", "deck-a", str(s[1][0]["png"]), str(s[0][0]["png"]), "--title", "A"]) == 0
    assert [x["slug"] for x in json.loads(capsys.readouterr().out)["slides"]] == ["slide-1", "slide-0"]
    assert cli.main(db + ["list"]) == 0 and json.loads(capsys.readouterr().out)[0]["title"] == "A"
    m.configure(store.path)
    try:
        r = json.loads(m.fundus_exhibit_deck(project="acme-dealerships", name="deck-b", slides=[str(s[0][0]["png"])]))
        assert r["name"] == "deck-b" and r["slides"][0]["slug"] == "slide-0"
        assert "error" in json.loads(m.fundus_exhibit_deck(project="acme-dealerships", name="x"))
        assert len(json.loads(m.fundus_exhibit_deck(action="list"))["decks"]) == 2
    finally:
        m.configure(m.DEFAULT_DB)


def test_slide_files_for_presentations(store, projects_root, tmp_path):
    """`exhibits files`: slides of a deck, a slide ref or rendered files, in order, with stored HTML/PNG copies."""
    from fundus import slide_search as ss
    s = build(projects_root, store, 3)
    decks.set_sequence(store, "acme-dealerships", "deck-v1", [str(s[2][0]["png"]), str(s[0][0]["png"])])
    r = ss.slide_files(store, ["deck:acme-dealerships/deck-v1", "acme-dealerships/slide-1", str(s[0][0]["html"])])
    assert [x["slug"] for x in r["slides"]] == ["slide-2", "slide-0", "slide-1"]        # slide-0 again via its file: once
    x = r["slides"][0]
    assert x["html"].endswith(".html") and os.path.isfile(x["html"]) and os.path.isfile(x["png"]) and x["version"] == 1
    assert x["from"] == "deck:acme-dealerships/deck-v1" and "confidence 0" in x["warning"]
    loose = tmp_path / "loose.html"
    loose.write_text("<html><body><h1>x</h1></body></html>")
    assert ss.slide_files(store, [str(loose)])["slides"] == [{"file": str(loose.resolve()), "registered": False, "from": str(loose)}]
    with pytest.raises(LookupError):
        ss.slide_files(store, [str(tmp_path / "nope.png")])
    with pytest.raises(ValueError):
        ss.slide_files(store, ["deck:x/y"], version="newest")
