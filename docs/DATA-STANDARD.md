# Fundus data standard

How values are written in Fundus, and how the tidy procedure (`fundus tidy`, skill `fundus-tidy`) keeps them that way.
Agreed with Falk on 1 Nov 2026: data hygiene runs fully automatically, and Claude decides the cases that need
judgement. Rules live in `fundus/tidy.py` (units) and `fundus/cleanup.py` (duplicates, variants, contradictions);
this file explains them. Change the rules there, then update this file.

## 1. Units

The unit names **what is measured**, not who was asked and not how sure the figure is.

| Kind | Standard | Examples of what is converted |
|---|---|---|
| Share | `%` | `% of respondents`, `% of companies`, `% of CEOs` → `%` (the counted group goes to *population*) |
| Share of a denominator | `% of <denominator>` | stays as is: `% of GDP`, `% of revenue`, `% of marketing budget`, `% of working time` |
| Percentage points | `pp` | `percentage points`, `pp balance`, `% (percentage points as stated)` |
| Change rates | `% change`, `% YoY`, `% CAGR`, `% per year` | `% growth`, `% yoy`, `% per year (CAGR)` |
| Money | `<ISO> [k\|m\|bn\|tn]` | `$ bn` → `USD bn`, `TCHF` → `CHF k`, `EUR million` → `EUR m` |
| Money with price base | `USD bn (2019 dollars)` | the base year stays in the unit |
| Index | `index (<base> = 100)` | `index, 2016 = 100` → `index (2016 = 100)` |
| Score | `score (a–b)` (en dash) | `score (1-7)`, `mean score (1–7)`, `points (0-100)`, `score/100` |
| Rates per time | `<unit> per <period>` | `minutes/day` → `minutes per day`, `USD/month` → `USD per month` |
| Multiples | `x` | `multiple`, `x (fold)`, `times as likely` |
| Fractions | `fraction (0–1)` | `fraction (0-1)` |
| Counts | the counted noun, or `count` | `count vehicles` → `vehicles` |

**Qualifiers** such as `(approx.)`, `(about)`, `(at least)`, `(more than)`, `(upper bound)`, `(range 3-5)`, `(31 sure + 51 suspect)`
or `(forecast)` are not part of the unit. The unit loses them, and the item's notes keep the original spelling (`Unit as given: …`).

**Not converted automatically:** compound units that still read as text after cleaning (`% -> 12.3% (2021 -> 2024)`,
`… ; >50,000 participants`). They stay as they are for a look by hand.

## 2. Population

The group the figure is about (`US marketers, n=537`, `respondents`, `hospitals`). When a unit `% of <group>` is
converted and the item has no population yet, `<group>` becomes the population.

## 3. Geography and periods

- Geography: country codes (ISO 3166-1 alpha-2, `UK` for the United Kingdom), `EU`, `WORLD`, `DACH` and similar groups
  (`fundus/geo.py`, `canonical_geo`). The writer converts on every write.
- Periods: `YYYY`, `YYYY-MM`, `YYYY-Qn`, `YYYY-Hn`, ranges as `YYYY-MM/YYYY-MM`.

## 4. Duplicates, variants, contradictions (`fundus/cleanup.py`)

| Class | What | Who decides |
|---|---|---|
| variant | spelling variants (units, geo, spaces) | automatic |
| safe | identical data point of the same source, or one wording contained in the other | automatic |
| orphan | publishers without documents or series | automatic |
| probable | same value of the same source with similar wording; series with the same definition; publishers that differ only by legal form | Claude (`fundus tidy review` / `decide`) |
| contradiction | same source, metric, period, region, category and figure, but different values | Claude |

Rules that keep false alarms out:
- Categories are compared with their symbols kept (`≥ USD 1bn` ≠ `< USD 1bn`, `C#` ≠ `C++`).
- Values of the same metric from two different figures or tables of one report count as a contradiction only within one figure.
- Empty series that are edition trackers (update_hint, next_expected, frequency, or "tracker" in the name) are not deletion candidates.

**How Claude decides** (each decision has a `why`, which the run records):
- **merge:** the same figure from the same source, worded differently. Keep the verified record or the one in a series.
  Publishers that differ only by legal form (`SAP` / `SAP SE`) also count. So does one series definition stored twice.
- **reject** ("no duplicate", never proposed again): the value is equal by chance, but the subjects differ (other
  product, platform, segment, composite or answer option).
- **dispute:** a real contradiction that cannot be resolved without the source. Unverified records become `disputed`.
- **later:** genuinely unclear. Leave it with a reason, and report it to Falk.

Every run is journalled (`cleanup_run`, `cleanup_journal`). `fundus cleanup undo RUN` restores the rows. Merged ids
redirect, so slide references keep working. The automatic backups `data/backup-before-cleanup-*.sqlite` keep the
newest three.

## 5. Datasets

See README "Datasets" and "Linked data": `code_systems` for code columns (`NAICS-2022`, `SIC-SEC`, `ISO3166`),
`kind='crosswalk'` for mapping tables, `inputs` for derived datasets.
