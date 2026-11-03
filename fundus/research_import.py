"""Importer for the analysis series in Fundus-Data/_analysen (3 Nov 2026): `fundus research import <path> [--dry-run]`.

What it reads (formats checked on the real folders):
- serie-N/RESEARCH-PROPOSAL.md or CONCEPT.md: title, summary, theory cards (heading + Mechanism + Refs with DOIs) and the
  hypothesis rows (table rows starting with **H…**, either `| **H6.1** statement (+). | …` or `| **H12.1** | statement | …`,
  or bullets `- **H10.1.1 (label):** statement`). Series 5 uses `- **H1:**` → code H5.1 (alias H1).
- ergebnisse/H*/ and nested ergebnisse/*/H*/ (e.g. _sample): kurz.md ("Pre-registration (written …)" section → frozen
  pre-registration, frozen_at = prereg.stamp, else the heading time, else time unknown), verdict.json → result
  (verdict mapping 'partly supported' → partly_supported …; a `sample: true` run is stored as sample run), other kurz.md
  sections → method doc, files → evidence, scripts named <code>_*.py → scripts with sha256, charts/*.csv → slide outputs.
  Series 5 folders (C1, K1 …) map to their hypothesis by the "(H12)" in the kurz.md title; their verdicts come from the
  verdict table in ERGEBNISSE.md.
- ERGEBNISSE.md verdict tables: a hypothesis that has no folder gets its result from there (flagged imported_partial).
- serie-2/3/4, the analyses of k8-autohandel: exploratory analyses without hypotheses → findings (quantitative,
  'quantified'), statement from the ERGEBNISSE.md entry or the kurz.md "Key finding"/"Befunde" section.
- Hypotheses only listed in a proposal: status idea, flagged imported_partial.

Idempotent: re-runs update imported rows only (never registered ones); a frozen pre-registration is never changed (a
changed source is reported); a verdict.json with a new time adds a new result version. Nothing writes interpretations:
only a kurz.md section titled "Interpretation…" is taken over (as written); everything else stays "Interpretation fehlt".
No model is used – the tables are regular enough for a deterministic parser.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from fundus import research as R
from fundus.store import Store

ACTOR = "research-import"
DEFAULT_PROJECT = "innovation-data"
PROJECTS = {"k8-autohandel": "acme-dealerships"}
SKIP_DIRS = {"integration", "charts", "_lib", "node_modules"}
RESULT_SKIP_FILES = {"kurz.md", "verdict.json", "prereg.stamp"}
CODE_RE = re.compile(r"^H\d+(?:\.\d+)+[a-z]?$")


# ------------------------------------------------------------------ markdown helpers
def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def _ts(s: str | None) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::(\d{2}))?", s or "")
    return f"{m.group(1)} {m.group(2)}:{m.group(3) or '00'}" if m else None


def _clean(s: str | None) -> str:
    s = (s or "").replace("**", "").replace("\\|", "|")
    s = re.sub(r"(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])", r"\1", s)      # *emphasis*
    return re.sub(r"[ \t]+", " ", s).strip()


def sections(text: str, level: int = 2) -> list[tuple[str, str]]:
    """[(heading, body)] for headings of exactly `level` (body up to the next heading of that level or higher)."""
    out, head, buf = [], None, []
    pat = re.compile(r"^(#{1,%d})\s+(.*)$" % level)
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            if head is not None:
                out.append((head, "\n".join(buf).strip()))
            head, buf = (m.group(2).strip() if len(m.group(1)) == level else None), []
            continue
        if head is not None:
            buf.append(line)
    if head is not None:
        out.append((head, "\n".join(buf).strip()))
    return out


def bold_paragraphs(text: str) -> dict[str, str]:
    """'**Key finding.** text' paragraphs (series 4 style) → {'key finding': text}."""
    out = {}
    for m in re.finditer(r"^\*\*([^*\n]{1,60}?)\.?\*\*\.?\s*(.+?)(?=\n\s*\n|\n\*\*[^*\n]{1,60}\*\*|\Z)", text, re.M | re.S):
        out.setdefault(m.group(1).strip().rstrip(".").lower(), m.group(2).strip())
    return out


def title_of(text: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def _strip_id(title: str | None, ident: str | None = None) -> str | None:
    if not title:
        return None
    t = re.sub(r"^\s*(?:H\d+(?:\.\d+)*[a-z]?|[A-Z]{1,3}\d+[a-z]?(?:[_/][A-Z]?\d+)*|S\d+\.\d+_\w+)\s*[–—:-]\s*", "", title).strip()
    t = re.sub(r"\s*(?:[–—-]\s*INTERNAL|\((?:internal|intern)\))\s*$", "", t, flags=re.I).strip()
    return t or title


def _direction(text: str) -> str | None:
    m = re.search(r"\((\+|−|-|–|≠)(?:[,;][^)]*)?\)\s*\.?\s*$", (text or "").strip()) or \
        re.search(r"[Pp]rediction:?\s*\((\+|−|-|–|≠)", text or "") or re.search(r"\(sign\s*(\+|−|-|–)\)", text or "") or re.search(r"\((\+|−|–|≠)(?:,[^)]*)?\)", text or "")
    if not m:
        return None
    return {"+": "+", "-": "−", "–": "−", "−": "−", "≠": "≠"}[m.group(1)]


def norm_code(raw: str, series_num: str | None) -> tuple[str, list[str]]:
    """('H5.1', ['H1']) for series 5's bare codes; ('H5.Q30a', ['Q30a']); others unchanged."""
    c = _clean(raw).strip(" :.*")
    if re.fullmatch(r"H\d+", c) and series_num:
        return f"H{series_num}.{c[1:]}", [c]
    if re.fullmatch(r"Q\d+[a-z]?", c) and series_num:
        return f"H{series_num}.{c}", [c]
    return c, []


def series_of_code(code: str) -> str | None:
    m = re.match(r"^H(\d+)\.", code)
    return f"serie-{m.group(1)}" if m else None


# ------------------------------------------------------------------ proposals
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;\]]+)")


def _refs(block: str) -> list[dict]:
    """Scholar entries from a '- **Refs:**' list: [{name, works [{title, doi, year}]}]."""
    out: dict[str, dict] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- ") or "Refs" in line[:12]:
            continue
        body = line[2:].strip()
        m = re.match(r"(?:Also:\s*)?(.+?)\s*\((\d{4}[a-z]?)(?:/\d{4})?\)", body)
        if not m:
            continue
        name = _clean(m.group(1)).strip(" ,")
        if len(name) > 120:
            continue
        doi = DOI_RE.search(body)
        work = {"title": _clean(body)[:300], "year": m.group(2)[:4]}
        if doi:
            work["doi"] = doi.group(1).rstrip(".)")
        out.setdefault(name, {"name": name, "works": []})["works"].append(work)
    return list(out.values())


def _card_head(head: str) -> tuple[str, str] | None:
    """'T1 Effectuation …' → ('T1', 'Effectuation …'); 'A. Layered … → sub-series 8.1' → ('A', 'Layered …')."""
    h = re.sub(r"\s*→.*$", "", head).strip()
    m = re.match(r"^(\(?[A-Z]{1,2}\d{0,2}\)?)[.:]?\s+(.+)$", h)
    if not m or m.group(1).startswith("H"):
        return None
    return m.group(1).strip("()"), m.group(2).strip()


def parse_proposal(path: Path, series_num: str | None) -> dict:
    text = _read(path)
    title = title_of(text) or path.parent.name
    title = re.sub(r"^\s*Seri(?:es|e)[- ]?\d+\s*(?:[–:—-]\s*)?", "", title).strip() or title
    summary = None
    for head, body in sections(text, 2):
        if re.search(r"summary|zusammenfassung|scope|framing", head, re.I):
            summary = body
            break
    if not summary:
        m = re.search(r"^#\s.+?\n\n(.+?)(?=\n#)", text, re.S | re.M)
        summary = m.group(1).strip() if m else None
    summary = summary[:2500] if summary else None
    # theory cards and the nearest card per line
    cards, card_at = [], {}
    lines = text.splitlines()
    heads = [(i, len(m.group(1)), m.group(2).strip()) for i, l in enumerate(lines) for m in [re.match(r"^(#{2,4})\s+(.*)$", l)] if m]
    for k, (i, lvl, head) in enumerate(heads):
        end = next((j for j, l2, _ in heads[k + 1:] if l2 <= lvl), len(lines))
        nxt = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        own = "\n".join(lines[i + 1:nxt])
        if "**Refs" not in own and "**Mechanism" not in own:
            continue
        ch = _card_head(head)
        if not ch:
            continue
        mech = re.search(r"\*\*Mechanism:\*\*(.+?)(?=\n- \*\*Refs|\n\||\Z)", own, re.S)
        refs = re.search(r"\*\*Refs:\*\*(.+?)(?=\n\n|\n\||\Z)", own, re.S)
        claim = re.sub(r"\s*\n\s*-?\s*", " ", mech.group(1)).strip() if mech else None
        cards.append({"id": ch[0], "name": ch[1], "claim": _clean(claim)[:900] if claim else None,
                      "scholars": _refs(refs.group(1)) if refs else []})
        for j in range(i, end):
            card_at[j] = ch[0]
    # hypothesis rows
    hyps, seen, header = [], set(), None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            m = re.match(r"^\*\*(H\d+(?:\.\d+)*[a-z]?)\*\*\s*(.*)$", cells[0]) if cells else None
            if not m:
                if cells and not re.match(r"^\*\*[A-Z]", cells[0]):
                    header = [_clean(c) for c in cells]
                continue
            code, alias = norm_code(m.group(1), series_num)
            rest = m.group(2).strip()
            cols = header or []
            if rest:
                statement, meta_cells, meta_heads = rest, cells[1:], cols[1:]
            else:
                statement, meta_cells, meta_heads = (cells[1] if len(cells) > 1 else ""), cells[2:], cols[2:]
            meta = {h or f"col{k}": _clean(v) for k, (h, v) in enumerate(zip(meta_heads, meta_cells)) if _clean(v)}
            theory = card_at.get(i)
            if not theory and meta.get("Theory"):
                theory = meta["Theory"]
            item = {"code": code, "aliases": alias, "statement": _clean(statement), "direction": _direction(statement),
                    "theory": [theory] if theory else [], "meta": meta, "line": i + 1}
        else:
            m = re.match(r"^[-*]\s+\*\*(H\d+(?:\.\d+)*[a-z]?)\s*(?:\(([^)]*)\))?:?\*\*:?\s*(.+)$", s)
            if not m:
                continue
            code, alias = norm_code(m.group(1), series_num)
            body = m.group(3).strip()
            # series 5: the statement may continue on indented sub-bullets
            k = i + 1
            while k < len(lines) and (lines[k].startswith("  ") and lines[k].strip()) and not re.match(r"^- \*\*", lines[k]):
                body += " " + re.sub(r"^[-*]\s+", "", lines[k].strip())
                k += 1
            data = re.search(r"\*([^*]+)\*\s*$", body)
            meta = {"data": _clean(data.group(1))} if data else {}
            body = re.sub(r"\s*\*[^*]+\*\s*$", "", body)
            item = {"code": code, "aliases": alias, "statement": _clean(body), "title": m.group(2),
                    "direction": _direction(body), "theory": [card_at[i]] if card_at.get(i) else [], "meta": meta, "line": i + 1}
        if item["code"] in seen or not item["statement"]:
            continue
        seen.add(item["code"])
        hyps.append(item)
    return {"title": title, "summary": summary, "cards": cards, "hypotheses": hyps}


def parse_s5_plan(text: str) -> dict[str, dict]:
    """Series 5 CONCEPT: per analysis '### C1 – … (H1)' the plan fields (Data, Method, Decision …) → {H1: {…}}."""
    out = {}
    for head, body in sections(text, 3):
        m = re.search(r"\((H\d+)\)\s*$", head)
        if not m:
            continue
        fields = {}
        for fm in re.finditer(r"^- \*\*([^*]+?):\*\*(.*?)(?=^- \*\*|\Z)", body, re.M | re.S):
            fields[fm.group(1).strip().lower()] = fm.group(2).strip()
        out[m.group(1)] = {"analysis": head, "fields": fields}
    return out


# ------------------------------------------------------------------ result folders
PREREG_MAP = [  # (keyword in the bullet label, target field)
    ("decision", "decision_rule"), ("verdict rule", "decision_rule"), ("rule", "decision_rule"),
    ("robust", "robustness_plan"), ("sensitivity", "robustness_plan"), ("placebo", "robustness_plan"),
    ("control", "controls"),
    ("estimator", "method"), ("model", "method"), ("test", "method"), ("design", "method"), ("statistic", "method"),
    ("method", "method"), ("se / ci", "method"), ("arm", "method"),
    ("sample", "sample"), ("unit", "sample"), ("population", "sample"), ("cohort", "sample"), ("cells", "sample"),
    ("years", "sample"),
    ("outcome", "measures"), ("exposure", "measures"), ("treatment", "measures"), ("measure", "measures"),
    ("moderator", "measures"), ("event", "measures"), ("variable", "measures"), ("construct", "measures"),
    ("data", "data_text"), ("hypothesis", "statement"), ("theory", "theory"),
]


def _blocks(body: str) -> list[str]:
    """Bullets ('- …') and bold-labelled paragraphs ('**Hypothesis.** …') of a section, continuation lines joined."""
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        if s.startswith(("- ", "* ")) and not line.startswith("  ") or s.startswith("**") or not out or out[-1] == "":
            out.append(s[2:].strip() if s.startswith(("- ", "* ")) else s)
        else:
            out[-1] += " " + re.sub(r"^[-*]\s+", "", s)
    return [b for b in out if b and not b.startswith("|")]


LABEL_RE = re.compile(r"^\*{0,2}((?:[^:(*.]|\([^)]*\)){1,80}?)[:.]\*{0,2}:?\s+(.*)$", re.S)


def parse_prereg(body: str) -> dict:
    """The bullets of a kurz.md pre-registration mapped onto the prereg fields (unmatched labels go to extra)."""
    fields: dict[str, list[str]] = defaultdict(list)
    extra: dict[str, str] = {}
    for b in _blocks(body):
        m = LABEL_RE.match(b)
        if not m or not re.match(r"^[A-Za-z]", m.group(1).strip()) or len(m.group(1).split()) > 9:
            fields["measures"].append(_clean(b))
            continue
        label, val = _clean(m.group(1)), _clean(m.group(2))
        low = re.sub(r"\([^)]*\)", "", label).lower().strip()
        target = next((t for k, t in PREREG_MAP if k in low), None)
        if target is None:
            extra[label] = val
        elif target in ("measures", "method", "sample", "robustness_plan", "controls"):
            fields[target].append(f"{label}: {val}")
        else:
            fields[target].append(val)
    out = {k: "\n".join(v) for k, v in fields.items() if v}
    if extra:
        out["extra"] = extra
    return out


def parse_kurz(text: str) -> dict:
    secs = sections(text, 2)
    out = {"title": title_of(text), "prereg": None, "prereg_time": None, "method": [], "interpretation": {}, "sections": secs}
    for head, body in secs:
        low = head.lower()
        if low.startswith("pre-registration") or low.startswith("präregistrierung"):
            out["prereg"] = body
            out["prereg_time"] = _ts(head)
        elif low.startswith("interpretation"):
            key = "limits" if "limit" in low or "grenz" in low else "summary"
            out["interpretation"][key] = ((out["interpretation"].get(key) or "") + "\n\n" + body).strip()
        elif low.startswith(("verdict", "inputs", "identification caveats", "robustness", "dateien", "files")):
            continue
        else:
            out["method"].append(f"## {head}\n\n{body}")
    return out


CI_RE = re.compile(r"^\s*(?:bootstrap|wilson|hc\d|95 ?%? ?ci)?\s*\[\s*([+−-]?\d+(?:\.\d+)?)\s*,\s*([+−-]?\d+(?:\.\d+)?)\s*\]\s*(pp|%)?\s*$", re.I)


def _f(s: str) -> float:
    return float(s.replace("−", "-"))


NUM = r"[+−-]?\d+(?:\.\d+)?"
INTERVAL_RE = re.compile(r"\[\s*(" + NUM + r")\s*,\s*(" + NUM + r")\s*\]\s*(pp|%)?")
EST_BEFORE_RE = re.compile(r"(" + NUM + r")\s*(pp|%)?\s*$")


def parse_numbers(effect, ci) -> dict:
    """estimate / ci_low / ci_high / unit from the effect and CI texts – only when unambiguous: one estimate and one
    bracketed interval. 'Poisson log-ratio … +0.021 [-0.004, +0.045]' → 0.021, -0.004, 0.045. A CI field with a single
    interval gives the CI; the estimate then needs exactly one '= number' in the effect (or the one interval of the
    effect being that CI). Estimates outside their interval are dropped."""
    eff, ci = str(effect or ""), str(ci or "").strip()
    out: dict = {}
    ivs = list(INTERVAL_RE.finditer(eff))
    m = CI_RE.match(ci) if ci else None
    if ci and not m:
        return out                                   # several or labelled intervals in the CI field: ambiguous
    if m:
        lo, hi, unit = _f(m.group(1)), _f(m.group(2)), m.group(3)
        est = None
        if not ivs:
            nums = re.findall(r"=\s*(" + NUM + r")", eff)
            if len(nums) == 1:
                est = _f(nums[0])
        elif len(ivs) == 1 and (_f(ivs[0].group(1)), _f(ivs[0].group(2))) == (lo, hi):
            b = EST_BEFORE_RE.search(eff[:ivs[0].start()])
            est = _f(b.group(1)) if b else None
            unit = unit or (b.group(2) if b else None) or ivs[0].group(3)
    elif len(ivs) == 1:
        lo, hi = _f(ivs[0].group(1)), _f(ivs[0].group(2))
        b = EST_BEFORE_RE.search(eff[:ivs[0].start()])
        est = _f(b.group(1)) if b else None
        unit = (b.group(2) if b else None) or ivs[0].group(3)
    else:
        return out
    if lo > hi:
        return out
    out.update({"ci_low": lo, "ci_high": hi})
    if unit:
        out["unit"] = unit
    if est is not None and lo <= est <= hi:
        out["estimate"] = est
    return out


def result_from_verdict(v: dict) -> dict:
    """verdict.json → add_result keyword arguments (numbers only where they are unambiguous)."""
    out = {"verdict": v.get("verdict"), "effect": v.get("effect"), "ci_text": v.get("ci"), "robustness": v.get("robustness"),
           "caveats": v.get("caveats"), "at": _ts(v.get("time")), "sample_run": bool(v.get("sample")),
           "partial_data": v.get("partial_data") or None, "skipped": bool(v.get("skipped"))}
    n = v.get("n")
    if isinstance(n, int):
        out["n"] = n
    elif n not in (None, ""):
        out["n_text"] = str(n)
    out.update(parse_numbers(v.get("effect"), v.get("ci")))
    known = {"h", "verdict", "effect", "ci", "n", "robustness", "caveats", "inputs", "time", "sample", "partial_data", "skipped"}
    extra = {k: x for k, x in v.items() if k not in known}
    if extra:
        out["measures"] = extra
    return out


def ergebnisse_verdicts(path: Path, series_num: str | None) -> dict[str, dict]:
    """Verdict tables of an ERGEBNISSE.md: {code: {verdict, effect, ci, n, claim}} (rows whose verdict cell parses)."""
    out, header = {}, None
    for line in _read(path).splitlines():
        s = line.strip()
        if not s.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        if header is None:
            header = [_clean(c).lower() for c in cells]
            continue
        code, alias = norm_code(cells[0], series_num)
        if not re.fullmatch(r"H\d+\.(?:\d+(?:\.\d+)*[a-z]?|Q\d+[a-z]?)", code):
            continue
        row = dict(zip(header, (_clean(c) for c in cells)))
        vcell = next((row[k] for k in row if k.startswith("verdict")), None)
        try:
            verdict = R.norm_verdict(re.sub(r"\s*\(.*$", "", vcell or ""))
        except ValueError:
            continue
        eff = next((row[k] for k in row if k.startswith(("effect", "key estimate", "key evidence"))), None)
        out[code] = {"verdict": verdict, "effect": eff, "ci": next((row[k] for k in row if "ci" in k.split()), None),
                     "n": row.get("n"), "claim": row.get("claim (short)") or row.get("test"), "verdict_text": vcell,
                     "aliases": alias}
    return out


def charts_map(root: Path) -> dict[tuple[str, str], list[int]]:
    """(series number, token) → exhibit ids from charts/fundus_exhibits*.csv (token: H8.1 or the folder id C1/Q3)."""
    out = defaultdict(list)
    for f in sorted((root / "charts").glob("fundus_exhibits*.csv")):
        try:
            rows = list(csv.DictReader(f.open(encoding="utf-8")))
        except (OSError, csv.Error):
            continue
        for r in rows:
            a, eid = r.get("analysis") or "", r.get("exhibit_id") or ""
            m = re.match(r"serie-(\d+)(?:-\d+)?/([^/]+)", a)
            if not m or not eid.strip().isdigit():
                continue
            tok = m.group(2).split("_")[0]
            if re.fullmatch(r"H\d+(?:-\d+)+", tok):
                tok = tok.replace("-", ".")
            if int(eid) not in out[(m.group(1), tok)]:
                out[(m.group(1), tok)].append(int(eid))
    return out


def find_scripts(series_dir: Path, code: str) -> list[dict]:
    sk = series_dir / "skripte"
    if not sk.is_dir():
        return []
    out = []
    for p in sorted(sk.rglob(f"{code}_*.py")) + sorted(sk.rglob(f"{code}.py")):
        out.append({"path": str(p), "sha256": R._file_sha(p)})
    return out


def ergebnisse_entries(path: Path) -> dict[str, tuple[str, str]]:
    """'**S1 10-K: … (dataset:44)** – text' → {'S1': (title, text)} from an ERGEBNISSE.md."""
    out = {}
    for m in re.finditer(r"^\*\*([A-Z]{1,3}\d+(?:[/_][A-Z]?\d+)*[a-z]?)\s+([^*]+)\*\*\s*[–—-]\s*(.+)$", _read(path), re.M):
        out.setdefault(m.group(1).replace("/", "_"), (_clean(m.group(2)), _clean(m.group(3))))
    return out


# ------------------------------------------------------------------ the run
class Report:
    def __init__(self):
        self.c = Counter()
        self.warnings: list[str] = []

    def add(self, key, n=1):
        self.c[key] += n

    def warn(self, msg):
        if len(self.warnings) < 300:
            self.warnings.append(msg)


def _discover(root: Path) -> list[Path]:
    dirs = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith((".", "_"))]
    return sorted(dirs, key=lambda p: R._code_key(p.name))


def _result_dirs(series_dir: Path) -> list[Path]:
    e = series_dir / "ergebnisse"
    if not e.is_dir():
        return []
    out = []
    for p in sorted(e.rglob("*")):
        if p.is_dir() and ((p / "kurz.md").exists() or (p / "verdict.json").exists() or (p / "prereg.stamp").exists()):
            out.append(p)
    return out


def run(store: Store, root: str | Path, dry_run: bool = False, actor: str = ACTOR) -> dict:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a folder: {root}")
    rep = Report()
    old_actor, store.actor = store.actor, actor
    store._research_batch = True
    try:
        _run(store, root, rep)
        imported = [r[0] for r in store.db.execute("SELECT id FROM hypothesis WHERE source LIKE 'imported:%'")]
        ctx = R._ctx(store)
        flags = Counter()
        for hid in imported:
            h = store.db.execute("SELECT * FROM hypothesis WHERE id = ?", (hid,)).fetchone()
            for f in R._flags_h(store, h, ctx):
                flags[f["flag"]] += 1
        for f in store.db.execute("SELECT * FROM finding WHERE source LIKE 'imported:%'"):
            for x in R._flags_f(store, f):
                flags["finding_" + x["flag"]] += 1
        totals = {"series": store.db.execute("SELECT count(*) FROM research_series WHERE source LIKE 'imported:%'").fetchone()[0],
                  "hypotheses": len(imported),
                  "preregs_frozen": store.db.execute("SELECT count(*) FROM hypothesis_prereg p JOIN hypothesis h ON h.id = p.hypothesis_id "
                                                     "WHERE h.source LIKE 'imported:%' AND p.frozen_at IS NOT NULL").fetchone()[0],
                  "results": store.db.execute("SELECT count(*) FROM hypothesis_result WHERE source LIKE 'imported:%'").fetchone()[0],
                  "findings": store.db.execute("SELECT count(*) FROM finding WHERE source LIKE 'imported:%'").fetchone()[0],
                  "imported_partial": store.db.execute("SELECT count(*) FROM hypothesis WHERE source LIKE 'imported:%' "
                                                       "AND imported_partial = 1").fetchone()[0],
                  "status": dict(store.db.execute("SELECT status, count(*) FROM hypothesis WHERE source LIKE 'imported:%' "
                                                  "GROUP BY status").fetchall()),
                  "verdicts_current": dict(Counter(R._current_result(store, h)["verdict"] for h in imported
                                                   if R._current_result(store, h) is not None))}
        out = {"root": str(root), "dry_run": dry_run, "changes": dict(sorted(rep.c.items())), "totals": totals,
               "integrity_flags": dict(sorted(flags.items())), "warnings": rep.warnings}
        if dry_run:
            store.db.rollback()
        else:
            store.db.commit()
        return out
    except Exception:
        store.db.rollback()
        raise
    finally:
        store._research_batch = False
        store.actor = old_actor


def _upsert_series(store: Store, rep: Report, slug: str, title: str, summary, proposal: Path | None, cards, project) -> int:
    r = store.db.execute("SELECT * FROM research_series WHERE slug = ?", (slug,)).fetchone()
    src = f"imported:{proposal}" if proposal else f"imported:{slug}"
    if r is None:
        R.create_series(store, slug, title, project=project, summary=summary, proposal_path=str(proposal) if proposal else None,
                        theory_cards=cards, source=src, actor=ACTOR)
        rep.add("series_created")
        return store.db.execute("SELECT id FROM research_series WHERE slug = ?", (slug,)).fetchone()[0]
    if not r["source"].startswith("imported:"):
        rep.add("series_skipped_registered")
        return r["id"]
    new = {"title": title, "summary": summary, "proposal_path": str(proposal) if proposal else None,
           "theory_cards": R._j(R._cards(cards))}
    if any((r[k] or None) != (v or None) for k, v in new.items()):
        store.db.execute("UPDATE research_series SET title = ?, summary = ?, proposal_path = ?, theory_cards = ?, updated_at = ? "
                         "WHERE id = ?", (*new.values(), R._now(), r["id"]))
        store.log("research_series", r["id"], "update", {"source": "reimport"})
        rep.add("series_updated")
    return r["id"]


def _run(store: Store, root: Path, rep: Report) -> None:
    exhibits = charts_map(root)
    known_ex = {r[0] for r in store.db.execute("SELECT id FROM exhibit")}
    dirs = _discover(root)
    # pass 1: series and proposal hypotheses
    plans: dict[str, dict] = {}            # code → {series, statement, … from the proposal}
    series_ids: dict[str, int] = {}
    series_dirs: dict[str, Path] = {}
    s5_plan: dict = {}
    for d in dirs:
        slug = d.name
        num = R._series_num(slug) if slug.startswith("serie-") else None
        prop = next((d / f for f in ("RESEARCH-PROPOSAL.md", "CONCEPT.md") if (d / f).exists()), None)
        erg = d / "ERGEBNISSE.md"
        if prop:
            pp = parse_proposal(prop, num)
            title, summary, cards = pp["title"], pp["summary"], pp["cards"]
            for h in pp["hypotheses"]:
                h["source_file"] = prop
                h["series"] = series_of_code(h["code"]) or slug
                plans.setdefault(h["code"], h)
            if num == "5":
                s5_plan = parse_s5_plan(_read(prop))
        elif erg.exists():
            t = title_of(_read(erg)) or slug
            title = re.sub(r"^\s*Seri(?:es|e)[- ]?\d+\s*[–:—-]?\s*", "", t).strip() or t
            m = re.match(r"^(?:Results|Ergebnisse)\s*\((.+?)\)\s*,?\s*(.*)$", title)
            if m:
                title = f"{m.group(1)}" + (f" ({m.group(2)})" if m.group(2) else "")
            summary, cards = None, None
        elif slug == "k8-autohandel":
            title, summary, cards = "K8 Autohandel – KI im Autohandel (Acme Dealership Academy)", None, None
        else:
            if not _result_dirs(d):
                rep.warn(f"{slug}: no proposal, no results – skipped")
                continue
            title, summary, cards = slug, None, None
        series_ids[slug] = _upsert_series(store, rep, slug, title, summary, prop, cards, PROJECTS.get(slug, DEFAULT_PROJECT))
        series_dirs[slug] = d
    # pass 2: result folders per hypothesis code (all series), findings for analyses without hypotheses
    folders: dict[str, list[Path]] = defaultdict(list)
    finding_dirs: list[tuple[str, Path]] = []
    s5_map: dict[str, list[str]] = {}       # series 5 folder → hypothesis codes
    for slug, d in series_dirs.items():
        num = R._series_num(slug) if slug.startswith("serie-") else None
        for rd in _result_dirs(d):
            if CODE_RE.match(rd.name):
                folders[rd.name].append(rd)
                continue
            if rd.parent.name != "ergebnisse":
                continue
            if num == "5":
                t = title_of(_read(rd / "kurz.md")) or ""
                codes = re.findall(r"\((H\d+)\)", t) or [c for c, p in s5_plan.items()
                                                         if p["analysis"].split(" ")[0] == rd.name]
                if rd.name == "Q30":
                    codes = ["Q30a", "Q30b", "Q30c"]
                if codes:
                    for c in codes:
                        code = norm_code(c, "5")[0]
                        folders[code].append(rd)
                        s5_map.setdefault(rd.name, []).append(code)
                else:
                    rep.add("folders_skipped_building_blocks")
                    rep.warn(f"{rd.relative_to(root)}: data building block (no hypothesis) – not imported")
                continue
            if re.search(r"validation|codebook|qc\b", rd.name, re.I):
                rep.add("folders_skipped_building_blocks")
                rep.warn(f"{rd.relative_to(root)}: validation/codebook folder – not imported")
            elif (rd / "kurz.md").exists():
                finding_dirs.append((slug, rd))
            else:
                rep.add("folders_skipped_no_kurz")
    erg_verdicts: dict[str, dict] = {}
    for slug, d in series_dirs.items():
        num = R._series_num(slug) if slug.startswith("serie-") else None
        if (d / "ERGEBNISSE.md").exists():
            for code, v in ergebnisse_verdicts(d / "ERGEBNISSE.md", num).items():
                v["file"] = d / "ERGEBNISSE.md"
                erg_verdicts.setdefault(code, v)
    codes = sorted(set(plans) | set(folders) | {c for c in erg_verdicts if c not in plans and c not in folders},
                   key=R._code_key)
    for code in codes:
        try:
            _import_hypothesis(store, rep, root, code, plans.get(code), folders.get(code, []), erg_verdicts.get(code),
                               series_ids, series_dirs, exhibits, known_ex, s5_plan)
        except (ValueError, LookupError, PermissionError) as e:
            rep.add("hypotheses_failed")
            rep.warn(f"{code}: {e}")
    for slug, rd in sorted(finding_dirs, key=lambda x: (R._code_key(x[0]), R._code_key(x[1].name))):
        try:
            _import_finding(store, rep, root, slug, rd, series_ids, series_dirs, exhibits, known_ex)
        except (ValueError, LookupError) as e:
            rep.add("findings_failed")
            rep.warn(f"{rd.relative_to(root)}: {e}")


def _pick_prereg(dirs: list[Path]) -> tuple[Path | None, dict | None, str | None, str]:
    """The pre-registration of a code: from the main folder first (not _sample). frozen_at = the prereg.stamp of THAT
    folder, else the time in its heading, else unknown – the stamp of a sample run never stands in for it."""
    for rd in sorted(dirs, key=lambda p: ("_sample" in p.parts, len(p.parts))):
        k = parse_kurz(_read(rd / "kurz.md")) if (rd / "kurz.md").exists() else None
        if not (k and k["prereg"]):
            continue
        st = _ts(_read(rd / "prereg.stamp")) if (rd / "prereg.stamp").exists() else None
        if st:
            return rd, k, st, "stamp"
        if k["prereg_time"]:
            return rd, k, k["prereg_time"], "heading"
        return rd, k, None, "unknown"
    return None, None, None, "unknown"


def _import_hypothesis(store, rep, root, code, plan, dirs, erg, series_ids, series_dirs, exhibits, known_ex, s5_plan):
    slug = series_of_code(code)
    if slug not in series_ids:
        # e.g. a code of a series folder that does not exist: attach to the folder's series
        slug = (plan or {}).get("series") or (dirs[0].parents[1].name if dirs else None)
        if dirs and slug not in series_ids:
            slug = next((s for s, d in series_dirs.items() if d in dirs[0].parents), None)
    sid = series_ids.get(slug)
    if sid is None:
        rep.warn(f"{code}: no series – skipped")
        rep.add("hypotheses_skipped_no_series")
        return
    num = R._series_num(slug)
    prereg_dir, kurz, frozen_at, time_source = _pick_prereg(dirs)
    first_kurz = kurz or next((parse_kurz(_read(d / "kurz.md")) for d in dirs if (d / "kurz.md").exists()), None)
    title = _strip_id(first_kurz["title"], code) if first_kurz and first_kurz.get("title") else None
    if title and re.search(r"\((H\d+)\)\s*$", title):
        title = re.sub(r"\s*\(H\d+\)\s*$", "", title)
    aliases = list((plan or {}).get("aliases") or (erg or {}).get("aliases") or [])
    for d in dirs:
        if d.name != code and d.name not in aliases and num == "5":
            aliases.append(d.name)
    pre_fields = parse_prereg(kurz["prereg"]) if kurz else {}
    statement = (plan or {}).get("statement") or pre_fields.get("statement") or (erg or {}).get("claim") or title
    if num == "5" and not plan and erg and erg.get("claim"):
        statement = erg["claim"]
    if not statement:
        rep.warn(f"{code}: no statement found – skipped")
        rep.add("hypotheses_skipped_no_statement")
        return
    meta = dict((plan or {}).get("meta") or {})
    if num == "5":
        alias = (aliases or [None])[0]
        plan5 = s5_plan.get(alias) if alias else None
        if plan5:
            meta["analysis"] = plan5["analysis"]
            for k in ("data", "method", "decision", "unit/sample"):
                if plan5["fields"].get(k):
                    meta[f"plan: {k}"] = plan5["fields"][k][:1500]
    if dirs:
        meta["folders"] = [str(d.relative_to(root)) for d in dirs]
    if plan and plan.get("title"):
        title = title or plan["title"]
    direction = (plan or {}).get("direction") or _direction(pre_fields.get("theory", "")) or _direction(statement)
    partial = not dirs
    src_file = (prereg_dir / "kurz.md") if prereg_dir else (plan or {}).get("source_file") or (erg or {}).get("file") or (dirs[0] if dirs else None)
    source = f"imported:{Path(src_file).relative_to(root) if src_file and str(src_file).startswith(str(root)) else src_file}"
    created_at = frozen_at or (_mtime(Path(plan["source_file"])) if plan and plan.get("source_file") else None)
    h = store.db.execute("SELECT * FROM hypothesis WHERE series_id = ? AND code = ?", (sid, code)).fetchone()
    if h is not None and not h["source"].startswith("imported:"):
        rep.add("hypotheses_skipped_registered")
        return
    theory = (plan or {}).get("theory") or []
    if h is None:
        R.create_hypothesis(store, statement, code=code, series=sid, kind="quantitative", title=title, direction=direction,
                            theory_refs=theory or None, visibility="internal", actor=ACTOR, source=source,
                            imported_partial=partial, aliases=aliases or None, meta=meta or None,
                            created_at=created_at)
        h = store.db.execute("SELECT * FROM hypothesis WHERE series_id = ? AND code = ?", (sid, code)).fetchone()
        rep.add("hypotheses_created")
    else:
        p0 = R._prereg(store, h["id"])
        upd = {"title": title, "theory_refs": R._j(theory or None), "aliases": R._j(aliases or None), "meta": R._j(meta or None),
               "imported_partial": int(partial)}
        if not (p0 and p0["frozen_at"]):
            upd.update({"statement": statement, "direction": direction})
        if any((h[k] if h[k] is not None else None) != v for k, v in upd.items()):
            store.db.execute(f"UPDATE hypothesis SET {', '.join(f'{k} = ?' for k in upd)}, updated_at = ? WHERE id = ?",
                             (*upd.values(), R._now(), h["id"]))
            store.log("hypothesis", h["id"], "update", {"source": "reimport"})
            rep.add("hypotheses_updated")
        h = store.db.execute("SELECT * FROM hypothesis WHERE id = ?", (h["id"],)).fetchone()
    if partial:
        rep.add("hypotheses_imported_partial")
    # pre-registration
    if kurz:
        data = [{"ref": r, **({"version": v} if v else {})} for r, v in R.parse_dataset_refs(kurz["prereg"])]
        fields = {"statement": pre_fields.get("statement") or statement, "direction": direction,
                  "unit": None, "sample": pre_fields.get("sample"), "measures": pre_fields.get("measures"),
                  "data": R._j(data) if data else pre_fields.get("data_text"), "method": pre_fields.get("method"),
                  "controls": pre_fields.get("controls"), "decision_rule": pre_fields.get("decision_rule"),
                  "robustness_plan": pre_fields.get("robustness_plan"),
                  "extra": R._j({**({"theory": pre_fields["theory"]} if pre_fields.get("theory") else {}),
                                 **(pre_fields.get("extra") or {})} or None),
                  "text": kurz["prereg"]}
        psrc = f"imported:{(prereg_dir / 'kurz.md').relative_to(root)}"
        p = R._prereg(store, h["id"])
        fa = frozen_at or R._now()
        if p and p["frozen_at"]:
            fa_cmp = p["frozen_at"]
            new_sha = hashlib.sha256(R.canonical_prereg(h["code"], {**fields, "frozen_at": fa_cmp, "source": psrc}).encode()).hexdigest()
            if new_sha != p["sha256"]:
                rep.add("prereg_source_changed")
                rep.warn(f"{code}: the pre-registration in {psrc[9:]} differs from the frozen one – left unchanged")
            else:
                rep.add("preregs_unchanged")
        else:
            if p:
                store.db.execute("DELETE FROM hypothesis_prereg WHERE hypothesis_id = ? AND frozen_at IS NULL", (h["id"],))
            R._freeze(store, h, fields, fa, time_source, psrc, ACTOR)
            rep.add("preregs_frozen")
            if time_source == "unknown":
                rep.add("preregs_time_unknown")
    # results: verdict.json per folder
    outputs = [f"exhibit:{e}" for e in exhibits.get((num, code), []) + sum((exhibits.get((num, d.name.split('_')[0]), [])
                                                                          for d in dirs if num == "5"), []) if e in known_ex]
    scripts = []
    for d in dirs:
        sd = next((p for p in d.parents if p.name.startswith("serie-") or p.name == "k8-autohandel"), None)
        if sd:
            for s in find_scripts(sd, code if num != "5" else d.name):
                if s not in scripts:
                    scripts.append(s)
    added_any = False
    for d in dirs:
        vf = d / "verdict.json"
        if not vf.exists():
            continue
        try:
            v = json.loads(_read(vf))
        except ValueError:
            rep.warn(f"{vf.relative_to(root)}: invalid JSON")
            continue
        kw = result_from_verdict(v)
        kw["at"] = kw["at"] or _mtime(vf)
        src = f"imported:{vf.relative_to(root)}"
        added_any |= _add_result(store, rep, h, kw, src, v.get("inputs") or [], d, root, scripts, outputs)
    if not any((d / "verdict.json").exists() for d in dirs) and erg:
        ef = erg["file"]
        kd = next((d for d in dirs if (d / "kurz.md").exists()), None)
        inputs = [f"{r}" + (f" v{ver}" if ver else "") for r, ver in R.parse_dataset_refs(_read(kd / "kurz.md"))] if kd else []
        kw = {"verdict": erg["verdict"], "effect": erg.get("effect"), "ci_text": erg.get("ci"),
              "at": _mtime(kd / "kurz.md") if kd else _mtime(ef)}
        n = (erg.get("n") or "").replace(",", "").replace("’", "")
        if n.isdigit():
            kw["n"] = int(n)
        elif erg.get("n"):
            kw["n_text"] = erg["n"]
        kw.update(parse_numbers(erg.get("effect"), erg.get("ci")))
        if erg.get("verdict_text") and erg["verdict_text"].lower() != erg["verdict"].replace("_", " "):
            kw["measures"] = {"verdict_text": erg["verdict_text"]}
        _add_result(store, rep, h, kw, f"imported:{ef.relative_to(root)}#{code}", inputs, kd, root, scripts, outputs)
    # interpretation sections written in kurz.md (taken over as written; never generated)
    if kurz and kurz["interpretation"]:
        cur = R._current_result(store, h["id"])
        if cur and not store.db.execute("SELECT 1 FROM interpretation WHERE owner_kind = 'hypothesis_result' AND owner_id = ?",
                                        (cur["id"],)).fetchone():
            R.set_interpretation(store, "result", cur["id"], kurz["interpretation"], author="kurz.md (Analyse)",
                                 source=f"imported:{(prereg_dir / 'kurz.md').relative_to(root)}")
            rep.add("interpretations_imported")


def _add_result(store, rep, h, kw, src, inputs, folder, root, scripts, outputs) -> bool:
    if store.db.execute("SELECT 1 FROM hypothesis_result WHERE hypothesis_id = ? AND source = ? AND at = ?",
                        (h["id"], src, kw["at"])).fetchone():
        rep.add("results_unchanged")
        return False
    k = parse_kurz(_read(folder / "kurz.md")) if folder is not None and (folder / "kurz.md").exists() else None
    method_doc = "\n\n".join(k["method"])[:20000] if k and k["method"] else None
    evidence = []
    if folder is not None:
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.name not in RESULT_SKIP_FILES:
                evidence.append({"kind": "file", "ref": str(f), "note": f.name})
    R.add_result(store, h["id"], kw.pop("verdict"), method_doc=method_doc, inputs=inputs, scripts=scripts or None,
                 outputs=outputs or None, source=src, actor=ACTOR, evidence=evidence[:80], **kw)
    rep.add("results_created")
    if kw.get("sample_run"):
        rep.add("results_sample_runs")
    return True


def _import_finding(store, rep, root, slug, rd, series_ids, series_dirs, exhibits, known_ex):
    sid = series_ids[slug]
    num = R._series_num(slug) if slug.startswith("serie-") else None
    text = _read(rd / "kurz.md")
    ident = rd.name.split("_")[0] if not re.match(r"^[A-Z]\d+_[A-Z]\d+_", rd.name) else "_".join(rd.name.split("_")[:2])
    entries = ergebnisse_entries(series_dirs[slug] / "ERGEBNISSE.md") if (series_dirs[slug] / "ERGEBNISSE.md").exists() else {}
    k = parse_kurz(text)
    bp = bold_paragraphs(text)
    secs = {h.lower(): b for h, b in k["sections"]}
    title = _strip_id(k["title"]) or rd.name
    statement = None
    if ident in entries:
        title = _strip_id(entries[ident][0]) or title
        statement = entries[ident][1]
    if not statement:
        for key in ("key finding", "befunde", "befund", "results", "ergebnis", "ergebnisse", "finding", "findings", "so what"):
            hit = next((v for kk, v in {**secs, **bp}.items() if kk.startswith(key)), None)
            if hit:
                statement = hit
                break
    if not statement:
        body = re.sub(r"^#.*$", "", text, count=1, flags=re.M).strip()
        paras = [x for x in re.split(r"\n\s*\n", body) if x.strip() and not re.match(r"^(Sources?|Quellen?|Stand|Status|Script|Skript)\b", x.strip(), re.I)]
        statement = paras[0] if paras else title
    statement = _clean(statement)[:3000]
    method = next((v for kk, v in {**secs, **bp}.items() if kk.startswith(("method", "methode", "design"))), None)
    sample = next((v for kk, v in {**secs, **bp}.items() if kk in ("n", "sample", "data", "daten", "stichprobe")), None)
    code_prefix = f"F{num}." if num else "FK8."
    alias = f"{slug}/{rd.name}"
    src = f"imported:{(rd / 'kurz.md').relative_to(root)}"
    f = store.db.execute("SELECT * FROM finding WHERE series_id = ? AND EXISTS (SELECT 1 FROM json_each(coalesce(aliases, '[]')) "
                         "WHERE value = ?)", (sid, alias)).fetchone()
    evidence = [{"kind": "dataset", "ref": r, **({"version": v} if v else {})} for r, v in R.parse_dataset_refs(text)
                if store.db.execute("SELECT 1 FROM dataset WHERE id = ?", (int(r.split(":")[1]),)).fetchone()]
    evidence += [{"kind": "file", "ref": str(p), "note": p.name} for p in sorted(rd.iterdir())
                 if p.is_file() and p.name not in RESULT_SKIP_FILES][:60]
    evidence += [{"kind": "exhibit", "ref": f"exhibit:{e}"} for e in exhibits.get((num, ident), []) if e in known_ex]
    context = {"setting": "Datenanalyse (explorativ)", **({"sample": _clean(sample)[:600]} if sample else {})}
    if f is not None and not f["source"].startswith("imported:"):
        rep.add("findings_skipped_registered")
        return
    if f is None:
        n = 1 + max([int(m.group(1)) for (c,) in store.db.execute("SELECT code FROM finding WHERE series_id = ?", (sid,))
                     for m in [re.fullmatch(re.escape(code_prefix) + r"(\d+)", c)] if m] or [0])
        R.create_finding(store, statement, code=f"{code_prefix}{n}", series=sid, kind="quantitative", title=title,
                         strength="quantified", context=context, method=_clean(method)[:3000] if method else None,
                         detail=text[:30000], status="recorded", visibility="internal", actor=ACTOR, source=src,
                         aliases=[alias, ident], evidence=evidence, created_at=_mtime(rd / "kurz.md"))
        rep.add("findings_created")
    else:
        upd = {"title": title, "statement": statement, "method": R._txt(_clean(method)[:3000]) if method else None,
               "detail": R._txt(text[:30000]), "context": R._j(context)}
        if any(f[kk] != v for kk, v in upd.items()):
            store.db.execute(f"UPDATE finding SET {', '.join(f'{kk} = ?' for kk in upd)}, updated_at = ? WHERE id = ?",
                             (*upd.values(), R._now(), f["id"]))
            store.log("finding", f["id"], "update", {"source": "reimport"})
            rep.add("findings_updated")
        for e in evidence:
            R.add_evidence(store, f"finding:{f['id']}", actor=ACTOR, **e)
        R._index_f(store, f["id"])
