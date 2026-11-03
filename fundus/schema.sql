-- Fundus: central evidence archive (figures, time series, forecasts, studies, quotes, practices, facts).
-- Every item points to a document (where it was published) and carries tags (industry, topic, region,
-- project) plus an origin record (which project/file it was imported from, for idempotent re-imports).
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Controlled vocabulary for tags. New values are allowed but must be registered here first.
CREATE TABLE IF NOT EXISTS vocab (
  facet       TEXT NOT NULL CHECK (facet IN ('industry','topic','region')),
  value       TEXT NOT NULL,                 -- slug, e.g. 'automotive-retail'
  parent      TEXT,                          -- slug of the broader value within the same facet
  label       TEXT NOT NULL,                 -- human label (English)
  description TEXT,
  PRIMARY KEY (facet, value)
);

CREATE TABLE IF NOT EXISTS publisher (
  id      TEXT PRIMARY KEY,                  -- slug, e.g. 'cdk-global'
  name    TEXT NOT NULL,
  type    TEXT CHECK (type IN ('official','academic','consultancy','analyst','vendor','association',
                               'media','company','ngo','other')),
  country TEXT,                              -- ISO-2 of headquarters
  url     TEXT,
  notes   TEXT
);

-- Alternative names of a publisher ('Pew' -> 'pew-research-center'); used by Store.publisher().
CREATE TABLE IF NOT EXISTS publisher_alias (
  alias        TEXT PRIMARY KEY,              -- publisher_slug() of the alternative name
  publisher_id TEXT NOT NULL                 -- canonical id (may be created later, hence no foreign key)
);

-- One concrete publication: a report edition, article, paper, press release, video, dataset.
CREATE TABLE IF NOT EXISTS document (
  id           INTEGER PRIMARY KEY,
  publisher_id TEXT REFERENCES publisher(id),
  title        TEXT NOT NULL,
  url          TEXT,
  url_key      TEXT,                         -- normalised URL for de-duplication (NULL if no URL)
  doi          TEXT,
  journal      TEXT,                         -- journal / container title (Crossref), the house is publisher_id
  kind         TEXT CHECK (kind IN ('survey_report','report','article','paper','press_release','video',
                                    'podcast','website','dataset','book','interview','other')),
  published    TEXT,                         -- ISO date, month or year precision allowed
  authors      TEXT,
  language     TEXT,
  access       TEXT CHECK (access IN ('open','registration','paywall','client_only','unknown')),
  is_vendor    INTEGER NOT NULL DEFAULT 0,   -- 1 if the publisher sells what it measures
  notes        TEXT,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_document_publisher ON document(publisher_id);
CREATE INDEX IF NOT EXISTS ix_document_url ON document(url_key);

-- A metric measured repeatedly in a comparable way. Observations attach to it over time.
CREATE TABLE IF NOT EXISTS series (
  id            TEXT PRIMARY KEY,            -- slug, e.g. 'cdk-dealer-ai-implemented-or-planned'
  publisher_id  TEXT REFERENCES publisher(id),
  name          TEXT NOT NULL,               -- metric definition in words
  unit          TEXT,
  geo           TEXT,
  population    TEXT,
  frequency     TEXT CHECK (frequency IN ('annual','semiannual','quarterly','monthly','irregular','once')),
  method        TEXT,                        -- survey, register, model, observation, panel ...
  comparability TEXT,                        -- wording changes, breaks, base differences
  update_hint   TEXT,                        -- where and how to look for the next edition
  next_expected TEXT,                        -- ISO month when the next edition is expected
  last_checked  TEXT,                        -- ISO date of the last update check
  notes         TEXT,
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observation (
  id            INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  series_id     TEXT REFERENCES series(id),  -- NULL for one-off data points
  document_id   INTEGER REFERENCES document(id),
  metric        TEXT NOT NULL,               -- what was measured, in words
  category      TEXT,                        -- breakdown: segment, answer option, sub-group
  value         REAL,
  value_text    TEXT,                        -- ranges or non-numeric values ('>50', 'about a third')
  unit          TEXT,
  period        TEXT,                        -- what the value refers to: '2023', '2026-08', '2025-Q2'
  geo           TEXT,
  population    TEXT,
  n             INTEGER,
  method        TEXT,
  locator       TEXT,                        -- page, chart or table in the document
  quote_context TEXT,                        -- verbatim sentence the value was taken from
  notes         TEXT,
  status        TEXT NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility    TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_observation_series ON observation(series_id, period);
CREATE INDEX IF NOT EXISTS ix_observation_document ON observation(document_id);

-- A statement about the future that can later be held against reality.
CREATE TABLE IF NOT EXISTS forecast (
  id                    INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  document_id           INTEGER REFERENCES document(id),
  statement             TEXT NOT NULL,       -- the prediction in words (English)
  original              TEXT,                -- verbatim wording
  forecaster            TEXT,                -- person or organisation if not the publisher
  metric                TEXT,
  value                 REAL,
  value_low             REAL,
  value_high            REAL,
  unit                  TEXT,
  target_period         TEXT,                -- the year/month the prediction is about
  made_on               TEXT,                -- when the prediction was stated
  scenario              TEXT,
  geo                   TEXT,
  series_id             TEXT REFERENCES series(id),  -- series that will show the actual value
  outcome               TEXT NOT NULL DEFAULT 'open'
                        CHECK (outcome IN ('open','due','hit','partly','miss','unresolvable')),
  actual_value          REAL,
  actual_observation_id INTEGER REFERENCES observation(id),
  resolved_at           TEXT,
  resolution_note       TEXT,
  notes                 TEXT,
  status                TEXT NOT NULL DEFAULT 'unverified'
                        CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility            TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            TEXT
);
CREATE INDEX IF NOT EXISTS ix_forecast_target ON forecast(target_period, outcome);

-- An empirical or academic study with its finding.
CREATE TABLE IF NOT EXISTS study (
  id           INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  document_id  INTEGER REFERENCES document(id),
  design       TEXT,                         -- experiment, survey, field study, meta-analysis, case, review ...
  n            INTEGER,
  region       TEXT,
  sector       TEXT,
  intervention TEXT,                         -- what was studied (e.g. the AI application)
  outcome      TEXT,                         -- outcome variable
  effect       TEXT,                         -- direction/size of effect
  moderators   TEXT,
  finding      TEXT NOT NULL,
  citations    INTEGER,
  citations_at TEXT,                         -- ISO date of the citation count
  notes        TEXT,
  status       TEXT NOT NULL DEFAULT 'unverified'
               CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility   TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS quote (
  id           INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  document_id  INTEGER REFERENCES document(id),
  text         TEXT NOT NULL,                -- quote (English; translated if needed)
  original     TEXT,                         -- verbatim in the original language
  language     TEXT,
  speaker      TEXT,
  role         TEXT,
  organisation TEXT,
  said_on      TEXT,
  context      TEXT,
  notes        TEXT,
  status       TEXT NOT NULL DEFAULT 'unverified'
               CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility   TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TEXT
);

-- A concrete example of what an organisation does (case, initiative, best practice).
CREATE TABLE IF NOT EXISTS practice (
  id           INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  document_id  INTEGER REFERENCES document(id),
  organisation TEXT,
  segment      TEXT,
  country      TEXT,
  year         TEXT,
  kind         TEXT,                         -- training, tool rollout, process, org change ...
  what         TEXT NOT NULL,
  scale        TEXT,
  result       TEXT,
  people       TEXT,                         -- who is involved / affected
  quote        TEXT,
  notes        TEXT,
  status       TEXT NOT NULL DEFAULT 'unverified'
               CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility   TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TEXT
);

-- Everything qualitative that is not a quote, study or practice: regulations, tools/products,
-- events on a timeline, definitions, frameworks, assessments.
CREATE TABLE IF NOT EXISTS fact (
  id          INTEGER PRIMARY KEY,
  ident TEXT,                                -- normalised natural identity (Store.ident_key)
  document_id INTEGER REFERENCES document(id),
  kind        TEXT NOT NULL CHECK (kind IN ('regulation','tool','event','definition','framework',
                                            'assessment','organisation','other')),
  title       TEXT NOT NULL,
  what        TEXT,
  date        TEXT,
  geo         TEXT,
  implication TEXT,                          -- why it matters
  attributes  TEXT,                          -- JSON object with kind-specific fields
  notes       TEXT,
  status      TEXT NOT NULL DEFAULT 'unverified'
              CHECK (status IN ('verified','unverified','disputed','retracted')),
  visibility  TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT
);

-- Tags for every item kind. item_id is TEXT so series slugs and integer ids share one table.
CREATE TABLE IF NOT EXISTS tag (
  item_kind TEXT NOT NULL CHECK (item_kind IN ('observation','forecast','study','quote','practice',
                                               'fact','document','series')),
  item_id   TEXT NOT NULL,
  facet     TEXT NOT NULL CHECK (facet IN ('industry','topic','region','project')),
  value     TEXT NOT NULL,
  PRIMARY KEY (item_kind, item_id, facet, value)
);
CREATE INDEX IF NOT EXISTS ix_tag_value ON tag(facet, value);

-- Where an item came from. (project, source_path, source_key) makes imports idempotent.
CREATE TABLE IF NOT EXISTS origin (
  item_kind   TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  project     TEXT NOT NULL,                 -- e.g. 'acme-dealerships', 'fhnw-marketing-studies', 'session'
  source_path TEXT NOT NULL DEFAULT '',      -- file the item was read from
  source_key  TEXT NOT NULL DEFAULT '',      -- id of the item inside that file
  importer    TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project, source_path, source_key, item_kind)
);
CREATE INDEX IF NOT EXISTS ix_origin_item ON origin(item_kind, item_id);

-- Audit trail of changes (status, values, forecast resolutions, update checks).
-- Records merged away by the clean-up (fundus.cleanup): where they live on, so references ('observation:4871' on a slide)
-- keep working. Chains are collapsed when a record is merged again.
CREATE TABLE IF NOT EXISTS item_redirect (
  item_kind  TEXT NOT NULL,
  old_id     TEXT NOT NULL,
  new_id     TEXT NOT NULL,
  run_id     INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (item_kind, old_id)
);

-- Trash (fundus.trash): slides, slide sets and projects deleted by Falk or a session; purged after 30 days or by hand.
CREATE TABLE IF NOT EXISTS trash (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('exhibit','set','project')),
  label      TEXT,                     -- title shown in the portal
  project    TEXT,                     -- project it came from
  ref        TEXT,                     -- project/set/slug, project/set or project
  exhibits   TEXT NOT NULL,            -- JSON list of the slide ids it holds
  origin     TEXT NOT NULL,            -- JSON: where everything was (project, set, slug, set rows, project row)
  removed    TEXT,                     -- JSON: deck, deck-copy and saved-search rows taken out (put back on restore)
  trashed_at TEXT NOT NULL,
  trashed_by TEXT,
  expires_at TEXT NOT NULL,            -- purged automatically after this
  state      TEXT NOT NULL DEFAULT 'trashed' CHECK (state IN ('trashed','restored','purged')),
  done_at    TEXT,
  done_by    TEXT
);
-- Old identity (project, set, slug) of a slide that was moved or trashed: the next render of the same file is added to
-- the moved slide, or skipped while it is in the trash.
CREATE TABLE IF NOT EXISTS exhibit_alias (
  project    TEXT NOT NULL,
  set_id     INTEGER NOT NULL,
  slug       TEXT NOT NULL,
  exhibit_id INTEGER NOT NULL,
  reason     TEXT NOT NULL CHECK (reason IN ('moved','trashed')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (project, set_id, slug)
);
-- Renders deleted for good: the same PNG is not registered again (a changed render is a new slide).
CREATE TABLE IF NOT EXISTS exhibit_tombstone (
  png_sha   TEXT PRIMARY KEY,
  project   TEXT,
  slug      TEXT,
  purged_at TEXT NOT NULL
);

-- Datasets (fundus.datasets): tables and documents stored in Fundus, profiled, described by the local model, queryable.
CREATE TABLE IF NOT EXISTS dataset (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  description     TEXT,                   -- own description (wins over the model's)
  summary_de      TEXT, summary_en TEXT,  -- by the local model
  keywords        TEXT,                   -- JSON list (own + model)
  tags            TEXT,                   -- JSON {industry: [], topic: [], region: []} (Fundus vocabulary)
  source_title    TEXT, publisher TEXT, url TEXT, license TEXT, published TEXT,
  period          TEXT, geo TEXT,         -- coverage
  project         TEXT,
  visibility      TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  note            TEXT,
  current_version INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','described','failed')),
  annotation      TEXT,                   -- JSON: model, unit of observation, column meanings
  error           TEXT,
  described_at    TEXT,
  created_at      TEXT NOT NULL,
  created_by      TEXT,
  updated_at      TEXT NOT NULL,
  removed_at      TEXT,                   -- hidden; purged 30 days later
  frequency       TEXT CHECK (frequency IN ('annual','semiannual','quarterly','monthly','irregular','once')),  -- new editions
  next_expected   TEXT,                   -- ISO month of the next edition
  update_hint     TEXT,                   -- where and how to get the next edition (URL, rebuild script)
  last_checked    TEXT,                   -- ISO date of the last check for a new edition
  kind            TEXT NOT NULL DEFAULT 'data' CHECK (kind IN ('data','crosswalk')),
  crosswalk       TEXT,                   -- JSON (kind crosswalk): from/to systems and versions, validity, quality, method,
                                          -- column roles, pairs found in the table
  code_systems    TEXT,                   -- JSON {"table/column" | "column": "NAICS-2022"} – code system of a column
  table_scope     TEXT NOT NULL DEFAULT 'version' CHECK (table_scope IN ('version','merged')),  -- merged: newest copy of every
                                          -- table across versions (a version that re-delivers only some tables keeps the rest)
  -- catalog & archive (3 Nov 2026, fundus.dataset_catalog)
  lifecycle       TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active','superseded','withdrawn')),
  superseded_by   INTEGER,                -- successor dataset (lifecycle superseded)
  lifecycle_note  TEXT, lifecycle_at TEXT,
  holding         TEXT NOT NULL DEFAULT 'stored' CHECK (holding IN ('stored','reference')),  -- reference: raw data stays
                                          -- where it is (dataset_location), nothing copied, profiled or converted
  retrieved_at    TEXT,                   -- when the data was downloaded/obtained (citation)
  script          TEXT,                   -- script that produced or fetched it (provenance)
  -- texts with versions (3 Nov 2026, fundus.dataset_texts): laws, regulations, guidelines, standards, terms, policies.
  -- A dataset is a text when act_type is set; its versions are the text's versions (Fassungen) with their validity.
  identifier      TEXT,                   -- official identifier: ELI, CELEX, SR number, URL
  jurisdiction    TEXT,                   -- EU, CH, DE, US, … (or 'global' for a provider policy)
  act_type        TEXT CHECK (act_type IN ('law','regulation','directive','ordinance','guideline','standard','terms','policy','other')),
  issuer          TEXT                    -- issuing body (parliament, ministry, standards body, provider)
);
-- lineage: a derived dataset (version) built from input datasets (versions); stale when an input has a newer version
CREATE TABLE IF NOT EXISTS dataset_input (
  dataset_id    INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  input_id      INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  input_version INTEGER NOT NULL,
  note          TEXT,                     -- what is taken from it (tables, join)
  created_at    TEXT NOT NULL,
  PRIMARY KEY (dataset_id, version, input_id)
);
CREATE INDEX IF NOT EXISTS ix_dataset_input ON dataset_input(input_id);
CREATE TABLE IF NOT EXISTS dataset_version (
  dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  n          INTEGER NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT,
  -- a version of a text (dataset.act_type set): its validity and where it was published
  valid_from   TEXT,                    -- ISO date from which this version applies
  valid_to     TEXT,                    -- ISO date of its last day (inclusive); NULL = still valid
  version_label TEXT,                   -- e.g. 'konsolidierte Fassung vom 27.07.2026'
  published_at TEXT,                    -- ISO date of publication (official journal, website)
  source_url   TEXT,                    -- where this version was obtained
  PRIMARY KEY (dataset_id, n)
);
CREATE TABLE IF NOT EXISTS dataset_file (
  id          INTEGER PRIMARY KEY,
  dataset_id  INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('table','document')),
  name        TEXT NOT NULL,              -- file name as imported
  table_name  TEXT,                       -- table: sheet name or file stem
  sha         TEXT NOT NULL, ext TEXT NOT NULL, bytes INTEGER,
  source_path TEXT,
  parquet_sha TEXT,                       -- table: the Parquet copy DuckDB reads
  rows        INTEGER,
  profile     TEXT,                       -- JSON: rows, columns [{name, type, nulls_pct, distinct, min, max, …, examples, top}]
  columns_n   INTEGER,                    -- table: number of columns (cached from the profile, for fast catalog counts)
  verified_at TEXT,                       -- fixity: last re-hash of the original (+ Parquet copy opened)
  verified_ok INTEGER                     -- 1 ok, 0 failed, NULL never checked
);
CREATE INDEX IF NOT EXISTS ix_dataset_file ON dataset_file(dataset_id, version);
-- usage log of datasets (sessions, portal, CLI): show/preview/query/update count as uses, search_hit/ask_hit/portal_view as seen
CREATE TABLE IF NOT EXISTS dataset_use (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dataset_id INTEGER NOT NULL,
  version    INTEGER,
  tool       TEXT NOT NULL,             -- show | preview | query | update | path | search_hit | ask_hit | portal_view
  actor      TEXT,                      -- mcp | portal | cli
  project    TEXT,
  detail     TEXT
);
CREATE INDEX IF NOT EXISTS ix_dataset_use ON dataset_use(dataset_id, ts);
-- where a reference dataset lies (holding 'reference'): a folder or file on the Spark, checked nightly
CREATE TABLE IF NOT EXISTS dataset_location (
  dataset_id   INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  version      INTEGER NOT NULL DEFAULT 1,
  path         TEXT NOT NULL,
  bytes        INTEGER, files INTEGER,
  newest_mtime TEXT,
  checked_at   TEXT,
  status       TEXT,                    -- ok | missing | changed
  manifest     TEXT,                    -- JSON [{name, bytes, mtime}] (at most 2000 entries, then {"more": N})
  PRIMARY KEY (dataset_id, version)
);

CREATE TABLE IF NOT EXISTS changelog (
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  item_kind TEXT NOT NULL,
  item_id   TEXT NOT NULL,
  action    TEXT NOT NULL,                   -- insert, update, verify, resolve, check
  detail    TEXT,                            -- JSON
  actor     TEXT                             -- importer name, 'mcp', 'cli', session label
);

-- Full-text index over all items (rebuilt by Store.reindex()).
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
  item_kind UNINDEXED, item_id UNINDEXED, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS ix_observation_ident ON observation(ident);
CREATE INDEX IF NOT EXISTS ix_forecast_ident ON forecast(ident);
CREATE INDEX IF NOT EXISTS ix_study_ident ON study(ident);
CREATE INDEX IF NOT EXISTS ix_quote_ident ON quote(ident);
CREATE INDEX IF NOT EXISTS ix_practice_ident ON practice(ident);
CREATE INDEX IF NOT EXISTS ix_fact_ident ON fact(ident);

-- MCP tool calls per day (written by the MCP server; read by `fundus metrics` for the dashboard).
CREATE TABLE IF NOT EXISTS usage (
  day   TEXT NOT NULL,                       -- ISO date
  tool  TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, tool)
);

-- Semantic search (fundus/embed.py). An embedding "space" is model + dim + quant; several spaces can exist side
-- by side (e.g. a new model computed in parallel) and meta 'embedding.active' names the one search uses.
-- `vector` holds the full-precision rescoring vector (quantised as `quant`); each space also has a sqlite-vec
-- vec0 table (created at runtime, needs the extension) with a binary-quantised copy for the coarse KNN stage,
-- whose rowid is embedding.id. text_hash identifies the text (+ visibility) the vector was computed from.
CREATE TABLE IF NOT EXISTS embedding (
  id         INTEGER PRIMARY KEY,            -- = rowid in the space's vec0 table
  item_kind  TEXT NOT NULL,
  item_id    TEXT NOT NULL,
  model      TEXT NOT NULL,                  -- e.g. 'qwen3-embedding:8b'
  dim        INTEGER NOT NULL,               -- stored dimensions (Matryoshka-truncated, L2-normalised)
  quant      TEXT NOT NULL DEFAULT 'int8',   -- float32 | float16 | int8 (storage of `vector`)
  text_hash  TEXT NOT NULL,
  vector     BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (item_kind, item_id, model, dim, quant)
);
CREATE INDEX IF NOT EXISTS ix_embedding_space ON embedding(model, dim, quant, updated_at);

CREATE TABLE IF NOT EXISTS embedding_space (
  space      TEXT PRIMARY KEY,               -- 'model|dim|quant'
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  quant      TEXT NOT NULL,
  vec_table  TEXT NOT NULL,                  -- name of the vec0 table (bit[dim], metadata kind + internal)
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Every fundus_search call (MCP and CLI): what was asked, how, what came back (search measurement).
CREATE TABLE IF NOT EXISTS search_log (
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actor     TEXT,                            -- mcp | cli
  query     TEXT,
  mode      TEXT,                            -- requested mode: hybrid | lexical | semantic
  filters   TEXT,                            -- JSON of the non-empty filters
  n_results INTEGER,
  top_ids   TEXT,                            -- JSON list of the first 10 results as "kind:id"
  ms        REAL,
  fallback  TEXT                             -- NULL, or why semantic search was skipped (lexical used instead)
);
CREATE INDEX IF NOT EXISTS ix_search_log_ts ON search_log(ts);

-- Entity layer (fundus/entities.py): organisations, people and products with their aliases and links to items.
-- Entities come from rule seeds (publishers, organisation/speaker fields, organisation/tool facts, acronym definitions)
-- and are enriched by the local LLM (canonical name, variants, abbreviations, parent). enriched_at NULL = not yet seen
-- by the LLM (seeds from acronym definitions stay unused until then).
CREATE TABLE IF NOT EXISTS entity (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('organisation','person','product')),
  name        TEXT NOT NULL,                 -- canonical name
  description TEXT,                          -- persons: role, affiliation
  country     TEXT,                          -- ISO-2 of headquarters
  parent_id   INTEGER REFERENCES entity(id) ON DELETE SET NULL,   -- parent organisation / a person's affiliation
  enriched_at TEXT,                          -- when the LLM enrichment ran (NULL = pending)
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_entity_parent ON entity(parent_id);

-- Every spelling of an entity. alias_norm = entities.norm(alias): case- and diacritic-insensitive tokens.
-- source: name | publisher | quote.organisation | practice.organisation | fact.* | speaker | pattern | abbrev | llm |
-- llm_parent | llm_part | manual; lookup-only (never used for text mentions or query detection): raw | compound |
-- publisher_alias | ambiguous.
CREATE TABLE IF NOT EXISTS entity_alias (
  entity_id  INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  alias      TEXT NOT NULL,
  alias_norm TEXT NOT NULL,
  lang       TEXT,
  source     TEXT,
  PRIMARY KEY (entity_id, alias_norm)
);
CREATE INDEX IF NOT EXISTS ix_entity_alias_norm ON entity_alias(alias_norm);

-- Entity <-> item (item_id TEXT like tag: integer ids and series slugs; exhibits since 2026-10-25, see fundus/exhibits.py). PK serves entity -> items, the index items -> entities.
CREATE TABLE IF NOT EXISTS entity_link (
  entity_id  INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
  item_kind  TEXT NOT NULL CHECK (item_kind IN ('observation','forecast','study','quote','practice','fact','series','exhibit')),
  item_id    TEXT NOT NULL,
  role       TEXT NOT NULL CHECK (role IN ('publisher','organisation','speaker','subject','mentioned','via')),
  source     TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','llm','manual')),
  confidence REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (entity_id, item_kind, item_id, role)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_entity_link_item ON entity_link(item_kind, item_id);

-- Names the LLM judged not to be a named entity (so rule seeding does not bring them back).
CREATE TABLE IF NOT EXISTS entity_rejected (
  alias_norm TEXT PRIMARY KEY,
  alias      TEXT,
  reason     TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Display names of projects. The project slug ('northwind') stays the identity everywhere (tags, origin, exhibits, sets);
-- a row exists only once a project has been given its own name (portal, `fundus project title`).
CREATE TABLE IF NOT EXISTS project (
  slug       TEXT PRIMARY KEY,
  title      TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Slide sets (Foliensätze) of a project: a project can hold several sets; each logical slide belongs to exactly one.
-- Every project has one default set (is_default = 1, slug 'main', folder '') that catches every render not under a more
-- specific set folder. A set owns project-relative folder prefixes (exhibit_set_folder); a render belongs to the set with
-- the longest folder prefix containing its PNG path. sharing: set-level override (NULL: project setting, then policy).
CREATE TABLE IF NOT EXISTS exhibit_set (
  id         INTEGER PRIMARY KEY,
  project    TEXT NOT NULL,
  slug       TEXT NOT NULL,
  title      TEXT NOT NULL,
  note       TEXT,
  sharing    TEXT CHECK (sharing IN ('shared','project')),
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project, slug)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_exhibit_set_default ON exhibit_set(project) WHERE is_default = 1;

-- Folder prefixes of a set, relative to the project root ('decks/board-2027'; '' = the whole project, default set only).
CREATE TABLE IF NOT EXISTS exhibit_set_folder (
  set_id  INTEGER NOT NULL REFERENCES exhibit_set(id) ON DELETE CASCADE,
  project TEXT NOT NULL,
  folder  TEXT NOT NULL,
  PRIMARY KEY (project, folder)
);
CREATE INDEX IF NOT EXISTS ix_exhibit_set_folder_set ON exhibit_set_folder(set_id);

-- Locks for the long-term archive (fundus/locks.py): a locked slide set or slide is frozen – no new version, no move,
-- no new slide in the set, no folder/name change, no new deck order. Copies are free and unlocked. Only Falk unlocks
-- (portal with confirmation, or interactively in the terminal); the row stays as history.
CREATE TABLE IF NOT EXISTS exhibit_lock (
  id          INTEGER PRIMARY KEY,
  target      TEXT NOT NULL CHECK (target IN ('set','exhibit')),
  target_id   INTEGER NOT NULL,
  versions    TEXT,                          -- JSON {exhibit id: version} frozen at lock time
  note        TEXT,
  locked_by   TEXT NOT NULL,
  locked_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  unlocked_by TEXT,
  unlocked_at TEXT,
  unlock_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_exhibit_lock_active ON exhibit_lock(target, target_id) WHERE unlocked_at IS NULL;

-- Exhibits (fundus/exhibits.py): slides, charts, posters and section slides built with the mck-exhibit skill. One row per
-- logical slide (identity: project + slide set + slug, palette variants and deck copies included); every content change
-- (PNG, HTML or generating script) is a new exhibit_version; files are stored content-addressed under
-- data/exhibits/<sha[:2]>/<sha>.<ext>.
-- kind: slide (text/framework), chart (has data charts), poster (not 16:9), section (title/divider/section slides).
-- visibility follows the sharing policy in meta 'exhibit_sharing' (share_everything | internal_default | auto_sources).
-- (Databases created before slide sets are rebuilt by Store._migrate_exhibit_sets; the index on set_id is created there.)
CREATE TABLE IF NOT EXISTS exhibit (
  id              INTEGER PRIMARY KEY,
  project         TEXT NOT NULL,
  set_id          INTEGER NOT NULL REFERENCES exhibit_set(id),   -- slide set (exhibit_set)
  slug            TEXT NOT NULL,             -- file stem without deck order prefix, palette or -vN suffix
  kind            TEXT NOT NULL DEFAULT 'slide' CHECK (kind IN ('slide','chart','poster','section')),
  title           TEXT,                      -- action title
  eyebrow         TEXT,
  meta            TEXT,                      -- meta line under the title
  so_what         TEXT,                      -- conclusion box / so-what
  chart_types     TEXT,                      -- comma-separated (data-mck-chart kinds, 'svg chart', 'bars')
  chart_heads     TEXT,
  labels          TEXT,
  sources         TEXT,                      -- source line items joined with ' · '
  notes           TEXT,
  body_text       TEXT,
  palette         TEXT,                      -- blue | hsg (of the current version)
  language        TEXT,                      -- de | en
  deck            TEXT,                      -- latest deck it was copied into (project-relative folder)
  status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final','presented')),
  released        INTEGER NOT NULL DEFAULT 0,   -- released for sharing outside the project (fundus exhibits share)
  sharing         TEXT CHECK (sharing IN ('shared','project')),   -- slide override (NULL: set, project, then policy)
  sources_public  INTEGER,                   -- 1 all sources public, 0 at least one internal source, NULL no sources
  internal_links  INTEGER NOT NULL DEFAULT 0,   -- linked internal Fundus items / client-only documents
  visibility      TEXT NOT NULL DEFAULT 'public' CHECK (visibility IN ('public','internal')),
  current_version INTEGER,
  created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT,
  archived_at     TEXT,                      -- archive (fundus.archive): findable, not listed in its project/set
  archived_by     TEXT,
  favorite_at     TEXT,                      -- favourite (fundus.favorites): marked in the portal or via MCP
  favorite_by     TEXT,
  global_id       TEXT,                      -- world-unique id (uuid4) for slide packages (fundus.package), set on first export
  UNIQUE (project, set_id, slug)
);

CREATE TABLE IF NOT EXISTS exhibit_version (
  id            INTEGER PRIMARY KEY,
  exhibit_id    INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  n             INTEGER NOT NULL,            -- version number within the exhibit (1, 2, …; ordered by content date)
  png_sha       TEXT NOT NULL,               -- sha256 of the rendered PNG
  html_sha      TEXT,                        -- sha256 of the HTML (or SVG) source, NULL for PNG-only renders
  html_ext      TEXT,                        -- html | svg
  script_sha    TEXT,                        -- sha256 of the generating Python script
  png_path      TEXT,                        -- where the files were found (absolute)
  html_path     TEXT,
  script_path   TEXT,
  width         INTEGER,
  height        INTEGER,
  palette       TEXT,
  kind          TEXT,
  title         TEXT,
  fit           REAL,                        -- body[data-fit] of the render
  density       TEXT,                        -- JSON: verdict (OK/CHECK/SPLIT) + measurements (density.py)
  words         INTEGER,
  extract       TEXT,                        -- JSON of the extracted text (eyebrow, title, meta, chart heads, labels, …)
  created_at    TEXT NOT NULL,               -- content date (file mtime of the PNG)
  registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note          TEXT,
  UNIQUE (exhibit_id, n)
);
CREATE INDEX IF NOT EXISTS ix_exhibit_version_png ON exhibit_version(png_sha);
CREATE INDEX IF NOT EXISTS ix_exhibit_version_html ON exhibit_version(html_sha);
CREATE INDEX IF NOT EXISTS ix_exhibit_version_script ON exhibit_version(script_sha);

-- Deck membership: a numbered copy of an exhibit version in a deck folder (byte-identical copies are memberships, not versions).
CREATE TABLE IF NOT EXISTS exhibit_deck (
  project    TEXT NOT NULL,
  deck       TEXT NOT NULL,                  -- project-relative folder, e.g. 'deck-hsg/main'
  file       TEXT NOT NULL,                  -- file name in the deck ('04-ai-c1-funnel.png')
  deck_path  TEXT,                           -- absolute folder
  position   INTEGER,                        -- order number from the file prefix
  slot       TEXT,                           -- raw prefix ('04', 'B03')
  exhibit_id INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version    INTEGER,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (project, deck, file)
);
CREATE INDEX IF NOT EXISTS ix_exhibit_deck_exhibit ON exhibit_deck(exhibit_id);

-- Ordered decks (Reihenfolgen, fundus/decks.py): the slide order of a deck as the session built it – passed as a manifest
-- (fundus_exhibit_deck, mck.fundus_deck) or read from a deck PDF (pages matched against stored versions). Folder decks
-- with numbered copies stay in exhibit_deck. source: manifest | pdf; a manifest naming a PDF wins over reading it.
CREATE TABLE IF NOT EXISTS exhibit_sequence (
  id         INTEGER PRIMARY KEY,
  project    TEXT NOT NULL,
  name       TEXT NOT NULL,                  -- 'deck-v4' or the PDF file name
  title      TEXT,
  set_id     INTEGER REFERENCES exhibit_set(id) ON DELETE SET NULL,
  source     TEXT NOT NULL CHECK (source IN ('manifest','pdf')),
  pdf_path   TEXT,
  pdf_sha    TEXT,
  pages      INTEGER,
  matched    INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (project, name)
);
CREATE TABLE IF NOT EXISTS exhibit_sequence_item (
  sequence_id INTEGER NOT NULL REFERENCES exhibit_sequence(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,              -- 1, 2, … slide order
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version     INTEGER,
  score       REAL,                          -- pdf: match distance of the page (0 = identical)
  PRIMARY KEY (sequence_id, position)
);
CREATE INDEX IF NOT EXISTS ix_exhibit_sequence_item_exhibit ON exhibit_sequence_item(exhibit_id);
-- PDFs already read (by content), so the nightly scan only looks at new or changed files.
CREATE TABLE IF NOT EXISTS exhibit_pdf_seen (
  pdf_sha    TEXT PRIMARY KEY,
  path       TEXT,
  result     TEXT,                           -- JSON: status deck | not_a_deck | manifest, counts
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Exhibit <-> Fundus item (observation … fact, series, document) or entity. role: data (values shown), source (named in the
-- source line), mentions (organisation/person/product named in the slide text).
CREATE TABLE IF NOT EXISTS exhibit_link (
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  target_kind TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('data','source','mentions')),
  source      TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','manual')),
  evidence    TEXT,                          -- the text that produced the link
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (exhibit_id, target_kind, target_id, role)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_exhibit_link_target ON exhibit_link(target_kind, target_id);

-- How often an exhibit was opened (fundus_exhibit calls, full PNG/HTML fetches) per day – for the dashboard.
CREATE TABLE IF NOT EXISTS exhibit_view (
  day        TEXT NOT NULL,
  exhibit_id INTEGER NOT NULL,
  via        TEXT NOT NULL,                  -- mcp | http
  n          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, exhibit_id, via)
);

-- ------------------------------------------------------------------ reviews (fundus/reviews.py)
-- Reviewers: people Falk invites by link (name chosen when sharing, reused across reviews) and other AI systems
-- (kind 'ai', e.g. Gemini on Vertex AI). Both count as independent checks; Claude sessions do not.
-- Names stay out of search and the evidence archive; deleted_at hides a person from the picker, their checks stay.
CREATE TABLE IF NOT EXISTS reviewer (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL DEFAULT 'person' CHECK (kind IN ('person','ai')),
  last_name  TEXT NOT NULL,
  first_name TEXT NOT NULL DEFAULT '',
  org        TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  deleted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reviewer_name ON reviewer(kind, lower(last_name), lower(first_name));

-- A review: a frozen set of slides (exhibit + version) released for checking, by people (links), by AI (gemini) or both.
-- scope: selection (marked slides) | set (whole slide set) | deck (ordered deck). status: draft (people: waiting for
-- publication to the review app) | open | closed. gemini_state: NULL (no AI check) | queued | running | done | failed.
CREATE TABLE IF NOT EXISTS review (
  id           INTEGER PRIMARY KEY,
  project      TEXT NOT NULL,
  title        TEXT NOT NULL,
  scope        TEXT NOT NULL CHECK (scope IN ('selection','set','deck')),
  set_id       INTEGER REFERENCES exhibit_set(id) ON DELETE SET NULL,
  sequence_id  INTEGER REFERENCES exhibit_sequence(id) ON DELETE SET NULL,
  round        INTEGER NOT NULL DEFAULT 1,
  parent_id    INTEGER REFERENCES review(id) ON DELETE SET NULL,   -- previous round
  status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','open','closed')),
  gemini_state TEXT CHECK (gemini_state IN ('queued','running','done','failed')),
  gemini_model TEXT,                          -- Vertex AI model id chosen for the AI check (fundus/gemini.py)
  gemini_report TEXT,                         -- JSON: progress, tokens, per-slide summary/counts/errors, deck summary
  remote_secret TEXT,                         -- review app: random Storage folder of the slide images (r/<secret>/)
  remote_state  TEXT,                         -- review app: JSON of what was pushed (access hashes, slide signatures, unpublished_at)
  focus        TEXT,                          -- JSON list: sources, numbers, logic, language
  expires_at   TEXT,
  created_by   TEXT NOT NULL DEFAULT 'portal',
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_at TEXT,
  completed_at TEXT,                          -- every invited person has finished (links stay readable until they expire)
  closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_review_project ON review(project);
CREATE TABLE IF NOT EXISTS review_slide (
  review_id  INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  position   INTEGER NOT NULL,
  exhibit_id INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version    INTEGER NOT NULL,               -- the version under review (frozen)
  PRIMARY KEY (review_id, position)
);
CREATE INDEX IF NOT EXISTS ix_review_slide_exhibit ON review_slide(exhibit_id);
-- One link per review and person. The token stays on the Spark (to show the link again); the review app only gets its hash.
CREATE TABLE IF NOT EXISTS review_access (
  review_id   INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  token       TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at  TEXT,
  finished_at TEXT,                           -- the person marked every slide as reviewed: from then on read-only
  PRIMARY KEY (review_id, reviewer_id)
);

-- Feedback inbox: one row per point to work on – a comment, a drawing, a doubtful/wrong check or an AI finding – from a
-- reviewer, an AI or Falk himself (source owner, reviewer_id NULL). Sessions claim (in_progress) and close every row with a
-- comment: incorporated (what changed + resolved_version), answered (why nothing changes), rejected (why), or hand it to
-- Falk (decision). instruction: Falk's guidance when he reopens a point. external_id: id in the review app (idempotent sync).
CREATE TABLE IF NOT EXISTS feedback (
  id               INTEGER PRIMARY KEY,
  review_id        INTEGER REFERENCES review(id) ON DELETE SET NULL,
  exhibit_id       INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version          INTEGER NOT NULL,           -- version the feedback refers to
  source           TEXT NOT NULL CHECK (source IN ('person','ai','owner')),
  reviewer_id      INTEGER REFERENCES reviewer(id),
  kind             TEXT NOT NULL DEFAULT 'comment' CHECK (kind IN ('comment','shape','check')),
  target_kind      TEXT,                       -- check: document | observation … | series
  target_id        TEXT,
  verdict          TEXT CHECK (verdict IN ('ok','doubt','wrong')),
  severity         TEXT CHECK (severity IN ('high','medium','low')),
  weight           TEXT CHECK (weight IN ('error','suggestion')),   -- error blocks the slide's stage; suggestion never does (NULL = error)
  text             TEXT NOT NULL,
  note             TEXT,                       -- e.g. a better link
  shape            TEXT,                       -- JSON: drawing on the slide (relative coordinates)
  external_id      TEXT UNIQUE,
  status           TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','in_progress','incorporated','answered','rejected','decision')),
  claimed_by       TEXT,
  claimed_at       TEXT,
  resolution       TEXT,
  resolved_version INTEGER,
  resolved_by      TEXT,
  resolved_at      TEXT,
  instruction      TEXT,
  remote_state     TEXT,                       -- review app: status|answer|version last pushed (NULL = not in the app)
  created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_feedback_exhibit ON feedback(exhibit_id, status);
CREATE INDEX IF NOT EXISTS ix_feedback_status ON feedback(status);

-- Independent checks of a source (document), data point or number on a slide by a reviewer (person or another AI): latest
-- verdict per reviewer counts. 1 reviewer ok = verified, 2+ = confirmed (bestätigt), any current 'wrong' = disputed.
-- aspect usage = "supports THIS slide" (exhibit_id; counts only for that slide); aspect source = "the source itself is genuine:
-- right publication, edition, working link" (counts everywhere). target_kind number = a row of exhibit_number.
CREATE TABLE IF NOT EXISTS item_check (
  id          INTEGER PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  exhibit_id  INTEGER,                        -- the slide the verdict refers to (usage)
  aspect      TEXT NOT NULL DEFAULT 'usage' CHECK (aspect IN ('usage','source')),
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  verdict     TEXT NOT NULL CHECK (verdict IN ('ok','doubt','wrong')),
  note        TEXT,
  evidence    TEXT,                           -- quote/page the reviewer found
  review_id   INTEGER REFERENCES review(id) ON DELETE SET NULL,
  feedback_id INTEGER REFERENCES feedback(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_item_check_target ON item_check(target_kind, target_id);

-- Reachability of source links, checked before a review goes out.
CREATE TABLE IF NOT EXISTS link_check (
  url        TEXT PRIMARY KEY,
  ok         INTEGER NOT NULL,
  status     INTEGER,
  final_url  TEXT,
  error      TEXT,
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Every number on a slide as an independent reviewer (Gemini) checked it: sourced (found in a source or on the web, with
-- quote and url), derived (computed from other numbers on the slide), wrong, unverified (nowhere to be found – possibly
-- invented). A new check of the same slide in the same review replaces the earlier rows.
CREATE TABLE IF NOT EXISTS exhibit_number (
  id          INTEGER PRIMARY KEY,
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  number      TEXT NOT NULL,
  claim       TEXT,
  verdict     TEXT NOT NULL CHECK (verdict IN ('sourced','derived','wrong','unverified')),
  note        TEXT,
  evidence    TEXT,
  url         TEXT,
  reviewer_id INTEGER REFERENCES reviewer(id),
  review_id   INTEGER REFERENCES review(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_exhibit_number ON exhibit_number(exhibit_id, version);

-- Review progress: which slides a reviewer has gone through ("Folie durchgesehen" in the review app). Done does not mean
-- everything is right – the reviewer is finished with the slide. Pulled by the review sync; the portal shows it per person
-- (percent of the review's slides) and per slide (who is through with it).
CREATE TABLE IF NOT EXISTS review_progress (
  review_id   INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
  reviewer_id INTEGER NOT NULL REFERENCES reviewer(id),
  position    INTEGER NOT NULL,
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  done_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (review_id, reviewer_id, position)
);
CREATE INDEX IF NOT EXISTS ix_review_progress_exhibit ON review_progress(exhibit_id);

-- Which version of a slide an independent reviewer (Gemini) has checked, and with what result. A follow-up check of a new
-- version that confirms every earlier error as fixed (result clean) lifts the slide from 'incorporated' to 'checked'.
CREATE TABLE IF NOT EXISTS exhibit_check (
  id          INTEGER PRIMARY KEY,
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version     INTEGER NOT NULL,
  reviewer_id INTEGER REFERENCES reviewer(id),
  review_id   INTEGER REFERENCES review(id) ON DELETE SET NULL,
  mode        TEXT NOT NULL CHECK (mode IN ('full','followup')),
  result      TEXT NOT NULL CHECK (result IN ('clean','issues')),
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_exhibit_check ON exhibit_check(exhibit_id, version);

-- A Gemini check loop started from a session: Gemini checks, the session works the errors in, Gemini checks the revised
-- slides again – until no slide needs changes or max_rounds is reached. The server's loop ticker drives it.
CREATE TABLE IF NOT EXISTS review_loop (
  id             INTEGER PRIMARY KEY,
  project        TEXT NOT NULL,
  scope          TEXT NOT NULL,              -- JSON {slides | set | deck}
  focus          TEXT,                       -- JSON list
  model          TEXT,
  max_rounds     INTEGER NOT NULL DEFAULT 5,
  round          INTEGER NOT NULL DEFAULT 0,
  state          TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','checking','waiting','done','stopped')),
  reason         TEXT,
  current_review INTEGER REFERENCES review(id) ON DELETE SET NULL,
  exhibits       TEXT,                       -- JSON list of exhibit ids in the loop (from round 1)
  rounds         TEXT,                       -- JSON list of round summaries
  created_by     TEXT,
  created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Fixed verified versions: as soon as a slide's current version reaches 'checked' or 'confirmed' (level >= 2) it is pinned
-- here and stays available even after the slide is developed further and loses the stage. version_id (not n) survives
-- renumbering; a folded duplicate moves its pin to the version it folds into.
CREATE TABLE IF NOT EXISTS exhibit_verified (
  id          INTEGER PRIMARY KEY,
  exhibit_id  INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  version_id  INTEGER NOT NULL REFERENCES exhibit_version(id) ON DELETE CASCADE,
  stage       TEXT NOT NULL CHECK (stage IN ('checked','confirmed')),
  level       INTEGER NOT NULL,
  pinned_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (exhibit_id, version_id)
);

-- Saved slide searches (Foliensuche): a name, the question and filters, the aspects and a fixed selection of slides
-- across projects. Sessions use them by reference ('search:<slug>'), alone or combined with slide sets and decks.
CREATE TABLE IF NOT EXISTS slide_collection (
  id          INTEGER PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  query       TEXT,
  filters     TEXT,                  -- JSON {years, min_confidence, levels, project, kind}
  aspects     TEXT,                  -- JSON [{name, summary}]
  note        TEXT,
  created_by  TEXT,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS slide_collection_item (
  collection_id INTEGER NOT NULL REFERENCES slide_collection(id) ON DELETE CASCADE,
  exhibit_id    INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,
  aspect        TEXT,
  version       INTEGER,             -- the slide's version when it was saved
  added_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (collection_id, exhibit_id)
);

-- Photo archive (fundus/photos.py): selected field-research photos for slides and graphics. Files live content-addressed
-- under data/photos/<sha[:2]>/<sha>.<ext> (the original as imported, plus derived JPEGs without metadata: thumb 400 px,
-- view 1600 px, slide 2400 px). Photos stay on the Spark: only the local LLM annotates them, never a cloud model.
-- Rights: consent of the people shown, the widest use Falk allows, identifiable faces. Sessions only get photos cleared
-- for their purpose (photos.cleared).
CREATE TABLE IF NOT EXISTS photo (
  id            INTEGER PRIMARY KEY,
  sha           TEXT NOT NULL UNIQUE,   -- sha256 of the original image file (JPEG/HEIC/PNG …; for RAW-only shots the RAW)
  ext           TEXT NOT NULL,          -- extension of the stored original
  raw_sha       TEXT,                   -- RAW file of the same shot (stored next to it), if imported with a JPEG/HEIC
  raw_ext       TEXT,
  orig_name     TEXT,                   -- file name at import
  source_path   TEXT,                   -- where it was imported from
  width         INTEGER, height INTEGER,
  orientation   TEXT CHECK (orientation IN ('landscape','portrait','square')),
  taken_at      TEXT,                   -- from the camera (EXIF), local time
  camera        TEXT,
  place         TEXT,                   -- coarse place (city, organisation); GPS is never stored
  dhash         TEXT,                   -- 64-bit difference hash (hex) for near-duplicates and bursts
  title         TEXT,                   -- own short title
  note          TEXT,                   -- own free text (context, what the photo shows, story)
  caption_de    TEXT, caption_en TEXT,  -- description by the LLM
  alt_de        TEXT, alt_en TEXT,      -- short alt text for slides
  interpretation TEXT,                  -- free interpretation by the LLM: what the scene says about people, work, design
  people_count  INTEGER,
  faces         TEXT NOT NULL DEFAULT 'unknown' CHECK (faces IN ('yes','no','unknown')),   -- identifiable faces
  consent       TEXT NOT NULL DEFAULT 'unknown' CHECK (consent IN ('yes','no','unknown')),
  usage         TEXT NOT NULL DEFAULT 'internal' CHECK (usage IN ('internal','teaching','publication')),
  sharing       TEXT NOT NULL DEFAULT 'private' CHECK (sharing IN ('private','team')),  -- Fundus Cloud later, set by Falk
  credit        TEXT,                   -- photo credit for the slide's source line (default: $FUNDUS_PHOTO_CREDIT)
  status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','annotated','confirmed','failed')),
  annotation    TEXT,                   -- JSON: model, at, raw answer extras (text_visible, quality, copy_space …)
  annotated_at  TEXT,
  error         TEXT,
  imported_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  removed_at    TEXT,                   -- hidden (files kept until purged)
  media         TEXT NOT NULL DEFAULT 'photo' CHECK (media IN ('photo','video')),   -- videos: fundus.videos
  duration      REAL,                   -- video length in seconds
  fps           REAL,
  audio         TEXT NOT NULL DEFAULT 'none' CHECK (audio IN ('none','ambient','speech','unknown'))  -- video sound
);
CREATE INDEX IF NOT EXISTS ix_photo_status ON photo(status);

-- Keywords per dimension (setting, activity, role, artefact, phase, method, mood, industry, region, keyword).
-- source 'own' = Falk, 'llm' = the annotation; state 'proposed' (LLM, unchecked), 'confirmed', 'rejected' (LLM proposal
-- Falk turned down – kept so a re-annotation does not bring it back).
CREATE TABLE IF NOT EXISTS photo_tag (
  photo_id   INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
  dim        TEXT NOT NULL,
  value      TEXT NOT NULL,
  source     TEXT NOT NULL CHECK (source IN ('own','llm')),
  state      TEXT NOT NULL CHECK (state IN ('proposed','confirmed','rejected')),
  confidence REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (photo_id, dim, value)
);
CREATE INDEX IF NOT EXISTS ix_photo_tag_value ON photo_tag(dim, value);

-- Groups: 'survey' = one field study / import (project, period, place), 'album' = own selection.
CREATE TABLE IF NOT EXISTS photo_group (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('survey','album')),
  slug        TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL,
  project     TEXT,
  period      TEXT,
  place       TEXT,
  description TEXT,
  created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS photo_group_item (
  group_id  INTEGER NOT NULL REFERENCES photo_group(id) ON DELETE CASCADE,
  photo_id  INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
  added_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (group_id, photo_id)
);

-- Where a photo is used (slides built by sessions).
CREATE TABLE IF NOT EXISTS photo_use (
  photo_id   INTEGER NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
  exhibit_id INTEGER REFERENCES exhibit(id) ON DELETE CASCADE,
  ref        TEXT,                    -- exhibit reference as given (project/set/slug) or another use
  project    TEXT,
  used_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_photo_use ON photo_use(photo_id);

-- Text embedding of each photo's description and keywords (semantic photo search; ~1000 photos: exact search in numpy).
CREATE TABLE IF NOT EXISTS photo_vec (
  photo_id  INTEGER PRIMARY KEY REFERENCES photo(id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  vec       BLOB NOT NULL             -- float32
);

-- ------------------------------------------------------------------ slide packages (fundus/package.py, Fundus Cloud)
-- Packages received from another Fundus installation. The original stays unchanged under <data dir>/received/<id>/ and
-- can be browsed; accepting copies slides into a project of the recipient. status: new | accepted | partly | rejected.
CREATE TABLE IF NOT EXISTS received_package (
  id                 INTEGER PRIMARY KEY,
  package_id         TEXT,                   -- uuid of the package (manifest)
  sender             TEXT,
  sender_fingerprint TEXT,                   -- key fingerprint of the sender (Fundus Cloud), NULL for files
  title              TEXT,
  received_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status             TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','accepted','partly','rejected')),
  manifest           TEXT NOT NULL,          -- JSON manifest as received
  path               TEXT,                   -- folder of the unpacked original
  accepted_project   TEXT,
  accepted_at        TEXT,
  result             TEXT                    -- JSON: slide mapping of the accepts (global id -> own slide), sets created
);
-- Packages sent (or exported for sending): recipients JSON list, transport file | cloud.
CREATE TABLE IF NOT EXISTS sent_package (
  id         INTEGER PRIMARY KEY,
  package_id TEXT,
  title      TEXT,
  recipients TEXT,
  transport  TEXT NOT NULL DEFAULT 'file',
  slides     INTEGER,
  sent_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  manifest   TEXT NOT NULL,
  path       TEXT
);
-- Verification stage a slide had at the sender ("geprüft bei <Absender>"), bound to the content hash of the version
-- (html sha, else png sha). It counts in reviews.slide_status while the current version has that hash, and lapses with
-- the first change. origin = the installation where the check happened.
CREATE TABLE IF NOT EXISTS exhibit_foreign_stage (
  id           INTEGER PRIMARY KEY,
  exhibit_id   INTEGER NOT NULL REFERENCES exhibit(id) ON DELETE CASCADE,
  content_hash TEXT NOT NULL,
  stage        TEXT NOT NULL,
  level        INTEGER,
  reviewers    TEXT,                         -- JSON list of names
  checked_at   TEXT,
  origin       TEXT NOT NULL,
  received_id  INTEGER REFERENCES received_package(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (exhibit_id, content_hash, origin)
);
CREATE INDEX IF NOT EXISTS ix_exhibit_foreign_stage ON exhibit_foreign_stage(exhibit_id);

-- Research registry (fundus.research, 3 Nov 2026): series, hypotheses (strictly pre-registered), findings (exploratory),
-- results, interpretations, evidence and links. Visibility internal by default; a frozen pre-registration and the
-- deviations are append-only (the triggers at the end refuse UPDATE/DELETE).
CREATE TABLE IF NOT EXISTS research_series (
  id            INTEGER PRIMARY KEY,
  slug          TEXT NOT NULL UNIQUE,         -- e.g. serie-8
  title         TEXT NOT NULL,
  programme     TEXT,                         -- research programme / cluster
  project       TEXT,                         -- Fundus project slug
  summary       TEXT,
  proposal_path TEXT,                         -- RESEARCH-PROPOSAL.md / CONCEPT.md
  theory_cards  TEXT,                         -- JSON [{id, name, scholars [{name, works [{title, doi}]}], claim}]
  visibility    TEXT NOT NULL DEFAULT 'internal' CHECK (visibility IN ('public','internal')),
  source        TEXT NOT NULL DEFAULT 'registered',   -- registered | imported:<path>
  created_by    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis (
  id                   INTEGER PRIMARY KEY,
  code                 TEXT NOT NULL,         -- H8.1 (unique per series), generated H<series>.<n> / H-<n>
  series_id            INTEGER REFERENCES research_series(id) ON DELETE SET NULL,
  kind                 TEXT NOT NULL DEFAULT 'quantitative' CHECK (kind IN ('quantitative','qualitative','mixed')),
  title                TEXT,
  statement            TEXT NOT NULL,
  direction            TEXT CHECK (direction IN ('+','−','≠','none')),
  theory_refs          TEXT,                  -- JSON list of theory card ids of the series (or free names)
  status               TEXT NOT NULL DEFAULT 'idea'
                       CHECK (status IN ('idea','preregistered','running','result','revised','withdrawn')),
  visibility           TEXT NOT NULL DEFAULT 'internal' CHECK (visibility IN ('public','internal')),
  published_in         TEXT,                  -- publication (citation / URL); visibility public only on request
  project              TEXT,
  tags                 TEXT,                  -- JSON {industry: [], topic: [], region: []} (Fundus vocabulary)
  aliases              TEXT,                  -- JSON list of other codes (folder name, 'H12' of series 5 …)
  derived_from_finding INTEGER REFERENCES finding(id) ON DELETE SET NULL,
  revises              INTEGER REFERENCES hypothesis(id) ON DELETE SET NULL,
  withdrawn_reason     TEXT,
  source               TEXT NOT NULL DEFAULT 'registered',   -- registered | imported:<path>
  imported_partial     INTEGER NOT NULL DEFAULT 0,           -- only listed in a proposal/summary, no result folder
  meta                 TEXT,                  -- JSON: proposal columns (data, method, threats, feasibility …)
  created_by           TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hypothesis_code ON hypothesis(coalesce(series_id, 0), code);
CREATE TABLE IF NOT EXISTS hypothesis_prereg (
  hypothesis_id   INTEGER PRIMARY KEY REFERENCES hypothesis(id) ON DELETE CASCADE,
  statement       TEXT, direction TEXT, unit TEXT, sample TEXT,
  measures        TEXT,                       -- operationalisation / measures (markdown)
  data            TEXT,                       -- JSON [{ref: 'dataset:N', version, note}] or material (qualitative)
  method          TEXT, controls TEXT,
  decision_rule   TEXT,                       -- supported if … / not supported if … / else inconclusive
  robustness_plan TEXT,
  extra           TEXT,                       -- JSON: further fields (power, placebos, theory, coding frame …)
  text            TEXT,                       -- the pre-registration as written (markdown, imports)
  frozen_at       TEXT,                       -- NULL = draft; set = immutable
  sha256          TEXT,                       -- of the canonical JSON of the frozen fields
  time_source     TEXT,                       -- registered | stamp | heading | unknown (imports without a timestamp)
  source          TEXT NOT NULL DEFAULT 'registered',   -- registered | imported:<file>
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis_deviation (
  id            INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypothesis(id) ON DELETE CASCADE,
  at            TEXT NOT NULL,
  field         TEXT NOT NULL,
  before        TEXT,
  after         TEXT,
  reason        TEXT NOT NULL,
  timing        TEXT NOT NULL CHECK (timing IN ('before_analysis','after_data_seen')),
  by            TEXT
);
CREATE INDEX IF NOT EXISTS ix_hypothesis_deviation ON hypothesis_deviation(hypothesis_id);
CREATE TABLE IF NOT EXISTS hypothesis_result (
  id            INTEGER PRIMARY KEY,
  hypothesis_id INTEGER NOT NULL REFERENCES hypothesis(id) ON DELETE CASCADE,
  verdict       TEXT NOT NULL CHECK (verdict IN ('supported','partly_supported','not_supported','inconclusive')),
  effect        TEXT,
  estimate      REAL, ci_low REAL, ci_high REAL, ci_text TEXT, unit TEXT,
  n             INTEGER, n_text TEXT,
  robustness    TEXT, caveats TEXT,
  method_doc    TEXT,                         -- markdown: model, specification, estimation, diagnostics
  scripts       TEXT,                         -- JSON [{path, sha256}]
  run_log       TEXT, environment TEXT,
  inputs        TEXT,                         -- JSON [{ref: 'dataset:N', version, note}]
  outputs       TEXT,                         -- JSON [refs: dataset:N, exhibit:N]
  measures      TEXT,                         -- JSON (further numbers)
  sample_run    INTEGER NOT NULL DEFAULT 0,   -- 1 = run on a sample / pilot (not the full data)
  partial_data  TEXT,                         -- what was incomplete when it ran
  skipped       INTEGER NOT NULL DEFAULT 0,   -- 1 = not testable at that time (verdict inconclusive)
  at            TEXT NOT NULL,
  by            TEXT,
  source        TEXT NOT NULL DEFAULT 'registered',   -- registered | imported:<file>
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hypothesis_result ON hypothesis_result(hypothesis_id, at);
CREATE TABLE IF NOT EXISTS finding (
  id           INTEGER PRIMARY KEY,
  code         TEXT NOT NULL,                 -- F<series>.<n> / F-<n>
  series_id    INTEGER REFERENCES research_series(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL DEFAULT 'qualitative' CHECK (kind IN ('qualitative','quantitative','mixed')),
  title        TEXT,
  statement    TEXT NOT NULL,
  strength     TEXT CHECK (strength IN ('single_observation','recurring_pattern','saturated','quantified')),
  context      TEXT,                          -- JSON {setting, sample, period}
  method       TEXT,
  detail       TEXT,                          -- markdown (the full note, imports)
  theory_refs  TEXT,
  status       TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('draft','recorded','revised','withdrawn')),
  visibility   TEXT NOT NULL DEFAULT 'internal' CHECK (visibility IN ('public','internal')),
  published_in TEXT,
  project      TEXT,
  tags         TEXT,
  aliases      TEXT,
  exploratory  INTEGER NOT NULL DEFAULT 1 CHECK (exploratory = 1),   -- a finding is never confirmatory
  withdrawn_reason TEXT,
  source       TEXT NOT NULL DEFAULT 'registered',
  created_by   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_finding_code ON finding(coalesce(series_id, 0), code);
CREATE TABLE IF NOT EXISTS interpretation (
  id           INTEGER PRIMARY KEY,
  owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('hypothesis_result','finding')),
  owner_id     INTEGER NOT NULL,
  summary      TEXT, explanation TEXT, alternatives TEXT, limits TEXT, theory TEXT, relations TEXT, next TEXT,
  author       TEXT,
  at           TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'registered'
);
CREATE INDEX IF NOT EXISTS ix_interpretation ON interpretation(owner_kind, owner_id, id);
CREATE TABLE IF NOT EXISTS research_evidence (
  id           INTEGER PRIMARY KEY,
  owner_kind   TEXT NOT NULL CHECK (owner_kind IN ('hypothesis_result','finding')),
  owner_id     INTEGER NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('dataset','observation','document','exhibit','photo','video','quote','note','file')),
  ref          TEXT,                          -- dataset:95, observation:4871, photo:12, a file path …
  version      INTEGER,
  quote        TEXT,
  speaker_role TEXT,                          -- interview quotes: role only, never names of private persons
  note         TEXT,
  created_by   TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_evidence ON research_evidence(owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS ix_research_evidence_ref ON research_evidence(ref);
CREATE TABLE IF NOT EXISTS research_link (
  id         INTEGER PRIMARY KEY,
  from_kind  TEXT NOT NULL CHECK (from_kind IN ('hypothesis','finding')),
  from_id    INTEGER NOT NULL,
  to_kind    TEXT NOT NULL CHECK (to_kind IN ('hypothesis','finding')),
  to_id      INTEGER NOT NULL,
  relation   TEXT NOT NULL CHECK (relation IN ('sharpens','replicates','contradicts','extends','derived_from',
                                               'competes_with','revises')),
  note       TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (from_kind, from_id, to_kind, to_id, relation)
);
CREATE VIRTUAL TABLE IF NOT EXISTS research_index USING fts5(
  item_kind UNINDEXED, item_id UNINDEXED, body, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS tr_prereg_frozen_update BEFORE UPDATE ON hypothesis_prereg
  WHEN OLD.frozen_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'pre-registration is frozen: record a deviation instead'); END;
CREATE TRIGGER IF NOT EXISTS tr_prereg_frozen_delete BEFORE DELETE ON hypothesis_prereg
  WHEN OLD.frozen_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'pre-registration is frozen'); END;
CREATE TRIGGER IF NOT EXISTS tr_deviation_update BEFORE UPDATE ON hypothesis_deviation
  BEGIN SELECT RAISE(ABORT, 'deviations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS tr_deviation_delete BEFORE DELETE ON hypothesis_deviation
  BEGIN SELECT RAISE(ABORT, 'deviations are append-only'); END;
