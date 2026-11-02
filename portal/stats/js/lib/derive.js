/**
 * Abgeleitete Kennzahlen aus landscape.json. Alle Funktionen vertragen fehlende
 * Felder und null. Nichts hier ist optimistisch: fehlende Daten → unknown.
 */
import { norm, worst, ratioState, SEVERITY, eolState } from './state.js';
import { hoursSince } from './fmt.js';

export const STALE_H = 8;
const arr = (v) => (Array.isArray(v) ? v : []);
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Sichere Sicht auf den Datensatz (fehlende Listen → leer). */
export function safe(data) {
  const d = data ?? {};
  return {
    schema: d.schema ?? null,
    generated: d.generated ?? null,
    collectors: arr(d.collectors),
    zones: arr(d.zones),
    nodes: arr(d.nodes),
    services: arr(d.services),
    links: arr(d.links),
    ports: arr(d.ports),
    internet: arr(d.internet),
    software: arr(d.software),
    backups: { jobs: arr(d.backups?.jobs), classes: arr(d.backups?.classes), daily: arr(d.backups?.daily), runs: arr(d.backups?.runs) },
    ai: d.ai ?? null,
    history: { series: arr(d.history?.series) },
    apps: arr(d.apps),
    findings: arr(d.findings),
    checks: arr(d.checks),
    portsClosed: arr(d.portsClosed),
    energy: d.energy && typeof d.energy === 'object' ? d.energy : null,
    findingsResolved: arr(d.findingsResolved),
    _sample: d._sample ?? false,
  };
}

/** Kollektoren mit Alter und Zustand (ok/warn/crit/unknown). */
export function collectorStatus(data, now = Date.now()) {
  return data.collectors.map((c) => {
    const ageH = hoursSince(c.at, now);
    let state;
    if (c.ok === false) state = 'crit';
    else if (ageH === null) state = 'unknown';
    else if (ageH > STALE_H) state = 'warn';
    else state = 'ok';
    return { ...c, ageH, state, errors: arr(c.errors) };
  });
}

/** Dienste-Kennzahlen: x von y laufen, Probleme, absichtlich aus. */
export function serviceStats(data) {
  const s = data.services;
  const intendedOn = s.filter((x) => x.intended !== 'off');
  const running = intendedOn.filter((x) => norm(x.state) === 'ok');
  const problems = s.filter((x) => ['warn', 'crit'].includes(norm(x.state)));
  const off = s.filter((x) => x.intended === 'off' && norm(x.state) === 'off');
  const unknown = s.filter((x) => norm(x.state) === 'unknown');
  return { total: intendedOn.length, running: running.length, problems: problems.length, off: off.length, unknown: unknown.length,
    crit: s.filter((x) => norm(x.state) === 'crit').length, warn: s.filter((x) => norm(x.state) === 'warn').length };
}

/** Sicherungen: schlechtester RPO (Verhältnis), Offsite-Status, Zustände. */
export function backupStats(data) {
  const jobs = data.backups.jobs.filter((j) => j.intended !== 'off');
  let worstRatio = null, worstJob = null, unknownJobs = 0;
  for (const j of jobs) {
    if (isNum(j.ageH) && isNum(j.rpoH) && j.rpoH > 0) {
      const r = j.ageH / j.rpoH;
      if (worstRatio === null || r > worstRatio) { worstRatio = r; worstJob = j; }
    } else unknownJobs++;
  }
  const classes = data.backups.classes;
  const offsiteYes = classes.filter((c) => isNum(c.offsite) && c.offsite >= 1).length;
  const offsiteKnown = classes.filter((c) => isNum(c.offsite)).length;
  const states = jobs.map((j) => norm(j.state));
  let state = worst([...states, ratioState(worstRatio)]);
  if (unknownJobs > 0 && state === 'ok') state = 'unknown';
  return { jobs: jobs.length, worstRatio, worstJob, unknownJobs, offsiteYes, offsiteKnown, classes: classes.length, state,
    problems: jobs.filter((j) => ['warn', 'crit'].includes(norm(j.state))).length };
}

/** Schwachstellen: Sicherheitsupdates + EOL + CVEs von aussen. */
export function vulnStats(data) {
  const security = data.nodes.reduce((a, n) => a + (isNum(n.patches?.security) ? n.patches.security : 0), 0);
  const pending = data.nodes.reduce((a, n) => a + (isNum(n.patches?.pending) ? n.patches.pending : 0), 0);
  const patchUnknown = data.nodes.filter((n) => n.state !== 'unknown' && !isNum(n.patches?.pending)).length;
  const eol = data.software.filter((s) => isNum(s.eolDays) && s.eolDays < 0);
  const eolSoon = data.software.filter((s) => isNum(s.eolDays) && s.eolDays >= 0 && s.eolDays < 180);
  // Vom Hersteller gepflegte Komponenten (z. B. QNAPs Docker in Container Station) machen ein System nicht „ohne Support“
  const eolNodes = new Set(eol.filter((s) => !s.vendorManaged).map((s) => s.node));
  const cves = data.internet.reduce((a, i) => a + arr(i.vulns).length, 0);
  const kev = data.internet.reduce((a, i) => a + arr(i.kev).length, 0);
  const epss = data.internet.reduce((a, i) => a + arr(i.epssHigh).length, 0);
  const hasPrio = data.internet.some((i) => Array.isArray(i.kev) || Array.isArray(i.epssHigh));
  // handlungsrelevant: nur KEV (aktiv ausgenutzt). EPSS-hoch und übrige CVEs stammen aus einem Versionsabgleich
  // von aussen (InternetDB) ohne Rücksicht auf Distributions-Backports → nur Hinweis, nicht mitgezählt.
  const relevantCves = hasPrio ? kev : 0;
  const actionable = (security > 0 ? 1 : 0) + eolNodes.size + relevantCves;
  let state = 'ok';
  if (eolNodes.size > 0 || security >= 100 || kev > 0) state = 'crit';
  else if (security > 0 || eolSoon.length || relevantCves || epss) state = 'warn';
  if (patchUnknown === data.nodes.length) state = 'unknown';
  return { security, pending, patchUnknown, eol, eolSoon, eolNodes: [...eolNodes], cves, kev, epss, hasPrio, relevantCves, actionable, state };
}

/** Exposition: unerwartete und unbewertete Ports, ohne Anmeldung an allen Schnittstellen. */
export function exposureStats(data) {
  const p = data.ports;
  const unexpected = p.filter((x) => x.expected === false);
  const unrated = p.filter((x) => x.expected === null || x.expected === undefined);
  const openNoAuth = p.filter((x) => x.scope === 'all' && x.auth === 'none');
  let state = 'ok';
  if (unexpected.some((x) => x.scope === 'all' || x.auth === 'none')) state = 'crit';
  else if (unexpected.length || unrated.length) state = 'warn';
  if (!p.length) state = 'unknown';
  return { total: p.length, unexpected, unrated, openNoAuth, state };
}

/** KI: Modelle geladen gegen Grenze. */
export function aiStats(data) {
  const ai = data.ai;
  if (!ai) return { state: 'unknown', loaded: 0, limit: null, loadedBytes: null, installedBytes: null, unused: [] };
  const loaded = arr(ai.loaded);
  const installed = arr(ai.installed);
  const limit = isNum(ai.limitLoaded) ? ai.limitLoaded : null;
  const used = new Set([...loaded.map((m) => m.name), ...arr(ai.consumers).flatMap((c) => arr(c.models))]);
  const unused = installed.filter((m) => !used.has(m.name));
  const loadedBytes = loaded.reduce((a, m) => a + (isNum(m.bytes) ? m.bytes : 0), 0);
  const installedBytes = installed.reduce((a, m) => a + (isNum(m.bytes) ? m.bytes : 0), 0);
  const unusedBytes = unused.reduce((a, m) => a + (isNum(m.bytes) ? m.bytes : 0), 0);
  let state = 'ok';
  if (limit !== null && loaded.length > limit) state = 'crit';
  else if (limit === null) state = 'unknown';
  return { state, loaded: loaded.length, limit, loadedBytes, installedBytes, unused, unusedBytes, installed: installed.length };
}

/** Offene (nicht quittierte) Befunde nach Schwere sortiert. */
export function openFindings(data) {
  return data.findings
    .filter((f) => !f.acked)
    .sort((a, b) => (SEVERITY[b.severity] ?? -1) - (SEVERITY[a.severity] ?? -1) || String(a.title).localeCompare(String(b.title), 'de'));
}
export function findingCounts(data) {
  const open = openFindings(data);
  const by = { crit: 0, high: 0, med: 0, low: 0 };
  for (const f of open) if (by[f.severity] !== undefined) by[f.severity]++;
  return { open: open.length, acked: data.findings.length - open.length, ...by };
}

/**
 * Gesamturteil: Satz + Akzentwort + Zustand. Berücksichtigt Befunde, Knoten,
 * Dienste, Kollektoren-Frische. Fehlende Daten sind nie grün.
 */
export function verdict(data, now = Date.now()) {
  const fc = findingCounts(data);
  const cols = collectorStatus(data, now);
  const stale = cols.filter((c) => c.state !== 'ok');
  const nodeStates = data.nodes.map((n) => norm(n.state));
  const svc = serviceStats(data);
  let state = worst([...nodeStates, fc.crit || fc.high ? 'crit' : fc.med || fc.low ? 'warn' : 'ok']);
  if (!data.generated || !data.nodes.length) state = 'unknown';
  if (stale.length && state === 'ok') state = 'warn';

  let text, accent;
  const parts = [];
  if (fc.crit) parts.push(`${fc.crit} ${fc.crit === 1 ? 'kritischer Befund' : 'kritische Befunde'}`);
  if (fc.high) parts.push(`${fc.high} ${fc.high === 1 ? 'hoher' : 'hohe'}`);
  if (fc.med) parts.push(`${fc.med} ${fc.med === 1 ? 'mittlerer' : 'mittlere'}`);
  if (fc.low) parts.push(`${fc.low} ${fc.low === 1 ? 'niedriger' : 'niedrige'}`);

  if (state === 'unknown') { text = 'Keine belastbaren Daten – das Urteil ist offen.'; accent = 'offen'; }
  else if (state === 'crit') {
    text = `Die Landschaft braucht Eingriff: ${joinDe(parts)}.`; accent = 'Eingriff';
  } else if (state === 'warn') {
    text = fc.open ? `Die Landschaft läuft, aber ${fc.open} ${fc.open === 1 ? 'Befund braucht' : 'Befunde brauchen'} Aufmerksamkeit.` : 'Die Landschaft läuft, ein Kollektor ist veraltet – Aufmerksamkeit nötig.';
    accent = 'Aufmerksamkeit';
  } else { text = 'Alles läuft, keine offenen Befunde.'; accent = 'läuft'; }

  const sub = [];
  sub.push(`${svc.running} von ${svc.total} Diensten laufen`);
  if (svc.off) sub.push(`${svc.off} absichtlich aus`);
  if (stale.length) sub.push(`${stale.length} ${stale.length === 1 ? 'Kollektor' : 'Kollektoren'} veraltet oder fehlgeschlagen`);
  if (fc.acked) sub.push(`${fc.acked} quittiert`);
  return { state, text, accent, sub: sub.join(' · '), collectors: cols, stale, counts: fc };
}

function joinDe(parts) {
  if (!parts.length) return 'keine Befunde';
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(', ')} und ${parts[parts.length - 1]}`;
}

/** Zeitreihe nach ID; Punkte als [{t: Date, v}] (null-Werte entfernt). */
export function series(data, id) {
  const s = data.history.series.find((x) => x.id === id);
  if (!s) return null;
  const points = arr(s.points).map(([t, v]) => ({ t: new Date(t), v })).filter((p) => !Number.isNaN(p.t.getTime()) && isNum(p.v));
  return { ...s, points };
}
export function seriesFor(data, node, metric) {
  return data.history.series.filter((s) => s.node === node && s.metric === metric).map((s) => series(data, s.id));
}
/** Punkte als Zahlenliste für Sparklines. */
export function sparkValues(s) { return s ? s.points.map((p) => p.v) : []; }

/** Services je Knoten. */
export function servicesOf(data, nodeId) { return data.services.filter((s) => s.node === nodeId); }
export function nodeById(data, id) { return data.nodes.find((n) => n.id === id) ?? null; }

/** Lebensende-Zeilen aus software + OS der Knoten (dedupliziert). */
export function eolRows(data) {
  const rows = [];
  for (const s of data.software) {
    if (s.eol || isNum(s.eolDays) || s.kind === 'os') rows.push({ node: s.node, name: s.name, version: s.installed, eol: s.eol ?? null, eolDays: isNum(s.eolDays) ? s.eolDays : null, state: eolState(isNum(s.eolDays) ? s.eolDays : null), kind: s.kind, vendorManaged: !!s.vendorManaged, note: s.note ?? null });
  }
  return rows;
}

/** Beginn der Beobachtung: ältester Kollektorlauf bzw. ältester Tages-Eintrag der Dienste; null ohne Verlauf. */
export function observationStart(data) {
  let t = null;
  for (const c of data.collectors) for (const r of arr(c.runs)) { const x = r?.at ? new Date(r.at).getTime() : NaN; if (Number.isFinite(x) && (t === null || x < t)) t = x; }
  return t;
}
/** „neu“: Zeitpunkt liegt nach Beobachtungsbeginn + 6 h und ist jünger als 6 h. Ohne Verlauf nie „neu“. */
export function isFresh(ts, data, hours = 6, now = Date.now()) {
  if (!ts) return false;
  const t = new Date(ts).getTime();
  if (!Number.isFinite(t)) return false;
  const start = observationStart(data);
  if (start === null) return false;
  return t > start + hours * 3.6e6 && now - t < hours * 3.6e6;
}
