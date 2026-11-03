# Changelog

What changed in each public release of Fundus. Updating an existing installation: AGENTS.md, section 10 (back up,
`git pull`, `pip install -e ".[render]"`, run `fundus stats` once – migrations are additive and automatic).

## 0.2.0 – 3 November 2026

### Optional services: your own language model, Gemini on Vertex AI

- **One place for the model wiring.** All model settings are now keys of the config file (`data/config.json`) or
  `FUNDUS_*` environment variables: `llm_enabled`, `llm_url`, `llm_api`, `llm_model_text`, `llm_model_vision`,
  `llm_model_embed`, `llm_embed_dim`, `llm_num_ctx`, `llm_api_key`. The defaults are unchanged (Ollama on
  `localhost:11434`, `gemma4:26b`, `qwen3-embedding:8b`); the older variables (`FUNDUS_OLLAMA_URL`,
  `FUNDUS_EMBED_MODEL`, `FUNDUS_EMBED_DIM`, `FUNDUS_PHOTO_MODEL`, `FUNDUS_DATASET_MODEL`, `FUNDUS_ENTITY_MODEL`) keep
  working.
- **Any OpenAI-compatible server** (LM Studio, vLLM, llama.cpp server, LocalAI, hosted services) besides Ollama:
  `llm_api = "openai"`. Structured answers use `response_format` with a JSON schema where the server supports it and
  fall back to JSON mode or a prompt instruction with validation; images go as image parts, embeddings through
  `/v1/embeddings`.
- `llm_enabled = false` switches every model call off; search falls back to full text and descriptions wait, as
  without a model.
- **`fundus llm check`** – shows endpoint, API style, models and where each setting comes from; tests that the
  endpoint answers and lists the models, that the text model returns JSON, that the vision model accepts an image, and
  the embedding dimension. It warns when the archive's search space was built with another embedding model and prints
  the re-embedding commands (`fundus embed --model … --dim …`, then `--activate`). `--quick` skips the test requests.
- **Vertex AI configurable** the same way: `vertex_project`, `vertex_location`, `vertex_models`, `vertex_key_file`.
  Credentials: a service-account key, a service-account file in `GOOGLE_APPLICATION_CREDENTIALS`, or Application
  Default Credentials (`gcloud auth application-default login`).
- **`fundus vertex check`** – project, region, kind of credentials (never key contents), model list, and one tiny
  test request (`--no-call` skips it).
- **Installation and update guide:** the installing assistant now asks at installation and at every update whether
  the user wants to connect their own language model (with hardware guidance and model choices) and Gemini on Vertex
  AI (step by step: project, billing and budget alert, API, credentials, region, model, costs, data protection).
  AGENTS.md sections 6 and 10.

### Dataset catalog and archive

- Portal **Datensätze** with three views – catalog, coverage and archive – and a fact sheet per dataset.
- **Usage log** (which session, portal or CLI used a dataset), separate from "seen".
- **Lifecycle:** active, superseded (with successor) and withdrawn, plus a computed "dormant"; searches leave withdrawn
  datasets out unless asked.
- **Reference datasets:** large folders stay where they are (no copy); the description comes from README/Markdown
  files and the file listing only. `fundus datasets locations` reports missing or changed references.
- **Fixity:** checksums of originals and Parquet copies are re-checked in nightly portions; orphaned files are reported
  (deleted only on explicit request).
- **Citation** and BibTeX per dataset, provenance (retrieved at, script), lineage and changes per version; catalog
  endpoint `GET /api/datasets/catalog`.

### Texts with versions (laws, regulations, guidelines, policies)

- A dataset can be a **text with versions**: laws, regulations, directives, guidelines, standards, terms of service or
  provider policies, where each dataset version is one version of the text with its validity period (`valid_from`,
  `valid_to`, version label, publication date, source). The dataset carries the official identifier (ELI, CELEX, SR
  number …), jurisdiction, type of act and issuer. A new version closes the previous open one the day before.
- Ingest only stores and describes: files as delivered (PDF, HTML, XHTML, XML), described by the local model from the
  metadata, title, preamble and table of contents. Nothing is split into articles or compared at ingest – that is
  analysis for a concrete use case.
- `fundus datasets add FILES --text --act-type … --identifier … --jurisdiction … --issuer … --valid-from …`, `datasets at
  REF DATE` (which version was valid), `datasets version REF N …`, MCP `fundus_dataset_add(text=True, …)` and
  `fundus_dataset(action='at')`; catalog facets for jurisdiction and type of act; a versions timeline on the detail page.

### Research registry

- **Hypotheses** with a strict pre-registration: required fields (statement, direction, theory, operationalisation,
  data, method, decision rule), frozen with timestamp and SHA-256; later changes only as append-only deviations.
- **Results** in versions, every verdict on equal footing (supported, not supported, inconclusive …), and
  **interpretations** in seven parts.
- **Findings** – exploratory results with evidence (quotes with speaker role, observations, photos, datasets,
  documents) and a strength level; always marked exploratory.
- Review hints (for example data newer than a result), work list, evidence map; portal area **Forschung**; MCP tools
  `fundus_hypothesis`, `fundus_finding`, `fundus_research_search`, `fundus_research_series`; REST `/api/research…`;
  CLI `fundus research …`.

### Importers and datasets: fixes

- Gzip-compressed CSV/TSV (`.csv.gz`) are read as tables instead of being skipped.
- CSV files with a metadata block before the header (for example central-bank data portals) are recognised.
- Excel sheets with multi-line headers (surveys) get one-line column names.
- `fundus datasets add --internal` now really creates an internal dataset; `--set FIELD=VALUE` and `--next-expected`
  apply directly when adding.

### Other fixes

- The deck view of a slide set shows the whole deck, including slides borrowed from other sets.
- The nightly slide sync never scans Fundus' own storage folders (a received package could otherwise be registered as
  a project).
- Portal writes are retried when another process holds the database lock for a moment.

## 0.1.0 – 2 November 2026 (first public snapshot)

- Evidence archive with sources, time series, forecasts, studies, quotes, practices and facts; full-text, semantic
  and hybrid search; MCP server with the `fundus_*` tools; portal.
- Slide archive with versions, sets, deck orders, locks, copies, trash, archive and favourites; verification with
  feedback inbox, review stages, review by people and by Gemini.
- Datasets (with free-text questions answered by the local model from the descriptions, never from the data), media
  archive (photos and videos), data hygiene with a unit standard.
- **Portability:** Linux, macOS and Windows – in-process job scheduler (`fundus serve`), `fundus render` (Playwright),
  `fundus service install` (systemd, launchd, Task Scheduler), install-specific settings in a config file.
- **Slide packages:** export slide sets as a package file and receive them in another installation (plan, accept per
  slide, reject; "received" and "sent" lists).
- **Fundus Cloud:** optional exchange of slide packages end-to-end encrypted between Fundus installations, with an
  account, sharing dialog and administration in the portal.
