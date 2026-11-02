"""Media archive (photos and videos): selected field-research photos and videos (human-centred design work) for slides.

Photos are imported from folders (JPEG, PNG, TIFF, WebP, HEIC from iPhone/iPhoto, camera RAW), stored content-addressed
under data/photos/<sha[:2]>/<sha>.<ext> and get three derived JPEGs without any metadata (GPS never leaves the import):
thumb (480 px long edge, grid), view (1600 px, lightbox) and slide (2400 px, for exhibits). A JPEG/HEIC and a RAW of the
same shot (same folder and file stem) are one photo; the RAW is kept next to it.

Annotation: the local vision LLM (gemma4:26b in Ollama, never a cloud model – the photos show people)
describes each photo: captions (de/en), alt texts, a free interpretation and keywords in fixed dimensions (DIMS: setting,
activity, role, artefact, phase, method, mood) plus Fundus industry/region slugs and free keywords. LLM keywords are
proposals; Falk's own keywords and his confirmations/rejections always win and survive a re-annotation.

Rights: consent (yes/no/unknown), usage (internal/teaching/publication) and identifiable faces decide what a session may
use (`cleared`): purpose 'external' (client decks, publications) needs usage 'publication', 'teaching' needs teaching or
publication, 'internal' only excludes refused consent; teaching and external also need no identifiable faces or consent.

Videos (media 'video', ref 'video:<id>', same table and id space as photos; files and ffmpeg in fundus.videos): imported
from the same folders and from slide decks; a playable H.264 copy, a poster frame (thumb/view/slide JPEG) and a contact
sheet of four frames, which the local vision model annotates. Same picture rights as photos, plus the sound: audio
none | ambient (no understandable speech) | speech | unknown (treated as speech). A video may be played WITH sound for a
purpose only if its picture is cleared for it and its sound has no speech or consent is 'yes' (`sound_clearance`);
otherwise only without sound (the muted copy has no audio track at all). Live Photos (.mov next to a .heic/.jpg of the
same name) stay photos.

    import_paths(store, paths, survey=…, project=…)   import files/folders (dedupe by content, RAW pairs)
    annotate_pending(store, limit) · annotate_one(store, id)   LLM annotation; embed_pending(store) for semantic search
    search(store, text, dims=…, purpose=…)              lexical + semantic, filters, rights
    listing(store, …) · get(store, id) · update · set_tags · confirm · groups · record_use · remove
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

from fundus import config as _config
from fundus.store import Store, slug as _slug

PUBLIC_URL = _config.public_url()   # $FUNDUS_PUBLIC_URL, config file, the running server, else http://127.0.0.1:8010
LLM_MODEL = os.environ.get("FUNDUS_PHOTO_MODEL", "gemma4:26b")
LLM_TIMEOUT = 300
LLM_IMAGE_EDGE = 1024           # long edge of the JPEG sent to the LLM
MAX_FILE_BYTES = 300 * 1024 * 1024
DEFAULT_CREDIT = os.environ.get("FUNDUS_PHOTO_CREDIT", "Fundus")   # photo credit when a photo names none
# Falk, 28 Oct 2026: everything he uploads is cleared for public use – new photos and videos default to consent 'yes' and
# usage 'publication'; finer privacy and rights management comes step by step (the fields stay and can be changed).
DEFAULT_CONSENT, DEFAULT_USAGE = "yes", "publication"

IMAGE_EXT = {"jpg", "jpeg", "png", "tif", "tiff", "webp", "heic", "heif", "bmp", "gif"}
RAW_EXT = {"cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf", "orf", "rw2", "pef", "srw", "x3f", "erf",
           "3fr", "mef", "mos", "iiq", "rwl"}
SIZES = {"thumb": 480, "view": 1600, "slide": 2400}
FILE_RE = re.compile(r"([0-9a-f]{64})\.(thumb|view|slide|sheet)\.jpg|([0-9a-f]{64})\.orig\.([a-z0-9]{2,5})"
                     r"|([0-9a-f]{64})\.(video|mute)\.mp4")
CONSENT = ("yes", "no", "unknown")
USAGE = ("internal", "teaching", "publication")
FACES = ("yes", "no", "unknown")
PURPOSES = ("external", "teaching", "internal", "all")
AUDIO = ("none", "ambient", "speech", "unknown")
MEDIA = ("photo", "video")

# Controlled dimensions: slug -> (German, English). The LLM picks from these; anything else becomes a free keyword.
DIMS: dict[str, dict] = {
    "setting": {"label": "Umgebung", "values": {
        "hospital-ward": ("Spitalstation", "hospital ward"), "clinic": ("Praxis, Ambulatorium", "clinic, outpatient"),
        "operating-room": ("Operationssaal", "operating room"), "care-home": ("Pflegeheim", "care home"),
        "home": ("Wohnung, Zuhause", "home"), "office": ("Büro", "office"), "factory-floor": ("Werkhalle, Produktion", "factory floor"),
        "lab": ("Labor, Werkstatt", "lab, workshop"), "warehouse": ("Lager, Logistik", "warehouse, logistics"),
        "retail-store": ("Laden", "retail store"), "showroom": ("Showroom, Autohaus", "showroom, dealership"),
        "hospitality": ("Restaurant, Hotel, Küche", "restaurant, hotel, kitchen"), "public-space": ("Öffentlicher Raum", "public space"),
        "vehicle": ("Fahrzeug, Verkehr", "vehicle, transport"), "classroom": ("Hörsaal, Schulzimmer", "classroom, lecture hall"),
        "workshop-room": ("Workshop-Raum, Kreativraum", "workshop room, studio"), "outdoor": ("Draussen, Feld", "outdoors, field"),
        "farm": ("Landwirtschaft", "farm"), "event": ("Veranstaltung, Messe", "event, fair")}},
    "activity": {"label": "Tätigkeit", "values": {
        "observation": ("Beobachtung", "observation"), "interview": ("Interview, Gespräch", "interview, conversation"),
        "workshop": ("Workshop", "workshop"), "co-creation": ("Co-Creation", "co-creation"), "prototyping": ("Prototypenbau", "prototyping"),
        "user-test": ("Test mit Nutzenden", "user test"), "presentation": ("Präsentation", "presentation"),
        "routine-work": ("Arbeitsalltag", "routine work"), "workaround": ("Workaround, Behelfslösung", "workaround"),
        "collaboration": ("Zusammenarbeit", "collaboration"), "waiting": ("Warten", "waiting"), "handover": ("Übergabe", "handover"),
        "documentation": ("Dokumentation, Schreibarbeit", "documentation, paperwork"), "training": ("Schulung, Lernen", "training, learning"),
        "purchase": ("Kauf, Beratung", "purchase, advice"), "care": ("Pflege, Behandlung", "care, treatment"),
        "maintenance": ("Wartung, Reparatur", "maintenance, repair"), "sorting-clustering": ("Sortieren, Clustern", "sorting, clustering")}},
    "role": {"label": "Rollen", "values": {
        "patient": ("Patient:in", "patient"), "relative": ("Angehörige", "relative"), "nurse": ("Pflegende", "nurse"),
        "physician": ("Ärztin, Arzt", "physician"), "therapist": ("Therapeut:in", "therapist"), "worker": ("Mitarbeitende", "worker, employee"),
        "technician": ("Techniker:in", "technician"), "engineer": ("Ingenieur:in", "engineer"), "manager": ("Führungskraft", "manager"),
        "customer": ("Kund:in", "customer"), "sales-staff": ("Verkauf, Beratung", "sales staff"), "student": ("Studierende", "student"),
        "researcher": ("Forschende", "researcher"), "designer": ("Designer:in", "designer"), "facilitator": ("Moderation", "facilitator"),
        "citizen": ("Bürger:in, Passant:in", "citizen, passer-by"), "child": ("Kind", "child"), "senior": ("ältere Person", "older person")}},
    "artefact": {"label": "Artefakte", "values": {
        "post-its": ("Post-its", "sticky notes"), "whiteboard": ("Whiteboard, Flipchart", "whiteboard, flip chart"),
        "prototype": ("Prototyp", "prototype"), "paper-prototype": ("Papierprototyp", "paper prototype"),
        "persona": ("Persona", "persona"), "journey-map": ("Journey Map", "journey map"), "sketch": ("Skizze", "sketch"),
        "form": ("Formular, Papier", "form, paperwork"), "screen": ("Bildschirm, Software", "screen, software"),
        "smartphone": ("Smartphone, Tablet", "smartphone, tablet"), "machine": ("Maschine, Anlage", "machine"),
        "tool": ("Werkzeug", "tool"), "medical-device": ("Medizingerät", "medical device"), "product": ("Produkt", "product"),
        "packaging": ("Verpackung", "packaging"), "signage": ("Beschilderung, Aushang", "signage, notice"),
        "workaround-object": ("Behelfsobjekt", "improvised object"), "vehicle": ("Fahrzeug", "vehicle"),
        "furniture": ("Möbel, Raum", "furniture, space"), "lego-model": ("Lego, Modell", "Lego, model")}},
    "phase": {"label": "HCD-Phase", "values": {
        "understand": ("Verstehen", "understand / empathise"), "define": ("Definieren", "define"), "ideate": ("Ideen finden", "ideate"),
        "prototype": ("Prototyp", "prototype"), "test": ("Testen", "test"), "implement": ("Umsetzen", "implement")}},
    "method": {"label": "Methode", "values": {
        "contextual-inquiry": ("Contextual Inquiry", "contextual inquiry"), "shadowing": ("Shadowing", "shadowing"),
        "interview": ("Interview", "interview"), "co-creation": ("Co-Creation", "co-creation"), "design-workshop": ("Design-Workshop", "design workshop"),
        "usability-test": ("Usability-Test", "usability test"), "diary-study": ("Tagebuchstudie", "diary study"),
        "cultural-probe": ("Cultural Probe", "cultural probe"), "service-safari": ("Service Safari", "service safari"),
        "bodystorming": ("Bodystorming", "bodystorming"), "role-play": ("Rollenspiel", "role play"), "card-sorting": ("Card Sorting", "card sorting"),
        "brainstorming": ("Brainstorming", "brainstorming"), "storyboard": ("Storyboard", "storyboard")}},
    "mood": {"label": "Stimmung", "values": {
        "focused": ("konzentriert", "focused"), "improvised": ("improvisiert", "improvised"), "crowded": ("beengt, voll", "crowded"),
        "calm": ("ruhig", "calm"), "stressful": ("hektisch, belastend", "stressful"), "collaborative": ("kollaborativ", "collaborative"),
        "playful": ("spielerisch", "playful"), "tired": ("erschöpft", "tired"), "frustrated": ("frustriert", "frustrated"),
        "joyful": ("fröhlich", "joyful"), "formal": ("formell", "formal"), "informal": ("informell", "informal"),
        "clinical": ("klinisch, steril", "clinical, sterile"), "messy": ("unordentlich", "messy")}},
}
OPEN_DIMS = ("industry", "region", "keyword")   # Fundus vocabulary (industry, region) and free keywords
ALL_DIMS = tuple(DIMS) + OPEN_DIMS


# ------------------------------------------------------------------ files
def files_dir(store: Store) -> Path:
    return Path(os.environ.get("FUNDUS_PHOTO_DIR") or (store.path.parent / "photos"))


def file_path(store: Store, sha: str, name: str) -> Path:
    """name: 'thumb.jpg' | 'view.jpg' | 'slide.jpg' | 'orig.<ext>'; videos also 'video.mp4' | 'mute.mp4' | 'sheet.jpg'."""
    return files_dir(store) / sha[:2] / f"{sha}.{name}"


def url(sha: str, size: str) -> str:
    return f"{PUBLIC_URL}/photos/{sha}.{size}.{'mp4' if size in ('video', 'mute') else 'jpg'}"


def ref_of(p) -> str:
    return f"{p['media'] if 'media' in p.keys() and p['media'] else 'photo'}:{p['id']}"


def _write(path: Path, data: bytes) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def resolve_file(store: Store, name: str) -> tuple[Path, str, str]:
    """Requested file name -> (path, content type, download name or ''). Hash names only; the photo must exist."""
    m = FILE_RE.fullmatch(name or "")
    if not m:
        raise ValueError("bad file name (expected <sha256>.thumb.jpg|view.jpg|slide.jpg|orig.<ext>)")
    if m.group(1):
        sha, part = m.group(1), f"{m.group(2)}.jpg"
        row = store.db.execute("SELECT id, orig_name FROM photo WHERE sha = ?", (sha,)).fetchone()
        ctype, dl = "image/jpeg", ""
    elif m.group(5):
        sha, part = m.group(5), f"{m.group(6)}.mp4"
        row = store.db.execute("SELECT id, orig_name FROM photo WHERE sha = ? AND media = 'video'", (sha,)).fetchone()
        ctype, dl = "video/mp4", ""
    else:
        sha, ext = m.group(3), m.group(4)
        part = f"orig.{ext}"
        row = store.db.execute("SELECT id, orig_name FROM photo WHERE (sha = ? AND ext = ?) OR (raw_sha = ? AND raw_ext = ?)",
                               (sha, ext, sha, ext)).fetchone()
        ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif",
                 "tif": "image/tiff", "tiff": "image/tiff", "heic": "image/heic", "heif": "image/heif",
                 "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}.get(ext, "application/octet-stream")
        dl = f"{Path(row['orig_name']).stem if row and row['orig_name'] else sha[:12]}.{ext}" if row else ""
    if not row:
        raise LookupError("not found")
    p = file_path(store, sha, part)
    if part == "mute.mp4" and file_path(store, sha, "video.mp4").is_file():
        return p, ctype, dl                     # may not exist yet: ensure_mute() makes it
    if not p.is_file():
        raise LookupError("not found")
    return p, ctype, dl


def ensure_mute(store: Store, sha: str) -> Path:
    """The copy of a video without sound (made on first request; seconds – the video track is only copied)."""
    from fundus import videos
    return videos.mute(file_path(store, sha, "video.mp4"), file_path(store, sha, "mute.mp4"))


# ------------------------------------------------------------------ reading images
_heif_ready = False


def _open(path: Path, data: bytes):
    """PIL image (upright, RGB) and its EXIF (dict-like or None) from any supported file."""
    global _heif_ready
    from PIL import Image, ImageOps
    ext = path.suffix.lower().lstrip(".")
    if ext in RAW_EXT:
        import rawpy
        with rawpy.imread(io.BytesIO(data)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
        exif = None
        try:                                          # many RAW formats are TIFF-based: PIL reads the EXIF
            with Image.open(io.BytesIO(data)) as probe:
                exif = probe.getexif()
        except Exception:
            pass
        return Image.fromarray(rgb), exif
    if ext in ("heic", "heif") and not _heif_ready:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _heif_ready = True
    im = Image.open(io.BytesIO(data))
    exif = im.getexif()
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB",):
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    return im, exif


def _exif_info(exif) -> dict:
    out = {}
    if not exif:
        return out
    try:
        sub = exif.get_ifd(0x8769)
    except Exception:
        sub = {}
    when = sub.get(36867) or sub.get(36868) or exif.get(306)
    if when:
        m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(when))
        if m and m.group(1) != "0000":
            out["taken_at"] = "{}-{}-{} {}:{}:{}".format(*m.groups())
    cam = " ".join(str(exif.get(t) or "").strip("\x00 ") for t in (271, 272)).strip()
    if cam:
        make = str(exif.get(271) or "").strip("\x00 ")
        model = str(exif.get(272) or "").strip("\x00 ")
        out["camera"] = model if make and model.lower().startswith(make.lower().split()[0]) else cam
    return out


def _resized(im, edge: int):
    from PIL import Image
    w, h = im.size
    if max(w, h) <= edge:
        return im
    f = edge / max(w, h)
    return im.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)


def _jpeg(im, quality: int = 86) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality, optimize=True, progressive=True)   # no exif= → no metadata, no GPS
    return b.getvalue()


def dhash(im) -> str:
    g = im.convert("L").resize((9, 8))
    px = list(g.tobytes())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{bits:016x}"


def hamming(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def orientation(w: int, h: int) -> str:
    r = w / h if h else 1
    return "square" if 0.95 <= r <= 1.05 else ("landscape" if r > 1 else "portrait")


# ------------------------------------------------------------------ groups
def group_id(store: Store, ref: str | int | None, kind: str = "survey", create: dict | None = None) -> int | None:
    """Group by id or slug or title; with create={title, project, period, place, description} it is created if missing."""
    if ref in (None, ""):
        return None
    if isinstance(ref, int) or str(ref).isdigit():
        row = store.db.execute("SELECT id FROM photo_group WHERE id = ?", (int(ref),)).fetchone()
        if not row:
            raise LookupError(f"photo group {ref} not found")
        return row[0]
    s = _slug(str(ref))
    row = store.db.execute("SELECT id FROM photo_group WHERE slug = ? OR title = ?", (s, str(ref))).fetchone()
    if row:
        return row[0]
    if create is None:
        raise LookupError(f"photo group '{ref}' not found")
    return create_group(store, kind, str(ref), **create)["id"]


def create_group(store: Store, kind: str, title: str, project: str | None = None, period: str | None = None,
                 place: str | None = None, description: str | None = None) -> dict:
    if kind not in ("survey", "album"):
        raise ValueError("kind must be 'survey' (field study / import) or 'album'")
    title = (title or "").strip()
    if not title:
        raise ValueError("a group needs a title")
    base = _slug(title) or kind
    s, n = base, 2
    while store.db.execute("SELECT 1 FROM photo_group WHERE slug = ?", (s,)).fetchone():
        s, n = f"{base}-{n}", n + 1
    cur = store.db.execute("INSERT INTO photo_group(kind, slug, title, project, period, place, description) VALUES (?,?,?,?,?,?,?)",
                           (kind, s, title, project, period, place, description))
    store.db.commit()
    return {"id": cur.lastrowid, "slug": s, "kind": kind, "title": title}


def update_group(store: Store, gid: int, **changes) -> dict:
    allowed = {k: v for k, v in changes.items() if k in ("title", "project", "period", "place", "description", "kind")}
    if "kind" in allowed and allowed["kind"] not in ("survey", "album"):
        raise ValueError("kind must be survey or album")
    if allowed:
        store.db.execute(f"UPDATE photo_group SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), gid))
        store.db.commit()
    return groups(store, gid=gid)[0]


def delete_group(store: Store, gid: int) -> dict:
    """Removes the group, never the photos."""
    n = store.db.execute("DELETE FROM photo_group WHERE id = ?", (gid,)).rowcount
    store.db.commit()
    return {"deleted": n}


def add_to_group(store: Store, gid: int, ids: list[int]) -> dict:
    store.db.executemany("INSERT OR IGNORE INTO photo_group_item(group_id, photo_id) VALUES (?, ?)", [(gid, int(i)) for i in ids])
    store.db.commit()
    return {"group": gid, "added": len(ids)}


def remove_from_group(store: Store, gid: int, ids: list[int]) -> dict:
    store.db.executemany("DELETE FROM photo_group_item WHERE group_id = ? AND photo_id = ?", [(gid, int(i)) for i in ids])
    store.db.commit()
    return {"group": gid, "removed": len(ids)}


def groups(store: Store, gid: int | None = None) -> list[dict]:
    q = ("SELECT g.*, (SELECT COUNT(*) FROM photo_group_item i JOIN photo p ON p.id = i.photo_id "
         "WHERE i.group_id = g.id AND p.removed_at IS NULL) AS n, "
         "(SELECT p.sha FROM photo_group_item i JOIN photo p ON p.id = i.photo_id WHERE i.group_id = g.id AND p.removed_at IS NULL "
         "ORDER BY p.taken_at LIMIT 1) AS cover FROM photo_group g")
    rows = store.db.execute(q + (" WHERE g.id = ?" if gid else " ORDER BY g.kind DESC, g.title"), (gid,) if gid else ()).fetchall()
    if gid and not rows:
        raise LookupError(f"photo group {gid} not found")
    return [{**{k: r[k] for k in ("id", "kind", "slug", "title", "project", "period", "place", "description", "n")},
             "cover_url": url(r["cover"], "thumb") if r["cover"] else None} for r in rows]


# ------------------------------------------------------------------ import
CONTAINER_EXT = {"pptx", "potx", "docx", "key", "pdf"}   # documents whose embedded pictures are imported
MIN_EMBEDDED_EDGE = 400        # embedded pictures smaller than this (icons, logos, bullets) are skipped


def _expand(doc: Path, work: Path) -> list[tuple[Path, str]]:
    """Pictures embedded in a slide deck or document (PowerPoint/Word/Keynote media folder, PDF images), extracted into
    work/; returns (file, 'deck.pptx#media/image3.jpeg'). Small pictures (icons, logos) are left out."""
    import subprocess
    import zipfile
    from PIL import Image
    out = []
    work.mkdir(parents=True, exist_ok=True)
    if doc.suffix.lower() == ".pdf":
        subprocess.run(["pdfimages", "-all", str(doc), str(work / "img")], check=True, capture_output=True, timeout=600)
        cands = sorted(work.glob("img-*"))
        label = lambda f: f"{doc}#{f.name}"
    else:
        with zipfile.ZipFile(doc) as z:
            from fundus.videos import VIDEO_EXT
            names = [n for n in z.namelist() if re.search(r"(^|/)(media|Data)/[^/]+$", n)
                     and n.rsplit(".", 1)[-1].lower() in IMAGE_EXT | VIDEO_EXT]
            cands = []
            for i, n in enumerate(names):
                f = work / f"{i:04d}-{Path(n).name}"
                f.write_bytes(z.read(n))
                cands.append(f)
            mapping = dict(zip(cands, names))
        label = lambda f: f"{doc}#{mapping[f]}"
    for f in cands:
        if f.suffix.lower().lstrip(".") not in IMAGE_EXT:      # videos from the deck: always (probed at import)
            out.append((f, label(f)))
            continue
        try:
            with Image.open(f) as im:
                if max(im.size) < MIN_EMBEDDED_EDGE:
                    continue
        except Exception:
            if f.suffix.lower().lstrip(".") not in ("heic", "heif"):
                continue
        if f.suffix.lower().lstrip(".") in IMAGE_EXT:
            out.append((f, label(f)))
    return out


def _collect(store: Store, paths: list[str | Path], recursive: bool = True) -> list[tuple[Path, str]]:
    """(file, source label) for all image files under paths; slide decks/documents contribute their embedded pictures."""
    files = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files += sorted(f for f in it if f.is_file() and not f.name.startswith("."))
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(f"not found: {p}")
    from fundus.videos import VIDEO_EXT
    out = []
    for f in files:
        ext = f.suffix.lower().lstrip(".")
        if ext in IMAGE_EXT | RAW_EXT | VIDEO_EXT:
            out.append((f, str(f)))
        elif ext in CONTAINER_EXT:
            work = files_dir(store) / "incoming" / hashlib.sha1(str(f.resolve()).encode()).hexdigest()[:16]
            out += _expand(f, work)
    return out


def import_paths(store: Store, paths: list[str | Path], survey: str | None = None, project: str | None = None,
                 period: str | None = None, place: str | None = None, consent: str = DEFAULT_CONSENT, usage: str = DEFAULT_USAGE,
                 credit: str | None = None, album: str | None = None, recursive: bool = True,
                 title: str | None = None, note: str | None = None, progress=None) -> dict:
    """Import image files and folders. Identical files are skipped (content hash); a JPEG/HEIC and a RAW with the same
    stem in one folder become one photo. survey: field study the photos come from (created if new, with project, period,
    place). consent/usage: defaults for all new photos (change per photo later). Slide decks and documents (.pptx, .docx,
    .key, .pdf) contribute their embedded pictures (not icons/logos below 400 px; the LLM hides graphics that are not
    photos). title/note: for all new photos (e.g. where they come from). Returns counts and the new ids."""
    if consent not in CONSENT or usage not in USAGE:
        raise ValueError(f"consent in {CONSENT}, usage in {USAGE}")
    from fundus.videos import VIDEO_EXT
    files = _collect(store, paths, recursive)
    shots: dict[tuple[str, str], dict] = {}
    for f, src in files:
        key = (str(f.parent), f.stem.lower())
        ext = f.suffix.lower().lstrip(".")
        kind = "raw" if ext in RAW_EXT else "video" if ext in VIDEO_EXT else "img"
        shots.setdefault(key, {}).setdefault(kind, f)
        shots[key].setdefault("src", src)
    gid = group_id(store, survey, "survey", create={"project": project, "period": period, "place": place}) if survey else None
    aid = group_id(store, album, "album", create={"project": project}) if album else None
    res = {"files": len(files), "imported": 0, "duplicates": 0, "failed": [], "ids": [], "survey": gid}
    for n, shot in enumerate(shots.values(), 1):
        main = shot.get("img") or shot.get("raw") or shot.get("video")   # a .mov next to a photo = Live Photo: the photo
        raw = shot.get("raw") if shot.get("img") else None
        try:
            if main is shot.get("video"):
                pid, new = _import_video(store, main, project=project, place=place, consent=consent, usage=usage,
                                         credit=credit, source=shot["src"], title=title, note=note)
            else:
                pid, new = _import_one(store, main, raw, project=project, place=place, consent=consent, usage=usage,
                                       credit=credit, source=shot["src"],
                                       title=title, note=note)
        except Exception as e:      # one broken file must not stop an import of hundreds
            res["failed"].append({"file": str(main), "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        for g in (gid, aid):
            if g:
                store.db.execute("INSERT OR IGNORE INTO photo_group_item(group_id, photo_id) VALUES (?, ?)", (g, pid))
        store.db.commit()
        res["imported" if new else "duplicates"] += 1
        res["ids"].append(pid)
        if progress:
            progress(n, len(shots), main)
    return res


def _import_one(store: Store, main: Path, raw: Path | None, **meta) -> tuple[int, bool]:
    if main.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"file larger than {MAX_FILE_BYTES // 2**20} MB")
    data = main.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    row = store.db.execute("SELECT id FROM photo WHERE sha = ? OR raw_sha = ?", (sha, sha)).fetchone()
    if row:
        if row[0] and store.db.execute("SELECT removed_at FROM photo WHERE id = ?", (row[0],)).fetchone()[0]:
            store.db.execute("UPDATE photo SET removed_at = NULL WHERE id = ?", (row[0],))   # imported again: back
        return row[0], False
    ext = main.suffix.lower().lstrip(".")
    ext = "jpg" if ext == "jpeg" else ext
    im, exif = _open(main, data)
    info = _exif_info(exif)
    raw_sha = raw_ext = None
    if raw is not None:
        rdata = raw.read_bytes()
        raw_sha, raw_ext = hashlib.sha256(rdata).hexdigest(), raw.suffix.lower().lstrip(".")
        _write(file_path(store, raw_sha, f"orig.{raw_ext}"), rdata)
        if not info.get("taken_at"):
            try:
                info = {**_exif_info(_open(raw, rdata)[1]), **info}
            except Exception:
                pass
    _write(file_path(store, sha, f"orig.{ext}"), data)
    for size, edge in SIZES.items():
        _write(file_path(store, sha, f"{size}.jpg"), _jpeg(_resized(im, edge), 82 if size == "thumb" else 86))
    w, h = im.size
    taken = info.get("taken_at") or datetime.fromtimestamp(main.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    cur = store.db.execute(
        "INSERT INTO photo(sha, ext, raw_sha, raw_ext, orig_name, source_path, width, height, orientation, taken_at, camera,"
        " place, dhash, consent, usage, credit, title, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha, ext, raw_sha, raw_ext, main.name, meta.get("source") or str(main), w, h, orientation(w, h), taken, info.get("camera"),
         meta.get("place"), dhash(im), meta["consent"], meta["usage"], meta.get("credit"), meta.get("title"), meta.get("note")))
    return cur.lastrowid, True


def _import_video(store: Store, main: Path, **meta) -> tuple[int, bool]:
    """One video: original, playable copy, poster JPEGs and contact sheet; probe data into the row."""
    from fundus import videos
    if not videos.available():
        raise RuntimeError("ffmpeg/ffprobe not installed (needed for videos)")
    if main.stat().st_size > videos.MAX_VIDEO_BYTES:
        raise ValueError(f"video larger than {videos.MAX_VIDEO_BYTES // 2**30} GB")
    sha = videos.sha_file(main)
    row = store.db.execute("SELECT id, removed_at FROM photo WHERE sha = ?", (sha,)).fetchone()
    if row:
        if row["removed_at"]:
            store.db.execute("UPDATE photo SET removed_at = NULL WHERE id = ?", (row["id"],))
        return row["id"], False
    info = videos.probe(main)
    ext = main.suffix.lower().lstrip(".")
    orig = file_path(store, sha, f"orig.{ext}")
    if not orig.exists():
        orig.parent.mkdir(parents=True, exist_ok=True)
        tmp = orig.with_name(orig.name + f".tmp{os.getpid()}")
        shutil.copyfile(main, tmp)
        os.chmod(tmp, 0o644)
        tmp.replace(orig)
    video = videos.transcode(orig, file_path(store, sha, "video.mp4"), sound=True)
    im = videos.frame(video, videos.poster_time(info["duration"]))
    for size, edge in SIZES.items():
        _write(file_path(store, sha, f"{size}.jpg"), _jpeg(_resized(im, edge), 82 if size == "thumb" else 86))
    _write(file_path(store, sha, "sheet.jpg"), _jpeg(videos.contact_sheet(video, info["duration"]), 85))
    w, h = info["width"] or im.width, info["height"] or im.height
    taken = _iso_local(info.get("created")) or datetime.fromtimestamp(main.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    cur = store.db.execute(
        "INSERT INTO photo(sha, ext, orig_name, source_path, width, height, orientation, taken_at, camera, place, dhash,"
        " consent, usage, credit, title, note, media, duration, fps, audio) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'video',?,?,?)",
        (sha, ext, main.name, meta.get("source") or str(main), w, h, orientation(w, h), taken, info.get("camera"),
         meta.get("place"), dhash(im), meta["consent"], meta["usage"], meta.get("credit"), meta.get("title"), meta.get("note"),
         info["duration"], info["fps"], "unknown" if info["has_audio"] else "none"))
    return cur.lastrowid, True


def _iso_local(v: str | None) -> str | None:
    """'2025-05-14T10:20:00.000000Z' / '2025-05-14T12:20:00+0200' -> local 'YYYY-MM-DD HH:MM:SS'."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T"))
        return (d.astimezone() if d.tzinfo else d).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", v)
        return f"{m.group(1)} {m.group(2)}" if m else None


# ------------------------------------------------------------------ annotation (local vision LLM)
class OllamaVision:
    """Ollama /api/chat with one image, JSON answer; respects an optional FUNDUS_MAX_MODELS limit (fundus.embed)."""

    def __init__(self, model: str = LLM_MODEL, url: str | None = None):
        from fundus import embed
        self.model = model
        self.url = (url or embed.OLLAMA_URL).rstrip("/")

    def check_capacity(self) -> None:
        from fundus import embed
        if embed.MAX_LOADED_MODELS <= 0:
            return
        loaded = embed.OllamaEmbedder(url=self.url).loaded_models()
        if not embed.capacity_ok(loaded, self.model):
            raise embed.EmbedUnavailable(f"ollama busy: {', '.join(loaded)} loaded; not loading {self.model} (FUNDUS_MAX_MODELS={embed.MAX_LOADED_MODELS})")

    def describe(self, prompt: str, jpeg: bytes) -> dict:
        from fundus.entities import LLM_KEEP_ALIVE, LLM_NUM_CTX
        body = {"model": self.model, "format": "json", "stream": False, "think": False, "keep_alive": LLM_KEEP_ALIVE,
                "options": {"temperature": 0, "num_ctx": LLM_NUM_CTX, "num_predict": 2048},   # same context as the warm
                # gemma4 (no second copy); the cap ends a looping answer (a description is ~600 tokens)
                "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(jpeg).decode()]}]}
        req = urllib.request.Request(self.url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            data = json.loads(r.read())
        return json.loads(data["message"]["content"])


_llm_override = None


def set_llm(llm) -> None:
    """Tests: replace the vision LLM (object with describe(prompt, jpeg) -> dict); None restores Ollama."""
    global _llm_override
    _llm_override = llm


def _llm():
    return _llm_override if _llm_override is not None else OllamaVision()


def _vocab_lines(store: Store) -> str:
    lines = [f"{d}: {', '.join(spec['values'])}" for d, spec in DIMS.items()]
    for facet in ("industry", "region"):
        vals = sorted(v for f, v in store.vocab_values() if f == facet)
        if facet == "region":
            vals = [v for v in vals if len(v) > 2 or v in ("ch", "de", "at", "us", "uk", "cn", "jp", "in", "fr", "it")]
        lines.append(f"{facet}: {', '.join(vals)}")
    return "\n".join(lines)


PROMPT = """You annotate a photo for a research photo archive of human-centred design field work (observations, interviews,
workshops, prototypes, work places in hospitals, factories, shops, homes). The archive is searched when slides need photos.
Describe only what is visible; never guess names or identities of people. Roles are what people visibly do.

Context given by the photographer (trust it over your guesses):
{context}

Answer with one JSON object:
{{"caption_en": "2-3 factual sentences", "caption_de": "the same in German",
 "alt_en": "alt text, max 120 characters", "alt_de": "Alternativtext, höchstens 120 Zeichen",
 "interpretation": "3-5 sentences in English: what the scene says about the people, their work and needs, tensions, workarounds and design opportunities - a researcher's reading, marked as interpretation",
 "setting": [], "activity": [], "role": [], "artefact": [], "phase": [], "method": [], "mood": [], "industry": [], "region": [],
 "keywords": ["5-12 short English keywords not covered above"],
 "people_count": 0, "faces_identifiable": false, "text_visible": ["readable text in the image"],
 "copy_space": "none|left|right|top|bottom (calm area for slide text)", "quality": "good|ok|poor",
 "kind": "photo|illustration|diagram|screenshot|logo|other (photo = a real camera photograph)"}}
For the list fields use ONLY these values (zero to three per field, most fitting first; empty if nothing fits; region only if clearly recognisable):
{vocab}"""

VIDEO_NOTE = """THIS IS A VIDEO, not a single photo: the image is a contact sheet of four frames of one video ({duration} long), in
time order left to right, top to bottom. Describe the video as a whole – the scene and what happens over time – never the
grid itself. Captions start with "Video:". people_count = the most people visible in one frame; faces_identifiable = true
if any frame shows an identifiable face; kind = "photo" for real camera footage, "screenshot" for a screen recording,
"illustration" for an animation.

"""


def _context(store: Store, p) -> str:
    parts = []
    for g in store.db.execute("SELECT g.kind, g.title, g.project, g.period, g.place, g.description FROM photo_group g "
                              "JOIN photo_group_item i ON i.group_id = g.id WHERE i.photo_id = ?", (p["id"],)):
        parts.append(f"{'Field study' if g['kind'] == 'survey' else 'Album'}: {g['title']}"
                     + "".join(f"; {k}: {g[k]}" for k in ("project", "period", "place", "description") if g[k]))
    for k, lab in (("title", "Title"), ("note", "Photographer's note"), ("place", "Place"), ("taken_at", "Taken")):
        if p[k]:
            parts.append(f"{lab}: {p[k]}")
    own = [f"{t['dim']}={t['value']}" for t in store.db.execute(
        "SELECT dim, value FROM photo_tag WHERE photo_id = ? AND source = 'own' AND state = 'confirmed'", (p["id"],))]
    if own:
        parts.append("Keywords set by the photographer: " + ", ".join(own))
    return "\n".join(parts) or "(none)"


def annotate_one(store: Store, pid: int, llm=None) -> dict:
    """Annotate one photo with the vision LLM. The LLM call runs without holding anything; writing is one short step."""
    p = store.db.execute("SELECT * FROM photo WHERE id = ?", (pid,)).fetchone()
    if not p:
        raise LookupError(f"photo {pid} not found")
    from PIL import Image
    is_video = p["media"] == "video"
    with Image.open(file_path(store, p["sha"], "sheet.jpg" if is_video else "view.jpg")) as im:
        jpeg = _jpeg(_resized(im.convert("RGB"), LLM_IMAGE_EDGE), 85)
    prompt = PROMPT.format(context=_context(store, p), vocab=_vocab_lines(store))
    if is_video:
        from fundus.videos import duration_label
        prompt = VIDEO_NOTE.format(duration=duration_label(p["duration"]) or "?") + prompt
    llm = llm or _llm()
    try:
        ans = llm.describe(prompt, jpeg)
        if not isinstance(ans, dict):
            raise ValueError("answer is not a JSON object")
    except Exception as e:
        store.db.execute("UPDATE photo SET status = CASE WHEN status = 'new' THEN 'failed' ELSE status END, error = ?,"
                         " updated_at = CURRENT_TIMESTAMP WHERE id = ?", (f"{type(e).__name__}: {e}"[:300], pid))
        store.db.commit()
        raise
    return apply_annotation(store, pid, ans, getattr(llm, "model", "llm"))


def _clean(v, n: int = 600) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v[:n] or None


def _norm_value(v: str) -> str:
    return _slug(str(v))[:60]


def apply_annotation(store: Store, pid: int, ans: dict, model: str) -> dict:
    known = store.vocab_values()
    tags: list[tuple[str, str]] = []
    for d, spec in DIMS.items():
        for v in (ans.get(d) or [])[:3] if isinstance(ans.get(d), list) else []:
            s = _norm_value(v)
            tags.append((d, s) if s in spec["values"] else ("keyword", s))
    for facet in ("industry", "region"):
        for v in (ans.get(facet) or [])[:3] if isinstance(ans.get(facet), list) else []:
            s = _norm_value(v)
            if (facet, s) in known:
                tags.append((facet, s))
    for v in (ans.get("keywords") or [])[:15] if isinstance(ans.get("keywords"), list) else []:
        s = _norm_value(v)
        if s:
            tags.append(("keyword", s))
    tags = list(dict.fromkeys(t for t in tags if t[1]))
    p = store.db.execute("SELECT faces, annotation FROM photo WHERE id = ?", (pid,)).fetchone()
    prev = json.loads(p["annotation"] or "{}")
    faces = p["faces"]
    if prev.get("faces_by") != "own" and isinstance(ans.get("faces_identifiable"), bool):
        faces = "yes" if ans["faces_identifiable"] else "no"
    people = ans.get("people_count") if isinstance(ans.get("people_count"), int) and ans["people_count"] >= 0 else None
    extra = {"model": model, "at": datetime.now().isoformat(timespec="seconds"), "faces_by": prev.get("faces_by", "llm"),
             "text_visible": [t for t in (ans.get("text_visible") or []) if isinstance(t, str)][:10]
             if isinstance(ans.get("text_visible"), list) else [],
             "copy_space": ans.get("copy_space") if ans.get("copy_space") in ("none", "left", "right", "top", "bottom") else None,
             "quality": ans.get("quality") if ans.get("quality") in ("good", "ok", "poor") else None,
             "kind": ans.get("kind") if ans.get("kind") in ("photo", "illustration", "diagram", "screenshot", "logo", "other") else None}
    src = store.db.execute("SELECT source_path FROM photo WHERE id = ?", (pid,)).fetchone()[0] or ""
    # pictures taken out of slide decks are often charts, logos or screenshots: those are hidden, not archived as photos
    not_photo = "#" in src and extra["kind"] not in (None, "photo")
    with store.db:
        store.db.execute("DELETE FROM photo_tag WHERE photo_id = ? AND source = 'llm' AND state = 'proposed'", (pid,))
        store.db.executemany("INSERT OR IGNORE INTO photo_tag(photo_id, dim, value, source, state) VALUES (?, ?, ?, 'llm', 'proposed')",
                             [(pid, d, v) for d, v in tags])
        store.db.execute(
            "UPDATE photo SET caption_de = ?, caption_en = ?, alt_de = ?, alt_en = ?, interpretation = ?, people_count = ?,"
            " faces = ?, annotation = ?, annotated_at = CURRENT_TIMESTAMP, error = NULL,"
            " status = CASE WHEN status = 'confirmed' THEN 'confirmed' ELSE 'annotated' END, updated_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (_clean(ans.get("caption_de")), _clean(ans.get("caption_en")), _clean(ans.get("alt_de"), 160),
             _clean(ans.get("alt_en"), 160), _clean(ans.get("interpretation"), 1500), people, faces, json.dumps(extra), pid))
        if not_photo:
            store.db.execute("UPDATE photo SET removed_at = CURRENT_TIMESTAMP, error = ? WHERE id = ?",
                             (f"kein Foto ({extra['kind']}) – aus einem Foliensatz, ausgeblendet", pid))
    store.db.execute("DELETE FROM photo_vec WHERE photo_id = ?", (pid,))
    store.db.commit()
    return {"id": pid, "tags": len(tags), "faces": faces, "people_count": people, "hidden": not_photo}


def pending(store: Store, retry_failed: bool = False) -> list[int]:
    states = ("new", "failed") if retry_failed else ("new",)
    return [r[0] for r in store.db.execute(
        f"SELECT id FROM photo WHERE removed_at IS NULL AND status IN ({','.join('?' * len(states))}) ORDER BY id", states)]


def annotate_pending(store: Store, limit: int | None = None, retry_failed: bool = False, llm=None, progress=None) -> dict:
    llm = llm or _llm()
    if hasattr(llm, "check_capacity"):
        llm.check_capacity()
    ids = pending(store, retry_failed)[:limit] if limit else pending(store, retry_failed)
    done, failed = 0, []
    for n, pid in enumerate(ids, 1):
        try:
            annotate_one(store, pid, llm)
            done += 1
        except Exception as e:
            failed.append({"id": pid, "error": f"{type(e).__name__}: {e}"[:200]})
        if progress:
            progress(n, len(ids), pid)
    emb = embed_pending(store)
    return {"annotated": done, "failed": failed, "left": len(pending(store)), "embedded": emb}


# ------------------------------------------------------------------ texts and semantic search
def _tag_rows(store: Store, ids: list[int]) -> dict[int, list]:
    out: dict[int, list] = {i: [] for i in ids}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in store.db.execute(f"SELECT photo_id, dim, value, source, state FROM photo_tag WHERE photo_id IN "
                                  f"({','.join('?' * len(chunk))}) ORDER BY dim, source DESC, value", chunk):
            out[r["photo_id"]].append(r)
    return out


def label(dim: str, value: str, lang: str = "de") -> str:
    spec = DIMS.get(dim)
    if spec and value in spec["values"]:
        return spec["values"][value][0 if lang == "de" else 1]
    return value.replace("-", " ")


def photo_text(store: Store, p, tags: list) -> str:
    """Text for search and embedding: title, note, captions, interpretation, keywords (both languages), groups."""
    words = [label(t["dim"], t["value"], "en") + " / " + label(t["dim"], t["value"], "de")
             for t in tags if t["state"] != "rejected"]
    grp = [r[0] for r in store.db.execute("SELECT g.title FROM photo_group g JOIN photo_group_item i ON i.group_id = g.id "
                                          "WHERE i.photo_id = ?", (p["id"],))]
    return "\n".join(x for x in ("Video" if p["media"] == "video" else None, p["title"], p["note"], p["caption_en"],
                                 p["caption_de"], p["interpretation"], "; ".join(words), "; ".join(grp), p["place"]) if x)


def _fold(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").casefold()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t)


def embed_pending(store: Store, embedder=None) -> int:
    """Embed descriptions of annotated photos without a current vector (needs the Ollama embedding model; skipped if busy)."""
    import numpy as np
    from fundus import embed
    space = embed.active_space(store) or embed.default_space()
    try:
        emb = embedder or embed.embedder_for(space)
        if hasattr(emb, "check_capacity"):
            emb.check_capacity()
    except Exception:
        return 0
    rows = store.db.execute("SELECT p.* FROM photo p LEFT JOIN photo_vec v ON v.photo_id = p.id "
                            "WHERE p.removed_at IS NULL AND p.status IN ('annotated','confirmed') AND v.photo_id IS NULL").fetchall()
    if not rows:
        return 0
    tags = _tag_rows(store, [r["id"] for r in rows])
    texts = [photo_text(store, r, tags[r["id"]])[:2000] for r in rows]
    n = 0
    for i in range(0, len(rows), 16):
        try:
            vecs = emb.embed_documents(texts[i:i + 16])
        except Exception:
            break
        for r, t, v in zip(rows[i:i + 16], texts[i:i + 16], vecs):
            store.db.execute("INSERT OR REPLACE INTO photo_vec(photo_id, model, dim, text_hash, vec) VALUES (?,?,?,?,?)",
                             (r["id"], space.model, len(v), hashlib.sha1(t.encode()).hexdigest(), np.asarray(v, np.float32).tobytes()))
            n += 1
        store.db.commit()
    return n


def _semantic(store: Store, text: str, embedder=None) -> dict[int, float]:
    import numpy as np
    from fundus import embed
    space = embed.active_space(store) or embed.default_space()
    rows = store.db.execute("SELECT photo_id, vec FROM photo_vec WHERE model = ?", (space.model,)).fetchall()
    if not rows or not text.strip():
        return {}
    try:
        q = np.asarray((embedder or embed.embedder_for(space)).embed_query(text), np.float32)
    except Exception:
        return {}
    m = np.stack([np.frombuffer(r["vec"], np.float32) for r in rows])
    if m.shape[1] != q.shape[0]:
        return {}
    sims = m @ q / (np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1) + 1e-9)
    return {r["photo_id"]: float(s) for r, s in zip(rows, sims)}


# ------------------------------------------------------------------ rights
def cleared_sql(purpose: str) -> str:
    """SQL condition on photo p: may a session use the photo for this purpose?"""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose in {PURPOSES}")
    if purpose == "all":
        return "1"
    people_ok = "(p.consent = 'yes' OR p.faces = 'no')"
    if purpose == "internal":
        return "p.consent != 'no'"
    if purpose == "teaching":
        return f"p.consent != 'no' AND p.usage IN ('teaching','publication') AND {people_ok}"
    return f"p.consent != 'no' AND p.usage = 'publication' AND {people_ok}"


def sound_clearance(p) -> str:
    """Widest purpose a video may be played WITH its sound for: speech (or unknown sound) of people needs consent 'yes',
    otherwise the sound is internal only. Photos and silent videos: as clearance()."""
    c = clearance(p)
    if (p["media"] if "media" in p.keys() else "photo") != "video" or p["audio"] in ("none", "ambient") or p["consent"] == "yes":
        return c
    return "internal" if c != "none" else "none"


def clearance(p) -> str:
    """Widest purpose a photo (or a video's picture) is cleared for: external | teaching | internal | none."""
    if p["consent"] == "no":
        return "none"
    people_ok = p["consent"] == "yes" or p["faces"] == "no"
    if p["usage"] == "publication" and people_ok:
        return "external"
    if p["usage"] in ("teaching", "publication") and people_ok:
        return "teaching"
    return "internal"


# ------------------------------------------------------------------ listing, search, detail
def _summary(store: Store, p, tags: list, detail: bool = False) -> dict:
    out = {"id": p["id"], "ref": ref_of(p), "title": p["title"], "caption": p["caption_de"] or p["caption_en"],
           "caption_en": p["caption_en"], "alt_en": p["alt_en"], "taken_at": p["taken_at"], "width": p["width"],
           "height": p["height"], "orientation": p["orientation"], "people_count": p["people_count"], "faces": p["faces"],
           "consent": p["consent"], "usage": p["usage"], "cleared_for": clearance(p), "status": p["status"],
           "thumb_url": url(p["sha"], "thumb"), "view_url": url(p["sha"], "view"), "slide_url": url(p["sha"], "slide"),
           "media": p["media"] or "photo",
           "tags": [{"dim": t["dim"], "value": t["value"], "label": label(t["dim"], t["value"]), "source": t["source"],
                     "state": t["state"]} for t in tags if detail or t["state"] != "rejected"]}
    if p["media"] == "video":
        out.update({"duration": p["duration"], "audio": p["audio"], "sound_cleared_for": sound_clearance(p),
                    "video_url": url(p["sha"], "video")})
        if p["audio"] != "none":
            out["mute_url"] = url(p["sha"], "mute")
    if detail:
        ann = json.loads(p["annotation"] or "{}")
        out.update({"note": p["note"], "place": p["place"], "camera": p["camera"], "orig_name": p["orig_name"],
                    "source_path": p["source_path"], "caption_de": p["caption_de"], "alt_de": p["alt_de"],
                    "interpretation": p["interpretation"], "credit": p["credit"] or DEFAULT_CREDIT, "sharing": p["sharing"],
                    "annotation": ann, "annotated_at": p["annotated_at"], "error": p["error"], "imported_at": p["imported_at"],
                    "slide_path": str(file_path(store, p["sha"], "slide.jpg")),
                    **({"video_path": str(file_path(store, p["sha"], "video.mp4")), "fps": p["fps"],
                        "sheet_url": url(p["sha"], "sheet")} if p["media"] == "video" else {}),
                    "original_url": f"{PUBLIC_URL}/photos/{p['sha']}.orig.{p['ext']}",
                    "raw_url": f"{PUBLIC_URL}/photos/{p['raw_sha']}.orig.{p['raw_ext']}" if p["raw_sha"] else None,
                    "groups": [dict(r) for r in store.db.execute(
                        "SELECT g.id, g.kind, g.slug, g.title FROM photo_group g JOIN photo_group_item i ON i.group_id = g.id "
                        "WHERE i.photo_id = ? ORDER BY g.kind DESC, g.title", (p["id"],))],
                    "uses": [dict(r) for r in store.db.execute(
                        "SELECT u.exhibit_id, u.ref, u.project, u.used_at, e.title FROM photo_use u LEFT JOIN exhibit e "
                        "ON e.id = u.exhibit_id WHERE u.photo_id = ? ORDER BY u.used_at DESC", (p["id"],))],
                    "similar": [{"id": r["id"], "thumb_url": url(r["sha"], "thumb")} for r in similar(store, p)]})
    return out


def similar(store: Store, p, max_dist: int = 6, limit: int = 8) -> list:
    """Near-duplicates (bursts, re-exports) by difference hash."""
    rows = store.db.execute("SELECT id, sha, dhash FROM photo WHERE id != ? AND removed_at IS NULL AND dhash IS NOT NULL",
                            (p["id"],)).fetchall()
    near = sorted((hamming(p["dhash"], r["dhash"]), r["id"], r) for r in rows)
    return [r for d, _, r in near if d <= max_dist][:limit]


def _filter_sql(dims: dict | None, group: int | None, status: str | None, purpose: str, orientation_: str | None,
                people: str | None, faces: str | None, min_width: int | None, removed: bool = False,
                media: str | None = None) -> tuple[str, list]:
    where, args = ["p.removed_at IS NOT NULL" if removed else "p.removed_at IS NULL", cleared_sql(purpose)], []
    if media in MEDIA:
        where.append("p.media = ?")
        args.append(media)
    elif media not in (None, "", "all"):
        raise ValueError("media: photo | video | all")
    for d, vals in (dims or {}).items():
        vals = [vals] if isinstance(vals, str) else list(vals or [])
        if not vals:
            continue
        if d not in ALL_DIMS:
            raise ValueError(f"unknown dimension '{d}' ({', '.join(ALL_DIMS)})")
        where.append(f"EXISTS (SELECT 1 FROM photo_tag t WHERE t.photo_id = p.id AND t.dim = ? AND t.state != 'rejected' "
                     f"AND t.value IN ({','.join('?' * len(vals))}))")
        args += [d, *[_norm_value(v) for v in vals]]
    if group:
        where.append("EXISTS (SELECT 1 FROM photo_group_item i WHERE i.photo_id = p.id AND i.group_id = ?)")
        args.append(group)
    if status == "unconfirmed":
        where.append("p.status IN ('new','annotated','failed')")
    elif status:
        where.append("p.status = ?")
        args.append(status)
    if orientation_:
        where.append("p.orientation = ?")
        args.append(orientation_)
    if people == "none":
        where.append("COALESCE(p.people_count, 0) = 0 AND p.faces != 'yes'")
    elif people == "some":
        where.append("(p.people_count > 0 OR p.faces = 'yes')")
    if faces in FACES:
        where.append("p.faces = ?")
        args.append(faces)
    if min_width:
        where.append("MAX(p.width, p.height) >= ?")
        args.append(int(min_width))
    return " AND ".join(where), args


def search(store: Store, text: str = "", dims: dict | None = None, purpose: str = "external", group: str | int | None = None,
           status: str | None = None, orientation: str | None = None, people: str | None = None, faces: str | None = None,
           min_width: int | None = None, limit: int = 24, offset: int = 0, sort: str | None = None,
           semantic: bool = True, embedder=None, removed: bool = False, media: str | None = None) -> dict:
    """Find photos. text: own words (German or English) – matched against captions, interpretation, keywords, notes and
    groups, plus meaning (embedding) when available. dims: {dimension: [values]} (all dimensions must match, any value
    within one). media: photo | video | all (default all). purpose: external | teaching | internal | all – only photos cleared for it. Returns total, photos,
    facet counts, and how many more would match with a lower clearance (`hidden_by_rights`)."""
    gid = group_id(store, group) if group not in (None, "") else None
    where, args = _filter_sql(dims, gid, status, purpose, orientation, people, faces, min_width, removed, media)
    rows = store.db.execute(f"SELECT p.* FROM photo p WHERE {where}", args).fetchall()
    ids = [r["id"] for r in rows]
    tags = _tag_rows(store, ids)
    text = (text or "").strip()
    scores: dict[int, float] = {}
    if text:
        toks = [t for t in _fold(text).split() if len(t) > 1]
        lex = {}
        for r in rows:
            body = " " + _fold(photo_text(store, r, tags[r["id"]])) + " "
            hit = sum(1 for t in toks if f" {t}" in body)
            if hit:
                lex[r["id"]] = hit / len(toks) + (0.5 if f" {_fold(text).strip()} " in body else 0)
        sem = _semantic(store, text, embedder) if semantic else {}
        ranked_l = sorted(lex, key=lambda i: -lex[i])
        ranked_s = sorted((i for i in sem if i in tags), key=lambda i: -sem[i])
        for rank, i in enumerate(ranked_l):
            scores[i] = scores.get(i, 0) + 1 / (60 + rank)
        cut = 0.45 if lex else 0.35          # semantic-only hits need some similarity
        for rank, i in enumerate(ranked_s):
            if sem[i] >= cut or i in lex:
                scores[i] = scores.get(i, 0) + 1 / (60 + rank)
        rows = [r for r in rows if r["id"] in scores]
        rows.sort(key=lambda r: -scores[r["id"]])
    else:
        key = {"taken": lambda r: r["taken_at"] or "", "imported": lambda r: r["imported_at"] or ""}.get(sort or "taken")
        rows.sort(key=key, reverse=(sort == "imported"))
    facets: dict[str, dict[str, int]] = {}
    for r in rows:
        for t in tags[r["id"]]:
            if t["state"] != "rejected":
                facets.setdefault(t["dim"], {}).setdefault(t["value"], 0)
                facets[t["dim"]][t["value"]] += 1
    hidden = 0
    if purpose != "all":
        w2, a2 = _filter_sql(dims, gid, status, "all", orientation, people, faces, min_width, removed, media)
        total_all = store.db.execute(f"SELECT COUNT(*) FROM photo p WHERE {w2}", a2).fetchone()[0]
        hidden = max(0, total_all - len(ids)) if not text else None
    page = rows[offset:offset + limit]
    return {"total": len(rows), "photos": [_summary(store, r, tags[r["id"]]) for r in page],
            "facets": {d: dict(sorted(v.items(), key=lambda kv: -kv[1])) for d, v in facets.items()},
            "purpose": purpose, "hidden_by_rights": hidden}


def _row(store: Store, ref) -> "sqlite3.Row":
    s = str(ref)
    m = re.fullmatch(r"(?:photo:|video:)?(\d+)", s.strip())
    row = store.db.execute("SELECT * FROM photo WHERE id = ?", (int(m.group(1)),)).fetchone() if m else \
        store.db.execute("SELECT * FROM photo WHERE sha = ?", (s,)).fetchone()
    if not row:
        raise LookupError(f"photo {ref} not found")
    return row


def get(store: Store, ref) -> dict:
    p = _row(store, ref)
    return _summary(store, p, _tag_rows(store, [p["id"]])[p["id"]], detail=True)


EDITABLE = {"title", "note", "place", "taken_at", "credit", "consent", "usage", "faces", "sharing", "audio"}


def update(store: Store, ref, changes: dict) -> dict:
    p = _row(store, ref)
    ch = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in changes.items() if k in EDITABLE}
    bad = set(changes) - EDITABLE
    if bad:
        raise ValueError(f"cannot change {sorted(bad)} (editable: {sorted(EDITABLE)})")
    if "audio" in ch and p["media"] != "video":
        raise ValueError("audio is a video field")
    for k, allowed in (("consent", CONSENT), ("usage", USAGE), ("faces", FACES), ("sharing", ("private", "team")), ("audio", AUDIO)):
        if k in ch:
            if ch[k] not in allowed:
                raise ValueError(f"{k} must be one of {allowed}")
    if not ch:
        return get(store, p["id"])
    with store.db:
        store.db.execute(f"UPDATE photo SET {', '.join(f'{k} = ?' for k in ch)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                         (*ch.values(), p["id"]))
        if "faces" in ch:
            ann = json.loads(p["annotation"] or "{}")
            ann["faces_by"] = "own"
            store.db.execute("UPDATE photo SET annotation = ? WHERE id = ?", (json.dumps(ann), p["id"]))
        if ch.keys() & {"title", "note", "place"}:
            store.db.execute("DELETE FROM photo_vec WHERE photo_id = ?", (p["id"],))
    return get(store, p["id"])


def _tag_pairs(items) -> list[tuple[str, str]]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            d, v = it.get("dim"), it.get("value")
        elif isinstance(it, (list, tuple)) and len(it) == 2:
            d, v = it
        elif isinstance(it, str) and ":" in it:
            d, v = it.split(":", 1)
        else:
            raise ValueError(f"tag '{it}': give {{dim, value}} or 'dim:value'")
        d, v = str(d).strip(), _norm_value(v)
        if d not in ALL_DIMS:
            raise ValueError(f"unknown dimension '{d}' ({', '.join(ALL_DIMS)})")
        if v:
            out.append((d, v))
    return out


def set_tags(store: Store, ids, add=None, confirm=None, reject=None, remove=None) -> dict:
    """Own keywords: add (own, confirmed; replaces a proposal), confirm/reject LLM proposals, remove own keywords.
    ids: one photo or a list (bulk). Industry/region values must be Fundus vocabulary; other open values are free."""
    ids = [_row(store, i)["id"] for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
    known = store.vocab_values()
    add_p = _tag_pairs(add)
    for d, v in add_p:
        if d in ("industry", "region") and (d, v) not in known:
            raise ValueError(f"{d} '{v}' is not in the Fundus vocabulary (fundus_vocab)")
    with store.db:
        for pid in ids:
            for d, v in add_p:
                store.db.execute("INSERT INTO photo_tag(photo_id, dim, value, source, state) VALUES (?, ?, ?, 'own', 'confirmed') "
                                 "ON CONFLICT(photo_id, dim, value) DO UPDATE SET source = 'own', state = 'confirmed'", (pid, d, v))
            for d, v in _tag_pairs(confirm):
                store.db.execute("UPDATE photo_tag SET state = 'confirmed' WHERE photo_id = ? AND dim = ? AND value = ?", (pid, d, v))
            for d, v in _tag_pairs(reject):
                store.db.execute("UPDATE photo_tag SET state = 'rejected' WHERE photo_id = ? AND dim = ? AND value = ? AND source = 'llm'",
                                 (pid, d, v))
                store.db.execute("DELETE FROM photo_tag WHERE photo_id = ? AND dim = ? AND value = ? AND source = 'own'", (pid, d, v))
            for d, v in _tag_pairs(remove):
                store.db.execute("DELETE FROM photo_tag WHERE photo_id = ? AND dim = ? AND value = ? AND source = 'own'", (pid, d, v))
            store.db.execute("DELETE FROM photo_vec WHERE photo_id = ?", (pid,))
            store.db.execute("UPDATE photo SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pid,))
    return get(store, ids[0]) if len(ids) == 1 else {"updated": len(ids)}


def confirm(store: Store, ids) -> dict:
    """Falk has looked at the photo: open proposals become confirmed, status 'confirmed'."""
    ids = [_row(store, i)["id"] for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
    with store.db:
        for pid in ids:
            store.db.execute("UPDATE photo_tag SET state = 'confirmed' WHERE photo_id = ? AND state = 'proposed'", (pid,))
            store.db.execute("UPDATE photo SET status = 'confirmed', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status != 'new'", (pid,))
    return {"confirmed": len(ids)}


def reannotate(store: Store, ids) -> dict:
    """Queue photos for a new annotation (own keywords, confirmations and rejections stay)."""
    ids = [_row(store, i)["id"] for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
    with store.db:
        store.db.executemany("UPDATE photo SET status = 'new', error = NULL WHERE id = ?", [(i,) for i in ids])
    return {"queued": len(ids)}


def remove(store: Store, ids, restore: bool = False) -> dict:
    """Hide photos (files stay until `purge`); restore=True brings them back."""
    ids = [_row(store, i)["id"] for i in (ids if isinstance(ids, (list, tuple)) else [ids])]
    with store.db:
        store.db.executemany(f"UPDATE photo SET removed_at = {'NULL' if restore else 'CURRENT_TIMESTAMP'} WHERE id = ?",
                             [(i,) for i in ids])
    return {"restored" if restore else "removed": len(ids)}


def record_use(store: Store, ref, exhibit: str | int | None = None, project: str | None = None) -> dict:
    """Note that a slide uses the photo (exhibit id or 'project/set/slug'; unknown refs are kept as text)."""
    p = _row(store, ref)
    eid = None
    if exhibit not in (None, ""):
        try:
            from fundus import exhibits as ex
            eid = ex.get(store, str(exhibit), include_internal=True, script=False)["id"]
        except Exception:
            eid = None
    store.db.execute("INSERT INTO photo_use(photo_id, exhibit_id, ref, project) VALUES (?, ?, ?, ?)",
                     (p["id"], eid, str(exhibit) if exhibit not in (None, "") else None, project))
    store.db.commit()
    return {"photo": p["id"], "ref": ref_of(p), "exhibit": eid,
            "credit": f"{'Video' if p['media'] == 'video' else 'Photo'}: {p['credit'] or DEFAULT_CREDIT}"}


def dims(store: Store) -> dict:
    """Vocabulary for the portal and sessions: controlled dimensions with labels, Fundus industry/region, counts."""
    counts: dict[tuple[str, str], int] = {}
    for r in store.db.execute("SELECT t.dim, t.value, COUNT(*) FROM photo_tag t JOIN photo p ON p.id = t.photo_id "
                              "WHERE t.state != 'rejected' AND p.removed_at IS NULL GROUP BY t.dim, t.value"):
        counts[(r[0], r[1])] = r[2]
    out = {d: {"label": spec["label"], "values": [{"value": v, "label": de, "en": en, "n": counts.get((d, v), 0)}
                                                  for v, (de, en) in spec["values"].items()]} for d, spec in DIMS.items()}
    vlabels = {(r["facet"], r["value"]): r["label"] for r in store.db.execute(
        "SELECT facet, value, label FROM vocab WHERE facet IN ('industry','region')")}
    for d, lab in (("industry", "Branche"), ("region", "Region")):
        vals = sorted({v for (f, v) in vlabels if f == d})
        out[d] = {"label": lab, "open": True, "values": [{"value": v, "label": vlabels[(d, v)] or v, "n": counts.get((d, v), 0)}
                                                          for v in vals if counts.get((d, v)) or d == "industry"]}
    out["keyword"] = {"label": "Schlagworte", "open": True, "values": sorted(
        [{"value": v, "label": v.replace("-", " "), "n": n} for (d, v), n in counts.items() if d == "keyword"],
        key=lambda x: (-x["n"], x["value"]))}
    return out


def stats(store: Store) -> dict:
    r = store.db.execute(
        "SELECT COUNT(*) n, SUM(status='new') new, SUM(status='annotated') annotated, SUM(status='confirmed') confirmed,"
        " SUM(status='failed') failed, SUM(faces='yes') faces, SUM(consent='yes') consent, SUM(media='video') videos"
        " FROM photo WHERE removed_at IS NULL"
    ).fetchone()
    cl = {"external": 0, "teaching": 0, "internal": 0, "none": 0}
    for p in store.db.execute("SELECT consent, usage, faces FROM photo WHERE removed_at IS NULL"):
        cl[clearance(p)] += 1
    return {**{k: r[k] or 0 for k in r.keys()}, "cleared": cl,
            "groups": store.db.execute("SELECT COUNT(*) FROM photo_group").fetchone()[0],
            "uses": store.db.execute("SELECT COUNT(*) FROM photo_use").fetchone()[0]}
