# Fundus

**An evidence and slide archive for research work, with an MCP server for AI assistants.**

Fundus keeps what research produces so that it is not repeated in every project: figures and time series,
forecasts, studies, quotes, practice examples and qualitative facts – **each with its source** – plus the slides and
charts built from them, with versions, decks and a verification stage. AI coding assistants (Claude Code, Codex,
Gemini CLI, Cursor …) reach it through an MCP server: they look in Fundus first, reuse what is there with its source,
and write new, sourced findings back. A browser portal shows the archive to people.

Everything runs locally on your machine: Python and SQLite are enough. Two **optional services** can be connected –
nobody has to:

- **Your own language model** (Ollama, LM Studio, vLLM, a llama.cpp server or any OpenAI-compatible endpoint) adds
  meaning-based, cross-lingual search, descriptions of datasets and photos, aspect groups in the slide search and
  free-text questions about your datasets. You choose the models; `fundus llm check` tests the connection.
- **Gemini on Vertex AI** (Google Cloud, paid by use) reviews slides as an independent AI reviewer – numbers against
  sources, logic, language. `fundus vertex check` tests the setup.

The installing assistant asks about both at installation and at every update (AGENTS.md, section 6).

> **Status:** Fundus was built and is used daily on Linux (Ubuntu). **macOS and Windows are untested so far.** The
> installation guide covers them step by step, but expect rough edges – issues and pull requests are welcome.

## Features

- **Evidence archive** – observations (numbers with unit, period, geography), time series that grow edition by edition,
  forecasts that are later held against reality, studies, quotes, practices and facts; every item points to a document
  with URL, publisher and date. Public and internal visibility per item.
- **Search** – full-text search out of the box; with a language model also semantic and hybrid search across
  languages (a German question finds English evidence).
- **MCP server** – about forty `fundus_*` tools over streamable HTTP: search, overview by industry/topic, series,
  add/update, forecasts, slides, feedback, datasets, photos.
- **Slide archive** – slides and charts (HTML + PNG) with versions, slide sets, deck orders, copies, archive locks,
  trash, favourites and the sources and data points each slide uses.
- **Verification** – a feedback inbox per slide, review stages from "unchecked" to "confirmed", optional review by
  people (Fundus Review web app) and by an AI reviewer.
- **Datasets** – CSV (also `.csv.gz`), Excel, JSON and Parquet tables stored with Parquet copies, profiled with
  DuckDB, described and searchable, queried read-only with SQL; a catalog with coverage and archive views, usage log,
  lifecycle (active, superseded, withdrawn), reference datasets that stay in place, fixity checks and citations.
- **Research registry** – hypotheses with a frozen pre-registration (timestamp and hash; changes only as recorded
  deviations), results with every verdict, interpretations, and exploratory findings with their evidence.
- **Media archive** – photos and videos (HEIC, RAW, video via ffmpeg) with rights per item; optional local description
  with a vision model.
- **Slide packages and Fundus Cloud** – send slide sets to other Fundus installations as a package file or end-to-end
  encrypted through the optional Fundus Cloud; received sets are reviewed before they enter the archive.
- **Data hygiene** – duplicates, spelling variants, contradictions and a unit standard, with undo.
- **Portal** – a browser UI for slides, sets, decks, reviews, datasets, photos and statistics.
- **Command line** – `fundus …` for everything the tools do, for scripts and timers.

No screenshots here on purpose: a screenshot of a real archive would show its contents.

## Quick start

```bash
git clone https://github.com/falkue/fundus-app.git fundus
cd fundus
python3 -m venv .venv
.venv/bin/pip install -e ".[render]"
.venv/bin/python -m playwright install chromium-headless-shell
.venv/bin/fundus stats                 # creates an empty archive and prints counts
.venv/bin/fundus serve                 # MCP server + portal on http://127.0.0.1:8010
```

Then open <http://127.0.0.1:8010/portal/> and register the MCP server in your assistant, e.g. Claude Code:

```bash
claude mcp add --transport http fundus http://127.0.0.1:8010/mcp
```

On Windows use `.venv\Scripts\` instead of `.venv/bin/`. The full, verified procedure for Linux, macOS and Windows –
including autostart, local models and troubleshooting – is in **[AGENTS.md](AGENTS.md)**. The easiest way to install is
to ask your AI coding assistant: *"Install Fundus by following AGENTS.md in this repository."*

## Documentation

| File | For |
|---|---|
| [AGENTS.md](AGENTS.md) | Installation instructions for AI coding assistants (step by step, per OS, with checks) |
| [INSTALL.md](INSTALL.md) | Short installation overview for people |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each release |
| [CLAUDE.md](CLAUDE.md) | Orientation for people and assistants who develop Fundus |
| [docs/DATA-STANDARD.md](docs/DATA-STANDARD.md) | How values and units are written |
| [importers/CONTRACT.md](importers/CONTRACT.md) | How to write an importer for your own research files |

## Your data stays yours

The repository contains code only – no slides, datasets, photos or evidence. Your archive lives in the data directory
(`data/` in the clone by default, or `$FUNDUS_DB`), which is ignored by git. Never commit or upload it.

## Licence

See [LICENSE](LICENSE).
