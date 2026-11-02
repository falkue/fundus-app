/**
 * Ansicht „Updates & Support“: gestapelte Balken je Knoten, EOL-Zeitleiste,
 * Versions-Tabelle mit Abstand als Badge.
 */
import { el, pageHead, chartFrame, card, legend, statePill, badge, mark, emptyNote, dataTable } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { behindState, BEHIND_WORD, eolState, STATES } from '../lib/state.js';
import { vulnStats, eolRows } from '../lib/derive.js';
import { stackedBars } from '../charts/bars.js';
import { eolTimeline } from '../charts/eol.js';

export function render(root, data, ctx) {
  const vu = vulnStats(data);
  // Knoten, deren Betriebssystem (nicht nur eine Komponente) ohne Support ist
  const osEol = data.nodes.filter((n) => typeof n.os?.eolDays === 'number' && n.os.eolDays < 0).map((n) => n.label ?? n.id);
  const compEol = data.software.filter((s) => typeof s.eolDays === 'number' && s.eolDays < 0 && !(s.kind === 'os' && osEol.includes(data.nodes.find((n) => n.id === s.node)?.label ?? s.node)));
  const joinDe = (a) => (a.length <= 1 ? a.join('') : `${a.slice(0, -1).join(', ')} und ${a[a.length - 1]}`);
  const lede = [vu.security ? `${fmt.int(vu.security)} Sicherheitsupdates offen (${fmt.int(vu.pending)} insgesamt)` : vu.pending ? `${fmt.int(vu.pending)} Updates offen, keine Sicherheitsupdates` : 'keine ausstehenden Updates gemeldet',
    osEol.length ? `Betriebssystem ohne Support: ${joinDe(osEol)}` : null,
    compEol.length ? `${compEol.length} ${compEol.length === 1 ? 'Komponente' : 'Komponenten'} ohne Support (${[...new Set(compEol.map((s) => `${s.name}${s.vendorManaged ? ', vom Hersteller gepflegt' : ''}`))].join('; ')})` : null,
    vu.eolSoon.length ? `${vu.eolSoon.length} Komponente(n) laufen in < 180 Tagen aus` : null,
    vu.patchUnknown ? `${vu.patchUnknown} Knoten ohne Patch-Daten` : null].filter(Boolean).join(' · ') + '.';
  const title = osEol.length ? `${joinDe(osEol)}: Betriebssystem ohne Support` : vu.security ? `${fmt.int(vu.security)} Sicherheitsupdates warten auf Hero` : compEol.length ? `${compEol.length} ${compEol.length === 1 ? 'Komponente' : 'Komponenten'} ohne Support` : vu.state === 'unknown' ? 'Patch-Stand ohne Daten' : 'Patch-Stand ohne offene Lücken';
  root.append(pageHead({ title, accent: osEol.length ? 'Support' : vu.security ? 'warten' : compEol.length ? 'Support' : vu.state === 'unknown' ? 'Daten' : 'Lücken', lede }));
  const ins = ctx.insight('patches'); if (ins) root.append(ins);

  // Balken
  const f1 = chartFrame({ title: 'Ausstehende Updates je Knoten', accent: 'Updates', sub: 'Anzahl ausstehender Pakete je Knoten, gestapelt: Sicherheitsupdates (kräftig blau) und übrige (blass). QNAP-Geräte liefern keine Paketzahl – dort steht der Firmware-Stand.',
    legend: legend([{ swatch: 'var(--chart-series)', label: 'Sicherheit' }, { swatch: 'var(--chart-series-2)', label: 'übrige' }, { hatch: true, label: 'keine Daten' }]) });
  const rows = data.nodes.filter((n) => n.state !== 'unknown' || n.patches).map((n) => ({
    label: n.label ?? n.id, sub: n.os?.name ? `${n.os.name} ${n.os.version ?? ''}` : '',
    values: n.patches && typeof n.patches.pending === 'number' ? { security: n.patches.security ?? 0, other: Math.max(0, n.patches.pending - (n.patches.security ?? 0)) } : { security: null, other: null },
    text: n.patches && typeof n.patches.pending !== 'number' ? (n.os?.name === 'QTS' ? `${n.os.name} ${n.os.version ?? ''} – Firmware${data.software.find((s) => s.node === n.id && s.kind === 'os')?.behind === 'none' ? ' aktuell laut Kollektor' : ', Paketzahl nicht prüfbar'}` : `keine Paketzahl erhoben (${n.patches.source ?? 'ohne Quelle'})`) : null,
    unit: 'Pakete',
    note: n.patches?.source ? `Quelle ${n.patches.source}${n.patches.rebootRequired ? ' · Neustart nötig' : ''}` : 'nicht erhoben',
  }));
  if (rows.length) stackedBars(f1.plot, rows, { segments: [{ key: 'security', label: 'Sicherheit', cls: 'bar' }, { key: 'other', label: 'übrige', cls: 'bar--soft' }], format: fmt.int, axisTitle: 'Anzahl ausstehender Pakete' });
  else f1.plot.append(emptyNote('Keine Knoten.'));
  f1.table([{ key: 'label', label: 'Knoten' }, { key: 'sub', label: 'System' }, { key: 'sec', label: 'Sicherheit', num: true, render: (r) => fmt.int(r.values.security) }, { key: 'oth', label: 'Übrige', num: true, render: (r) => fmt.int(r.values.other) }, { key: 'note', label: 'Hinweis' }], rows);
  root.append(card({}, f1.el));

  // EOL
  const eol = eolRows(data);
  const f2 = chartFrame({ title: 'Lebensende der Systeme', accent: 'Lebensende', sub: 'Jede Zeile eine Komponente: Balken von heute bis zum Supportende (Quelle endoflife.date). Abgelaufenes zeigt den Abstand zum Supportende als gestrichelte Linie bis heute. Vom Hersteller gepflegte Komponenten (z. B. Docker in QTS) erscheinen abgeschwächt.',
    legend: legend([{ swatch: 'var(--chart-series)', label: 'gestützt' }, { swatch: 'var(--state-warn)', label: 'weniger als 180 Tage' }, { swatch: 'var(--state-crit)', label: 'abgelaufen' }, { line: true, dashed: true, swatch: 'var(--chart-hatch)', label: 'Ende unbekannt' }]) });
  if (eol.length) eolTimeline(f2.plot, eol);
  else f2.plot.append(emptyNote('Keine Lebensende-Daten.'));
  f2.table([{ key: 'node', label: 'Knoten' }, { key: 'name', label: 'Komponente' }, { key: 'version', label: 'Version' }, { key: 'eol', label: 'Supportende', fmt: (v) => (v ? fmt.date(v) : 'unbekannt') }, { key: 'eolDays', label: 'Abstand', fmt: (v) => fmt.days(v) }, { key: 'state', label: 'Zustand', render: (r) => statePill(r.state, r.state === 'crit' ? 'abgelaufen' : r.state === 'warn' ? 'bald' : r.state === 'ok' ? 'gestützt' : undefined) }], eol);
  root.append(card({}, f2.el));

  // Versionen
  const c = card({ title: 'Versionen', accent: 'Versionen', sub: 'Installiert gegen aktuell; Abstand als Badge. „Sicherheit“ markiert Updates mit Sicherheitsbezug.' });
  const sw = [...data.software].sort((a, b) => rank(b) - rank(a) || String(a.node).localeCompare(String(b.node)) || String(a.name).localeCompare(String(b.name), 'de'));
  c.append(dataTable([
    { key: 'node', label: 'Knoten' }, { key: 'name', label: 'Komponente', render: (r) => el('span', r.name ?? '–', r.kind ? el('span.small.muted', ` · ${KIND[r.kind] ?? r.kind}`) : null) },
    { key: 'installed', label: 'Installiert', render: (r) => el('span.mono', r.installed ?? '–') }, { key: 'latest', label: 'Aktuell', render: (r) => el('span.mono', r.latest ?? '–') },
    { key: 'behind', label: 'Abstand', render: (r) => { const s = behindState(r.behind, r.security); return el('span.row', badge(BEHIND_WORD[r.behind] ?? 'unbekannt', s === 'ok' ? 'ok' : s === 'unknown' ? 'unknown' : s), r.security ? badge('Sicherheit', 'crit') : null); } },
    { key: 'eol', label: 'Supportende', render: (r) => (r.eol ? el('span.row', mark(eolState(r.eolDays)), fmt.date(r.eol)) : el('span.muted', '–')) },
    { key: 'source', label: 'Quelle / Hinweis', render: (r) => el('span.small.muted', [r.source, r.note, r.vendorManaged ? 'vom Hersteller verwaltet' : null].filter(Boolean).join(' · ') || '–') },
  ], sw, { compact: true, empty: 'Keine Software-Einträge.' }));
  root.append(c);
}
const KIND = { app: 'App', os: 'OS', firmware: 'Firmware', container: 'Image', runtime: 'Runtime' };
function rank(s) { return { major: 4, minor: 3, patch: 2, unknown: 1, none: 0 }[s.behind] ?? 1 + (s.security ? 5 : 0); }
