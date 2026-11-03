"""Gemini on Vertex AI as an independent reviewer (Fundus Review).

A Gemini review checks every slide of a review: the slide image, its text, the Fundus data points it shows and its
sources. The Spark fetches the sources itself (HTML, PDF via pdftotext, data files) and sends Gemini an excerpt –
letting Gemini fetch URLs on its own pulled whole web pages into the prompt (> 1 M tokens in a test). Long sources are cut
to the passages around the numbers on the slide. Gemini answers in a fixed JSON schema; its findings become feedback from
the reviewer 'Gemini': comments (with a box on the slide where it points), source and data point checks (an independent
check – Gemini counts like a person for verification), and deck-level remarks on the storyline.

The newest models are only served by the global endpoint (location 'global'); the EU region europe-west4 no longer has
any of them (tested 25.10.2026 with project fundus-review). Falk picks the model per review; the default is the strongest
generally available one. Override the list with FUNDUS_GEMINI_MODELS ("id=Label;id=Label").

Configuration (fundus.config, `vertex_*`): project, location, key file and model list. Auth: a service-account key
(vertex_key_file, default <user config dir>/fundus-review-sa.json, or a service-account file in
GOOGLE_APPLICATION_CREDENTIALS) is exchanged for an access token (signed JWT); without one, Application Default
Credentials (`gcloud auth application-default print-access-token`) and then the gcloud user login are used.
`fundus vertex check` shows what is configured and whether the model answers.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_mod
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from fundus import config as _config
from fundus.store import Store

# project, region, key file and model list come from fundus.config (vertex_project, vertex_location, vertex_key_file,
# vertex_models; env FUNDUS_VERTEX_* or the older FUNDUS_GCP_PROJECT / FUNDUS_GEMINI_LOCATION / FUNDUS_REVIEW_KEY /
# FUNDUS_GEMINI_MODELS)
_VX = _config.vertex_settings()
PROJECT = _VX["project"]
LOCATION = _VX["location"]
KEY_FILE = _VX["key_file"]

# (id, label, note) – strongest first; all answered a test call on 25.10.2026 (global endpoint)
MODELS = [
    ("gemini-4-argon", "Gemini 4 (Argon)", "neuestes und stärkstes Modell, allgemein verfügbar"),
    ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (Preview)", "starkes Denkmodell, Vorschau"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash", "schnell und günstig, für grosse Decks"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", "älteres Flash-Modell"),
]
if _VX["models"]:
    MODELS = [(i, lbl, "") for i, lbl in _VX["models"]]
DEFAULT = MODELS[0][0]

WORKERS = 4                     # slides checked in parallel
SOURCE_CHARS = 40000            # per source in the prompt
SOURCES_CHARS = 160000          # all sources of one slide
FETCH_BYTES = 30_000_000
CACHE_DAYS = 14
MAX_COMMENTS = 8
TIMEOUT = 300
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
FOCUS_TEXT = {"sources": "Sources: is every source the named publication in the right edition, and does it support the slide's statement?",
              "numbers": "Numbers: does every number on the slide match the source or the Fundus data point (value, unit, period, base)?",
              "logic": "Logic: does the key message (action title, so-what) follow from the data? Is the argument sound, is anything important missing?",
              "language": "Language: clarity, spelling, terminology, consistent notation."}


def models() -> dict:
    return {"default": DEFAULT, "location": LOCATION,
            "models": [{"id": i, "label": l, "note": n} for i, l, n in MODELS]}


def check_model(model: str | None) -> str:
    m = (model or DEFAULT).strip()
    if m not in {i for i, _, _ in MODELS}:
        raise ValueError(f"unknown Gemini model '{m}'; available: {', '.join(i for i, _, _ in MODELS)}")
    return m


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ auth and call
_tok = {"value": None, "exp": 0.0}
_tok_lock = threading.Lock()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sa_token(key_file: str) -> tuple[str, float]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(key_file) as f:
        info = json.load(f)
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": info.get("private_key_id")}).encode())
    claim = _b64(json.dumps({"iss": info["client_email"], "scope": "https://www.googleapis.com/auth/cloud-platform",
                             "aud": info.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    sig = _b64(key.sign(f"{head}.{claim}".encode(), padding.PKCS1v15(), hashes.SHA256()))
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": f"{head}.{claim}.{sig}"}).encode()
    with urllib.request.urlopen(urllib.request.Request(info.get("token_uri", "https://oauth2.googleapis.com/token"), data=data),
                                timeout=30) as r:
        d = json.load(r)
    return d["access_token"], time.time() + int(d.get("expires_in", 3600)) - 120


def token() -> str:
    with _tok_lock:
        if _tok["value"] and time.time() < _tok["exp"]:
            return _tok["value"]
        kind, key_file = credentials()
        if kind == "service_account":
            _tok["value"], _tok["exp"] = _sa_token(key_file)
        else:
            _tok["value"], _tok["exp"] = _gcloud_token(), time.time() + 1800
        return _tok["value"]


def _sa_file(path: str | None) -> bool:
    """True if `path` is a service-account key file (only its 'type' field is looked at; nothing is printed)."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            return json.load(f).get("type") == "service_account"
    except (OSError, ValueError, AttributeError):
        return False


def credentials() -> tuple[str, str | None]:
    """Which credentials a Vertex call uses, without reading secrets aloud: ('service_account', path) for the configured
    key file (vertex_key_file, default <user config dir>/fundus-review-sa.json) or a service-account file in
    GOOGLE_APPLICATION_CREDENTIALS; else ('gcloud', None) – Application Default Credentials (`gcloud auth
    application-default login`) or the gcloud user login."""
    if os.path.isfile(KEY_FILE):
        return "service_account", KEY_FILE
    adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if _sa_file(adc):
        return "service_account", adc
    return "gcloud", None


def _gcloud_token() -> str:
    missing = (f"Gemini review is an optional extension (INSTALL.md, E3) and is not set up here: no service-account key "
               f"at {KEY_FILE} (vertex_key_file) and no gcloud login (gcloud auth application-default login)")
    for cmd in (["gcloud", "auth", "application-default", "print-access-token"], ["gcloud", "auth", "print-access-token"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except OSError:
            raise RuntimeError(missing) from None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    raise RuntimeError(missing)


def endpoint(model: str) -> str:
    host = "aiplatform.googleapis.com" if LOCATION == "global" else f"{LOCATION}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{model}:generateContent"


def check(model: str | None = None, call: bool = True) -> dict:
    """`fundus vertex check`: project, region, credentials (kind and path only – never key contents or tokens), model list,
    and with `call` one tiny structured request to the model (a fraction of a cent)."""
    import shutil
    vx = _config.vertex_settings(with_origin=True)
    kind, path = credentials()
    rep = {"project": PROJECT, "location": LOCATION, "endpoint": endpoint(model or DEFAULT).split("/publishers/")[0],
           "credentials": kind, "key_file": _config.display_path(Path(path)) if path else None,
           "configured_key_file": _config.display_path(Path(KEY_FILE)), "gcloud": bool(shutil.which("gcloud")),
           "models": [i for i, _, _ in MODELS], "model": model or DEFAULT, "origin": vx["origin"],
           "config_file": str(_config.config_file() or "") or None, "checks": {}, "warnings": [], "ok": False}
    if vx["origin"]["project"] == "default":
        rep["warnings"].append(f"vertex_project is not set – using the built-in default '{PROJECT}'; set your own Google Cloud "
                               "project id in the config file (vertex_project)")
    if kind == "gcloud" and not rep["gcloud"]:
        rep["warnings"].append("no service-account key and no gcloud CLI – install the Google Cloud CLI and run "
                               "`gcloud auth application-default login`, or place a service-account key and set vertex_key_file")
    try:
        token()
        rep["checks"]["credentials"] = {"ok": True, "kind": kind}
    except Exception as e:     # noqa: BLE001 – a check reports everything
        rep["checks"]["credentials"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        return rep
    if not call:
        rep["ok"] = True
        return rep
    try:
        ans, usage = generate(rep["model"], [{"text": 'Reply with the JSON object {"ok": true}.'}],
                              {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}, "required": ["ok"]}, max_tokens=1024)
        rep["checks"]["model"] = {"ok": bool(isinstance(ans, dict) and ans.get("ok")), "answer": ans,
                                  "tokens": usage.get("totalTokenCount")}
    except Exception as e:     # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:300]
        rep["checks"]["model"] = {"ok": False, "error": msg}
        if "403" in msg:
            rep["warnings"].append("403: Vertex AI API not enabled in the project, billing missing, or the account lacks "
                                   "roles/aiplatform.user")
        elif "404" in msg:
            rep["warnings"].append(f"404: model '{rep['model']}' is not served in location '{LOCATION}' – try "
                                   "vertex_location = global or another model (vertex_models)")
    rep["ok"] = all(c.get("ok") for c in rep["checks"].values())
    return rep


def format_check(rep: dict) -> str:
    o = rep.get("origin", {})
    lines = [f"Gemini on Vertex AI: project {rep['project']} [{o.get('project', '')}], location {rep['location']} "
             f"[{o.get('location', '')}]",
             f"  config file:  {rep.get('config_file') or 'none (environment/defaults only)'}",
             f"  credentials:  {rep['credentials']}" + (f" ({rep['key_file']})" if rep.get("key_file") else
                                                         f" (no key at {rep['configured_key_file']}; gcloud CLI "
                                                         f"{'found' if rep['gcloud'] else 'missing'})"),
             f"  model:        {rep['model']}  (available: {', '.join(rep['models'])})"]
    for k, v in rep.get("checks", {}).items():
        extra = {kk: vv for kk, vv in v.items() if kk != "ok"}
        lines.append(f"  check {k:<12} {'ok' if v.get('ok') else 'FAILED'}  {json.dumps(extra, ensure_ascii=False)[:200]}")
    lines += [f"WARNING: {w}" for w in rep.get("warnings", [])]
    lines.append("Result: " + ("ready for Gemini reviews" if rep["ok"] else "not ready – see above"))
    return "\n".join(lines)


class SearchLimit(RuntimeError):
    """Gemini ran out of tool calls (too many web searches) before answering."""


def generate(model: str, parts: list, schema: dict, max_tokens: int = 16384, search: bool = False) -> tuple[dict, dict]:
    """One structured call: returns (parsed JSON answer, usage metadata; with search=True Google Search grounding is on and
    usage['searches'] counts the web queries). Retries rate limits and server errors."""
    payload = {"contents": [{"role": "user", "parts": parts}],
               "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                    "maxOutputTokens": max_tokens, "temperature": 0.2}}
    if search:
        payload["tools"] = [{"googleSearch": {}}]
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(4):
        req = urllib.request.Request(endpoint(model), data=body, headers={"Authorization": f"Bearer {token()}",
                                                                          "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.load(r)
            c = (d.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
            if not text:
                if c.get("finishReason") == "TOO_MANY_TOOL_CALLS":
                    raise SearchLimit("too many web searches")
                raise RuntimeError(f"empty answer (finish {c.get('finishReason')})")
            usage = dict(d.get("usageMetadata") or {})
            usage["searches"] = len((c.get("groundingMetadata") or {}).get("webSearchQueries") or [])
            return json.loads(text), usage
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            last = RuntimeError(f"Vertex AI {e.code}: {msg}")
            if e.code == 401:
                _tok["value"] = None
            if e.code not in (401, 429, 500, 502, 503, 504):
                raise last
        except SearchLimit:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = RuntimeError(f"Vertex AI: {e}")
        time.sleep(4 * 2 ** attempt)
    raise last


# ------------------------------------------------------------------ sources
def _cache_dir(store: Store) -> Path:
    d = Path(os.environ.get("FUNDUS_SOURCE_CACHE") or (store.path.parent / "source-cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def html_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", "\n", t)
    t = html_mod.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def pdf_text(data: bytes) -> str:
    out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"], input=data, capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext: {out.stderr.decode()[:200]}")
    pages = out.stdout.decode("utf-8", "replace").split("\f")
    return "\n".join(f"[Page {i}]\n{p.strip()}" for i, p in enumerate(pages, 1) if p.strip())


def fetch_source(store: Store, url: str) -> dict:
    """Text of a source URL ({ok, text, kind, error}), cached for CACHE_DAYS under data/source-cache."""
    key = hashlib.sha1(url.encode()).hexdigest()
    d = _cache_dir(store)
    meta_p, text_p = d / f"{key}.json", d / f"{key}.txt"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text())
        if time.time() - meta.get("t", 0) < CACHE_DAYS * 86400:
            return {**meta, "text": text_p.read_text() if text_p.exists() else ""}
    meta = {"url": url, "t": time.time(), "ok": False, "kind": None, "error": None}
    text = ""
    try:
        req = urllib.request.Request(url if "://" in url else "https://" + url,
                                     headers={"User-Agent": UA, "Accept": "text/html,application/pdf,application/json,text/*,*/*"})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read(FETCH_BYTES)
            ctype = (r.headers.get("content-type") or "").lower()
        if "pdf" in ctype or data[:5] == b"%PDF-":
            text, meta["kind"] = pdf_text(data), "pdf"
        elif "html" in ctype or b"<html" in data[:2000].lower():
            text, meta["kind"] = html_text(data.decode("utf-8", "replace")), "html"
        else:
            text, meta["kind"] = data.decode("utf-8", "replace"), "text"
        meta["ok"] = bool(text.strip())
        if not meta["ok"]:
            meta["error"] = "kein Text"
    except Exception as e:   # HTTP errors, bot walls, timeouts, broken PDFs
        meta["error"] = str(e)[:200]
    text_p.write_text(text)
    meta_p.write_text(json.dumps(meta))
    return {**meta, "text": text}


_NUM = re.compile(r"(?<![\w.,])\d+(?:[.,’']\d+)*")


def needles(*texts) -> list[str]:
    """Numbers named on the slide (and data points) – where to look in a long source."""
    out = []
    for t in texts:
        for m in _NUM.findall(str(t or "")):
            m = m.strip()
            if len(m.replace(".", "").replace(",", "")) >= 2 or m.isdigit() and int(m) >= 3:
                out.append(m)
    return list(dict.fromkeys(out))[:60]


def excerpt(text: str, look_for: list[str], limit: int = SOURCE_CHARS, window: int = 700) -> str:
    """The whole text if short; else the beginning plus the passages around the slide's numbers (merged), up to limit."""
    if len(text) <= limit:
        return text
    spans = []
    for n in look_for:
        variants = {n, n.replace(".", ","), n.replace(",", ".")}
        for v in variants:
            for m in re.finditer(r"(?<![\d.,])" + re.escape(v) + r"(?![\d])", text):
                spans.append((max(0, m.start() - window), min(len(text), m.end() + window)))
                if len(spans) > 400:
                    break
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    head = min(6000, limit // 5)
    out, used = [text[:head]], head
    for a, b in merged:
        if b <= head:
            continue
        a = max(a, head)
        piece = text[a:b]
        if used + len(piece) > limit:
            break
        out.append(f"[… passage {a}–{b} of {len(text)} characters …]\n{piece}")
        used += len(piece)
    return "\n".join(out) + f"\n[excerpt: {used} of {len(text)} characters]"


# ------------------------------------------------------------------ prompts and schemas
# Gemini always answers in English (Falk's decision 25.10.2026): reviewers read the findings in English, German or Ukrainian.
SLIDE_SCHEMA = {"type": "OBJECT", "properties": {
    "verdict": {"type": "STRING", "enum": ["clean", "issues"], "description": "clean = no errors on this slide (suggestions allowed)"},
    "summary": {"type": "STRING", "description": "2-3 sentences overall verdict on the slide"},
    "comments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "type": {"type": "STRING", "enum": ["error", "suggestion"], "description": "error = something is wrong; suggestion = could be better"},
        "evidence": {"type": "STRING", "description": "for an error: the quote, calculation or contradiction that proves it"},
        "text": {"type": "STRING", "description": "concrete finding with a suggestion what to change"},
        "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "category": {"type": "STRING", "enum": ["numbers", "sources", "logic", "language", "design"]},
        "box": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "[ymin, xmin, ymax, xmax] 0-1000 on the slide"}},
        "required": ["type", "text", "severity", "category"]}},
    "source_checks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "source_id": {"type": "INTEGER"}, "verdict": {"type": "STRING", "enum": ["ok", "doubt", "wrong"]},
        "note": {"type": "STRING"}, "evidence": {"type": "STRING", "description": "verbatim quote with page/location"},
        "better_url": {"type": "STRING"}}, "required": ["source_id", "verdict", "note"]}},
    "number_checks": {"type": "ARRAY", "description": "EVERY number on the slide (title, text, chart, boxes, footnotes)", "items": {
        "type": "OBJECT", "properties": {
            "number": {"type": "STRING", "description": "the number as on the slide, e.g. '82%' or '3 h 37 min'"},
            "claim": {"type": "STRING", "description": "what the number says on the slide"},
            "verdict": {"type": "STRING", "enum": ["sourced", "derived", "wrong", "unverified"]},
            "note": {"type": "STRING", "description": "wrong: the right value; derived: the calculation; unverified: where you searched"},
            "evidence": {"type": "STRING", "description": "verbatim quote from the source or the web with page/location"},
            "url": {"type": "STRING"},
            "box": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["number", "claim", "verdict", "note"]}},
    "claim_checks": {"type": "ARRAY", "description": "key statements without a number (action title, so-what, factual claims)", "items": {
        "type": "OBJECT", "properties": {
            "claim": {"type": "STRING"}, "verdict": {"type": "STRING", "enum": ["ok", "doubt", "wrong"]},
            "note": {"type": "STRING"}, "evidence": {"type": "STRING"}, "url": {"type": "STRING"},
            "box": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["claim", "verdict", "note"]}},
    "missing_sources": {"type": "ARRAY", "description": "numbers and factual claims without a source in the source line", "items": {
        "type": "OBJECT", "properties": {
            "claim": {"type": "STRING"}, "note": {"type": "STRING", "description": "which source is missing, suggestion"},
            "url": {"type": "STRING", "description": "source found, if any"},
            "box": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["claim", "note"]}},
    "datapoint_checks": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ref": {"type": "STRING", "description": "e.g. observation:123"}, "verdict": {"type": "STRING", "enum": ["ok", "doubt", "wrong"]},
        "note": {"type": "STRING"}, "evidence": {"type": "STRING"}}, "required": ["ref", "verdict", "note"]}}},
    "required": ["verdict", "summary", "comments", "source_checks", "number_checks", "claim_checks", "missing_sources", "datapoint_checks"]}

DECK_SCHEMA = {"type": "OBJECT", "properties": {
    "summary": {"type": "STRING"},
    "comments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "type": {"type": "STRING", "enum": ["error", "suggestion"]}, "evidence": {"type": "STRING"},
        "text": {"type": "STRING"}, "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "positions": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "slides concerned (positions)"}},
        "required": ["type", "text", "severity", "positions"]}}},
    "required": ["summary", "comments"]}

ROLE = ("You review slides of a consulting deck as an independent, careful fact-checker: you check numbers, sources and the "
        "soundness of statements. You are a different system than the author; your verdict counts as an independent check. "
        "A good slide has no errors – verdict 'clean' with no error comments is an expected and correct result, not a failure; "
        "do not invent problems to have something to say. Write everything in English, also when the slide is in German.")

RULES = f"""Rules:
- Every comment has a type, and you decide it:
  error = something on the slide is wrong or unsupported: a wrong or unsourced number, a wrong or missing source, a factual
    error, a broken calculation, a key message that contradicts the data (name the contradiction: "title says X, chart shows Y"),
    a contradiction with another slide. An error needs evidence (quote, calculation or the contradiction) – without it,
    it is a suggestion.
  suggestion = the slide is correct but could be better: wording, layout, labels, structure, an addition. Suggestions never
    block the slide; keep them few (at most 3) and only when they clearly help.
- At most {MAX_COMMENTS} comments in total. No praise.
- box: where the point sits on the slide, [ymin, xmin, ymax, xmax] in 0–1000; leave it out if it concerns the whole slide.
- Sources and data points marked "already_verified" you have checked before: give no new verdict, they count as correct
  (their texts are attached anyway so you can use them to source the numbers on the slide).
- source_checks: exactly one verdict per source in the list (source_id), as far as you can judge it.
  ok = the source is the named publication in the right edition and supports the statements of the slide that rely on it;
  doubt = cannot be confirmed (other edition, number not found, only partly supported);
  wrong = the source contradicts the slide. evidence = verbatim quote from the source text with page/location.
  If the source text is missing (not retrievable), give no verdict unless the slide contradicts itself.
  better_url only if you are sure of a better public source.
- datapoint_checks: one verdict per Fundus data point (ref): does the value on the slide match the data point and its source?
- number_checks – the most important part: invented numbers must not be on a slide. List EVERY number on the slide separately
  (title, text, chart values, boxes, footnotes; for charts with many values one entry per data series with all values).
  sourced = found in the attached source texts or via Google Search in a reputable source (evidence = quote, url);
  derived = computed from other numbers on the slide (difference, share, factor) – recompute it, note = the calculation; if it
  does not add up it is wrong; wrong = the source gives another value or period (note = the right value);
  unverified = found neither in the sources nor by search – say in note where you searched. Never guess; if in doubt, unverified.
  Search sparingly: at most 8 web searches per slide – one per source or data series (e.g. the report a table comes from),
  never one per number. Values that are in a report you found count as sourced with a quote.
  Do not list years, slide numbers or axis labels.
- claim_checks: check the key statements (action title, so-what, facts without numbers) for correctness – with the sources,
  otherwise via Google Search. ok/doubt/wrong with evidence and url.
- missing_sources: every number or factual claim for which the source line names no source or whose source is not in the
  source list above. If you find the source by search: url and exact title in note, so the author can add it.
- Do not report points again that were already declined or answered."""


def _image_part(store: Store, png_sha: str) -> dict:
    from PIL import Image
    from fundus import exhibits as ex
    im = Image.open(ex.file_path(store, png_sha, "png")).convert("RGB")
    im.thumbnail((1920, 1920))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


def _ai_ok(store: Store, kind: str, tid, exhibit_id: int) -> str | None:
    """Date of Gemini's latest check of a source or data point ON THIS SLIDE if it said ok (then it is not checked again)."""
    r = store.db.execute("SELECT c.verdict, c.created_at FROM item_check c JOIN reviewer r ON r.id = c.reviewer_id AND r.kind = 'ai' "
                         "WHERE c.target_kind = ? AND c.target_id = ? AND c.aspect = 'usage' AND c.exhibit_id = ? ORDER BY c.id DESC LIMIT 1",
                         (kind, str(tid), exhibit_id)).fetchone()
    return r["created_at"][:10] if r and r["verdict"] == "ok" else None


def known_numbers(store: Store, eid: int, rid: int) -> list[dict]:
    """Numbers of the slide Gemini already found in a source in an earlier review (any version): carried over, not searched again."""
    rows = store.db.execute("SELECT number, claim, evidence, url, created_at FROM exhibit_number WHERE exhibit_id = ? AND verdict = 'sourced' "
                            "AND review_id IS NOT ? AND id IN (SELECT max(id) FROM exhibit_number WHERE exhibit_id = ? GROUP BY number) "
                            "ORDER BY id", (eid, rid, eid)).fetchall()
    return [{"number": r["number"], "claim": r["claim"], "evidence": r["evidence"], "url": r["url"], "checked": r["created_at"][:10]} for r in rows]


def already_checked(store: Store, rid: int, eid: int, version: int) -> dict | None:
    """An earlier review whose Gemini check covered exactly this version of the slide without error – then it is not checked
    again: same content, same sources, same verdict. If that check found errors, None: the follow-up checks the answers."""
    for r in store.db.execute("SELECT r.id, r.gemini_report FROM review r JOIN review_slide s ON s.review_id = r.id "
                              "WHERE r.id != ? AND s.exhibit_id = ? AND s.version = ? AND r.gemini_report IS NOT NULL ORDER BY r.id DESC",
                              (rid, eid, version)):
        rep = json.loads(r["gemini_report"])
        if rep.get("superseded"):
            continue                       # a check that was cancelled and replaced does not count
        got = (rep.get("slides") or {}).get(str(eid))
        if got and not got.get("error") and not got.get("skipped"):
            if got.get("clean") is False or got.get("errors") or got.get("not_fixed") or got.get("new_errors"):
                pending = store.db.execute("SELECT count(*) FROM feedback WHERE exhibit_id = ? AND source = 'ai' "
                                           "AND coalesce(weight, 'error') = 'error' AND status IN ('open', 'in_progress', 'decision')",
                                           (eid,)).fetchone()[0]
                if not pending:
                    return None            # it found errors and all are answered now: check whether the answers hold (follow-up)
            return {"review": r["id"], **{k: got[k] for k in ("summary", "numbers", "claims", "missing_sources") if k in got}}
    return None


def slide_job(store: Store, review: dict, slide: dict, deck_titles: list[str]) -> dict:
    """Everything one slide call needs (built on the store's thread; the call itself runs in a worker)."""
    from fundus import reviews as rv
    eid, version = slide["exhibit_id"], slide["version"]
    v = store.db.execute("SELECT * FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, version)).fetchone()
    x = json.loads(v["extract"]) if v and v["extract"] else {}
    evd = rv.slide_evidence(store, eid)
    data = [{"ref": f"{d['kind']}:{d['id']}", **{k: d.get(k) for k in ("label", "name", "value", "unit", "period") if d.get(k) is not None},
             "source": (d.get("source") or {}).get("title"),
             **({"already_verified": f"by Gemini on {ok}"} if (ok := _ai_ok(store, d["kind"], d["id"], eid)) else {})} for d in evd["data"]]
    look = needles(x.get("title"), x.get("so_what"), x.get("body"), " ".join(x.get("labels") or []),
                   " ".join(x.get("chart_heads") or []), *[d.get("value") for d in data])
    texts, sources, used = [], [], 0
    for s in evd["sources"]:
        info = {"source_id": s["id"], "title": s.get("title"), "publisher": s.get("publisher"), "published": s.get("published"), "url": s.get("url")}
        if ok := _ai_ok(store, "document", s["id"], eid):
            info["already_verified"] = f"by Gemini on {ok}"
        got = fetch_source(store, s["url"]) if s.get("url") else {"ok": False, "error": "no URL"}
        if got.get("ok") and used < SOURCES_CHARS:
            ex_t = excerpt(got["text"], look, min(SOURCE_CHARS, SOURCES_CHARS - used))
            used += len(ex_t)
            texts.append(f"=== SOURCE source_id={s['id']} · {s.get('publisher') or ''} · {s.get('title') or ''} · {s.get('url')}\n{ex_t}")
            info["text"] = "attached"
        else:
            info["text"] = f"not retrievable ({got.get('error')})"
        sources.append(info)
    known = known_numbers(store, eid, review["id"])
    old = [f"- {r['text']} → {r['status']}: {r['resolution']}" for r in store.db.execute(
        "SELECT text, status, resolution FROM feedback WHERE exhibit_id = ? AND status IN ('rejected','answered') ORDER BY id DESC LIMIT 20", (eid,))]
    focus = review.get("focus") or ["sources", "numbers", "logic"]
    pos = slide["position"]
    prompt = "\n\n".join(filter(None, [
        ROLE,
        "Focus of this review:\n" + "\n".join(f"- {FOCUS_TEXT[f]}" for f in focus if f in FOCUS_TEXT),
        f"Deck \"{review['title']}\", slide {pos} of {len(deck_titles)}. All slide titles:\n" +
        "\n".join(f"{i}. {t}" for i, t in enumerate(deck_titles, 1)),
        "Text of the slide (from the HTML): " + json.dumps({k: x.get(k) for k in ("eyebrow", "title", "meta", "so_what", "body", "labels",
                                                                             "chart_heads", "sources", "sources_more", "notes") if x.get(k)},
                                                         ensure_ascii=False),
        (f"The author marked the slide as needing no sources ({x['no_sources']}). If it does contain numbers or facts, "
         "report them as missing_sources.") if x.get("no_sources") else None,
        "Fundus data points on this slide: " + (json.dumps(data, ensure_ascii=False) if data else "none linked"),
        "Sources of the slide: " + (json.dumps(sources, ensure_ascii=False) if sources else "none linked (only the source line in the slide text)"),
        "Points on this slide already dealt with:\n" + "\n".join(old) if old else None,
        ("Numbers on this slide you already sourced in an earlier review (do not search again; same number = take it over as sourced "
         "with this evidence, check only changed or new numbers): " + json.dumps(known, ensure_ascii=False)) if known else None,
        RULES]))
    parts = [_image_part(store, v["png_sha"]), {"text": prompt}] + ([{"text": "\n\n".join(texts)}] if texts else [])
    return {"exhibit_id": eid, "version": version, "position": pos, "parts": parts,
            "source_ids": {s["source_id"] for s in sources if not s.get("already_verified")},
            "data_refs": {d["ref"] for d in data if not d.get("already_verified")},
            "verified_before": sum(1 for s in sources if s.get("already_verified")) + sum(1 for d in data if d.get("already_verified")),
            "known_numbers": len(known),
            "sources_unreachable": [s["source_id"] for s in sources if s["text"] != "attached"]}


def _box(b) -> dict | None:
    if not isinstance(b, list) or len(b) != 4:
        return None
    y0, x0, y1, x1 = (max(0, min(1000, int(v))) for v in b)
    if y1 <= y0 or x1 <= x0:
        return None
    return {"tool": "rect", "x": x0 / 1000, "y": y0 / 1000, "w": (x1 - x0) / 1000, "h": (y1 - y0) / 1000}


def _ext(rid: int, eid: int, *parts) -> str:
    return f"gemini:{rid}:{eid}:" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


NO_SEARCH = ("Note: web search is not available for this slide (search limit). Judge with the attached source texts only; "
             "numbers without evidence in them are unverified, with note 'checked without web search'.")


def check_slide(model: str, parts: list) -> tuple[dict, dict]:
    """Slide check with Google Search; if Gemini runs out of searches, once more with the attached sources only."""
    try:
        return generate(model, parts, SLIDE_SCHEMA, 32768, True)
    except SearchLimit:
        ans, usage = generate(model, parts + [{"text": NO_SEARCH}], SLIDE_SCHEMA, 32768, False)
        usage["search_limit"] = True
        return ans, usage


FOLLOWUP_SCHEMA = {"type": "OBJECT", "properties": {
    "verdict": {"type": "STRING", "enum": ["clean", "issues"], "description": "clean = every earlier error is fixed and no new error"},
    "summary": {"type": "STRING", "description": "1-2 sentences"},
    "fixes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "ref": {"type": "STRING", "description": "the earlier point, e.g. F-118"},
        "fixed": {"type": "BOOLEAN"}, "note": {"type": "STRING"}, "evidence": {"type": "STRING"}},
        "required": ["ref", "fixed", "note"]}},
    "new_errors": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "text": {"type": "STRING"}, "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "evidence": {"type": "STRING"}, "box": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
        "required": ["text", "severity", "evidence"]}}},
    "required": ["verdict", "summary", "fixes", "new_errors"]}

FOLLOWUP_RULES = """This is a FOLLOW-UP check of a slide you checked before. Do not review the whole slide again.
- For every earlier error listed above: is it fixed on the current slide? fixed=true only if the problem is really gone;
  fixed=false with note (what is still wrong) and evidence. An answered or declined point counts as fixed if the reason given
  holds; otherwise fixed=false and say why.
- new_errors: only errors that the CHANGES introduced (see the text diff) – wrong numbers, missing sources, contradictions.
  Each needs evidence. No suggestions, no comments on unchanged parts – they were accepted in the earlier check.
- verdict clean = every earlier error fixed and no new error. That is the expected result after a careful revision."""


def prior_check(store: Store, eid: int, rid: int) -> dict | None:
    """The latest earlier Gemini check of this slide that found errors (not a cancelled one): its version and error points."""
    from fundus import reviews as rv
    rows = store.db.execute(
        "SELECT f.*, r.gemini_report FROM feedback f JOIN review r ON r.id = f.review_id WHERE f.exhibit_id = ? AND f.source = 'ai' "
        "AND f.review_id != ? AND coalesce(f.weight, 'error') = 'error' AND coalesce(f.resolved_by, '') != 'gemini-superseded' "
        "ORDER BY f.review_id DESC, f.id", (eid, rid)).fetchall()
    rows = [r for r in rows if not json.loads(r["gemini_report"] or "{}").get("superseded")]
    if not rows:
        return None
    last = rows[0]["review_id"]
    pts = [r for r in rows if r["review_id"] == last]
    return {"review": last, "version": max(r["version"] for r in pts),
            "points": [{"ref": f"F-{r['id']}", "text": r["text"], "status": r["status"], "answer": r["resolution"],
                        "fixed_in": r["resolved_version"]} for r in pts]}


def _slide_text(store: Store, eid: int, version: int) -> list[str]:
    v = store.db.execute("SELECT extract FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (eid, version)).fetchone()
    x = json.loads(v[0]) if v and v[0] else {}
    lines = [x.get("title") or "", x.get("so_what") or "", *(x.get("labels") or []), *(x.get("chart_heads") or []),
             *(x.get("sources") or []), *(x.get("notes") or []), x.get("body") or ""]
    return [" ".join(str(l).split()) for l in lines if str(l).strip()]


def followup_job(store: Store, review: dict, slide: dict, prior: dict) -> dict:
    """A follow-up check: the earlier errors with the session's answers and the text diff between the checked and the
    current version, plus the slide image and its sources (for verifying fixed numbers)."""
    import difflib
    eid, version = slide["exhibit_id"], slide["version"]
    job = slide_job(store, review, slide, [])
    diff = "\n".join(difflib.unified_diff(_slide_text(store, eid, prior["version"]), _slide_text(store, eid, version),
                                          f"v{prior['version']}", f"v{version}", lineterm="", n=0))
    head = "\n\n".join([ROLE,
        f"Earlier check: version {prior['version']}; current version: {version}.",
        "Earlier errors and what the author did:\n" + json.dumps(prior["points"], ensure_ascii=False),
        "Text diff between the checked and the current version:\n" + (diff[:20000] or "(no text change – check the image)"),
        FOLLOWUP_RULES])
    parts = [job["parts"][0], {"text": head}] + job["parts"][2:]   # image, follow-up task, source texts
    return {**job, "parts": parts, "mode": "followup", "prior": prior}


def check_followup(model: str, parts: list) -> tuple[dict, dict]:
    return generate(model, parts, FOLLOWUP_SCHEMA, 8192, False)


def apply_followup(store: Store, rid: int, job: dict, ans: dict) -> dict:
    """Not fixed → the earlier point opens again with Gemini's reason; new errors become points; the checked version is recorded."""
    from fundus import reviews as rv
    eid, ver = job["exhibit_id"], job["version"]
    known = {p["ref"] for p in job["prior"]["points"]}
    n = {"fixed": 0, "not_fixed": 0, "new_errors": 0, "ignored": 0}
    for f in ans.get("fixes") or []:
        ref = str(f.get("ref") or "").strip().upper().replace("F", "F-", 1).replace("F--", "F-")
        if ref not in known:
            n["ignored"] += 1
            continue
        if f.get("fixed"):
            n["fixed"] += 1
        else:
            rv.reopen(store, ref, f"Gemini follow-up (v{ver}): not fixed – {' '.join(str(f.get('note') or '').split())}", actor="gemini")
            n["not_fixed"] += 1
    for c in ans.get("new_errors") or []:
        text = " ".join(str(c.get("text") or "").split())
        ev = " ".join(str(c.get("evidence") or "").split())
        if not text:
            continue
        shape = _box(c.get("box"))
        rv.add(store, eid, text, source="ai", review_id=rid, kind="shape" if shape else "comment", version=ver,
               severity=c.get("severity") if c.get("severity") in rv.SEVERITIES else None, shape=shape,
               note=f"Evidence: {ev}" if ev else None, external_id=_ext(rid, eid, "f", text), actor="gemini",
               weight="error" if ev else "suggestion")
        n["new_errors"] += 1
    clean = not n["not_fixed"] and not n["new_errors"]
    record_check(store, eid, ver, rid, "followup", "clean" if clean else "issues")
    return {**n, "clean": clean, "mode": "followup", "since_version": job["prior"]["version"]}


def record_check(store: Store, eid: int, version: int, rid: int, mode: str, result: str) -> None:
    from fundus import reviews as rv
    store.db.execute("INSERT INTO exhibit_check(exhibit_id, version, reviewer_id, review_id, mode, result) VALUES (?,?,?,?,?,?)",
                     (eid, version, rv.ai_reviewer(store)["id"], rid, mode, result))
    store.db.commit()


def apply_slide(store: Store, rid: int, job: dict, ans: dict) -> dict:
    """Turn one slide answer into feedback and checks. Returns counts."""
    from fundus import reviews as rv
    eid, ver = job["exhibit_id"], job["version"]
    n = {"comments": 0, "checks": 0, "ignored": 0, "numbers": {"sourced": 0, "derived": 0, "wrong": 0, "unverified": 0},
         "claims": {"ok": 0, "doubt": 0, "wrong": 0}, "missing_sources": 0}
    clean = lambda v: " ".join(str(v or "").split())

    def point(tag, text, sev, box, note, weight="error"):
        shape = _box(box)
        rv.add(store, eid, text, source="ai", review_id=rid, kind="shape" if shape else "comment", version=ver, severity=sev,
               shape=shape, note=note or None, external_id=_ext(rid, eid, tag, text), actor="gemini", weight=weight)

    def proof(c):
        bits = [f"Evidence: \"{clean(c.get('evidence'))}\"" if c.get("evidence") else None, clean(c.get("url")) or None]
        return " – ".join(b for b in bits if b)

    # every number on the slide: stored with the slide (exhibit_number); wrong or unverified numbers are high-weight points
    gem = rv.ai_reviewer(store)
    rows = []
    for c in ans.get("number_checks") or []:
        if c.get("verdict") in n["numbers"] and clean(c.get("number")):
            rows.append((eid, ver, clean(c.get("number"))[:80], clean(c.get("claim"))[:400] or None, c["verdict"], clean(c.get("note"))[:1000] or None,
                         clean(c.get("evidence"))[:1000] or None, clean(c.get("url"))[:500] or None, gem["id"], rid))
    store.db.execute("DELETE FROM exhibit_number WHERE exhibit_id = ? AND review_id = ?", (eid, rid))
    store.db.executemany("INSERT INTO exhibit_number(exhibit_id, version, number, claim, verdict, note, evidence, url, reviewer_id, review_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    store.db.commit()
    for c in ans.get("number_checks") or []:
        v = c.get("verdict")
        if v not in n["numbers"]:
            n["ignored"] += 1
            continue
        n["numbers"][v] += 1
        num, claim, note = clean(c.get("number")), clean(c.get("claim")), clean(c.get("note"))
        if v == "wrong":
            point("n", f"Number wrong: {num} ({claim}). {note}".strip(), "high", c.get("box"), proof(c))
        elif v == "unverified":
            point("n", f"Number not verifiable: {num} ({claim}). {note} Add the source or remove the number.".strip(),
                  "high", c.get("box"), proof(c))
    for c in ans.get("claim_checks") or []:
        v = c.get("verdict")
        if v not in n["claims"]:
            n["ignored"] += 1
            continue
        n["claims"][v] += 1
        if v in ("doubt", "wrong"):
            word = "wrong" if v == "wrong" else "doubtful"
            point("a", f"Statement {word}: {clean(c.get('claim'))}. {clean(c.get('note'))}".strip(), "high" if v == "wrong" else "medium",
                  c.get("box"), proof(c), weight="error" if v == "wrong" else "suggestion")
    for c in ans.get("missing_sources") or []:
        claim, note = clean(c.get("claim")), clean(c.get("note"))
        if not claim:
            continue
        point("m", f"Source missing: {claim}. {note}".strip(), "medium", c.get("box"),
              f"Source found: {clean(c.get('url'))}" if c.get("url") else None)
        n["missing_sources"] += 1
    n["errors"], n["suggestions"] = 0, 0
    for c in (ans.get("comments") or [])[:MAX_COMMENTS]:
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            continue
        sev = c.get("severity") if c.get("severity") in rv.SEVERITIES else None
        shape = _box(c.get("box"))
        cat = c.get("category")
        ev = clean(c.get("evidence"))
        weight = "error" if c.get("type") == "error" and ev else "suggestion"   # an error without evidence is a suggestion
        note = " · ".join(x for x in [f"Category: {cat}" if cat else None, f"Evidence: {ev}" if ev else None] if x)
        rv.add(store, eid, text, source="ai", review_id=rid, kind="shape" if shape else "comment", version=ver, severity=sev,
               shape=shape, note=note or None, external_id=_ext(rid, eid, "c", text), actor="gemini", weight=weight)
        n["comments"] += 1
        n["errors" if weight == "error" else "suggestions"] += 1
    for c in ans.get("source_checks") or []:
        sid = c.get("source_id")
        if sid not in job["source_ids"] or c.get("verdict") not in rv.VERDICTS:
            n["ignored"] += 1
            continue
        note = " ".join(str(c.get("note") or "").split())
        if c.get("better_url"):
            note += f" Better source: {c['better_url']}"
        rv.add(store, eid, "", source="ai", review_id=rid, kind="check", version=ver, target=("document", str(sid)),
               verdict=c["verdict"], note=note or None, evidence=c.get("evidence"), external_id=_ext(rid, eid, "s", sid),
               actor="gemini", weight="error" if c["verdict"] == "wrong" else "suggestion")
        if c["verdict"] == "ok":   # Gemini found the cited passage: the source itself is genuine as well
            with store.batch("gemini"):
                rv.record_check(store, "document", str(sid), rv.ai_reviewer(store), "ok", note="right publication, passage found",
                                evidence=c.get("evidence"), review_id=rid, exhibit_id=eid, aspect="source")
        n["checks"] += 1
    for c in ans.get("datapoint_checks") or []:
        ref = str(c.get("ref") or "").strip()
        if ref not in job["data_refs"] or c.get("verdict") not in rv.VERDICTS:
            n["ignored"] += 1
            continue
        kind, tid = ref.split(":", 1)
        rv.add(store, eid, "", source="ai", review_id=rid, kind="check", version=ver, target=(kind, tid), verdict=c["verdict"],
               note=" ".join(str(c.get("note") or "").split()) or None, evidence=c.get("evidence"),
               external_id=_ext(rid, eid, "d", ref), actor="gemini", weight="error" if c["verdict"] == "wrong" else "suggestion")
        n["checks"] += 1
    blocking = n["errors"] + n["numbers"]["wrong"] + n["numbers"]["unverified"] + n["claims"]["wrong"] + n["missing_sources"]
    n["clean"] = blocking == 0      # Gemini's own verdict covers only its comments; numbers and sources count too
    record_check(store, eid, ver, rid, "full", "clean" if n["clean"] else "issues")
    return n


def deck_parts(store: Store, review: dict, slides: list[dict]) -> list:
    from fundus import exhibits as ex
    lines = []
    for s in slides:
        r = store.db.execute("SELECT v.title, e.so_what FROM exhibit_version v JOIN exhibit e ON e.id = v.exhibit_id "
                             "WHERE v.exhibit_id = ? AND v.n = ?", (s["exhibit_id"], s["version"])).fetchone()
        x = store.db.execute("SELECT extract FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (s["exhibit_id"], s["version"])).fetchone()
        so = (json.loads(x[0]).get("so_what") if x and x[0] else None) or (r["so_what"] if r else None)
        lines.append(f"{s['position']}. {r['title'] if r else ''}" + (f" — so-what: {so}" if so else ""))
    if review.get("scope") == "deck":
        task = (f"Check the storyline of the deck \"{review['title']}\" ({len(slides)} slides, in presentation order): do the slides follow "
                "logically? Do statements or numbers contradict each other across slides? Is a step missing, is anything duplicated? "
                "At most 6 comments, each with the positions concerned.")
    else:
        task = (f"Check the {len(slides)} slides \"{review['title']}\" together. The order is arbitrary (a selection, not a finished "
                "deck) – do not judge it. Look only for contradicting statements or numbers across slides, duplicates and "
                "inconsistent terms or notation. At most 6 comments, each with the positions concerned.")
    parts = [{"text": "\n\n".join([ROLE, task, "Slides (position, action title, so-what):\n" + "\n".join(lines)])}]
    for s in slides[:40]:   # small previews for the overall picture
        v = store.db.execute("SELECT png_sha FROM exhibit_version WHERE exhibit_id = ? AND n = ?", (s["exhibit_id"], s["version"])).fetchone()
        p = ex.file_path(store, v["png_sha"], "preview.png")
        if p.exists():
            parts.append({"text": f"Slide {s['position']}:"})
            parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(p.read_bytes()).decode()}})
    return parts


def apply_deck(store: Store, rid: int, slides: list[dict], ans: dict, scope: str | None = None) -> int:
    from fundus import reviews as rv
    by_pos = {s["position"]: s for s in slides}
    n = 0
    for c in (ans.get("comments") or [])[:6]:
        poss = [p for p in c.get("positions") or [] if p in by_pos] or [slides[0]["position"]]
        s = by_pos[poss[0]]
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            continue
        label = "Deck" if scope == "deck" else "Across slides"
        weight = "error" if c.get("type") == "error" and str(c.get("evidence") or "").strip() else "suggestion"
        rv.add(store, s["exhibit_id"], f"{label} (slides {', '.join(map(str, poss))}): {text}", source="ai", review_id=rid, weight=weight,
               version=s["version"], severity=c.get("severity") if c.get("severity") in rv.SEVERITIES else None,
               external_id=_ext(rid, s["exhibit_id"], "deck", text), actor="gemini")
        n += 1
    return n


# ------------------------------------------------------------------ run a review
def _report(store: Store, rid: int) -> dict:
    r = store.db.execute("SELECT gemini_report FROM review WHERE id = ?", (rid,)).fetchone()
    return json.loads(r[0]) if r and r[0] else {}


def _save(store: Store, rid: int, rep: dict, state: str | None = None) -> None:
    store.db.execute("UPDATE review SET gemini_report = ?" + (", gemini_state = ?" if state else "") + " WHERE id = ?",
                     (json.dumps(rep, ensure_ascii=False), *([state] if state else []), rid))
    store.db.commit()


_running: set[int] = set()
_run_lock = threading.Lock()


def run_review(db_path, rid: int, retry: bool = False) -> dict:
    """Check every slide of review `rid` with its Gemini model (slides already done are skipped), then the deck as a whole.
    retry=True runs slides that failed before again. Progress, tokens, summaries and errors go to review.gemini_report."""
    with _run_lock:
        if rid in _running:
            return {"review": rid, "skipped": "already running"}
        _running.add(rid)
    st = Store(db_path)
    st.actor = "gemini"
    try:
        row = st.db.execute("SELECT * FROM review WHERE id = ?", (rid,)).fetchone()
        if row is None or not row["gemini_state"]:
            raise LookupError(f"review {rid} has no Gemini check")
        review = {"id": rid, "title": row["title"], "scope": row["scope"], "focus": json.loads(row["focus"]) if row["focus"] else None}
        model = check_model(row["gemini_model"])
        slides = [dict(r) for r in st.db.execute("SELECT position, exhibit_id, version FROM review_slide WHERE review_id = ? "
                                                 "ORDER BY position", (rid,))]
        titles = [st.db.execute("SELECT coalesce(title, slug) FROM exhibit WHERE id = ?", (s["exhibit_id"],)).fetchone()[0] for s in slides]
        rep = _report(st, rid)
        rep.setdefault("slides", {})
        rep.setdefault("tokens", {"prompt": 0, "output": 0})
        rep.update(model=model, started_at=rep.get("started_at") or _now(), total=len(slides))
        rep.pop("error", None)
        todo = [s for s in slides if str(s["exhibit_id"]) not in rep["slides"] or (retry and rep["slides"][str(s["exhibit_id"])].get("error"))]
        rep["done"] = len(slides) - len(todo)
        _save(st, rid, rep, "running")
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        futs = {}

        def finish(f):
            j = futs.pop(f)
            key = str(j["exhibit_id"])
            try:
                ans, usage = f.result()
                counts = apply_followup(st, rid, j, ans) if j.get("mode") == "followup" else apply_slide(st, rid, j, ans)
                rep["slides"][key] = {"position": j["position"], "summary": ans.get("summary"), **counts,
                                      "unreachable_sources": j.get("sources_unreachable", []),
                                      "verified_before": j["verified_before"], "known_numbers": j["known_numbers"],
                                      **({"search_limit": True} if usage.get("search_limit") else {})}
                rep["tokens"]["prompt"] += usage.get("promptTokenCount", 0)
                rep["tokens"]["output"] += usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
                rep["searches"] = rep.get("searches", 0) + usage.get("searches", 0)
            except Exception as e:
                rep["slides"][key] = {"position": j["position"], "error": str(e)[:300]}
            rep["done"] = sum(1 for v in rep["slides"].values() if not v.get("error"))
            _save(st, rid, rep)

        try:
            for s in todo:   # prepare slide by slide (sources are fetched here) and hand each to Gemini at once
                prev = already_checked(st, rid, s["exhibit_id"], s["version"])
                if prev:   # this exact version was checked by Gemini before: not again
                    rep["slides"][str(s["exhibit_id"])] = {"position": s["position"], "skipped": f"bereits in Prüfung #{prev['review']} geprüft (v{s['version']})",
                                                          **{k: v for k, v in prev.items() if k != "review"}}
                else:
                    try:
                        prior = prior_check(st, s["exhibit_id"], rid)
                        if prior and s["version"] >= prior["version"]:   # checked before, revised since: check only the fixes and changes
                            j = followup_job(st, review, s, prior)
                        else:
                            j = slide_job(st, review, s, titles)
                        futs[pool.submit(check_followup if j.get("mode") == "followup" else check_slide, model, j["parts"])] = j
                    except Exception as e:
                        rep["slides"][str(s["exhibit_id"])] = {"position": s["position"], "error": f"Vorbereitung: {e}"[:300]}
                for f in [f for f in futs if f.done()]:
                    finish(f)
            for f in as_completed(list(futs)):
                finish(f)
        finally:
            pool.shutdown(wait=True)
        rep["done"] = sum(1 for v in rep["slides"].values() if not v.get("error"))
        focus = review["focus"] or []
        if len(slides) > 1 and ("logic" in focus or not focus) and not rep.get("deck"):
            try:
                ans, usage = generate(model, deck_parts(st, review, slides), DECK_SCHEMA)
                rep["deck"] = {"summary": ans.get("summary"), "comments": apply_deck(st, rid, slides, ans, review["scope"])}
                rep["tokens"]["prompt"] += usage.get("promptTokenCount", 0)
                rep["tokens"]["output"] += usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
            except Exception as e:
                rep["deck"] = {"error": str(e)[:300]}
        failed = [v for v in rep["slides"].values() if v.get("error")]
        rep["finished_at"] = _now()
        state = "failed" if failed and len(failed) == len(slides) else "done"
        if failed:
            rep["error"] = f"{len(failed)} von {len(slides)} Folien nicht geprüft"
        _save(st, rid, rep, state)
        if state == "done" and not st.db.execute("SELECT 1 FROM review_access WHERE review_id = ?", (rid,)).fetchone():
            # a Gemini-only review has nothing left to wait for: it is finished (the feedback stays in the inbox)
            st.db.execute("UPDATE review SET status = 'closed', closed_at = ? WHERE id = ? AND status != 'closed'", (_now(), rid))
            st.db.commit()
        st.log("review", rid, "gemini_" + state, {"model": model, "tokens": rep["tokens"], "failed": len(failed)})
        from fundus import reviews as rv
        rv.pin_verified(st, ids=[s["exhibit_id"] for s in slides])   # verified slides: fix this version
        st.db.commit()
        return {"review": rid, "state": state, **{k: rep[k] for k in ("done", "total", "tokens")}, "error": rep.get("error")}
    except Exception as e:
        try:
            rep = _report(st, rid)
            rep["error"] = str(e)[:300]
            _save(st, rid, rep, "failed")
        except Exception:
            pass
        raise
    finally:
        st.close()
        with _run_lock:
            _running.discard(rid)


def start(db_path, rid: int, retry: bool = False, daemon: bool = True) -> None:
    """Run in a background thread (portal, MCP). daemon=False keeps a short-lived process alive until the check is done."""
    def go():
        try:
            run_review(db_path, rid, retry=retry)
        except Exception:
            pass   # state and error are in review.gemini_report
    threading.Thread(target=go, daemon=daemon, name=f"gemini-{rid}").start()


def resume(db_path) -> list[int]:
    """Restart checks that were queued or interrupted (server start)."""
    st = Store(db_path)
    try:
        ids = [r[0] for r in st.db.execute("SELECT id FROM review WHERE gemini_state IN ('queued','running') ORDER BY id")]
    finally:
        st.close()
    for rid in ids:
        start(db_path, rid)
    return ids
