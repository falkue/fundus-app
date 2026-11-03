"""Embeddings for semantic search (the optional local model – Ollama or an OpenAI-compatible server, configured in
fundus.config: llm_url, llm_api, llm_model_embed, default qwen3-embedding:8b), stored for millions of items.

Each item (observation, forecast, study, quote, practice, fact, series, exhibit = slide/chart) gets a short kind-specific text
(`item_text`) that is embedded once; a hash of that text (+ visibility) means only new or changed items are
embedded again (`embed_pending`). Vectors are truncated to `dim` dimensions (Matryoshka) and L2-normalised.

Storage – an embedding *space* is model + dim + quant ('qwen3-embedding:8b|1024|int8'):
  * `embedding` (one row per item and space): the rescoring vector, quantised as float32 | float16 | int8.
  * a sqlite-vec vec0 table per space (rowid = embedding.id) with a binary-quantised copy (bit[dim], 1/32 of
    float32) plus metadata `kind` and `internal`, for the coarse KNN stage (hamming distance, brute force in C).
  Search: coarse top-k on bits (or exact scoring when filters leave few candidates), then exact cosine on the
  rescoring vectors. Both tables live in the same SQLite file and are written in the same transaction.
  Several spaces can be computed side by side; meta 'embedding.active' (set_active) switches search atomically.

Queries are embedded with the Qwen3 instruction prefix (`QUERY_INSTRUCT`), documents without it.

Optional model limit: with FUNDUS_MAX_MODELS=N (N > 0) a batch first looks at /api/ps and stops (resumable) instead of
loading our model next to N others. Default 0 = no limit (the Spark has no limit since 27 Oct 2026).

    python -m fundus.cli embed [--all] [--limit N] [--status] [--activate] [--rebuild-index] [--model M --dim D --quant Q]
"""
from __future__ import annotations

try:                       # POSIX; Windows has msvcrt.locking instead (see _lock)
    import fcntl
except ImportError:        # pragma: no cover - Windows
    fcntl = None
import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from fundus import llm
from fundus.store import ITEM_KINDS, Store

# endpoint and model come from fundus.config (llm_url, llm_model_embed, llm_embed_dim); these names stay for callers
_LLM = llm.settings()
OLLAMA_URL = _LLM["url"]
MODEL = _LLM["model_embed"]
DIM = _LLM["embed_dim"]
QUANT = os.environ.get("FUNDUS_EMBED_QUANT", "int8")
QUANTS = ("float32", "float16", "int8")
KEEP_ALIVE = "30m"
MAX_LOADED_MODELS = int(os.environ.get("FUNDUS_MAX_MODELS", "0"))   # 0 = no limit
BATCH_SIZE = 32          # small batches keep query embeddings (same GPU queue) under the 4 s fallback limit
MAX_CHARS = 2000            # per item text; ~500 tokens
QUERY_INSTRUCT = ("Instruct: Given a search query, retrieve evidence (figures, studies, quotes, facts) that answers it\n"
                  "Query: ")
EMBED_KINDS = ITEM_KINDS + ("series", "exhibit")   # one vector per logical exhibit, from its current text
ACTIVE_KEY = "embedding.active"
SEARCH_LOG_RETENTION_DAYS = 180


# Embedding is not possible right now (model switched off, server down, model capacity rule, timeout) – the same class
# as llm.Unavailable, so callers catching either see every model failure
EmbedUnavailable = llm.Unavailable


# ------------------------------------------------------------------ spaces
@dataclass(frozen=True)
class Space:
    model: str
    dim: int
    quant: str = "int8"

    def __post_init__(self):
        if self.quant not in QUANTS:
            raise ValueError(f"quant must be one of {', '.join(QUANTS)}")
        if self.dim <= 0 or self.dim % 8:
            raise ValueError("dim must be a positive multiple of 8")

    @property
    def key(self) -> str:
        return f"{self.model}|{self.dim}|{self.quant}"

    @property
    def vec_table(self) -> str:
        return "vec_" + re.sub(r"[^a-z0-9]+", "_", f"{self.model}_{self.dim}_{self.quant}".lower()).strip("_")

    @classmethod
    def parse(cls, key: str) -> "Space":
        model, dim, quant = key.rsplit("|", 2)
        return cls(model, int(dim), quant)


def default_space() -> Space:
    """The configured embedding space (llm_model_embed, llm_embed_dim; read now, so a changed config applies)."""
    s = llm.settings()
    return Space(s["model_embed"], s["embed_dim"], QUANT)


def active_space(store: Store) -> Space | None:
    """The space search uses: meta 'embedding.active', else None (no semantic search yet)."""
    r = store.db.execute("SELECT value FROM meta WHERE key = ?", (ACTIVE_KEY,)).fetchone()
    return Space.parse(r[0]) if r and r[0] else None


def set_active(store: Store, space: Space) -> None:
    """Switch search to another space (one UPDATE – atomic for all readers)."""
    if not store.db.execute("SELECT 1 FROM embedding_space WHERE space = ?", (space.key,)).fetchone():
        raise LookupError(f"embedding space {space.key} does not exist – run `fundus embed` for it first")
    store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (ACTIVE_KEY, space.key))
    store.db.commit()


def load_vec(store: Store) -> None:
    """Load the sqlite-vec extension into this connection (needed for every vec0 read/write)."""
    if getattr(store, "_vec_loaded", None) is store.db:
        return
    import sqlite_vec
    store.db.enable_load_extension(True)
    try:
        sqlite_vec.load(store.db)
    finally:
        store.db.enable_load_extension(False)
    store._vec_loaded = store.db


def ensure_space(store: Store, space: Space) -> None:
    load_vec(store)
    store.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {space.vec_table} USING vec0("
                     f"embedding bit[{space.dim}], kind text, internal integer)")
    store.db.execute("INSERT OR IGNORE INTO embedding_space(space, model, dim, quant, vec_table) VALUES (?,?,?,?,?)",
                     (space.key, space.model, space.dim, space.quant, space.vec_table))
    store.db.commit()


def space_count(store: Store, space: Space) -> int:
    return store.db.execute("SELECT count(*) FROM embedding WHERE model = ? AND dim = ? AND quant = ?",
                            (space.model, space.dim, space.quant)).fetchone()[0]


# ------------------------------------------------------------------ vectors
def normalise(vecs, dim: int = DIM) -> np.ndarray:
    """Truncate to `dim` (Matryoshka) and L2-normalise; returns float32 (n, dim)."""
    a = np.asarray(vecs, dtype=np.float32)
    if a.ndim == 1:
        a = a[None, :]
    a = a[:, :dim]
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (a / n).astype(np.float32)


def encode(vecs: np.ndarray, quant: str) -> list[bytes]:
    """Normalised float vectors → storage blobs. int8 uses a per-vector scale (cancels out in cosine)."""
    if quant == "float32":
        a = vecs.astype("<f4")
    elif quant == "float16":
        a = vecs.astype("<f2")
    else:
        m = np.abs(vecs).max(axis=1, keepdims=True)
        m[m == 0] = 1.0
        a = np.clip(np.round(vecs / m * 127), -127, 127).astype(np.int8)
    return [row.tobytes() for row in a]


def decode(blobs: list[bytes], quant: str, dim: int) -> np.ndarray:
    """Storage blobs → L2-normalised float32 matrix (n, dim)."""
    if not blobs:
        return np.zeros((0, dim), dtype=np.float32)
    dt = {"float32": "<f4", "float16": "<f2", "int8": np.int8}[quant]
    return normalise(np.frombuffer(b"".join(blobs), dtype=dt).reshape(len(blobs), dim), dim)


def bits(vecs: np.ndarray) -> list[bytes]:
    """Binary quantisation (sign bits) for the coarse vec0 index."""
    return [row.tobytes() for row in np.packbits(np.asarray(vecs) > 0, axis=1)]


# ------------------------------------------------------------------ embedders
class OllamaEmbedder:
    """Calls Ollama /api/embed (or /v1/embeddings of an OpenAI-compatible server, llm_api = 'openai').
    `embed_documents` for item texts, `embed_query` for search queries."""

    def __init__(self, model: str | None = None, dim: int | None = None, url: str | None = None,
                 keep_alive: str = KEEP_ALIVE):
        s = llm.settings()
        self.model, self.dim = model or s["model_embed"], dim or s["embed_dim"]
        self.url, self.keep_alive, self.api = (url or s["url"]).rstrip("/"), keep_alive, s["api"]

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def raw_embed(self, texts: list[str], timeout: float = 300.0) -> list[list[float]]:
        """Vectors as the model returns them (full dimension, not normalised) – one try, no capacity check."""
        s = llm.require()
        if self.api == "openai":
            return llm.openai_embed(self.model, texts, timeout, {**s, "url": self.url})
        body = {"model": self.model, "input": texts, "keep_alive": self.keep_alive, "truncate": True}
        return self._post("/api/embed", body, timeout).get("embeddings") or []

    def loaded_models(self, timeout: float = 2.0) -> list[str]:
        if self.api != "ollama":
            return []
        try:
            with urllib.request.urlopen(self.url + "/api/ps", timeout=timeout) as r:
                return [m.get("name") or m.get("model") for m in json.loads(r.read()).get("models", [])]
        except (OSError, ValueError) as e:
            raise EmbedUnavailable(f"ollama not reachable: {e}") from e

    def check_capacity(self, timeout: float = 2.0) -> None:
        """Raise EmbedUnavailable if loading the model would exceed FUNDUS_MAX_MODELS (no check without a limit)."""
        llm.require()
        if MAX_LOADED_MODELS <= 0 or self.api != "ollama":
            return
        loaded = self.loaded_models(timeout)
        if not capacity_ok(loaded, self.model):
            raise EmbedUnavailable(f"ollama busy: {', '.join(loaded)} loaded; not loading {self.model} (FUNDUS_MAX_MODELS={MAX_LOADED_MODELS})")

    def _embed(self, texts: list[str], timeout: float, retries: int) -> np.ndarray:
        llm.require()
        body = {"model": self.model, "input": texts, "keep_alive": self.keep_alive, "truncate": True}
        last = None
        for attempt in range(retries + 1):
            try:
                if self.api == "openai":
                    embs = llm.openai_embed(self.model, texts, timeout, {**llm.settings(), "url": self.url})
                    data = {"embeddings": embs}
                else:
                    data = self._post("/api/embed", body, timeout)
                embs = data.get("embeddings")
                if not embs or len(embs) != len(texts):
                    raise EmbedUnavailable(f"unexpected answer from the embedding model: {str(data)[:200]}")
                return normalise(embs, self.dim)
            except (OSError, ValueError, EmbedUnavailable) as e:
                last = e
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        raise EmbedUnavailable(f"embedding failed: {last}") from last

    def embed_documents(self, texts: list[str], timeout: float = 300.0, retries: int = 2) -> np.ndarray:
        return self._embed(list(texts), timeout, retries)

    def embed_query(self, text: str, timeout: float = 4.0) -> np.ndarray:
        self.check_capacity(timeout=min(timeout, 1.5))
        return self._embed([QUERY_INSTRUCT + text], timeout, 0)[0]


def capacity_ok(loaded: list[str], model: str, max_loaded: int = MAX_LOADED_MODELS) -> bool:
    """True if `model` is already loaded, there is room for one more model, or there is no limit (max_loaded <= 0)."""
    if max_loaded <= 0:
        return True
    names = {n for n in loaded if n}
    return model in names or (model + ":latest") in names or len(names) < max_loaded


_override = None
_embedders: dict[tuple[str, int], OllamaEmbedder] = {}


def embedder_for(space: Space):
    """Embedder producing vectors for `space` (the test override if one is set)."""
    if _override is not None:
        return _override
    key = (space.model, space.dim)
    if key not in _embedders:
        _embedders[key] = OllamaEmbedder(space.model, space.dim)
    return _embedders[key]


def set_embedder(e) -> None:
    """Replace the embedder process-wide (tests: a fake without network); None restores Ollama."""
    global _override
    _override = e


# ------------------------------------------------------------------ item texts
def _fmt_num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _join(parts: list[tuple[str, object]]) -> str:
    out = []
    for label, v in parts:
        v = " ".join(str(v).split()) if v not in (None, "") else ""
        if v:
            out.append(f"{label}: {v}" if label else v)
    return "\n".join(out)


class _Context:
    """Bulk lookups (documents, publishers, tag labels) so building many texts stays fast."""

    def __init__(self, store: Store):
        db = store.db
        self.docs = {r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT d.id, d.title, p.name FROM document d LEFT JOIN publisher p ON p.id = d.publisher_id")}
        self.publishers = dict(db.execute("SELECT id, name FROM publisher").fetchall())
        labels = {(f, v): lab for f, v, lab in db.execute("SELECT facet, value, label FROM vocab")}
        self.tags: dict[tuple[str, str], list[str]] = {}
        for kind, iid, facet, value in db.execute("SELECT item_kind, item_id, facet, value FROM tag "
                                                  "WHERE facet != 'project' ORDER BY facet, value"):
            self.tags.setdefault((kind, iid), []).append(labels.get((facet, value), value))

    def source(self, doc_id) -> tuple[str | None, str | None]:
        return self.docs.get(doc_id, (None, None)) if doc_id else (None, None)

    def tag_labels(self, kind: str, iid) -> str:
        return ", ".join(dict.fromkeys(self.tags.get((kind, str(iid)), [])))


def item_text(kind: str, row, ctx: _Context) -> str:
    """The text that represents one item for embedding (field labels in English, content as stored)."""
    r = {k: row[k] for k in row.keys()}
    title, publisher = ctx.source(r.get("document_id"))
    if kind == "observation":
        value = " ".join(x for x in (_fmt_num(r["value"]) if r["value"] is not None else (r["value_text"] or ""),
                                     r["unit"] or "") if x)
        parts = [("Figure", r["metric"]), ("Category", r["category"]), ("Value", value), ("Period", r["period"]),
                 ("Geography", r["geo"]), ("Population", r["population"]), ("Source", title), ("Publisher", publisher)]
    elif kind == "forecast":
        if r["value"] is not None:
            value = _fmt_num(r["value"])
        elif r["value_low"] is not None or r["value_high"] is not None:
            value = f"{_fmt_num(r['value_low'])}–{_fmt_num(r['value_high'])}"
        else:
            value = ""
        parts = [("Forecast", r["statement"]), ("Target period", r["target_period"]),
                 ("Value", f"{value} {r['unit'] or ''}".strip() if value else ""),
                 ("Forecaster", r["forecaster"] or publisher), ("Source", title)]
    elif kind == "study":
        parts = [("Study finding", r["finding"]), ("Intervention", r["intervention"]), ("Outcome", r["outcome"]),
                 ("Effect", r["effect"]), ("Sector", r["sector"]), ("Region", r["region"]), ("Title", title),
                 ("Publisher", publisher)]
    elif kind == "quote":
        parts = [("Quote", r["text"]), ("Speaker", r["speaker"]), ("Role", r["role"]),
                 ("Organisation", r["organisation"]), ("Source", title)]
    elif kind == "practice":
        parts = [("Organisation", r["organisation"]), ("Practice", r["what"]), ("Result", r["result"]),
                 ("Country", r["country"]), ("Source", title)]
    elif kind == "fact":
        parts = [("Fact", r["title"]), ("What", r["what"]), ("Kind", r["kind"]), ("Geography", r["geo"]),
                 ("Implication", r["implication"]), ("Source", title)]
    elif kind == "series":
        parts = [("Time series", r["name"]), ("Unit", r["unit"]), ("Geography", r["geo"]),
                 ("Publisher", ctx.publishers.get(r["publisher_id"]))]
    elif kind == "exhibit":   # a slide/chart: what it claims and shows (re-embedded when a new version changes the text)
        label = {"chart": "Chart slide", "poster": "Poster", "section": "Section slide"}.get(r["kind"], "Slide")
        parts = [(label, r["title"]), ("So what", r["so_what"]), ("Eyebrow", r["eyebrow"]), ("Meta", r["meta"]),
                 ("Chart heads", r["chart_heads"]), ("Chart types", r["chart_types"]),
                 ("Labels", (r["labels"] or "")[:500]), ("Sources", (r["sources"] or "")[:400]),
                 ("Text", (r["body_text"] or "")[:500]), ("Project", r["project"])]
    else:
        raise ValueError(f"unknown kind {kind}")
    parts.append(("Tags", ctx.tag_labels(kind, r["id"])))
    return _join(parts)[:MAX_CHARS]


def text_hash(text: str, internal: bool = False) -> str:
    """Hash of the embedded text plus the visibility flag (kept in the vec0 metadata)."""
    return hashlib.sha1((text + ("\x00internal" if internal else "")).encode("utf-8")).hexdigest()


def _internal(kind: str, row) -> int:
    return int(kind != "series" and row["visibility"] == "internal")


# ------------------------------------------------------------------ pending / embedding runs
def pending(store: Store, space: Space, all_items: bool = False, before: str | None = None):
    """(todo, orphans): todo = [(kind, id, text, hash, internal)] for items without a current vector (all items
    with all_items, or – resuming a full re-embed – those whose vector is older than `before`);
    orphans = [embedding.id] of vectors whose item no longer exists."""
    ctx = _Context(store)
    have = {(k, i): (eid, h, u) for eid, k, i, h, u in store.db.execute(
        "SELECT id, item_kind, item_id, text_hash, updated_at FROM embedding WHERE model = ? AND dim = ? AND quant = ?",
        (space.model, space.dim, space.quant))}
    todo, seen = [], set()
    for kind in EMBED_KINDS:
        for row in store.db.execute(f"SELECT * FROM {kind} ORDER BY rowid"):
            iid = str(row["id"])
            seen.add((kind, iid))
            text = item_text(kind, row, ctx)
            internal = _internal(kind, row)
            h = text_hash(text, bool(internal))
            cur = have.get((kind, iid))
            if all_items or cur is None or cur[1] != h or (before and cur[2] < before):
                todo.append((kind, iid, text, h, internal))
    orphans = [v[0] for k, v in have.items() if k not in seen]
    return todo, orphans


def _lock(store: Store):
    f = open(store.path.parent / (store.path.name + ".embed.lock"), "w")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:              # Windows: lock the first byte, non-blocking; released when the file is closed
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write(store: Store, space: Space, chunk: list, vecs: np.ndarray) -> None:
    """Upsert one batch into embedding + the space's vec0 table (one transaction)."""
    now = _now()
    blobs, bitv = encode(vecs, space.quant), bits(vecs)
    db = store.db
    for (kind, iid, _, h, internal), blob, bv in zip(chunk, blobs, bitv):
        eid = db.execute(
            "INSERT INTO embedding(item_kind, item_id, model, dim, quant, text_hash, vector, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_kind, item_id, model, dim, quant) DO UPDATE SET "
            "text_hash = excluded.text_hash, vector = excluded.vector, updated_at = excluded.updated_at RETURNING id",
            (kind, iid, space.model, space.dim, space.quant, h, blob, now)).fetchone()[0]
        db.execute(f"DELETE FROM {space.vec_table} WHERE rowid = ?", (eid,))
        db.execute(f"INSERT INTO {space.vec_table}(rowid, embedding, kind, internal) VALUES (?, vec_bit(?), ?, ?)",
                   (eid, bv, kind, internal))
    db.commit()


def _delete(store: Store, space: Space, ids: list[int]) -> None:
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        store.db.execute(f"DELETE FROM {space.vec_table} WHERE rowid IN ({marks})", part)
        store.db.execute(f"DELETE FROM embedding WHERE id IN ({marks})", part)
    store.db.commit()


def embed_pending(store: Store, limit: int | None = None, all_items: bool = False, embedder=None,
                  space: Space | None = None, batch_size: int = BATCH_SIZE, progress=None) -> dict:
    """Embed new/changed items of `space` (default: the active space, else the configured default) and drop
    vectors of deleted items. One transaction per batch, so an interrupted run loses at most one batch and
    simply continues next time; a full re-embed (all_items) is checkpointed in meta and resumes where it
    stopped. Checks the Ollama two-model rule before every batch. The first space ever filled becomes active."""
    if embedder is None and _override is not None:
        embedder = _override
    if space is None:
        base = active_space(store) or default_space()
        space = Space(embedder.model, embedder.dim, base.quant) if embedder is not None else base
    emb = embedder or embedder_for(space)
    if (emb.model, emb.dim) != (space.model, space.dim):
        raise ValueError(f"embedder {emb.model}/{emb.dim} does not match space {space.key}")
    t0 = time.monotonic()
    lock = _lock(store)
    if lock is None:
        return {"space": space.key, "skipped": "another embedding run is active"}
    try:
        ensure_space(store, space)
        ckey = f"embedding.reembed.{space.key}"
        before = (store.db.execute("SELECT value FROM meta WHERE key = ?", (ckey,)).fetchone() or [None])[0]
        if all_items and not before:
            before = _now()
            store.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (ckey, before))
            store.db.commit()
        todo, orphans = pending(store, space, before=before)
        if orphans:
            _delete(store, space, orphans)
        total = len(todo)
        if limit is not None:
            todo = todo[: max(0, int(limit))]
        out = {"space": space.key, "pending": total, "embedded": 0, "removed": len(orphans)}
        if before:
            out["full_reembed_since"] = before
        t_embed = time.monotonic()
        for i in range(0, len(todo), batch_size):
            if hasattr(emb, "check_capacity"):
                try:
                    emb.check_capacity()
                except EmbedUnavailable as e:
                    out["stopped"] = str(e)
                    break
            chunk = todo[i:i + batch_size]
            try:
                vecs = emb.embed_documents([t for _, _, t, _, _ in chunk])
            except EmbedUnavailable as e:
                out["stopped"] = str(e)
                break
            _write(store, space, chunk, vecs)
            out["embedded"] += len(chunk)
            if progress:
                progress(out["embedded"], len(todo))
        secs = time.monotonic() - t_embed
        if before and "stopped" not in out and out["embedded"] == total:
            store.db.execute("DELETE FROM meta WHERE key = ?", (ckey,))
            store.db.commit()
        if out["embedded"] and active_space(store) is None:
            set_active(store, space)
            out["activated"] = True
        out["seconds"] = round(time.monotonic() - t0, 2)
        if out["embedded"]:
            out["items_per_s"] = round(out["embedded"] / secs, 1) if secs > 0 else None
        out["total_in_space"] = space_count(store, space)
        return out
    finally:
        lock.close()


def rebuild_index(store: Store, space: Space | None = None, batch: int = 5000) -> dict:
    """Recreate the vec0 coarse index of a space from the stored vectors (no re-embedding)."""
    space = space or active_space(store) or default_space()
    load_vec(store)
    t0 = time.monotonic()
    store.db.execute(f"DROP TABLE IF EXISTS {space.vec_table}")
    store.db.commit()
    ensure_space(store, space)
    vis = {}
    for kind in ITEM_KINDS + ("exhibit",):
        vis.update({(kind, str(i)): int(v == "internal") for i, v in store.db.execute(f"SELECT id, visibility FROM {kind}")})
    n, last = 0, 0
    while True:
        rows = store.db.execute("SELECT id, item_kind, item_id, vector FROM embedding WHERE model = ? AND dim = ? "
                                "AND quant = ? AND id > ? ORDER BY id LIMIT ?",
                                (space.model, space.dim, space.quant, last, batch)).fetchall()
        if not rows:
            break
        bv = bits(decode([r[3] for r in rows], space.quant, space.dim))
        store.db.executemany(f"INSERT INTO {space.vec_table}(rowid, embedding, kind, internal) VALUES (?, vec_bit(?), ?, ?)",
                             [(r[0], b, r[1], vis.get((r[1], r[2]), 0)) for r, b in zip(rows, bv)])
        store.db.commit()
        n += len(rows)
        last = rows[-1][0]
    return {"space": space.key, "indexed": n, "seconds": round(time.monotonic() - t0, 2)}


def derive_space(store: Store, src: Space, dst: Space, batch: int = 5000) -> dict:
    """Fill space `dst` from the stored vectors of `src` without Ollama: fewer dimensions (Matryoshka truncation
    + renormalisation) and/or another quantisation. Lossless from a float32 source; from int8/float16 the
    quantisation error carries over. Same model only."""
    if src.model != dst.model or dst.dim > src.dim:
        raise ValueError("derive needs the same model and dst.dim <= src.dim")
    if src == dst:
        raise ValueError("source and target space are the same")
    ensure_space(store, dst)
    vis = {}
    for kind in ITEM_KINDS + ("exhibit",):
        vis.update({(kind, str(i)): int(v == "internal") for i, v in store.db.execute(f"SELECT id, visibility FROM {kind}")})
    t0, n, last = time.monotonic(), 0, 0
    while True:
        rows = store.db.execute("SELECT id, item_kind, item_id, text_hash, vector FROM embedding WHERE model = ? AND dim = ? "
                                "AND quant = ? AND id > ? ORDER BY id LIMIT ?",
                                (src.model, src.dim, src.quant, last, batch)).fetchall()
        if not rows:
            break
        vecs = normalise(decode([r[4] for r in rows], src.quant, src.dim), dst.dim)
        _write(store, dst, [(r[1], r[2], None, r[3], vis.get((r[1], r[2]), 0)) for r in rows], vecs)
        n += len(rows)
        last = rows[-1][0]
    return {"from": src.key, "to": dst.key, "vectors": n, "seconds": round(time.monotonic() - t0, 2)}


def drop_space(store: Store, space: Space) -> dict:
    """Delete all vectors of a space that is not active (e.g. the old model after a switch)."""
    act = active_space(store)
    if act == space:
        raise ValueError("cannot drop the active embedding space – activate another one first")
    load_vec(store)
    n = space_count(store, space)
    store.db.execute("DELETE FROM embedding WHERE model = ? AND dim = ? AND quant = ?", (space.model, space.dim, space.quant))
    store.db.execute(f"DROP TABLE IF EXISTS {space.vec_table}")
    store.db.execute("DELETE FROM embedding_space WHERE space = ?", (space.key,))
    store.db.execute("DELETE FROM meta WHERE key = ?", (f"embedding.reembed.{space.key}",))
    store.db.commit()
    return {"dropped": space.key, "vectors": n}


def status(store: Store) -> dict:
    act = active_space(store)
    n_items = sum(store.db.execute(f"SELECT count(*) FROM {k}").fetchone()[0] for k in EMBED_KINDS)
    spaces = []
    for (key,) in store.db.execute("SELECT space FROM embedding_space ORDER BY created_at"):
        sp = Space.parse(key)
        r = store.db.execute("SELECT count(*), sum(length(vector)), max(updated_at) FROM embedding "
                             "WHERE model = ? AND dim = ? AND quant = ?", (sp.model, sp.dim, sp.quant)).fetchone()
        ck = store.db.execute("SELECT value FROM meta WHERE key = ?", (f"embedding.reembed.{key}",)).fetchone()
        spaces.append({"space": key, "active": sp == act, "vectors": r[0], "vector_bytes": r[1] or 0,
                       "last_update": r[2], **({"full_reembed_since": ck[0]} if ck else {})})
    return {"items": n_items, "active": act.key if act else None, "spaces": spaces}


def prune_search_log(store: Store, days: int = SEARCH_LOG_RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    n = store.db.execute("DELETE FROM search_log WHERE ts < ?", (cutoff,)).rowcount
    store.db.commit()
    return n
