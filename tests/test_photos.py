"""Photo archive (fundus.photos): import (dedupe, RAW pairs, HEIC, pictures from slide decks), annotation by a (fake)
vision LLM, own keywords that survive re-annotation, rights per purpose, search, groups, uses, MCP tools."""
import io
import json
import zipfile

import pytest
from PIL import Image

from fundus import mcp_server as m
from fundus import photos


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_PHOTO_DIR", str(tmp_path / "photos"))
    yield
    photos.set_llm(None)
    m.configure(m.DEFAULT_DB)


def jpeg(path, size=(1200, 800), rgb=(120, 60, 30), exif_date=None):
    im = Image.new("RGB", size, rgb)
    for x in range(0, size[0], 50):                      # some structure so the dhash differs between colours
        for y in range(0, size[1], 7):
            im.putpixel((x, y), (255 - rgb[0], 255 - rgb[1], 255 - rgb[2]))
    kw = {}
    if exif_date:
        ex = Image.Exif()
        ex[0x0110] = "Field Cam"
        ex.get_ifd(0x8769)[36867] = exif_date
        kw["exif"] = ex
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", **kw)
    return path


class FakeLLM:
    model = "fake-vision"

    def __init__(self, answer=None):
        self.answer = answer or {}
        self.prompts = []

    def describe(self, prompt, jpeg_bytes):
        assert jpeg_bytes[:2] == b"\xff\xd8"
        self.prompts.append(prompt)
        base = {"caption_en": "A nurse writes on a paper form at a ward station.", "caption_de": "Eine Pflegende schreibt auf ein Formular.",
                "alt_en": "Nurse with paper form", "alt_de": "Pflegende mit Formular",
                "interpretation": "Paper persists next to the digital system: a workaround.",
                "setting": ["hospital-ward"], "activity": ["documentation", "workaround"], "role": ["nurse"],
                "artefact": ["form", "flying-saucer"], "phase": ["understand"], "method": [], "mood": ["stressful"],
                "industry": ["healthcare", "no-such-industry"], "region": [], "keywords": ["Paper Trail", "shift"],
                "people_count": 1, "faces_identifiable": True, "text_visible": ["Station 3"], "copy_space": "left",
                "quality": "good", "kind": "photo"}
        return {**base, **self.answer}


def test_import_dedupes_and_pairs_raw_and_strips_metadata(store, tmp_path):
    a = jpeg(tmp_path / "in" / "IMG_1.jpg", exif_date="2019:05:14 10:20:30")
    jpeg(tmp_path / "in" / "IMG_2.jpg", rgb=(10, 200, 30))
    (tmp_path / "in" / "IMG_2.CR2").write_bytes(b"not really raw")         # RAW twin of IMG_2: stored, not decoded
    (tmp_path / "in" / "notes.txt").write_text("x")
    res = photos.import_paths(store, [tmp_path / "in"], survey="Feldstudie Spital 2019", project="northwind",
                              period="2019", place="Zürich")
    assert res["imported"] == 2 and res["duplicates"] == 0 and not res["failed"]
    again = photos.import_paths(store, [a], survey="Feldstudie Spital 2019")
    assert again["imported"] == 0 and again["duplicates"] == 1
    p1 = photos.get(store, res["ids"][0])
    assert p1["taken_at"] == "2019-05-14 10:20:30" and p1["orientation"] == "landscape" and p1["width"] == 1200
    assert p1["groups"][0]["title"] == "Feldstudie Spital 2019" and p1["status"] == "new"
    p2 = photos.get(store, res["ids"][1])
    assert p2["raw_url"] and p2["raw_url"].endswith(".orig.cr2")
    # derived files exist, are JPEGs without EXIF, sized
    sha = store.db.execute("SELECT sha FROM photo WHERE id = ?", (p1["id"],)).fetchone()[0]
    with Image.open(photos.file_path(store, sha, "thumb.jpg")) as t:
        assert max(t.size) == 480 and not t.getexif()
    path, ctype, _ = photos.resolve_file(store, f"{sha}.slide.jpg")
    assert path.is_file() and ctype == "image/jpeg"
    with pytest.raises(ValueError):
        photos.resolve_file(store, "../etc/passwd")


def test_heic_import(store, tmp_path):
    import pillow_heif
    pillow_heif.register_heif_opener()
    f = tmp_path / "IMG_9.HEIC"
    Image.new("RGB", (900, 1200), (30, 90, 160)).save(f, "HEIF")
    res = photos.import_paths(store, [f])
    assert res["imported"] == 1, res
    assert photos.get(store, res["ids"][0])["orientation"] == "portrait"


def test_pictures_from_slide_deck_and_non_photos_hidden(store, tmp_path):
    deck = tmp_path / "deck.pptx"
    big, small = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (1000, 700), (200, 10, 10)).save(big, "JPEG")
    Image.new("RGB", (64, 64), (0, 0, 0)).save(small, "PNG")
    with zipfile.ZipFile(deck, "w") as z:
        z.writestr("ppt/slides/slide1.xml", "<x/>")
        z.writestr("ppt/media/image1.jpeg", big.getvalue())
        z.writestr("ppt/media/image2.png", small.getvalue())   # icon: skipped
    res = photos.import_paths(store, [deck], note="aus Deck X")
    assert res["imported"] == 1
    p = photos.get(store, res["ids"][0])
    assert p["source_path"].endswith("deck.pptx#ppt/media/image1.jpeg") and p["note"] == "aus Deck X"
    r = photos.annotate_one(store, p["id"], FakeLLM({"kind": "diagram"}))
    assert r["hidden"]
    assert photos.search(store, purpose="all")["total"] == 0


def test_annotation_tags_own_keywords_win_and_survive(store, tmp_path):
    res = photos.import_paths(store, [jpeg(tmp_path / "a.jpg")], survey="Spital")
    pid = res["ids"][0]
    photos.set_tags(store, pid, add=["setting:operating-room", {"dim": "keyword", "value": "Nachtschicht"}])
    llm = FakeLLM()
    out = photos.annotate_pending(store, llm=llm)
    assert out["annotated"] == 1 and out["left"] == 0
    assert "setting=operating-room" in llm.prompts[0] and "Spital" in llm.prompts[0]
    p = photos.get(store, pid)
    tags = {(t["dim"], t["value"]): t for t in p["tags"]}
    assert tags[("setting", "operating-room")]["source"] == "own"
    assert tags[("setting", "hospital-ward")]["state"] == "proposed"
    assert ("keyword", "flying-saucer") in tags                 # unknown artefact -> free keyword
    assert ("industry", "healthcare") in tags and ("industry", "no-such-industry") not in tags
    assert ("keyword", "paper-trail") in tags
    assert p["faces"] == "yes" and p["people_count"] == 1 and p["status"] == "annotated"
    assert p["annotation"]["copy_space"] == "left" and p["interpretation"].startswith("Paper persists")
    # Falk rejects one proposal and confirms the photo; a re-annotation keeps both decisions
    photos.set_tags(store, pid, reject=["mood:stressful"])
    photos.confirm(store, pid)
    photos.reannotate(store, pid)
    photos.annotate_pending(store, llm=FakeLLM())
    p = photos.get(store, pid)
    tags = {(t["dim"], t["value"]): t for t in p["tags"]}
    assert tags[("mood", "stressful")]["state"] == "rejected"
    assert tags[("setting", "hospital-ward")]["state"] == "confirmed"
    assert p["status"] == "annotated"          # new proposals after a re-annotation: look again
    # own faces setting is not overwritten by the LLM
    photos.update(store, pid, {"faces": "no"})
    photos.reannotate(store, pid)
    photos.annotate_pending(store, llm=FakeLLM())
    assert photos.get(store, pid)["faces"] == "no"


def test_rights_decide_what_sessions_get(store, tmp_path):
    # default (Falk, 28 Oct 2026): everything uploaded is cleared for public use
    d = photos.import_paths(store, [jpeg(tmp_path / "dflt.jpg", rgb=(9, 99, 9))])["ids"][0]
    assert (photos.get(store, d)["consent"], photos.get(store, d)["usage"], photos.get(store, d)["cleared_for"]) == ("yes", "publication", "external")
    photos.remove(store, d)
    ids = photos.import_paths(store, [jpeg(tmp_path / f"{i}.jpg", rgb=(i * 40, 20, 20)) for i in range(4)],
                              consent="unknown", usage="internal")["ids"]
    for pid in ids:
        photos.annotate_one(store, pid, FakeLLM())                 # all show an identifiable face
    photos.update(store, ids[0], {"usage": "publication", "consent": "yes"})      # external
    photos.update(store, ids[1], {"usage": "publication"})                        # face, no consent -> internal only
    photos.update(store, ids[2], {"usage": "teaching", "consent": "yes"})         # teaching
    photos.update(store, ids[3], {"consent": "no"})                               # never
    got = lambda purpose: {p["id"] for p in photos.search(store, purpose=purpose)["photos"]}
    assert got("external") == {ids[0]}
    assert got("teaching") == {ids[0], ids[2]}
    assert got("internal") == {ids[0], ids[1], ids[2]}
    assert got("all") == set(ids)
    assert photos.search(store, purpose="external")["hidden_by_rights"] == 3
    assert photos.get(store, ids[1])["cleared_for"] == "internal"


def test_search_text_dims_groups_and_use(store, tmp_path):
    a, b = photos.import_paths(store, [jpeg(tmp_path / "a.jpg", rgb=(1, 2, 3)), jpeg(tmp_path / "b.jpg", rgb=(200, 2, 3))])["ids"]
    photos.annotate_one(store, a, FakeLLM())
    photos.annotate_one(store, b, FakeLLM({"caption_en": "Engineers test a prototype on the factory floor.", "caption_de": "",
                                           "setting": ["factory-floor"], "activity": ["user-test"], "role": ["engineer"],
                                           "interpretation": "", "keywords": [], "faces_identifiable": False, "people_count": 2,
                                           "artefact": ["prototype"], "mood": [], "industry": ["manufacturing"], "phase": ["test"]}))
    r = photos.search(store, "Formular Pflege", purpose="all")
    assert [p["id"] for p in r["photos"]] == [a]
    r = photos.search(store, dims={"setting": ["factory-floor"]}, purpose="all")
    assert [p["id"] for p in r["photos"]] == [b] and r["facets"]["role"] == {"engineer": 1}
    assert photos.search(store, people="none", purpose="all")["total"] == 0
    alb = photos.create_group(store, "album", "Workarounds")
    photos.add_to_group(store, alb["id"], [a])
    assert [p["id"] for p in photos.search(store, group="workarounds", purpose="all")["photos"]] == [a]
    assert photos.groups(store)[0]["n"] == 1
    use = photos.record_use(store, f"photo:{a}", exhibit="northwind/main/x", project="northwind")
    assert use["credit"] == f"Photo: {photos.DEFAULT_CREDIT}"
    assert photos.get(store, a)["uses"][0]["ref"] == "northwind/main/x"
    photos.remove(store, [b])
    assert photos.search(store, purpose="all")["total"] == 1
    photos.remove(store, [b], restore=True)
    assert photos.search(store, purpose="all")["total"] == 2


def test_mcp_tools(store, tmp_path):
    m.configure(store.path)
    out = json.loads(m.fundus_photo_import([str(jpeg(tmp_path / "x.jpg"))], survey="Werk 2018", annotate=False))
    assert out["imported"] == 1
    pid = out["ids"][0]
    photos.annotate_one(store, pid, FakeLLM({"faces_identifiable": False}))
    photos.update(store, pid, {"usage": "publication"})
    hits = json.loads(m.fundus_photo_search("nurse paper form"))
    assert hits["photos"][0]["id"] == pid and hits["photos"][0]["slide_url"].endswith(".slide.jpg")
    one = json.loads(m.fundus_photo(f"photo:{pid}"))
    assert one["slide_path"].endswith(".slide.jpg") and one["alt_en"]
    used = json.loads(m.fundus_photo(f"photo:{pid}", action="use", exhibit="p/main/s"))
    assert used["credit"].startswith("Photo:")
    assert "error" in json.loads(m.fundus_photo_search("x", purpose="nonsense"))


def test_portal_api(store, tmp_path):
    from test_reviews import call
    m.configure(store.path)
    pid = photos.import_paths(store, [jpeg(tmp_path / "x.jpg")], survey="Werk")["ids"][0]
    photos.annotate_one(store, pid, FakeLLM())
    code, out = call(m, m.photos_api, query=b'q=nurse&dims={"setting":["hospital-ward"]}', path="/api/photos")
    assert code == 200 and out["total"] == 1 and out["photos"][0]["id"] == pid
    code, meta = call(m, m.photos_api, path="/api/photos/meta")
    assert code == 200 and meta["stats"]["n"] == 1 and meta["dims"]["setting"]["values"]
    code, one = call(m, m.photos_api, "POST", f"/api/photos/{pid}", path_params={"pid": pid},
                     body={"title": "Station 3", "consent": "yes", "tags": {"add": ["keyword:paper"], "reject": ["mood:stressful"]},
                           "action": "confirm"})
    assert code == 200 and one["title"] == "Station 3" and one["consent"] == "yes" and one["status"] == "confirmed"
    code, g = call(m, m.photo_groups_api, "POST", "/api/photo-groups", body={"kind": "album", "title": "Pflege"})
    code, b = call(m, m.photos_api, "POST", "/api/photos/bulk", body={"ids": [pid], "action": "group_add", "group": g["id"]})
    assert code == 200 and photos.groups(store, gid=g["id"])[0]["n"] == 1
    code, _ = call(m, m.photos_api, "POST", f"/api/photos/{pid}", path_params={"pid": pid}, body={"consent": "maybe"})
    assert code == 400
    code, _ = call(m, m.photos_api, "POST", "/api/photos/bulk", body={"ids": [pid], "action": "confirm"}, origin="http://evil.example")
    assert code == 403


def test_slide_render_records_photo_use(store, tmp_path, monkeypatch):
    from test_exhibits import page, png_bytes, reg, write
    from fundus import exhibits as ex
    root = tmp_path / "Projects"
    monkeypatch.setattr(ex, "PROJECTS_ROOT", root)
    pid = photos.import_paths(store, [jpeg(tmp_path / "x.jpg")])["ids"][0]
    html = page("Paper persists", extra=f'<img src="data:image/jpeg;base64,AA" data-fundus-photo="photo:{pid}">')
    r = reg(store, write(root, "Northwind/exhibits/paper", html, png=png_bytes()))
    uses = photos.get(store, pid)["uses"]
    assert len(uses) == 1 and uses[0]["exhibit_id"] == r["exhibit_id"] and uses[0]["project"] == "northwind"
    reg(store, write(root, "Northwind/exhibits/paper", html + " ", png=png_bytes(rgb=(1, 1, 1))))   # new version: no 2nd row
    assert len(photos.get(store, pid)["uses"]) == 1
