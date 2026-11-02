"""Exhibits in Fundus: slides, charts, posters and section slides (mck-exhibit skill), stored, versioned and searchable.

A logical exhibit is identified by project + slide set + slug. The slug is the file stem without a deck order prefix ('04-',
'B03-', '00_'), a palette suffix ('-hsg', '-blue', '-zhaw') or a version suffix ('-v2'); the blue, HSG and ZHAW render of a
slide (folders exhibits/, exhibits-hsg/, exhibits-zhaw/) are therefore one exhibit with palette variants. Every content change – rendered PNG, HTML
or generating script – is a new version (content hashes); identical content is not stored again, and byte-identical copies
in deck folders become deck memberships. Files live content-addressed under data/exhibits/<sha[:2]>/<sha>.<ext> (png,
html/svg, py) plus a 640 px wide <png sha>.preview.png, served read-only by the MCP HTTP server under /exhibits/<file>.

    register(store, html, png, script=None, project=None, deck=None, note='', set_ref=None)   one render (render.sh hook, MCP, CLI)
    import_dir(store, path, project=None, deck=None, set_ref=None)                            backfill a folder tree
    get(store, ref) · listing(store) · versions(store, ref) · share(store, ref) · set_policy(store, mode)
    create_set(store, project, title, folders) · list_sets · show_set · add/remove_set_folder · rename_set

Slide sets (Foliensätze, tables exhibit_set / exhibit_set_folder): a project holds one or more sets. Every project has a
default set (slug 'main', folder '') that catches everything; a set created explicitly (`fundus exhibits set create`, MCP
fundus_exhibit_set) owns project-relative folders, and a render belongs to the set with the longest folder prefix that
contains its PNG (an explicit set argument overrides the path). The same slug in two sets is two exhibits. Deck folders
(exhibit_deck.deck) are editions/copies inside a set, not sets. Refs: id, 'project/slug' (default set, or unique across
the project's sets), 'project/set/slug'.

Sharing: slide setting > set setting > project setting > global policy (meta 'exhibit_sharing'), applied to
exhibit.visibility, which every search/get path respects (query.vis_sql). Policies:
  share_everything  all exhibits public (current setting)
  internal_default  visible only in their own project unless released (`fundus exhibits share <id>`)
  auto_sources      public when all sources are public and no internal Fundus item is linked (or released), else internal
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from fundus import config as _config
from fundus import projects as _projects
from fundus.layout import CATEGORIES, FOLDER_SLUGS, folder_slug
from fundus.projects import titles as project_titles
from fundus.store import ITEM_KINDS, Store, norm_url, slug as _slug

KINDS = ("slide", "chart", "poster", "section")
STATUSES = ("draft", "final", "presented")
POLICIES = ("share_everything", "internal_default", "auto_sources")
POLICY_KEY = "exhibit_sharing"
DEFAULT_POLICY = "share_everything"
PUBLIC_URL = _config.public_url()   # $FUNDUS_PUBLIC_URL, config file, the running server, else http://127.0.0.1:8010
PROJECTS_ROOT = _config.projects_root()   # $FUNDUS_PROJECTS / config projects_root, default ~/Projects
# project folder (slug of its name) -> project slug; other folders map to the slug of their name. Folders in the category
# layout (~/Projects/<Category>/<Folder>) map through layout.FOLDER_SLUGS first (projects keep their slug from before the move).
PROJECT_MAP = {"acme-dealerships": "acme-dealerships", "northwind": "northwind", "contoso": "contoso",
               "fabrikan": "fabrikam", "fabrikam": "fabrikam"}
# the client organisation of each project: subject entity of all its exhibits (created if missing)
PROJECT_CLIENTS = {"acme-dealerships": ("Acme AG", ["Acme"]), "northwind": ("Northwind", ["Northwind AG"]),
                   "contoso": ("Contoso Group", ["Contoso"]), "fabrikam": ("Fabrikam", ["Fabrikam KG"])}
SKILL_DIR = _config.mck_skill_dir()   # $FUNDUS_MCK_SKILL / config mck_skill, default ~/.claude/skills/mck-exhibit
PREVIEW_W = 640
MAX_SCRIPT = 60000
FILE_RE = re.compile(r"([0-9a-f]{64})\.(png|preview\.png|html|svg|py)")
CTYPES = {"png": "image/png", "preview.png": "image/png", "html": "text/html; charset=utf-8",
          "svg": "image/svg+xml", "py": "text/plain; charset=utf-8"}
# slugs that are generic deck elements: an order prefix in front of them is part of the identity ('11-divider')
GENERIC = {"section", "divider", "sec", "appendix", "part", "chapter", "kapitel", "separator", "trenner", "abschnitt"}
SECTION_RE = re.compile(r"(^|-)(section|divider|sec|cover|title|appendix|chapter|part|kapitel)(-\d+)?$")
SKIP_DIR_RE = re.compile(r"^(logos?|art(-\w+)?|raw|__pycache__|\..*|node_modules|venv|pdf|tex|brand|fonts?|tmp|temp|tests?|"
                         r".*skill.*|charts-demo)$", re.I)
SKIP_FILE_RE = re.compile(r"(^overview|contact[-_ ]?sheet|^preview|[-_]preview$|thumb|^test[_-]|[_-]test$|^_)", re.I)
INTERNAL_SOURCE = re.compile(r"\b(internal|intern|interne[nr]?|confidential|vertraulich|client data|kundendaten|interviews?|"
                             r"workshops?|not public|nicht öffentlich|unpublished|unveröffentlicht|proprietary|NDA)\b", re.I)
_DE = set("der die das und mit für nicht ist von den im auf eine ein zu bei wie sind werden auch des dem nur oder als "
          "noch über aus wird sich durch nach".split())
_EN = set("the and of to in is for with are that on by from as than this not be or at only more their its".split())
_STOP = set("the and for with from that this report survey study data index annual global state edition press release "
            "releases newsroom news page website blog article abstract results figures update edition".split()) | _DE | _EN


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clip(t, n):
    t = " ".join(str(t or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _fold(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().casefold()


def _tokens(t: str) -> set[str]:
    """Content words for title matching (no stop words, no bare years or numbers)."""
    return {w for w in re.findall(r"[a-z0-9]+", _fold(t)) if len(w) >= 4 and w not in _STOP and not w.isdigit()}


# ------------------------------------------------------------------ identity
def exhibit_slug(stem: str) -> str:
    """Logical slide name: without deck order prefix ('04-', 'B03-', '00_' – unless what remains is a generic deck element
    such as 'divider' or starts with a digit, '01-00-section'), palette suffix ('-hsg', '-blue', '-zhaw') and version suffix ('-v2')."""
    s = stem
    m = re.match(r"^[A-Za-z]?\d{1,3}[-_](.+)$", s)
    if m and not re.match(r"^\d", m.group(1)) and _slug(m.group(1)) not in GENERIC:
        s = m.group(1)
    for _ in range(2):
        s = re.sub(r"[-_](hsg|blue|zhaw)$", "", s, flags=re.I)
        s = re.sub(r"[-_.]v\d+$", "", s, flags=re.I)
    return _slug(s) or _slug(stem) or "exhibit"


def order_prefix(name: str) -> tuple[int | None, str | None]:
    m = re.match(r"^([A-Za-z]?)(\d{1,3})[-_]", name)
    return (int(m.group(2)), m.group(1) + m.group(2)) if m else (None, None)


def project_at(rel: Path) -> tuple[str, int] | None:
    """(project slug, number of path parts of the project folder) for a path relative to PROJECTS_ROOT: new layout
    <Category>/<Folder>/… (2 parts; slug from layout.FOLDER_SLUGS for projects from before the move, else the folder name
    without year prefix) or old layout <Folder>/… (1 part). None for a file directly in the root or in a category folder."""
    parts = rel.parts
    if parts and parts[0] in CATEGORIES:
        if len(parts) < 3:
            return None
        s = FOLDER_SLUGS.get(f"{parts[0]}/{parts[1]}") or folder_slug(parts[1])
        return PROJECT_MAP.get(s, s), 2
    if len(parts) >= 2:
        s = _slug(parts[0])
        return PROJECT_MAP.get(s, s), 1
    return None


def _locate(path) -> tuple[str, Path, Path] | None:
    """(project slug, project folder, path relative to it) – tried as given and as realpath, against PROJECTS_ROOT and its
    realpath (a moved project folder kept as a symlink at its old path resolves to the new folder and keeps its slug)."""
    p = Path(os.path.abspath(path))
    roots = dict.fromkeys((PROJECTS_ROOT, Path(os.path.realpath(PROJECTS_ROOT))))
    for cand in (p, Path(os.path.realpath(p))):
        for root in roots:
            try:
                rel = cand.relative_to(root)
            except ValueError:
                continue
            hit = project_at(rel)
            if hit:
                return hit[0], root.joinpath(*rel.parts[:hit[1]]), Path(*rel.parts[hit[1]:])
    return None


def detect_project(path) -> tuple[str, Path]:
    """(project slug, project root) from the path: the project folder under ~/Projects (old layout ~/Projects/<Folder>,
    new layout ~/Projects/<Category>/<Folder>; as given and as realpath; the mapping folds renamed folders,
    Fabrikan → fabrikam), else the slug of the parent folder."""
    hit = _locate(path)
    if hit:
        return hit[0], hit[1]
    p = Path(os.path.abspath(path))
    return _slug(p.parent.name) or "misc", p.parent


def auto_deck(rel_dir: Path) -> str | None:
    """Deck name = project-relative folder when one of its parts is a deck folder ('deck', 'deck-hsg', 'deck_v2' …)."""
    if any(re.search(r"deck", part, re.I) for part in rel_dir.parts):
        return rel_dir.as_posix()
    return None


def under_projects(path) -> bool:
    p = os.path.realpath(path)
    return any(p.startswith(str(r) + os.sep) for r in (PROJECTS_ROOT, Path(os.path.realpath(PROJECTS_ROOT))))


# ------------------------------------------------------------------ text extraction
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SOWHAT = re.compile(r"^\s*(so[ -]what|implications?|key takeaways?|takeaways?|bottom line|what (?:this|it) means|fazit|"
                     r"was (?:das|dies) hei(?:ss|ß)t|konsequenzen?|implikationen?)\b\s*[:\-–—]?\s*", re.I)


_NUM = re.compile(r"^[-+−~<>≈]?\$?€?[\d][\d.,’' ]*\s*(%|pp|x|×|bn|m|k|mn|mio|mrd|tn)?\s*$", re.I)
# chart types that are drawings, not data charts (an exhibit with only these is a 'slide')
NON_DATA_TYPES = {"diagram", "icon", "schematic", "architecture", "logo"}


class _Extractor(HTMLParser):
    """Collects the text of an mck-exhibit page by role (class names written by mck.page() and mck_charts)."""

    BUCKETS = ("eyebrow", "title", "meta", "chart_heads", "so_what", "kicker", "labels", "body", "src_more", "big", "doc_title")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.entries: dict[str, list[str]] = {b: [] for b in self.BUCKETS}
        self.sources: list[str] = []
        self._src_cur: list[str] | None = None
        self.chart_types: list[str] = []
        self.capture: int | None = None
        self.cap_parts: list[str] = []
        self.so_what_kw: list[str] = []
        self.has_h1 = False

    def _top(self) -> dict:
        return self.stack[-1] if self.stack else {"tag": None, "skip": False, "svg": False, "ctx": None, "entry": None}

    def _new_entry(self, bucket: str) -> tuple[str, int]:
        self.entries[bucket].append("")
        return bucket, len(self.entries[bucket]) - 1

    def _end_src_item(self):
        if self._src_cur is not None:
            t = " ".join(" ".join(self._src_cur).split())
            if t:
                self.sources.append(t)
        self._src_cur = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = set(a.get("class", "").split())
        par = self._top()
        fr = {"tag": tag, "skip": par["skip"] or tag in ("script", "style", "head", "noscript"),
              "svg": par["svg"] or tag == "svg", "ctx": par["ctx"], "entry": par["entry"]}
        if tag == "title" and not par["svg"]:
            fr["skip"] = False
            fr["entry"] = self._new_entry("doc_title")
        if tag == "svg":
            kind = a.get("data-mck-chart") or None
            if not kind and not par["svg"]:
                vb = a.get("viewbox", a.get("viewBox", "")).split()
                try:   # older hand-built SVGs of chart size (logos are smaller); numeric labels decide chart vs diagram
                    if (len(vb) == 4 and float(vb[2]) >= 300 and float(vb[3]) >= 150
                            and "logo" not in (a.get("id", "") + a.get("class", "")).lower()):
                        kind = "svg"
                except ValueError:
                    pass
            fr["svgkind"] = [kind, 0] if kind else None
        ctx = fr["ctx"]
        if tag == "h1":
            self.has_h1 = True
            fr["entry"] = self._new_entry("title")
        elif "eyebrow" in cls:
            fr["entry"] = self._new_entry("eyebrow")
        elif "meta" in cls and ctx is None:
            fr["entry"] = self._new_entry("meta")
        elif "chead" in cls:
            fr["entry"] = self._new_entry("chart_heads")
        elif "conclusion" in cls:
            fr["ctx"] = "conclusion"
            fr["entry"] = self._new_entry("so_what")
        elif ctx == "conclusion" and "k" in cls:
            fr["entry"] = self._new_entry("kicker")
        elif "src" in cls and ctx is None:
            fr["ctx"] = "src"
            fr["entry"] = None
            self._src_cur = None
        elif ctx == "src":
            if "sn" in cls:
                self._end_src_item()
                self._src_cur = []
                fr["skip"] = True
            elif "sd" in cls:
                self._end_src_item()
                fr["skip"] = True
            elif "srck" in cls:
                fr["skip"] = True
            elif "srcm" in cls:
                self._end_src_item()
                fr["ctx"] = "srcm"
                fr["entry"] = self._new_entry("src_more")
        elif "copy" in cls:
            fr["skip"] = True
        elif fr["svg"] and tag == "text":
            fr["entry"] = self._new_entry("labels")
        elif cls & {"lab", "labb"}:
            fr["entry"] = self._new_entry("labels")
        elif not fr["svg"]:
            m = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", a.get("style", ""))
            if m and float(m.group(1)) >= 40 and ctx is None:
                fr["entry"] = self._new_entry("big")
        if tag in _VOID:
            return
        self.stack.append(fr)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.stack and self.stack[-1]["tag"] == tag:
            self.stack.pop()

    def _close_svg(self, f):
        """Chart type of a closed <svg>: its data-mck-chart kind; generic SVGs count as a chart ('svg chart') only with
        at least 6 numeric labels, else 'diagram'."""
        kind, nums = f["svgkind"]
        self.chart_types.append(("svg chart" if nums >= 6 else "diagram") if kind == "svg" else kind)

    def handle_endtag(self, tag):
        if tag in _VOID or not any(f["tag"] == tag for f in self.stack):
            return
        while self.stack:
            f = self.stack.pop()
            if f.get("svgkind"):
                self._close_svg(f)
            if f["ctx"] == "src" and (not self.stack or self.stack[-1]["ctx"] != "src"):
                self._end_src_item()
            if f["tag"] == tag:
                break
        if self.capture is not None and len(self.stack) < self.capture:
            if self.cap_parts:
                self.so_what_kw.append(" ".join(self.cap_parts))
            self.capture, self.cap_parts = None, []

    def handle_data(self, data):
        f = self._top()
        if f["skip"]:
            return
        t = " ".join(data.split())
        if not t:
            return
        if f["svg"] and _NUM.match(t):
            for fr in reversed(self.stack):
                if fr.get("svgkind"):
                    fr["svgkind"][1] += 1
                    break
        if f["ctx"] == "src":
            if self._src_cur is None:
                self._src_cur = []
            self._src_cur.append(t)
            return
        bucket = f["entry"][0] if f["entry"] else "body"
        if bucket in ("body", "labels") and not f["svg"]:
            if self.capture is not None:
                if len(" ".join(self.cap_parts)) < 600:
                    self.cap_parts.append(t)
            elif len(t) <= 60 and _SOWHAT.match(t):
                self.capture = max(1, len(self.stack) - 1)
                rest = _SOWHAT.sub("", t, count=1)
                if rest:
                    self.cap_parts.append(rest)
        if f["entry"]:
            b, i = f["entry"]
            self.entries[b][i] = (self.entries[b][i] + " " + t).strip()
        else:
            self.entries["body"].append(t)


def _lang(text: str) -> str | None:
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in _DE for w in words)
    en = sum(w in _EN for w in words)
    if not de and not en:
        return None
    return "de" if de > en * 1.2 else "en"


def _palette_from_text(html: str) -> str | None:
    s = html.upper()
    if 'ID="ZHAW_LOGO"' in s:
        return "zhaw"                     # the blue palette with the ZHAW logo
    hsg = s.count("#0A5F2D") + s.count("#00802F") + 3 * s.count("HSG_LOGO")
    blue = s.count("#051C2C") + s.count("#2251FF")
    if hsg > blue:
        return "hsg"
    if blue:
        return "blue"
    return None


_BARS = (re.compile(r"width:\s*[\d.]+%;\s*height:\s*[\d.]+px;\s*background:"),                       # mck.vbars/hbars
         re.compile(r"position:\s*absolute;[^\"]*left:\s*[\d.]+%;[^\"]*width:\s*(?:max\([^)]*\)|[\d.]+%)[^\"]*background:"))
_DOTS = re.compile(r"position:\s*absolute;[^\"]*left:\s*(?:calc\()?[\d.]+%[^\"]*border-radius:\s*50%")


def _html_charts(html: str) -> list[str]:
    """Charts drawn with positioned HTML (older exhibits without data-mck-chart): bars and dot/dumbbell plots."""
    styles = re.findall(r'style="([^"]*)"', html)
    out = []
    if sum(1 for st in styles if any(b.search(st + '"') for b in _BARS)) >= 3:
        out.append("bars")
    if sum(1 for st in styles if _DOTS.search(st + '"')) >= 3:
        out.append("dots")
    return out


# <meta name="fundus-refs" content="observation:4871 series:bfs-genai-use-ch document:45"> – the Fundus items a slide shows
# (mck.page(..., fundus=[…]) / mck.fundus_refs(html, […]) write it). Documents become sources, items and series data.
# <script type="application/json" id="fundus-sources">[{title, url, publisher, published}]</script> – EVERY source the slide
# used, also those not in the visible source line (mck.page(..., sources=[…])). Registered as documents and linked as sources.
FUNDUS_SOURCES_RE = re.compile(r"""<script[^>]*id=["']fundus-sources["'][^>]*>(.*?)</script>""", re.I | re.S)
FUNDUS_NONE_RE = re.compile(r"""<meta\s+name=["']fundus-sources["']\s+content=["']none(?::\s*([^"']*))?["']""", re.I)
FUNDUS_REFS_RE = re.compile(r"""<meta\s+name=["']fundus-refs["']\s+content=["']([^"']*)["']""", re.I)
REF_KINDS = ("observation", "forecast", "study", "quote", "practice", "fact", "series", "document", "dataset")


def extract(html: str) -> dict:
    """Text and facts of an exhibit page: eyebrow, title (action title), meta line, chart heads, labels, so-what (conclusion
    box or a 'So what'/'Implication' block), body text, sources (numbered source line + further sources), notes, chart types
    (data-mck-chart, older SVG charts, HTML bars), palette and language."""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:   # malformed markup: keep what was parsed
        pass
    p._end_src_item()
    for f in reversed(p.stack):   # unclosed <svg> at the end of malformed markup
        if f.get("svgkind"):
            p._close_svg(f)
    if p.capture is not None and p.cap_parts:
        p.so_what_kw.append(" ".join(p.cap_parts))
    e = {k: [x for x in v if x] for k, v in p.entries.items()}
    title = " ".join(e["title"]) or (e["big"][0] if e["big"] else "") or " ".join(e["doc_title"])
    so_what = " ".join(e["so_what"]) or " ".join(p.so_what_kw)
    kicker = " ".join(e["kicker"])
    if kicker and so_what.startswith(kicker):
        so_what = so_what[len(kicker):].strip()
    sources = [s.strip(" ·;") for s in p.sources if s.strip(" ·;")]
    more, notes = [], []
    for part in re.split(r"\s+·\s+", " ".join(e["src_more"])):
        if re.match(r"(further sources|weitere quellen)\s*:", part, re.I):
            more += [x.strip() for x in re.split(r";", part.split(":", 1)[1]) if x.strip()]
        elif part.strip():
            notes.append(re.sub(r"^(note|notes|hinweis|anmerkung)\s*:\s*", "", part.strip(), flags=re.I))
    if not sources and not more:   # older source lines without numbered items
        raw = " ".join(x for x in p.sources)
        raw = re.sub(r"^(sources?|quellen?)\s*:?\s*", "", raw, flags=re.I)
        sources = [x.strip() for x in re.split(r"\s+·\s+|;\s+", raw) if len(x.strip()) > 2]
    charts = list(dict.fromkeys(p.chart_types)) + _html_charts(html)
    labels = list(dict.fromkeys(x for x in e["labels"] if len(x) > 1))
    body = " ".join(e["body"])
    text_all = " ".join([title, so_what, body, " ".join(labels)])
    refs = [r for m in FUNDUS_REFS_RE.findall(html or "") for r in re.split(r"[\s,;]+", m) if r]
    source_list = []
    for m in FUNDUS_SOURCES_RE.findall(html or ""):
        try:
            got = json.loads(m)
        except ValueError:
            continue
        source_list += [x for x in (got if isinstance(got, list) else []) if isinstance(x, dict) and (x.get("url") or x.get("title"))]
    m_none = FUNDUS_NONE_RE.search(html or "")
    return {"fundus_refs": refs, "source_list": source_list,
            "no_sources": ((m_none.group(1) or "").strip() or "keine Quellen nötig") if m_none else None, "eyebrow": " ".join(e["eyebrow"]), "title": title, "meta": " ".join(e["meta"]),
            "chart_heads": e["chart_heads"], "labels": labels, "so_what": so_what, "kicker": kicker,
            "body": body, "sources": sources, "sources_more": more, "notes": notes, "chart_types": charts,
            "palette": _palette_from_text(html), "language": _lang(text_all), "has_h1": p.has_h1,
            "words": len(re.findall(r"\w+", " ".join([title, so_what, body, " ".join(e["chart_heads"])])))}


def png_size(data: bytes) -> tuple[int | None, int | None]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None, None


def classify(slug: str, width, height, ex: dict | None) -> str:
    if width and height and abs(width / height - 16 / 9) > 0.06:
        return "poster"
    if SECTION_RE.search(slug) or (ex is not None and ex.get("is_html") and not ex.get("has_h1")):
        return "section"
    if ex and any(t not in NON_DATA_TYPES for t in ex.get("chart_types") or []):
        return "chart"
    return "slide"


# ------------------------------------------------------------------ density (skill's density.py)
def _density_mod():
    if str(SKILL_DIR) not in sys.path and (SKILL_DIR / "density.py").exists():
        sys.path.insert(0, str(SKILL_DIR))
    try:
        import density   # noqa: F401
        return density
    except Exception:
        return None


def density_facts(d: dict | None) -> dict | None:
    """Compact density record (verdict + measurements) from the skill's measurement JSON."""
    if not d or not isinstance(d, dict):
        return None
    mod = _density_mod()
    level, reasons = "n/a", []
    if mod is not None:
        try:
            level, reasons, _ = mod.verdict(d)
        except Exception:
            pass
    out = {"verdict": level, "reasons": reasons[:4]}
    for k in ("fit", "fmed", "words", "units", "claims"):
        if d.get(k) is not None:
            out[k] = d[k]
    if d.get("skip"):
        out["skip"] = d["skip"]
    return out


def dom_facts(dom: str) -> tuple[float | None, dict | None]:
    """(fit, density record) from a --dump-dom of the page (render.sh passes it to the hook)."""
    m = re.search(r'data-fit="([0-9.]+)"', dom or "")
    fit = float(m.group(1)) if m else None
    d = None
    mod = _density_mod()
    try:
        if mod is not None:
            d = mod.parse_dom(dom)
        else:
            import html as _h
            m2 = re.search(r'data-density="([^"]*)"', dom or "")
            d = json.loads(_h.unescape(m2.group(1))) if m2 else None
    except Exception:
        d = None
    return fit, density_facts(d)


def measure_many(paths: list[str], jobs: int = 8) -> dict[str, dict]:
    """Density measurements for older HTML (headless render of a temp copy; the originals are never touched)."""
    mod = _density_mod()
    if mod is None or not paths:
        return {}
    h = mod.headless()
    if not h:          # the skill knows a few Chromium paths (Linux arm64, macOS Chrome); else Playwright's (fundus.render)
        from fundus.render import chromium_executable
        h = chromium_executable()
    if not h:
        return {}

    def one(p):
        try:
            return p, mod.measure(p, h)
        except Exception as e:
            return p, {"err": str(e)[:120]}
    with ThreadPoolExecutor(jobs) as ex:
        return dict(ex.map(one, paths))


# ------------------------------------------------------------------ file store
def files_dir(store: Store) -> Path:
    return Path(os.environ.get("FUNDUS_EXHIBIT_DIR") or (store.path.parent / "exhibits"))


def file_path(store: Store, sha: str, ext: str) -> Path:
    return files_dir(store) / sha[:2] / f"{sha}.{ext}"


def put_file(store: Store, data: bytes, ext: str) -> str:
    """Store bytes content-addressed (never twice); returns the sha256."""
    sha = sha256(data)
    p = file_path(store, sha, ext)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        tmp.replace(p)
    return sha


def make_preview(store: Store, png_sha: str) -> tuple[bool, str | None]:
    """640 px wide preview of a stored PNG (once per PNG); also returns the palette seen in the pixels (hsg/blue/None)."""
    src = file_path(store, png_sha, "png")
    out = file_path(store, png_sha, "preview.png")
    try:
        from PIL import Image
    except ImportError:
        return False, None
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > PREVIEW_W:
                im = im.resize((PREVIEW_W, max(1, round(im.height * PREVIEW_W / im.width))), Image.LANCZOS)
            if not out.exists():
                tmp = out.with_name(out.name + f".tmp{os.getpid()}")
                im.save(tmp, "PNG", optimize=True)
                os.chmod(tmp, 0o644)
                tmp.replace(out)
            return True, _palette_from_pixels(im)
    except Exception:
        return False, None


def _palette_from_pixels(im) -> str | None:
    small = im.resize((160, max(1, round(160 * im.height / im.width))))
    keys = {"hsg": [(0x0A, 0x5F, 0x2D), (0x00, 0x80, 0x2F)], "blue": [(0x05, 0x1C, 0x2C), (0x22, 0x51, 0xFF)]}
    cnt = {"hsg": 0, "blue": 0}
    raw = small.tobytes()
    for i in range(0, len(raw) - 2, 3):
        px = raw[i:i + 3]
        for name, cols in keys.items():
            if any(abs(px[0] - c[0]) + abs(px[1] - c[1]) + abs(px[2] - c[2]) < 40 for c in cols):
                cnt[name] += 1
    if max(cnt.values()) < 20:
        return None
    return max(cnt, key=cnt.get)


def url(sha: str | None, ext: str, project: str | None = None, internal: bool = False) -> str | None:
    if not sha:
        return None
    u = f"{PUBLIC_URL}/exhibits/{sha}.{ext}"
    return u + f"?project={quote(project)}" if internal and project else u


def resolve_file(store: Store, name: str, project: str | None = None, include_internal: bool = False):
    """Validate a requested file name (hash names only) and check it belongs to an exhibit the caller may see.
    Returns (path, content type, exhibit id); ValueError bad name, LookupError unknown, PermissionError internal."""
    m = FILE_RE.fullmatch(name or "")
    if not m:
        raise ValueError("bad file name (expected <sha256>.png|preview.png|html|svg|py)")
    sha, ext = m.groups()
    col = {"png": "png_sha", "preview.png": "png_sha", "html": "html_sha", "svg": "html_sha", "py": "script_sha"}[ext]
    rows = store.db.execute(f"SELECT e.id, e.visibility, e.project FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
                            f"WHERE v.{col} = ?", (sha,)).fetchall()
    if not rows:
        raise LookupError("not found")
    ok = [r for r in rows if include_internal or r[1] != "internal" or (project and r[2] == project)]
    if not ok:
        raise PermissionError("internal exhibit; pass ?project=<its project>")
    p = file_path(store, sha, ext)
    if not p.is_file():
        raise LookupError("not found")
    return p, CTYPES[ext], ok[0][0]


def record_view(store: Store, exhibit_id: int, via: str) -> None:
    try:
        store.db.execute("INSERT INTO exhibit_view(day, exhibit_id, via, n) VALUES (date('now', 'localtime'), ?, ?, 1) "
                         "ON CONFLICT(day, exhibit_id, via) DO UPDATE SET n = n + 1", (exhibit_id, via))
        store.db.commit()
    except sqlite3.Error:
        pass


# ------------------------------------------------------------------ generating script
_script_cache: dict[tuple[str, float], str] = {}


def _read_script(p: Path) -> str:
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return ""
    if key not in _script_cache:
        try:
            _script_cache[key] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            _script_cache[key] = ""
    return _script_cache[key]


def find_script(path: Path, root: Path, stem: str) -> Path | None:
    """The script that writes this exhibit: *.py next to the file, then skripte/ or scripts/ in every folder up to the
    project root, nearest first. Match order: a script named '<stem>.py' (or the slug), then '<stem>.html'/'<stem>.png' literally, the stem quoted, the stem without
    order prefix plus '.html' (f-strings like f'{i:02d}-00-section.html')."""
    path = Path(os.path.abspath(path))
    dirs = [path.parent]
    d = path.parent
    root = Path(os.path.abspath(root))
    while True:
        dirs += [d / "skripte", d / "scripts"]
        if d == root or d.parent == d or root not in d.parents:
            break
        d = d.parent
    scripts = []
    for dd in dirs:
        if dd.is_dir():
            scripts += sorted(x for x in dd.glob("*.py") if not x.name.startswith("test_"))
            if dd.name in ("skripte", "scripts"):     # one level of sub-folders (skripte/s3/…), after the direct ones
                scripts += sorted(x for x in dd.glob("*/*.py") if not x.name.startswith("test_") and x.parent.name != "__pycache__")
    if not scripts:
        return None
    base = exhibit_slug(stem)
    m = re.match(r"^[A-Za-z]?\d{1,3}[-_](.+)$", stem)
    rest = m.group(1) if m else stem
    # a script named like the slide wins over any text match (a deck order file names every slug)
    # exact name only: an analysis script 'aei_education.py' next to slide 'aei-education' is not it; the slide script
    # itself may write nothing literal (it calls a shared frame module), so its content is not checked
    names = {stem, base, rest}
    for sc in scripts:
        if sc.stem in names:
            return sc
    passes = [
        lambda t: any(f"{s}{e}" in t for s in {stem, base, rest} for e in (".html", ".png")),
        lambda t: any(f"{q}{s}{q}" in t for s in {stem, base} for q in ("'", '"')),
        lambda t: any(f"{sep}{rest}.html" in t for sep in ("-", "_", "}")),
    ]
    for test in passes:
        for sc in scripts:
            if test(_read_script(sc)):
                return sc
    return None


def caller_matches(caller: Path, stem: str) -> bool:
    """Is the script that ran render.sh this slide's script? Named like it, or names its output file."""
    base = exhibit_slug(stem)
    m = re.match(r"^[A-Za-z]?\d{1,3}[-_](.+)$", stem)
    rest = m.group(1) if m else stem
    if caller.stem in {stem, base, rest}:
        return True
    t = _read_script(caller)
    return any(f"{x}{e}" in t for x in {stem, base, rest} for e in (".html", ".png")) or \
        any(f"{q}{x}{q}" in t for x in {stem, base} for q in ("'", '"'))


def _single_output(script_text: str) -> bool:
    return len(set(re.findall(r"""['"/]([\w.-]+)\.html['"]""", script_text))) <= 1


# ------------------------------------------------------------------ links
_REF = re.compile(r"\b(obs(?:ervations?)?|forecasts?|stud(?:y|ies)|quotes?|practices?|facts?)\.?\s*(?:ids?\s*)?[#:]?\s*"
                  r"(\d+(?:\s*(?:[–-]|,|/|and|und)\s*\d+)*)", re.I)
_SREF = re.compile(r"\bseries\s+([a-z0-9][a-z0-9-]{4,})", re.I)
_KINDWORD = {"obs": "observation", "observation": "observation", "observations": "observation", "forecast": "forecast",
             "forecasts": "forecast", "study": "study", "studies": "study", "quote": "quote", "quotes": "quote",
             "practice": "practice", "practices": "practice", "fact": "fact", "facts": "fact"}


def data_refs(text: str) -> list[tuple[str, str]]:
    """Explicit Fundus references ('Fundus obs 4871, 15391', 'obs 344–347', 'series bfs-genai-use-ch'), only in texts
    that name Fundus."""
    if "fundus" not in (text or "").lower():
        return []
    out = []
    for kw, nums in _REF.findall(text):
        kind = _KINDWORD.get(kw.lower().rstrip("."))
        if not kind:
            continue
        for part in re.split(r"\s*(?:,|/|and|und)\s*", nums):
            r = re.split(r"\s*[–-]\s*", part)
            if len(r) == 2 and r[0].isdigit() and r[1].isdigit() and 0 <= int(r[1]) - int(r[0]) <= 60:
                out += [(kind, str(i)) for i in range(int(r[0]), int(r[1]) + 1)]
            elif r[0].isdigit():
                out.append((kind, r[0]))
    out += [("series", s.lower()) for s in _SREF.findall(text)]
    return list(dict.fromkeys(out))


_pub_cache: dict = {}


def _publisher_index(store: Store):
    sig = store.db.execute("SELECT count(*) || ':' || coalesce(max(rowid), 0) FROM publisher").fetchone()[0]
    key = str(store.path)
    if key in _pub_cache and _pub_cache[key][0] == sig:
        return _pub_cache[key][1]
    names: dict[str, set[str]] = {}
    for pid, name in store.db.execute("SELECT id, name FROM publisher"):
        n = " ".join((name or "").split())
        if len(n) >= 3 and n.lower() not in ("other", "unknown", "various", "n/a", "own analysis"):
            names.setdefault(n, set()).add(pid)
    ci = [n for n in names if not (n.isupper() and len(n) <= 5)]
    cs = [n for n in names if n.isupper() and len(n) <= 5]
    mk = lambda ns, fl: re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in sorted(ns, key=len, reverse=True)) + r")(?!\w)", fl) if ns else None
    pid_name = {pid: n for n, ps in names.items() for pid in ps}
    idx = (mk(ci, re.I), mk(cs, 0), {n.lower(): p for n, p in names.items()}, pid_name)
    _pub_cache[key] = (sig, idx)
    return idx


def match_documents(store: Store, text: str) -> list[int]:
    """Fundus documents named in one source-line item: by URL, else publisher name + title words (+ year)."""
    out = []
    for u in re.findall(r"(?:https?://|www\.)[^\s,;)]+", text or ""):
        k = norm_url(u.rstrip("."))
        out += [r[0] for r in store.db.execute("SELECT id FROM document WHERE url_key = ?", (k,))]
    if out:
        return list(dict.fromkeys(out))
    ci, cs, lookup, pid_name = _publisher_index(store)
    pids = set()
    for rx in (ci, cs):
        if rx is None:
            continue
        for m in rx.finditer(text or ""):
            pids |= lookup.get(m.group(1).lower(), set())
    if not pids:
        return []
    st = _tokens(text)
    years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text))
    best: list[tuple[float, int]] = []
    for pid in pids:
        for did, title, pub in store.db.execute("SELECT id, title, published FROM document WHERE publisher_id = ?", (pid,)):
            if len(title or "") >= 12 and " ".join(_fold(title).split()) in " ".join(_fold(text).split()):
                best.append((2.0, did))   # the full document title appears in the source line
                continue
            tt = _tokens(title) - _tokens(pid_name.get(pid, ""))   # title words beyond the publisher's name
            if not tt:
                continue
            hit = len(tt & st)
            ratio = hit / len(tt)
            yr = bool(pub and pub[:4] in years)
            if years and pub and not yr and not (ratio >= 0.8 and hit >= 3):
                continue   # another edition/year named in the source line
            if (ratio >= 0.6 and hit >= 2) or (ratio >= 0.4 and hit >= 2 and yr) or (len(tt) <= 2 and hit == len(tt) and yr):
                best.append((ratio + (0.2 if yr else 0), did))
    best.sort(reverse=True)
    if not best:
        return []
    top = best[0][0]
    return [d for s, d in best if s >= top - 1e-9][:2]


_URL = re.compile(r"""https?://[^\s,;()<>"']+""")


def source_documents(store: Store, src: dict) -> list[int]:
    """Fundus documents for a source listed in a slide (fundus-sources): the existing one per URL, else a new document
    (title, url, publisher, published). A url field holding several URLs yields one document each; text around them is
    ignored. Without a URL only an exact title match is used – no guessing."""
    raw = str(src.get("url") or "").strip()
    urls = list(dict.fromkeys(u.rstrip(".") for u in _URL.findall(raw)))
    title = " ".join(str(src.get("title") or "").split())[:300]
    out = []
    for url in urls:
        row = store.db.execute("SELECT id FROM document WHERE url_key = ?", (norm_url(url),)).fetchone()
        if row:
            out.append(row[0])
            continue
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0])
        fields = {k: str(src[k])[:200] for k in ("published",) if src.get(k)}
        if src.get("note"):
            fields["notes"] = str(src["note"])[:500]
        out.append(store.document(title or host, url=url, publisher=(str(src["publisher"])[:200] if src.get("publisher") else None), **fields))
    if not urls and title:
        rows = store.db.execute("SELECT id FROM document WHERE title = ? LIMIT 2", (title,)).fetchall()
        if len(rows) == 1:
            out.append(rows[0][0])
    return list(dict.fromkeys(out))


def compute_links(store: Store, ex: dict, script_text: str | None = None) -> list[tuple[str, str, str, str]]:
    """[(target_kind, target_id, role, evidence)] for exhibit_link: documents named in the source line (role source) and
    explicit Fundus references in notes/script (data). Entities: link_entities()."""
    out: dict[tuple[str, str, str], str] = {}
    srcs = list(ex.get("sources") or []) + list(ex.get("sources_more") or [])
    for s in srcs:
        for did in match_documents(store, s):
            out.setdefault(("document", str(did), "source"), _clip(s, 160))
    refs = data_refs(" ".join(ex.get("notes") or []) + " " + (ex.get("body") or ""))
    if script_text and _single_output(script_text):
        refs += data_refs(script_text)
    for kind, iid in dict.fromkeys(refs):
        iid = store.follow(kind, iid)                  # merged by the clean-up: link the record it lives on in
        key = iid if kind == "series" else int(iid)
        if store.db.execute(f"SELECT 1 FROM {kind} WHERE id = ?", (key,)).fetchone():
            out.setdefault((kind, iid, "data"), "explicit Fundus reference")
    for src in ex.get("source_list") or []:
        for did in source_documents(store, src):
            out.setdefault(("document", str(did), "source"), "fundus-sources in the slide")
    for r in ex.get("fundus_refs") or []:
        m = re.fullmatch(r"([a-z]+)[:#]([\w.-]+)", r.strip().lower())
        if not m or m.group(1) not in REF_KINDS:
            continue
        kind, iid = m.groups()
        if kind != "series" and not iid.isdigit():
            continue
        iid = store.follow(kind, iid)
        if store.db.execute(f"SELECT 1 FROM {kind} WHERE id = ?", (iid if kind == "series" else int(iid),)).fetchone():
            out.setdefault((kind, iid, "source" if kind == "document" else "data"), "fundus-refs in the slide")
    return [(k, i, role, ev) for (k, i, role), ev in out.items()]


def client_entity(store: Store, project: str) -> int | None:
    """Entity of the project's client organisation (PROJECT_CLIENTS), created when missing."""
    spec = PROJECT_CLIENTS.get(project)
    if not spec:
        return None
    from fundus import entities as ent
    name, aliases = spec
    eid = ent._find(store, [name, *aliases], "organisation")
    if eid is None:
        eid = ent.create_entity(store, "organisation", name, "manual", aliases=[(a, "manual") for a in aliases])
    return eid


def entity_rows(store: Store, ex: dict, project: str) -> dict[tuple[int, str], tuple[float, str]]:
    """{(entity id, role): (confidence, where)} for one exhibit – the same entity graph as the evidence (entity_link):
    the project's client and organisations/people named in the action title, eyebrow or chart/case heads are 'subject';
    others are 'mentioned' with the prominence used for evidence (so-what/meta 0.7, body, labels and sources 0.5)."""
    from fundus import entities as ent
    r = ent.resolver(store)
    out: dict[tuple[int, str], tuple[float, str]] = {}
    cl = client_entity(store, project)
    if cl is not None:
        out[(cl, "subject")] = (1.0, "client")
    subj_text = " \n".join([ex.get("title") or "", ex.get("eyebrow") or "", " \n".join(ex.get("chart_heads") or [])])
    for eid, _ in r.mentions(subj_text):
        out[(eid, "subject")] = (1.0, "title")
    subjects = {e for e, _ in out}
    tiers = [(0.7, "so_what", [ex.get("so_what") or "", ex.get("meta") or "", ex.get("kicker") or ""]),
             (0.5, "body", [ex.get("body") or "", " \n".join(ex.get("labels") or []), " \n".join(ex.get("notes") or [])]),
             (0.5, "source", list(ex.get("sources") or []) + list(ex.get("sources_more") or []))]
    for conf, where, texts in tiers:
        for t in texts:
            for eid, _ in r.mentions(t):
                if eid not in subjects and (eid, "mentioned") not in out:
                    out[(eid, "mentioned")] = (conf, where)
    return out


def link_entities(store: Store, eid: int, ex: dict | None, project: str) -> int:
    """(Re)write the exhibit's rule links in entity_link (item_kind 'exhibit') and its entity rows in exhibit_link."""
    store.db.execute("DELETE FROM entity_link WHERE item_kind = 'exhibit' AND item_id = ? AND source = 'rule'", (str(eid),))
    store.db.execute("DELETE FROM exhibit_link WHERE exhibit_id = ? AND target_kind = 'entity' AND source = 'rule'", (eid,))
    rows = entity_rows(store, ex or {}, project)
    store.db.executemany("INSERT OR IGNORE INTO entity_link(entity_id, item_kind, item_id, role, source, confidence) "
                         "VALUES (?, 'exhibit', ?, ?, 'rule', ?)", [(e, str(eid), role, c) for (e, role), (c, _) in rows.items()])
    store.db.executemany("INSERT OR IGNORE INTO exhibit_link(exhibit_id, target_kind, target_id, role, evidence) "
                         "VALUES (?, 'entity', ?, ?, ?)",
                         [(eid, str(e), "source" if w == "source" else "mentions", f"{role} ({w})")
                          for (e, role), (c, w) in rows.items()])
    return len(rows)


def _text_extract(store: Store, eid: int) -> dict | None:
    r = store.db.execute("SELECT extract FROM exhibit_version WHERE exhibit_id = ? AND extract IS NOT NULL ORDER BY n DESC LIMIT 1",
                         (eid,)).fetchone()
    return json.loads(r[0]) if r else None


def relink_entities(store: Store, ids: list[int] | None = None) -> int:
    """Entity links of the given exhibits (all by default) from their newest text – called by entities.link() so new
    aliases, merges and full rebuilds reach the slides too."""
    rows = store.db.execute("SELECT id, project FROM exhibit" + (f" WHERE id IN ({','.join('?' * len(ids))})" if ids else ""),
                            ids or []).fetchall()
    for eid, project in rows:
        link_entities(store, eid, _text_extract(store, eid), project)
    return len(rows)


# ------------------------------------------------------------------ sharing policy
def policy(store: Store) -> str:
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (POLICY_KEY,)).fetchone()
    return r[0] if r and r[0] in POLICIES else DEFAULT_POLICY


def sources_public(store: Store, ex: dict) -> int | None:
    srcs = list(ex.get("sources") or []) + list(ex.get("sources_more") or [])
    if not srcs:
        return None
    return 0 if any(INTERNAL_SOURCE.search(s) for s in srcs) else 1


def internal_links(store: Store, eid: int) -> int:
    """Linked Fundus items the caller could not see without the project: internal items, internal series, client-only
    documents or documents only used by internal items."""
    from fundus import query as q
    n = 0
    for kind, tid in store.db.execute("SELECT target_kind, target_id FROM exhibit_link WHERE exhibit_id = ? AND "
                                      "target_kind != 'entity'", (eid,)).fetchall():
        if kind in ITEM_KINDS:
            r = store.db.execute(f"SELECT visibility FROM {kind} WHERE id = ?", (int(tid),)).fetchone()
            n += bool(r and r[0] == "internal")
        elif kind == "series":
            r = store.db.execute("SELECT * FROM series WHERE id = ?", (tid,)).fetchone()
            n += bool(r and not q.is_visible(store, "series", r))
        elif kind == "document":
            r = store.db.execute("SELECT access FROM document WHERE id = ?", (int(tid),)).fetchone()
            n += bool(r and (r[0] == "client_only" or not q.doc_visible(store, int(tid))))
    return n


SHARING = ("shared", "project")          # shared = visible to all projects, project = only inside its own project
PROJECT_SHARING_KEY = "exhibit_sharing:project:"
TRASH_PROJECT = "_trash"          # hidden project of the trash (fundus.trash); never listed, searched or counted


def project_sharing(store: Store) -> dict[str, str]:
    """Project-level sharing settings {project: 'shared' | 'project'} (projects without a row follow the policy)."""
    n = len(PROJECT_SHARING_KEY)
    return {k[n:]: v for k, v in store.db.execute("SELECT key, value FROM meta WHERE key LIKE ?", (PROJECT_SHARING_KEY + "%",))
            if v in SHARING}


def visibility_for(row, mode: str, n_internal: int, project_setting: str | None = None, set_setting: str | None = None) -> str:
    """Effective visibility: the slide's own setting wins, then its slide set's, then its project's, then the global policy."""
    own = row["sharing"] if "sharing" in row.keys() else None
    for s in (own, set_setting, project_setting):
        if s == "shared":
            return "public"
        if s == "project":
            return "internal"
    if mode == "share_everything" or row["released"]:
        return "public"
    if mode == "internal_default":
        return "internal"
    return "public" if row["sources_public"] != 0 and n_internal == 0 else "internal"


def recompute(store: Store, ids: list[int] | None = None, mode: str | None = None) -> dict:
    """Apply the sharing policy to the given exhibits (all by default); returns counts of public/internal/changed."""
    mode = mode or policy(store)
    rows = store.db.execute("SELECT * FROM exhibit" + (f" WHERE id IN ({','.join('?' * len(ids))})" if ids else ""),
                            ids or []).fetchall()
    out = {"policy": mode, "public": 0, "internal": 0, "changed": 0}
    proj = project_sharing(store)
    sets = dict(store.db.execute("SELECT id, sharing FROM exhibit_set WHERE sharing IS NOT NULL").fetchall())
    for r in rows:
        n = internal_links(store, r["id"])
        vis = visibility_for(r, mode, n, proj.get(r["project"]), sets.get(r["set_id"]))
        out[vis] += 1
        if vis != r["visibility"] or n != r["internal_links"]:
            store.db.execute("UPDATE exhibit SET visibility = ?, internal_links = ? WHERE id = ?", (vis, n, r["id"]))
            if vis != r["visibility"]:
                out["changed"] += 1
                store.log("exhibit", r["id"], "update", {"visibility": {"old": r["visibility"], "new": vis}, "policy": mode})
    return out


def set_policy(store: Store, mode: str, actor: str = "cli") -> dict:
    if mode not in POLICIES:
        raise ValueError(f"policy must be one of {', '.join(POLICIES)}")
    with store.batch(actor):
        store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (POLICY_KEY, mode))
        res = recompute(store, mode=mode)
    return res


def share(store: Store, ref, released: bool = True, actor: str = "cli") -> dict:
    row = _resolve(store, ref, include_internal=True)
    with store.batch(actor):
        store.db.execute("UPDATE exhibit SET released = ? WHERE id = ?", (1 if released else 0, row["id"]))
        store.log("exhibit", row["id"], "update", {"released": {"old": row["released"], "new": int(released)}})
        res = recompute(store, [row["id"]])
    vis = store.db.execute("SELECT visibility FROM exhibit WHERE id = ?", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "project": row["project"], "slug": row["slug"], "released": bool(released),
            "visibility": vis, "policy": res["policy"]}


def set_project_sharing(store: Store, project: str, sharing: str | None, actor: str = "cli") -> dict:
    """Project-level sharing: 'shared', 'project' or None (follow the global policy). Applies to every slide of the
    project that has no own setting and whose slide set has none."""
    if sharing not in (*SHARING, None):
        raise ValueError("sharing must be 'shared', 'project' or 'inherit'")
    p = _slug(project)
    ids = [r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE project = ?", (p,))]
    if not ids:
        raise LookupError(f"no exhibits in project '{project}'")
    old = project_sharing(store).get(p)
    with store.batch(actor):
        if sharing:
            store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PROJECT_SHARING_KEY + p, sharing))
        else:
            store.db.execute("DELETE FROM meta WHERE key = ?", (PROJECT_SHARING_KEY + p,))
        store.log("exhibit_project", p, "update", {"sharing": {"old": old, "new": sharing}})
        res = recompute(store, ids)
    return {"project": p, "sharing": sharing, "public": res["public"], "internal": res["internal"], "changed": res["changed"]}


def set_sharing(store: Store, ref, sharing: str | None, actor: str = "cli") -> dict:
    """Slide-level override: 'shared', 'project' or None (follow the set setting, project setting, policy)."""
    if sharing not in (*SHARING, None):
        raise ValueError("sharing must be 'shared', 'project' or 'inherit'")
    row = _resolve(store, ref, include_internal=True)
    with store.batch(actor):
        store.db.execute("UPDATE exhibit SET sharing = ? WHERE id = ?", (sharing, row["id"]))
        store.log("exhibit", row["id"], "update", {"sharing": {"old": row["sharing"], "new": sharing}})
        recompute(store, [row["id"]])
    vis = store.db.execute("SELECT visibility FROM exhibit WHERE id = ?", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "project": row["project"], "slug": row["slug"], "sharing": sharing, "visibility": vis}


# ------------------------------------------------------------------ slide sets (Foliensätze)
def project_rel(path) -> tuple[str, str | None]:
    """(project slug, project-relative posix path) of a file; the path is None outside a project folder under ~/Projects."""
    hit = _locate(path)
    if hit:
        return hit[0], hit[2].as_posix()
    return detect_project(path)[0], None


def norm_folder(folder, project: str | None = None) -> str:
    """Project-relative folder prefix: posix, no leading/trailing slash, no '.'/'..' ('' = the whole project). An absolute
    path under ~/Projects/<project>/ is made relative (and must belong to `project`)."""
    f = str(folder or "").strip()
    if f.startswith(("/", "~")):
        proj, rel = project_rel(os.path.join(os.path.expanduser(f), "_"))
        if rel is None or (project and proj != project):
            raise ValueError(f"folder {folder!r} is not inside the project folder of '{project}' under {PROJECTS_ROOT}")
        f = rel.rsplit("/", 1)[0] if "/" in rel else ""
    parts = [x for x in f.replace("\\", "/").split("/") if x not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"folder must stay inside the project: {folder!r}")
    return "/".join(parts)


def _in_folder(rel: str | None, folder: str) -> bool:
    return folder == "" or (rel is not None and (rel == folder or rel.startswith(folder + "/")))


def _set_folders(store: Store, project: str) -> list[tuple[int, str]]:
    return [(r[0], r[1]) for r in store.db.execute("SELECT set_id, folder FROM exhibit_set_folder WHERE project = ?", (project,))]


def ensure_default_set(store: Store, project: str) -> int:
    """Id of the project's default set (slug 'main', folder ''), created on first use (call inside a batch)."""
    r = store.db.execute("SELECT id FROM exhibit_set WHERE project = ? AND is_default = 1", (project,)).fetchone()
    if r:
        return r[0]
    slug, title = Store.DEFAULT_SET
    if store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ? AND slug = ?", (project, slug)).fetchone():
        slug = "main-default"
    now = _now()
    sid = store.db.execute("INSERT INTO exhibit_set(project, slug, title, is_default, created_at, updated_at) "
                           "VALUES (?, ?, ?, 1, ?, ?)", (project, slug, title, now, now)).lastrowid
    store.db.execute("INSERT OR IGNORE INTO exhibit_set_folder(set_id, project, folder) VALUES (?, ?, '')", (sid, project))
    store.log("exhibit_set", sid, "insert", {"project": project, "slug": slug, "default": True})
    return sid


def set_for_path(store: Store, project: str, rel: str | None) -> int:
    """The set whose folder is the longest prefix of the project-relative path; else the project's default set."""
    best = None
    for sid, folder in _set_folders(store, project):
        if _in_folder(rel, folder) and (best is None or len(folder) > len(best[1])):
            best = (sid, folder)
    return best[0] if best else ensure_default_set(store, project)


def resolve_set(store: Store, ref, project: str | None = None):
    """Set row for an id, 'project/set' or a set slug/title within `project`."""
    ref = str(ref).strip()
    if re.fullmatch(r"\d+", ref):
        r = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (int(ref),)).fetchone()
        if r is not None and project and r["project"] != _slug(project):
            r = None
    else:
        if "/" in ref:
            project, ref = ref.split("/", 1)
        if not project:
            raise ValueError(f"slide set '{ref}': give 'project/set', the set id or a project")
        r = store.db.execute("SELECT * FROM exhibit_set WHERE project = ? AND slug = ?", (_slug(project), _slug(ref))).fetchone()
    if r is None:
        raise LookupError(f"no slide set '{ref}'" + (f" in project '{_slug(project)}'" if project else "") +
                          " (fundus exhibits set list / set create)")
    return r


def _set_brief(row) -> dict:
    return {"id": row["id"], "slug": row["slug"], "title": row["title"]}


def set_info(store: Store, row) -> dict:
    """One set with folders and counts (list/show, gallery)."""
    n, nv, upd = store.db.execute(
        "SELECT count(*), coalesce(sum((SELECT count(*) FROM exhibit_version v WHERE v.exhibit_id = e.id)), 0), "
        "max(coalesce(e.updated_at, e.created_at)) FROM exhibit e WHERE e.set_id = ? AND e.archived_at IS NULL", (row["id"],)).fetchone()
    folders = [r[0] for r in store.db.execute("SELECT folder FROM exhibit_set_folder WHERE set_id = ? ORDER BY folder", (row["id"],))]
    from fundus import locks as _locks
    out = {"id": row["id"], "slug": row["slug"], "title": row["title"], "sharing": row["sharing"],
           "is_default": bool(row["is_default"]), "exhibits": n, "versions": nv, "folders": folders,
           "archived": store.db.execute("SELECT count(*) FROM exhibit WHERE set_id = ? AND archived_at IS NOT NULL",
                                        (row["id"],)).fetchone()[0],
           "favorites": store.db.execute("SELECT count(*) FROM exhibit WHERE set_id = ? AND favorite_at IS NOT NULL "
                                         "AND archived_at IS NULL", (row["id"],)).fetchone()[0],
           "updated_at": max(upd or "", row["updated_at"] or "") or None}
    lk = _locks.set_lock(store, row["id"])
    if lk:
        out["locked"] = lk
    return out


def _exhibit_rels(store: Store, eid: int, project: str) -> list[str | None]:
    """Project-relative paths of all files of an exhibit (version PNGs and deck copies)."""
    paths = [r[0] for r in store.db.execute("SELECT png_path FROM exhibit_version WHERE exhibit_id = ? AND png_path IS NOT NULL",
                                            (eid,))]
    paths += [os.path.join(r[0], r[1]) for r in store.db.execute("SELECT deck_path, file FROM exhibit_deck WHERE exhibit_id = ? "
                                                                 "AND deck_path IS NOT NULL", (eid,))]
    out = []
    for p in dict.fromkeys(paths):
        proj, rel = project_rel(p)
        out.append(rel if proj == project else None)
    return out


def _move(store: Store, eid: int, slug: str, src: int, dst: int, reason: str) -> None:
    store.db.execute("UPDATE exhibit SET set_id = ? WHERE id = ?", (dst, eid))
    store.log("exhibit", eid, "update", {"set": {"old": src, "new": dst}, "slug": slug, "reason": reason})
    store._touch("exhibit", eid)


def _rebalance(store: Store, project: str, target: int | None = None, released_from: int | None = None,
               reason: str = "") -> dict:
    """After a folder change, move slides whose files now belong to another set by path.
    target (folder added to that set): slides of other sets whose files all lie in the target's folders and whose current
    set owns them only by a shorter folder prefix (slides put into an unrelated set explicitly stay).
    released_from (folder removed from that set): its slides whose files are no longer under any of its folders go to the
    set their path resolves to. A slide is not moved when the target already has its slug or when its files are split
    across sets (reported as conflicts)."""
    from fundus import locks as _locks
    folders = _set_folders(store, project)

    def match(sid, rel):
        return max((len(f) for s, f in folders if s == sid and _in_folder(rel, f)), default=-1)

    def best(rel):
        c = [(len(f), s) for s, f in folders if _in_folder(rel, f)]
        return max(c)[1] if c else None
    names = {r[0]: r[1] for r in store.db.execute("SELECT id, slug FROM exhibit_set WHERE project = ?", (project,))}
    moved, conflicts = [], []
    where, params = ("set_id = ?", [released_from]) if released_from else ("set_id != ?", [target])
    for eid, slug, cur in store.db.execute(f"SELECT id, slug, set_id FROM exhibit WHERE project = ? AND {where} ORDER BY id",
                                           [project, *params]).fetchall():
        rels = _exhibit_rels(store, eid, project)
        if not rels:
            continue
        dests = {best(r) for r in rels}
        if released_from:
            if all(match(cur, r) >= 0 for r in rels):
                continue
        else:
            if target not in dests:
                continue
            if any(match(cur, r) < 0 for r in rels):   # put into its set explicitly, not by path
                continue
        if len(dests) != 1 or None in dests:
            conflicts.append({"id": eid, "slug": slug, "set": names.get(cur), "reason": "files lie in several slide sets' folders"})
            continue
        dst = dests.pop()
        if dst == cur:
            continue
        if _locks.exhibit_lock(store, eid) or _locks.set_lock(store, dst):
            conflicts.append({"id": eid, "slug": slug, "set": names.get(cur), "reason": "locked (archive): stays where it is"})
            continue
        if store.db.execute("SELECT 1 FROM exhibit WHERE project = ? AND set_id = ? AND slug = ?", (project, dst, slug)).fetchone():
            conflicts.append({"id": eid, "slug": slug, "set": names.get(cur),
                              "reason": f"slug already exists in set '{names.get(dst)}'"})
            continue
        _move(store, eid, slug, cur, dst, reason)
        moved.append({"id": eid, "slug": slug, "from": names.get(cur), "to": names.get(dst)})
    if moved:
        recompute(store, [m["id"] for m in moved])
    return {"moved": moved, "conflicts": conflicts}


def _check_folders(store: Store, project: str, folders, sid: int | None = None) -> list[str]:
    out = []
    for f in folders or []:
        nf = norm_folder(f, project)
        if nf == "":
            raise ValueError("folder '' (the whole project) belongs to the default set; give a subfolder")
        owner = store.db.execute("SELECT s.id, s.slug FROM exhibit_set_folder f JOIN exhibit_set s ON s.id = f.set_id "
                                 "WHERE f.project = ? AND f.folder = ?", (project, nf)).fetchone()
        if owner is not None and owner[0] != sid:
            raise ValueError(f"folder '{nf}' already belongs to slide set '{project}/{owner[1]}'")
        if nf not in out:
            out.append(nf)
    return out


def create_set(store: Store, project: str, title: str, folders=(), slug: str | None = None, note: str | None = None,
               actor: str = "cli") -> dict:
    """Register a new slide set of a project (when Falk starts one: "neuer Foliensatz X") with its own folder(s). Slides
    already registered under those folders (in the default set or a set owning a shorter prefix) move into it, unless the
    new set would get a slug twice (reported as conflicts)."""
    p = _slug(project)
    title = " ".join(str(title or "").split())
    if not p or not title:
        raise ValueError("project and title are required")
    s = _slug(slug or title)
    if not s:
        raise ValueError("cannot derive a slug from the title; give one")
    with store.batch(actor):
        ensure_default_set(store, p)
        if store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ? AND slug = ?", (p, s)).fetchone():
            raise ValueError(f"slide set '{p}/{s}' exists already (fundus exhibits set show {p}/{s})")
        fl = _check_folders(store, p, folders)
        now = _now()
        sid = store.db.execute("INSERT INTO exhibit_set(project, slug, title, note, is_default, created_at, updated_at) "
                               "VALUES (?, ?, ?, ?, 0, ?, ?)", (p, s, title, note or None, now, now)).lastrowid
        store.db.executemany("INSERT INTO exhibit_set_folder(set_id, project, folder) VALUES (?, ?, ?)", [(sid, p, f) for f in fl])
        store.log("exhibit_set", sid, "insert", {"project": p, "slug": s, "title": title, "folders": fl})
        res = _rebalance(store, p, target=sid, reason=f"slide set '{s}' created") if fl else {"moved": [], "conflicts": []}
        row = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (sid,)).fetchone()
        out = {"project": p, **set_info(store, row), **res}
    if not fl:
        out["hint"] = f"no folder: renders land in the default set unless registered with --set {s}"
    return out


def add_set_folder(store: Store, ref, folder: str, project: str | None = None, actor: str = "cli") -> dict:
    from fundus import locks as _locks
    row = resolve_set(store, ref, project)
    _locks.guard_set(store, row["id"], "a folder change")
    with store.batch(actor):
        fl = _check_folders(store, row["project"], [folder], row["id"])
        store.db.execute("INSERT OR IGNORE INTO exhibit_set_folder(set_id, project, folder) VALUES (?, ?, ?)",
                         (row["id"], row["project"], fl[0]))
        store.db.execute("UPDATE exhibit_set SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
        store.log("exhibit_set", row["id"], "update", {"folder_added": fl[0]})
        res = _rebalance(store, row["project"], target=row["id"], reason=f"folder '{fl[0]}' added to set '{row['slug']}'")
        out = {"project": row["project"], **set_info(store, row), **res}
    return out


def remove_set_folder(store: Store, ref, folder: str, project: str | None = None, actor: str = "cli") -> dict:
    from fundus import locks as _locks
    row = resolve_set(store, ref, project)
    _locks.guard_set(store, row["id"], "a folder change")
    nf = norm_folder(folder, row["project"])
    if row["is_default"] and nf == "":
        raise ValueError("the default set keeps folder '' (it catches everything outside other sets)")
    with store.batch(actor):
        n = store.db.execute("DELETE FROM exhibit_set_folder WHERE set_id = ? AND folder = ?", (row["id"], nf)).rowcount
        if not n:
            raise LookupError(f"slide set '{row['project']}/{row['slug']}' has no folder '{nf}'")
        store.db.execute("UPDATE exhibit_set SET updated_at = ? WHERE id = ?", (_now(), row["id"]))
        store.log("exhibit_set", row["id"], "update", {"folder_removed": nf})
        res = _rebalance(store, row["project"], released_from=row["id"],
                         reason=f"folder '{nf}' removed from set '{row['slug']}'")
        out = {"project": row["project"], **set_info(store, row), **res}
    return out


def rename_set(store: Store, ref, title: str | None = None, slug: str | None = None, note: str | None = None,
               project: str | None = None, actor: str = "cli") -> dict:
    from fundus import locks as _locks
    row = resolve_set(store, ref, project)
    _locks.guard_set(store, row["id"], "renaming")
    ch = {}
    if title and " ".join(title.split()) != row["title"]:
        ch["title"] = " ".join(title.split())
    if slug and _slug(slug) != row["slug"]:
        s = _slug(slug)
        if store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ? AND slug = ?", (row["project"], s)).fetchone():
            raise ValueError(f"slide set '{row['project']}/{s}' exists already")
        ch["slug"] = s
    if note is not None and (note or None) != row["note"]:
        ch["note"] = note or None
    if not ch:
        raise ValueError("nothing to change (give a new title, slug or note)")
    with store.batch(actor):
        store.db.execute(f"UPDATE exhibit_set SET {', '.join(f'{k} = ?' for k in ch)}, updated_at = ? WHERE id = ?",
                         [*ch.values(), _now(), row["id"]])
        store.log("exhibit_set", row["id"], "update", {k: {"old": row[k], "new": v} for k, v in ch.items()})
    new = store.db.execute("SELECT * FROM exhibit_set WHERE id = ?", (row["id"],)).fetchone()
    return {"project": new["project"], **set_info(store, new)}


def set_set_sharing(store: Store, set_ref, sharing: str | None, actor: str = "cli", project: str | None = None) -> dict:
    """Set-level sharing: 'shared', 'project' or None (follow the project setting / policy). Applies to every slide of
    the set without an own setting."""
    if sharing not in (*SHARING, None):
        raise ValueError("sharing must be 'shared', 'project' or 'inherit'")
    row = resolve_set(store, set_ref, project)
    ids = [r[0] for r in store.db.execute("SELECT id FROM exhibit WHERE set_id = ?", (row["id"],))]
    with store.batch(actor):
        store.db.execute("UPDATE exhibit_set SET sharing = ?, updated_at = ? WHERE id = ?", (sharing, _now(), row["id"]))
        store.log("exhibit_set", row["id"], "update", {"sharing": {"old": row["sharing"], "new": sharing}})
        res = recompute(store, ids) if ids else {"public": 0, "internal": 0, "changed": 0}
    return {"project": row["project"], "set": _set_brief(row), "sharing": sharing, "public": res["public"],
            "internal": res["internal"], "changed": res["changed"]}


def list_sets(store: Store, project: str | None = None) -> list[dict]:
    rows = store.db.execute("SELECT * FROM exhibit_set" + (" WHERE project = ?" if project else "") +
                            " ORDER BY project, is_default DESC, created_at, id", ([_slug(project)] if project else [])).fetchall()
    return [{"project": r["project"], **set_info(store, r), **({"note": r["note"]} if r["note"] else {})} for r in rows]


def show_set(store: Store, ref, project: str | None = None) -> dict:
    row = resolve_set(store, ref, project)
    out = {"project": row["project"], **set_info(store, row), **({"note": row["note"]} if row["note"] else {})}
    out["exhibits_list"] = [{k: v for k, v in {"id": r["id"], "slug": r["slug"], "kind": r["kind"], "title": _clip(r["title"], 110),
                                               "versions": r["nv"], "updated_at": r["updated_at"]}.items() if v not in (None, "")}
                            for r in store.db.execute("SELECT e.*, (SELECT count(*) FROM exhibit_version v WHERE v.exhibit_id = e.id) "
                                                      "AS nv FROM exhibit e WHERE e.set_id = ? ORDER BY e.slug", (row["id"],))]
    out["decks"] = [dict(r) for r in store.db.execute(
        "SELECT d.deck, count(*) AS slides, max(d.updated_at) AS updated_at FROM exhibit_deck d JOIN exhibit e ON e.id = d.exhibit_id "
        "WHERE e.set_id = ? GROUP BY d.deck ORDER BY d.deck", (row["id"],))]
    return out


# ------------------------------------------------------------------ registration
def _renumber(store: Store, eid: int, drop: dict[int, int] | None = None) -> dict[int, int]:
    """Version numbers in content-date order (created_at, then insertion): n = 1, 2, … without gaps. `drop` maps version
    numbers to remove onto the number they fold into. Deck memberships follow. Returns {old n: new n}."""
    drop = drop or {}
    vs = store.db.execute("SELECT id, n FROM exhibit_version WHERE exhibit_id = ? ORDER BY created_at, id", (eid,)).fetchall()
    keep = [v for v in vs if v["n"] not in drop]
    new = {v["n"]: i + 1 for i, v in enumerate(keep)}
    for old, into in drop.items():
        while into in drop:
            into = drop[into]
        new[old] = new[into]
    if all(new[v["n"]] == v["n"] for v in vs) and not drop:
        return new
    ids_by_n = {v["n"]: v["id"] for v in vs}
    for old, into in drop.items():
        while into in drop:
            into = drop[into]
        if old in ids_by_n and into in ids_by_n:   # a verified pin follows its duplicate into the kept version
            store.db.execute("UPDATE OR IGNORE exhibit_verified SET version_id = ? WHERE version_id = ?", (ids_by_n[into], ids_by_n[old]))
    for old in drop:
        store.db.execute("DELETE FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, old))
    for v in keep:   # two steps: UNIQUE(exhibit_id, n)
        store.db.execute("UPDATE exhibit_version SET n = ? WHERE id = ?", (-new[v["n"]], v["id"]))
    store.db.execute("UPDATE exhibit_version SET n = -n WHERE exhibit_id = ? AND n < 0", (eid,))
    for d in store.db.execute("SELECT rowid, version FROM exhibit_deck WHERE exhibit_id = ? AND version IS NOT NULL", (eid,)).fetchall():
        if d["version"] in new and new[d["version"]] != d["version"]:
            store.db.execute("UPDATE exhibit_deck SET version = ? WHERE rowid = ?", (new[d["version"]], d["rowid"]))
    for d in store.db.execute("SELECT sequence_id, position, version FROM exhibit_sequence_item WHERE exhibit_id = ? "
                              "AND version IS NOT NULL", (eid,)).fetchall():
        if d["version"] in new and new[d["version"]] != d["version"]:
            store.db.execute("UPDATE exhibit_sequence_item SET version = ? WHERE sequence_id = ? AND position = ?",
                             (new[d["version"]], d["sequence_id"], d["position"]))
    return new


def fold_script_versions(store: Store, apply: bool = False, actor: str = "cli") -> dict:
    """Clean-up for the old rule 'a script edit is a new version': versions whose PNG and HTML equal an earlier version
    of the same exhibit are folded into it (its script becomes the newest one), numbers are renumbered by content date
    and deck memberships follow. Without apply only reports."""
    from fundus import locks as _locks
    out = {"exhibits": 0, "versions_removed": 0, "renumbered": 0, "examples": []}
    eids = [r[0] for r in store.db.execute("SELECT id FROM exhibit ORDER BY id") if not _locks.exhibit_lock(store, r[0])]
    with store.batch(actor):
        for eid in eids:
            vs = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? ORDER BY created_at, id", (eid,)).fetchall()
            first: dict[tuple, object] = {}
            drop: dict[int, int] = {}
            newest_script: dict[int, object] = {}
            for v in vs:
                k = (v["png_sha"], v["html_sha"] or "")
                if k in first:
                    drop[v["n"]] = first[k]["n"]
                    if v["script_sha"]:
                        newest_script[first[k]["id"]] = v
                else:
                    first[k] = v
            order_ok = [v["n"] for v in vs] == sorted(v["n"] for v in vs)
            if not drop and order_ok:
                continue
            out["exhibits"] += 1
            out["versions_removed"] += len(drop)
            out["renumbered"] += 0 if order_ok else 1
            if len(out["examples"]) < 8:
                out["examples"].append({"id": eid, "versions": len(vs), "after": len(vs) - len(drop), "fold": drop})
            if not apply:
                continue
            for vid, v in newest_script.items():
                store.db.execute("UPDATE exhibit_version SET script_sha = ?, script_path = ? WHERE id = ?",
                                 (v["script_sha"], v["script_path"], vid))
            mapping = _renumber(store, eid, drop)
            store.log("exhibit", eid, "update", {"fold_versions": {str(k): v for k, v in mapping.items() if str(k) != str(v)},
                                                 "removed": sorted(drop)})
            _refresh(store, eid)
        if not apply:
            store.db.rollback()
    out["applied"] = apply
    return out


def _refresh(store: Store, eid: int) -> None:
    """Exhibit row from its versions: current = highest n; text fields from the newest version with extracted text
    (a PNG-only copy never wipes the title); status 'final' once it sits in a deck."""
    vs = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? ORDER BY n", (eid,)).fetchall()
    if not vs:
        return
    cur = vs[-1]
    txt = next((v for v in reversed(vs) if v["extract"]), None)
    ex = json.loads(txt["extract"]) if txt else {}
    row = store.db.execute("SELECT * FROM exhibit WHERE id = ?", (eid,)).fetchone()
    deck = store.db.execute("SELECT deck FROM exhibit_deck WHERE exhibit_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                            (eid,)).fetchone()
    title = ex.get("title") or row["slug"].replace("-", " ").capitalize()
    fields = {
        "kind": (txt["kind"] if txt else cur["kind"]) or "slide", "title": _clip(title, 400),
        "eyebrow": _clip(ex.get("eyebrow"), 300) or None, "meta": _clip(ex.get("meta"), 600) or None,
        "so_what": _clip(ex.get("so_what"), 800) or None,
        "chart_types": ", ".join(ex.get("chart_types") or []) or None,
        "chart_heads": _clip(" | ".join(ex.get("chart_heads") or []), 1200) or None,
        "labels": _clip(" · ".join(ex.get("labels") or []), 2000) or None,
        "sources": _clip(" · ".join((ex.get("sources") or []) + (ex.get("sources_more") or [])), 2000) or None,
        "notes": _clip(" · ".join(ex.get("notes") or []), 1200) or None,
        "body_text": _clip(ex.get("body"), 4000) or None,
        "palette": cur["palette"] or (txt["palette"] if txt else None), "language": ex.get("language"),
        "deck": deck[0] if deck else None, "current_version": cur["n"], "sources_public": sources_public(store, ex),
        "created_at": vs[0]["created_at"], "updated_at": cur["created_at"],
    }
    if row["status"] == "draft" and deck:
        fields["status"] = "final"
    store.db.execute(f"UPDATE exhibit SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), eid])
    store._touch("exhibit", eid)


def _membership(store: Store, project: str, deck: str, png: Path, eid: int, n: int) -> None:
    pos, slot = order_prefix(png.name)
    store.db.execute("INSERT INTO exhibit_deck(project, deck, file, deck_path, position, slot, exhibit_id, version, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(project, deck, file) DO UPDATE SET deck_path = excluded.deck_path, "
                     "position = excluded.position, slot = excluded.slot, exhibit_id = excluded.exhibit_id, "
                     "version = excluded.version, updated_at = excluded.updated_at",
                     (project, deck, png.name, str(png.parent), pos, slot, eid, n, _now()))


def register(store: Store, html=None, png=None, script=None, project: str | None = None, deck: str | None = None,
             note: str = "", mtime: float | None = None, dom: str | None = None, density: dict | None = None,
             find: bool = True, actor: str = "exhibits", set_ref: str | None = None, fresh: bool = False,
             caller: str | None = None) -> dict:
    """Register one render. png is required; html (or an .svg source) optional (PNG-only deck copies). The slide set is
    the one with the longest folder prefix containing the PNG's project-relative path, else the project's default set
    (created on the project's first registration); `set_ref` (set slug or id) overrides the path. Returns {exhibit_id, project,
    set, set_id, slug, version, status: new_exhibit | new_version | duplicate | deck_copy | locked, deck}. A locked slide
    or set (fundus.locks) takes no new version and no new slide: status 'locked' with the reason, nothing is written.
    fresh: a deliberate copy – a byte-identical slide elsewhere in the set does not absorb it."""
    if not png:
        raise ValueError("png is required")
    # source paths are stored as realpath (a renamed project folder kept as a symlink resolves to its new name)
    png = Path(os.path.realpath(os.path.expanduser(str(png))))
    if not png.is_file():
        raise ValueError(f"PNG not found: {png}")
    hpath = Path(os.path.realpath(os.path.expanduser(str(html)))) if html else None
    if hpath and not hpath.is_file():
        raise ValueError(f"HTML not found: {hpath}")
    if not project and not under_projects(png):
        # a render outside the project folders (session scratchpad, /tmp, Downloads): never guess a project from the folder
        # name ('scratchpad'). A copy of a registered render points to it; anything else needs project=… or the project folder.
        sha = sha256(png.read_bytes())
        known = store.db.execute("SELECT e.id, e.project, e.slug, v.n FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
                                 "WHERE v.png_sha = ? ORDER BY v.id LIMIT 1", (sha,)).fetchone()
        if known:
            return {"exhibit_id": known["id"], "project": known["project"], "slug": known["slug"], "version": known["n"],
                    "status": "duplicate", "note": f"copy outside the project folders – already registered as {known['project']}/{known['slug']} v{known['n']}"}
        raise ValueError(f"{png} is outside ~/Projects – render into the project folder (~/Projects/<project>/…) or pass project=…; "
                         "Fundus does not guess a project from a temporary folder")
    proj, root = detect_project(png)
    project = _slug(project) if project else proj
    rel_proj, rel_png = project_rel(png)
    rel_png = rel_png if rel_proj == project else None
    png_bytes = png.read_bytes()
    width, height = png_size(png_bytes)
    html_text = hpath.read_text(encoding="utf-8", errors="replace") if hpath else None
    html_ext = ("svg" if hpath.suffix.lower() == ".svg" else "html") if hpath else None
    stem = (hpath or png).stem
    slug = exhibit_slug(stem)
    try:
        rel_dir = png.parent.relative_to(root)
    except ValueError:
        rel_dir = Path(png.parent.name)
    deck = deck or auto_deck(rel_dir)
    created = _ts(mtime if mtime is not None else png.stat().st_mtime)
    # caller: the Python script that ran render.sh (passed by the hook) – taken when it is this slide's script; it beats a
    # search that could pick a same-named script elsewhere or a helper listing every slug
    cpath = Path(os.path.realpath(caller)) if caller else None
    if not script and cpath and cpath.is_file() and caller_matches(cpath, stem):
        script = cpath
    spath = Path(os.path.realpath(os.path.expanduser(str(script)))) if script else (find_script(hpath or png, root, stem) if find else None)
    script_bytes = spath.read_bytes() if spath and spath.is_file() else None
    if script_bytes is None:
        spath = None
    # files first (content-addressed, idempotent), then one transaction for the rows
    png_sha = put_file(store, png_bytes, "png")
    html_sha = put_file(store, html_text.encode("utf-8"), html_ext) if html_text is not None else None
    script_sha = put_file(store, script_bytes, "py") if script_bytes is not None else None
    _, pix_palette = make_preview(store, png_sha)
    ex = None
    if html_text is not None:
        ex = extract(html_text) if html_ext == "html" else _extract_svg(html_text)
        ex["is_html"] = html_ext == "html"
    fit, dens = dom_facts(dom) if dom else (None, None)
    if density and not dens:
        dens = density_facts(density)
    if dens and fit is None:
        fit = dens.get("fit")
    palette = ((ex or {}).get("palette") or pix_palette or
               ("hsg" if re.search(r"hsg", str(rel_dir), re.I) else "zhaw" if re.search(r"zhaw", str(rel_dir), re.I)
                else "blue" if re.search(r"blue", str(rel_dir), re.I) else None))
    kind = classify(slug, width, height, ex)
    words = (dens or {}).get("words") or (ex or {}).get("words")
    with store.batch(actor):
        set_id = resolve_set(store, set_ref, project)["id"] if set_ref not in (None, "") else set_for_path(store, project, rel_png)
        from fundus import trash as _trash
        tstatus, moved_to, tnote = _trash.check_register(store, project, set_id, slug, png_sha)
        if tstatus:        # in the trash or deleted for good: the file on disk must not bring it back
            return {"exhibit_id": moved_to, "project": project, "set_id": set_id, "slug": slug, "version": None,
                    "status": tstatus, "note": tnote}
        if moved_to:       # moved by a session or in the portal: the render belongs to the slide at its new place
            m = store.db.execute("SELECT project, set_id, slug FROM exhibit WHERE id = ?", (moved_to,)).fetchone()
            project, set_id, slug = m["project"], m["set_id"], m["slug"]
        else:              # a set moved to another project keeps its folders: the slide belongs to the set's project
            project = store.db.execute("SELECT project FROM exhibit_set WHERE id = ?", (set_id,)).fetchone()[0]
        row = store.db.execute("SELECT * FROM exhibit WHERE project = ? AND set_id = ? AND slug = ?",
                               (project, set_id, slug)).fetchone()
        dup = None
        if row is not None:   # same PNG and HTML = same version, whatever the script (a script edit alone is not new content)
            dup = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND png_sha = ? AND coalesce(html_sha, '') = "
                                   "coalesce(?, '') ORDER BY coalesce(script_sha, '') = coalesce(?, '') DESC, n DESC LIMIT 1",
                                   (row["id"], png_sha, html_sha, script_sha)).fetchone()
        if dup is None and not fresh:   # byte-identical copy (same PNG, same HTML if given) somewhere in the same slide set
            cand = store.db.execute(
                "SELECT v.* FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id WHERE e.project = ? AND e.set_id = ? "
                "AND v.png_sha = ?" + (" AND v.html_sha = ?" if html_sha else "") + " ORDER BY (e.slug = ?) DESC, v.n DESC LIMIT 1",
                [project, set_id, png_sha, *([html_sha] if html_sha else []), slug]).fetchone()
            if cand is not None and (row is None or cand["exhibit_id"] != row["id"] or html_sha is None or script_sha is None
                                     or cand["script_sha"] == script_sha):
                dup = cand
        from fundus import locks as _locks
        if dup is None:
            try:
                if row is not None:
                    _locks.guard_exhibit(store, row["id"], "a new version")
                    _locks.guard_review(store, row["id"], "a new version")
                _locks.guard_set(store, set_id, "a new slide")
            except _locks.Locked as e:
                return {"exhibit_id": row["id"] if row is not None else None, "project": project, "set_id": set_id, "slug": slug,
                        "version": row["current_version"] if row is not None else None, "status": "locked", "note": str(e)}
        frozen = dup is not None and _locks.exhibit_lock(store, dup["exhibit_id"])
        if dup is not None:
            eid, n = dup["exhibit_id"], dup["n"]
            status = "deck_copy" if deck else "duplicate"
            # archived (frozen): the version stays exactly as locked – no new script pointer, no measurements
            if not frozen and script_sha and dup["script_sha"] != script_sha and dup["exhibit_id"] == (row["id"] if row is not None else None):
                # the script changed but still renders this content: the version points to the script as it is now
                store.db.execute("UPDATE exhibit_version SET script_sha = ?, script_path = ? WHERE id = ?",
                                 (script_sha, str(spath) if spath else None, dup["id"]))
                store.log("exhibit", eid, "update", {"n": n, "script": {"old": (dup["script_sha"] or "")[:12], "new": script_sha[:12]}})
            if dup["fit"] is None and (fit is not None or dens) and not frozen:
                store.db.execute("UPDATE exhibit_version SET fit = coalesce(fit, ?), density = coalesce(density, ?), "
                                 "words = coalesce(?, words) WHERE id = ?",
                                 (fit, json.dumps(dens) if dens else None, words, dup["id"]))
        else:
            if row is None:
                eid = store.db.execute("INSERT INTO exhibit(project, set_id, slug, kind, title, created_at, updated_at) "
                                       "VALUES (?,?,?,?,?,?,?)",
                                       (project, set_id, slug, kind, (ex or {}).get("title") or slug, created, created)).lastrowid
                status = "new_exhibit"
            else:
                eid, status = row["id"], "new_version"
            n = store.db.execute("SELECT coalesce(max(n), 0) + 1 FROM exhibit_version WHERE exhibit_id = ?", (eid,)).fetchone()[0]
            store.db.execute(
                "INSERT INTO exhibit_version(exhibit_id, n, png_sha, html_sha, html_ext, script_sha, png_path, html_path, "
                "script_path, width, height, palette, kind, title, fit, density, words, extract, created_at, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, n, png_sha, html_sha, html_ext, script_sha, str(png), str(hpath) if hpath else None,
                 str(spath) if spath else None, width, height, palette, kind, _clip((ex or {}).get("title"), 400) or None,
                 fit, json.dumps(dens) if dens else None, words,
                 json.dumps({k: v for k, v in ex.items() if k not in ("is_html",)}, ensure_ascii=False) if ex else None,
                 created, note or None))
            store.log("exhibit", eid, "insert" if status == "new_exhibit" else "version",
                      {"n": n, "png": png_sha[:12], "file": png.name})
            n = _renumber(store, eid).get(n, n)   # an older file found later goes to its place by content date
        if deck:
            _membership(store, project, deck, png, eid, n)
        _refresh(store, eid)
        if dup is None and ex is not None:   # links follow the newest version with text
            store.db.execute("DELETE FROM exhibit_link WHERE exhibit_id = ? AND source = 'rule'", (eid,))
            st_text = script_bytes.decode("utf-8", "replace") if script_bytes else None
            store.db.executemany("INSERT OR IGNORE INTO exhibit_link(exhibit_id, target_kind, target_id, role, evidence) "
                                 "VALUES (?,?,?,?,?)", [(eid, k, i, r, ev) for k, i, r, ev in compute_links(store, ex, st_text)])
            link_entities(store, eid, ex, project)
            _photo_uses(store, eid, html_text, project)
        elif status == "new_exhibit":   # PNG-only exhibit: at least the client as subject
            link_entities(store, eid, None, project)
        recompute(store, [eid])
        set_slug = store.db.execute("SELECT slug FROM exhibit_set WHERE id = (SELECT set_id FROM exhibit WHERE id = ?)",
                                    (eid,)).fetchone()[0]
    return {"exhibit_id": eid, "project": project, "set": set_slug, "set_id": set_id, "slug": slug, "version": n,
            "status": status, "deck": deck,
            "kind": kind, "palette": palette, "script": str(spath) if spath else None}


# <img … data-fundus-photo="photo:12"> (mck_photo.photo_img) and <video … data-fundus-video="video:7"> (mck_video.video_html):
# photos and videos of the media archive on the slide – their use is recorded.
PHOTO_REF_RE = re.compile(r"""data-fundus-(?:photo|video)=["'](?:photo|video):(\d+)["']""")


def _photo_uses(store: Store, eid: int, html_text: str | None, project: str | None) -> None:
    for pid in sorted({int(m) for m in PHOTO_REF_RE.findall(html_text or "")}):
        if not store.db.execute("SELECT 1 FROM photo WHERE id = ?", (pid,)).fetchone():
            continue
        if not store.db.execute("SELECT 1 FROM photo_use WHERE photo_id = ? AND exhibit_id = ?", (pid, eid)).fetchone():
            store.db.execute("INSERT INTO photo_use(photo_id, exhibit_id, ref, project) VALUES (?, ?, 'render', ?)", (pid, eid, project))


class _SvgText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        t = " ".join(data.split())
        if t and not self.skip:
            self.parts.append(t)


def _extract_svg(svg: str) -> dict:
    p = _SvgText()
    try:
        p.feed(svg)
    except Exception:
        pass
    parts = list(dict.fromkeys(p.parts))
    body = " ".join(parts)
    return {"eyebrow": "", "title": parts[0] if parts else "", "meta": "", "chart_heads": [], "labels": parts[1:200],
            "so_what": "", "kicker": "", "body": body, "sources": [], "sources_more": [], "notes": [], "chart_types": [],
            "palette": _palette_from_text(svg), "language": _lang(body), "has_h1": True,
            "words": len(re.findall(r"\w+", body))}


# ------------------------------------------------------------------ backfill
def _slide_shaped(w, h) -> bool:
    """16:9 (±2 %) and at least 1280 px wide – the shape of a rendered slide or a deck page export."""
    return bool(w and h and w >= 1280 and abs(w * 9 / h - 16) <= 0.32)


def scan(path) -> tuple[list[dict], list[dict]]:
    """Candidate renders under a folder: (items, skipped). An item is an HTML/PNG pair, an SVG/PNG pair or a PNG alone;
    logo/art/skill folders, contact sheets, previews, tests and HTML without PNG (poster sources) are skipped."""
    items, skipped = [], []
    for d, dirs, files in os.walk(path):
        dirs[:] = sorted(x for x in dirs if not SKIP_DIR_RE.match(x))
        fs = set(files)
        stems_png = {f[:-4] for f in fs if f.lower().endswith(".png")}
        for f in sorted(fs):
            low = f.lower()
            stem, ext = os.path.splitext(f)
            if ext.lower() not in (".html", ".png"):
                continue
            if SKIP_FILE_RE.search(stem):
                skipped.append({"file": os.path.join(d, f), "reason": "contact sheet/preview/test"})
                continue
            if low.endswith(".html"):
                if stem not in stems_png:
                    skipped.append({"file": os.path.join(d, f), "reason": "HTML without PNG"})
                continue
            p = os.path.join(d, f)
            data = Path(p).read_bytes()
            w, h = png_size(data)
            if w is not None and w < 600:
                skipped.append({"file": p, "reason": f"small image {w}x{h} (icon/logo)"})
                continue
            src = None
            if stem + ".html" in fs:
                src = os.path.join(d, stem + ".html")
            elif stem + ".svg" in fs:
                src = os.path.join(d, stem + ".svg")
            items.append({"png": p, "html": src, "mtime": os.stat(p).st_mtime, "png_sha": sha256(data),
                          "slide_shaped": _slide_shaped(w, h), "size": f"{w}x{h}"})
    # website pictures, page renders, figure crops, assets: a PNG without its source counts only when it has the shape
    # of a rendered slide or is a copy of a render found here (Falk, 1 Nov 2026: 348 NECTAR website images were "posters")
    with_src = {i["png_sha"] for i in items if i["html"]}
    keep = []
    for i in items:
        if i["html"] or i["slide_shaped"] or i["png_sha"] in with_src:
            keep.append(i)
        else:
            skipped.append({"file": i["png"], "reason": f"image without source, not slide-shaped ({i['size']})"})
    return keep, skipped


def import_dir(store: Store, path, project: str | None = None, deck: str | None = None, measure: bool = False,
               actor: str = "importer:exhibits", progress=None, set_ref: str | None = None) -> dict:
    """Backfill: register every render under `path`, oldest first (file mtime), so older copies (versions/, _archive/,
    deck_v1 …) become older versions. PNG-only files identical to a render with HTML are registered last (deck
    memberships). Read-only on the folder; files are copied into the Fundus store."""
    path = Path(os.path.realpath(os.path.expanduser(str(path))))
    if not path.is_dir():
        raise ValueError(f"not a folder: {path}")
    items, skipped = scan(path)
    with_src = {i["png_sha"] for i in items if i["html"]}
    first = sorted((i for i in items if i["html"] or i["png_sha"] not in with_src), key=lambda i: (i["mtime"], i["png"]))
    later = sorted((i for i in items if not i["html"] and i["png_sha"] in with_src), key=lambda i: (i["mtime"], i["png"]))
    dens = measure_many([i["html"] for i in first if i["html"] and i["html"].endswith(".html")]) if measure else {}
    counts = {"files": len(items), "new_exhibit": 0, "new_version": 0, "duplicate": 0, "deck_copy": 0, "failed": 0}
    failed, touched = [], set()
    for k, it in enumerate(first + later):
        try:
            r = register(store, it["html"], it["png"], project=project, deck=deck, mtime=it["mtime"],
                         density=dens.get(it["html"]) if it["html"] else None, actor=actor, set_ref=set_ref)
            counts[r["status"]] = counts.get(r["status"], 0) + 1   # also locked, trashed, deleted …
            touched.add(r["exhibit_id"])
        except Exception as e:
            counts["failed"] += 1
            failed.append({"file": it["png"], "error": f"{type(e).__name__}: {e}"[:200]})
        if progress and (k + 1) % 50 == 0:
            progress(k + 1, len(items))
    ids = sorted(touched)
    proj = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for p, n_ex, n_v in store.db.execute(f"SELECT e.project, count(DISTINCT e.id), count(v.id) FROM exhibit e JOIN exhibit_version v "
                                             f"ON v.exhibit_id = e.id WHERE e.id IN ({marks}) GROUP BY e.project", ids):
            proj[p] = {"logical": n_ex, "versions": n_v}
        for p, n_d in store.db.execute(f"SELECT project, count(*) FROM exhibit_deck WHERE exhibit_id IN ({marks}) GROUP BY project", ids):
            proj.setdefault(p, {})["deck_memberships"] = n_d
        for p, n_var in store.db.execute(f"SELECT project, count(*) FROM (SELECT e.project FROM exhibit e JOIN exhibit_version v "
                                         f"ON v.exhibit_id = e.id WHERE e.id IN ({marks}) AND v.palette IS NOT NULL GROUP BY e.id "
                                         f"HAVING count(DISTINCT v.palette) > 1) GROUP BY project", ids):
            proj.setdefault(p, {})["palette_variants"] = n_var
        for p, kind, n in store.db.execute(f"SELECT project, kind, count(*) FROM exhibit WHERE id IN ({marks}) GROUP BY 1, 2", ids):
            proj.setdefault(p, {}).setdefault("by_kind", {})[kind] = n
    return {"path": str(path), "counts": counts, "projects": proj, "failed": failed, "skipped": skipped}


def project_roots(store: Store) -> list[Path]:
    """Project folders under PROJECTS_ROOT that already hold registered renders (the project folder of each version's PNG,
    old or category layout; a folder reached through a compatibility symlink counts once, by its real path)."""
    root = Path(os.path.realpath(PROJECTS_ROOT))
    out = set()
    for (pth,) in store.db.execute("SELECT DISTINCT png_path FROM exhibit_version WHERE png_path IS NOT NULL"):
        try:
            rel = Path(pth).relative_to(root)
        except ValueError:
            continue
        hit = project_at(rel)
        if hit and len(rel.parts) > hit[1]:
            d = root.joinpath(*rel.parts[:hit[1]])
            if d.is_dir():
                out.add(Path(os.path.realpath(d)))
    return sorted(out)


def project_roots_by_slug(store: Store) -> dict[str, Path]:
    """{project slug: folder} for the project folders with registered renders."""
    return {detect_project(d / "_")[0]: d for d in project_roots(store)}


def sync_projects(store: Store, actor: str = "importer:exhibits", progress=None) -> dict:
    """Nightly catch-up: re-scan every project folder that holds registered renders, so renders and deck folders that
    did not pass render.sh (copies, edits, other tools) are registered too. Idempotent: known files are duplicates."""
    out = {}
    for d in project_roots(store):
        try:
            r = import_dir(store, d, actor=actor, progress=progress)
            out[d.name] = {**r["counts"], **({"failed_files": r["failed"][:10]} if r["failed"] else {})}
        except Exception as e:
            store.db.rollback()
            out[d.name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    try:   # deck PDFs: the slide order as the session built it (fundus/decks.py)
        from fundus import decks
        out["_pdf_decks"] = decks.sync_pdfs(store, actor=actor)
    except Exception as e:
        store.db.rollback()
        out["_pdf_decks"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return out


# ------------------------------------------------------------------ read
_ROW = ("SELECT e.*, s.slug AS set_slug, s.title AS set_title, s.is_default AS set_default FROM exhibit e "
        "JOIN exhibit_set s ON s.id = e.set_id")


def _resolve(store: Store, ref, include_internal=False, project=None):
    """Exhibit row (with set_slug, set_title, set_default) for an id, 'project/set/slug', 'project/slug' (the default
    set's slide, or the only one with that slug across the project's sets) or a slug (unique among the visible ones)."""
    from fundus import query as q
    ref = str(ref).strip()
    rows = []
    parts = ref.split("/")
    if re.fullmatch(r"\d+", ref):
        rows = store.db.execute(_ROW + " WHERE e.id = ?", (int(ref),)).fetchall()
    elif len(parts) >= 3:
        p, st, sl = parts[0], parts[1], "/".join(parts[2:])
        rows = store.db.execute(_ROW + " WHERE e.project = ? AND (s.slug = ? OR CAST(s.id AS TEXT) = ?) AND e.slug = ?",
                                (_slug(p), _slug(st), st, exhibit_slug(sl))).fetchall()
    elif len(parts) == 2:
        rows = store.db.execute(_ROW + " WHERE e.project = ? AND e.slug = ? ORDER BY s.is_default DESC, e.id",
                                (_slug(parts[0]), exhibit_slug(parts[1]))).fetchall()
    else:
        sl = exhibit_slug(ref)
        rows = store.db.execute(_ROW + " WHERE e.slug = ?" + (" ORDER BY e.project = ? DESC, s.is_default DESC" if project else ""),
                                (sl, *([project] if project else []))).fetchall()
        if not rows:
            rows = store.db.execute(_ROW + " WHERE e.slug LIKE ? ORDER BY length(e.slug) LIMIT 20", (f"%{sl}%",)).fetchall()
    if not rows:
        raise LookupError(f"no exhibit '{ref}' (try fundus_search with kind='exhibit')")
    vis = [r for r in rows if q.is_visible(store, "exhibit", r, include_internal, project)]
    if not vis:
        raise PermissionError(f"exhibit '{ref}' is internal; pass include_internal=true or its project")
    if len(vis) > 1:
        cand = [r for r in vis if r["slug"] == exhibit_slug(parts[-1])] or vis
        if project and any(r["project"] == project for r in cand):
            cand = [r for r in cand if r["project"] == project]
        if len(cand) > 1 and len({r["project"] for r in cand}) == 1:   # one project, several sets: its default set wins
            dflt = [r for r in cand if r["set_default"]]
            cand = dflt if len(dflt) == 1 else cand
        if len(cand) != 1:
            raise LookupError(f"'{ref}' matches several exhibits: " +
                              ", ".join(f"{r['id']} {r['project']}/{r['set_slug']}/{r['slug']}" for r in cand[:12]) +
                              " – use the id or project/set/slug")
        vis = cand
    return vis[0]


def _density_brief(v) -> str | None:
    try:
        return json.loads(v["density"]).get("verdict") if v["density"] else None
    except ValueError:
        return None


def version_urls(v, project: str, internal: bool) -> dict:
    return {k: x for k, x in {"preview": url(v["png_sha"], "preview.png", project, internal),
                              "png": url(v["png_sha"], "png", project, internal),
                              "html": url(v["html_sha"], v["html_ext"] or "html", project, internal),
                              "script": url(v["script_sha"], "py", project, internal)}.items() if x}


def get(store: Store, ref, include_internal: bool = False, project: str | None = None, script: bool = True,
        max_script: int = MAX_SCRIPT) -> dict:
    """Everything about one exhibit: metadata, all versions (with palette, fit, density verdict, file paths and URLs),
    decks, links, URLs of the current render and the generating script's source text (to adapt it)."""
    row = _resolve(store, ref, include_internal, project)
    eid = row["id"]
    internal = row["visibility"] == "internal"
    vs = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? ORDER BY n", (eid,)).fetchall()
    cur = vs[-1] if vs else None
    out = {k: row[k] for k in ("id", "project", "slug", "kind", "title", "eyebrow", "meta", "so_what", "chart_types",
                               "chart_heads", "sources", "notes", "palette", "language", "deck", "status", "visibility",
                               "released", "current_version", "created_at", "updated_at") if row[k] not in (None, "")}
    out["kind_label"] = "exhibit"
    out["set"] = {"id": row["set_id"], "slug": row["set_slug"], "title": row["set_title"], "is_default": bool(row["set_default"])}
    out["labels"] = _clip(row["labels"], 600) if row["labels"] else None
    if cur is not None:
        out["urls"] = version_urls(cur, row["project"], internal)
        out["size"] = f"{cur['width']}x{cur['height']}" if cur["width"] else None
    variants = {}
    for v in vs:
        if v["palette"]:
            variants[v["palette"]] = v["n"]
    out["variants"] = variants
    out["versions"] = [{k: x for k, x in {
        "n": v["n"], "created_at": v["created_at"], "palette": v["palette"], "kind": v["kind"],
        "title": v["title"] if v["title"] and v["title"] != row["title"] else None, "fit": v["fit"],
        "density": _density_brief(v), "words": v["words"], "note": v["note"], "png_path": v["png_path"],
        "html_path": v["html_path"], "script_path": v["script_path"], "png_sha": v["png_sha"][:12],
        "source": "png only" if not v["html_sha"] else None, "urls": version_urls(v, row["project"], internal)}.items()
        if x not in (None, "", {})} for v in vs]
    out["decks"] = [dict(r) for r in store.db.execute(
        "SELECT deck, file, position, slot, version, deck_path FROM exhibit_deck WHERE exhibit_id = ? ORDER BY deck, position",
        (eid,))]
    links = []
    for k, tid, role, ev in store.db.execute("SELECT target_kind, target_id, role, evidence FROM exhibit_link "
                                             "WHERE exhibit_id = ? ORDER BY role, target_kind", (eid,)):
        lab = None
        if k == "entity":
            r = store.db.execute("SELECT name FROM entity WHERE id = ?", (int(tid),)).fetchone()
            lab = r[0] if r else None
        elif k == "document":
            r = store.db.execute("SELECT d.title, p.name FROM document d LEFT JOIN publisher p ON p.id = d.publisher_id "
                                 "WHERE d.id = ?", (int(tid),)).fetchone()
            lab = f"{r[1] + ': ' if r and r[1] else ''}{r[0]}" if r else None
        links.append({"role": role, "kind": k, "id": tid, "label": _clip(lab, 140) if lab else None, "evidence": _clip(ev, 120)})
    out["links"] = [{k: v for k, v in l.items() if v} for l in links]
    if script and cur is not None:
        sv = next((v for v in reversed(vs) if v["script_sha"]), None)
        if sv is not None:
            p = file_path(store, sv["script_sha"], "py")
            text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
            out["script"] = {"path": sv["script_path"], "version": sv["n"], "sha": sv["script_sha"][:12],
                             "url": url(sv["script_sha"], "py", row["project"], internal),
                             "text": text[:max_script], **({"truncated": len(text)} if len(text) > max_script else {})}
    from fundus import reviews as _rv
    rs = _rv.slide_status(store, ids=[eid]).get(eid) or {}
    if rs.get("stage"):
        out["review_stage"], out["confidence"] = rs["stage"], rs["level"]
    if rs.get("verified"):
        out["verified_version"] = _verified_brief(store, row, rs["verified"], internal)
    pins = {r[0]: r[1] for r in store.db.execute("SELECT v.n, p.stage FROM exhibit_verified p JOIN exhibit_version v ON v.id = p.version_id "
                                                 "WHERE p.exhibit_id = ?", (eid,))}
    for v in out.get("versions") or []:
        if v["n"] in pins:
            v["verified"] = pins[v["n"]]
    from fundus import locks as _locks
    if "archived_at" in row.keys() and row["archived_at"]:
        out["archived"] = {"at": row["archived_at"], "by": row["archived_by"],
                           "hint": "in the archive: findable, not listed in its project – fundus_exhibit_archive(action='unarchive') lists it again"}
    if "favorite_at" in row.keys() and row["favorite_at"]:
        out["favorite"] = {"at": row["favorite_at"], "by": row["favorite_by"]}
    if row["project"] == TRASH_PROJECT:      # in the trash: say where it came from and how to get it back
        t = store.db.execute("SELECT id, ref, expires_at FROM trash WHERE state = 'trashed' AND EXISTS (SELECT 1 FROM "
                             "json_each(trash.exhibits) j WHERE j.value = ?) ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
        if t:
            out["trashed"] = {"trash_id": t["id"], "from": t["ref"], "purged_after": t["expires_at"],
                              "hint": "in the trash – fundus_exhibit_trash(action='restore', trash_id=…) brings it back"}
    lk = _locks.exhibit_lock(store, eid)
    if lk:
        out["locked"] = {**lk, "hint": "archived: no new version, no move – copy it to work on it (fundus_exhibit_copy)"}
    rl = _locks.review_locks(store, [eid]).get(eid)
    if rl:
        out["in_review"] = rl            # frozen while these reviews run (no new version until they end)
    out["policy"] = policy(store)
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def versions(store: Store, ref, include_internal: bool = False, project: str | None = None) -> dict:
    d = get(store, ref, include_internal, project, script=False)
    return {"id": d["id"], "project": d["project"], "set": d.get("set"), "slug": d["slug"], "title": d.get("title"),
            "versions": d.get("versions", []), "decks": d.get("decks", [])}


def listing(store: Store, project: str | None = None, kind: str | None = None, limit: int = 100,
            include_internal: bool = False, set_ref: str | None = None) -> list[dict]:
    from fundus import query as q
    where, params = [], []
    if project:
        where.append("x.project = ?")
        params.append(_slug(project))
    if set_ref not in (None, ""):
        where.append("x.set_id = ?")
        params.append(resolve_set(store, set_ref, project)["id"])
    if kind:
        where.append("x.kind = ?")
        params.append(kind)
    vs, vp = q.vis_sql("exhibit", "x", include_internal, _slug(project) if project else None)
    sql = ("SELECT x.*, s.slug AS set_slug, s.title AS set_title, "
           "(SELECT count(*) FROM exhibit_version v WHERE v.exhibit_id = x.id) AS nv, "
           "(SELECT count(*) FROM exhibit_deck d WHERE d.exhibit_id = x.id) AS nd FROM exhibit x "
           "JOIN exhibit_set s ON s.id = x.set_id WHERE 1=1"
           + "".join(f" AND {w}" for w in where) + vs + " ORDER BY x.project, s.is_default DESC, s.slug, x.slug LIMIT ?")
    return [{k: v for k, v in {"id": r["id"], "project": r["project"], "set": r["set_slug"], "set_title": r["set_title"],
                               "slug": r["slug"], "kind": r["kind"],
                               "title": _clip(r["title"], 110), "versions": r["nv"], "decks": r["nd"], "palette": r["palette"],
                               "visibility": r["visibility"] if r["visibility"] == "internal" else None,
                               "updated_at": r["updated_at"]}.items() if v not in (None, "")}
            for r in store.db.execute(sql, [*params, *vp, int(limit)])]


def _gallery_sets(store: Store, project: str) -> list[dict]:
    return [set_info(store, r) for r in store.db.execute("SELECT * FROM exhibit_set WHERE project = ? "
                                                         "ORDER BY is_default DESC, created_at, id", (project,))]


def gallery(store: Store, project: str | None = None, set_ref: str | None = None) -> dict:
    """Data of the web gallery / portal. Without project: one row per project (own title, slides, versions, decks, internal, sharing)
    with its slide sets [{id, slug, title, sharing, is_default, exhibits, versions, folders, updated_at}]. With project:
    its sets, its slides (current version: preview/PNG/HTML URLs, density verdict, sharing state, set_id) and its decks in
    slide order (each with the set_id of its slides) – only those of one set when set_ref (id or slug) is given. Admin
    view: internal exhibits are included and marked."""
    pol = policy(store)
    if not project:
        vers = dict(store.db.execute("SELECT e.project, count(*) FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
                                     "GROUP BY e.project").fetchall())
        decks = dict(store.db.execute("SELECT project, count(DISTINCT deck) FROM exhibit_deck GROUP BY project").fetchall())
        rows = store.db.execute("SELECT project, sum(archived_at IS NULL) AS n, sum(archived_at IS NOT NULL) AS archived, "
                                "sum(favorite_at IS NOT NULL) AS favorites, "
                                "sum(visibility = 'internal') AS internal, "
                                "sum(released) AS released, max(coalesce(updated_at, created_at)) AS updated_at "
                                "FROM exhibit WHERE project != ? GROUP BY project ORDER BY n DESC, project", (TRASH_PROJECT,)).fetchall()
        proj = project_sharing(store)
        names = project_titles(store)
        out = [{"project": r["project"], "title": names.get(r["project"]), "sharing": proj.get(r["project"]), "exhibits": r["n"],
                "versions": vers.get(r["project"], 0), "decks": decks.get(r["project"], 0), "internal": r["internal"] or 0,
                "archived": r["archived"] or 0,
                "favorites": r["favorites"] or 0,
                "released": r["released"] or 0, "updated_at": r["updated_at"], "sets": _gallery_sets(store, r["project"])}
               for r in rows]
        seen = {r["project"] for r in rows}
        for (pr,) in store.db.execute("SELECT DISTINCT project FROM exhibit_set ORDER BY project"):   # sets without slides yet
            if pr not in seen and pr != TRASH_PROJECT:
                out.append({"project": pr, "title": names.get(pr), "sharing": proj.get(pr), "exhibits": 0, "versions": 0, "decks": 0, "internal": 0,
                            "released": 0, "updated_at": None, "sets": _gallery_sets(store, pr)})
        made = _projects.created(store)
        for o in out:
            o["created_at"] = made.get(o["project"])
        return {"policy": pol, "project_sort": _projects.get_sort(store), "projects": out}
    p = _slug(project)
    sel = resolve_set(store, set_ref, p)["id"] if set_ref not in (None, "") else None
    rows = store.db.execute(
        "SELECT x.*, v.png_sha, v.html_sha, v.html_ext, v.density, v.width, v.height, v.created_at AS v_created, "
        "(SELECT count(*) FROM exhibit_version w WHERE w.exhibit_id = x.id) AS nv FROM exhibit x "
        "LEFT JOIN exhibit_version v ON v.exhibit_id = x.id AND v.n = x.current_version WHERE x.project = ? "
        "AND x.archived_at IS NULL"          # archived slides: only in the archive view and in searches (fundus.archive)
        + (" AND x.set_id = ?" if sel else "") +
        " ORDER BY coalesce(v.created_at, x.updated_at, x.created_at) DESC, x.id DESC", (p, *([sel] if sel else []))).fetchall()
    if not rows and not sel and not store.db.execute("SELECT 1 FROM exhibit_set WHERE project = ?", (p,)).fetchone():
        raise LookupError(f"no exhibits in project '{project}'")
    from fundus import locks as _locks
    lks = _locks.states(store, p)
    inrev = _locks.review_locks(store, project=p)
    items = []
    for r in rows:
        internal = r["visibility"] == "internal"
        it = {"id": r["id"], "set_id": r["set_id"], "slug": r["slug"], "kind": r["kind"], "title": _clip(r["title"], 240),
              "eyebrow": _clip(r["eyebrow"], 80),
              "so_what": _clip(r["so_what"], 240), "chart_types": r["chart_types"], "palette": r["palette"],
              "language": r["language"], "status": r["status"], "visibility": r["visibility"], "released": bool(r["released"]),
              "sharing": r["sharing"],
              "version": r["current_version"], "versions": r["nv"], "density": _density_brief(r) if r["density"] else None,
              "size": f"{r['width']}x{r['height']}" if r["width"] else None, "updated_at": r["v_created"] or r["updated_at"],
              "locked": lks["exhibits"].get(r["id"]) or lks["sets"].get(r["set_id"]), "in_review": inrev.get(r["id"]),
              "favorite": True if r["favorite_at"] else None}
        if r["png_sha"]:
            it["preview"] = url(r["png_sha"], "preview.png", p, internal)
            it["png"] = url(r["png_sha"], "png", p, internal)
        if r["html_sha"]:
            it["html"] = url(r["html_sha"], r["html_ext"] or "html", p, internal)
        items.append({k: v for k, v in it.items() if v not in (None, "")})
    decks: dict[str, dict] = {}
    for d in store.db.execute("SELECT d.deck, d.file, d.position, d.slot, d.exhibit_id, d.version, d.updated_at, e.set_id "
                              "FROM exhibit_deck d JOIN exhibit e ON e.id = d.exhibit_id WHERE d.project = ? "
                              "ORDER BY d.deck, d.position IS NULL, d.position, d.file", (p,)):
        e = decks.setdefault(d["deck"], {"deck": d["deck"], "set_id": None, "updated_at": d["updated_at"], "slides": [], "_sets": {}})
        e["updated_at"] = max(e["updated_at"] or "", d["updated_at"] or "") or None
        e["_sets"][d["set_id"]] = e["_sets"].get(d["set_id"], 0) + 1
        e["slides"].append({k: v for k, v in {"id": d["exhibit_id"], "slot": d["slot"], "file": d["file"],
                                              "version": d["version"]}.items() if v not in (None, "")})
    for e in decks.values():   # a deck folder lies in one set; mixed decks (explicit set arguments) take the majority
        e["set_id"] = max(e.pop("_sets").items(), key=lambda kv: kv[1])[0]
    dl = [e for e in decks.values() if sel is None or e["set_id"] == sel]
    from fundus import decks as _decks
    return {"policy": pol, "project": p, "title": project_titles(store).get(p), "sharing": project_sharing(store).get(p),
            "sets": _gallery_sets(store, p), "sequences": _decks.gallery_sequences(store, p, sel),
            "set": sel, "exhibits": items, "decks": sorted(dl, key=lambda e: e["updated_at"] or "", reverse=True)}


def summary_fields(store: Store, row) -> dict:
    """Search-result fields of an exhibit (query.summary)."""
    v = store.db.execute("SELECT png_sha, html_sha, html_ext, script_sha FROM exhibit_version WHERE exhibit_id = ? AND n = ?",
                         (row["id"], row["current_version"])).fetchone()
    internal = row["visibility"] == "internal"
    decks = [r[0] for r in store.db.execute("SELECT DISTINCT deck FROM exhibit_deck WHERE exhibit_id = ? LIMIT 4", (row["id"],))]
    st = store.db.execute("SELECT slug, title FROM exhibit_set WHERE id = ?", (row["set_id"],)).fetchone() \
        if "set_id" in row.keys() else None
    out = {"exhibit_type": row["kind"], "title": _clip(row["title"], 240), "so_what": _clip(row["so_what"], 240) or None,
           "project": row["project"], "set": st[0] if st else None, "set_title": st[1] if st else None, "slug": row["slug"], "version": row["current_version"], "palette": row["palette"],
           "decks": decks or None, "chart_types": row["chart_types"], "status": row["status"],
           "updated_at": row["updated_at"]}
    if "favorite_at" in row.keys() and row["favorite_at"]:
        out["favorite"] = True
    if "archived_at" in row.keys() and row["archived_at"]:
        out["archived"] = row["archived_at"][:10]      # in the archive: findable, not listed in its project (fundus.archive)
    if v is not None:
        out["preview_url"] = url(v["png_sha"], "preview.png", row["project"], internal)
        out["png_url"] = url(v["png_sha"], "png", row["project"], internal)
    if internal:
        out["visibility"] = "internal"
    from fundus import reviews as _rv
    rs = _rv.slide_status(store, ids=[row["id"]]).get(row["id"])
    if rs and rs.get("stage"):
        out["review_stage"], out["confidence"] = rs["stage"], rs["level"]   # verification stage, 0-3
    if rs and rs.get("verified"):
        out["verified_version"] = _verified_brief(store, row, rs["verified"], internal)
    from fundus import locks as _locks
    if _locks.exhibit_lock(store, row["id"]):
        out["locked"] = True                       # archived: copy it to work on it
    if _locks.review_locks(store, [row["id"]]).get(row["id"]):
        out["in_review"] = True                    # frozen while a review runs
    return out


def _verified_brief(store: Store, row, ver: dict, internal: bool) -> dict:
    """The fixed verified version of a slide with its URLs – offered next to a newer, not yet verified current version."""
    v = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (row["id"], ver["version"])).fetchone()
    out = {"version": ver["version"], "stage": ver["stage"], "confidence": ver["level"], "pinned_at": ver["pinned_at"],
           "is_current": ver["is_current"]}
    if not ver["is_current"]:
        out["note"] = (f"v{ver['version']} is the verified version ({ver['label']}); the current v{row['current_version']} is newer and "
                       "not verified yet – choose which one to use")
    if v is not None and not ver["is_current"]:   # the current version's urls are given anyway
        out["urls"] = version_urls(v, row["project"], internal)
        if v["script_path"]:
            out["script_path"] = v["script_path"]
    return out
