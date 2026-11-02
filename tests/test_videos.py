"""Videos in the media archive (fundus.photos + fundus.videos): import with ffmpeg (playable copy, poster, contact
sheet), Live Photos stay photos, videos from slide decks, annotation via the contact sheet, sound rights, muted copy,
search by medium, files over HTTP, use on a rendered slide. Needs ffmpeg/ffprobe (skipped without)."""
import json
import shutil
import subprocess
import zipfile

import pytest

from fundus import mcp_server as m
from fundus import photos, videos
from test_photos import FakeLLM, jpeg

pytestmark = pytest.mark.skipif(not videos.available(), reason="ffmpeg/ffprobe not installed")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNDUS_PHOTO_DIR", str(tmp_path / "photos"))
    yield
    photos.set_llm(None)
    m.configure(m.DEFAULT_DB)


def clip(path, seconds=2, size="640x360", sound=True, created=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25"]
    if sound:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=330"]
    cmd += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if sound else [])
    if created:
        cmd += ["-metadata", f"creation_time={created}"]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return path


def streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json",
                        str(path)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"]


def test_import_video_files_poster_sheet_and_probe(store, tmp_path):
    v = clip(tmp_path / "in" / "Interview.mov", seconds=3, size="1280x720", created="2025-05-14T10:20:00Z")
    p = clip(tmp_path / "in" / "Portrait.mp4", seconds=1, size="360x640", sound=False)
    r = photos.import_paths(store, [tmp_path / "in"], survey="Spital 2025")
    assert r["imported"] == 2 and not r["failed"]
    a, b = (photos.get(store, i) for i in r["ids"])
    a, b = (a, b) if a["orig_name"] == "Interview.mov" else (b, a)
    assert a["ref"] == f"video:{a['id']}" and a["media"] == "video" and a["duration"] == pytest.approx(3, abs=0.2)
    assert (a["width"], a["height"], a["orientation"]) == (1280, 720, "landscape") and a["taken_at"].startswith("2025-05-14")
    assert a["audio"] == "unknown" and a["mute_url"].endswith(".mute.mp4") and a["video_url"].endswith(".video.mp4")
    assert (b["orientation"], b["audio"], "mute_url" in b) == ("portrait", "none", False)
    sha = photos._row(store, a["id"])["sha"]
    for part in ("orig.mov", "video.mp4", "thumb.jpg", "view.jpg", "slide.jpg", "sheet.jpg"):
        assert photos.file_path(store, sha, part).is_file(), part
    s = {x["codec_type"]: x for x in streams(photos.file_path(store, sha, "video.mp4"))}
    assert s["video"]["codec_name"] == "h264" and s["audio"]["codec_name"] == "aac"
    assert photos.import_paths(store, [v])["duplicates"] == 1                               # same content: once
    assert photos.search(store, media="video")["total"] == 2 and photos.search(store, media="photo")["total"] == 0
    assert photos.stats(store)["videos"] == 2


def test_live_photo_stays_a_photo_and_deck_videos_are_taken(store, tmp_path):
    jpeg(tmp_path / "live" / "IMG_7.jpg")
    clip(tmp_path / "live" / "IMG_7.mov", seconds=1)
    r = photos.import_paths(store, [tmp_path / "live"])
    assert r["imported"] == 1 and photos.get(store, r["ids"][0])["media"] == "photo"
    deck = tmp_path / "deck.pptx"
    with zipfile.ZipFile(deck, "w") as z:
        z.write(clip(tmp_path / "m.mp4", seconds=1), "ppt/media/media1.mp4")
        z.write(jpeg(tmp_path / "pic.jpg", rgb=(10, 200, 10)), "ppt/media/image1.jpeg")
    r = photos.import_paths(store, [deck])
    got = sorted(photos.get(store, i)["media"] for i in r["ids"])
    assert got == ["photo", "video"]
    vid = next(photos.get(store, i) for i in r["ids"] if photos.get(store, i)["media"] == "video")
    assert vid["source_path"].endswith("deck.pptx#ppt/media/media1.mp4")


def test_annotation_uses_contact_sheet(store, tmp_path):
    vid = photos.import_paths(store, [clip(tmp_path / "w.mp4", seconds=2)])["ids"][0]
    llm = FakeLLM({"caption_en": "Video: a nurse walks the ward."})
    out = photos.annotate_one(store, vid, llm)
    assert "THIS IS A VIDEO" in llm.prompts[0] and "0:02" in llm.prompts[0] and not out["hidden"]
    d = photos.get(store, vid)
    assert d["status"] == "annotated" and d["caption_en"].startswith("Video:") and d["faces"] == "yes"
    assert photos.search(store, "video nurse", purpose="all")["photos"][0]["id"] == vid


def test_sound_rights(store, tmp_path):
    vid = photos.import_paths(store, [clip(tmp_path / "s.mp4")], consent="unknown", usage="publication")["ids"][0]
    photos.update(store, vid, {"faces": "no"})
    d = photos.get(store, vid)
    assert (d["cleared_for"], d["sound_cleared_for"]) == ("external", "internal")       # unknown sound = speech
    assert photos.update(store, vid, {"audio": "ambient"})["sound_cleared_for"] == "external"
    photos.update(store, vid, {"audio": "speech"})
    assert photos.update(store, vid, {"consent": "yes"})["sound_cleared_for"] == "external"
    assert photos.update(store, vid, {"consent": "no"})["sound_cleared_for"] == "none"
    with pytest.raises(ValueError):
        photos.update(store, vid, {"audio": "loud"})
    pid = photos.import_paths(store, [jpeg(tmp_path / "p.jpg")])["ids"][0]
    with pytest.raises(ValueError):
        photos.update(store, pid, {"audio": "none"})
    # default (Falk, 28 Oct 2026): consent yes / publication → with sound for every purpose
    v2 = photos.get(store, photos.import_paths(store, [clip(tmp_path / "t.mp4", seconds=1)])["ids"][0])
    assert (v2["consent"], v2["sound_cleared_for"]) == ("yes", "external")


def test_files_over_http_and_muted_copy(store, tmp_path):
    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route
    m.configure(store.path)
    vid = photos.import_paths(store, [clip(tmp_path / "h.mp4")])["ids"][0]
    sha = photos._row(store, vid)["sha"]
    app = Starlette(routes=[Route("/photos/{name}", m.photo_file)])
    with TestClient(app) as c:
        r = c.get(f"/photos/{sha}.video.mp4", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "video/mp4"
        assert not photos.file_path(store, sha, "mute.mp4").exists()
        r = c.get(f"/photos/{sha}.mute.mp4")
        assert r.status_code == 200
        assert c.get(f"/photos/{sha}.sheet.jpg").status_code == 200
        assert c.get(f"/photos/{'0' * 64}.video.mp4").status_code == 404
    assert [s["codec_type"] for s in streams(photos.file_path(store, sha, "mute.mp4"))] == ["video"]   # no audio track at all


def test_mcp_search_by_medium_and_render_records_video_use(store, tmp_path, monkeypatch):
    from test_exhibits import page, png_bytes, reg, write
    from fundus import exhibits as ex
    m.configure(store.path)
    out = json.loads(m.fundus_photo_import([str(clip(tmp_path / "x.mp4")), str(jpeg(tmp_path / "y.jpg"))], annotate=False))
    assert out["imported"] == 2
    for i in out["ids"]:
        photos.annotate_one(store, i, FakeLLM({"faces_identifiable": False}))
    assert json.loads(m.fundus_photo_search("nurse"))["total"] == 1                       # default: photos only
    hits = json.loads(m.fundus_photo_search("nurse", media="video"))
    vid = hits["photos"][0]
    assert vid["ref"].startswith("video:") and vid["video_url"].endswith(".video.mp4") and vid["duration"]
    assert json.loads(m.fundus_photo_search("nurse", media="all"))["total"] == 2
    root = tmp_path / "Projects"
    monkeypatch.setattr(ex, "PROJECTS_ROOT", root)
    html = page("Ward walk", extra=f'<video src="{vid["video_url"]}" data-fundus-video="{vid["ref"]}"></video>')
    r = reg(store, write(root, "Northwind/exhibits/walk", html, png=png_bytes()))
    uses = photos.get(store, vid["id"])["uses"]
    assert len(uses) == 1 and uses[0]["exhibit_id"] == r["exhibit_id"]
    used = json.loads(m.fundus_photo(vid["ref"], action="use", exhibit="northwind/walk"))
    assert used["credit"].startswith("Video:") and used["ref"] == vid["ref"]
