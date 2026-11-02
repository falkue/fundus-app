"""Fundus-Statistik fürs Portal (#statistik): Kennzahlen zum Datenbestand und zum Betrieb, ganz aus Fundus selbst.

Früher lieferte das System-Dashboard (~/nas-dashboard/v2) diese Daten in seiner landscape.json; Fundus ist dort nicht
mehr Teil, die Berechnung liegt deshalb hier:
- ``metrics.compute`` (Datenbestand, Folien, Nutzung) und ``metrics.health`` (MCP, Timer, Jobs, Snapshot),
- ``app_entry``: Betriebszustand ok/warn/crit mit Gründen und Tageskennzahlen (wie die App-Karte im Dashboard),
- ``collector_entry``: Laufzeit und Verlauf der letzten Berechnungen (``data/stats-runs.json``).
Der Server liefert das Ergebnis unter GET /api/stats (eine Minute zwischengespeichert); ``python -m fundus.stats_export``
schreibt dasselbe nach ``portal/data/stats.json`` (Rückfall, wenn der Server die Route nicht kennt).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

from fundus import config, metrics
from fundus.store import Store

OUT = Path(__file__).resolve().parent.parent / "portal" / "data" / "stats.json"
RUNS_KEEP = 48


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def app_entry(m: dict) -> dict:
    """App-Eintrag (platform 'service') aus den Kennzahlen: Zustand und Gründe bei gestörtem Betrieb."""
    h = m.get("health") or {}
    mcp = h.get("mcp") or {}
    units = {u.get("unit"): u for u in (h.get("units") or [])}
    snap = (h.get("snapshot") or {}).get("ageHours")
    snap_current = (h.get("snapshot") or {}).get("upToDate")
    imp = h.get("lastImportJob") or {}
    reasons, state = [], "ok"
    if not mcp.get("ok"):
        state = "crit"
        reasons.append(f"MCP-Server nicht erreichbar ({mcp.get('error') or 'keine Antwort'})")
    for u in ("fundus-snapshot.timer", "fundus-import.timer"):
        if (units.get(u) or {}).get("active") != "active":
            state = "warn" if state == "ok" else state
            reasons.append(f"{u} nicht aktiv")
    if imp.get("result") not in (None, "", "success"):
        state = "warn" if state == "ok" else state
        reasons.append(f"letzter nächtlicher Import: {imp.get('result')} (Exit {imp.get('exitStatus')})")
    if snap is None or (snap > 3 and snap_current is not True):   # unchanged content = no new snapshot needed
        state = "warn" if state == "ok" else state
        reasons.append("kein aktueller Datenbank-Snapshot" if snap is None else f"Snapshot {snap} h alt")
    act = m.get("activity") or {}
    calls = act.get("toolCallsByDay") or []
    ex = m.get("exhibits") or {}
    today = dt.date.today().isoformat()
    return {"id": "fundus", "label": "Fundus", "role": "Zentrales Evidenzarchiv: Zahlen, Zeitreihen, Prognosen, Studien, Zitate",
            "platform": "service", "project": config.display_path(config.REPO_ROOT), "region": config.site_label(), "tier": "Dienst",
            "state": state, "stateReason": "; ".join(reasons) or None, "collected": m.get("generatedAt"),
            "source": "Fundus (live)", "legacyAt": None, "errors24h": None, "errors": [],
            "history": None, "repo": h.get("repo"),
            "service": {"mcp": mcp, "units": h.get("units"), "snapshot": h.get("snapshot"), "lastImportJob": imp,
                        "lastSnapshotJob": h.get("lastSnapshotJob"), "dbBytes": (m.get("db") or {}).get("bytes")},
            "metrics": {"items": m.get("itemsTotal"), "series": (m.get("series") or {}).get("multiPeriod"),
                        "forecastsDue": (m.get("forecasts") or {}).get("byOutcome", {}).get("due"),
                        "verifiedShare": (m.get("quality") or {}).get("verifiedShare"),
                        "toolCallsToday": next((c["calls"] for c in calls if c.get("date") == today), 0),
                        "sessionWrites7d": act.get("sessionWrites7d"), "projects": len(m.get("byProject") or []),
                        "exhibits": ex.get("logical"), "exhibitVersions": ex.get("versions"),
                        "exhibitVersions7d": ex.get("versions7d"), "exhibitProjects": len(ex.get("byProject") or []) or None},
            "series": {"toolCalls": [[c["date"], c["calls"]] for c in calls],
                       "exhibitVersions": [[d["date"], d["n"]] for d in (ex.get("versionsByDay") or [])]}}


def _runs_file(st: Store) -> Path:
    return st.path.parent / "stats-runs.json"


def collector_entry(st: Store, ok: bool, took: float, errors: list[str]) -> dict:
    """Laufzeit dieser Berechnung und die letzten Läufe (für die Karte „Erfassung“)."""
    f = _runs_file(st)
    try:
        runs = json.loads(f.read_text())
    except (OSError, ValueError):
        runs = []
    at = _now()
    runs = (runs + [{"at": at, "ok": ok, "tookS": round(took, 1), "errorCount": len(errors)}])[-RUNS_KEEP:]
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs))
        tmp.replace(f)
    except OSError:
        pass
    return {"id": "fundus", "label": "Fundus (Kennzahlen, live)", "ok": ok, "at": at, "tookS": round(took, 1),
            "errors": errors, "partial": bool(errors), "runs": runs}


def build(st: Store, probe_mcp: bool = True) -> dict:
    """Alles, was die Ansicht braucht: {generated, fundus, apps, collectors, interp}."""
    t0 = time.monotonic()
    errors = []
    m = metrics.compute(st)
    try:
        m["health"] = metrics.health(st, probe_mcp=probe_mcp)
    except Exception as e:      # Betrieb nicht lesbar: die Kennzahlen zum Bestand gelten trotzdem
        errors.append(f"health: {e}"[:200])
    return {"generated": _now(), "fundus": m, "apps": [app_entry(m)],
            "collectors": [collector_entry(st, not errors, time.monotonic() - t0, errors)], "interp": None}


def main() -> int:
    st = Store()
    try:
        data = build(st)
    finally:
        st.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str))
    tmp.replace(OUT)
    print(f"{OUT}: {OUT.stat().st_size} Bytes, Stand {data['generated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
