"""Write and maintenance API for the Fundus database.

All importers, the CLI and the MCP server go through Store, so de-duplication, tagging, origin
records and the changelog work the same everywhere. Typical importer use:

    st = Store()
    with st.batch("acme-importer"):
        doc = st.document("CDK Global AI survey 2021", url="https://...", publisher="CDK Global",
                          kind="survey_report", published="2021-12")
        st.series("cdk-dealer-ai-implemented-or-planned", "Share of dealers ...", unit="%", publisher="CDK Global")
        st.add("observation", {"series_id": "cdk-dealer-ai-implemented-or-planned", "document_id": doc,
                               "metric": "...", "value": 68, "unit": "%", "period": "2021", "status": "verified"},
               tags={"industry": ["automotive-retail"], "topic": ["ai-adoption"], "region": ["us"]},
               origin=("acme-dealerships", "zahlen/adoption.json", "1"))
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fundus import config as _config
from fundus import vocab
from fundus.publisher_aliases import ALIASES
from fundus.publisher_types import TYPES as PUBLISHER_TYPES
from fundus.geo import COUNTRIES, canonical_geo, region_slug
from fundus.units import conversion, normalise_unit

SCHEMA_VERSION = "1"
DEFAULT_DB = _config.default_db()   # $FUNDUS_DB, else data/fundus.sqlite in the checkout (fundus/config.py)

ITEM_KINDS = ("observation", "forecast", "study", "quote", "practice", "fact")
REQUIRED = {"observation": ("metric",), "forecast": ("statement",), "study": ("finding",),
            "quote": ("text",), "practice": ("what",), "fact": ("kind", "title")}
# Columns that form the natural identity of an item when no origin record matches.
IDENTITY = {
    "observation": ("series_id", "metric", "category", "period", "geo", "population", "unit", "n",
                    "value", "value_text", "document_id"),
    "forecast": ("statement", "document_id"),
    "study": ("finding", "document_id"),
    "quote": ("text", "speaker"),
    "practice": ("organisation", "what"),
    "fact": ("kind", "title", "document_id"),
}
# Text that goes into the full-text index per kind.
SEARCH_TEXT = {
    "observation": ("metric", "category", "value_text", "unit", "period", "geo", "population", "quote_context", "notes"),
    "forecast": ("statement", "original", "forecaster", "metric", "target_period", "scenario", "geo"),
    "study": ("design", "region", "sector", "intervention", "outcome", "effect", "moderators", "finding"),
    "quote": ("text", "original", "speaker", "role", "organisation", "context"),
    "practice": ("organisation", "segment", "country", "kind", "what", "scale", "result", "people", "quote"),
    "fact": ("kind", "title", "what", "geo", "implication", "attributes"),
    # slides/charts (fundus/exhibits.py); 'kind' gives the words slide | chart | poster | section
    "exhibit": ("kind", "title", "eyebrow", "meta", "so_what", "chart_heads", "labels", "sources", "notes", "body_text",
                "chart_types", "project", "slug", "deck"),
}
# Indexed kinds that are not evidence items (own tables, no tags/document): exhibits.
EXTRA_INDEX_KINDS = ("exhibit",)
TRACKED = ("value", "value_text", "status", "visibility", "outcome", "actual_value", "period", "notes", "document_id")
# Fields a re-import must not overwrite once someone changed them by hand (MCP, CLI).
CURATED = ("status", "visibility", "notes", "document_id", "value", "value_text", "period", "series_id")


def slug(text: str, maxlen: int = 80) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t[:maxlen].rstrip("-")


_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src$)")


def norm_url(url: str | None) -> str | None:
    """Comparable form of a URL: lower-case host without www, no fragment, no tracking parameters,
    no trailing slash, http/https treated alike."""
    if not url or not url.strip():
        return None
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    p = urlsplit(u)
    host = p.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or ""
    return urlunsplit(("", host, path, query, "")).lstrip("/")


_LEGAL = re.compile(r"[\s,]+(&\s*(co|company)|inc|ltd|llc|gmbh|ag|plc|corp|corporation|s\.?a\.?|n\.?v\.?)\.?$", re.I)


_SHORT_FORM = re.compile(r"^[A-Z][A-Za-z0-9&./-]{1,11}$")


def publisher_detail(name: str | None) -> list[str]:
    """Brackets in a publisher name that describe the document rather than the organisation (not short forms)."""
    return [b.strip() for b in re.findall(r"\(([^()]*)\)", name or "") if b.strip() and not _SHORT_FORM.match(b.strip())]


def publisher_name(name: str) -> str:
    """The organisation's name without document-specific brackets: 'University of St.Gallen (master's thesis)' →
    'University of St.Gallen'; 'Bundesamt für Statistik (BFS)' stays."""
    out = name.strip()
    for b in publisher_detail(name):
        out = out.replace(f"({b})", " ")
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;–-")
    return out or name.strip()


def publisher_slug(name: str) -> str:
    """Slug without legal suffixes, so 'McKinsey & Company' and 'McKinsey' land on the same publisher."""
    n = re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip()
    while True:
        m = _LEGAL.search(n)
        if not m:
            break
        n = n[:m.start()]
    return slug(n or name)


def _norm(v):
    if isinstance(v, str):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", v)).strip().casefold()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def ident_key(kind: str, data) -> str | None:
    """Normalised natural identity of an item (Unicode-aware, whitespace-insensitive), stored in `ident`."""
    cols = IDENTITY[kind]
    vals = [_norm(data[c]) if c in data.keys() and data[c] not in (None, "") else None for c in cols]
    if not any(v is not None for v in vals):
        return None
    return hashlib.sha1(json.dumps(vals, ensure_ascii=False, default=str).encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.actor = "api"
        self.warnings: list[str] = []   # notes from the last add() (unit mismatches …), shown by the MCP server
        self._columns: dict[str, list[str]] = {}
        self._vocab: set[tuple[str, str]] | None = None
        self.init()

    # ---------------------------------------------------------------- setup
    def _init_fingerprint(self) -> str:
        """Changes when the schema, the migrations or the seed data change (then init() has work to do)."""
        here = Path(__file__).parent
        h = hashlib.sha1(SCHEMA_VERSION.encode())
        for f in ("schema.sql", "store.py", "vocab.py", "geo.py", "publisher_aliases.py"):
            try:
                h.update((here / f).read_bytes())
            except OSError:
                h.update(f.encode())
        return h.hexdigest()

    def _schema_complete(self) -> bool:
        """Read-only check that every table/index of schema.sql and every migrated column exists (an old copy or a
        hand-edited database still gets the full init)."""
        text = (Path(__file__).parent / "schema.sql").read_text()
        names = set(re.findall(r"CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+\"?(\w+)", text, re.I))
        have = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master")}
        if not names <= have:
            return False
        need = {t: set(c) for t, c in self.EXHIBIT_COLUMNS.items()}
        need.setdefault("exhibit", set()).add("set_id")
        need["review"] = {"gemini_model", "gemini_report", "remote_secret", "remote_state", "completed_at"}
        need["origin"] = {"merged"}
        for t, cols in need.items():
            if cols - {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}:
                return False
        return True

    def init(self) -> None:
        # Opening the database must not need the write lock when nothing changed: the nightly import holds it for a while,
        # and services starting meanwhile (review-sync, embed) failed with 'database is locked' (1 Nov 2026).
        fp = self._init_fingerprint()
        try:
            r = self.db.execute("SELECT value FROM meta WHERE key = 'init_fingerprint'").fetchone()
            if r and r[0] == fp and self._schema_complete():
                return
        except sqlite3.OperationalError:
            pass                                  # new database: no meta table yet
        self._init_full()
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('init_fingerprint', ?)", (fp,))
        self.db.commit()

    def _init_full(self) -> None:
        self.db.executescript((Path(__file__).parent / "schema.sql").read_text())
        if "merged" not in [r[1] for r in self.db.execute("PRAGMA table_info(origin)")]:
            self.db.execute("ALTER TABLE origin ADD COLUMN merged INTEGER NOT NULL DEFAULT 0")
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(review)")]
        for col in ("gemini_model", "gemini_report", "remote_secret", "remote_state"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE review ADD COLUMN {col} TEXT")
        if "finished_at" not in [r[1] for r in self.db.execute("PRAGMA table_info(review_access)")]:
            self.db.execute("ALTER TABLE review_access ADD COLUMN finished_at TEXT")
        if "completed_at" not in [r[1] for r in self.db.execute("PRAGMA table_info(review)")]:
            self.db.execute("ALTER TABLE review ADD COLUMN completed_at TEXT")
        ic = [r[1] for r in self.db.execute("PRAGMA table_info(item_check)")]
        if "exhibit_id" not in ic:
            self.db.execute("ALTER TABLE item_check ADD COLUMN exhibit_id INTEGER")
        if "aspect" not in ic:
            self.db.execute("ALTER TABLE item_check ADD COLUMN aspect TEXT NOT NULL DEFAULT 'usage'")
        if "weight" not in [r[1] for r in self.db.execute("PRAGMA table_info(feedback)")]:
            self.db.execute("ALTER TABLE feedback ADD COLUMN weight TEXT")
        if "remote_state" not in [r[1] for r in self.db.execute("PRAGMA table_info(feedback)")]:
            self.db.execute("ALTER TABLE feedback ADD COLUMN remote_state TEXT")
        self._migrate_exhibits()
        self._migrate_exhibit_sets()
        # column counts of dataset tables, cached from the profiles once (catalog without parsing 190 MB of JSON)
        self.db.execute("UPDATE dataset_file SET columns_n = coalesce(json_array_length(profile, '$.columns'), 0) "
                        "WHERE role = 'table' AND columns_n IS NULL AND profile IS NOT NULL AND json_valid(profile)")
        # covering index for the dataset catalog (fundus.dataset_catalog.FILE_COLS): no profile blobs read
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_dataset_file_catalog ON dataset_file(dataset_id, version, role, rows, "
                        "columns_n, bytes, sha, ext, parquet_sha, verified_at, verified_ok, name, table_name)")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_exhibit_global ON exhibit(global_id)")   # after the column migration
        self._migrate_entity_link()
        if not self.db.execute("SELECT 1 FROM vocab LIMIT 1").fetchone():
            for facet, rows in vocab.SEED.items():
                self.db.executemany("INSERT OR IGNORE INTO vocab(facet, value, parent, label, description) VALUES (?,?,?,?,?)",
                                    [(facet, *r) for r in rows])
        self.db.executemany("INSERT OR IGNORE INTO vocab(facet, value, parent, label) VALUES ('region', ?, ?, ?)",
                            [(c, parent, label) for c, (label, parent, _) in COUNTRIES.items()])
        self.db.executemany("INSERT OR IGNORE INTO publisher_alias(alias, publisher_id) VALUES (?, ?)",
                            [(a, c) for a, (c, _) in ALIASES.items()])
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    # Columns added to the exhibit tables after their first release (additive migrations: name -> SQL type/default).
    EXHIBIT_COLUMNS: dict[str, dict[str, str]] = {
        # per-slide override of the sharing: NULL = follow project/global, 'shared' = all projects, 'project' = own project
        "exhibit": {"sharing": "TEXT CHECK (sharing IN ('shared','project'))",
                    # archive (fundus.archive): findable everywhere, not listed in its project/set
                    "archived_at": "TEXT", "archived_by": "TEXT",
                    # favourites (fundus.favorites, 1 Nov 2026): marked by Falk in the portal or by a session via MCP
                    "favorite_at": "TEXT", "favorite_by": "TEXT",
                    # world-unique id for slide packages (fundus.package, Nov 2026): uuid4, assigned on first export
                    "global_id": "TEXT"},
        "exhibit_version": {},
        # journal / container title of an article (from Crossref by DOI), separate from the publishing house (31 Oct 2026)
        "document": {"journal": "TEXT"},
        # media archive: videos next to photos (28 Oct 2026)
        # update reminders for datasets (31 Oct 2026)
        "dataset": {"frequency": "TEXT CHECK (frequency IN ('annual','semiannual','quarterly','monthly','irregular','once'))",
                    "next_expected": "TEXT", "update_hint": "TEXT", "last_checked": "TEXT",
                    # linked data (1 Nov 2026): crosswalk datasets, column code systems, tables across versions
                    "kind": "TEXT NOT NULL DEFAULT 'data' CHECK (kind IN ('data','crosswalk'))", "crosswalk": "TEXT",
                    "code_systems": "TEXT",
                    "table_scope": "TEXT NOT NULL DEFAULT 'version' CHECK (table_scope IN ('version','merged'))",
                    # catalog & archive (3 Nov 2026): lifecycle, reference datasets, provenance
                    "lifecycle": "TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','superseded','withdrawn'))",
                    "superseded_by": "INTEGER", "lifecycle_note": "TEXT", "lifecycle_at": "TEXT",
                    "holding": "TEXT NOT NULL DEFAULT 'stored' CHECK (holding IN ('stored','reference'))",
                    "retrieved_at": "TEXT", "script": "TEXT",
                    # texts with versions (3 Nov 2026, fundus.dataset_texts): a dataset is a text when act_type is set.
                    # Not a new value of `kind`: widening that CHECK would mean rebuilding the dataset table (four tables
                    # reference it); an added column is a plain ALTER TABLE.
                    "identifier": "TEXT", "jurisdiction": "TEXT",
                    "act_type": "TEXT CHECK (act_type IN ('law','regulation','directive','ordinance','guideline','standard',"
                                "'terms','policy','other'))",
                    "issuer": "TEXT"},
        # validity of a text's versions (3 Nov 2026)
        "dataset_version": {"valid_from": "TEXT", "valid_to": "TEXT", "version_label": "TEXT", "published_at": "TEXT",
                            "source_url": "TEXT"},
        # column count cache and fixity of dataset files (3 Nov 2026)
        "dataset_file": {"columns_n": "INTEGER", "verified_at": "TEXT", "verified_ok": "INTEGER"},
        # research registry (3 Nov 2026): an identical result is marked, never deleted
        "hypothesis_result": {"duplicate_of": "INTEGER"},
        "photo": {"media": "TEXT NOT NULL DEFAULT 'photo' CHECK (media IN ('photo','video'))", "duration": "REAL", "fps": "REAL",
                  "audio": "TEXT NOT NULL DEFAULT 'none' CHECK (audio IN ('none','ambient','speech','unknown'))"}}

    def _migrate_exhibits(self) -> None:
        for table, cols in self.EXHIBIT_COLUMNS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    DEFAULT_SET = ("main", "Hauptfoliensatz")   # slug, title of the default slide set every project gets

    def _migrate_exhibit_sets(self) -> None:
        """Slide sets: identity of an exhibit becomes project + set + slug. Databases created before get one default set
        per project (slug 'main', folder ''), all exhibits go into it, and the exhibit table is rebuilt with set_id NOT
        NULL and UNIQUE(project, set_id, slug) – ids, all columns and all rows pointing at exhibits stay as they are.
        One short transaction (BEGIN IMMEDIATE, other writers wait); foreign keys are off during the rebuild only so that
        dropping the old table does not cascade into versions/decks/links (the SQLite table-rebuild procedure)."""
        if "set_id" in {r[1] for r in self.db.execute("PRAGMA table_info(exhibit)")}:
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_exhibit_set ON exhibit(set_id)")
            return
        m = re.search(r"CREATE TABLE IF NOT EXISTS exhibit \((.*?)\n\);",
                      (Path(__file__).parent / "schema.sql").read_text(), re.S)
        if not m or "set_id" not in m.group(1):
            raise RuntimeError("schema.sql: exhibit table definition with set_id not found")
        ddl = "CREATE TABLE exhibit_new (" + m.group(1) + "\n)"
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if "set_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(exhibit)")}:   # nobody else did it
                    self._rebuild_exhibit_with_sets(ddl)
                self.db.execute("CREATE INDEX IF NOT EXISTS ix_exhibit_set ON exhibit(set_id)")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        finally:
            self.db.execute("PRAGMA foreign_keys = ON")

    def _rebuild_exhibit_with_sets(self, ddl: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        slug_, title = self.DEFAULT_SET
        before = self.db.execute("SELECT count(*), coalesce(sum(id), 0) FROM exhibit").fetchone()
        projects = [r[0] for r in self.db.execute("SELECT DISTINCT project FROM exhibit ORDER BY project")]
        for p in projects:
            self.db.execute("INSERT OR IGNORE INTO exhibit_set(project, slug, title, is_default, created_at, updated_at) "
                            "VALUES (?, ?, ?, 1, ?, ?)", (p, slug_, title, now, now))
            sid = self.db.execute("SELECT id FROM exhibit_set WHERE project = ? AND is_default = 1", (p,)).fetchone()[0]
            self.db.execute("INSERT OR IGNORE INTO exhibit_set_folder(set_id, project, folder) VALUES (?, ?, '')", (sid, p))
        self.db.execute("DROP TABLE IF EXISTS exhibit_new")
        self.db.execute(ddl)
        old_cols = [r[1] for r in self.db.execute("PRAGMA table_info(exhibit)")]
        new_cols = {r[1] for r in self.db.execute("PRAGMA table_info(exhibit_new)")}
        missing = [c for c in old_cols if c not in new_cols]
        if missing:
            raise RuntimeError(f"exhibit columns {missing} are not in schema.sql – add them before migrating")
        cols = ", ".join(old_cols)
        self.db.execute(f"INSERT INTO exhibit_new({cols}, set_id) SELECT {cols}, (SELECT s.id FROM exhibit_set s WHERE "
                        f"s.project = exhibit.project AND s.is_default = 1) FROM exhibit")
        after = self.db.execute("SELECT count(*), coalesce(sum(id), 0) FROM exhibit_new").fetchone()
        if tuple(after) != tuple(before):
            raise RuntimeError(f"exhibit rebuild lost rows: {tuple(before)} -> {tuple(after)}")
        self.db.execute("DROP TABLE exhibit")
        self.db.execute("ALTER TABLE exhibit_new RENAME TO exhibit")
        for t in ("exhibit", "exhibit_version", "exhibit_deck", "exhibit_link", "exhibit_set_folder"):
            bad = self.db.execute(f"PRAGMA foreign_key_check({t})").fetchall()
            if bad:
                raise RuntimeError(f"foreign key check failed after the exhibit rebuild ({t}: {len(bad)} rows)")
        self.db.execute("INSERT INTO changelog(item_kind, item_id, action, detail, actor) VALUES ('exhibit_set', '*', "
                        "'migrate', ?, 'migration')", (json.dumps({"default_sets": projects, "exhibits": before[0]}),))
        self._columns.pop("exhibit", None)

    def _migrate_entity_link(self) -> None:
        """entity_link may link exhibits (slides) too: widen the item_kind CHECK of databases created before (table rebuild,
        one transaction; other connections simply see the new schema)."""
        r = self.db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entity_link'").fetchone()
        if not r or "'exhibit'" in r[0]:
            return
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            sql = self.db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entity_link'").fetchone()[0]
            if "'exhibit'" not in sql:
                new = sql.replace("'fact','series')", "'fact','series','exhibit')", 1)
                new = re.sub(r"CREATE TABLE\s+\"?entity_link\"?", "CREATE TABLE entity_link_new", new, count=1)
                if "'exhibit'" not in new:
                    raise RuntimeError("unexpected entity_link schema – cannot add 'exhibit'")
                self.db.execute(new)
                self.db.execute("INSERT INTO entity_link_new SELECT * FROM entity_link")
                self.db.execute("DROP TABLE entity_link")
                self.db.execute("ALTER TABLE entity_link_new RENAME TO entity_link")
                self.db.execute("CREATE INDEX IF NOT EXISTS ix_entity_link_item ON entity_link(item_kind, item_id)")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            self._columns[table] = [r[1] for r in self.db.execute(f"PRAGMA table_info({table})")]
        return self._columns[table]

    @contextmanager
    def batch(self, actor: str = "api"):
        """One transaction; rolls back on error. Rebuilds the search index for touched items."""
        prev, self.actor = self.actor, actor
        self._touched: set[tuple[str, str]] = set()
        try:
            yield self
            self._index_touched()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        finally:
            self.actor = prev

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- vocabulary
    def vocab_values(self) -> set[tuple[str, str]]:
        if self._vocab is None:
            self._vocab = {(r[0], r[1]) for r in self.db.execute("SELECT facet, value FROM vocab")}
        return self._vocab

    def add_vocab(self, facet: str, value: str, label: str, parent: str | None = None, description: str | None = None) -> str:
        value = slug(value)
        if parent and (facet, parent) not in self.vocab_values():
            raise ValueError(f"unknown parent {facet}:{parent}")
        self.db.execute("INSERT OR IGNORE INTO vocab(facet, value, parent, label, description) VALUES (?,?,?,?,?)",
                        (facet, value, parent, label, description))
        self._vocab = None
        return value

    # ---------------------------------------------------------------- reference entities
    def publisher(self, name: str, type: str | None = None, country: str | None = None,
                  url: str | None = None, id: str | None = None, notes: str | None = None) -> str | None:
        """Upsert by slug; existing rows only get empty fields filled. Returns the publisher id."""
        if not name or not name.strip():
            return None
        pid = id or publisher_slug(name)
        alias = self.db.execute("SELECT publisher_id FROM publisher_alias WHERE alias = ?", (pid,)).fetchone()
        if alias:
            pid = alias[0]
            if pid in {c for c, _ in ALIASES.values()}:   # canonical name instead of the variant's
                name = next(n for c, n in ALIASES.values() if c == pid)
        type = type or PUBLISHER_TYPES.get(pid)
        row = self.db.execute("SELECT * FROM publisher WHERE id = ?", (pid,)).fetchone()
        if row is None:
            # the row stands for the organisation; a bracket that describes one document ('(master's thesis)',
            # '(YouTube channel …)') would label every later document of it – only short forms ('(BFS)') stay
            self.db.execute("INSERT INTO publisher(id, name, type, country, url, notes) VALUES (?,?,?,?,?,?)",
                            (pid, publisher_name(name), type, country, url, notes))
        else:
            self._fill("publisher", "id", pid, row, {"type": type, "country": country, "url": url, "notes": notes})
        return pid

    def merge_publisher(self, src: str, dst: str) -> None:
        """Fold publisher `src` into `dst`: documents and series move, src becomes an alias of dst."""
        if src == dst:
            return
        if not self.db.execute("SELECT 1 FROM publisher WHERE id = ?", (dst,)).fetchone():
            raise KeyError(f"publisher {dst} not found")
        row = self.db.execute("SELECT * FROM publisher WHERE id = ?", (src,)).fetchone()
        if row:
            self._fill("publisher", "id", dst, self.db.execute("SELECT * FROM publisher WHERE id = ?", (dst,)).fetchone(),
                       {k: row[k] for k in ("type", "country", "url", "notes")})
        for (doc_id,) in self.db.execute("SELECT id FROM document WHERE publisher_id = ?", (src,)).fetchall():
            self._touch_document(doc_id)
        for (sid,) in self.db.execute("SELECT id FROM series WHERE publisher_id = ?", (src,)).fetchall():
            self._touch("series", sid)
        self.db.execute("UPDATE document SET publisher_id = ? WHERE publisher_id = ?", (dst, src))
        self.db.execute("UPDATE series SET publisher_id = ? WHERE publisher_id = ?", (dst, src))
        self.db.execute("UPDATE publisher_alias SET publisher_id = ? WHERE publisher_id = ?", (dst, src))
        self.db.execute("DELETE FROM publisher WHERE id = ?", (src,))
        self.db.execute("INSERT OR REPLACE INTO publisher_alias(alias, publisher_id) VALUES (?, ?)", (src, dst))
        self.log("publisher", dst, "merge", {"from": src})

    def merge_aliases(self) -> list[tuple[str, str]]:
        """Fold every existing publisher row that is a registered alias into its canonical publisher."""
        done = []
        for src, dst in self.db.execute("SELECT a.alias, a.publisher_id FROM publisher_alias a "
                                        "JOIN publisher p ON p.id = a.alias").fetchall():
            if not self.db.execute("SELECT 1 FROM publisher WHERE id = ?", (dst,)).fetchone():
                name = ALIASES.get(src, (dst, dst))[1]
                self.db.execute("INSERT INTO publisher(id, name) VALUES (?, ?)", (dst, name))
            self.merge_publisher(src, dst)
            done.append((src, dst))
        return done

    def document(self, title: str, url: str | None = None, publisher: str | None = None, **fields) -> int:
        """Upsert a document. Matches on the normalised URL, else on DOI, else on title + publisher.
        Documents sharing a URL but with different `published` dates stay separate (survey editions on one
        landing page). `publisher` may be a name or an existing publisher id. Returns the document id."""
        pid = None
        if publisher:
            exists = self.db.execute("SELECT id FROM publisher WHERE id = ?", (publisher,)).fetchone()
            pid = publisher if exists else self.publisher(publisher)
        key = norm_url(url)
        row = None
        pub = fields.get("published")
        if key:
            # Independent of import order: a dated request takes the same edition (same date, or the same date at
            # another precision like 2023 / 2023-05); an undated request takes the page's undated document.
            # Otherwise a new document. Documents of one page stay linked through url_key.
            cands = self.db.execute("SELECT * FROM document WHERE url_key = ? ORDER BY id", (key,)).fetchall()
            undated = next((c for c in cands if not c["published"]), None)
            if pub:
                row = next((c for c in cands if c["published"] == pub), None) or \
                      next((c for c in cands if c["published"] and (c["published"].startswith(pub) or pub.startswith(c["published"]))), None)
            else:
                row = undated
        if row is None and fields.get("doi"):
            row = self.db.execute("SELECT * FROM document WHERE lower(doi) = lower(?)", (fields["doi"],)).fetchone()
        if row is None and not key:
            row = self.db.execute("SELECT * FROM document WHERE lower(title) = lower(?) AND url_key IS NULL "
                                  "AND coalesce(publisher_id, '') = coalesce(?, '') AND coalesce(published, '') = coalesce(?, '')",
                                  (title.strip(), pid, pub)).fetchone()
        allowed = set(self.columns("document")) - {"id", "url_key", "created_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown document fields: {sorted(unknown)}")
        if row is None:
            data = {"title": title.strip(), "url": url, "url_key": key, "publisher_id": pid, **fields}
            cols = ", ".join(data)
            cur = self.db.execute(f"INSERT INTO document({cols}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
            return cur.lastrowid
        self._fill("document", "id", row["id"], row, {"publisher_id": pid, "url": url, **fields})
        return row["id"]

    def series(self, id: str, name: str, publisher: str | None = None, refresh: bool = False, **fields) -> str:
        """Upsert a series by its slug id. Existing rows only get empty fields filled, unless refresh=True:
        then the given name and fields overwrite the stored ones (changes are logged)."""
        sid = slug(id, 120)
        if fields.get("unit"):
            fields["unit"] = normalise_unit(fields["unit"])
        if fields.get("geo"):
            fields["geo"] = canonical_geo(fields["geo"])
        pid = None
        if publisher:
            exists = self.db.execute("SELECT id FROM publisher WHERE id = ?", (publisher,)).fetchone()
            pid = publisher if exists else self.publisher(publisher)
        allowed = set(self.columns("series")) - {"id", "created_at", "publisher_id", "name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown series fields: {sorted(unknown)}")
        row = self.db.execute("SELECT * FROM series WHERE id = ?", (sid,)).fetchone()
        if row is None:
            data = {"id": sid, "name": name, "publisher_id": pid, **fields}
            self.db.execute(f"INSERT INTO series({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
            self._touch("series", sid)
        elif refresh:
            changes = {k: v for k, v in {"name": name, "publisher_id": pid, **fields}.items() if v not in (None, "")}
            diff = {k: (row[k], v) for k, v in changes.items() if row[k] != v}
            if diff:
                self.db.execute(f"UPDATE series SET {', '.join(f'{k} = ?' for k in diff)} WHERE id = ?",
                                [v for _, v in diff.values()] + [sid])
                self.log("series", sid, "update", {k: {"old": o, "new": n} for k, (o, n) in diff.items()})
                self._touch("series", sid)
        else:
            self._fill("series", "id", sid, row, {"publisher_id": pid, **fields})
        return sid

    # ---------------------------------------------------------------- items
    def add(self, kind: str, fields: dict, tags: dict | None = None,
            origin: tuple[str, str, str] | None = None) -> int:
        """Insert or update one item. With `origin` (project, source_path, source_key) a re-import updates
        the same row; otherwise an item with the same natural identity is reused. Returns the item id."""
        if kind not in ITEM_KINDS:
            raise ValueError(f"unknown kind {kind}")
        allowed = set(self.columns(kind)) - {"id", "created_at", "updated_at", "ident"}
        data = {k: v for k, v in fields.items() if v is not None and v != ""}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown {kind} fields: {sorted(unknown)}")
        for req in REQUIRED[kind]:
            if not data.get(req):
                raise ValueError(f"{kind} needs '{req}'")
        if kind == "fact" and isinstance(data.get("attributes"), (dict, list)):
            data["attributes"] = json.dumps(data["attributes"], ensure_ascii=False)
        self.warnings = []
        if data.get("unit"):
            data["unit"] = normalise_unit(data["unit"])
        if data.get("geo"):
            data["geo"] = canonical_geo(data["geo"])      # one notation: country codes, WORLD (fundus.geo)
        if kind in ("observation", "forecast") and data.get("series_id"):
            self._fit_series_unit(kind, data)

        item_id = None
        if origin:
            r = self.db.execute("SELECT item_id FROM origin WHERE project=? AND source_path=? AND source_key=? AND item_kind=?",
                                (origin[0], origin[1] or "", str(origin[2] or ""), kind)).fetchone()
            item_id = int(r[0]) if r else None
        merged = False
        if origin and item_id is not None:
            merged = bool(self.db.execute("SELECT merged FROM origin WHERE project=? AND source_path=? AND source_key=? "
                                          "AND item_kind=?", (origin[0], origin[1] or "", str(origin[2] or ""), kind)).fetchone()[0])
        if item_id is not None and merged:
            # This source record was folded into another item as a duplicate: only fill gaps there.
            row = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (item_id,)).fetchone()
            self._fill(kind, "id", item_id, row, {k: v for k, v in data.items() if k not in ("status", "visibility")})
        elif item_id is not None:
            # Re-import of the same source record: refresh it, but keep what was curated by hand.
            self.update(kind, item_id, self._reimport_changes(kind, item_id, data))
        else:
            item_id = self._find_identical(kind, data)
            if item_id is not None:
                # Same item from another source: never rewrite its source or status, only fill gaps.
                row = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (item_id,)).fetchone()
                self._fill(kind, "id", item_id, row, {k: v for k, v in data.items() if k not in ("status", "visibility")})
            else:
                data["ident"] = ident_key(kind, data)
                cols = ", ".join(data)
                cur = self.db.execute(f"INSERT INTO {kind}({cols}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
                item_id = cur.lastrowid
                self.log(kind, item_id, "insert")
        if origin:
            self.db.execute("INSERT OR IGNORE INTO origin(item_kind, item_id, project, source_path, source_key, importer) "
                            "VALUES (?,?,?,?,?,?)", (kind, str(item_id), origin[0], origin[1] or "", str(origin[2] or ""), self.actor))
        if tags:
            self.tag(kind, item_id, **tags)
        reg = region_slug(data.get("geo") or data.get("country") or data.get("region"))
        if reg:
            self.tag(kind, item_id, region=[reg])
        if origin and origin[0]:
            self.tag(kind, item_id, project=[origin[0]])
        self._touch(kind, item_id)
        return item_id

    def update(self, kind: str, item_id: int, changes: dict) -> dict:
        """Apply changed fields; logs tracked fields to the changelog. Returns {field: (old, new)}."""
        row = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"{kind} {item_id} not found")
        if changes.get("unit"):
            changes = {**changes, "unit": normalise_unit(changes["unit"])}
        if changes.get("geo"):
            changes = {**changes, "geo": canonical_geo(changes["geo"])}
        diff = {k: (row[k], v) for k, v in changes.items() if k in row.keys() and k not in ("id", "ident") and row[k] != v}
        if diff:
            if kind in IDENTITY and set(diff) & set(IDENTITY[kind]):
                merged = {**{k: row[k] for k in row.keys()}, **{k: n for k, (_, n) in diff.items()}}
                diff["ident"] = (row["ident"], ident_key(kind, merged))
            sets = ", ".join(f"{k} = ?" for k in diff)
            extra = ", updated_at = CURRENT_TIMESTAMP" if "updated_at" in row.keys() else ""
            self.db.execute(f"UPDATE {kind} SET {sets}{extra} WHERE id = ?", [v[1] for v in diff.values()] + [item_id])
            tracked = {k: v for k, v in diff.items() if k in TRACKED}
            if tracked:
                self.log(kind, item_id, "update", {k: {"old": o, "new": n} for k, (o, n) in tracked.items()})
            self._touch(kind, item_id)
        return diff

    def tag(self, kind: str, item_id, **facets) -> None:
        """tag('observation', 5, industry=['automotive-retail'], topic=['ai-adoption'], region=['us'])"""
        known = self.vocab_values()
        for facet, values in facets.items():
            if facet not in ("industry", "topic", "region", "project"):
                raise ValueError(f"unknown facet {facet}")
            for v in [values] if isinstance(values, str) else (values or []):
                if not v:
                    continue
                v = slug(v)
                if facet != "project" and (facet, v) not in known:
                    raise ValueError(f"unknown {facet} '{v}' – register it with Store.add_vocab() first")
                self.db.execute("INSERT OR IGNORE INTO tag(item_kind, item_id, facet, value) VALUES (?,?,?,?)",
                                (kind, str(item_id), facet, v))
        self._touch(kind, item_id)

    def follow(self, kind: str, item_id) -> str:
        """The id under which a record merged by the clean-up lives on (the id itself if it was never merged)."""
        cur = str(item_id)
        for _ in range(20):
            r = self.db.execute("SELECT new_id FROM item_redirect WHERE item_kind = ? AND old_id = ?", (kind, cur)).fetchone()
            if not r:
                break
            cur = r[0]
        return cur

    def log(self, kind: str, item_id, action: str, detail: dict | None = None) -> None:
        self.db.execute("INSERT INTO changelog(item_kind, item_id, action, detail, actor) VALUES (?,?,?,?,?)",
                        (kind, str(item_id), action, json.dumps(detail, ensure_ascii=False, default=str) if detail else None, self.actor))

    # ---------------------------------------------------------------- search index
    def reindex(self) -> int:
        """Rebuild the full-text index from scratch."""
        self.db.execute("DELETE FROM search_index")
        n = 0
        for kind in ITEM_KINDS + ("series",) + EXTRA_INDEX_KINDS:
            for (iid,) in self.db.execute(f"SELECT id FROM {kind}").fetchall():
                self._index(kind, str(iid))
                n += 1
        self.db.commit()
        return n

    def _index_touched(self) -> None:
        for kind, iid in getattr(self, "_touched", set()):
            if kind in ITEM_KINDS or kind == "series" or kind in EXTRA_INDEX_KINDS:
                self._index(kind, iid)
        self._touched = set()

    def _index(self, kind: str, iid: str) -> None:
        self.db.execute("DELETE FROM search_index WHERE item_kind = ? AND item_id = ?", (kind, iid))
        row = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (iid if kind == "series" else int(iid),)).fetchone()
        if row is None or (kind == "exhibit" and row["project"] == "_trash"):   # slides in the trash are not searchable
            return
        parts = [str(row[c]) for c in (SEARCH_TEXT.get(kind) or ("name", "unit", "geo", "population", "method", "notes"))
                 if c in row.keys() and row[c] not in (None, "")]
        if "document_id" in row.keys() and row["document_id"]:
            d = self.db.execute("SELECT d.title, p.name FROM document d LEFT JOIN publisher p ON p.id = d.publisher_id "
                                "WHERE d.id = ?", (row["document_id"],)).fetchone()
            if d:
                parts += [x for x in d if x]
        elif kind == "series" and row["publisher_id"]:
            p = self.db.execute("SELECT name FROM publisher WHERE id = ?", (row["publisher_id"],)).fetchone()
            if p:
                parts.append(p[0])
        parts += [r[0] for r in self.db.execute("SELECT value FROM tag WHERE item_kind = ? AND item_id = ? AND facet != 'project'",
                                                (kind, iid))]
        self.db.execute("INSERT INTO search_index(item_kind, item_id, body) VALUES (?,?,?)", (kind, iid, " | ".join(parts)))

    # ---------------------------------------------------------------- helpers
    def _touch(self, kind: str, iid) -> None:
        if hasattr(self, "_touched"):
            self._touched.add((kind, str(iid)))

    def _reimport_changes(self, kind: str, item_id: int, data: dict) -> dict:
        """Changes a re-import may apply: fields edited by hand (MCP/CLI, logged in the changelog) stay, an
        internal item never becomes public again, and a verification is never undone by the source."""
        row = self.db.execute(f"SELECT status, visibility FROM {kind} WHERE id = ?", (item_id,)).fetchone()
        curated = set()
        for action, detail in self.db.execute(
                "SELECT action, detail FROM changelog WHERE item_kind = ? AND item_id = ? AND actor NOT LIKE 'importer:%' "
                "AND action IN ('update', 'verify', 'resolve')", (kind, str(item_id))):
            if action == "verify":
                curated.add("status")
            try:
                curated |= set(json.loads(detail or "{}"))
            except ValueError:
                pass
        changes = {k: v for k, v in data.items() if not (k in curated and k in CURATED)}
        if row["visibility"] == "internal" and changes.get("visibility") == "public":
            changes.pop("visibility")
        if row["status"] != "unverified" and changes.get("status") == "unverified":
            changes.pop("status")
        return changes

    def _fit_series_unit(self, kind: str, data: dict) -> None:
        """Bring a value into its series' unit when only the scale differs (USD m -> USD bn), noting the
        original; warn when the units are incompatible."""
        srow = self.db.execute("SELECT unit FROM series WHERE id = ?", (data["series_id"],)).fetchone()
        target = srow[0] if srow else None
        unit = data.get("unit")
        if not target or not unit or unit == target:
            return
        f = conversion(unit, target)
        if f is None:
            self.warnings.append(f"unit '{unit}' differs from the unit of series {data['series_id']} ('{target}')")
            return
        cols = ("value",) if kind == "observation" else ("value", "value_low", "value_high")
        orig = ", ".join(f"{data[c]:g}" for c in cols if isinstance(data.get(c), (int, float)))
        for c in cols:
            if isinstance(data.get(c), (int, float)):
                data[c] = float(f"{data[c] * f:.12g}")
        note = f"Converted from {orig} {unit} to the series unit {target}."
        data["notes"] = f"{data['notes']} {note}" if data.get("notes") else note
        data["unit"] = target

    def _changed_since(self, since: int | None) -> str:
        """SQL condition template restricting a kind to items changed after changelog id `since` (temp table _chg);
        '' for a full run. Series changes pull in their observations/forecasts (unit conversion depends on them)."""
        if since is None:
            return ""
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS _chg(kind TEXT, id TEXT, PRIMARY KEY(kind, id))")
        self.db.execute("DELETE FROM _chg")
        self.db.execute("INSERT OR IGNORE INTO _chg SELECT item_kind, item_id FROM changelog WHERE id > ?", (since,))
        for kind in ("observation", "forecast"):
            self.db.execute(f"INSERT OR IGNORE INTO _chg SELECT '{kind}', CAST(id AS TEXT) FROM {kind} WHERE series_id IN "
                            "(SELECT id FROM _chg WHERE kind = 'series')")
        return " AND CAST({col} AS TEXT) IN (SELECT id FROM _chg WHERE kind = '{kind}')"

    def maintenance_marker(self) -> int | None:
        r = self.db.execute("SELECT value FROM meta WHERE key = 'maintenance:changelog_id'").fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def set_maintenance_marker(self) -> None:
        top = self.db.execute("SELECT coalesce(max(id), 0) FROM changelog").fetchone()[0]
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('maintenance:changelog_id', ?)", (str(top),))

    def normalise(self, since: int | None = None) -> dict:
        """Maintenance for existing data: one unit notation everywhere, values converted into their series'
        unit, region tags from geo, curated publisher types filled. Idempotent. With `since` (a changelog id) only
        items changed after it are looked at – the nightly import uses this so maintenance stays cheap at millions
        of items."""
        counts = {"units": 0, "converted": 0, "publisher_types": 0}
        cond = self._changed_since(since)
        for table in ("series", "observation", "forecast"):
            only = cond.format(col="id", kind=table)
            for key, unit in self.db.execute(f"SELECT id, unit FROM {table} WHERE unit IS NOT NULL{only}").fetchall():
                n = normalise_unit(unit)
                if n != unit:
                    if table == "series":
                        self.db.execute("UPDATE series SET unit = ? WHERE id = ?", (n, key))
                        self._touch("series", key)
                    else:
                        self.update(table, key, {"unit": n})
                    counts["units"] += 1
        for kind in ("observation", "forecast"):
            cols = "value" if kind == "observation" else "value, value_low, value_high"
            only = cond.format(col="id", kind=kind)
            for r in self.db.execute(f"SELECT id, series_id, unit, notes, {cols} FROM {kind} WHERE series_id IS NOT NULL "
                                     f"AND unit IS NOT NULL{only}").fetchall():
                data = dict(r)
                before = dict(data)
                self._fit_series_unit(kind, data)
                if data != before:
                    self.update(kind, r["id"], {k: v for k, v in data.items() if k not in ("id", "series_id")})
                    counts["converted"] += 1
        counts["region_tags"] = 0
        for kind, col in (("observation", "geo"), ("forecast", "geo"), ("fact", "geo"), ("practice", "country"),
                          ("study", "region")):
            only = cond.format(col="id", kind=kind)
            for iid, g in self.db.execute(f"SELECT id, {col} FROM {kind} WHERE {col} IS NOT NULL{only}").fetchall():
                reg = region_slug(g)
                if reg and not self.db.execute("SELECT 1 FROM tag WHERE item_kind = ? AND item_id = ? AND facet = 'region' "
                                               "AND value = ?", (kind, str(iid), reg)).fetchone():
                    self.tag(kind, iid, region=[reg])
                    counts["region_tags"] += 1
        for pid, typ in PUBLISHER_TYPES.items():
            cur = self.db.execute("UPDATE publisher SET type = ? WHERE id = ? AND type IS NULL", (typ, pid))
            counts["publisher_types"] += cur.rowcount
        return counts

    # Words that make two otherwise similar metrics different measurements.
    _DISCRIMINATORS = {"new", "used", "outlet", "specialty", "department", "wholesale", "retail", "online", "offline",
                       "monthly", "weekly", "daily", "yearly", "annual", "gen", "millennials", "boomers", "z", "x",
                       "men", "women", "male", "female", "b2b", "b2c", "min", "max", "minimum", "maximum", "median",
                       "average", "mean", "top", "bottom", "lowest", "highest", "not", "never", "no", "more", "less"}
    _STOP = {"of", "the", "a", "an", "in", "to", "and", "share", "percent", "pct", "by", "for", "with", "that", "who",
             "s", "are", "is", "or", "on", "at", "their", "its"}

    def dedupe(self, dry_run: bool = False, since: int | None = None) -> list[dict]:
        """Fold duplicate observations: same document (URL), period, value, unit, geo, category and visibility;
        one metric wording contained in the other, no distinguishing word or number, compatible populations,
        and not two different defined series. The kept item gets
        the other's tags, origins (marked merged, so re-imports only fill gaps) and missing fields."""
        def toks(t):
            return set(re.findall(r"[a-z0-9]+", (t or "").lower())) - self._STOP

        def ptoks(t):   # population words without sample sizes ("n=537", "~215")
            return {w for w in toks(t) if not w.isdigit() and w != "n"}
        scope = ""
        if since is not None:   # only sources (documents/URLs) that gained or changed an observation since `since`
            self._changed_since(since)
            scope = (" AND coalesce(d.url_key, 'doc:' || o.document_id) IN ("
                     "SELECT coalesce(d2.url_key, 'doc:' || o2.document_id) FROM observation o2 "
                     "JOIN document d2 ON d2.id = o2.document_id "
                     "WHERE CAST(o2.id AS TEXT) IN (SELECT id FROM _chg WHERE kind = 'observation'))")
        rows = self.db.execute(
            "SELECT o.*, coalesce(d.url_key, 'doc:' || o.document_id) AS dkey FROM observation o "
            "JOIN document d ON d.id = o.document_id WHERE o.value IS NOT NULL" + scope + " ORDER BY o.id").fetchall()
        groups: dict[tuple, list] = {}
        for r in rows:
            # population is free text worded differently per research file, so it is not part of the key
            k = (r["dkey"], r["period"], r["value"], r["unit"], (r["geo"] or "").lower(), (r["category"] or "").lower(),
                 r["visibility"])
            groups.setdefault(k, []).append(r)
        rank = {"verified": 0, "unverified": 1, "disputed": 2, "retracted": 3}
        merges, gone = [], set()
        for items in groups.values():
            if len(items) < 2:
                continue
            items = sorted(items, key=lambda r: (r["series_id"] is None, rank.get(r["status"], 9), r["id"]))
            for i, keep in enumerate(items):
                if keep["id"] in gone:
                    continue
                for drop in items[i + 1:]:
                    if drop["id"] in gone:
                        continue
                    if keep["series_id"] and drop["series_id"] and keep["series_id"] != drop["series_id"]:
                        continue
                    a, b = toks(keep["metric"]), toks(drop["metric"])
                    if (a ^ b) & self._DISCRIMINATORS or any(t.isdigit() for t in a ^ b):
                        continue
                    # one wording must contain the other completely ("Dealers using AI" ⊂ "Share of dealers using AI");
                    # a single differing content word ("Nephrology" vs "Cardiology") means a different measurement
                    if not (a <= b or b <= a) or len(a & b) / max(1, len(a | b)) < 0.5:
                        continue
                    pa, pb = ptoks(keep["population"]), ptoks(drop["population"])
                    if pa and pb and not (pa <= pb or pb <= pa):   # "Men" vs "White": different sub-groups
                        continue
                    raw_a, raw_b = (_norm(keep["population"] or ""), _norm(drop["population"] or ""))
                    if raw_a != raw_b and (":" in raw_a + raw_b
                                     or (keep["locator"] and keep["locator"] == drop["locator"]
                                         and re.search(r"\d", keep["locator"]))):
                        continue   # "US adults" vs "US adults: Democrat", or two cells of the same figure

                    merges.append({"keep": keep["id"], "drop": drop["id"], "metric": keep["metric"],
                                   "other": drop["metric"], "period": keep["period"], "value": keep["value"]})
                    gone.add(drop["id"])
                    if not dry_run:
                        self._merge_item("observation", keep["id"], drop["id"])
        return merges

    def _merge_item(self, kind: str, keep: int, drop: int) -> None:
        row_k = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (keep,)).fetchone()
        row_d = self.db.execute(f"SELECT * FROM {kind} WHERE id = ?", (drop,)).fetchone()
        self._fill(kind, "id", keep, row_k, {k: row_d[k] for k in row_d.keys()
                                            if k not in ("id", "ident", "status", "visibility", "created_at", "updated_at")})
        self.db.execute("UPDATE OR IGNORE tag SET item_id = ? WHERE item_kind = ? AND item_id = ?", (str(keep), kind, str(drop)))
        self.db.execute("DELETE FROM tag WHERE item_kind = ? AND item_id = ?", (kind, str(drop)))
        self.db.execute("UPDATE origin SET item_id = ?, merged = 1 WHERE item_kind = ? AND item_id = ?", (str(keep), kind, str(drop)))
        if kind == "observation":
            self.db.execute("UPDATE forecast SET actual_observation_id = ? WHERE actual_observation_id = ?", (keep, drop))
        self.db.execute(f"DELETE FROM {kind} WHERE id = ?", (drop,))
        self.db.execute("DELETE FROM search_index WHERE item_kind = ? AND item_id = ?", (kind, str(drop)))
        self.log(kind, keep, "merge", {"merged": drop})
        self._touch(kind, keep)

    def _touch_document(self, doc_id: int) -> None:
        for kind in ITEM_KINDS:
            for (iid,) in self.db.execute(f"SELECT id FROM {kind} WHERE document_id = ?", (doc_id,)).fetchall():
                self._touch(kind, iid)

    def _fill(self, table: str, keycol: str, key, row: sqlite3.Row, values: dict) -> None:
        fill = {k: v for k, v in values.items() if v not in (None, "") and k in row.keys() and row[k] in (None, "")}
        if table == "document" and values.get("is_vendor") and not row["is_vendor"]:
            fill["is_vendor"] = 1   # column defaults to 0, so 'empty' never applies
        if table == "document" and "url" in fill:
            fill["url_key"] = norm_url(fill["url"])
        if fill:
            self.db.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in fill)} WHERE {keycol} = ?",
                            list(fill.values()) + [key])
            if table == "document":
                self._touch_document(key)
            elif table in ITEM_KINDS:
                self._touch(table, key)

    def _find_identical(self, kind: str, data: dict) -> int | None:
        key = ident_key(kind, data)
        if key is None:
            return None
        r = self.db.execute(f"SELECT id FROM {kind} WHERE ident = ? LIMIT 1", (key,)).fetchone()
        return r[0] if r else None

    def stats(self) -> dict:
        out = {t: self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
               for t in ("publisher", "document", "series") + ITEM_KINDS}
        out["series_with_2plus_periods"] = self.db.execute(
            "SELECT count(*) FROM (SELECT series_id FROM observation WHERE series_id IS NOT NULL "
            "GROUP BY series_id HAVING count(DISTINCT period) >= 2)").fetchone()[0]
        out["by_project"] = dict(self.db.execute("SELECT project, count(*) FROM origin GROUP BY project").fetchall())
        out["exhibits"] = {"logical": self.db.execute("SELECT count(*) FROM exhibit").fetchone()[0],
                           "versions": self.db.execute("SELECT count(*) FROM exhibit_version").fetchone()[0],
                           "sets": self.db.execute("SELECT count(*) FROM exhibit_set").fetchone()[0]}
        out["by_industry"] = dict(self.db.execute("SELECT value, count(*) FROM tag WHERE facet='industry' GROUP BY value "
                                                  "ORDER BY 2 DESC").fetchall())
        return out
