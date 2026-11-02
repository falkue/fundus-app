/**
 * Ansicht „Sicherung“: 3-2-1-1-0-Matrix, Sankey Quelle → Job → Ziel, Kalender-Heatmap
 * der täglich gesicherten Menge (je Job umschaltbar), RPO-Bullets je Job.
 */
import { el, pageHead, chartFrame, card, legend, mark, statePill, segmented, clear, emptyNote, dataTable, icon } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { STATES, norm, ratioState } from '../lib/state.js';
import { backupStats } from '../lib/derive.js';
import { backupSankey } from '../charts/sankey.js';
import { calendarE } from '../charts/echarts-charts.js';
import { runGantt } from '../charts/history.js';
import { bulletRow } from '../charts/bullet.js';

const COLS = [
  { key: 'copies', label: '3 Kopien', sub: 'inkl. Original', rule: (v) => (v >= 3 ? 'ok' : v === 2 ? 'warn' : 'crit') },
  { key: 'media', label: '2 Medien', sub: 'getrennte Geräte', rule: (v) => (v >= 2 ? 'ok' : v === 1 ? 'warn' : 'crit') },
  { key: 'offsite', label: '1 Offsite', sub: 'ausser Haus', rule: (v) => (v >= 1 ? 'ok' : 'crit') },
  { key: 'immutable', label: '1 Unveränderlich', sub: 'append-only / Snapshot', rule: (v) => (v >= 1 ? 'ok' : 'warn') },
  { key: 'verified', label: '0 Fehler', sub: 'geprüft + Restore-Test', rule: (v) => (v >= 1 ? 'ok' : 'warn') },
];

export function render(root, data, ctx) {
  const bs = backupStats(data);
  const classes = data.backups.classes;
  const gaps = classes.filter((c) => COLS.some((col) => typeof c[col.key] === 'number' && col.rule(c[col.key]) === 'crit')).length;
  const unknownClasses = classes.filter((c) => COLS.some((col) => typeof c[col.key] !== 'number')).length;
  const lede = [bs.jobs ? `${bs.jobs} aktive Jobs` : 'keine aktiven Jobs', bs.worstJob ? `schlechtester RPO ${bs.worstJob.label} (${fmt.num1(bs.worstRatio)}× Soll)` : null, bs.unknownJobs ? `${bs.unknownJobs} ohne Alter` : null,
    classes.length ? `${bs.offsiteYes} von ${bs.offsiteKnown || classes.length} Datenklassen mit Kopie ausser Haus` : 'keine Datenklassen'].filter(Boolean).join(' · ') + '.';
  const title = gaps ? `${numWord(gaps)} ${gaps === 1 ? 'Datenklasse' : 'Datenklassen'} mit Lücke in der 3-2-1-Regel`
    : !classes.length ? 'Sicherungen ohne Datenklassen'
    : unknownClasses ? `${numWord(unknownClasses)} ${unknownClasses === 1 ? 'Datenklasse' : 'Datenklassen'} ohne vollständige Prüfung`
    : 'Alle Datenklassen erfüllen 3-2-1';
  root.append(pageHead({ title, accent: gaps ? 'Lücke' : !classes.length ? 'Datenklassen' : unknownClasses ? 'Prüfung' : '3-2-1', lede }));
  const ins = ctx.insight('sicherung'); if (ins) root.append(ins);

  // 3-2-1-1-0
  const m = card({ title: 'Die 3-2-1-1-0-Prüfung', accent: '3-2-1-1-0', sub: 'Zeilen sind Datenklassen, nicht Geräte. Ein Snapshot auf demselben Gerät zählt nicht als zweites Medium. Schraffur: nicht erhoben – nie grün.' });
  if (!classes.length) m.append(emptyNote('Keine Datenklassen geliefert.'));
  else {
    const t = el('table.m321');
    t.append(el('colgroup', el('col.m321__col-class'), ...COLS.map(() => el('col'))));
    t.append(el('thead', el('tr', el('th', 'Datenklasse'), ...COLS.map((c) => el('th', c.label, el('small', c.sub))))));
    const tb = el('tbody');
    for (const c of classes) {
      const tr = el('tr');
      tr.append(el('td', el('span.m321__class', c.label ?? c.id, el('small', [c.primary ? `Original: ${c.primary}` : null, c.note].filter(Boolean).join(' · ')))));
      for (const col of COLS) {
        const v = c[col.key];
        const st = typeof v === 'number' ? col.rule(v) : 'unknown';
        tr.append(el('td', el('span.m321__cell', { class: `m321__cell m321__cell--${st}`, title: `${col.label}: ${typeof v === 'number' ? v : 'keine Daten'}` }, mark(st), typeof v === 'number' ? String(v) : '?')));
      }
      tb.append(tr);
    }
    t.append(tb);
    m.append(el('div.table-wrap', { style: { margin: 0 } }, t));
    m.append(legend([{ mark: 'ok', label: 'erfüllt' }, { mark: 'warn', label: 'teilweise' }, { mark: 'crit', label: 'Lücke' }, { mark: 'unknown', label: 'keine Daten' }]));
  }
  root.append(m);

  // Sankey
  const nodesById = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
  const jobs = data.backups.jobs;
  const f1 = chartFrame({ title: 'Wohin die Daten fliessen', accent: 'fliessen', sub: 'Von links nach rechts: Quelle → Sicherungsjob → Ziel. Die Breite eines Stroms entspricht der gesicherten Datenmenge (Repository-Grösse); ohne bekannte Grösse gestrichelt und schmal. Die Farbe zeigt den Zustand des Jobs (blau = läuft).', hint: 'Strom berühren für Details.' });
  if (jobs.length) backupSankey(f1.plot, jobs, nodesById, { onHover: (j) => f1.setHint(j ? `${j.label}: ${j.source ?? '?'} → ${j.target ?? '?'} · ${j.schedule ?? 'ohne Plan'} · ${j.repoBytes ? fmt.bytes(j.repoBytes) : 'Grösse unbekannt'} · ${STATES[norm(j.state)].word}` : 'Strom berühren für Details.') });
  else f1.plot.append(emptyNote('Keine Jobs geliefert.'));
  f1.table([{ key: 'label', label: 'Job' }, { key: 'tool', label: 'Werkzeug' }, { key: 'source', label: 'Quelle' }, { key: 'target', label: 'Ziel' }, { key: 'schedule', label: 'Zeitplan' }, { key: 'repoBytes', label: 'Repo', num: true, fmt: (v) => fmt.bytes(v) }, { key: 'snapshots', label: 'Stände', num: true, fmt: fmt.int }, { key: 'state', label: 'Zustand', render: (r) => statePill(r.state) }], jobs);
  root.append(card({}, f1.el));

  const two = el('div.stack');
  // Kalender
  const daily = data.backups.daily;
  const jobIds = [...new Set(daily.map((d) => d.job))].filter(Boolean);
  const f2 = chartFrame({ title: 'Gesichert pro Tag', accent: 'Tag', sub: 'Letzte 26 Wochen, Farbe nach Menge (Quantile). Leer: kein Lauf.', legend: legend([{ swatch: 'var(--cal-0)', label: 'kein Lauf' }, { swatch: 'var(--cal-1)', label: 'wenig' }, { swatch: 'var(--cal-4)', label: 'viel' }]) });
  let sel = 'all';
  const drawCal = () => {
    clear(f2.plot);
    const rows = sel === 'all' ? daily : daily.filter((d) => d.job === sel);
    if (!rows.length) { f2.plot.append(emptyNote('Keine Tageswerte.')); f2.setHint(''); return; }
    const plot = el('div.echart');
    f2.plot.append(plot);
    calendarE(plot, rows, { weeks: 26 }).then((r) => f2.setHint(`${fmt.int(r.runDays)} von ${fmt.int(r.days)} Tagen mit Lauf · ${fmt.bytes(r.total)} gesichert · Kästchen berühren zeigt die Tagesmenge.`)).catch((e) => { plot.replaceChildren(emptyNote(`Kalender konnte nicht geladen werden: ${e.message}`)); });
  };
  if (jobIds.length > 1) {
    const seg = segmented([{ value: 'all', label: 'Alle' }, ...jobIds.map((j) => ({ value: j, label: jobs.find((x) => x.id === j)?.label ?? j }))], sel, (v) => { sel = v; drawCal(); }, 'Job');
    f2.el.querySelector('.chart__tools').prepend(seg.el);
  }
  drawCal();
  const byDate = new Map();
  for (const d of daily) { const k = `${d.date}|${d.job}`; byDate.set(k, (byDate.get(k) ?? 0) + (d.bytes ?? 0)); }
  f2.table([{ key: 'date', label: 'Datum', fmt: fmt.date }, { key: 'job', label: 'Job', fmt: (v) => jobs.find((x) => x.id === v)?.label ?? v }, { key: 'bytes', label: 'Menge', num: true, fmt: fmt.bytes }], [...daily].sort((a, b) => String(b.date).localeCompare(String(a.date))).slice(0, 60));
  two.append(card({}, f2.el));

  // N4 Laufplan 14 Tage
  const f4 = chartFrame({ title: 'Laufplan der letzten 14 Tage', accent: 'Laufplan', sub: 'Ein Balken je Lauf von Start bis Ende, grün = ok, koralle = fehlgeschlagen, grau = Ergebnis unbekannt, blau = läuft. Stündliche Jobs erscheinen als dichte Tick-Spur. Wochenenden sind leicht getönt.',
    legend: legend([{ swatch: 'var(--state-ok)', label: 'ok' }, { swatch: 'var(--state-crit)', label: 'fehlgeschlagen' }, { swatch: 'var(--state-unknown)', label: 'unbekannt' }, { swatch: 'var(--blue)', label: 'läuft' }]) });
  const runs = data.backups.runs ?? [];
  runGantt(f4.plot, runs, jobs, { days: 14 });
  f4.setHint(runs.length ? `${fmt.int(runs.length)} Läufe · ${fmt.int(runs.filter((r) => r.ok === false).length)} fehlgeschlagen · Balken berühren für Dauer und Menge.` : 'Noch keine Läufe protokolliert – der Verlauf beginnt mit dem ersten v2-Lauf.');
  f4.table([{ key: 'job', label: 'Job', fmt: (v) => jobs.find((j) => j.id === v)?.label ?? v }, { key: 'start', label: 'Start', fmt: fmt.dateTime }, { key: 'end', label: 'Ende', fmt: (v) => (v ? fmt.time(v) : 'offen') }, { key: 'ok', label: 'Ergebnis', fmt: (v, r) => (v === true ? 'ok' : v === false ? `fehlgeschlagen (${r.exitCode ?? '?'})` : 'unbekannt') }, { key: 'bytesAdded', label: 'Hinzugefügt', num: true, fmt: (v) => fmt.bytes(v) }], [...runs].sort((a, b) => String(b.start).localeCompare(String(a.start))).slice(0, 80));
  two.append(card({}, f4.el));

  // RPO-Bullets
  const r = card({ title: 'Wie alt die letzte Sicherung ist', accent: 'alt', sub: 'Balken = Alter des letzten erfolgreichen Laufs, Strich = Soll-RPO des Jobs. Bis zum Strich ist alles in Ordnung, orange bis zum Doppelten, koralle darüber. „Jetzt sichern“ startet den Job auf dem Server.' });
  const b = el('div.bullets');
  for (const j of [...jobs].sort((a, b) => ratio(b) - ratio(a))) {
    if (j.intended === 'off') continue;
    const rt = typeof j.ageH === 'number' && typeof j.rpoH === 'number' && j.rpoH > 0 ? j.ageH / j.rpoH : null;
    const st = rt === null ? 'unknown' : ratioState(rt);
    const row = bulletRow(b, { label: j.label ?? j.id, sub: `${j.source ?? '?'} → ${j.target ?? '?'} · ${j.schedule ?? 'ohne Plan'}${j.running ? ' · läuft gerade' : ''}`, value: j.ageH ?? null, target: j.rpoH ?? null, max: typeof j.rpoH === 'number' ? j.rpoH * 2.5 : null,
      format: (v) => fmt.hours(v), state: norm(j.state) === 'crit' ? 'crit' : st, note: j.lastOk ? `letzter Erfolg ${fmt.dateTime(j.lastOk)}` : 'noch kein erfolgreicher Lauf',
      valueText: typeof j.ageH === 'number' ? `${fmt.hours(j.ageH)} alt · Soll ${fmt.hours(j.rpoH)}` : j.running ? 'läuft – noch kein Stand' : 'kein erfolgreicher Lauf', targetLabel: `Soll-RPO ${fmt.hours(j.rpoH)}` });
    if (TRIGGERABLE.has(j.id)) { row.classList.add('bullet-row--action'); row.append(backupButton(j, ctx)); }
  }
  const offJobs = jobs.filter((j) => j.intended === 'off');
  if (offJobs.length) b.append(el('p.small.muted', { style: { paddingTop: '8px' } }, `Absichtlich aus: ${offJobs.map((j) => `${j.label} (letzter Lauf ${fmt.date(j.lastOk)})`).join(', ')}.`));
  if (!jobs.length) b.append(emptyNote('Keine Jobs.'));
  r.append(b);
  two.append(r);
  root.append(two);
}
/** Jobs, die der Server per POST ../api/backup auslösen darf. */
const TRIGGERABLE = new Set(['restic-dokumente', 'restic-freigaben', 'restic-bilder', 'restic-system', 'restic-obsidian', 'restic-projects']);
function backupButton(j, ctx) {
  const running = j.running === true;
  const btn = el('button.btn.btn--small.bullet-row__action', { type: 'button', disabled: running || undefined, title: running ? 'Diese Sicherung läuft gerade' : `${j.label ?? j.id} jetzt sichern` },
    icon('ic-play', 'btn__icon'), running ? 'läuft …' : 'Jetzt sichern');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const started = await ctx.backup(j);
    if (started) { btn.replaceChildren(icon('ic-play', 'btn__icon'), 'gestartet …'); } else btn.disabled = false;
  });
  return btn;
}
function ratio(j) { return typeof j.ageH === 'number' && typeof j.rpoH === 'number' && j.rpoH > 0 ? j.ageH / j.rpoH : -1; }
function numWord(n) { return ['Keine', 'Eine', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
