/**
 * Detail-Panel je App (im Drawer): Kennzahlen, Sparklines, Kontingente, Functions, Cloud Run, Aufrufer.
 */
import { el, clear, dl, statePill, mark, headline, badge, lu, emptyNote, append } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { norm } from '../lib/state.js';
import { sparkline } from '../charts/sparkline.js';
import { appStats } from '../charts/apps-charts.js';
import { quotaBullets, functionTable, callerList } from './firebase.js';

export const FB_SERVICES = [['firestore', 'database', 'Firestore'], ['functions', 'plug', 'Cloud Functions'], ['run', 'container', 'Cloud Run'], ['storage', 'archive', 'Storage'], ['auth', 'key-round', 'Authentication'], ['hosting', 'globe', 'Hosting']];

export function serviceBar(a) {
  const bar = el('span.svcbar');
  for (const [key, ic, name] of FB_SERVICES) {
    const v = a.services?.[key];
    const on = v === true || (typeof v === 'number' && v > 0);
    const unknown = v === null || v === undefined;
    bar.append(el('span.svcbar__item', { class: `svcbar__item${on ? ' svcbar__item--on' : unknown ? ' svcbar__item--unknown' : ''}`, title: `${name}: ${unknown ? 'unbekannt' : on ? (typeof v === 'number' ? `${fmt.int(v)}` : 'genutzt') : 'nicht genutzt'}` }, lu(ic, 'lu')));
  }
  return bar;
}

export function openAppDrawer(a) {
  if (a.platform === 'service') { openServiceDrawer(a); return; }
  const drawer = document.getElementById('drawer');
  const body = clear(document.getElementById('drawer-body'));
  const title = document.getElementById('drawer-title');
  clear(title); append(title, [a.label ?? a.id, el('em', { style: { color: 'var(--color-headline-accent)', fontStyle: 'italic' } }, ` ${a.tier ?? a.platform ?? ''}`)]);
  const st = appStats(a);
  body.append(el('div.row', statePill(a.state), a.access === 'missing' ? el('span.apps__access', lu('eye-off', 'lu lu--sm'), 'Nutzungsdaten nicht freigegeben') : null, serviceBar(a)));
  if (a.stateReason) body.append(el('p.secondary', a.stateReason));
  if (a.accessNote) body.append(el('p.small.muted', a.accessNote));
  body.append(section('Steckbrief', dl([['Rolle', a.role ?? null], ['Projekt', a.project ?? null], ['Tarif', a.tier ?? null], ['Region', a.region ?? null], ['Domains', (a.hosting ?? []).flatMap((h) => h.names ?? []).join(', ') || null], ['Stand', a.collected ? fmt.ago(a.collected) : null]])));
  // Kennzahlen mit Sparklines
  const k = el('div');
  const rowsK = [
    ['Lesezugriffe / Tag (Ø 7 T.)', st.readsDay === null ? null : fmt.int(st.readsDay), a.series?.reads],
    ['Schreibzugriffe / Tag', st.writesDay === null ? null : fmt.int(st.writesDay), a.series?.writes],
    ['Function-Aufrufe / Tag', st.fnDay === null ? null : fmt.int(st.fnDay), a.series?.fnExec],
    ['Fehlerquote Functions', st.errRate === null ? null : fmt.pct(st.errRate * 100, 2), a.series?.fnErrors],
    ['Kosten / Monat (Schätzung)', st.costMonth === null ? null : `${fmt.num2(st.costMonth)} USD`, a.series?.costUsd],
    ['Speicher', a.series?.storageBytes?.length ? fmt.bytes(a.series.storageBytes[a.series.storageBytes.length - 1][1]) : null, a.series?.storageBytes],
    ['Trend 7 Tage', typeof a.trend7d === 'number' ? `${a.trend7d > 0 ? '+' : ''}${fmt.num1(a.trend7d)} %` : null, null],
  ];
  for (const [label, value, ser] of rowsK) {
    const row = el('div.vital', el('span.vital__label', label), el('span.vital__value', value ?? el('span.row', mark('unknown'), el('span.small.muted', 'keine Daten'))));
    if (ser?.length) { const sp = el('span.vital__spark'); sparkline(sp, ser.map((p) => p[1]), { width: 90, height: 26 }); row.append(sp); }
    k.append(row);
  }
  body.append(section('Kennzahlen (30 Tage)', k));
  if (a.quotas?.length) body.append(section('Kontingente', quotaBullets(a)));
  if (a.functions?.length) body.append(section(`Functions (${a.functions.length})`, functionTable([a], { compact: true })));
  if (a.runServices?.length) body.append(section('Cloud Run', dl(a.runServices.map((r) => [r.name, `${fmt.int(r.requests24h)} Anfragen / 24 h · ${fmt.int(r.errors5xx24h)} 5xx · ${r.region ?? ''}`]))));
  if (a.readsByMethod) body.append(section('Lesezugriffe nach Methode', dl(Object.entries(a.readsByMethod).map(([k2, v]) => [k2, fmt.int(v)]))));
  if (a.callers?.length) body.append(section('Aufrufer (24 h)', callerList(a)));
  if (!st.hasData && a.access !== 'missing') body.append(emptyNote('Keine Nutzungsreihen erhoben.'));
  drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('drawer-backdrop').dataset.open = 'true';
  document.getElementById('drawer-close').focus();
}
function section(t, ...content) { return el('div.drawer__section', headline('h3', t), ...content); }
export { badge };

/* ==========================================================================
 * Lokale Dienste auf dem Spark (platform: "service", z. B. Fundus).
 * Statt Firebase-Diensten: Betriebszustand aus systemd + MCP-Probe.
 * Gemeinsam genutzt von Kartenraster, Detail-Panel und Ansicht #fundus.
 * svc = {mcp:{url,ok,ms,error}, units:[{unit,active,sub,result,next,lastTrigger,since}],
 *        snapshot:{ageHours,bytes,path}, lastImportJob, lastSnapshotJob, dbBytes, repo}
 * ========================================================================== */
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const TZ_OFFSET = { CEST: '+02:00', CET: '+01:00', UTC: '+00:00', GMT: '+00:00' };
const UNIT_LABEL = { mcp: 'MCP-Dienst (systemd)', snapshot: 'Snapshot-Timer', import: 'Import-Timer' };

/** systemd-Zeitangabe („Sat 2026-10-24 13:35:00 CEST“) → Date oder null. */
export function parseUnitTime(s) {
  if (!s || typeof s !== 'string') return null;
  const m = s.match(/(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s*([A-Z]{2,5})?/);
  if (!m) return fmt.parseDate(s);
  const d = new Date(`${m[1]}T${m[2]}${TZ_OFFSET[m[3]] ?? ''}`);
  return Number.isNaN(d.getTime()) ? null : d;
}
const when = (s) => { const d = parseUnitTime(s); return d ? `${fmt.dateTime(d)} (${fmt.ago(d)})` : null; };
const unitKind = (u) => (/mcp/.test(u?.unit ?? '') ? 'mcp' : /snapshot/.test(u?.unit ?? '') ? 'snapshot' : /import/.test(u?.unit ?? '') ? 'import' : null);
export function unitLabel(u) { return UNIT_LABEL[unitKind(u)] ?? (String(u?.unit ?? '').endsWith('.timer') ? 'Timer' : 'Dienst'); }
/** Zustand einer systemd-Einheit: aktiv = läuft, failed = kritisch, sonst Achtung; fehlt = keine Daten. */
export function unitState(u) {
  if (!u || !u.active) return 'unknown';
  if (u.active === 'failed' || u.result === 'failed') return 'crit';
  if (u.active !== 'active') return 'warn';
  if (u.result && u.result !== 'success') return 'warn';
  return 'ok';
}
const SUB_WORD = { running: 'läuft', waiting: 'wartet', exited: 'beendet', dead: 'gestoppt', failed: 'fehlgeschlagen', elapsed: 'abgelaufen' };
const ACTIVE_WORD = { active: 'aktiv', inactive: 'inaktiv', failed: 'fehlgeschlagen', activating: 'startet', deactivating: 'stoppt', reloading: 'lädt neu' };
function mcpState(m) { return !m ? 'unknown' : m.ok === true ? 'ok' : m.ok === false ? 'crit' : 'unknown'; }
/** Alter des Snapshots: bis 3 h normal (stündlicher Timer), bis 26 h Achtung, darüber kritisch. */
function snapshotState(s) { const h = s?.ageHours; return !isNum(h) ? 'unknown' : h <= 3 ? 'ok' : h <= 26 ? 'warn' : 'crit'; }
function jobState(j) { return !j || !j.result ? 'unknown' : j.result === 'success' ? 'ok' : 'crit'; }
function jobText(j) {
  if (!j || !j.result) return 'keine Angabe';
  const res = j.result === 'success' ? 'erfolgreich' : `fehlgeschlagen (${j.result})`;
  return `${res}${j.exitStatus !== undefined && j.exitStatus !== null ? ` · Exit ${j.exitStatus}` : ''}`;
}

/** Kurzleiste für die App-Karte: MCP · Snapshot · Import, je Marke + Wort (Details im title). */
export function opsBar(svc) {
  const bar = el('span.opsbar');
  const units = svc?.units ?? [];
  const byKind = (k) => units.find((u) => unitKind(u) === k);
  const mcpUnit = byKind('mcp');
  const mcpS = svc?.mcp ? mcpState(svc.mcp) : unitState(mcpUnit);
  const items = [
    ['MCP', mcpS, svc?.mcp ? (svc.mcp.ok ? `MCP-Server erreichbar${isNum(svc.mcp.ms) ? ` (${fmt.int(svc.mcp.ms)} ms)` : ''}` : `MCP-Server nicht erreichbar${svc.mcp.error ? `: ${svc.mcp.error}` : ''}`) : 'MCP-Server: keine Probe'],
    ['Snapshot', snapshotState(svc?.snapshot) === 'unknown' ? unitState(byKind('snapshot')) : snapshotState(svc?.snapshot), `Snapshot-Timer ${ACTIVE_WORD[byKind('snapshot')?.active] ?? 'unbekannt'}${isNum(svc?.snapshot?.ageHours) ? ` · letzter Snapshot vor ${fmt.hours(svc.snapshot.ageHours)}` : ''}`],
    ['Import', unitState(byKind('import')) === 'ok' ? jobState(svc?.lastImportJob) === 'crit' ? 'crit' : 'ok' : unitState(byKind('import')), `Import-Timer ${ACTIVE_WORD[byKind('import')?.active] ?? 'unbekannt'}${byKind('import')?.next ? ` · nächster Lauf ${when(byKind('import').next)}` : ''} · letzter Import ${jobText(svc?.lastImportJob)}`],
  ];
  for (const [label, s, title] of items) bar.append(el('span.opsbar__item', { class: `opsbar__item opsbar__item--${norm(s)}`, title }, mark(s), el('span', label)));
  return bar;
}

/** Gesamtzustand des Betriebs (schlechtester Einzelzustand; nur unbekannt, wenn gar nichts geliefert wurde). */
export function opsState(svc) {
  if (!svc) return 'unknown';
  const rank = { ok: 0, unknown: 1, warn: 2, crit: 3 };
  const states = [mcpState(svc.mcp), ...(svc.units ?? []).map(unitState), snapshotState(svc.snapshot), jobState(svc.lastImportJob)].filter((s) => s !== 'unknown');
  return states.length ? states.reduce((w, s) => (rank[s] > rank[w] ? s : w), 'ok') : 'unknown';
}

/** Betriebsliste (Drawer und Ansicht #fundus): MCP, Einheiten, Snapshot, Jobs, Datenbank, Repository. */
export function opsList(svc) {
  const list = el('div.ops', { role: 'list' });
  const row = (state, label, value, sub, title) => list.append(el('div.ops__row', { role: 'listitem' },
    mark(state), el('span.ops__label', { title: title ?? label }, label), el('span.ops__value', value ?? el('span.muted', 'keine Angabe')),
    sub ? el('span.ops__sub', { title: typeof sub === 'string' ? sub : null }, sub) : null));
  if (!svc) { list.append(emptyNote('Keine Betriebsdaten geliefert.')); return list; }
  const m = svc.mcp;
  row(mcpState(m), 'MCP-Server (HTTP-Probe)', !m ? null : m.ok ? `erreichbar${isNum(m.ms) ? ` · ${fmt.int(m.ms)} ms` : ''}` : 'nicht erreichbar', m?.error ? `Fehler: ${m.error}` : (m?.url ?? null));
  for (const u of svc.units ?? []) {
    const k = unitKind(u);
    const word = [ACTIVE_WORD[u.active] ?? u.active, SUB_WORD[u.sub] ?? u.sub].filter(Boolean).join(' · ');
    let sub;
    if (k === 'mcp' || !String(u.unit).endsWith('.timer')) sub = u.since ? `${u.unit} · seit ${when(u.since) ?? u.since}` : u.unit;
    else sub = `${u.unit} · nächster Lauf ${u.next ? when(u.next) ?? u.next : 'keiner geplant'} · letzter ${u.lastTrigger ? when(u.lastTrigger) ?? u.lastTrigger : 'noch nie ausgelöst'}`;
    row(unitState(u), unitLabel(u), word || null, sub, u.unit);
  }
  const s = svc.snapshot;
  row(snapshotState(s), 'Letzter Snapshot', isNum(s?.ageHours) ? `vor ${fmt.hours(s.ageHours)}` : null, s ? [isNum(s.bytes) ? fmt.bytes(s.bytes) : null, s.path ?? null].filter(Boolean).join(' · ') || null : null);
  if (svc.lastSnapshotJob) row(jobState(svc.lastSnapshotJob), 'Letzter Snapshot-Lauf', jobText(svc.lastSnapshotJob), svc.lastSnapshotJob.finished ? `beendet ${when(svc.lastSnapshotJob.finished) ?? svc.lastSnapshotJob.finished}` : 'Endzeit nicht gemeldet');
  row(jobState(svc.lastImportJob), 'Letzter Import-Lauf', jobText(svc.lastImportJob), svc.lastImportJob?.finished ? `beendet ${when(svc.lastImportJob.finished) ?? svc.lastImportJob.finished}` : 'Endzeit nicht gemeldet (Timer seit dem Start noch nicht ausgelöst)');
  row(isNum(svc.dbBytes) ? 'ok' : 'unknown', 'Datenbank (SQLite)', isNum(svc.dbBytes) ? fmt.bytes(svc.dbBytes) : null, svc.dbPath ?? null);
  if (svc.repo && /^https?:\/\//.test(svc.repo)) {
    list.append(el('div.ops__row', { role: 'listitem' }, lu('external-link', 'lu lu--sm ops__icon'), el('span.ops__label', 'Repository'),
      el('a.ops__value.ops__link', { href: svc.repo, target: '_blank', rel: 'noopener noreferrer', title: svc.repo }, svc.repo.replace(/^https?:\/\/(www\.)?/, ''))));
  }
  return list;
}

/** Betriebsdaten der App (apps[].service) in die gemeinsame Form bringen. */
export function serviceOf(a) { return a?.service ? { ...a.service, repo: a.service.repo ?? a.repo ?? null } : null; }

function openServiceDrawer(a) {
  const drawer = document.getElementById('drawer');
  const body = clear(document.getElementById('drawer-body'));
  const title = document.getElementById('drawer-title');
  clear(title); append(title, [a.label ?? a.id, el('em', { style: { color: 'var(--color-headline-accent)', fontStyle: 'italic' } }, ` ${a.tier ?? 'Dienst'}`)]);
  const svc = serviceOf(a);
  const mt = a.metrics ?? {};
  body.append(el('div.row', statePill(a.state), opsBar(svc)));
  if (a.stateReason) body.append(el('p.secondary', a.stateReason));
  body.append(section('Steckbrief', dl([['Rolle', a.role ?? null], ['Plattform', a.region ?? 'Spark'], ['Projekt', a.project ?? null], ['Quelle', a.source ?? null], ['Stand', a.collected ? fmt.ago(a.collected) : null]])));
  body.append(section('Betrieb', opsList(svc)));
  const k = el('div');
  const calls = (a.series?.toolCalls ?? []).map((p) => (Array.isArray(p) ? p[1] : p?.calls));
  const rowsK = [
    ['Einträge gesamt', isNum(mt.items) ? fmt.int(mt.items) : null, null],
    ['Projekte', isNum(mt.projects) ? fmt.int(mt.projects) : null, null],
    ['Zeitreihen mit ≥ 2 Perioden', isNum(mt.series) ? fmt.int(mt.series) : null, null],
    ['Anteil verifiziert', isNum(mt.verifiedShare) ? fmt.pct(mt.verifiedShare * 100, 1) : null, null],
    ['Fällige Prognosen', isNum(mt.forecastsDue) ? fmt.int(mt.forecastsDue) : null, null],
    ['Schreibvorgänge aus Sitzungen (7 T.)', isNum(mt.sessionWrites7d) ? fmt.int(mt.sessionWrites7d) : null, null],
    ['MCP-Werkzeugaufrufe heute', isNum(mt.toolCallsToday) ? fmt.int(mt.toolCallsToday) : null, calls],
    ['Folien und Grafiken', isNum(mt.exhibits) ? `${fmt.int(mt.exhibits)}${isNum(mt.exhibitVersions) ? ` · ${fmt.int(mt.exhibitVersions)} Versionen` : ''}` : null, null],
    ['Neue Folien-Versionen (7 T.)', isNum(mt.exhibitVersions7d) ? fmt.int(mt.exhibitVersions7d) : null, (a.series?.exhibitVersions ?? []).map((p) => (Array.isArray(p) ? p[1] : p?.n))],
  ];
  for (const [label, value, ser] of rowsK) {
    const r = el('div.vital', el('span.vital__label', label), el('span.vital__value', value ?? el('span.row', mark('unknown'), el('span.small.muted', 'keine Daten'))));
    if (ser) { const sp = el('span.vital__spark', { title: ser.length ? `${ser.length} Tage` : 'noch keine Aufrufe protokolliert' }); sparkline(sp, ser, { width: 90, height: 26 }); r.append(sp); }
    k.append(r);
  }
  body.append(section('Kennzahlen', k));
  if (!calls.length) body.append(el('p.small.muted', 'Werkzeugaufrufe werden erst gezählt, seit der MCP-Server die Nutzung protokolliert – bisher liegen keine Aufrufe vor.'));
  if (a.errors?.length) body.append(section('Fehler', el('ul.small', a.errors.map((e) => el('li', String(e?.message ?? e?.msg ?? e))))));
  body.append(el('div.row', el('a.btn.btn--primary', { href: '#fundus' }, 'Alle Fundus-Kennzahlen'), el('span.small.muted', 'Zusammensetzung, Folien, Zeitreihen, Prognosen, Quellen, Datenqualität')));
  body.append(el('div.row', el('a.btn', { href: 'http://127.0.0.1:8010/portal/', target: '_blank', rel: 'noopener' }, 'Fundus-Portal öffnen'), el('span.small.muted', 'Folien je Projekt und Foliensatz, Sichtbarkeit')));
  drawer.setAttribute('aria-hidden', 'false');
  document.getElementById('drawer-backdrop').dataset.open = 'true';
  document.getElementById('drawer-close').focus();
}
