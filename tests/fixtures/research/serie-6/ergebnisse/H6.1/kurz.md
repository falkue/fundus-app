# H6.1 – Verticality of post-2023 AI-native firms

## Pre-registration (written 2026-11-01 09:42, before estimation)

- Theory: effectuation under abundant means (Sarasvathy 2001): generic AI products push founders to commit early to one
  customer industry. Prediction (+).
- Outcome: vertical = 1 if dataset:100 names one paying customer industry, else 0.
- Sample: firms of batches 2015-2026 with a business function (dataset:100); groups A = AI-native (dataset:47 v3) of
  batches >= 2023, B = non-AI firms of batches 2015-2022.
- Main estimator: logit vertical ~ A + C(function) + C(batch_season), HC1 SE.
- Robustness: (r1) LPM; (r2) include ai_enabled in AI.
- Power: MDE about 6 pp at 80 % power.
- Decision: supported if the main A coefficient CI excludes 0 upwards; not supported if the CI lies below 0; else
  inconclusive.

## Estimates

| model | est | lo | hi |
|---|---|---|---|
| main | -0.10 | -0.30 | 0.10 |

## Verdict

**inconclusive** – log-odds A vs B = -0.10; 95 % CI [-0.30, 0.10]; n = 3000

## Robustness

pre-trend joint p = 0.24

## Identification caveats

Self-descriptions are a snapshot (synthetic fixture text).

## Inputs

dataset:47, dataset:100
