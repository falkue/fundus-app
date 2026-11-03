"""Datasets: large tables kept in Fundus – stored, profiled, described, findable, viewable and queryable.

A dataset is one or more files: tables (CSV/TSV/TXT, Excel .xlsx, JSON/JSONL, Parquet) and accompanying documents
(codebook, method notes: PDF, Markdown, Word, text). Originals are stored content-addressed under
data/datasets/<sha[:2]>/<sha>.<ext>; every table (a CSV, each Excel sheet, a JSON file) also gets a Parquet copy
(<sha>.parquet) that DuckDB reads for the profile, the preview and SQL queries – fast also for files of many GB.

Profile per table: rows, and per column type, nulls, distinct values (approx.), min/max, mean, quartiles and examples
(DuckDB SUMMARIZE). Description: the local model (gemma4, never a cloud model – datasets can be client data) writes a
summary (de/en), keywords, the meaning of each column, period and geography from the profile and a few rows; Falk's own
title, description and source always win. A new upload into an existing dataset is a new version.

    add(store, paths, title=…, description=…, source=…, project=…, dataset=None)   import (or a new version)
    describe_pending(store) · describe_one(store, id)                            local LLM description
    search(store, text, project=…) · get(store, ref) · preview(store, ref, table, offset, limit) · query(store, ref, sql)
    update · remove(restore=…) · purge_removed(store, days=30)
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from fundus.geo import canonical_geo
from fundus import config as _config
from fundus.store import Store, slug as _slug

TABLE_EXT = {"csv", "tsv", "txt", "dat", "xlsx", "xlsm", "json", "jsonl", "ndjson", "parquet", "gz"}   # gz: only .csv.gz/.tsv.gz   # .dat: delimited text (Census: '|')
DOC_EXT = {"pdf", "md", "docx", "doc", "rtf", "html", "htm", "xhtml", "pptx", "odt", "xml"}      # .xml: e.g. SDMX structures; .xhtml: EUR-Lex
MAX_BYTES = 50 * 1024 ** 3
PREVIEW_MAX = 500
QUERY_MAX_ROWS = 2000
QUERY_TIMEOUT = 60
MEMORY = os.environ.get("FUNDUS_DUCKDB_MEMORY", "12GB")   # DuckDB spills to disk above this (Gemma and sessions need RAM)
SAMPLE_FROM = 2_000_000          # from this many rows, examples and frequent values come from a 1-million-row sample
SAMPLE_ROWS = 1_000_000
PORTAL_UPLOAD_MAX = 2 * 1024 ** 3   # larger files: fundus_dataset_add / `fundus datasets add` (no detour through a browser)
PUBLIC_URL = _config.public_url()   # $FUNDUS_PUBLIC_URL, config file, the running server, else http://127.0.0.1:8010
LLM_MODEL = os.environ.get("FUNDUS_DATASET_MODEL") or _config.llm_settings()["model_text"]   # llm_model_text (fundus.config)
DESCRIBE_TIMEOUT = int(os.environ.get("FUNDUS_DATASET_TIMEOUT", "1800"))   # seconds for one description by the local model
RETRY_TIMEOUTS = 3                 # a description that timed out is tried again (busy model), up to this many attempts
RETRY_AFTER_H = 2                  # … at the earliest this many hours after the last attempt
EDITABLE = {"title", "description", "source_title", "publisher", "url", "license", "published", "period", "geo", "project",
            "visibility", "keywords", "tags", "note", "frequency", "next_expected", "update_hint", "last_checked",
            "kind", "crosswalk", "code_systems", "table_scope", "inputs", "confirm_public",
            # catalog & archive (3 Nov 2026): lifecycle and provenance
            "lifecycle", "superseded_by", "lifecycle_note", "retrieved_at", "script",
            # texts with versions (3 Nov 2026, fundus.dataset_texts): setting act_type makes a dataset a text
            "identifier", "jurisdiction", "act_type", "issuer"}
KINDS = ("data", "crosswalk")
TABLE_SCOPES = ("version", "merged")
# crosswalk metadata (dataset.crosswalk JSON); a table may carry several system pairs in from_system/to_system columns
XWALK_META = ("from_system", "from_version", "to_system", "to_version", "valid_from", "valid_to", "quality", "method", "table",
              "columns", "note")
XWALK_ROLES = ("from_system", "from_version", "from_code", "from_label", "to_system", "to_version", "to_code", "to_label",
               "weight", "relation", "quality", "method", "valid_from", "valid_to")
FREQUENCIES = ("annual", "semiannual", "quarterly", "monthly", "irregular", "once")
MONTHS = {"annual": 12, "semiannual": 6, "quarterly": 3, "monthly": 1}
ISO_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-\d{2})?$")


# ------------------------------------------------------------------ files
def files_dir(store: Store) -> Path:
    return Path(os.environ.get("FUNDUS_DATASET_DIR") or (store.path.parent / "datasets"))


def file_path(store: Store, sha: str, ext: str) -> Path:
    return files_dir(store) / sha[:2] / f"{sha}.{ext}"


def url(sha: str, ext: str, name: str | None = None) -> str:
    return f"{PUBLIC_URL}/datasets/{sha}.{ext}" + (f"?name={name}" if name else "")


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _store_file(store: Store, src: Path, sha: str, ext: str) -> Path:
    dst = file_path(store, sha, ext)
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o644)
        tmp.replace(dst)
    return dst


def _q(p) -> str:
    """A path as a DuckDB string literal."""
    return "'" + str(p).replace("'", "''") + "'"


def _duck(store: Store | None = None):
    """DuckDB with a memory cap; above it, sorts and aggregations spill into data/datasets/tmp."""
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads TO 8")
    con.execute(f"SET memory_limit = {_q(MEMORY)}")
    con.execute("SET preserve_insertion_order = false")       # lower memory for COPY of very large files
    tmp = (files_dir(store) if store is not None else Path(os.environ.get("FUNDUS_DATASET_DIR") or tempfile.gettempdir())) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = {_q(tmp)}")
    return con


# ------------------------------------------------------------------ reading tables into Parquet
def _gz_table(name: str) -> bool:
    """A gzip-compressed delimited text table (x.csv.gz, x.tsv.gz, x.txt.gz) – read by DuckDB without unpacking."""
    return bool(re.search(r"\.(csv|tsv|txt|dat)\.gz$", name.lower()))


def _csv_options(p: Path) -> str:
    """Delimiter and decimal separator: a ';'-separated file is the German/Swiss convention with decimal commas."""
    import gzip
    opener = gzip.open if p.suffix.lower() == ".gz" else open
    with opener(p, "rt", encoding="utf-8", errors="replace") as f:
        head = f.read(65536)
    try:
        delim = csv.Sniffer().sniff(head, delimiters=";,\t|").delimiter
    except csv.Error:
        delim = "\t" if head.count("\t") > head.count(",") else ","
    opts = f"delim={_q(delim)}, header=true, sample_size=200000, ignore_errors=true"
    skip = _preamble_rows(head, delim)
    if skip:
        opts += f", skip={skip}"
    if delim == ";":
        opts += ", decimal_separator=','"
    return opts


def _preamble_rows(head: str, delim: str) -> int:
    """Lines to skip before the real header: a short metadata block followed by a blank line (e.g. SNB data portal CSVs:
    'CubeId;…', 'PublishingDate;…', blank, then 'Date;D0;Value'). Only when every preamble line has fewer fields than the
    header after the blank line, and the blank line comes within the first 10 lines."""
    lines = head.lstrip("\ufeff").splitlines()[:40]
    for i, line in enumerate(lines[:10]):
        if line.strip() or i == 0:
            continue
        before = [ln for ln in lines[:i] if ln.strip()]
        after = [ln for ln in lines[i + 1:] if ln.strip()]
        if not before or len(after) < 2:
            return 0
        width = lambda ln: len(next(csv.reader([ln], delimiter=delim)))
        hdr = width(after[0])
        if hdr >= 2 and all(width(ln) < hdr for ln in before) and all(width(ln) == hdr for ln in after[1:6]):
            return i + 1
        return 0
    return 0


def _xlsx_sheets(p: Path, work: Path) -> list[tuple[str, Path]]:
    """Each non-empty sheet as a CSV (first non-empty row = header)."""
    import openpyxl
    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        f = work / f"{len(out):03d}.csv"
        n = 0
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for row in ws.iter_rows(values_only=True):
                if n == 0 and not any(v not in (None, "") for v in row):
                    continue
                vals = ["" if v is None else (v.isoformat() if hasattr(v, "isoformat") else v) for v in row]
                if n == 0:   # multi-line question headers (survey exports): one line per column name
                    vals = [" ".join(str(v).split()) if isinstance(v, str) else v for v in vals]
                w.writerow(vals)
                n += 1
        if n > 1:
            out.append((ws.title, f))
    wb.close()
    return out


def _to_parquet(store: Store, src: Path, ext: str, work: Path, stem: str) -> list[tuple[str, Path]]:
    """[(table name, parquet file in work/)] for one data file; stem = the original file name without extension."""
    con = _duck(store)
    out = []
    try:
        if ext in ("xlsx", "xlsm"):
            sources = [(name, f"read_csv({_q(f)}, header=true, delim=',', quote='\"', escape='\"', sample_size=-1)")
                       for name, f in _xlsx_sheets(src, work)]   # our own CSV: no dialect sniffing (fails on multi-line cells)
        elif ext in ("csv", "tsv", "txt", "dat"):
            sources = [(stem, f"read_csv({_q(src)}, {_csv_options(src)})")]
        elif ext == "gz":                    # x.csv.gz: stem is 'x.csv'
            sources = [(re.sub(r"\.(csv|tsv|txt|dat)$", "", stem, flags=re.I), f"read_csv({_q(src)}, {_csv_options(src)}, compression='gzip')")]
        elif ext in ("json", "jsonl", "ndjson"):
            sources = [(stem, f"read_json_auto({_q(src)}, maximum_object_size=268435456)")]
        elif ext == "parquet":
            sources = [(stem, f"read_parquet({_q(src)})")]
        else:
            return []
        for i, (name, sql) in enumerate(sources):
            dst = work / f"t{i:03d}.parquet"
            con.execute(f"COPY (SELECT * FROM {sql}) TO {_q(dst)} (FORMAT parquet, COMPRESSION zstd)")
            out.append((name, dst))
    finally:
        con.close()
    return out


def profile(parquet: Path, store: Store | None = None) -> dict:
    """Rows and per-column statistics of a Parquet table (DuckDB SUMMARIZE, one pass, exact rows/min/max/mean), plus
    examples and frequent values – for very large tables from a sample (marked `sampled`)."""
    con = _duck(store)
    try:
        rows = con.execute(f"SELECT count(*) FROM read_parquet({_q(parquet)})").fetchone()[0]   # from the Parquet footer
        sampled = rows >= SAMPLE_FROM
        src = f"read_parquet({_q(parquet)})"
        if sampled:
            con.execute(f"CREATE TEMP TABLE s AS SELECT * FROM {src} USING SAMPLE {SAMPLE_ROWS} ROWS")
        part = "s" if sampled else src
        summ = con.execute(f"SUMMARIZE SELECT * FROM read_parquet({_q(parquet)})")
        names = [d[0] for d in summ.description]
        cols = []
        for r in summ.fetchall():
            c = dict(zip(names, r))
            col = {"name": c["column_name"], "type": c["column_type"], "distinct": c["approx_unique"],
                   "nulls_pct": float(c["null_percentage"]) if c["null_percentage"] is not None else None,
                   "min": c["min"], "max": c["max"]}
            for k in ("avg", "std", "q25", "q50", "q75"):
                if c.get(k) not in (None, ""):
                    col[k] = c[k]
            ident = '"' + str(c["column_name"]).replace('"', '""') + '"'
            ex = con.execute(f"SELECT DISTINCT CAST({ident} AS VARCHAR) FROM {part} "
                             f"WHERE {ident} IS NOT NULL LIMIT 5").fetchall()
            col["examples"] = [str(e[0])[:80] for e in ex]
            if c["approx_unique"] is not None and c["approx_unique"] <= 30 and rows:
                top = con.execute(f"SELECT CAST({ident} AS VARCHAR) v, count(*) n FROM {part} "
                                  f"GROUP BY 1 ORDER BY 2 DESC LIMIT 8").fetchall()
                f = rows / SAMPLE_ROWS if sampled else 1          # sample counts scaled to the table
                col["top"] = [[str(v)[:60] if v is not None else None, round(n * f)] for v, n in top]
            cols.append(col)
        return {"rows": rows, "columns": cols, **({"sampled": SAMPLE_ROWS} if sampled else {})}
    finally:
        con.close()


# ------------------------------------------------------------------ import
def _collect(paths) -> list[Path]:
    out = []
    for p in paths if isinstance(paths, (list, tuple)) else [paths]:
        p = Path(p).expanduser()
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))
        elif p.is_file():
            out.append(p)
        else:
            raise FileNotFoundError(f"not found: {p}")
    return out


def _unique_slug(store: Store, title: str) -> str:
    base = _slug(title)[:70] or "datensatz"
    s, n = base, 1
    while store.db.execute("SELECT 1 FROM dataset WHERE slug = ?", (s,)).fetchone():
        n += 1
        s = f"{base}-{n}"
    return s


def add(store: Store, paths, title: str | None = None, description: str | None = None, source_title: str | None = None,
        publisher: str | None = None, url_: str | None = None, license: str | None = None, published: str | None = None,
        project: str | None = None, visibility: str = "public", dataset=None, note: str | None = None,
        keywords: list[str] | None = None, frequency: str | None = None, next_expected: str | None = None,
        update_hint: str | None = None, actor: str = "cli", progress=None, kind: str | None = None,
        crosswalk: dict | None = None, code_systems: dict | None = None, inputs: list | None = None,
        table_scope: str | None = None, retrieved_at: str | None = None, script: str | None = None,
        text: bool = False, identifier: str | None = None, jurisdiction: str | None = None, act_type: str | None = None,
        issuer: str | None = None, valid_from: str | None = None, valid_to: str | None = None,
        version_label: str | None = None, published_at: str | None = None, source_url: str | None = None) -> dict:
    """Store files as a dataset (or as a new version of `dataset`). Tables become Parquet and get profiled; other files
    are kept as documents. text=True (or a new version of a text): a text with versions (fundus.dataset_texts) – every
    file is kept as delivered as a document (never a table); act_type (new text), identifier, jurisdiction, issuer on the
    dataset; valid_from (required), valid_to, version_label, published_at, source_url on the version. A new version ends
    earlier open versions the day before its valid_from (explicit valid_to values stay). Linked data: kind ('crosswalk' with crosswalk metadata), code_systems of columns, inputs (the
    datasets it was built from – lineage; internal inputs make it internal), table_scope ('merged': tables across versions).
    A new version of a derived dataset without `inputs` keeps the previous inputs at their current versions (it was rebuilt
    from them). Returns {dataset, slug, version, tables, documents, skipped, warnings}."""
    if kind and kind not in KINDS:
        raise ValueError(f"kind: {' | '.join(KINDS)}")
    if table_scope and table_scope not in TABLE_SCOPES:
        raise ValueError(f"table_scope: {' | '.join(TABLE_SCOPES)}")
    for x in _input_refs(inputs):
        _row(store, x["ref"])                # unknown inputs fail before any file is copied
    if visibility not in ("public", "internal"):
        raise ValueError("visibility: public | internal")
    if frequency and frequency not in FREQUENCIES:
        raise ValueError(f"frequency: {' | '.join(FREQUENCIES)}")
    if next_expected and not ISO_MONTH.match(next_expected):
        raise ValueError("next_expected: ISO month, e.g. 2027-09")
    if retrieved_at and not ISO_MONTH.match(retrieved_at):
        raise ValueError("retrieved_at: ISO date, e.g. 2026-11-03")
    if dataset not in (None, "") and (_row(store, dataset)["holding"] or "stored") == "reference":
        raise ValueError("this dataset is a reference (raw data stays where it is); register a new reference instead")
    from fundus import dataset_texts as tx
    existing = _row(store, dataset) if dataset not in (None, "") else None
    text = bool(text or act_type or (existing is not None and tx.is_text(existing)))
    if text:
        if kind or crosswalk or table_scope:
            raise ValueError("a text with versions has no kind/crosswalk/table_scope")
        tmeta = tx.check_meta(act_type=act_type, valid_from=valid_from, valid_to=valid_to, published_at=published_at,
                              need_act_type=existing is None or not tx.is_text(existing))
    files = _collect(paths)
    if not files:
        raise ValueError("no files")
    tables, docs, skipped = [], [], []
    work = files_dir(store) / "incoming" / f"w{os.getpid()}-{threading.get_ident()}-{datetime.now():%H%M%S%f}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        for n, f in enumerate(files, 1):
            ext = f.suffix.lower().lstrip(".")
            if f.stat().st_size > MAX_BYTES:
                skipped.append({"file": str(f), "why": "larger than 50 GB"})
                continue
            if ext not in TABLE_EXT | DOC_EXT or (ext == "gz" and not _gz_table(f.name)):
                skipped.append({"file": str(f), "why": f"unknown format .{ext}" + (" (only .csv.gz/.tsv.gz tables)" if ext == "gz" else "")})
                continue
            sha = _sha_file(f)
            _store_file(store, f, sha, ext)
            entry = {"name": f.name, "sha": sha, "ext": ext, "bytes": f.stat().st_size, "source_path": str(f)}
            if text or ext in DOC_EXT or (ext in ("txt", "dat") and not _looks_tabular(f)):
                docs.append(entry)
            else:
                try:
                    sub = work / sha[:12]
                    sub.mkdir(exist_ok=True)
                    for name, pq in _to_parquet(store, file_path(store, sha, ext), ext, sub, f.stem):
                        psha = _sha_file(pq)
                        _store_file(store, pq, psha, "parquet")
                        prof = profile(file_path(store, psha, "parquet"), store)
                        tables.append({**entry, "table": name, "parquet_sha": psha, "rows": prof["rows"],
                                       "profile": prof})
                except Exception as e:      # a broken file must not stop the others
                    skipped.append({"file": str(f), "why": f"{type(e).__name__}: {e}"[:300]})
                    docs.append(entry)
            if progress:
                progress(n, len(files), f)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not tables and not docs:
        raise ValueError("nothing importable: " + "; ".join(s["why"] for s in skipped[:3]))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with store.batch(actor):
        if dataset not in (None, ""):
            d = _row(store, dataset)
            did = d["id"]
            n = (d["current_version"] or 0) + 1
            store.db.execute("UPDATE dataset SET current_version = ?, status = 'new', updated_at = ? WHERE id = ?", (n, now, did))
        else:
            t = title or (files[0].stem.replace("_", " ").replace("-", " ") if len(files) == 1 else files[0].parent.name)
            did = store.db.execute(
                "INSERT INTO dataset(slug, title, description, source_title, publisher, url, license, published, project, visibility, "
                "keywords, note, current_version, status, created_at, updated_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'new',?,?,?)",
                (_unique_slug(store, t), t, description, source_title, publisher, url_, license, published,
                 _slug(project) if project else None, visibility, json.dumps(keywords or [], ensure_ascii=False), note, now, now, actor)).lastrowid
            n = 1
        store.db.execute("INSERT INTO dataset_version(dataset_id, n, note, created_at, created_by) VALUES (?,?,?,?,?)",
                         (did, n, note, now, actor))
        for t in tables:
            store.db.execute("INSERT INTO dataset_file(dataset_id, version, role, name, table_name, sha, ext, bytes, source_path, "
                             "parquet_sha, rows, profile, columns_n) VALUES (?,?,'table',?,?,?,?,?,?,?,?,?,?)",
                             (did, n, t["name"], t["table"], t["sha"], t["ext"], t["bytes"], t["source_path"], t["parquet_sha"],
                              t["rows"], json.dumps(t["profile"], ensure_ascii=False, default=str), len(t["profile"]["columns"])))
        for dct in docs:
            store.db.execute("INSERT INTO dataset_file(dataset_id, version, role, name, sha, ext, bytes, source_path) "
                             "VALUES (?,?,'document',?,?,?,?,?)", (did, n, dct["name"], dct["sha"], dct["ext"], dct["bytes"], dct["source_path"]))
        upd = {k: v for k, v in (("frequency", frequency), ("next_expected", next_expected), ("update_hint", update_hint),
                                 ("retrieved_at", retrieved_at), ("script", script)) if v}
        if dataset not in (None, ""):        # a new edition arrived: it counts as checked today
            upd["last_checked"] = now[:10]
        if upd:
            store.db.execute(f"UPDATE dataset SET {', '.join(f'{k} = ?' for k in upd)} WHERE id = ?", (*upd.values(), did))
        store.log("dataset", did, "insert" if n == 1 else "version", {"version": n, "tables": len(tables), "documents": len(docs)})
        slug = store.db.execute("SELECT slug FROM dataset WHERE id = ?", (did,)).fetchone()[0]
        warnings, closed = [], []
        if text:
            tf = {k: v.strip() for k, v in (("identifier", identifier), ("jurisdiction", jurisdiction), ("act_type", act_type),
                                            ("issuer", issuer)) if v and v.strip()}
            if not (frequency or (existing is not None and existing["frequency"])):
                tf["frequency"] = "irregular"      # new versions appear irregularly: tracked by due()/update_targets
            if tf:
                store.db.execute(f"UPDATE dataset SET {', '.join(f'{k} = ?' for k in tf)} WHERE id = ?", (*tf.values(), did))
            closed = tx.apply_version(store, did, n, valid_from=tmeta["valid_from"], valid_to=tmeta["valid_to"],
                                      version_label=version_label, published_at=tmeta["published_at"], source_url=source_url)
        if inputs is None and n > 1:           # rebuilt: same inputs, now at their current versions
            prev = [{"ref": f"dataset:{r['input_id']}", "note": r["note"]} for r in store.db.execute(
                "SELECT input_id, note FROM dataset_input WHERE dataset_id = ? AND version = ?", (did, n - 1))]
            if prev:
                inputs = prev
        if inputs:
            _set_inputs(store, _row(store, did), inputs, n)
            internal = internal_inputs(store, did)
            if internal and _row(store, did)["visibility"] == "public":
                store.db.execute("UPDATE dataset SET visibility = 'internal' WHERE id = ?", (did,))
                store.log("dataset", did, "update", {"visibility": {"old": "public", "new": "internal"},
                                                     "because": f"inputs {', '.join(internal)} are internal"})
                warnings.append(f"stored as internal: built from internal {', '.join(internal)}")
        linked = {k: v for k, v in (("kind", kind), ("crosswalk", crosswalk), ("code_systems", code_systems),
                                    ("table_scope", table_scope)) if v}
        cur = _row(store, did)
        if not linked and cur["kind"] == "crosswalk":      # a new edition of a crosswalk: recount its system pairs
            linked = {"kind": "crosswalk"}
        if linked:
            warnings += _update_linked(store, did, linked)
    return {"dataset": did, "ref": f"dataset:{did}", "slug": slug, "version": n, **({"warnings": warnings} if warnings else {}),
            **({"text": True, "closed": closed} if text else {}),
            "tables": [{"table": t["table"], "file": t["name"], "rows": t["rows"], "columns": len(t["profile"]["columns"])} for t in tables],
            "documents": [d["name"] for d in docs], "skipped": skipped}


def _looks_tabular(p: Path) -> bool:
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        lines = [f.readline() for _ in range(5)]
    seps = [max(line.count(c) for c in ";,\t|") for line in lines if line.strip()]
    return len(seps) >= 2 and min(seps) >= 1 and len(set(seps)) == 1


# ------------------------------------------------------------------ description (local LLM)
PROMPT = """You describe a dataset for a research archive so that it can be found later for analyses. Use only what is
given; never invent numbers. Datasets may be confidential – stay factual.

Given by the owner (trust it): {given}

Files and tables (column name: type, examples; rows):
{tables}

First rows of the main table:
{sample}

Answer with one JSON object:
{{"title": "short title if none is given, else repeat it", "summary_en": "3-5 sentences: what the data contains, unit of observation, coverage, how it can be used",
 "summary_de": "the same in German", "keywords": ["8-15 short English keywords"],
 "period": "covered time span, e.g. '2015-2024', or null", "geo": "countries/regions covered, e.g. 'CH' or 'EU-27', or null",
 "unit_of_observation": "one row is …", "columns": {{"column name": "meaning in a few words", "…": "…"}},
 "industry": [], "topic": [], "region": []}}
For industry, topic and region use ONLY these values (zero to three each):
{vocab}"""

_llm = None


def set_llm(llm) -> None:
    global _llm
    _llm = llm


def _chat():
    if _llm is not None:
        return _llm
    from fundus.entities import OllamaChat
    # big datasets on a busy Spark (other sessions on the same model) need long: 30 min by default (1 Nov 2026, Falk)
    return OllamaChat(model=LLM_MODEL, max_tokens=3000, timeout=DESCRIBE_TIMEOUT)


def _vocab(store: Store) -> str:
    out = []
    for facet in ("industry", "topic", "region"):
        vals = [r[0] for r in store.db.execute("SELECT value FROM vocab WHERE facet = ? ORDER BY value", (facet,))]
        out.append(f"{facet}: {', '.join(vals[:250])}")
    return "\n".join(out)


YEAR_COL = re.compile(r"(^|_)(jahr|year|yr|periode|period|datum|date|stichtag|zeit|time|monat|month)($|_)", re.I)


def period_from_data(tabs) -> str | None:
    """Covered period from the data itself – the earliest and latest year of the year or date columns across ALL tables
    (a dataset of 75 quarterly packages spans their union, not the largest package; the model guesses worse)."""
    lo_all, hi_all = None, None
    for t in tabs:
        for c in json.loads(t["profile"] or "{}").get("columns", []):
            typ = str(c.get("type") or "").upper()
            if not (YEAR_COL.search(str(c["name"])) or typ in ("DATE", "TIMESTAMP")):
                continue
            ylo, yhi = re.match(r"(\d{4})", str(c.get("min") or "")), re.match(r"(\d{4})", str(c.get("max") or ""))
            if ylo and yhi and 1800 < int(ylo.group(1)) <= int(yhi.group(1)) < 2200:
                lo_all = min(lo_all or 9999, int(ylo.group(1)))
                hi_all = max(hi_all or 0, int(yhi.group(1)))
    if lo_all is None:
        return None
    return str(lo_all) if lo_all == hi_all else f"{lo_all}-{hi_all}"


def describe_one(store: Store, did: int, llm=None) -> dict:
    d = _row(store, did)
    from fundus import dataset_texts as tx
    if tx.is_text(d):            # a text: metadata, beginning (title, preamble) and headings only – never the whole text
        prompt, text_input = tx.prompt(store, d, _vocab(store))
        return _describe_answer(store, d, [], prompt, llm, {"input": text_input})
    tabs = _tables(store, d)
    lines, sample = [], "(no table)"
    if (d["holding"] or "stored") == "reference":     # raw data stays where it is: README/Markdown and file listing only
        from fundus import dataset_catalog as cat
        listing, readme = cat.reference_inputs(store, d)
        tabs = []
        lines = [f"(a reference dataset: the files below are NOT read; describe it from the listing and the documents)\n{listing}"]
        sample = f"(no data rows are read – README/Markdown files instead)\n{readme}"
    for t in tabs[:6]:
        prof = json.loads(t["profile"] or "{}")
        cols = "; ".join(f"{c['name']}: {c['type']} ({', '.join(c.get('examples', [])[:3])})" for c in prof.get("columns", [])[:60])
        lines.append(f"- {t['name']} / {t['table_name']}: {prof.get('rows')} rows · {cols}")
    if tabs:
        con = _duck(store)
        try:
            cur = con.execute(f"SELECT * FROM read_parquet({_q(file_path(store, tabs[0]['parquet_sha'], 'parquet'))}) LIMIT 5")
            hdr = [x[0] for x in cur.description]
            sample = "\n".join([" | ".join(hdr)] + [" | ".join(str(v)[:40] for v in r) for r in cur.fetchall()])
        finally:
            con.close()
    docs = [f["name"] for f in _files(store, d["id"], d["current_version"], "document")]
    given = json.dumps({k: d[k] for k in ("title", "description", "source_title", "publisher", "published", "project") if d[k]}
                       | ({"documents": docs} if docs else {}), ensure_ascii=False)
    prompt = PROMPT.format(given=given, tables="\n".join(lines) or "(none)", sample=sample, vocab=_vocab(store))
    return _describe_answer(store, d, tabs, prompt, llm)


def _describe_answer(store: Store, d, tabs, prompt: str, llm=None, extra: dict | None = None) -> dict:
    """Ask the local model and store summary, keywords, tags, period, geo and column meanings (failures are recorded)."""
    try:
        ans = (llm or _chat()).complete("You answer with JSON only.", prompt)
        if not isinstance(ans, dict):
            raise ValueError("answer is not a JSON object")
    except Exception as e:
        n = int((re.search(r"\[attempt (\d+)", d["error"] or "") or [0, 0])[1]) + 1 if d["status"] == "failed" else 1
        store.db.execute("UPDATE dataset SET status = 'failed', error = ? WHERE id = ?",
                         (f"{type(e).__name__}: {e}"[:300] + f" [attempt {n} {datetime.now():%Y-%m-%d %H:%M}]", d["id"]))
        store.db.commit()
        raise
    known = store.vocab_values()
    tags = {f: [v for v in (ans.get(f) or []) if isinstance(v, str) and (f, v) in known][:3] for f in ("industry", "topic", "region")}
    own = json.loads(d["tags"] or "{}")
    for f, vals in (own or {}).items():          # own tags win, the model only adds
        tags[f] = list(dict.fromkeys(list(vals) + tags.get(f, [])))
    ann = {"model": getattr(llm or _chat(), "model", "llm"), "at": datetime.now().isoformat(timespec="seconds"),
           "unit_of_observation": ans.get("unit_of_observation"),
           "columns": {str(k)[:120]: str(v)[:200] for k, v in (ans.get("columns") or {}).items()} if isinstance(ans.get("columns"), dict) else {},
           **(extra or {})}
    kw = list(dict.fromkeys(json.loads(d["keywords"] or "[]") + [str(k)[:60] for k in (ans.get("keywords") or []) if isinstance(k, str)]))[:25]
    clean = lambda v, n=1500: " ".join(v.split())[:n] if isinstance(v, str) and v.strip() else None
    store.db.execute(
        "UPDATE dataset SET summary_en = ?, summary_de = ?, keywords = ?, tags = ?, period = coalesce(period, ?), geo = coalesce(geo, ?), "
        "annotation = ?, status = 'described', error = NULL, described_at = ? WHERE id = ?",
        (clean(ans.get("summary_en")), clean(ans.get("summary_de")), json.dumps(kw, ensure_ascii=False), json.dumps(tags),
         period_from_data(tabs) or clean(ans.get("period"), 60), canonical_geo(clean(ans.get("geo"), 120)), json.dumps(ann, ensure_ascii=False),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["id"]))
    store.db.commit()
    return {"id": d["id"], "keywords": len(kw), "tags": tags}


def pending(store: Store, retry_failed: bool = False, retry_timeouts: bool = False) -> list[int]:
    """Datasets to describe: new ones; with retry_failed all failed ones; with retry_timeouts the ones whose description
    timed out (busy model) – again after RETRY_AFTER_H hours, at most RETRY_TIMEOUTS attempts."""
    states = ("new", "failed") if retry_failed else ("new",)
    ids = [r[0] for r in store.db.execute(f"SELECT id FROM dataset WHERE removed_at IS NULL AND status IN "
                                          f"({','.join('?' * len(states))}) ORDER BY id", states)]
    if retry_timeouts and not retry_failed:
        cutoff = (datetime.now() - timedelta(hours=RETRY_AFTER_H)).strftime("%Y-%m-%d %H:%M")
        for did, err in store.db.execute("SELECT id, error FROM dataset WHERE removed_at IS NULL AND status = 'failed' "
                                         "AND lower(error) LIKE '%timed out%' ORDER BY id"):
            m = re.search(r"\[attempt (\d+) ([\d-]+ [\d:]+)\]", err or "")
            n, when = (int(m[1]), m[2]) if m else (1, "")
            if n < RETRY_TIMEOUTS and when <= cutoff:
                ids.append(did)
    return ids


def describe_pending(store: Store, limit: int | None = None, retry_failed: bool = False, llm=None) -> dict:
    done, failed = 0, []
    for did in pending(store, retry_failed)[:limit] if limit else pending(store, retry_failed):
        try:
            describe_one(store, did, llm)
            done += 1
        except Exception as e:
            failed.append({"id": did, "error": f"{type(e).__name__}: {e}"[:200]})
    return {"described": done, "failed": failed}


# ------------------------------------------------------------------ reading
def _row(store: Store, ref):
    s = str(ref).strip()
    m = re.fullmatch(r"(?:dataset:)?(\d+)", s)
    r = store.db.execute("SELECT * FROM dataset WHERE id = ?", (int(m.group(1)),)).fetchone() if m else \
        store.db.execute("SELECT * FROM dataset WHERE slug = ?", (s,)).fetchone()
    if r is None:
        raise LookupError(f"dataset {ref} not found")
    return r


def _files(store: Store, did: int, version: int, role: str | None = None) -> list:
    """Files of a version; tables with the main (largest) table first."""
    return store.db.execute("SELECT * FROM dataset_file WHERE dataset_id = ? AND version = ?" + (" AND role = ?" if role else "")
                            + " ORDER BY coalesce(rows, 0) DESC, id", (did, version, *([role] if role else []))).fetchall()


def _tables(store: Store, d) -> list:
    """The dataset's tables: those of the current version, or with table_scope 'merged' the newest copy of every table name
    across all versions up to the current one (main table first)."""
    if (d["table_scope"] if "table_scope" in d.keys() else "version") != "merged":
        return _files(store, d["id"], d["current_version"], "table")
    rows = store.db.execute("SELECT * FROM dataset_file WHERE dataset_id = ? AND version <= ? AND role = 'table' "
                            "ORDER BY version DESC, id", (d["id"], d["current_version"])).fetchall()
    seen, out = set(), []
    for r in rows:
        key = r["table_name"] or r["name"]
        if key not in seen:
            seen.add(key)
            out.append(r)
    return sorted(out, key=lambda r: (-(r["rows"] or 0), r["id"]))


def _fold(t: str) -> str:
    return unicodedata.normalize("NFKD", (t or "").casefold()).encode("ascii", "ignore").decode()


def _visible(d, include_internal: bool, project: str | None) -> bool:
    return include_internal or d["visibility"] != "internal" or (project and d["project"] == _slug(project))


def _brief(store: Store, d, agg: dict | None = None) -> dict:
    """Short form of a dataset. columns = columns of the main (largest) table; columns_max / columns_total over all tables.
    agg: precomputed counts (dataset_catalog.aggregate + stale, publisher_group) – the catalog passes them in bulk."""
    from fundus import dataset_catalog as cat
    if agg is None:
        agg = cat.aggregate(d, cat.file_rows(store, d["id"]))
        if (d["holding"] or "stored") == "reference":
            loc = cat.location(store, d["id"], manifest=False)
            agg["bytes"] = (loc or {}).get("bytes") or 0
    stale = agg["stale"] if "stale" in agg else stale_inputs(store, d)
    group = agg["publisher_group"] if "publisher_group" in agg else cat.publisher_group(store, d["publisher"])
    life = d["lifecycle"] or "active"
    out = {"id": d["id"], "ref": f"dataset:{d['id']}", "slug": d["slug"], "title": d["title"],
           "summary": d["summary_de"] or d["summary_en"] or d["description"], "summary_en": d["summary_en"],
           "publisher": d["publisher"], "publisher_group": group, "published": d["published"], "period": d["period"], "geo": d["geo"],
           "project": d["project"], "visibility": d["visibility"], "status": d["status"], "version": d["current_version"],
           "lifecycle": life, "holding": d["holding"] or "stored",
           **({"superseded_by": d["superseded_by"], "successor": f"dataset:{d['superseded_by']}"} if life == "superseded" else {}),
           "tables": agg["tables"], "rows": agg["rows"], "columns": agg["columns"], "columns_max": agg["columns_max"],
           "columns_total": agg["columns_total"],
           "bytes": agg["bytes"],   # a workbook once, not per sheet; a reference: the size of its location
           "formats": agg["formats"],
           "keywords": json.loads(d["keywords"] or "[]")[:12], "tags": json.loads(d["tags"] or "{}"),
           "updated_at": d["updated_at"], "frequency": d["frequency"], "next_expected": d["next_expected"],
           "last_checked": d["last_checked"], "update_due": due_reason(d),
           "kind": "text" if d["act_type"] else d["kind"] if d["kind"] != "data" else None,
           **({"identifier": d["identifier"], "jurisdiction": d["jurisdiction"], "act_type": d["act_type"], "issuer": d["issuer"],
               "in_force": _texts().in_force(store, d)} if d["act_type"] else {}),
           "code_systems": sorted(set(code_systems_of(d).values())) or None,
           "crosswalk_pairs": [f"{p_.get('from_system')} → {p_.get('to_system')}" for p_ in _json(d["crosswalk"], {}).get("pairs") or []][:12] or None,
           "stale_inputs": stale or None}
    if d["removed_at"]:
        out["removed_at"] = d["removed_at"]
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _texts():
    from fundus import dataset_texts
    return dataset_texts


def _text(store: Store, d) -> str:
    cols = []
    for t in _tables(store, d):
        cols += [c["name"] for c in json.loads(t["profile"] or "{}").get("columns", [])] + [t["name"], t["table_name"] or ""]
    ann = json.loads(d["annotation"] or "{}")
    tags = json.loads(d["tags"] or "{}")
    return " ".join(str(x) for x in (d["title"], d["description"], d["summary_de"], d["summary_en"], d["source_title"], d["publisher"],
                                     d["project"], d["period"], d["geo"], d["note"], " ".join(json.loads(d["keywords"] or "[]")),
                                     " ".join(v for vals in tags.values() for v in vals), " ".join(cols),
                                     " ".join(f"{k} {v}" for k, v in (ann.get("columns") or {}).items()),
                                     " ".join(code_systems_of(d).values()), d["kind"] if d["kind"] != "data" else "",
                                     d["identifier"], d["jurisdiction"], d["issuer"],
                                     f"{d['act_type']} {_texts().LABELS[d['act_type']]} text" if d["act_type"] else "",
                                     " ".join(r[0] for r in store.db.execute("SELECT version_label FROM dataset_version WHERE "
                                              "dataset_id = ? AND version_label IS NOT NULL", (d["id"],))) if d["act_type"] else "",
                                     " ".join(f"{p_.get('from_system')} {p_.get('to_system')}" for p_ in _json(d["crosswalk"], {}).get("pairs") or []))
                    if x)


def search(store: Store, text: str = "", project: str | None = None, include_internal: bool = False, removed: bool = False,
           limit: int = 50, visible_project: str | None = None, kind: str | None = None, code_system: str | None = None,
           stale: bool = False, lifecycle: str | None = None, jurisdiction: str | None = None, act_type: str | None = None) -> dict:
    """Datasets for a question (words matched against title, description, summaries, keywords, tags, column names and
    meanings, code systems, source, project; all datasets without text), best first. kind: data | crosswalk; code_system:
    only datasets with a column in that system (or crosswalks from/to it); stale: only derived datasets with a newer input.
    kind 'text': texts with versions; jurisdiction (EU, CH …) and act_type narrow to texts.
    lifecycle: withdrawn datasets are left out unless lifecycle='all' (or 'withdrawn'); 'active' | 'superseded' narrow it."""
    where, args = ["removed_at IS NOT NULL" if removed else "removed_at IS NULL"], []
    if lifecycle in (None, ""):
        where.append("lifecycle != 'withdrawn'")
    elif lifecycle != "all":
        if lifecycle not in ("active", "superseded", "withdrawn"):
            raise ValueError("lifecycle: active | superseded | withdrawn | all")
        where.append("lifecycle = ?")
        args.append(lifecycle)
    if kind == "text":
        where.append("act_type IS NOT NULL")
    elif kind:
        if kind not in KINDS:
            raise ValueError(f"kind: {' | '.join(KINDS)} | text")
        where.append("kind = ? AND act_type IS NULL")
        args.append(kind)
    if jurisdiction:
        where.append("upper(jurisdiction) = upper(?)")
        args.append(jurisdiction.strip())
    if act_type:
        where.append("act_type = ?")
        args.append(act_type)
    if project:
        where.append("project = ?")
        args.append(_slug(project))
    rows = [d for d in store.db.execute(f"SELECT * FROM dataset WHERE {' AND '.join(where)} ORDER BY updated_at DESC", args)
            if _visible(d, include_internal, visible_project or project)]
    if code_system:
        cs = norm_system(code_system)
        rows = [d for d in rows if cs in {norm_system(v) for v in code_systems_of(d).values()} |
                {norm_system(p_.get(k)) for p_ in _json(d["crosswalk"], {}).get("pairs") or [] for k in ("from_system", "to_system")}]
    if stale:
        rows = [d for d in rows if stale_inputs(store, d)]
    toks = [t for t in re.findall(r"\w+", _fold(text)) if len(t) > 1]
    if toks:
        scored = []
        for d in rows:
            body = " " + " ".join(re.findall(r"\w+", _fold(_text(store, d)))) + " "
            title = " " + " ".join(re.findall(r"\w+", _fold(d["title"]))) + " "
            hit = sum(1 for t in toks if f" {t}" in body)
            if hit:
                scored.append((hit / len(toks) + 0.3 * sum(1 for t in toks if f" {t}" in title) / len(toks), d))
        rows = [d for s, d in sorted(scored, key=lambda x: -x[0])]
    return {"total": len(rows), "datasets": [_brief(store, d) for d in rows[:limit]]}


def get(store: Store, ref, include_internal: bool = True, project: str | None = None) -> dict:
    from fundus import dataset_catalog as cat
    d = _row(store, ref)
    if not _visible(d, include_internal, project):
        raise PermissionError(f"dataset {ref} is internal; pass include_internal=true or its project")
    out = {}
    life = d["lifecycle"] or "active"
    if life == "superseded":
        s = store.db.execute("SELECT title FROM dataset WHERE id = ?", (d["superseded_by"],)).fetchone()
        out["notice"] = (f"ERSETZT: dieser Datensatz ist ersetzt durch dataset:{d['superseded_by']}"
                         f"{' (' + s[0] + ')' if s else ''} – für neue Analysen den Nachfolger verwenden.")
    elif life == "withdrawn":
        out["notice"] = ("ZURÜCKGEZOGEN: dieser Datensatz ist zurückgezogen und soll nicht mehr verwendet werden"
                         + (f" ({d['lifecycle_note']})" if d["lifecycle_note"] else "") + ".")
    if (d["holding"] or "stored") == "reference":
        loc = cat.location(store, d["id"], manifest=False) or {}
        out["notice"] = ((out["notice"] + " ") if out.get("notice") else "") + \
            f"REFERENZ: die Daten liegen unter {loc.get('path')}, nicht in Fundus gespeichert (keine Vorschau, keine Abfrage)."
    out.update(_brief(store, d))
    ann = json.loads(d["annotation"] or "{}")
    meanings = ann.get("columns") or {}
    out.update({k: d[k] for k in ("description", "summary_de", "source_title", "url", "license", "note", "created_at", "created_by",
                                  "error", "described_at", "update_hint") if d[k]})
    out["tracker_series"] = linked_series(store, d["id"])
    out["used_on_slides"] = [{"id": r["id"], "ref": f"{r['project']}/{r['slug']}", "title": r["title"]} for r in store.db.execute(
        "SELECT e.id, e.project, e.slug, e.title FROM exhibit_link l JOIN exhibit e ON e.id = l.exhibit_id "
        "WHERE l.target_kind = 'dataset' AND l.target_id = ? AND e.project != '_trash' ORDER BY e.updated_at DESC LIMIT 50", (str(d["id"]),))]
    if ann.get("unit_of_observation"):
        out["unit_of_observation"] = ann["unit_of_observation"]
    cs = code_systems_of(d)
    if d["kind"] == "crosswalk":
        out["crosswalk"] = _json(d["crosswalk"], {})
    if d["table_scope"] == "merged":
        out["table_scope"] = "merged: the newest copy of every table across versions"
    ins = inputs_of(store, d)
    if ins:
        out["inputs"] = ins
        if any(x.get("stale") for x in ins):
            out["stale"] = "an input has a newer version – rebuild this dataset (then add it as a new version with inputs)"
    ub = used_by(store, d["id"])
    if ub:
        out["used_by"] = ub
    out["tables_detail"] = []
    for t in _tables(store, d):
        prof = json.loads(t["profile"] or "{}")
        for c in prof.get("columns", []):
            if c["name"] in meanings:
                c["meaning"] = meanings[c["name"]]
            sysname = _system_for(cs, t["table_name"] or t["name"], c["name"])
            if sysname:
                c["code_system"] = sysname
        out["tables_detail"].append({"id": t["id"], "file": t["name"], "table": t["table_name"], "rows": t["rows"], "format": t["ext"],
                                     **({"version": t["version"]} if t["version"] != d["current_version"] else {}), **({"sampled": prof["sampled"]} if prof.get("sampled") else {}),
                                     "bytes": t["bytes"], "path": str(file_path(store, t["parquet_sha"], "parquet")),
                                     "verified_at": t["verified_at"], "verified_ok": t["verified_ok"],
                                     "original_path": str(file_path(store, t["sha"], t["ext"])),
                                     "download_url": url(t["sha"], t["ext"], t["name"]), "columns": prof.get("columns", [])})
    out["documents"] = [{"id": x["id"], "file": x["name"], "format": x["ext"], "bytes": x["bytes"],
                         "verified_at": x["verified_at"], "verified_ok": x["verified_ok"],
                         "path": str(file_path(store, x["sha"], x["ext"])), "download_url": url(x["sha"], x["ext"], x["name"])}
                        for x in _files(store, d["id"], d["current_version"], "document")]
    out["versions"] = _texts().versions(store, d)      # a text: validity per version, the one in force, files per version
    cat.detail_extras(store, d, out)
    from fundus import research
    out["research"] = research.research_for_dataset(store, d["id"], include_internal=include_internal, project=project)
    if d["act_type"]:
        out["text"] = {"act_type_label": _texts().LABELS[d["act_type"]],
                       "how_to_use": "A text with versions: versions[] has valid_from/valid_to and the files of each version; "
                                     "fundus_dataset(ref, action='at', at='YYYY-MM-DD') names the version valid at a date. "
                                     "The files are stored as delivered – parse or compare them only in a concrete analysis "
                                     "(Spark script, result as its own derived dataset)."}
    out["how_to_use"] = ("Load a table directly: duckdb.sql(\"SELECT * FROM read_parquet('<path>')\") or "
                         "pandas.read_parquet('<path>'); or fundus_dataset(ref, action='query', sql=…) with the table names as views.")
    return out


def _refuse_reference(store: Store, d) -> None:
    if (d["holding"] or "stored") == "reference":
        r = store.db.execute("SELECT path FROM dataset_location WHERE dataset_id = ? ORDER BY version DESC LIMIT 1", (d["id"],)).fetchone()
        raise ValueError(f"dataset:{d['id']} liegt als Referenz unter {r[0] if r else '?'}, nicht in Fundus gespeichert – "
                         "keine Vorschau und keine Abfrage; die Dateien dort direkt lesen")


def _table(store: Store, d, table) -> "sqlite3.Row":
    _refuse_reference(store, d)
    tabs = _tables(store, d)
    if not tabs:
        raise LookupError("this dataset has no table")
    if table in (None, ""):
        return tabs[0]
    for t in tabs:
        if str(t["id"]) == str(table) or t["table_name"] == table or t["name"] == table:
            return t
    raise LookupError(f"no table '{table}' (tables: {', '.join(t['table_name'] for t in tabs)})")


def preview(store: Store, ref, table=None, offset: int = 0, limit: int = 100, order: str | None = None) -> dict:
    d = _row(store, ref)
    t = _table(store, d, table)
    limit = max(1, min(int(limit), PREVIEW_MAX))
    con = _duck(store)
    try:
        src = f"read_parquet({_q(file_path(store, t['parquet_sha'], 'parquet'))})"
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
        ob = ""
        if order:
            name, _, direction = order.partition(":")
            if name in cols:
                ob = ' ORDER BY "' + name.replace('"', '""') + '"' + (" DESC" if direction == "desc" else "") + " NULLS LAST"
        cur = con.execute(f"SELECT * FROM {src}{ob} LIMIT {limit} OFFSET {max(0, int(offset))}")
        rows = [[_jsonable(v) for v in r] for r in cur.fetchall()]
    finally:
        con.close()
    return {"dataset": d["id"], "table": t["table_name"], "table_id": t["id"], "total": t["rows"], "offset": offset,
            "columns": cols, "rows": rows}


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


_FORBIDDEN = re.compile(r"\b(copy|attach|detach|install|load|export|import|pragma|create|insert|update|delete|drop|alter|"
                        r"set|call|checkpoint|vacuum|use)\b|read_\w+|\bglob\s*\(|parquet_scan|sniff_csv|\w+_scan\s*\(", re.I)


def _view_name(name: str, taken: set) -> str:
    v = re.sub(r"\W+", "_", _fold(name)).strip("_").lower() or "t"
    if v[0].isdigit():
        v = "t_" + v
    base, n = v, 1
    while v in taken:
        n += 1
        v = f"{base}_{n}"
    taken.add(v)
    return v


def query(store: Store, ref, sql: str, limit: int = 500) -> dict:
    """Read-only SQL over the dataset's tables (DuckDB). Each table is a view named after it (see `views`); the first
    (main = largest) table is also `t`. Only SELECT/WITH; no file access, no writes; at most 2000 rows back, 60 s."""
    d = _row(store, ref)
    _refuse_reference(store, d)
    sql = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", sql) or ";" in sql or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)):
        raise ValueError("only one SELECT/WITH query over the dataset's tables (no file functions, no writes)")
    con = _duck(store)
    taken: set = set()
    views = {}
    try:
        for i, t in enumerate(_tables(store, d)):
            v = _view_name(t["table_name"] or t["name"], taken)
            con.execute(f"CREATE VIEW {v} AS SELECT * FROM read_parquet({_q(file_path(store, t['parquet_sha'], 'parquet'))})")
            views[v] = t["table_name"]
            if i == 0 and "t" not in taken:
                con.execute(f"CREATE VIEW t AS SELECT * FROM {v}")
                taken.add("t")
        # only the dataset's own Parquet files stay readable; every other file access (read_csv('/etc/…'), COPY …) is refused
        allowed = ", ".join(_q(file_path(store, t["parquet_sha"], "parquet")) for t in _tables(store, d))
        con.execute(f"SET allowed_paths = [{allowed}]")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        timer = threading.Timer(QUERY_TIMEOUT, con.interrupt)
        timer.start()
        try:
            cur = con.execute(sql)
            cols = [c[0] for c in cur.description]
            n = max(1, min(int(limit), QUERY_MAX_ROWS))
            rows = cur.fetchmany(n + 1)
        finally:
            timer.cancel()
    except Exception as e:
        if "interrupt" in str(e).lower():
            raise ValueError(f"query took longer than {QUERY_TIMEOUT} s") from None
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"SQL: {e}"[:500]) from None
    finally:
        con.close()
    more = len(rows) > n
    return {"dataset": d["id"], "views": views, "columns": cols, "rows": [[_jsonable(v) for v in r] for r in rows[:n]],
            "truncated": more}


# ------------------------------------------------------------------ editing
def update(store: Store, ref, changes: dict, actor: str = "cli") -> dict:
    d = _row(store, ref)
    bad = set(changes) - EDITABLE
    if bad:
        raise ValueError(f"cannot change {sorted(bad)} (editable: {sorted(EDITABLE)})")
    changes = dict(changes)
    inputs = changes.pop("inputs", None)
    confirm_public = bool(changes.pop("confirm_public", False))
    linked = {k: changes.pop(k) for k in ("kind", "crosswalk", "code_systems", "table_scope") if k in changes}
    life = {k: changes.pop(k) for k in ("lifecycle", "superseded_by", "lifecycle_note") if k in changes}
    warnings = []
    if changes.get("visibility") == "public" and d["visibility"] != "public":
        internal = internal_inputs(store, d["id"])
        if internal and not confirm_public:
            raise ValueError(f"dataset:{d['id']} is built from internal data ({', '.join(internal)}); publishing it could leak "
                             "them – check that nothing internal is in it, then pass confirm_public=true")
        if internal:
            warnings.append(f"published although built from internal {', '.join(internal)} (confirmed)")
    ch = {}
    for k, v in changes.items():
        if k in ("keywords",):
            v = json.dumps([str(x).strip() for x in (v or []) if str(x).strip()], ensure_ascii=False)
        elif k == "tags":
            v = json.dumps({f: list(vals) for f, vals in (v or {}).items() if f in ("industry", "topic", "region")})
        elif k == "visibility" and v not in ("public", "internal"):
            raise ValueError("visibility: public | internal")
        elif k == "act_type" and v not in (None, "") and v not in _texts().ACT_TYPES:
            raise ValueError(f"act_type: {' | '.join(_texts().ACT_TYPES)}")
        elif k == "frequency" and v not in (None, "") and v not in FREQUENCIES:
            raise ValueError(f"frequency: {' | '.join(FREQUENCIES)}")
        elif k in ("next_expected", "last_checked", "retrieved_at") and v not in (None, "") and not ISO_MONTH.match(str(v).strip()):
            raise ValueError(f"{k}: ISO month or date, e.g. 2027-09")
        elif k == "project":
            v = _slug(v) if v else None
        elif isinstance(v, str):
            v = v.strip() or None
        ch[k] = v
    if life:
        from fundus import dataset_catalog as cat
        ch.update(cat.check_lifecycle(store, d, life))
    ch = {k: v for k, v in ch.items() if d[k] != v}
    with store.batch(actor):
        if ch:
            store.db.execute(f"UPDATE dataset SET {', '.join(f'{k} = ?' for k in ch)}, updated_at = ? WHERE id = ?",
                             (*ch.values(), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["id"]))
            store.log("dataset", d["id"], "update", {k: {"old": d[k], "new": v} for k, v in ch.items()})
        if linked:
            warnings += _update_linked(store, d["id"], linked)
        if inputs is not None:
            _set_inputs(store, _row(store, d["id"]), inputs)
            internal = internal_inputs(store, d["id"])
            cur = _row(store, d["id"])
            if internal and cur["visibility"] == "public" and not confirm_public:
                store.db.execute("UPDATE dataset SET visibility = 'internal' WHERE id = ?", (d["id"],))
                store.log("dataset", d["id"], "update", {"visibility": {"old": "public", "new": "internal"},
                                                         "because": f"inputs {', '.join(internal)} are internal"})
                warnings.append(f"made internal: built from internal {', '.join(internal)}")
        moved = propagate_visibility(store, d["id"], actor)
        if moved:
            warnings.append(f"made internal because built from this dataset: {', '.join(moved)}")
    out = get(store, d["id"])
    if warnings:
        out["warnings"] = warnings
    return out


def _update_linked(store: Store, did: int, linked: dict) -> list[str]:
    """kind, table_scope, code_systems (merged; a column set to null is removed) and crosswalk metadata, in that order."""
    d = _row(store, did)
    ch, warn = {}, []
    if "table_scope" in linked:
        if linked["table_scope"] not in TABLE_SCOPES:
            raise ValueError(f"table_scope: {' | '.join(TABLE_SCOPES)}")
        ch["table_scope"] = linked["table_scope"]
    kind = linked.get("kind", d["kind"])
    if kind not in KINDS:
        raise ValueError(f"kind: {' | '.join(KINDS)}")
    if "crosswalk" in linked and linked["crosswalk"]:
        kind = "crosswalk"
    ch["kind"] = kind
    for k, v in ch.items():
        store.db.execute(f"UPDATE dataset SET {k} = ? WHERE id = ?", (v, did))
    d2 = _row(store, did)
    if "code_systems" in linked:
        cur = code_systems_of(d2)
        new = linked["code_systems"] or {}
        if not isinstance(new, dict):
            raise ValueError('code_systems: {"column": "NAICS-2022"}')
        merged = {**cur, **new} if new else {}
        merged = {k: v for k, v in merged.items() if v not in (None, "")}
        ch["code_systems"] = _check_code_systems(store, d2, merged)
        store.db.execute("UPDATE dataset SET code_systems = ? WHERE id = ?", (ch["code_systems"], did))
    if kind == "crosswalk":
        meta = linked.get("crosswalk")
        if meta is None:
            meta = {k: v for k, v in _json(d2["crosswalk"], {}).items() if k != "pairs"}
        if not meta:
            raise ValueError("kind 'crosswalk' needs crosswalk metadata: {from_system, to_system, …} or from_system/to_system columns "
                             "named in crosswalk.columns")
        ch["crosswalk"] = _check_crosswalk(store, d2, meta)
        store.db.execute("UPDATE dataset SET crosswalk = ? WHERE id = ?", (ch["crosswalk"], did))
    elif "crosswalk" in linked or d2["crosswalk"]:
        ch["crosswalk"] = None
        store.db.execute("UPDATE dataset SET crosswalk = NULL WHERE id = ?", (did,))
    old = {k: d[k] for k in ch}
    diff = {k: {"old": old[k], "new": v} for k, v in ch.items() if old[k] != v}
    if diff:
        store.db.execute("UPDATE dataset SET updated_at = ? WHERE id = ?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), did))
        store.log("dataset", did, "update", diff)
    return warn


def redescribe(store: Store, ref) -> dict:
    d = _row(store, ref)
    store.db.execute("UPDATE dataset SET status = 'new' WHERE id = ?", (d["id"],))
    store.db.commit()
    return {"queued": d["id"]}


def remove(store: Store, ref, restore: bool = False, actor: str = "cli") -> dict:
    """Hide a dataset (purged after 30 days, nightly) or bring it back."""
    d = _row(store, ref)
    with store.batch(actor):
        store.db.execute("UPDATE dataset SET removed_at = ? WHERE id = ?",
                         (None if restore else datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["id"]))
        store.log("dataset", d["id"], "restore" if restore else "remove", None)
    return {"restored" if restore else "removed": d["id"], "days": 30}


def purge_removed(store: Store, days: int = 30, actor: str = "cli") -> dict:
    """Delete datasets removed more than `days` ago, with the files no other dataset uses."""
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    ids = [r[0] for r in store.db.execute("SELECT id FROM dataset WHERE removed_at IS NOT NULL AND removed_at <= ?", (cut,))]
    files = set()
    with store.batch(actor):
        for did in ids:
            for f in store.db.execute("SELECT sha, ext, parquet_sha FROM dataset_file WHERE dataset_id = ?", (did,)):
                files.add((f["sha"], f["ext"]))
                if f["parquet_sha"]:
                    files.add((f["parquet_sha"], "parquet"))
            store.db.execute("DELETE FROM exhibit_link WHERE target_kind = 'dataset' AND target_id = ?", (str(did),))
            store.db.execute("DELETE FROM dataset WHERE id = ?", (did,))   # versions and files cascade
            store.log("dataset", did, "purge", None)
    n = 0
    for sha, ext in files:
        if store.db.execute("SELECT 1 FROM dataset_file WHERE sha = ? OR parquet_sha = ? LIMIT 1", (sha, sha)).fetchone():
            continue
        p = file_path(store, sha, ext)
        if p.exists():
            p.unlink()
            n += 1
    return {"purged": len(ids), "files_deleted": n}


def resolve_file(store: Store, name: str) -> tuple[Path, str]:
    """GET /datasets/<sha>.<ext>: a stored file of a dataset (originals, documents, Parquet copies)."""
    m = re.fullmatch(r"([0-9a-f]{64})\.([a-z0-9]{1,8})", name or "")
    if not m:
        raise ValueError("bad file name")
    sha, ext = m.groups()
    if not store.db.execute("SELECT 1 FROM dataset_file WHERE (sha = ? AND ext = ?) OR (parquet_sha = ? AND ? = 'parquet')",
                            (sha, ext, sha, ext)).fetchone():
        raise LookupError("not found")
    p = file_path(store, sha, ext)
    if not p.is_file():
        raise LookupError("not found")
    ctype = {"csv": "text/csv", "tsv": "text/tab-separated-values", "txt": "text/plain", "json": "application/json",
             "jsonl": "application/x-ndjson", "ndjson": "application/x-ndjson", "parquet": "application/vnd.apache.parquet",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf",
             "md": "text/markdown", "html": "text/html"}.get(ext, "application/octet-stream")
    return p, ctype


def stats(store: Store) -> dict:
    r = store.db.execute("SELECT count(*), sum(status = 'new'), sum(status = 'failed') FROM dataset WHERE removed_at IS NULL").fetchone()
    t = store.db.execute("SELECT coalesce(sum(f.rows), 0), coalesce(sum(f.bytes), 0) FROM dataset_file f JOIN dataset d ON d.id = f.dataset_id "
                         "AND f.version = d.current_version WHERE d.removed_at IS NULL").fetchone()
    return {"datasets": r[0] or 0, "pending": r[1] or 0, "failed": r[2] or 0, "rows": t[0], "bytes": t[1],
            "removed": store.db.execute("SELECT count(*) FROM dataset WHERE removed_at IS NOT NULL").fetchone()[0]}


def metrics(store: Store, days: int = 30) -> dict:
    """Figures for the statistics: stock (datasets, tables, rows, columns, storage), state of the descriptions, visibility,
    formats, projects, the largest and newest datasets, and how sessions use them (tool calls per day)."""
    from datetime import date
    q = store.db.execute
    live = "d.removed_at IS NULL"
    cur = f"FROM dataset_file f JOIN dataset d ON d.id = f.dataset_id AND f.version = d.current_version WHERE {live}"
    n = q(f"SELECT count(*), sum(status = 'described'), sum(status = 'new'), sum(status = 'failed'), sum(visibility = 'internal'), "
          f"sum(current_version > 1) FROM dataset d WHERE {live}").fetchone()
    t = q(f"SELECT sum(role = 'table'), sum(role = 'document'), coalesce(sum(CASE WHEN role = 'table' THEN rows END), 0), "
          f"(SELECT coalesce(sum(b), 0) FROM (SELECT DISTINCT f.sha, f.bytes AS b {cur})) {cur}").fetchone()   # a workbook once, not per sheet
    cols = 0
    for (p,) in q(f"SELECT profile {cur} AND role = 'table'"):
        cols += len(json.loads(p or "{}").get("columns", []))
    stored = 0                                   # everything on disk: originals of all versions plus Parquet copies
    seen = set()
    for sha, ext, psha in q("SELECT DISTINCT sha, ext, parquet_sha FROM dataset_file"):
        for s, e in ((sha, ext), (psha, "parquet")):
            if s and (s, e) not in seen:
                seen.add((s, e))
                p = file_path(store, s, e)
                stored += p.stat().st_size if p.exists() else 0
    fmt_rows = [{"format": r[0], "files": r[1], "bytes": r[2]} for r in q(
        f"SELECT f.ext, count(*), coalesce(sum(f.bytes), 0) {cur} GROUP BY 1 ORDER BY 2 DESC")]
    proj = [{"project": r[0] or "ohne Projekt", "datasets": r[1]} for r in q(
        f"SELECT d.project, count(*) FROM dataset d WHERE {live} GROUP BY 1 ORDER BY 2 DESC")]
    largest = [{"id": r[0], "slug": r[1], "title": r[2], "rows": r[3], "bytes": r[4]} for r in q(
        f"SELECT d.id, d.slug, d.title, coalesce(sum(CASE WHEN f.role = 'table' THEN f.rows END), 0) rws, "
        f"(SELECT coalesce(sum(b), 0) FROM (SELECT DISTINCT g.sha, g.bytes b FROM dataset_file g WHERE g.dataset_id = d.id "
        f"AND g.version = d.current_version)) {cur} GROUP BY d.id ORDER BY rws DESC LIMIT 8")]
    newest = [{"id": r[0], "slug": r[1], "title": r[2], "created_at": r[3], "by": r[4]} for r in q(
        f"SELECT d.id, d.slug, d.title, d.created_at, d.created_by FROM dataset d WHERE {live} ORDER BY d.created_at DESC LIMIT 6")]
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    added = dict(q("SELECT substr(created_at, 1, 10), count(*) FROM dataset_version WHERE created_at >= ? GROUP BY 1", (since,)).fetchall())
    calls: dict[str, dict] = {}
    for day, tool, c in q("SELECT day, tool, calls FROM usage WHERE tool LIKE 'fundus_dataset%' AND day >= ?", (since,)):
        calls.setdefault(day, {})[tool] = c
    dlist = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    return {"datasets": n[0] or 0, "described": n[1] or 0, "pending": n[2] or 0, "failed": n[3] or 0, "internal": n[4] or 0,
            "with_versions": n[5] or 0, "tables": t[0] or 0, "documents": t[1] or 0, "rows": t[2], "columns": cols,
            "bytes_current": t[3], "bytes_stored": stored,
            "removed": q("SELECT count(*) FROM dataset WHERE removed_at IS NOT NULL").fetchone()[0],
            "formats": fmt_rows, "byProject": proj, "largest": largest, "newest": newest,
            "tracked": q(f"SELECT count(*) FROM dataset d WHERE {live} AND (frequency IS NOT NULL OR next_expected IS NOT NULL) "
                         f"AND coalesce(frequency, '') != 'once'").fetchone()[0],
            "updateDue": due(store, include_internal=True)[:12],
            "addedByDay": [{"date": d, "n": added.get(d, 0)} for d in dlist],
            "callsByDay": [{"date": d, "add": calls.get(d, {}).get("fundus_dataset_add", 0),
                            "search": calls.get(d, {}).get("fundus_dataset_search", 0),
                            "use": calls.get(d, {}).get("fundus_dataset", 0)} for d in dlist]}


# ------------------------------------------------------------------ new editions (update reminders)
def due_reason(d, today=None, older_than_days: int = 180) -> str | None:
    """'overdue' (next_expected passed), 'never_checked' / 'stale_check' (a periodic dataset without next_expected), or
    None (not due, one-off, or not tracked – no frequency and no next_expected). Mirrors query.update_targets for series."""
    from datetime import date
    if d["removed_at"] or d["frequency"] == "once":
        return None
    today = today or date.today()
    if d["next_expected"]:                 # due from the start of the expected month (a full date: from that day)
        nxt = d["next_expected"].strip()
        return "overdue" if (nxt <= today.isoformat() if len(nxt) > 7 else nxt <= today.isoformat()[:7]) else None
    if not d["frequency"]:
        return None
    if not d["last_checked"]:
        return "never_checked"
    return "stale_check" if d["last_checked"][:10] < (today - timedelta(days=older_than_days)).isoformat() else None


def linked_series(store: Store, did: int) -> list[str]:
    """Edition tracker series whose update_hint names this dataset ('dataset:9'); a check of the dataset checks them too."""
    pat = re.compile(rf"\bdataset:{did}\b")
    return [r[0] for r in store.db.execute("SELECT id, update_hint FROM series WHERE update_hint LIKE ?", (f"%dataset:{did}%",))
            if pat.search(r[1] or "")]


def due(store: Store, older_than_days: int = 180, include_internal: bool = False, project: str | None = None, today=None) -> list[dict]:
    """Datasets that should be checked for a new edition, overdue first."""
    out = []
    for d in store.db.execute("SELECT * FROM dataset WHERE removed_at IS NULL AND (frequency IS NOT NULL OR next_expected IS NOT NULL)"):
        r = due_reason(d, today, older_than_days)
        if not r or not _visible(d, include_internal, project):
            continue
        out.append({k: v for k, v in {"ref": f"dataset:{d['id']}", "slug": d["slug"], "title": d["title"], "publisher": d["publisher"], "frequency": d["frequency"],
                                      "next_expected": d["next_expected"], "last_checked": d["last_checked"], "version": d["current_version"],
                                      "update_hint": d["update_hint"], "url": d["url"], "project": d["project"], "reason": r,
                                      "tracker_series": linked_series(store, d["id"]) or None}.items() if v not in (None, "")})
    have = {x["ref"] for x in out}
    for d in store.db.execute("SELECT DISTINCT d.* FROM dataset d JOIN dataset_input i ON i.dataset_id = d.id AND i.version = d.current_version "
                              "WHERE d.removed_at IS NULL"):
        st_ = stale_inputs(store, d)
        if st_ and _visible(d, include_internal, project) and f"dataset:{d['id']}" not in have:
            out.append({"ref": f"dataset:{d['id']}", "slug": d["slug"], "title": d["title"], "version": d["current_version"],
                        "project": d["project"], "reason": "inputs_updated", "stale_inputs": st_,
                        **({"update_hint": d["update_hint"]} if d["update_hint"] else {})})
    order = {"overdue": 0, "inputs_updated": 1, "stale_check": 2, "never_checked": 3}
    return sorted(out, key=lambda x: (order[x["reason"]], x.get("next_expected") or ""))


def _advance(month: str, freq: str, today) -> str:
    """The month one or more cadence steps after `month`, first one after today."""
    y, m = int(month[:4]), int(month[5:7])
    step = MONTHS[freq]
    while True:
        m += step
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        if f"{y:04d}-{m:02d}" > today.isoformat()[:7]:
            return f"{y:04d}-{m:02d}"


def mark_checked(store: Store, ref, next_expected: str | None = None, update_hint: str | None = None, note: str | None = None,
                 actor: str = "cli", today=None) -> dict:
    """A dataset was checked for a new edition today (found or not). next_expected: when the next edition is expected; without
    it a passed next_expected moves on by the dataset's cadence (annual: +12 months …). Linked tracker series are checked too."""
    from datetime import date
    today = today or date.today()
    d = _row(store, ref)
    if next_expected and not ISO_MONTH.match(next_expected):
        raise ValueError("next_expected: ISO month, e.g. 2027-09")
    ch = {"last_checked": today.isoformat()}
    if next_expected:
        ch["next_expected"] = next_expected
    elif d["next_expected"] and d["next_expected"][:7] <= today.isoformat()[:7] and d["frequency"] in MONTHS:
        ch["next_expected"] = _advance(d["next_expected"][:7], d["frequency"], today)
    if update_hint:
        ch["update_hint"] = update_hint
    series = linked_series(store, d["id"])
    with store.batch(actor):
        store.db.execute(f"UPDATE dataset SET {', '.join(f'{k} = ?' for k in ch)} WHERE id = ?", (*ch.values(), d["id"]))
        store.log("dataset", d["id"], "check", {**ch, "note": note})
        for sid in series:
            sch = {"last_checked": ch["last_checked"], **({"next_expected": ch["next_expected"]} if "next_expected" in ch else {})}
            store.update("series", sid, sch)
            store.log("series", sid, "check", {**sch, "via": f"dataset:{d['id']}"})
    return {"dataset": f"dataset:{d['id']}", **ch, "tracker_series_checked": series}


# ------------------------------------------------------------------ linked data (1 Nov 2026)
# Crosswalk datasets, code systems of columns, lineage of derived datasets (stale when an input has a newer version),
# visibility propagation (anything built from an internal input is internal) and tables across versions.

def norm_system(name) -> str:
    """'NAICS 2022', 'naics_2022', 'NAICS-2022' → 'naics-2022' (comparison key of a code system)."""
    return re.sub(r"[\s_./]+", "-", _fold(str(name or "")).strip()).strip("-")


def _json(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def code_systems_of(d) -> dict:
    return _json(d["code_systems"] if "code_systems" in d.keys() else None, {})


def _system_for(cs: dict, table: str | None, column: str) -> str | None:
    return cs.get(f"{table}/{column}") or cs.get(column)


def _check_code_systems(store: Store, d, value) -> str | None:
    """{"table/column" | "column": "NAICS-2022"} – every key must name a column of the dataset's tables."""
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ValueError('code_systems: {"column": "NAICS-2022"} or {"table/column": "NACE-2"}')
    cols = {}
    for t in _tables(store, d):
        for c in _json(t["profile"], {}).get("columns", []):
            cols.setdefault(c["name"], set()).add(t["table_name"] or t["name"])
    out = {}
    for k, v in value.items():
        k = str(k).strip()
        table, _, col = k.rpartition("/")
        if col not in cols or (table and table not in cols[col]):
            raise ValueError(f"code_systems: no column '{k}' (columns: {', '.join(sorted(cols)[:40])})")
        if v not in (None, ""):
            out[k] = str(v).strip()
    return json.dumps(out, ensure_ascii=False) if out else None


def _xwalk_pairs(store: Store, d, meta: dict) -> list[dict]:
    """System pairs the crosswalk table holds: from columns from_system/to_system (rows per pair), else the metadata pair."""
    roles = {**{r: r for r in XWALK_ROLES}, **(meta.get("columns") or {})}
    try:
        t = _table(store, d, meta.get("table"))
    except LookupError:
        return []
    names = {c["name"] for c in _json(t["profile"], {}).get("columns", [])}
    fs, ts = roles.get("from_system"), roles.get("to_system")
    if fs in names and ts in names:
        fv, tv = roles.get("from_version"), roles.get("to_version")
        sel = ", ".join(f'"{c}"' if c in names else "NULL" for c in (fs, fv, ts, tv))
        con = _duck(store)
        try:
            rows = con.execute(f"SELECT {sel}, count(*) FROM read_parquet({_q(file_path(store, t['parquet_sha'], 'parquet'))}) "
                               f"GROUP BY ALL ORDER BY 1, 3").fetchall()
        finally:
            con.close()
        return [{k: v for k, v in zip(("from_system", "from_version", "to_system", "to_version", "rows"), r) if v not in (None, "")}
                for r in rows[:500]]
    if meta.get("from_system") and meta.get("to_system"):
        return [{k: meta[k] for k in ("from_system", "from_version", "to_system", "to_version") if meta.get(k)} | {"rows": t["rows"]}]
    return []


def _check_crosswalk(store: Store, d, value) -> str | None:
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise ValueError("crosswalk: {from_system, to_system, from_version, to_version, valid_from, valid_to, quality, method, "
                         "table, columns: {role: column}}")
    bad = set(value) - set(XWALK_META) - {"pairs"}
    if bad:
        raise ValueError(f"crosswalk: unknown keys {sorted(bad)} (allowed: {', '.join(XWALK_META)})")
    meta = {k: v for k, v in value.items() if k in XWALK_META and v not in (None, "", {})}
    cols = meta.get("columns") or {}
    if set(cols) - set(XWALK_ROLES):
        raise ValueError(f"crosswalk.columns: roles are {', '.join(XWALK_ROLES)}")
    try:
        t = _table(store, d, meta.get("table"))
    except LookupError as e:
        raise ValueError(f"crosswalk: {e}") from None
    names = {c["name"] for c in _json(t["profile"], {}).get("columns", [])}
    roles = {**{r: r for r in XWALK_ROLES}, **cols}
    for need in ("from_code", "to_code"):
        if roles[need] not in names:
            raise ValueError(f"crosswalk: table '{t['table_name']}' has no column for {need} ('{roles[need]}'); map it in columns")
    meta["pairs"] = _xwalk_pairs(store, d, meta)
    if not meta["pairs"]:
        raise ValueError("crosswalk: name from_system and to_system, or give the table from_system/to_system columns")
    return json.dumps(meta, ensure_ascii=False, default=str)


def crosswalks(store: Store, system_from: str | None = None, system_to: str | None = None, include_internal: bool = False,
               project: str | None = None) -> list[dict]:
    """Crosswalk datasets (and their system pairs), optionally only those from/to the given systems (either direction)."""
    a, b = norm_system(system_from), norm_system(system_to)
    out = []
    for d in store.db.execute("SELECT * FROM dataset WHERE kind = 'crosswalk' AND removed_at IS NULL ORDER BY id"):
        if not _visible(d, include_internal, project):
            continue
        meta = _json(d["crosswalk"], {})
        pairs = []
        for pr in meta.get("pairs") or []:
            f, t = norm_system(pr.get("from_system")), norm_system(pr.get("to_system"))
            fwd = (not a or a == f) and (not b or b == t)
            rev = bool(a or b) and (not a or a == t) and (not b or b == f)
            if fwd or rev:
                pairs.append({**pr, **({"direction": "reverse"} if rev and not fwd else {})})
        if pairs:
            out.append({"ref": f"dataset:{d['id']}", "title": d["title"], "version": d["current_version"],
                        "visibility": d["visibility"], "pairs": pairs,
                        **{k: meta[k] for k in ("valid_from", "valid_to", "quality", "method", "note") if meta.get(k)}})
    return out


def crosswalk_lookup(store: Store, system_from: str, system_to: str, codes=None, at: str | None = None, limit: int = 500,
                     include_internal: bool = False, project: str | None = None) -> dict:
    """Map codes of `system_from` to `system_to` through the crosswalk datasets. codes: one code, a list, or None (the
    whole mapping up to `limit` rows). at: an ISO date – only rows valid then (valid_from/valid_to columns). A crosswalk
    that only holds the other direction is read backwards (marked 'reverse'; weights then do not apply as they are)."""
    if not system_from or not system_to:
        raise ValueError("system_from and system_to are needed (fundus_crosswalk() without them lists the crosswalks)")
    found = crosswalks(store, system_from, system_to, include_internal, project)
    codes = [str(codes)] if isinstance(codes, (str, int)) else [str(c) for c in (codes or [])]
    a, b = norm_system(system_from), norm_system(system_to)
    results, used = [], []
    for x in found:
        d = _row(store, x["ref"])
        meta = _json(d["crosswalk"], {})
        roles = {**{r: r for r in XWALK_ROLES}, **(meta.get("columns") or {})}
        t = _table(store, d, meta.get("table"))
        names = {c["name"] for c in _json(t["profile"], {}).get("columns", [])}
        for pr in x["pairs"]:
            rev = pr.get("direction") == "reverse"
            src, dst = ("to", "from") if rev else ("from", "to")
            where, args = [], []
            if roles["from_system"] in names and roles["to_system"] in names:
                where.append(f'"{roles["from_system"]}" = ? AND "{roles["to_system"]}" = ?')
                args += [pr.get("from_system"), pr.get("to_system")]
            if codes:
                where.append(f'CAST("{roles[src + "_code"]}" AS VARCHAR) IN ({", ".join("?" * len(codes))})')
                args += codes
            if at:
                if roles["valid_from"] in names:
                    where.append(f'(CAST("{roles["valid_from"]}" AS VARCHAR) IS NULL OR CAST("{roles["valid_from"]}" AS VARCHAR) <= ?)')
                    args.append(at)
                if roles["valid_to"] in names:
                    where.append(f'(CAST("{roles["valid_to"]}" AS VARCHAR) IS NULL OR CAST("{roles["valid_to"]}" AS VARCHAR) >= ?)')
                    args.append(at)
            cols = [(role, roles[role]) for role in XWALK_ROLES if roles[role] in names]
            sel = ", ".join(f'"{c}" AS "{r}"' for r, c in cols)
            con = _duck(store)
            try:
                cur = con.execute(f"SELECT {sel} FROM read_parquet({_q(file_path(store, t['parquet_sha'], 'parquet'))})"
                                  + (f" WHERE {' AND '.join(where)}" if where else "") + f" LIMIT {int(limit) + 1}", args)
                rows = [dict(zip([r for r, _ in cols], [_jsonable(v) for v in row])) for row in cur.fetchall()]
            finally:
                con.close()
            for r in rows[:limit]:
                m = {"from_code": r.get(f"{src}_code"), "from_label": r.get(f"{src}_label"),
                     "to_code": r.get(f"{dst}_code"), "to_label": r.get(f"{dst}_label")}
                m.update({k: r[k] for k in ("weight", "relation", "quality", "method", "valid_from", "valid_to") if r.get(k) not in (None, "")})
                if rev:
                    m["direction"] = "reverse"
                m["crosswalk"] = x["ref"]
                results.append({k: v for k, v in m.items() if v not in (None, "")})
            used.append({"crosswalk": x["ref"], "title": x["title"], "version": x["version"], **{k: v for k, v in pr.items() if k != "rows"}})
    missing = [c for c in codes if c not in {str(r.get("from_code")) for r in results}]
    return {"system_from": system_from, "system_to": system_to, "crosswalks": used, "mappings": results[:limit],
            "truncated": len(results) > limit, **({"not_found": missing} if missing else {}),
            **({} if found else {"hint": f"no crosswalk {a} → {b}; fundus_crosswalk() lists the ones there are"})}


def code_system_registry(store: Store, include_internal: bool = False, project: str | None = None) -> list[dict]:
    """Every code system Fundus knows: on dataset columns (code_systems) and in crosswalks – with where it is used."""
    reg: dict[str, dict] = {}

    def entry(name):
        k = norm_system(name)
        return reg.setdefault(k, {"system": name, "columns": [], "crosswalks": []})
    for d in store.db.execute("SELECT * FROM dataset WHERE removed_at IS NULL ORDER BY id"):
        if not _visible(d, include_internal, project):
            continue
        for k, sysname in code_systems_of(d).items():
            entry(sysname)["columns"].append(f"dataset:{d['id']} {k}")
        for pr in _json(d["crosswalk"], {}).get("pairs") or []:
            for side, other in (("from_system", "to_system"), ("to_system", "from_system")):
                if pr.get(side):
                    entry(pr[side])["crosswalks"].append(f"dataset:{d['id']} ↔ {pr.get(other)}")
    return sorted(({**v, "crosswalks": sorted(set(v["crosswalks"]))} for v in reg.values()), key=lambda r: r["system"].lower())


# lineage ----------------------------------------------------------------------------------------------------------
def _input_refs(inputs) -> list[dict]:
    out = []
    for x in inputs or []:
        if isinstance(x, dict):
            ref, ver, note = x.get("ref") or x.get("dataset"), x.get("version"), x.get("note")
        else:
            ref, ver, note = x, None, None
        if ref in (None, ""):
            continue
        out.append({"ref": str(ref), "version": int(ver) if ver not in (None, "") else None, "note": note})
    return out


def inputs_of(store: Store, d, version: int | None = None) -> list[dict]:
    """Inputs of a dataset version with the version used and whether a newer one exists (stale)."""
    v = version or d["current_version"]
    out = []
    for r in store.db.execute("SELECT i.*, x.title, x.current_version AS cur, x.visibility, x.removed_at FROM dataset_input i "
                              "JOIN dataset x ON x.id = i.input_id WHERE i.dataset_id = ? AND i.version = ? ORDER BY i.input_id",
                              (d["id"], v)):
        item = {"ref": f"dataset:{r['input_id']}", "title": r["title"], "version": r["input_version"], "current_version": r["cur"],
                "visibility": r["visibility"], **({"note": r["note"]} if r["note"] else {})}
        if r["cur"] > r["input_version"]:
            item["stale"] = True
        if r["removed_at"]:
            item["removed"] = True
        out.append(item)
    return out


def used_by(store: Store, did: int) -> list[dict]:
    return [{"ref": f"dataset:{r['dataset_id']}", "title": r["title"], "version": r["version"], "input_version": r["input_version"]}
            for r in store.db.execute("SELECT i.dataset_id, i.version, i.input_version, d.title FROM dataset_input i "
                                      "JOIN dataset d ON d.id = i.dataset_id WHERE i.input_id = ? AND i.version = d.current_version "
                                      "AND d.removed_at IS NULL ORDER BY i.dataset_id", (did,))]


def stale_inputs(store: Store, d) -> list[str]:
    return [x["ref"] for x in inputs_of(store, d) if x.get("stale")]


def _set_inputs(store: Store, d, inputs, version: int | None = None) -> list[str]:
    """Record the inputs of a dataset version (replaces what was recorded). Refuses cycles. Returns warnings."""
    v = version or d["current_version"]
    rows, warn = [], []
    for x in _input_refs(inputs):
        src = _row(store, x["ref"])
        if src["id"] == d["id"]:
            raise ValueError("a dataset cannot be its own input")
        if d["id"] in _ancestors(store, src["id"]):
            raise ValueError(f"{x['ref']} is itself built from dataset:{d['id']} (cycle)")
        ver = x["version"] or src["current_version"]
        if not 1 <= ver <= src["current_version"]:
            raise ValueError(f"{x['ref']} has no version {ver}")
        rows.append((d["id"], v, src["id"], ver, x["note"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    store.db.execute("DELETE FROM dataset_input WHERE dataset_id = ? AND version = ?", (d["id"], v))
    store.db.executemany("INSERT INTO dataset_input(dataset_id, version, input_id, input_version, note, created_at) VALUES (?,?,?,?,?,?)", rows)
    store.log("dataset", d["id"], "lineage", {"version": v, "inputs": [f"dataset:{r[2]} v{r[3]}" for r in rows]})
    return warn


def _ancestors(store: Store, did: int) -> set[int]:
    seen, todo = set(), [did]
    while todo:
        cur = todo.pop()
        for (i,) in store.db.execute("SELECT i.input_id FROM dataset_input i JOIN dataset d ON d.id = i.dataset_id "
                                     "WHERE i.dataset_id = ? AND i.version = d.current_version", (cur,)):
            if i not in seen:
                seen.add(i)
                todo.append(i)
    return seen


def _descendants(store: Store, did: int) -> list[int]:
    seen, todo, order = set(), [did], []
    while todo:
        cur = todo.pop(0)
        for (i,) in store.db.execute("SELECT i.dataset_id FROM dataset_input i JOIN dataset d ON d.id = i.dataset_id "
                                     "WHERE i.input_id = ? AND i.version = d.current_version", (cur,)):
            if i not in seen:
                seen.add(i)
                order.append(i)
                todo.append(i)
    return order


def internal_inputs(store: Store, did: int) -> list[str]:
    return [f"dataset:{i}" for i in sorted(_ancestors(store, did))
            if store.db.execute("SELECT visibility FROM dataset WHERE id = ?", (i,)).fetchone()[0] == "internal"]


def propagate_visibility(store: Store, did: int, actor: str = "cli") -> list[str]:
    """A dataset built (directly or indirectly) from an internal one is internal too: makes public descendants internal."""
    changed = []
    if store.db.execute("SELECT visibility FROM dataset WHERE id = ?", (did,)).fetchone()[0] != "internal":
        return changed
    for i in _descendants(store, did):
        r = store.db.execute("SELECT visibility FROM dataset WHERE id = ?", (i,)).fetchone()
        if r and r[0] == "public":
            store.db.execute("UPDATE dataset SET visibility = 'internal', updated_at = ? WHERE id = ?",
                             (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), i))
            store.log("dataset", i, "update", {"visibility": {"old": "public", "new": "internal"}, "because": f"input dataset:{did} is internal"})
            changed.append(f"dataset:{i}")
    return changed
