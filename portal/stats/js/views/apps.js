/**
 * Ansicht „Applikationsüberblick“: Portfolio aller Apps (Firebase-Projekte + Websites) –
 * Portfolio-Matrix (Aktivität × Kosten), Kartenraster mit Zweck, Tarif, Region, Diensten, Domains,
 * Zustand, Zugriffsstand, Kosten, Sparkline; Filter; Gesamtzeile.
 */
import { el, pageHead, chartFrame, card, legend, mark, statePill, badge, emptyNote, segmented, clear, lu } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { norm } from '../lib/state.js';
import { sparkline } from '../charts/sparkline.js';
import { portfolioMatrix, appStats } from '../charts/apps-charts.js';
import { openAppDrawer, serviceBar, opsBar, serviceOf } from './appdrawer.js';

export function render(root, data, ctx) {
  const apps = data.apps;
  const stats = apps.map((a) => ({ a, st: appStats(a) }));
  const fb = apps.filter((a) => a.platform === 'firebase');
  const svc = apps.filter((a) => a.platform === 'service');
  const web = apps.filter((a) => a.platform !== 'firebase' && a.platform !== 'service');
  const missing = apps.filter((a) => a.access === 'missing');
  const cost = stats.reduce((s, x) => s + (x.st.costMonth ?? 0), 0);
  const parts = [fb.length ? `${fb.length} auf Firebase` : null, web.length ? `${web.length} als Website auf dem Droplet` : null, svc.length ? `${svc.length} als ${svc.length === 1 ? 'Dienst' : 'Dienste'} auf dem Spark` : null].filter(Boolean);
  const lede = apps.length ? `${apps.length} Anwendungen: ${parts.join(', ')} · ${missing.length ? `${missing.length} ohne freigegebene Nutzungsdaten · ` : ''}zusammen ≈ ${fmt.num2(cost)} USD pro Monat (Schätzung${svc.length ? ', Spark-Dienste ohne Kosten' : ''}).` : 'Keine Anwendungen geliefert.';
  root.append(pageHead({ title: apps.length ? `${numWord(apps.length)} Anwendungen im Portfolio` : 'Keine Anwendungen', accent: apps.length ? 'Portfolio' : 'Anwendungen', lede }));
  const ins = ctx.insight('apps', { quiet: true }); if (ins) root.append(ins);
  if (!apps.length) { root.append(card({}, emptyNote('apps[] ist leer.'))); return; }

  // Portfolio-Matrix: nur Apps mit Lese-/Kostenmodell (Firebase, Websites). Spark-Dienste haben weder
  // Firestore-Lesezugriffe noch Cloud-Kosten – sie stehen nicht in der Matrix, sondern nur als Hinweis darunter.
  const matrixApps = apps.filter((a) => a.platform !== 'service');
  const f = chartFrame({ title: 'Portfolio-Matrix', accent: 'Matrix', sub: `Jede App ein Punkt: waagrecht die Aktivität (Lesezugriffe pro Tag, logarithmisch), senkrecht die geschätzten Kosten pro Monat. Form und Farbe = Zustand, kleiner Punkt = Spark-Tarif. Apps ohne Nutzungsdaten stehen unter der Achse.${svc.length ? ` Nicht in der Matrix: ${svc.map((a) => a.label ?? a.id).join(', ')} – ${svc.length === 1 ? 'ein lokaler Dienst' : 'lokale Dienste'} auf dem Spark ohne Lesezugriffe und Cloud-Kosten (Kennzahlen in der Karte unten).` : ''}`,
    legend: legend([{ mark: 'ok', label: 'läuft' }, { mark: 'warn', label: 'Achtung' }, { mark: 'crit', label: 'Kritisch' }, { mark: 'unknown', label: 'keine Daten' }, { line: true, dashed: true, swatch: 'var(--chart-threshold)', label: 'Spark-Gratisgrenze' }]) });
  portfolioMatrix(f.plot, matrixApps, { onPick: (a) => openAppDrawer(a) });
  f.table([{ key: 'label', label: 'App' }, { key: 'tier', label: 'Tarif', fmt: (v) => v ?? '–' }, { key: 'r', label: 'Reads/Tag', num: true, render: (r) => fmt.int(appStats(r).readsDay) }, { key: 'c', label: 'USD/Monat', num: true, render: (r) => (appStats(r).costMonth === null ? '–' : fmt.num2(appStats(r).costMonth)) }, { key: 'state', label: 'Zustand', render: (r) => statePill(r.state) }], matrixApps);
  root.append(card({}, f.el));

  // Filter + Raster
  const gridCard = card({ title: 'Alle Anwendungen', accent: 'Alle', sub: 'Sortiert nach Aktivität. Klick auf eine Karte öffnet Kennzahlen, Kontingente, Functions und Aufrufer.' });
  let filter = 'all';
  const FILTERS = { all: () => true, active: (x) => x.st.active === true, idle: (x) => x.st.active === false, blaze: (x) => x.a.tier === 'Blaze', spark: (x) => x.a.tier === 'Spark', missing: (x) => x.a.access === 'missing' };
  const seg = segmented([
    { value: 'all', label: 'Alle', count: apps.length }, { value: 'active', label: 'Aktiv', count: stats.filter(FILTERS.active).length }, { value: 'idle', label: 'Ruhend', count: stats.filter(FILTERS.idle).length },
    { value: 'blaze', label: 'Blaze', count: stats.filter(FILTERS.blaze).length }, { value: 'spark', label: 'Spark', count: stats.filter(FILTERS.spark).length }, { value: 'missing', label: 'Ohne Daten', count: missing.length },
  ], filter, (v) => { filter = v; drawGrid(); }, 'Filter');
  gridCard.querySelector('.card__head').append(el('div.chart__tools', seg.el));
  const grid = el('div.appgrid');
  gridCard.append(grid);
  const total = el('div.apptotal');
  gridCard.append(total);
  root.append(gridCard);
  drawGrid();

  function drawGrid() {
    clear(grid); clear(total);
    const list = stats.filter(FILTERS[filter]).sort((p, q) => (q.st.readsDay ?? -1) - (p.st.readsDay ?? -1) || String(p.a.label).localeCompare(String(q.a.label), 'de'));
    if (!list.length) grid.append(emptyNote('Nichts in dieser Auswahl.'));
    for (const { a, st } of list) grid.append(a.platform === 'service' ? serviceCard(a) : appCard(a, st));
    const sumReads = list.reduce((s, x) => s + (x.st.readsDay ?? 0), 0), sumFn = list.reduce((s, x) => s + (x.st.fnDay ?? 0), 0), sumCost = list.reduce((s, x) => s + (x.st.costMonth ?? 0), 0);
    total.append(el('span.label', 'Gesamt'), el('span', el('strong', fmt.int(sumReads)), ' Reads/Tag'), el('span', el('strong', fmt.int(sumFn)), ' Function-Aufrufe/Tag'), el('span', el('strong', `${fmt.num2(sumCost)} USD`), ' pro Monat (Schätzung)'), el('span.small.muted', [`${list.filter((x) => !x.st.hasData && x.a.platform !== 'service').length} ohne Nutzungsdaten nicht enthalten`, list.some((x) => x.a.platform === 'service') ? `Spark-Dienste (${list.filter((x) => x.a.platform === 'service').map((x) => x.a.label ?? x.a.id).join(', ')}) haben kein Lese-/Kostenmodell` : null].filter(Boolean).join(' · ')));
  }
}

function appCard(a, st) {
  const c = el('article.appcard', { tabindex: '0', role: 'button', 'aria-label': `${a.label}: ${a.role ?? ''}` });
  c.addEventListener('click', () => openAppDrawer(a));
  c.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openAppDrawer(a); } });
  c.append(el('div.appcard__head', mark(a.state), el('span.appcard__name', a.label ?? a.id), badge(a.tier ?? (a.platform === 'web' ? 'Droplet' : '–'), a.tier === 'Blaze' ? 'accent' : 'outline')));
  c.append(el('p.appcard__role', { title: a.role ?? '' }, a.role ?? '–'));
  c.append(el('p.appcard__meta', [a.project, a.region].filter(Boolean).join(' · ') || '–'));
  const dom = (a.hosting ?? []).flatMap((h) => h.names ?? []);
  c.append(el('p.appcard__meta', { title: dom.join(', ') }, lu('globe', 'lu lu--sm'), ' ', dom.length ? dom.join(', ') : 'keine Domain'));
  c.append(serviceBar(a));
  const kv = el('div.appcard__kv');
  kv.append(kvItem('Reads/Tag', st.readsDay === null ? null : fmt.int(st.readsDay)));
  kv.append(kvItem('USD/Monat', st.costMonth === null ? null : fmt.num2(st.costMonth)));
  kv.append(kvItem('Trend 7 T.', typeof a.trend7d === 'number' ? `${a.trend7d > 0 ? '+' : ''}${fmt.num1(a.trend7d)} %` : null));
  c.append(kv);
  const foot = el('div.appcard__foot');
  const sp = el('span.appcard__spark');
  if (a.series?.reads?.length) sparkline(sp, a.series.reads.map((p) => p[1]), { width: 110, height: 28 }); else sparkline(sp, [], { width: 110, height: 28 });
  foot.append(sp);
  foot.append(a.access === 'missing' ? el('span.apps__access', lu('eye-off', 'lu lu--sm'), 'Nutzungsdaten nicht freigegeben') : el('span.small.muted', st.active === null ? 'Aktivität unbekannt' : st.active ? 'aktiv' : 'ruhend'));
  c.append(foot);
  return c;
}
/** Karte für einen lokalen Dienst auf dem Spark (platform: "service"): Betriebszustand statt Firebase-Dienste. */
function serviceCard(a) {
  const mt = a.metrics ?? {};
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const c = el('article.appcard.appcard--service', { tabindex: '0', role: 'button', 'aria-label': `${a.label}: ${a.role ?? ''}` });
  c.addEventListener('click', () => openAppDrawer(a));
  c.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openAppDrawer(a); } });
  c.append(el('div.appcard__head', mark(a.state), el('span.appcard__name', { title: a.label ?? a.id }, a.label ?? a.id), badge('Spark-Dienst', 'outline')));
  c.append(el('p.appcard__role', { title: a.role ?? '' }, a.role ?? '–'));
  const meta = [a.region, a.project].filter(Boolean).join(' · ');
  c.append(el('p.appcard__meta', { title: meta }, meta || '–'));
  const url = a.service?.mcp?.url ?? null;
  c.append(el('p.appcard__meta', { title: url ?? '' }, lu('plug', 'lu lu--sm'), ' ', url ? `MCP ${url.replace(/^https?:\/\//, '')}` : 'MCP-Adresse unbekannt'));
  c.append(opsBar(serviceOf(a)));
  const kv = el('div.appcard__kv');
  const kvT = (label, title, value) => { const k = kvItem(label, value); k.title = title; return k; };
  kv.append(kvT('Einträge', `Einträge gesamt${isNum(mt.projects) ? ` aus ${mt.projects} Projekten` : ''}`, isNum(mt.items) ? fmt.int(mt.items) : null));
  kv.append(kvT('Zeitreihen', 'Zeitreihen mit mindestens zwei Perioden', isNum(mt.series) ? fmt.int(mt.series) : null));
  kv.append(kvT('Aufrufe heute', 'MCP-Werkzeugaufrufe aus Sitzungen heute', isNum(mt.toolCallsToday) ? fmt.int(mt.toolCallsToday) : null));
  if (isNum(mt.exhibits)) {   // Folien und Grafiken (seit 25.10.2026)
    kv.append(kvT('Folien', `Folien und Grafiken in Fundus${isNum(mt.exhibitProjects) ? ` aus ${mt.exhibitProjects} Projekten` : ''}`, fmt.int(mt.exhibits)));
    kv.append(kvT('Versionen', 'Eingelagerte Folien-Versionen gesamt', isNum(mt.exhibitVersions) ? fmt.int(mt.exhibitVersions) : null));
    kv.append(kvT('Neu (7 T.)', 'Neue Folien-Versionen in den letzten 7 Tagen', isNum(mt.exhibitVersions7d) ? fmt.int(mt.exhibitVersions7d) : null));
  }
  c.append(kv);
  const foot = el('div.appcard__foot');
  const calls = (a.series?.toolCalls ?? []).map((p) => (Array.isArray(p) ? p[1] : p?.calls));
  const sp = el('span.appcard__spark', { title: calls.length ? 'MCP-Werkzeugaufrufe pro Tag' : 'noch keine Werkzeugaufrufe protokolliert' });
  sparkline(sp, calls, { width: 110, height: 28 });
  foot.append(sp);
  foot.append(el('span.small.muted.appcard__note', calls.length ? `${fmt.int(calls.reduce((s, v) => s + (isNum(v) ? v : 0), 0))} Aufrufe in ${calls.length} T.` : (isNum(mt.sessionWrites7d) ? `${fmt.int(mt.sessionWrites7d)} Schreibvorgänge in 7 T.` : 'keine Aufrufe protokolliert')));
  c.append(foot);
  return c;
}
function kvItem(label, value) { return el('span.appcard__kvi', el('span.appcard__kvl', label), el('span.appcard__kvv', value ?? el('span.row', mark('unknown'), '–'))); }
function numWord(n) { return ['Keine', 'Eine', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
