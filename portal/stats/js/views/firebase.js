/**
 * Ansicht „Firebase-Monitoring“: über alle Projekte – Heatmap App × Tag, Reads/Writes/Deletes
 * (ECharts mit dataZoom), Functions vs. Fehler, Kostenbalken, Kontingent-Bullets (tarifabhängig),
 * 48-h-Stundenprofil, Top-Aufrufer. Klick auf eine App öffnet das Detail-Panel.
 */
import { el, pageHead, chartFrame, card, legend, mark, badge, emptyNote, dataTable, headline } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { norm, fillState } from '../lib/state.js';
import { bulletRow } from '../charts/bullet.js';
import { stackedBars } from '../charts/bars.js';
import { readsHeatmap, appSeriesE, hourlyProfileE, appStats } from '../charts/apps-charts.js';
import { openAppDrawer } from './appdrawer.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

export function render(root, data, ctx) {
  const apps = data.apps.filter((a) => a.platform === 'firebase');
  const stats = apps.map((a) => ({ a, st: appStats(a) }));
  const withData = stats.filter((x) => x.st.hasData);
  const readsTotal = withData.reduce((s, x) => s + (x.st.readsDay ?? 0), 0);
  const cost = withData.reduce((s, x) => s + (x.st.costMonth ?? 0), 0);
  const over = apps.filter((a) => (a.quotas ?? []).some((q) => norm(q.state) === 'crit'));
  const lede = apps.length ? `${apps.length} Projekte, ${withData.length} mit Nutzungsdaten · zusammen ≈ ${fmt.int(readsTotal)} Lesezugriffe pro Tag und ≈ ${fmt.num2(cost)} USD pro Monat (Schätzung)${over.length ? ` · ${over.map((a) => a.label).join(', ')} am Kontingent` : ''}.` : 'Keine Firebase-Projekte geliefert.';
  root.append(pageHead({ title: over.length ? `${over.length === 1 ? 'Ein Projekt stösst' : `${over.length} Projekte stossen`} an das Kontingent` : apps.length ? `${numWord(apps.length)} Firebase-Projekte im Blick` : 'Firebase ohne Daten', accent: over.length ? 'Kontingent' : apps.length ? 'Blick' : 'Daten', lede }));
  const ins = ctx.insight('firebase', { quiet: true }); if (ins) root.append(ins);
  if (!apps.length) { root.append(card({}, emptyNote('Keine Firebase-Projekte in apps[].'))); return; }
  const pick = (a) => openAppDrawer(a);

  // (a) Heatmap
  const f1 = chartFrame({ title: 'Lesezugriffe je App und Tag', accent: 'Tag', sub: 'Eine Zeile je Projekt, eine Zelle je Tag der letzten 30 Tage. Farbe = Lesezugriffe in logarithmischen Stufen (eine Stufe ≈ Faktor 10), Schraffur = keine Daten. Rechts die Summe über 30 Tage.',
    legend: legend([{ swatch: 'var(--cal-0)', label: '0' }, { swatch: 'var(--cal-1)', label: 'wenig' }, { swatch: 'var(--cal-4)', label: 'viel' }, { hatch: true, label: 'keine Daten' }]) });
  readsHeatmap(f1.plot, apps, { onPick: pick });
  f1.table([{ key: 'label', label: 'App' }, { key: 'reads', label: 'Reads Σ 30 T.', num: true, render: (r) => fmt.int(appStats(r).reads30) }, { key: 'readsDay', label: 'Reads/Tag Ø', num: true, render: (r) => fmt.int(appStats(r).readsDay) }], apps);
  root.append(card({}, f1.el));

  // (b) Reads/Writes/Deletes
  const f2 = chartFrame({ title: 'Lese-, Schreib- und Löschvorgänge', accent: 'Löschvorgänge', sub: 'Je Projekt ein Diagramm (Firestore-Operationen pro Tag, 30 Tage). Der Zoom-Regler unten gilt für alle; der Zeiger ist gekoppelt.' });
  const plot2 = el('div.echart'); f2.plot.append(plot2);
  appSeriesE(plot2, apps, { cols: window.matchMedia('(max-width: 760px)').matches ? 1 : 2 }).then((r) => { if (!r) plot2.replaceChildren(emptyNote('Keine Nutzungsreihen erhoben.')); }).catch((e) => plot2.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${e.message}`)));
  f2.table([{ key: 'label', label: 'App' }, { key: 'r', label: 'Reads/Tag', num: true, render: (r) => fmt.int(appStats(r).readsDay) }, { key: 'w', label: 'Writes/Tag', num: true, render: (r) => fmt.int(appStats(r).writesDay) }], apps);
  root.append(card({}, f2.el));

  const two = el('div.grid.grid--2');
  // (c) Functions
  const fc = card({ title: 'Functions: Ausführungen und Fehler', accent: 'Fehler', sub: 'Je Projekt Ausführungen pro Tag (Ø 7 Tage) mit Fehlerquote; darunter die Functions mit den meisten Fehlern in 24 h.' });
  const fnRows = stats.filter((x) => x.st.fnDay !== null).sort((p, q) => (q.st.fnDay ?? 0) - (p.st.fnDay ?? 0));
  if (!fnRows.length) fc.append(emptyNote('Keine Function-Daten erhoben.'));
  else {
    const b = el('div.bullets');
    for (const { a, st } of fnRows) bulletRow(b, { label: a.label, sub: `${fmt.int(st.fnDay)} Aufrufe/Tag · ${fmt.int(st.fnErrDay ?? 0)} Fehler`, value: (st.errRate ?? 0) * 100, target: 1, max: 5, format: (v) => `${fmt.num2(v)} %`, state: st.errRate === null ? 'unknown' : st.errRate > 0.02 ? 'crit' : st.errRate > 0.01 ? 'warn' : 'ok', valueText: fmt.pct((st.errRate ?? 0) * 100, 2), targetLabel: 'Warnschwelle 1 %' });
    fc.append(b);
    fc.append(el('div.label', 'Functions mit den meisten Fehlern (24 h)'));
    fc.append(functionTable(apps, { top: 8 }));
  }
  two.append(fc);
  // (d) Kosten
  const f4 = chartFrame({ title: 'Kosten je Projekt', accent: 'Kosten', sub: 'Geschätzte Kosten pro Monat in USD (aus den täglichen Schätzwerten hochgerechnet). Spark-Projekte kosten nichts, bis sie an die harte Grenze stossen.' });
  const costRows = stats.filter((x) => x.st.costMonth !== null || x.a.access === 'missing').map((x) => ({ label: x.a.label, sub: x.a.tier ?? '', values: { cost: x.st.costMonth }, unit: 'USD', text: x.st.costMonth === null ? 'Nutzungsdaten nicht freigegeben' : null, note: 'Schätzung' }));
  if (costRows.length) stackedBars(f4.plot, costRows, { segments: [{ key: 'cost', label: 'USD/Monat', cls: 'bar' }], format: (v) => fmt.num2(v), axisTitle: 'USD pro Monat (Schätzung)' });
  else f4.plot.append(emptyNote('Keine Kostendaten.'));
  f4.setHint(`Summe ≈ ${fmt.num2(cost)} USD/Monat · Schätzung aus Nutzung, keine Rechnung.`);
  f4.table([{ key: 'label', label: 'App' }, { key: 'sub', label: 'Tarif' }, { key: 'c', label: 'USD/Monat', num: true, render: (r) => (r.values.cost === null ? '–' : fmt.num2(r.values.cost)) }], costRows);
  two.append(card({}, f4.el));
  root.append(two);

  // (e) Kontingente
  const qc = card({ title: 'Kontingente je Projekt', accent: 'Kontingente', sub: 'Spark: harte Grenze – ab 100 % werden Zugriffe abgelehnt (koralle). Blaze: das Gratiskontingent ist nur eine Markierung – darüber wird abgerechnet (orange).' });
  const withQ = apps.filter((a) => a.quotas?.length);
  if (!withQ.length) qc.append(emptyNote('Keine Kontingentdaten.'));
  for (const a of withQ) { qc.append(el('div.row', { style: { marginTop: '10px' } }, mark(a.state), el('strong', a.label), badge(a.tier ?? '–', a.tier === 'Spark' ? 'outline' : 'accent'), a.freeTierExceeded ? badge('Gratiskontingent überschritten', 'warning') : null)); qc.append(quotaBullets(a)); }
  root.append(qc);

  // (f) Stundenprofil
  const f6 = chartFrame({ title: 'Stundenprofil der letzten 48 Stunden', accent: '48 Stunden', sub: 'Lesezugriffe pro Stunde je Projekt. Ein flaches Profil ohne Nachtabsenkung (orange, „Dauerlast“) deutet auf einen Hintergrundprozess oder eine Schleife – etwa bei einer App, in der eigentlich nichts laufen sollte.' });
  const plot6 = el('div.echart'); f6.plot.append(plot6);
  hourlyProfileE(plot6, apps, { cols: window.matchMedia('(max-width: 760px)').matches ? 1 : 3 }).then((r) => { if (!r) plot6.replaceChildren(emptyNote('Keine Stundendaten erhoben.')); }).catch((e) => plot6.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${e.message}`)));
  f6.table([{ key: 'label', label: 'App' }, { key: 'peak', label: 'Spitze/h', num: true, render: (r) => (r.hourly?.reads?.length ? fmt.int(Math.max(...r.hourly.reads.map((p) => p[1]))) : '–') }], apps);
  root.append(card({}, f6.el));

  // (g) Aufrufer
  const cc = card({ title: 'Wer greift zu', accent: 'zu', sub: 'Top-Aufrufer je Projekt in den letzten 24 Stunden nach Credential-Typ (Service-Account, Web-App, Benutzer).' });
  const grid = el('div.grid.grid--3');
  for (const a of apps) { const c = el('div.callers__app', el('div.row', mark(a.state), el('strong', a.label)), callerList(a)); grid.append(c); }
  cc.append(grid);
  root.append(cc);
}

export function quotaBullets(a) {
  const b = el('div.bullets');
  for (const q of a.quotas ?? []) {
    const pct = isNum(q.pct) ? q.pct : (isNum(q.used) && isNum(q.limit) && q.limit > 0 ? (100 * q.used) / q.limit : null);
    const f = q.unit === 'bytes' ? (v) => fmt.bytes(v) : (v) => fmt.int(v);
    const spark = a.tier === 'Spark';
    const st = q.state ? norm(q.state) : pct === null ? 'unknown' : spark ? (pct >= 100 ? 'crit' : pct >= 80 ? 'warn' : 'ok') : (pct >= 100 ? 'warn' : 'ok');
    bulletRow(b, { label: q.label ?? q.id, sub: pct === null ? 'nicht gemessen' : `${fmt.pct(pct, 1)} ${spark ? 'der harten Grenze' : 'des Gratiskontingents'}${q.note ? ` · ${q.note}` : ''}`, value: q.used ?? null, target: q.limit ?? null, max: isNum(q.limit) ? q.limit * 1.6 : null, format: f, state: st,
      valueText: pct === null ? 'keine Daten' : `${f(q.used)} von ${f(q.limit)}${pct >= 100 ? (spark ? ' · Grenze erreicht' : ' · wird abgerechnet') : ''}`, targetLabel: spark ? 'harte Grenze' : 'Gratiskontingent', note: q.note ?? (spark ? 'Spark: harte Grenze' : 'Blaze: Überschreitung wird abgerechnet') });
  }
  return b;
}
export function functionTable(apps, { top = null, compact = false } = {}) {
  const rows = apps.flatMap((a) => (a.functions ?? []).map((f) => ({ ...f, app: a.label })));
  rows.sort((p, q) => (q.errors24h ?? 0) - (p.errors24h ?? 0) || (q.exec24h ?? 0) - (p.exec24h ?? 0));
  const list = top ? rows.slice(0, top) : rows;
  return dataTable([
    ...(compact ? [] : [{ key: 'app', label: 'App' }]), { key: 'name', label: 'Function' }, { key: 'exec24h', label: 'Aufrufe 24 h', num: true, fmt: fmt.int }, { key: 'errors24h', label: 'Fehler 24 h', num: true, render: (r) => el('span.row', mark(r.errors24h > 0 && r.exec24h && r.errors24h / r.exec24h > 0.02 ? 'crit' : r.errors24h > 0 ? 'warn' : 'ok'), fmt.int(r.errors24h)) },
    { key: 'rate', label: 'Quote', num: true, render: (r) => (r.exec24h ? fmt.pct((100 * (r.errors24h ?? 0)) / r.exec24h, 2) : '–') }, { key: 'runtime', label: 'Laufzeit', render: (r) => el('span', { title: [r.runtime, r.generation, r.region].filter(Boolean).join(' · ') }, r.runtime ?? '–') },
  ], list, { compact: true, empty: 'Keine Functions.' });
}
export function callerList(a) {
  const ul = el('ul.callers');
  const list = (a.callers ?? []).slice().sort((p, q) => (q.requests24h ?? 0) - (p.requests24h ?? 0)).slice(0, 5);
  const max = Math.max(1, ...list.map((c) => c.requests24h ?? 0));
  for (const c of list) ul.append(el('li.callers__item', badge({ 'service-account': 'Service-Account', 'web-app': 'Web-App', user: 'Benutzer', 'api-key': 'API-Key' }[c.type] ?? c.type ?? '–', 'outline'), el('span.callers__name', { title: c.name ?? '' }, c.name ?? '–'), el('span.callers__bar', el('span.callers__fill', { style: { width: `${(100 * (c.requests24h ?? 0)) / max}%` } })), el('span.callers__num', fmt.int(c.requests24h))));
  if (!list.length) ul.append(el('li.small.muted', a.access === 'missing' ? 'Nutzungsdaten nicht freigegeben' : 'keine Aufrufer erhoben'));
  return ul;
}
function numWord(n) { return ['Keine', 'Ein', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
export { headline, fillState };
