"""Search measurement: run a judged query set through each search mode and report retrieval quality.

    python -m fundus.cli eval-search [--file eval/search_eval.json] [--modes lexical,semantic,hybrid] [--json]
    python -m fundus.cli eval-search --unjudged      # top results without a judgment (for the next pooling pass)

File format (see eval/README.md):
    {"queries": [{"id": "q01", "lang": "de", "text": "…", "filters": {"industry": "healthcare"}, "intent": "…",
                  "relevant": {"study:12": 2, "observation:345": 1, "quote:7": 0},
                  "nonrelevant": ["quote:8", "fact:3"]}]}
Grades: 2 = directly answers the query, 1 = relevant context, 0 = judged not relevant. `nonrelevant` is a list of
items judged not relevant (equivalent to grade 0, but kept out of `relevant`); both keep an item out of --unjudged.

Modes: lexical | semantic | hybrid (with the entity ranking) | hybrid-noent (hybrid without it, for comparison).
Metrics per mode, overall, per language and per intent (quality metrics over queries with at least one grade ≥ 1):
    Recall@5, Recall@10 (share of the relevant items found), MRR@k (1 / rank of the first relevant hit),
    nDCG@k (gain 2^grade − 1), zero-hit rate and mean/p95 latency (over all queries), fallbacks.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from fundus import query as q
from fundus.store import Store

FILTER_KEYS = {"kind", "industry", "topic", "region", "since", "status", "project", "include_internal", "visible_project"}


def load(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = data.get("queries")
    if not isinstance(queries, list):
        raise ValueError(f"{path}: expected {{\"queries\": [...]}}")
    for i, qu in enumerate(queries):
        if not qu.get("text") or not qu.get("id"):
            raise ValueError(f"{path}: query #{i} needs 'id' and 'text'")
        bad = set(qu.get("filters") or {}) - FILTER_KEYS
        if bad:
            raise ValueError(f"{path}: query {qu['id']} has unknown filters {sorted(bad)}")
        for key, g in (qu.get("relevant") or {}).items():
            if ":" not in key or g not in (0, 1, 2):
                raise ValueError(f"{path}: query {qu['id']}: judgment {key!r}: {g!r} (want 'kind:id': 0|1|2)")
        nonrel = qu.get("nonrelevant") or []
        if not isinstance(nonrel, list) or any(not isinstance(k, str) or ":" not in k for k in nonrel):
            raise ValueError(f"{path}: query {qu['id']}: 'nonrelevant' must be a list of 'kind:id' strings")
    return data


# ------------------------------------------------------------------ metrics
def recall_at(ranked: list[str], rel: dict[str, int], n: int) -> float | None:
    pos = {k for k, g in rel.items() if g >= 1}
    if not pos:
        return None
    return len(pos & set(ranked[:n])) / len(pos)


def rr_at(ranked: list[str], rel: dict[str, int], n: int) -> float | None:
    if not any(g >= 1 for g in rel.values()):
        return None
    for i, key in enumerate(ranked[:n], 1):
        if rel.get(key, 0) >= 1:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: list[str], rel: dict[str, int], n: int) -> float | None:
    grades = sorted((g for g in rel.values() if g >= 1), reverse=True)
    if not grades:
        return None
    dcg = sum((2 ** rel.get(key, 0) - 1) / math.log2(i + 1) for i, key in enumerate(ranked[:n], 1))
    idcg = sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(grades[:n], 1))
    return dcg / idcg


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _p95(xs):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, math.ceil(0.95 * len(xs)) - 1)], 1) if xs else None


def _aggregate(rows: list[dict]) -> dict:
    judged = [r for r in rows if r["judged"]]
    return {"queries": len(rows), "judged": len(judged),
            "recall@5": _mean(r["recall@5"] for r in judged), "recall@10": _mean(r["recall@10"] for r in judged),
            "mrr": _mean(r["rr"] for r in judged), "ndcg": _mean(r["ndcg"] for r in judged),
            "zero_hit_rate": _mean(1.0 if r["n"] == 0 else 0.0 for r in rows),
            "mean_ms": round(sum(r["ms"] for r in rows) / len(rows), 1) if rows else None,
            "p95_ms": _p95([r["ms"] for r in rows]),
            "fallbacks": sum(1 for r in rows if r.get("fallback"))}


EVAL_MODES = {"hybrid-noent": ("hybrid", {"entities": False})}   # extra eval-only modes → (search mode, kwargs)


def run_query(store: Store, qu: dict, mode: str, k: int = 10, embedder=None, space=None) -> tuple[dict, float]:
    t0 = time.perf_counter()
    smode, extra = EVAL_MODES.get(mode, (mode, {}))
    res = q.search(store, qu["text"], mode=smode, limit=max(k, 10), embedder=embedder, space=space,
                   **(qu.get("filters") or {}), **extra)
    return res, (time.perf_counter() - t0) * 1000


def evaluate(store: Store, data: dict, modes=("lexical", "semantic", "hybrid"), k: int = 10, embedder=None,
             space=None) -> dict:
    """space: evaluate semantic/hybrid against this embedding space instead of the active one."""
    out = {"k": k, "modes": {}, "per_query": [], "space": space.key if space else None}
    per_q = {qu["id"]: {"id": qu["id"], "lang": qu.get("lang"), "intent": qu.get("intent"), "text": qu["text"]}
             for qu in data["queries"]}
    for mode in modes:
        rows = []
        for qu in data["queries"]:
            res, ms = run_query(store, qu, mode, k, embedder, space)
            ranked = [f"{r['kind']}:{r['id']}" for r in res["results"]]
            rel = qu.get("relevant") or {}
            row = {"id": qu["id"], "lang": qu.get("lang") or "?", "intent": qu.get("intent") or "?",
                   "n": res["count"], "ms": round(ms, 1),
                   "judged": any(g >= 1 for g in rel.values()),
                   "recall@5": recall_at(ranked, rel, 5), "recall@10": recall_at(ranked, rel, 10),
                   "rr": rr_at(ranked, rel, k), "ndcg": ndcg_at(ranked, rel, k), "fallback": res.get("fallback")}
            rows.append(row)
            per_q[qu["id"]][mode] = {x: row[x] for x in ("recall@10", "rr", "ndcg", "n", "ms")} | (
                {"fallback": row["fallback"]} if row["fallback"] else {})
        agg = _aggregate(rows)
        langs = sorted({r["lang"] for r in rows})
        agg["by_lang"] = {lang: _aggregate([r for r in rows if r["lang"] == lang]) for lang in langs}
        agg["by_intent"] = {it: _aggregate([r for r in rows if r["intent"] == it]) for it in sorted({r["intent"] for r in rows})}
        out["modes"][mode] = agg
    out["per_query"] = list(per_q.values())
    return out


def _label(r: dict) -> str:
    lab = r.get(q.LABEL_COL.get(r["kind"], ""), "") or ""
    extra = []
    if r["kind"] == "observation":
        v = r.get("value", r.get("value_text"))
        extra = [f"{v}{(' ' + r['unit']) if r.get('unit') else ''}" if v is not None else "", r.get("period") or ""]
    src = (r.get("source") or {}).get("publisher") or ""
    return " | ".join(x for x in [q.clip(str(lab), 160), *extra, src] if x)


def unjudged(store: Store, data: dict, modes=("lexical", "semantic", "hybrid"), k: int = 10, embedder=None) -> dict:
    """Per query: top-k results of any mode that have no judgment yet (candidates for the next pooling pass)."""
    out, total = [], 0
    for qu in data["queries"]:
        rel = qu.get("relevant") or {}
        judged = set(rel) | set(qu.get("nonrelevant") or [])
        found: dict[str, dict] = {}
        for mode in modes:
            res, _ = run_query(store, qu, mode, k, embedder)
            for rank, r in enumerate(res["results"][:k], 1):
                key = f"{r['kind']}:{r['id']}"
                if key in judged:
                    continue
                e = found.setdefault(key, {"key": key, "label": _label(r), "ranks": {}})
                e["ranks"][mode] = rank
        items = sorted(found.values(), key=lambda e: min(e["ranks"].values()))
        total += len(items)
        out.append({"id": qu["id"], "lang": qu.get("lang"), "text": qu["text"], "unjudged": items})
    return {"k": k, "modes": list(modes), "total": total, "queries": out}


# ------------------------------------------------------------------ text output
def _f(v, pct=False):
    if v is None:
        return "–"
    return f"{v:.3f}"


def format_report(res: dict) -> str:
    k = res["k"]
    lines = [f"Search evaluation (k={k}{', space ' + res['space'] if res.get('space') else ''})", ""]
    hdr = f"{'mode':12s} {'lang':5s} {'n':>4s} {'judged':>6s} {'R@5':>6s} {'R@10':>6s} {'MRR':>6s} {f'nDCG@{k}':>8s} " \
          f"{'zero':>6s} {'ms':>8s} {'p95':>8s} {'fallb':>5s}"
    lines += [hdr, "-" * len(hdr)]
    for mode, agg in res["modes"].items():
        for lang, a in [("all", agg), *agg["by_lang"].items()]:
            lines.append(f"{mode:12s} {lang:5s} {a['queries']:4d} {a['judged']:6d} {_f(a['recall@5']):>6s} "
                         f"{_f(a['recall@10']):>6s} {_f(a['mrr']):>6s} {_f(a['ndcg']):>8s} {_f(a['zero_hit_rate']):>6s} "
                         f"{a['mean_ms'] or 0:8.1f} {a['p95_ms'] or 0:8.1f} {a['fallbacks']:5d}")
    modes = list(res["modes"])
    intents = sorted({i for agg in res["modes"].values() for i in agg.get("by_intent", {})})
    if intents and intents != ["?"]:
        lines += ["", f"nDCG@{k} per intent (queries):",
                  f"{'intent':14s} {'n':>3s} " + " ".join(f"{m:>12s}" for m in modes)]
        for it in intents:
            n = next((agg["by_intent"][it]["judged"] for agg in res["modes"].values() if it in agg.get("by_intent", {})), 0)
            lines.append(f"{it[:14]:14s} {n:3d} " + " ".join(
                f"{_f(res['modes'][m].get('by_intent', {}).get(it, {}).get('ndcg')):>12s}" for m in modes))
    lines += ["", "Per query (nDCG / R@10 / hits):",
              f"{'id':12s} {'lang':4s} " + " ".join(f"{m:>20s}" for m in modes) + "  text"]
    for row in res["per_query"]:
        cells = []
        for m in modes:
            c = row.get(m, {})
            cells.append(f"{_f(c.get('ndcg'))}/{_f(c.get('recall@10'))}/{c.get('n', 0):>3d}{'*' if c.get('fallback') else ' '}")
        lines.append(f"{row['id'][:12]:12s} {str(row.get('lang') or '?')[:4]:4s} " + " ".join(f"{c:>20s}" for c in cells)
                     + "  " + q.clip(row["text"], 70))
    lines.append("(* = semantic fallback to lexical)")
    return "\n".join(lines)


def format_unjudged(res: dict) -> str:
    lines = [f"Unjudged top-{res['k']} results ({res['total']} in total; modes {', '.join(res['modes'])})"]
    for qu in res["queries"]:
        if not qu["unjudged"]:
            continue
        lines += ["", f"[{qu['id']}] ({qu.get('lang') or '?'}) {qu['text']}"]
        for e in qu["unjudged"]:
            ranks = " ".join(f"{m[:3]}#{r}" for m, r in e["ranks"].items())
            lines.append(f"  {e['key']:18s} {ranks:24s} {e['label']}")
    return "\n".join(lines)
