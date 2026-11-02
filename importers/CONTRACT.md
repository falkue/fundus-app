# Importer contract

Every source project gets one module `importers/<name>.py`. Importers only READ their sources and write
through `fundus.Store`, never with raw SQL inserts.

## Module shape

```python
PROJECT = "acme-dealerships"          # project slug, becomes origin.project and a 'project' tag
SOURCE_ROOT = project_dir("Acme-Dealerships", env="FUNDUS_SRC_ACME")   # fundus.layout: new location first, else old

def run(store, root=SOURCE_ROOT) -> dict:
    """Import everything; returns counts per kind, e.g. {"observation": 1606, "quote": 275}."""
    with store.batch(f"importer:{PROJECT}"):
        ...
    return counts

if __name__ == "__main__":
    from importers._cli import main
    main(run)
```

`python -m importers.<name> --db /tmp/fundus-<name>.sqlite` runs it against a scratch database and prints
the counts plus `store.stats()`. Running it twice must not create duplicates (second run: same counts in
the database, no new rows).

## Mapping rules

- **Origin on every item**: `origin=(PROJECT, <file path relative to root>, <stable id inside the file>)`.
  Use the item's own id if it has one, else a stable composite (e.g. series_id + period + category).
  Never use list positions if the source has real ids.
- **Documents**: one `store.document(...)` per source publication (title, url, publisher, kind, published,
  doi, access, is_vendor). Missing title → use the publisher name plus the period, or the URL host.
  Items without any source get no document_id and `status='unverified'`.
- **Publishers**: pass the plain organisation name; for "X (via Y)" the publisher is X and Y goes into
  document notes. Set `type` where obvious (consultancy, vendor, official, academic, media, association,
  analyst, company).
- **Series**: create a series only when a metric is measured repeatedly in a comparable way or is clearly
  meant to be continued (survey with editions). Keep source series ids where they exist (prefix them with
  a short project tag only if they would collide). Put wording changes into `comparability`, and where
  to look for the next edition into `update_hint`. One-off numbers go in as observations without series.
- **Observations**: value as number in `value`; ranges or text in `value_text` (and the midpoint or
  lower bound NOT invented). `period` = what the value refers to (fieldwork year/month, or the year the
  figure describes), `notes` for anything else. Verbatim sentence → `quote_context`.
- **Forecasts**: every statement about a future period (e.g. "by 2030, 40 % of …", "will grow to …",
  scenario values) is a `forecast`, NOT an observation: statement (English), original wording, value /
  value_low / value_high, unit, `target_period`, `made_on` (publication date), scenario, geo; link
  `series_id` when an existing series will show the actual value. Projection rows inside data tables
  (flag like `forecast=1`) also become forecasts.
- **Studies** (academic/empirical with a finding) → `study`; **quotes** → `quote` (English `text`,
  `original` if translated); **practices** (what a named organisation does) → `practice`; regulations,
  tools/vendors, timeline events, frameworks, matrix positions → `fact` with the right `kind` and
  kind-specific fields in `attributes` (dict).
- **Status**: source says verified/checked (1, true, "yes", "verified") → `verified`; explicitly doubtful
  or failed check → `disputed`; else `unverified`. Put the verification note into `notes`.
- **Visibility**: `public` for anything taken from public sources. `internal` for data the user or a
  client collected or provided (own surveys, client figures, interview notes, personal data about
  private individuals). When in doubt: `internal`, and say so in the report.
- **Tags** (`tags={...}` on `store.add`): at least one `industry` and one `topic` per item, `region`
  where the geography is known. Use only slugs from `fundus/vocab.py`; if something really is missing,
  register it once with `store.add_vocab(facet, value, label, parent, description)` at the start of
  `run()` and mention it in the report. Use `cross-industry` + `ai` for evidence about AI in general.
- **Language**: content fields in English as in the sources; keep German sources' original wording in
  `original`/`quote_context`.
- **No invention**: never fill a value, date, URL or name the source does not contain.

## Working copy and running

Run an importer against a scratch database first:

```bash
.venv/bin/python -m importers.<name> --db /tmp/fundus-<name>.sqlite
.venv/bin/python -m pytest -q tests/test_importer_<name>.py
```

Never write `~/Projects/Software/Fundus/data/fundus.sqlite` (the integration run does that). Never modify source
projects. Add `tests/test_importer_<name>.py` with a small fixture copied from the real format (a few
records written by the test into tmp_path) that checks mapping, tags, forecasts and idempotency.
