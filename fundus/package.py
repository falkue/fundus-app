"""Slide packages: slides travel between Fundus installations (Fundus Cloud, Nov 2026).

A package is a zip file with a manifest and the files it names. Everything travels: every version of a slide (PNG, HTML),
the generating script and the local files the script reads, sources, linked data points (observations with value, unit,
period and source), slide sets and deck orders with their titles, and the verification stage note of the sender (stage,
who, when, bound to the content hash of the version). Files above 25 MB and dataset files become references (title,
source, download hint); licence-locked data never travels. The mck-exhibit skill travels as a snapshot (`git archive
HEAD`, version = commit) and is only installed on an explicit `install_skill`.

This module is the unencrypted package and everything local around it; encryption, accounts and the transport live in
fundus/cloud*.py and call this interface:

    export(store, refs, out_path, actor='cli', include_skill=True) -> {"path", "manifest"}
    internal_report(store, refs) -> {"internal": [{"ref", "title", "reasons": [...]}], "count": n}
    receive(store, path, sender=None, sender_fingerprint=None) -> {"received_id", ...}
    accept(store, received_id, project, slides=None, decisions=None, actor='portal') -> report
    record_sent(store, manifest, recipients, transport='file') -> sent_id

Matching at the recipient uses exhibit.global_id (uuid4, assigned on first export) and the content hash of a version
(html sha, else png sha): unknown id → new slide; same id and same hash → nothing; recipient unchanged → offer a new
version; both changed → new version, variant (copy) or skip, never overwrite; locked or under review → a copy. A recipient's
re-sent edit is recognised at the original sender the same way. Nothing in a package is executed on import.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fundus import exhibits as ex
from fundus.store import Store, slug as _slug

FORMAT = "fundus-package"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 25 * 1024 * 1024          # Falk, Nov 2026: files up to 25 MB travel, larger ones become references
MAX_SCRIPT_FILES = 200                     # local files of one script
SKILL_NAME = "mck-exhibit"
SENDER_KEY = "package:sender"              # meta: the name this installation sends under
STATUSES = ("new", "accepted", "partly", "rejected")
SHA_FILE = re.compile(r"[0-9a-f]{64}\.(png|preview\.png|html|svg|py)")
ASSET = re.compile(r"[0-9a-f]{64}")
CTYPES = {**ex.CTYPES, "py": "text/plain; charset=utf-8"}
# evidence texts of rule links computed from ids in the slide/script – those ids are the sender's, never ours
SENDER_ID_LINKS = ("fundus-refs in the slide", "explicit Fundus reference")
REASON_LABELS = {"client_project": "Kundenprojekt", "internal_source": "interne Quelle", "internal_data": "interne Daten verknüpft",
                 "internal_dataset": "interner Datensatz", "licence": "Datensatz mit Lizenzbeschränkung",
                 "sharing_project": "nur im Projekt freigegeben", "visibility_internal": "intern"}
LICENCE_LOCKED = re.compile(r"\b(nda|confidential|vertraulich|proprietary|internal|intern|no redistribution|not for redistribution|"
                            r"all rights reserved|non[- ]?commercial|nc\b|ycombinator|yc terms|terms of use)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def chash(v) -> str | None:
    """Content hash of a version: the HTML sha (what the slide says), else the PNG sha (PNG-only renders)."""
    return v["html_sha"] or v["png_sha"]


def received_dir(store: Store) -> Path:
    return Path(os.environ.get("FUNDUS_RECEIVED_DIR") or (store.path.parent / "received"))


def _size(p: Path) -> int:
    """File size (tests mock this to try the 25 MB rule without big files)."""
    return p.stat().st_size


def sender_name(store: Store) -> str:
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (SENDER_KEY,)).fetchone()
    if r and r[0]:
        return r[0]
    if os.environ.get("FUNDUS_SENDER_NAME"):
        return os.environ["FUNDUS_SENDER_NAME"]
    try:
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except (KeyError, AttributeError):
        gecos = ""
    return gecos or os.environ.get("USER") or "Fundus"


# ------------------------------------------------------------------ global ids
def global_id(store: Store, eid: int) -> str:
    """The slide's world-unique id, assigned (uuid4) on first use."""
    r = store.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (eid,)).fetchone()
    if r is None:
        raise LookupError(f"no slide {eid}")
    if r[0]:
        return r[0]
    store.db.execute("UPDATE exhibit SET global_id = ? WHERE id = ? AND global_id IS NULL", (str(uuid.uuid4()), eid))
    store.db.commit()
    return store.db.execute("SELECT global_id FROM exhibit WHERE id = ?", (eid,)).fetchone()[0]


def match(store: Store, gid: str):
    """The own slide with this global id (not in the trash; the most recently changed one if a copy kept it)."""
    return store.db.execute("SELECT * FROM exhibit WHERE global_id = ? AND project != ? ORDER BY coalesce(updated_at, created_at) DESC, "
                            "id DESC LIMIT 1", (gid, ex.TRASH_PROJECT)).fetchone()


# ------------------------------------------------------------------ which slides
def _resolve(store: Store, refs) -> tuple[list[int], dict[int, str], set[int]]:
    """([exhibit ids in order], {id: the ref that brought it}, {deck sequence ids}). refs: slide ids, 'project/slug',
    'project/set/slug', 'set:<project>/<set>', 'deck:<project>/<deck>', 'project:<slug>'."""
    from fundus import decks
    order, origin, seqs = [], {}, set()

    def add(ids, ref):
        for i in ids:
            if i not in origin:
                order.append(i)
                origin[i] = ref
    for ref in refs or []:
        s = str(ref).strip()
        kind, _, rest = s.partition(":") if re.match(r"^(set|deck|project|exhibit|satz|foliensatz):", s, re.I) else ("", "", s)
        kind = {"satz": "set", "foliensatz": "set"}.get(kind.lower(), kind.lower())
        if kind == "project":
            p = _slug(rest)
            ids = [r[0] for r in store.db.execute("SELECT e.id FROM exhibit e JOIN exhibit_set s ON s.id = e.set_id WHERE e.project = ? "
                                                  "ORDER BY s.is_default DESC, s.id, e.id", (p,))]
            if not ids:
                raise LookupError(f"project '{rest}' has no slides")
            seqs |= {r[0] for r in store.db.execute("SELECT id FROM exhibit_sequence WHERE project = ?", (p,))}
            add(ids, s)
        elif kind == "set":
            proj, _, name = rest.partition("/")
            if not name:
                raise ValueError(f"set reference needs project/set: '{s}'")
            row = ex.resolve_set(store, name, _slug(proj))
            add([r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ? ORDER BY id", (row["id"],))], s)
            seqs |= {r[0] for r in store.db.execute("SELECT id FROM exhibit_sequence WHERE set_id = ?", (row["id"],))}
        elif kind == "deck":
            proj, _, name = rest.partition("/")
            seq = decks.get_sequence(store, name, project=_slug(proj))
            seqs.add(seq["id"])
            add([x["id"] for x in seq["slides"]], s)
        else:
            row = ex._resolve(store, rest, include_internal=True)
            add([row["id"]], s)
    trash = {r[0] for r in store.db.execute(f"SELECT id FROM exhibit WHERE project = ? AND id IN ({','.join('?' * len(order))})",
                                            [ex.TRASH_PROJECT, *order])} if order else set()
    order = [i for i in order if i not in trash]
    return order, origin, seqs


# ------------------------------------------------------------------ internal report
_CLIENT_CACHE: dict[str, object] = {}


def _is_client_project(store: Store, project: str) -> bool:
    """A client project: known client (exhibits.PROJECT_CLIENTS) or its folder lies under ~/Projects/Clients/."""
    if project in ex.PROJECT_CLIENTS:
        return True
    import time
    key = f"{store.path}:{project}"
    if key not in _CLIENT_CACHE or time.time() - _CLIENT_CACHE.get(f"{store.path}:@", 0) > 600:
        # project_roots scans every version path: once per call burst (10 minutes), not per slide
        _CLIENT_CACHE.clear()
        for p, d in ex.project_roots_by_slug(store).items():
            _CLIENT_CACHE[f"{store.path}:{p}"] = "Clients" in d.parts
        _CLIENT_CACHE[f"{store.path}:@"] = time.time()
        _CLIENT_CACHE.setdefault(key, False)
    return bool(_CLIENT_CACHE[key])


def _slide_reasons(store: Store, row, proj_sharing: dict, set_sharing: dict) -> list[str]:
    out = []
    if _is_client_project(store, row["project"]):
        out.append("client_project")
    if row["sources_public"] == 0:
        out.append("internal_source")
    if row["internal_links"]:
        out.append("internal_data")
    for d in store.db.execute("SELECT d.visibility, d.license FROM exhibit_link l JOIN dataset d ON d.id = CAST(l.target_id AS INTEGER) "
                              "WHERE l.exhibit_id = ? AND l.target_kind = 'dataset'", (row["id"],)):
        if d["visibility"] == "internal" and "internal_dataset" not in out:
            out.append("internal_dataset")
        if d["license"] and LICENCE_LOCKED.search(d["license"]) and "licence" not in out:
            out.append("licence")
    if "project" in (row["sharing"], set_sharing.get(row["set_id"]), proj_sharing.get(row["project"])):
        out.append("sharing_project")
    if row["visibility"] == "internal":
        out.append("visibility_internal")
    return out


def internal_report(store: Store, refs) -> dict:
    """Slides of the selection that are internal in some way – the sender confirms them before they leave:
    client project, internal source, linked internal data or dataset, licence-locked dataset, sharing=project, visibility
    internal. reasons: codes; labels: the same in German for the dialog."""
    ids, origin, _ = _resolve(store, refs)
    proj = ex.project_sharing(store)
    sets = dict(store.db.execute("SELECT id, sharing FROM exhibit_set WHERE sharing IS NOT NULL").fetchall())
    out = []
    for eid in ids:
        row = store.db.execute("SELECT * FROM exhibit WHERE id = ?", (eid,)).fetchone()
        reasons = _slide_reasons(store, row, proj, sets)
        if reasons:
            out.append({"ref": f"{row['project']}/{row['slug']}", "id": eid, "title": row["title"] or row["slug"],
                        "reasons": reasons, "labels": [REASON_LABELS[r] for r in reasons]})
    return {"internal": out, "count": len(out), "slides": len(ids)}


# ------------------------------------------------------------------ script files
_LIT = re.compile(r"""(?<![\w])[rRbBuUfF]{0,2}(['"])((?:(?!\1)[^\n\\]){2,400})\1""")
_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", re.M)


def _home_form(p: Path, home: Path) -> str | None:
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return None


def _inside(p: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def script_paths(text: str, script_path: str | None) -> list[str]:
    """Path-like string literals of a script (absolute, ~, relative with a folder or a file extension)."""
    out = []
    for m in _LIT.finditer(text or ""):
        s = m.group(2).strip()
        if not s or s.startswith(("http://", "https://", "data:", "#", "<")) or "{" in s or len(s.split()) > 4:
            continue
        if s.startswith(("/", "~", "./", "../")) or "/" in s or re.search(r"\.[A-Za-z0-9]{1,6}$", s):
            if s not in out:
                out.append(s)
    return out


def _script_files(store: Store, text: str, script_path: str | None, project_root: Path | None,
                  own_outputs: set[str]) -> list[dict]:
    """The files a script needs, classified: role project (inside the sender's project folder) | extern | skill (the
    mck-exhibit skill, travels separately) | data (Fundus data or Parquet: a reference). Each: literal, path, kind
    file|dir|missing, included (travels), sha, size, reason."""
    from fundus import datasets
    sdir = Path(script_path).parent if script_path else None
    home = Path.home()
    skill = Path(os.path.realpath(ex.SKILL_DIR))
    data_dirs = [Path(os.path.realpath(store.path.parent)), Path(os.path.realpath(datasets.files_dir(store)))]
    cands = [(lit, lit) for lit in script_paths(text, script_path)]
    for m in _IMPORT.finditer(text or ""):          # sibling helper modules (import common_helpers)
        name = m.group(1) or m.group(2)
        if sdir and (sdir / f"{name}.py").is_file():
            cands.append((f"{name}.py", f"{name}.py"))
    out, seen = [], set()
    for lit, s in cands:
        if s.startswith("~"):
            p = Path(os.path.expanduser(s))
        elif s.startswith("/"):
            p = Path(s)
        elif sdir is not None:
            p = sdir / s
            if not p.exists() and project_root is not None and (project_root / s).exists():
                p = project_root / s
        else:
            continue
        p = Path(os.path.normpath(p))
        if str(p) in seen:
            continue
        seen.add(str(p))
        absolute = s.startswith(("/", "~"))
        kind = "file" if p.is_file() else "dir" if p.is_dir() else "missing"
        if kind == "missing" and not absolute:
            continue
        rp = Path(os.path.realpath(p))
        role = ("skill" if _inside(rp, skill) or _inside(p, ex.SKILL_DIR) else
                "data" if p.suffix.lower() == ".parquet" else
                "project" if _inside(p, project_root) or _inside(rp, project_root) else
                "data" if any(_inside(rp, d) for d in data_dirs) else "extern")
        e = {"literal": lit, "path": str(p), "kind": kind, "role": role, "absolute": absolute, "included": False}
        if project_root is not None and role == "project":
            e["rel"] = (p.relative_to(project_root) if _inside(p, project_root) else rp.relative_to(project_root)).as_posix()
        if kind == "file" and role in ("project", "extern") and str(p) not in own_outputs and str(rp) not in own_outputs:
            size = _size(p)
            e["size"] = size
            if size > MAX_FILE_BYTES:
                e["reason"] = f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB – reference"
            elif len([x for x in out if x["included"]]) >= MAX_SCRIPT_FILES:
                e["reason"] = f"more than {MAX_SCRIPT_FILES} files – reference"
            else:
                e["sha"] = ex.sha256(p.read_bytes())
                e["included"] = True
        elif role == "data":
            e["reason"] = "dataset or Fundus data – reference (datasets never travel)"
        elif kind == "file" and role in ("project", "extern"):
            e["reason"] = "output of the slide (travels as version file)"
        out.append(e)
    return out


# ------------------------------------------------------------------ linked data and the stage note
def _publisher_name(store: Store, pid) -> str | None:
    if not pid:
        return None
    r = store.db.execute("SELECT name FROM publisher WHERE id = ?", (pid,)).fetchone()
    return r[0] if r else pid


def _doc(store: Store, did) -> dict | None:
    r = store.db.execute("SELECT * FROM document WHERE id = ?", (int(did),)).fetchone()
    if r is None:
        return None
    return {k: v for k, v in {"id": r["id"], "title": r["title"], "url": r["url"], "doi": r["doi"], "journal": r["journal"],
                              "publisher": _publisher_name(store, r["publisher_id"]), "published": r["published"],
                              "kind": r["kind"], "access": r["access"], "authors": r["authors"],
                              "language": r["language"]}.items() if v not in (None, "")}


OBS_FIELDS = ("metric", "category", "value", "value_text", "unit", "period", "geo", "population", "n", "method", "locator",
              "quote_context", "notes", "visibility")
SERIES_FIELDS = ("unit", "geo", "population", "frequency", "method", "comparability", "notes")


def _linked(store: Store, eid: int) -> dict:
    docs, obs, dsets = {}, [], []
    for r in store.db.execute("SELECT target_kind, target_id, role FROM exhibit_link WHERE exhibit_id = ? AND target_kind != 'entity' "
                              "ORDER BY target_kind, target_id", (eid,)):
        kind, tid = r["target_kind"], r["target_id"]
        if kind == "document":
            d = _doc(store, tid)
            if d:
                docs[d["id"]] = d
        elif kind == "observation":
            o = store.db.execute("SELECT * FROM observation WHERE id = ?", (int(tid),)).fetchone()
            if o is None or o["status"] == "retracted":
                continue
            item = {"id": o["id"], **{k: o[k] for k in OBS_FIELDS if o[k] not in (None, "")}}
            if o["document_id"]:
                d = _doc(store, o["document_id"])
                if d:
                    docs.setdefault(d["id"], d)
                    item["document"] = d["id"]
            if o["series_id"]:
                srow = store.db.execute("SELECT * FROM series WHERE id = ?", (o["series_id"],)).fetchone()
                if srow is not None:
                    item["series"] = {"id": srow["id"], "name": srow["name"],
                                      "publisher": _publisher_name(store, srow["publisher_id"]),
                                      **{k: srow[k] for k in SERIES_FIELDS if srow[k] not in (None, "")}}
            obs.append(item)
        elif kind == "dataset":
            d = store.db.execute("SELECT * FROM dataset WHERE id = ?", (int(tid),)).fetchone()
            if d is None:
                continue
            dsets.append({k: v for k, v in {"ref": f"dataset:{d['id']}", "title": d["title"], "source_title": d["source_title"],
                                            "publisher": d["publisher"], "url": d["url"], "license": d["license"],
                                            "published": d["published"], "visibility": d["visibility"],
                                            "download_hint": d["update_hint"] or d["url"],
                                            "licence_locked": bool(d["license"] and LICENCE_LOCKED.search(d["license"])) or None,
                                            }.items() if v not in (None, "")})
    return {"documents": list(docs.values()), "observations": obs, "datasets": dsets}


def _review_notes(store: Store, eid: int, st: dict, vers: list) -> list[dict]:
    """The stage note of a slide: the current version's stage (and its fixed verified version), each bound to the
    content hash of that version. origin None = checked here (the recipient puts the sender's name); a stage that came
    from another installation keeps its origin."""
    by_n = {v["n"]: v for v in vers}
    out: list[dict] = []
    pinned = {r["n"]: r["pinned_at"] for r in store.db.execute(
        "SELECT v.n, p.pinned_at FROM exhibit_verified p JOIN exhibit_version v ON v.id = p.version_id WHERE p.exhibit_id = ?", (eid,))}
    if st and st.get("stage") and st.get("version") in by_n:
        f = st.get("foreign")
        out.append({"version": st["version"], "content_hash": chash(by_n[st["version"]]), "stage": st["stage"], "level": st["level"],
                    "label": st.get("label"), "reviewers": (f or {}).get("reviewers") if f and st["stage"] == f["stage"] else st.get("reviewers") or [],
                    "at": (f["checked_at"] if f and st["stage"] == f["stage"] else pinned.get(st["version"])) or _now(),
                    "origin": f["origin"] if f and st["stage"] == f["stage"] else None})
    ver = (st or {}).get("verified")
    if ver and not ver.get("is_current") and ver["version"] in by_n:
        out.append({"version": ver["version"], "content_hash": chash(by_n[ver["version"]]), "stage": ver["stage"],
                    "level": ver["level"], "label": ver.get("label"), "reviewers": [], "at": ver.get("pinned_at"), "origin": None})
    have = {(n["content_hash"], n["origin"]) for n in out}
    hashes = {chash(v): v["n"] for v in vers}
    for r in store.db.execute("SELECT * FROM exhibit_foreign_stage WHERE exhibit_id = ?", (eid,)):
        if r["content_hash"] in hashes and (r["content_hash"], r["origin"]) not in have:
            out.append({"version": hashes[r["content_hash"]], "content_hash": r["content_hash"], "stage": r["stage"], "level": r["level"],
                        "label": None, "reviewers": json.loads(r["reviewers"]) if r["reviewers"] else [], "at": r["checked_at"],
                        "origin": r["origin"]})
    return out


# ------------------------------------------------------------------ export
def _skill_snapshot() -> tuple[bytes | None, dict | None]:
    d = ex.SKILL_DIR
    if not (d / ".git").exists():
        return None, None
    try:
        commit = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        date = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        tar = subprocess.run(["git", "-C", str(d), "archive", "--format=tar", "HEAD"], capture_output=True, check=True, timeout=120).stdout
    except (subprocess.SubprocessError, OSError):
        return None, None
    return tar, {"name": SKILL_NAME, "version": commit, "date": date, "file": f"skill/{SKILL_NAME}.tar", "size": len(tar)}


def export(store: Store, refs, out_path, actor: str = "cli", include_skill: bool = True) -> dict:
    """Write a package (zip) of the slides behind refs to out_path. Returns {"path", "manifest"}. Assigns global ids to
    slides that have none. Every version travels with PNG, HTML and script, the script's local files (≤ 25 MB, larger ones
    and dataset files as references), sources, linked observations, sets and decks with order, and the stage notes."""
    from fundus import reviews as rv
    ids, origin, seqs = _resolve(store, refs)
    if not ids:
        raise ValueError("nothing to export – give slide ids, project/slug, set:<project>/<set>, deck:<project>/<deck> or project:<slug>")
    stages = rv.slide_status(store, ids=ids)
    files: dict[str, Path | bytes] = {}
    references: list[dict] = []
    slides, sets = [], {}
    proj_titles = dict(store.db.execute("SELECT slug, title FROM project").fetchall())
    proj_sharing = ex.project_sharing(store)
    set_sharing = dict(store.db.execute("SELECT id, sharing FROM exhibit_set WHERE sharing IS NOT NULL").fetchall())
    eid_gid: dict[int, str] = {}

    def add_file(sha: str, ext: str, fallback: str | None) -> bool:
        name = f"files/{sha}.{ext}"
        if name in files:
            return True
        p = ex.file_path(store, sha, ext)
        if not p.is_file() and fallback and Path(fallback).is_file() and ex.sha256(Path(fallback).read_bytes()) == sha:
            p = Path(fallback)
        if not p.is_file():
            references.append({"file": name, "reason": "missing in the content store"})
            return False
        size = _size(p)
        if size > MAX_FILE_BYTES:
            references.append({"file": name, "size": size, "path": fallback, "reason": f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"})
            return False
        files[name] = p
        return True

    for eid in ids:
        row = store.db.execute("SELECT e.*, s.slug set_slug, s.title set_title, s.is_default FROM exhibit e JOIN exhibit_set s "
                               "ON s.id = e.set_id WHERE e.id = ?", (eid,)).fetchone()
        gid = eid_gid[eid] = global_id(store, eid)
        vers = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? ORDER BY n", (eid,)).fetchall()
        vlist = []
        for v in vers:
            hit = ex._locate(v["png_path"]) if v["png_path"] else None
            root = hit[1] if hit else None
            e = {"n": v["n"], "content_hash": chash(v), "png": v["png_sha"], "html": v["html_sha"], "html_ext": v["html_ext"],
                 "script": v["script_sha"], "created_at": v["created_at"], "width": v["width"], "height": v["height"],
                 "title": v["title"], "palette": v["palette"], "kind": v["kind"], "png_path": v["png_path"],
                 "html_path": v["html_path"], "script_path": v["script_path"], "project_root": str(root) if root else None}
            e["png_ok"] = add_file(v["png_sha"], "png", v["png_path"])
            if v["html_sha"]:
                e["html_ok"] = add_file(v["html_sha"], v["html_ext"] or "html", v["html_path"])
            if v["script_sha"]:
                e["script_ok"] = add_file(v["script_sha"], "py", v["script_path"])
            vlist.append(e)
        cur = next((x for x in vlist if x["n"] == row["current_version"]), vlist[-1] if vlist else None)
        script_files = []
        if cur and cur.get("script") and cur.get("script_ok"):
            text = Path(files[f"files/{cur['script']}.py"]).read_text(encoding="utf-8", errors="replace")
            outs = {p for x in vlist for p in (x["png_path"], x["html_path"]) if p}
            script_files = _script_files(store, text, cur["script_path"], Path(cur["project_root"]) if cur["project_root"] else None, outs)
            for f in script_files:
                if f["included"]:
                    files.setdefault(f"assets/{f['sha']}", Path(f["path"]))
                elif f.get("reason") and f["role"] != "skill" and f["kind"] == "file":
                    references.append({"file": f["path"], "size": f.get("size"), "slide": gid, "reason": f["reason"]})
        linked = _linked(store, eid)
        for d in linked["datasets"]:
            references.append({"dataset": d["ref"], "title": d["title"], "source": d.get("source_title") or d.get("publisher"),
                               "url": d.get("url"), "download_hint": d.get("download_hint"), "slide": gid,
                               "reason": "datasets travel as references" + (" (licence-locked: never included)" if d.get("licence_locked") else "")})
        xr = store.db.execute("SELECT extract FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, cur["n"])).fetchone() if cur else None
        x = json.loads(xr[0]) if xr and xr[0] else {}
        set_key = f"{row['project']}/{row['set_slug']}"
        sets.setdefault(set_key, {"key": set_key, "project": row["project"], "project_title": proj_titles.get(row["project"]),
                                  "slug": row["set_slug"], "title": row["set_title"], "is_default": bool(row["is_default"]), "slides": []})
        sets[set_key]["slides"].append(gid)
        reasons = _slide_reasons(store, row, proj_sharing, set_sharing)
        slides.append({"global_id": gid, "ref": f"{row['project']}/{row['set_slug']}/{row['slug']}", "project": row["project"],
                       "set": set_key, "slug": row["slug"], "kind": row["kind"], "title": row["title"], "eyebrow": row["eyebrow"],
                       "so_what": row["so_what"], "sources": row["sources"], "source_list": x.get("source_list") or [],
                       "palette": row["palette"], "language": row["language"], "status": row["status"],
                       "internal": row["visibility"] == "internal", "reasons": reasons,
                       "current_version": row["current_version"], "versions": vlist,
                       "review_notes": _review_notes(store, eid, stages.get(eid) or {}, vers),
                       "script_files": script_files, **linked})
    decks_out = []
    for sid in sorted(seqs):
        s = store.db.execute("SELECT q.*, es.slug set_slug FROM exhibit_sequence q LEFT JOIN exhibit_set es ON es.id = q.set_id "
                             "WHERE q.id = ?", (sid,)).fetchone()
        items = []
        for it in store.db.execute("SELECT i.exhibit_id, i.position, v.html_sha, v.png_sha FROM exhibit_sequence_item i LEFT JOIN "
                                   "exhibit_version v ON v.exhibit_id = i.exhibit_id AND v.n = i.version WHERE i.sequence_id = ? "
                                   "ORDER BY i.position", (sid,)):
            if it["exhibit_id"] in eid_gid:
                items.append({"global_id": eid_gid[it["exhibit_id"]], "content_hash": it["html_sha"] or it["png_sha"]})
        if items:
            decks_out.append({"name": s["name"], "title": s["title"], "set": f"{s['project']}/{s['set_slug']}" if s["set_slug"] else None,
                              "slides": items})
    skill_tar, skill = (_skill_snapshot() if include_skill else (None, None))
    title = _package_title(store, refs, sets, slides)
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "package_id": str(uuid.uuid4()), "created_at": _now(),
                "title": title, "sender": {"name": sender_name(store)}, "actor": actor, "refs": [str(r) for r in refs],
                "sender_env": {"home": str(Path.home()), "skill_dir": str(ex.SKILL_DIR), "projects_root": str(ex.PROJECTS_ROOT),
                               "data_dir": str(store.path.parent)},
                "slides": slides, "sets": list(sets.values()), "decks": decks_out, "references": references,
                "files": {}, "skill": skill,
                "internal": [s["ref"] for s in slides if s["reasons"]]}
    out = Path(os.path.expanduser(str(out_path)))
    if out.is_dir() or not out.suffix:            # a folder (existing or not): file name from the title
        out = out /f"{_slug(title) or 'folien'}.fundus.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".tmp{os.getpid()}")
    with zipfile.ZipFile(tmp, "w") as z:
        for name, src in sorted(files.items()):
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            manifest["files"][name] = len(data)
            comp = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
            z.writestr(name, data, compress_type=comp)
        if skill_tar is not None:
            z.writestr(skill["file"], skill_tar, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1), compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(out)
    store.log("package", manifest["package_id"], "export", {"slides": len(slides), "path": str(out), "refs": manifest["refs"]})
    store.db.commit()
    return {"path": str(out), "manifest": manifest}


def _package_title(store: Store, refs, sets: dict, slides: list) -> str:
    if len(sets) == 1:
        s = next(iter(sets.values()))
        name = s["project_title"] or s["project"]
        return name if s["is_default"] else f"{s['title']} ({name})"
    projects = {s["project"] for s in sets.values()}
    if len(projects) == 1:
        p = next(iter(projects))
        return store.db.execute("SELECT coalesce((SELECT title FROM project WHERE slug = ?), ?)", (p, p)).fetchone()[0]
    return f"{len(slides)} Folien"


def record_sent(store: Store, manifest: dict, recipients, transport: str = "file", path: str | None = None) -> int:
    """Note a package as sent (portal "Gesendet"): recipients (names or addresses), transport file | cloud."""
    rec = [recipients] if isinstance(recipients, str) else list(recipients or [])
    with store.batch("package"):
        sid = store.db.execute("INSERT INTO sent_package(package_id, title, recipients, transport, slides, manifest, path) "
                               "VALUES (?,?,?,?,?,?,?)", (manifest.get("package_id"), manifest.get("title"),
                                                          json.dumps(rec, ensure_ascii=False), transport,
                                                          len(manifest.get("slides") or []), json.dumps(manifest, ensure_ascii=False),
                                                          path)).lastrowid
        store.log("sent_package", sid, "insert", {"package": manifest.get("package_id"), "recipients": rec, "transport": transport})
    return sid


def sent_list(store: Store, limit: int = 200) -> dict:
    items = []
    for r in store.db.execute("SELECT id, package_id, title, recipients, transport, slides, sent_at, path FROM sent_package "
                              "ORDER BY id DESC LIMIT ?", (limit,)):
        items.append({**dict(r), "recipients": json.loads(r["recipients"] or "[]")})
    return {"items": items, "count": len(items)}


# ------------------------------------------------------------------ receive
def _check_member(name: str) -> str | None:
    """Kind of an allowed package member, None for anything else (never extracted)."""
    if name == "manifest.json":
        return "manifest"
    m = re.fullmatch(r"files/([0-9a-f]{64})\.(png|html|svg|py)", name)
    if m:
        return "file"
    if re.fullmatch(r"assets/[0-9a-f]{64}", name):
        return "asset"
    if re.fullmatch(r"skill/[a-z0-9-]{1,40}\.tar", name):
        return "skill"
    return None


def read_manifest(path) -> dict:
    with zipfile.ZipFile(path) as z:
        try:
            man = json.loads(z.read("manifest.json").decode("utf-8"))
        except KeyError:
            raise ValueError("not a Fundus package: manifest.json missing")
    if man.get("format") != FORMAT:
        raise ValueError("not a Fundus package (format)")
    if int(man.get("format_version") or 0) > FORMAT_VERSION:
        raise ValueError(f"package format {man.get('format_version')} is newer than this Fundus ({FORMAT_VERSION}) – update Fundus")
    return man


def inspect(path) -> dict:
    """What a package holds, without receiving it."""
    man = read_manifest(path)
    return {"title": man.get("title"), "package_id": man.get("package_id"), "sender": (man.get("sender") or {}).get("name"),
            "created_at": man.get("created_at"), "slides": [{"global_id": s["global_id"], "ref": s["ref"], "title": s["title"],
                                                             "versions": len(s["versions"]), "internal": s.get("internal")}
                                                            for s in man.get("slides") or []],
            "sets": [{"key": s["key"], "title": s["title"], "slides": len(s["slides"])} for s in man.get("sets") or []],
            "decks": [{"name": d["name"], "slides": len(d["slides"])} for d in man.get("decks") or []],
            "files": len(man.get("files") or {}), "bytes": sum((man.get("files") or {}).values()),
            "references": man.get("references") or [], "skill": man.get("skill"), "internal": man.get("internal") or []}


def receive(store: Store, path, sender: str | None = None, sender_fingerprint: str | None = None) -> dict:
    """Take in a package file (already decrypted): check it, unpack the original under <data dir>/received/<id>/ (left
    unchanged afterwards), note it in received_package with status new. Nothing is executed or registered yet."""
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file():
        raise LookupError(f"no package file {p}")
    man = read_manifest(p)
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        bad = [n for n in names if _check_member(n) is None]
        if bad:
            raise ValueError(f"package holds unexpected entries: {', '.join(bad[:5])}")
        for n in names:                            # integrity: content-addressed members must match their name
            k = _check_member(n)
            if k in ("file", "asset"):
                sha = n.split("/")[1].split(".")[0]
                if ex.sha256(z.read(n)) != sha:
                    raise ValueError(f"package damaged: {n} does not match its hash")
        name = sender or (man.get("sender") or {}).get("name") or "unbekannt"
        prior = store.db.execute("SELECT id FROM received_package WHERE package_id = ? ORDER BY id LIMIT 1",
                                 (man.get("package_id"),)).fetchone()
        with store.batch("package"):
            rid = store.db.execute("INSERT INTO received_package(package_id, sender, sender_fingerprint, title, manifest) VALUES (?,?,?,?,?)",
                                   (man.get("package_id"), name, sender_fingerprint, man.get("title"),
                                    json.dumps(man, ensure_ascii=False))).lastrowid
            store.log("received_package", rid, "insert", {"package": man.get("package_id"), "sender": name, "slides": len(man.get("slides") or [])})
        dest = received_dir(store) / str(rid)
        dest.mkdir(parents=True, exist_ok=True)
        for n in names:
            target = dest / n
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
            os.chmod(target, 0o644)
    shutil.copyfile(p, dest / "package.zip")
    _previews(dest, man)
    store.db.execute("UPDATE received_package SET path = ? WHERE id = ?", (str(dest), rid))
    store.db.commit()
    pl = plan(store, rid)
    return {"received_id": rid, "title": man.get("title"), "sender": name, "slides": len(man.get("slides") or []),
            "path": str(dest), "already_received": prior[0] if prior else None,
            "cases": {c: sum(1 for x in pl["slides"] if x["case"] == c) for c in {x["case"] for x in pl["slides"]}}}


def _previews(dest: Path, man: dict) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    for s in man.get("slides") or []:
        for v in s["versions"]:
            src = dest / "files" / f"{v['png']}.png"
            out = dest / "files" / f"{v['png']}.preview.png"
            if not src.is_file() or out.exists():
                continue
            try:
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    if im.width > ex.PREVIEW_W:
                        im = im.resize((ex.PREVIEW_W, max(1, round(im.height * ex.PREVIEW_W / im.width))), Image.LANCZOS)
                    im.save(out, "PNG", optimize=True)
            except Exception:
                continue


def _row(store: Store, rid) -> tuple:
    r = store.db.execute("SELECT * FROM received_package WHERE id = ?", (int(rid),)).fetchone()
    if r is None:
        raise LookupError(f"no received package {rid}")
    return r, json.loads(r["manifest"])


def received_file(store: Store, rid, name: str) -> tuple[Path, str]:
    """A file of the received original (hash names only): (path, content type)."""
    r, _ = _row(store, rid)
    m = SHA_FILE.fullmatch(name or "")
    if not m:
        raise ValueError("bad file name")
    p = Path(r["path"] or received_dir(store) / str(r["id"])) / "files" / name
    if not p.is_file():
        raise LookupError("not found")
    return p, CTYPES[m.group(1)]


# ------------------------------------------------------------------ plan: what accepting would do
def _locked_reason(store: Store, eid: int) -> str | None:
    from fundus import locks
    lk = locks.exhibit_lock(store, eid)
    if lk:
        return "gesperrt (Archiv)"
    if locks.review_locks(store, [eid]).get(eid):
        return "in Prüfung"
    return None


def _plan_slide(store: Store, s: dict) -> dict:
    vh = [v["content_hash"] for v in s["versions"]]
    cur = next((v for v in s["versions"] if v["n"] == s["current_version"]), s["versions"][-1] if s["versions"] else None)
    pcur = cur["content_hash"] if cur else None
    out = {"global_id": s["global_id"], "title": s["title"], "slug": s["slug"], "ref": s["ref"], "versions": len(vh),
           "internal": bool(s.get("internal"))}
    loc = match(store, s["global_id"])
    if loc is None:
        return out | {"case": "new", "options": ["accept", "skip"], "default": "accept"}
    lv = store.db.execute("SELECT n, html_sha, png_sha FROM exhibit_version WHERE exhibit_id = ? ORDER BY n", (loc["id"],)).fetchall()
    lh = [chash(v) for v in lv]
    lcur = next((chash(v) for v in lv if v["n"] == loc["current_version"]), lh[-1] if lh else None)
    st = store.db.execute("SELECT slug, title FROM exhibit_set WHERE id = ?", (loc["set_id"],)).fetchone()
    out["local"] = {"exhibit_id": loc["id"], "project": loc["project"], "set": st["slug"], "set_title": st["title"], "slug": loc["slug"],
                    "ref": f"{loc['project']}/{st['slug']}/{loc['slug']}", "current_version": loc["current_version"]}
    if pcur == lcur:
        return out | {"case": "same", "options": ["skip"], "default": "skip"}
    if pcur in lh:
        return out | {"case": "older", "options": ["skip"], "default": "skip"}
    if lcur in vh:
        out |= {"case": "update", "options": ["new_version", "skip"], "default": "new_version",
                "new_versions": len(vh) - vh.index(lcur) - 1}
    else:
        out |= {"case": "conflict", "options": ["new_version", "variant", "skip"], "default": "variant"}
    why = _locked_reason(store, loc["id"])
    if why:
        out |= {"case": "locked", "locked": why, "was": out["case"], "options": ["variant", "skip"], "default": "variant"}
    return out


CASE_LABELS = {"new": "neu", "same": "schon vorhanden", "older": "eigene Fassung ist neuer", "update": "neue Version",
               "conflict": "beide geändert", "locked": "gesperrt oder in Prüfung – als Kopie"}


def plan(store: Store, rid) -> dict:
    """Per slide of a received package: case new | same | older | update | conflict | locked, the options and the default."""
    _, man = _row(store, rid)
    slides = [_plan_slide(store, s) | {"label": None} for s in man.get("slides") or []]
    for x in slides:
        x["label"] = CASE_LABELS[x["case"]]
    return {"received_id": int(rid), "slides": slides}


# ------------------------------------------------------------------ path smoothing
def rewrite_paths(text: str, prefixes: list[tuple[str, str]]) -> str:
    """Replace path prefixes (longest first, one pass, so a new path is never rewritten again). A prefix only matches at
    a path boundary (end, '/', quote, whitespace, bracket)."""
    pairs = sorted({a: b for a, b in prefixes if a}.items(), key=lambda x: -len(x[0]))
    if not pairs or not text:
        return text
    rx = re.compile("|".join(f"(?:{re.escape(a)})(?=[/'\"\\s)\\],]|$)" for a, _ in pairs), re.M)
    table = dict(pairs)
    return rx.sub(lambda m: table[m.group(0)], text)


def rewrite_ids(text: str, ids: dict[str, str]) -> str:
    """observation:4871 → observation:<own id> (fundus=[…] in the script) for every mapped Fundus item."""
    if not ids or not text:
        return text
    return re.sub(r"\b(observation|document)([:#])(\d+)\b",
                  lambda m: f"{m.group(1)}{m.group(2)}{ids[f'{m.group(1)}:{m.group(3)}'].split(':')[1]}"
                  if f"{m.group(1)}:{m.group(3)}" in ids else m.group(0), text)


def _unresolved(text: str, script_files: list[dict], prefixes: list[tuple[str, str]], env: dict) -> list[dict]:
    """Absolute sender paths in the script that no rewrite covers (or that point at data that did not travel)."""
    covered = [a for a, _ in prefixes if a]
    out = []
    for i, line in enumerate((text or "").splitlines(), 1):
        for m in _LIT.finditer(line):
            s = m.group(2).strip()
            if not s.startswith(("/", "~")) or s.startswith("//"):
                continue
            if any(s == a or s.startswith(a.rstrip("/") + "/") for a in covered):
                continue
            if s.startswith(("/usr/", "/bin/", "/etc/", "/opt/", "/tmp", "/dev/")):
                continue
            out.append({"line": i, "path": s})
    for f in script_files:
        if f["role"] == "data":
            out.append({"path": f["path"], "reason": "Datensatz oder Fundus-Daten des Absenders – nicht mitgeschickt"})
        elif f["kind"] == "file" and not f["included"] and f["role"] in ("project", "extern") and f.get("reason", "").startswith("larger"):
            out.append({"path": f["path"], "reason": f["reason"]})
    seen, uniq = set(), []
    for u in out:
        k = (u.get("line"), u["path"])
        if k not in seen:
            seen.add(k)
            uniq.append(u)
    return uniq


def _project_base(store: Store, project: str) -> tuple[Path, bool]:
    """(base folder for received files of a project, inside the project folder?): the exhibits project folder when one is
    known, else <data dir>/received-files/<project>/."""
    root = ex.project_roots_by_slug(store).get(project)
    if root is not None:
        return root, True
    return Path(os.environ.get("FUNDUS_RECEIVED_FILES") or (store.path.parent / "received-files")) / project, False


def _received_set(store: Store, project: str, man: dict, set_key: str, rid: int, sender: str, cache: dict, actor: str) -> tuple[int, Path]:
    """The slide set that takes the new slides of one sender set: created on first use, named after the sender's set,
    with its own folder received/<slug> when the project folder is known. Returns (set id, its folder)."""
    if set_key in cache:
        sid = cache[set_key]
        row = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (sid,)).fetchone()
        if row is not None:
            return sid, _set_dir(store, row)
    s = next((x for x in man.get("sets") or [] if x["key"] == set_key), {"title": "Erhaltene Folien", "slug": "erhalten", "is_default": False})
    base_title = (s.get("project_title") or s.get("project") or s["title"]) if s.get("is_default") else s["title"]
    title = f"{base_title} – von {sender}"
    slug = _slug(f"{s['slug'] if not s.get('is_default') else (s.get('project') or 'folien')}-erhalten")
    base, inside = _project_base(store, project)
    n = 1
    while store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ? AND slug = ?", (project, slug if n == 1 else f"{slug}-{n}")).fetchone():
        n += 1
    slug = slug if n == 1 else f"{slug}-{n}"
    folders = [f"received/{slug}"] if inside else []
    made = ex.create_set(store, project, title if n == 1 else f"{title} ({n})", folders=folders, slug=slug,
                         note=f"erhalten (Paket #{rid}, {man.get('title')})", actor=actor)
    cache[set_key] = made["id"]
    return made["id"], (base / "received" / slug) if inside else base / slug


def _set_dir(store: Store, row) -> Path:
    base, inside = _project_base(store, row["project"])
    if not inside:
        return base / row["slug"]
    fl = [r[0] for r in store.db.execute("SELECT folder FROM exhibit_set_folder WHERE set_id = ? ORDER BY length(folder) DESC", (row["id"],))]
    return base / fl[0] if fl and fl[0] else base


def _ts(created: str | None) -> float | None:
    if not created:
        return None
    try:
        return datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _place(src: Path, dst: Path, data: bytes | None = None, mtime: float | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
    if data is not None:
        tmp.write_bytes(data)
    else:
        shutil.copyfile(src, tmp)
    tmp.replace(dst)
    if mtime:
        os.utime(dst, (mtime, mtime))


def _rel_in(path: str | None, root: str | None) -> Path | None:
    if not path or not root:
        return None
    try:
        return Path(path).relative_to(root)
    except ValueError:
        return None


def _place_slide(store: Store, src_dir: Path, man: dict, s: dict, versions: list[dict], dest_root: Path, project: str,
                 set_id: int, slug: str, ids: dict, rid: int, actor: str) -> dict:
    """Write the versions of a received slide into dest_root (sender's project layout kept) and register them in order.
    Scripts get their paths rewritten to the new places; the script's packaged files are placed next to them."""
    env = man.get("sender_env") or {}
    home = Path(env.get("home") or "/nonexistent-home")
    placed, unresolved, added, status = [], [], [], None
    eid = None
    cur = next((v for v in s["versions"] if v["n"] == s["current_version"]), s["versions"][-1])
    sroot = cur.get("project_root")
    prefixes: list[tuple[str, str]] = []
    if sroot:
        prefixes.append((sroot, str(dest_root)))
        hf = _home_form(Path(sroot), home)
        if hf:
            prefixes.append((hf, str(dest_root)))
    if env.get("skill_dir"):
        prefixes.append((env["skill_dir"], str(ex.SKILL_DIR)))
        hf = _home_form(Path(env["skill_dir"]), home)
        if hf:
            prefixes.append((hf, str(ex.SKILL_DIR)))
    assets = []
    for f in s.get("script_files") or []:          # the script's own files: project layout, others under _extern/
        if not f["included"]:
            continue
        src = src_dir / "assets" / f["sha"]
        if not src.is_file():
            continue
        if f.get("rel"):
            dst = dest_root / f["rel"]
        else:
            dst = dest_root / "_extern" / Path(f["path"]).name
            if dst.exists() and ex.sha256(dst.read_bytes()) != f["sha"]:
                dst = dest_root / "_extern" / f"{f['sha'][:8]}-{Path(f['path']).name}"
            prefixes.append((f["path"], str(dst)))
            hf = _home_form(Path(f["path"]), home)
            if hf:
                prefixes.append((hf, str(dst)))
        assets.append((src, dst))
    for src, dst in assets:                        # helper scripts (import cmo_common) get the same path smoothing
        if dst.suffix in (".py", ".sh"):
            _place(src, dst, data=rewrite_ids(rewrite_paths(src.read_text(encoding="utf-8", errors="replace"), prefixes), ids).encode("utf-8"))
        else:
            _place(src, dst)
        placed.append(str(dst))
    for v in versions:
        png_src = src_dir / "files" / f"{v['png']}.png"
        if not png_src.is_file():
            added.append({"n": v["n"], "skipped": "PNG did not travel"})
            continue
        rel = _rel_in(v.get("png_path"), v.get("project_root")) or Path("exhibits") / f"{slug}.png"
        if ex.exhibit_slug(rel.stem) != slug:
            rel = rel.with_name(f"{slug}.png")
        png = dest_root / rel
        mt = _ts(v.get("created_at"))
        _place(png_src, png, mtime=mt)
        html = None
        if v.get("html"):
            hsrc = src_dir / "files" / f"{v['html']}.{v.get('html_ext') or 'html'}"
            if hsrc.is_file():
                html = png.with_suffix(f".{v.get('html_ext') or 'html'}")
                _place(hsrc, html, mtime=mt)
        script = None
        if v.get("script"):
            ssrc = src_dir / "files" / f"{v['script']}.py"
            if ssrc.is_file():
                srel = _rel_in(v.get("script_path"), v.get("project_root")) or Path("skripte") / (Path(v.get("script_path") or f"{slug}.py").name)
                script = dest_root / srel
                text = ssrc.read_text(encoding="utf-8", errors="replace")
                new = rewrite_ids(rewrite_paths(text, prefixes), ids)
                _place(ssrc, script, data=new.encode("utf-8"))
                if v is versions[-1]:
                    unresolved = _unresolved(text, s.get("script_files") or [], prefixes, env)
        r = ex.register(store, html, png, script=script, project=project, set_ref=str(set_id), fresh=True, mtime=mt, find=False,
                        note=f"erhalten: Paket #{rid} von {man.get('sender', {}).get('name') or '?'}", actor=actor)
        status = r["status"]
        if r["status"] == "locked":
            added.append({"n": v["n"], "skipped": r.get("note")})
            break
        eid = r["exhibit_id"]
        added.append({"n": v["n"], "version": r["version"], "status": r["status"]})
        placed += [str(png)] + ([str(html)] if html else []) + ([str(script)] if script else [])
    return {"exhibit_id": eid, "versions": added, "placed": placed, "unresolved": unresolved, "status": status}


def _import_data(store: Store, s: dict, project: str, package_id: str, ids: dict) -> dict:
    """Linked documents and observations become own entries (unverified, same visibility). ids collects
    {'observation:<sender id>': 'observation:<own id>'}."""
    made = {"documents": 0, "observations": 0}
    docs = {d["id"]: d for d in s.get("documents") or []}
    for did, d in docs.items():
        key = f"document:{did}"
        if key in ids:
            continue
        fields = {k: d[k] for k in ("doi", "journal", "published", "kind", "access", "authors", "language") if d.get(k)}
        ids[key] = f"document:{store.document(d['title'], url=d.get('url'), publisher=d.get('publisher'), **fields)}"
        made["documents"] += 1
    for o in s.get("observations") or []:
        key = f"observation:{o['id']}"
        if key in ids:
            continue
        fields = {k: o[k] for k in OBS_FIELDS if k in o}
        fields["status"] = "unverified"            # Claude sessions and imports never set verified; checks here do
        if o.get("document") and f"document:{o['document']}" in ids:
            fields["document_id"] = int(ids[f"document:{o['document']}"].split(":")[1])
        if o.get("series"):
            sr = o["series"]
            fields["series_id"] = store.series(sr["id"], sr["name"], publisher=sr.get("publisher"),
                                               **{k: sr[k] for k in SERIES_FIELDS if sr.get(k)})
        new = store.add("observation", fields, origin=(project, f"package:{package_id}", key))
        ids[key] = f"observation:{new}"
        made["observations"] += 1
    return made


def _link_data(store: Store, eid: int, s: dict, ids: dict) -> None:
    """Links of the own slide to the own copies of the sender's data points (the rule links computed from the sender's ids
    in the slide are dropped – those ids mean something else here)."""
    store.db.execute(f"DELETE FROM exhibit_link WHERE exhibit_id = ? AND source = 'rule' AND evidence IN "
                     f"({','.join('?' * len(SENDER_ID_LINKS))})", (eid, *SENDER_ID_LINKS))
    rows = []
    for o in s.get("observations") or []:
        if f"observation:{o['id']}" in ids:
            rows.append((eid, "observation", ids[f"observation:{o['id']}"].split(":")[1], "data"))
    for d in s.get("documents") or []:
        if f"document:{d['id']}" in ids:
            rows.append((eid, "document", ids[f"document:{d['id']}"].split(":")[1], "source"))
    store.db.executemany("INSERT OR IGNORE INTO exhibit_link(exhibit_id, target_kind, target_id, role, source, evidence) "
                         "VALUES (?,?,?,?, 'manual', 'received package')", rows)


def _store_notes(store: Store, eid: int, s: dict, sender: str, rid: int) -> int:
    hashes = {chash(v) for v in store.db.execute("SELECT html_sha, png_sha FROM exhibit_version WHERE exhibit_id = ?", (eid,))}
    n = 0
    me = sender_name(store)
    for note in s.get("review_notes") or []:
        if note.get("content_hash") not in hashes or not note.get("stage") or note.get("origin") == me:
            continue                               # a note about our own check came back: we have the check itself
        store.db.execute("INSERT OR REPLACE INTO exhibit_foreign_stage(exhibit_id, content_hash, stage, level, reviewers, checked_at, "
                         "origin, received_id) VALUES (?,?,?,?,?,?,?,?)",
                         (eid, note["content_hash"], note["stage"], note.get("level"),
                          json.dumps(note.get("reviewers") or [], ensure_ascii=False), note.get("at"), note.get("origin") or sender, rid))
        n += 1
    return n


def _local_version_for(store: Store, eid: int, h: str | None) -> int | None:
    for v in store.db.execute("SELECT n, html_sha, png_sha FROM exhibit_version WHERE exhibit_id = ? ORDER BY n DESC", (eid,)):
        if chash(v) == h:
            return v["n"]
    return None


def accept(store: Store, received_id, project, slides=None, decisions=None, actor: str = "portal") -> dict:
    """Take slides of a received package into a project (existing or new; a text that is not a slug becomes the new
    project's name). slides: global ids (or slugs) to take, None = all. decisions: {global id: accept | new_version |
    variant | skip}; without one the default of the plan applies (new → accept, update → new_version, conflict and locked
    → variant). Returns a report: per slide what happened, the sets and decks made, data points, placed files and the
    paths in scripts that could not be resolved."""
    rec, man = _row(store, received_id)
    rid = rec["id"]
    proj = _slug(str(project or ""))
    if not proj or proj == ex.TRASH_PROJECT:
        raise ValueError("choose a project (existing or new)")
    sender = rec["sender"] or (man.get("sender") or {}).get("name") or "unbekannt"
    src_dir = Path(rec["path"] or received_dir(store) / str(rid))
    decisions = {str(k): v for k, v in (decisions or {}).items()}
    pick = None if slides in (None, "", []) else {str(x) for x in slides}
    prev = json.loads(rec["result"]) if rec["result"] else {}
    set_cache = {k: v for k, v in (prev.get("sets") or {}).items()}
    mapping: dict[str, dict] = prev.get("slides") or {}
    from fundus import projects as _projects
    new_project = proj not in _projects.known(store)
    report = {"received_id": rid, "project": proj, "new_project": new_project, "slides": [], "decks": [], "data": {"documents": 0, "observations": 0},
              "placed": [], "unresolved": []}
    ids: dict[str, str] = dict(prev.get("ids") or {})
    taken = 0
    for s in man.get("slides") or []:
        g = s["global_id"]
        p = _plan_slide(store, s)
        item = {"global_id": g, "title": s["title"], "case": p["case"], "label": CASE_LABELS[p["case"]]}
        cur = next((v for v in s["versions"] if v["n"] == s["current_version"]), s["versions"][-1])
        if p["case"] in ("same", "older") and p.get("local"):
            # already here (selected or not): remember the own slide; a check made at the sender since still counts here
            mapping[g] = {"exhibit_id": p["local"]["exhibit_id"], "content_hash": cur["content_hash"]}
            with store.batch(actor):
                notes = _store_notes(store, p["local"]["exhibit_id"], s, sender, rid)
            report["slides"].append(item | {"action": "skip", "exhibit_id": p["local"]["exhibit_id"], "notes": notes})
            continue
        if pick is not None and g not in pick and s["slug"] not in pick:
            report["slides"].append(item | {"action": "not_selected"})
            continue
        dec = decisions.get(g) or decisions.get(s["slug"]) or p["default"]
        if dec not in p["options"]:
            raise ValueError(f"slide '{s['title'] or s['slug']}' ({CASE_LABELS[p['case']]}): choose one of {', '.join(p['options'])}, not '{dec}'")
        if dec == "skip":
            report["slides"].append(item | {"action": "skip"})
            continue
        with store.batch(actor):
            made = _import_data(store, s, proj, man.get("package_id") or str(rid), ids)
        for k in made:
            report["data"][k] += made[k]
        if dec in ("accept", "variant"):
            set_id, dest = _received_set(store, proj, man, s["set"], rid, sender, set_cache, actor)
            slug = s["slug"]
            n = 1
            while store.db.execute("SELECT 1 FROM exhibit WHERE set_id = ? AND slug = ?", (set_id, slug)).fetchone():
                n += 1
                slug = f"{s['slug']}-{'variante' if dec == 'variant' else 'erhalten'}{'' if n == 2 else f'-{n - 1}'}"
            vers = s["versions"] if dec == "accept" else [cur]
            res = _place_slide(store, src_dir, man, s, vers, dest, proj, set_id, slug, ids, rid, actor)
            eid = res["exhibit_id"]
            if eid and dec == "accept":
                store.db.execute("UPDATE exhibit SET global_id = ? WHERE id = ? AND global_id IS NULL", (g, eid))
        else:                                          # new_version on the own slide that has this global id
            loc = store.db.execute("SELECT * FROM exhibit WHERE id = ?", (p["local"]["exhibit_id"],)).fetchone()
            vh = [v["content_hash"] for v in s["versions"]]
            lcur = chash(store.db.execute("SELECT html_sha, png_sha FROM exhibit_version WHERE exhibit_id = ? AND n = ?",
                                          (loc["id"], loc["current_version"])).fetchone())
            vers = s["versions"][vh.index(lcur) + 1:] if p["case"] == "update" and lcur in vh else [cur]
            setrow = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (loc["set_id"],)).fetchone()
            dest = _set_dir(store, setrow) / "received" / f"paket-{rid}"
            res = _place_slide(store, src_dir, man, s, vers, dest, loc["project"], loc["set_id"], loc["slug"], ids, rid, actor)
            eid = res["exhibit_id"] or loc["id"]
        if eid:
            with store.batch(actor):
                _link_data(store, eid, s, ids)
                notes = _store_notes(store, eid, s, sender, rid)
                if s.get("internal"):
                    store.db.execute("UPDATE exhibit SET sharing = 'project' WHERE id = ?", (eid,))
                ex.recompute(store, [eid])
            mapping[g] = {"exhibit_id": eid, "content_hash": cur["content_hash"]}
            taken += 1
            item |= {"notes": notes}
        report["placed"] += res["placed"]
        report["unresolved"] += [u | {"slide": s["title"] or s["slug"]} for u in res["unresolved"]]
        report["slides"].append(item | {"action": dec, "exhibit_id": eid, "versions": res["versions"], "status": res["status"]})
    if new_project and taken and isinstance(project, str) and project.strip() != proj:
        _projects.set_title(store, proj, project.strip(), actor=actor)
    report["decks"] = _accept_decks(store, man, proj, mapping, rid, set_cache, actor)
    handled = [x for x in report["slides"] if x["action"] != "not_selected"]
    complete = all(g in mapping for g in (s["global_id"] for s in man.get("slides") or []))
    status = "accepted" if complete else ("partly" if mapping else rec["status"] if rec["status"] != "rejected" else "rejected")
    with store.batch(actor):
        store.db.execute("UPDATE received_package SET status = ?, accepted_project = ?, accepted_at = ?, result = ? WHERE id = ?",
                         (status, proj, _now(), json.dumps({"slides": mapping, "sets": set_cache, "ids": ids}, ensure_ascii=False), rid))
        store.log("received_package", rid, "accept", {"project": proj, "status": status, "slides": len(handled), "taken": taken})
    report["status"] = status
    report["taken"] = taken
    return report


def _accept_decks(store: Store, man: dict, project: str, mapping: dict, rid: int, set_cache: dict, actor: str) -> list[dict]:
    from fundus import decks
    out = []
    for d in man.get("decks") or []:
        items = []
        for it in d["slides"]:
            m = mapping.get(it["global_id"])
            if not m:
                continue
            row = store.db.execute("SELECT project, current_version FROM exhibit WHERE id = ?", (m["exhibit_id"],)).fetchone()
            if row is None or row["project"] != project:
                continue
            n = _local_version_for(store, m["exhibit_id"], it.get("content_hash")) or row["current_version"]
            items.append((m["exhibit_id"], n, None))
        if not items:
            continue
        name = d["name"]
        old = store.db.execute("SELECT id, set_id FROM exhibit_sequence WHERE project = ? AND name = ?", (project, name)).fetchone()
        set_id = set_cache.get(d.get("set")) if d.get("set") else None
        if old is not None and old["set_id"] != set_id:
            name = f"{name} (Paket {rid})"
        try:
            got = decks._save(store, project, name, items, "manifest", title=d.get("title"), set_id=set_id, actor=actor)
            out.append({"name": got["name"], "id": got["id"], "slides": len(items)})
        except PermissionError as e:
            out.append({"name": name, "skipped": str(e)})
    return out


def reject(store: Store, received_id, actor: str = "portal") -> dict:
    rec, _ = _row(store, received_id)
    if rec["status"] in ("accepted", "partly"):
        raise ValueError("already accepted (completely or partly) – the slides are in your project; delete them there if needed")
    with store.batch(actor):
        store.db.execute("UPDATE received_package SET status = 'rejected' WHERE id = ?", (rec["id"],))
        store.log("received_package", rec["id"], "reject", None)
    return {"received_id": rec["id"], "status": "rejected"}


# ------------------------------------------------------------------ listing and detail (portal)
def received_list(store: Store, limit: int = 200) -> dict:
    items = []
    for r in store.db.execute("SELECT id, package_id, sender, sender_fingerprint, title, received_at, status, accepted_project, accepted_at, "
                              "json_array_length(manifest, '$.slides') AS slides FROM received_package ORDER BY id DESC LIMIT ?", (limit,)):
        items.append(dict(r))
    return {"items": items, "count": len(items), "new": sum(1 for x in items if x["status"] == "new")}


def _furl(rid: int, sha: str | None, ext: str) -> str | None:
    return f"/api/received/{rid}/file/{sha}.{ext}" if sha else None


def detail(store: Store, received_id) -> dict:
    """A received original as it arrived: slides with every version, stage note, sources, data points and script, plus
    what accepting would do (plan) and – after an accept – the own copy with a hint when it changed since."""
    rec, man = _row(store, received_id)
    rid = rec["id"]
    pl = {x["global_id"]: x for x in plan(store, rid)["slides"]}
    result = json.loads(rec["result"]) if rec["result"] else {}
    mapping = result.get("slides") or {}
    src_dir = Path(rec["path"] or received_dir(store) / str(rid))
    slides = []
    for s in man.get("slides") or []:
        vers = []
        for v in s["versions"]:
            has_prev = (src_dir / "files" / f"{v['png']}.preview.png").is_file()
            vers.append({"n": v["n"], "created_at": v.get("created_at"), "title": v.get("title"), "content_hash": v["content_hash"],
                         "png": _furl(rid, v["png"], "png") if v.get("png_ok", True) else None,
                         "preview": _furl(rid, v["png"], "preview.png" if has_prev else "png") if v.get("png_ok", True) else None,
                         "html": _furl(rid, v.get("html"), v.get("html_ext") or "html") if v.get("html_ok") else None,
                         "script": _furl(rid, v.get("script"), "py") if v.get("script_ok") else None})
        cur = next((x for x in vers if x["n"] == s["current_version"]), vers[-1] if vers else {})
        notes = [{**n, "origin": n.get("origin") or rec["sender"]} for n in s.get("review_notes") or []]
        cur_note = next((n for n in notes if n.get("content_hash") == cur.get("content_hash")), None)
        own = None
        m = mapping.get(s["global_id"])
        if m:
            row = store.db.execute("SELECT e.id, e.project, e.slug, e.set_id, e.current_version, v.html_sha, v.png_sha FROM exhibit e "
                                   "LEFT JOIN exhibit_version v ON v.exhibit_id = e.id AND v.n = e.current_version WHERE e.id = ?",
                                   (m["exhibit_id"],)).fetchone()
            if row is not None:
                own = {"exhibit_id": row["id"], "project": row["project"], "slug": row["slug"], "set_id": row["set_id"],
                       "version": row["current_version"], "changed": chash(row) != m.get("content_hash"),
                       "href": f"#folien?p={row['project']}&set={row['set_id']}"}
        slides.append({"global_id": s["global_id"], "ref": s["ref"], "title": s["title"], "eyebrow": s.get("eyebrow"),
                       "so_what": s.get("so_what"), "kind": s.get("kind"), "sources": s.get("sources"),
                       "source_list": s.get("source_list") or [], "documents": s.get("documents") or [],
                       "observations": s.get("observations") or [], "datasets": s.get("datasets") or [],
                       "internal": s.get("internal"), "reasons": s.get("reasons") or [], "set": s.get("set"),
                       "current_version": s["current_version"], "current": cur, "versions": vers,
                       "review_notes": notes, "stage": cur_note,
                       "script_files": [{k: f.get(k) for k in ("path", "role", "kind", "included", "reason", "size")}
                                        for f in s.get("script_files") or []],
                       "plan": pl.get(s["global_id"]), "own": own})
    return {"id": rid, "package_id": rec["package_id"], "title": rec["title"], "sender": rec["sender"],
            "sender_fingerprint": rec["sender_fingerprint"], "received_at": rec["received_at"], "status": rec["status"],
            "accepted_project": rec["accepted_project"], "accepted_at": rec["accepted_at"], "created_at": man.get("created_at"),
            "sets": man.get("sets") or [], "decks": [{"name": d["name"], "title": d.get("title"), "slides": len(d["slides"])}
                                                    for d in man.get("decks") or []],
            "references": man.get("references") or [], "skill": man.get("skill") and {**man["skill"], **skill_state(store, rid)},
            "slides": slides}


# ------------------------------------------------------------------ the mck-exhibit skill
def _git(d: Path, *args) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _installed(target: Path) -> dict:
    """Version of the skill dir at the recipient: a git checkout (commit, date, uncommitted changes) or an installed
    snapshot (.fundus-skill.json)."""
    if not target.exists():
        return {"exists": False}
    if (target / ".git").exists():
        return {"exists": True, "git": True, "version": _git(target, "rev-parse", "HEAD"),
                "date": _git(target, "log", "-1", "--format=%cI"), "dirty": bool(_git(target, "status", "--porcelain"))}
    meta = target / ".fundus-skill.json"
    if meta.is_file():
        try:
            m = json.loads(meta.read_text())
            return {"exists": True, "git": False, "version": m.get("version"), "date": m.get("date"), "dirty": False}
        except ValueError:
            pass
    return {"exists": True, "git": False, "version": None, "date": None, "dirty": False}


def skill_state(store: Store, received_id) -> dict:
    rec, man = _row(store, received_id)
    sk = man.get("skill")
    if not sk:
        return {"skill_installed": None}
    inst = _installed(ex.SKILL_DIR)
    same = inst.get("version") == sk.get("version")
    newer = bool(inst.get("date") and sk.get("date") and inst["date"] > sk["date"] and not same)
    return {"target": str(ex.SKILL_DIR), "installed_version": inst.get("version"), "installed_date": inst.get("date"),
            "up_to_date": same, "local_newer": newer, "local_changes": bool(inst.get("dirty"))}


def install_skill(store: Store, received_id, force: bool = False, actor: str = "portal") -> dict:
    """Install the mck-exhibit snapshot of a received package into the own skill dir – only on this explicit call.
    Refuses (unless force) when the skill dir has uncommitted changes or holds a newer version. The old dir is kept as
    <dir>.bak-<time>. Nothing of the skill is executed."""
    rec, man = _row(store, received_id)
    sk = man.get("skill")
    if not sk:
        raise LookupError("the package holds no skill snapshot")
    tar = Path(rec["path"] or received_dir(store) / str(rec["id"])) / sk["file"]
    if not tar.is_file():
        raise LookupError("skill snapshot missing in the received package")
    target = ex.SKILL_DIR
    inst = _installed(target)
    if inst.get("version") == sk["version"] and not inst.get("dirty") and not force:
        return {"installed": False, "reason": "already installed", "version": sk["version"], "target": str(target)}
    if not force:
        if inst.get("dirty"):
            raise PermissionError(f"{target} has uncommitted local changes – commit them or install with force")
        if inst.get("date") and sk.get("date") and inst["date"] > sk["date"]:
            raise PermissionError(f"{target} holds a newer version ({inst.get('version', '')[:12]}, {inst['date']}) than the package "
                                  f"({sk['version'][:12]}, {sk['date']}) – install with force to replace it")
        if inst.get("exists") and not inst.get("version"):
            raise PermissionError(f"{target} exists with an unknown version – install with force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".skill-", dir=target.parent))
    try:
        with tarfile.open(tar) as t:
            t.extractall(tmp, filter="data")        # no absolute paths, no '..', no links out of the folder
        (tmp / ".fundus-skill.json").write_text(json.dumps({"name": sk["name"], "version": sk["version"], "date": sk.get("date"),
                                                            "installed_at": _now(), "received": rec["id"],
                                                            "sender": rec["sender"]}, indent=1))
        backup = None
        if target.exists():
            backup = target.with_name(f"{target.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            target.rename(backup)
        tmp.rename(target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    with store.batch(actor):
        store.log("received_package", rec["id"], "install_skill", {"version": sk["version"], "target": str(target),
                                                                     "backup": str(backup) if backup else None, "force": force})
    return {"installed": True, "version": sk["version"], "target": str(target), "backup": str(backup) if backup else None}
