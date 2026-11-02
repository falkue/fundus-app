"""Forecast follow-up: find due forecasts, propose an outcome from the linked series, resolve.

- `due(store, today)` marks open forecasts whose target period has ENDED as 'due' and returns all
  open/due forecasts whose target period is over. Range targets count by their end ('2026-2030' → 2030).
- `evaluate(store, forecast)` proposes an outcome from the linked series' observation for the target
  period. Only observations visible for the forecast are used (public ones; for an internal forecast
  also internal ones of the same project). Proposals are never written unless `review(..., apply=True)`
  or `resolve()` is called explicitly, and review() only applies clean proposals (`auto_apply`).

Matching the actual value (in this order)
- exact: same granularity and same period (annual value for a year target, Q4 value for a Q4 target) → auto_apply
- sub_period: a finer value that ends with the target (Dec or Q4 value for a year target) – valid only if the
  metric is a level, not a flow → proposal only
- covering_period: a coarser value containing the target (annual value for a month target) → proposal only
- nearest_later_period: same granularity, first period after the target → proposal only
Unit mismatches and values that exist only for a category (no headline value) are flagged as well.

Scoring rules
- value_low / value_high given: hit if the actual lies inside the range (a missing bound is open), else miss.
- point value: relative deviation ≤ 10 % → hit, ≤ 25 % → partly, else miss. For percentage units the
  deviation is measured in percentage points instead: ≤ 2 points → hit, ≤ 5 points → partly.
"""
from __future__ import annotations

from datetime import date

from fundus.query import GRAN_ORDER, _period_key, clip, is_visible, parse_period
from fundus.store import Store

OUTCOMES = ("hit", "partly", "miss", "unresolvable", "open", "due")
PCT_UNITS = {"%", "percent", "pct", "percentage", "percentage points", "pp", "% points", "%-points"}


def _is_pct(unit) -> bool:
    u = (unit or "").strip().lower()
    return u in PCT_UNITS or u.startswith("%") or u.startswith("percent")


def _row(store: Store, forecast):
    if isinstance(forecast, (int, str)):
        r = store.db.execute("SELECT * FROM forecast WHERE id = ?", (int(forecast),)).fetchone()
        if r is None:
            raise LookupError(f"forecast {forecast} not found")
        return r
    return forecast


def reached(target_period, today: date) -> bool:
    """True once the target period is over (its last day lies before today)."""
    p = parse_period(target_period)
    return p is not None and p.end < today


def due(store: Store, today: date | None = None, mark: bool = True, actor: str = "forecasts") -> list[dict]:
    """Open forecasts whose target period is over are marked 'due' (if `mark`). Returns every open or
    due forecast whose target period is over, soonest target first."""
    today = today or date.today()
    rows = store.db.execute("SELECT * FROM forecast WHERE outcome IN ('open','due')").fetchall()
    hit = [r for r in rows if reached(r["target_period"], today)]
    if mark and any(r["outcome"] == "open" for r in hit):
        with store.batch(actor):
            for r in hit:
                if r["outcome"] == "open":
                    store.update("forecast", r["id"], {"outcome": "due"})
    hit.sort(key=lambda r: _period_key(r["target_period"]))
    ids = [r["id"] for r in hit]
    if not ids:
        return []
    fresh = {r["id"]: dict(r) for r in store.db.execute(
        f"SELECT * FROM forecast WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return [fresh[i] for i in ids]


def _projects(store: Store, kind: str, item_id) -> set[str]:
    return {v for (v,) in store.db.execute("SELECT value FROM tag WHERE item_kind = ? AND item_id = ? AND facet = 'project'",
                                           (kind, str(item_id)))}


def usable_for(store: Store, f, o) -> bool:
    """May observation o serve as the actual of forecast f? Public observations always; internal ones only
    for an internal forecast of the same project."""
    if o["visibility"] != "internal":
        return True
    if f["visibility"] != "internal":
        return False
    return bool(_projects(store, "forecast", f["id"]) & _projects(store, "observation", o["id"]))


def _actual(store: Store, f) -> tuple[dict, list[str]] | None:
    """(observation, flags) for the forecast's target period, or None when no usable actual exists."""
    if not f["series_id"] or not f["target_period"]:
        return None
    t = parse_period(f["target_period"])
    if t is None:
        return None
    rows = store.db.execute("SELECT * FROM observation WHERE series_id = ? AND value IS NOT NULL AND status != 'retracted'",
                            (f["series_id"],)).fetchall()
    obs = [(o, parse_period(o["period"])) for o in rows if usable_for(store, f, o)]
    obs = [(o, p) for o, p in obs if p is not None]
    tg = GRAN_ORDER[t.gran]
    flags: list[str] = []
    chosen = [o for o, p in obs if p.gran == t.gran and p.end == t.end]
    if not chosen:
        sub = [(o, p) for o, p in obs if GRAN_ORDER[p.gran] < tg and p.end == t.end]
        cover = [(o, p) for o, p in obs if GRAN_ORDER[p.gran] > tg and p.start <= t.last_start and p.end >= t.end]
        later = [(o, p) for o, p in obs if p.gran == t.gran and p.start > t.end]
        if sub:
            g = max(GRAN_ORDER[p.gran] for _, p in sub)
            chosen = [o for o, p in sub if GRAN_ORDER[p.gran] == g]
            flags.append(f"sub_period: value for {chosen[0]['period']} used for target {f['target_period']} "
                         f"(only valid if the metric is a level/stock, not a flow)")
        elif cover:
            g = min(GRAN_ORDER[p.gran] for _, p in cover)
            chosen = [o for o, p in cover if GRAN_ORDER[p.gran] == g]
            flags.append(f"covering_period: value for {chosen[0]['period']} covers target {f['target_period']}")
        elif later:
            first = min(p.start for _, p in later)
            chosen = [o for o, p in later if p.start == first]
            flags.append(f"nearest_later_period: no value for {f['target_period']}; using {chosen[0]['period']}")
        else:
            return None

    def pref(o):   # headline value first: no category, same geo, verified, newest row
        return (o["category"] not in (None, ""), (f["geo"] or "") != (o["geo"] or "") if f["geo"] else False,
                o["status"] != "verified", -o["id"])
    o = dict(sorted(chosen, key=pref)[0])
    if o["category"] not in (None, ""):
        flags.append(f"category_only: value is for category '{clip(o['category'], 80)}', no headline value")
    if f["unit"] and o["unit"] and f["unit"].strip().lower() != o["unit"].strip().lower():
        flags.append(f"unit_mismatch: forecast in {f['unit']}, observation in {o['unit']}")
    return o, flags


def score(f, actual: float) -> tuple[str, str]:
    """Outcome for an actual value; returns (outcome, explanation)."""
    lo, hi, v = f["value_low"], f["value_high"], f["value"]
    if lo is not None or hi is not None:
        inside = (lo is None or actual >= lo) and (hi is None or actual <= hi)
        return ("hit" if inside else "miss"), f"range [{lo if lo is not None else '-inf'}, {hi if hi is not None else 'inf'}]"
    if v is None:
        return "unresolvable", "forecast has no numeric value"
    if _is_pct(f["unit"]):
        d = abs(actual - v)
        return ("hit" if d <= 2 else "partly" if d <= 5 else "miss"), f"deviation {d:.2f} percentage points"
    d = abs(actual - v) / abs(v) if v else (0.0 if actual == v else float("inf"))
    return ("hit" if d <= 0.10 else "partly" if d <= 0.25 else "miss"), f"relative deviation {d:.1%}"


def evaluate(store: Store, forecast) -> dict | None:
    """Proposal {forecast_id, outcome, actual_value, actual_observation_id, actual_period, note, auto_apply,
    flags} or None when no usable actual value is available yet. auto_apply is False whenever a flag is set."""
    f = _row(store, forecast)
    found = _actual(store, f)
    if found is None:
        return None
    o, flags = found
    outcome, why = score(f, o["value"])
    note = f"actual {o['value']:g}{(' ' + o['unit']) if o['unit'] else ''} for {o['period']} (observation {o['id']}); {why}"
    if flags:
        note += "; NEEDS REVIEW: " + "; ".join(flags)
    out = {"forecast_id": f["id"], "outcome": outcome, "actual_value": o["value"], "actual_observation_id": o["id"],
           "actual_period": o["period"], "note": note, "auto_apply": not flags}
    if flags:
        out["flags"] = flags
    return out


def resolve(store: Store, forecast_id: int, outcome: str, actual_value: float | None = None,
            observation_id: int | None = None, note: str | None = None, actor: str = "cli",
            today: date | None = None, include_internal: bool = False, project: str | None = None) -> dict:
    """Write an outcome through Store.update and log action 'resolve'. Reopening (open/due) clears the
    actual value, observation, note and resolution date. Internal forecasts need include_internal or their
    project; an internal observation can never resolve a public forecast. Returns the changed fields."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    f = _row(store, forecast_id)
    if not is_visible(store, "forecast", f, include_internal, project):
        raise PermissionError(f"forecast {f['id']} is internal; pass include_internal=true (or its project)")
    reopen = outcome in ("open", "due")
    if reopen:
        changes = {"outcome": outcome, "actual_value": None, "actual_observation_id": None,
                   "resolution_note": None, "resolved_at": None}
    else:
        if observation_id is not None:
            o = store.db.execute("SELECT * FROM observation WHERE id = ?", (int(observation_id),)).fetchone()
            if o is None:
                raise LookupError(f"observation {observation_id} not found")
            if not usable_for(store, f, o):
                raise PermissionError(f"observation {observation_id} is internal and cannot resolve this "
                                      f"{f['visibility']} forecast")
            if actual_value is None:
                actual_value = o["value"]
        changes = {"outcome": outcome, "resolved_at": (today or date.today()).isoformat()}
        if note:
            changes["resolution_note"] = note
        if actual_value is not None:
            changes["actual_value"] = float(actual_value)
        if observation_id is not None:
            changes["actual_observation_id"] = int(observation_id)
    with store.batch(actor):
        diff = store.update("forecast", f["id"], changes)
        store.log("forecast", f["id"], "resolve", {"outcome": outcome, "actual_value": actual_value,
                                                   "observation_id": observation_id, "note": clip(note, 500)})
    return {k: v[1] for k, v in diff.items()}


def review(store: Store, today: date | None = None, apply: bool = False, actor: str = "cli") -> list[dict]:
    """due() + evaluate() for each due forecast. With apply=True only clean proposals (auto_apply, i.e. exact
    period, same unit, headline value) are written via resolve(); flagged ones stay proposals."""
    out = []
    for f in due(store, today, mark=True, actor=actor):
        p = evaluate(store, f["id"])
        entry = {"forecast": {k: f[k] for k in ("id", "statement", "value", "value_low", "value_high", "unit",
                                                "target_period", "series_id", "outcome", "visibility")
                              if f.get(k) is not None},
                 "proposal": p}
        entry["forecast"]["statement"] = clip(entry["forecast"]["statement"])
        if apply and p:
            if p["auto_apply"]:
                resolve(store, f["id"], p["outcome"], p["actual_value"], p["actual_observation_id"],
                        "auto: " + p["note"], actor=actor, today=today, include_internal=True)
                entry["applied"] = True
            else:
                entry["applied"] = False
                entry["reason"] = "flagged proposal – resolve manually after review"
        out.append(entry)
    return out
