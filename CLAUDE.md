# Fundus – notes for people and AI assistants who develop Fundus

Fundus is an evidence archive (figures, time series, forecasts, studies, quotes, practices, facts – each with its
source) plus a slide archive with verification, served to AI assistants through an MCP server and to people through a
browser portal. This file is for sessions that change Fundus itself. Installing it: `AGENTS.md`.

## Map

| Path | What |
|---|---|
| `fundus/store.py`, `fundus/schema.sql` | SQLite store, schema, additive migrations, change log, full-text index |
| `fundus/query.py`, `fundus/embed.py` | search (full-text core; semantic/hybrid with embeddings of the optional language model) |
| `fundus/config.py`, `fundus/llm.py` | settings (env → config file → defaults), incl. the optional language model (`llm_*`: Ollama or any OpenAI-compatible endpoint, `fundus llm check`) and Vertex AI (`vertex_*`, `fundus vertex check`) |
| `fundus/mcp_server.py` | MCP tools (`fundus_*`), portal static files, REST API (`/api/…`); `TOOLS` lists the tools |
| `fundus/cli.py` | command line (`fundus …` / `python -m fundus.cli …`) |
| `fundus/exhibits.py`, `fundus/locks.py`, `fundus/decks.py` | slide archive: registration, versions, sets, locks, copies, deck orders |
| `fundus/reviews.py`, `fundus/review_loop.py`, `fundus/gemini.py` | reviews, feedback inbox, verification stage per slide, optional AI review |
| `fundus/review_sync.py`, `fundus/firebase.py`, `review-app/` | optional review by people (web app on Firebase) |
| `fundus/datasets.py` | datasets: tables stored with Parquet copies, profiled (DuckDB), described, searchable, read-only SQL |
| `fundus/photos.py`, `fundus/videos.py` | media archive: import, optional local description, rights, search |
| `fundus/archive.py`, `fundus/trash.py`, `fundus/favorites.py` | archive view, trash (30 days) and moving, favourites |
| `fundus/cleanup.py`, `fundus/tidy.py`, `docs/DATA-STANDARD.md` | data hygiene and the unit standard |
| `fundus/slide_search.py`, `fundus/entities.py` | slide search across projects; organisations/people and their name variants |
| `fundus/metrics.py`, `fundus/stats_export.py` | statistics for the portal (`/api/stats`) |
| `importers/` | importers for research files (rules in `importers/CONTRACT.md`) |
| `portal/` | the portal (vanilla JS modules, hash router; `portal/stats/` = statistics view) |
| `deploy/` | service templates |
| `tests/` | pytest; temporary databases, never network, Ollama or cloud services |

## Working rules

- **Tests:** `pip install pytest` once, then `python -m pytest -q` before every commit. Tests use temporary databases
  and must never touch the network, Ollama or cloud services (`FUNDUS_NO_NET=1` is set by the test setup where needed).
  Portal JS: `node --check <file>`; check UI changes in a headless browser at desktop and tablet width, light and dark.
- **Cross-platform:** Fundus should run on Linux, macOS and Windows. Use `pathlib`, no POSIX-only modules without a
  fallback, no hard-coded home paths, hosts or user names – configuration goes through `FUNDUS_*` environment
  variables with sensible local defaults (`127.0.0.1:8010`, `data/` in the repository).
- **Server:** the running server serves portal files straight from disk (no restart for `portal/` changes; bump the
  `?v=` in `portal/index.html` for CSS/JS that browsers cache). Python changes need a server restart.
- **Database:** the live database is the user's data. Before a migration make a backup with
  `sqlite3 data/fundus.sqlite ".backup data/backup-before-<what>.sqlite"` (never `cp` while the server runs, WAL mode).
  Migrations are additive.
- **Never commit data:** `data/`, datasets, slides, photos, exports and keys stay out of git (`.gitignore`). Test
  fixtures are written by the tests into a temporary folder.
- **Secrets:** never read, print or commit service-account keys or tokens.
- **Models:** every model call goes through the wiring in `fundus/config.py` / `fundus/llm.py` – no hard-coded model
  names or URLs in features; a missing or switched-off model must degrade gracefully (full-text search, no description).
- **Data rules:** non-public data only with `visibility: internal`; no personal data of private persons in the archive.
  Assistants never mark sources or data points as verified – independent reviews (people, an AI reviewer) do.
- **Style:** code comments and docs in English; the portal UI is German at the moment (translations welcome). Match
  the surrounding code.
