/**
 * Ansicht „Fundus“ (#fundus): Kennzahlen des zentralen Evidenzarchivs auf dem Spark
 * (SQLite + MCP-Server). Quelle: landscape.json → fundus (erzeugt von fundus/metrics.py, stündlich).
 *
 * Aufbau: Kopfsatz + Kennzahlen · Zusammensetzung (Art × Status, Sichtbarkeit) · Projekte (+ Projekt × Art) ·
 * Folien und Grafiken (Exhibits: je Projekt, Versionen je Tag, Art, Grafiktypen, Dichte, Wiederverwendung) ·
 * Branchen × Themen, Regionen, Branchen · Zeitreihen (Perioden, Spannweite, Aktualität, längste Reihen) ·
 * Abdeckung über die Zeit (Kleinvielfache, gemeinsame Jahresachse, je eigene y-Skala) · Prognosen ·
 * Quellen · Datenqualität · Aktivität (30 Tage) · Betrieb (gemeinsamer Baustein mit dem Detail-Panel).
 *
 * Balken sind HTML (Raster mit minmax(0, …), Namen mit „…“ und title) – robust in Chrome und Safari.
 * Säulen und Kleinvielfache sind D3 in echten Pixeln; jede Achsenbeschriftung wird gemessen und
 * gekürzt bzw. ausgedünnt. Farben nur über Tokens/CSS-Klassen (Dark Mode ohne Neuzeichnen).
 * Fehlt fundus (Kollektor gescheitert), erscheint ein Fehlerzustand mit dem Kollektor-Text.
 */
import { el, pageHead, chartFrame, card, legend, mark, emptyNote, dataTable, dl, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { responsive, svgPx } from '../lib/chart.js';
import { fitOne, wrapText, measure } from '../lib/svgtext.js';
import { sparkline } from '../charts/sparkline.js';
import { opsList, opsState } from './appdrawer.js';

const d3 = globalThis.d3;
const FUNDUS_PORTAL = 'http://127.0.0.1:8010/portal/';   // eigenständiges Fundus-Portal (Fundus-Server, nur im Tailnet)
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const arr = (v) => (Array.isArray(v) ? v : []);
const sum = (xs) => xs.reduce((s, v) => s + (isNum(v) ? v : 0), 0);
const share = (n, base) => (isNum(n) && isNum(base) && base > 0 ? n / base : null);

const KIND = { observation: 'Beobachtungen', forecast: 'Prognosen', study: 'Studien', quote: 'Zitate', practice: 'Praktiken', fact: 'Fakten' };
const KIND_ORDER = ['observation', 'fact', 'quote', 'study', 'practice', 'forecast'];
const STATUS = [
  { key: 'verified', label: 'verifiziert', cls: 'fx-seg--ok', swatch: 'var(--state-ok)', state: 'ok' },
  { key: 'unverified', label: 'unverifiziert', cls: 'fx-seg--unknown', hatch: true, state: 'unknown' },
  { key: 'disputed', label: 'umstritten', cls: 'fx-seg--warn', swatch: 'var(--state-warn)', state: 'warn' },
  { key: 'retracted', label: 'zurückgezogen', cls: 'fx-seg--off', swatch: 'var(--state-off)', state: 'off' },
];
// Reihenfolge im Stapel geprüft (dataviz-Validator): Orange nie neben Koralle oder Grün, Grau trennt Grün und Koralle.
const OUTCOME = [
  { key: 'due', label: 'fällig', cls: 'fx-seg--warn', swatch: 'var(--state-warn)' },
  { key: 'open', label: 'offen', cls: 'fx-seg--series', swatch: 'var(--chart-series)' },
  { key: 'hit', label: 'getroffen', cls: 'fx-seg--ok', swatch: 'var(--state-ok)' },
  { key: 'partly', label: 'teilweise', cls: 'fx-seg--ok-hatch', swatch: 'var(--state-ok)', hatch: true },
  { key: 'unresolvable', label: 'nicht entscheidbar', cls: 'fx-seg--off', swatch: 'var(--state-off)' },
  { key: 'miss', label: 'verfehlt', cls: 'fx-seg--crit', swatch: 'var(--state-crit)' },
];
const PUB_TYPE = { academic: 'Wissenschaft', media: 'Medien', vendor: 'Anbieter', company: 'Unternehmen', official: 'Amtliche Stellen', association: 'Verbände', consultancy: 'Beratungen', analyst: 'Analysten', ngo: 'NGO', other: 'Sonstige', unbekannt: 'unbekannt' };
const DOC_KIND = { article: 'Artikel', paper: 'Fachaufsatz', report: 'Bericht', website: 'Website', survey_report: 'Umfragebericht', press_release: 'Medienmitteilung', other: 'Sonstige', unbekannt: 'unbekannt', dataset: 'Datensatz', book: 'Buch', video: 'Video', podcast: 'Podcast', interview: 'Interview' };

export function render(root, data, ctx) {
  const f = data.fundus && typeof data.fundus === 'object' ? data.fundus : null;
  const app = data.apps.find((a) => a.platform === 'service' && a.id === 'fundus') ?? null;
  if (!f) return renderUnavailable(root, data, app);

  const totals = f.totals ?? {};
  const items = isNum(f.itemsTotal) ? f.itemsTotal : sum(KIND_ORDER.map((k) => totals[k]));
  const projects = arr(f.byProject);
  const q = f.quality ?? {};
  const ser = f.series ?? {};
  const fc = f.forecasts ?? {};
  const src = f.sources ?? {};
  const act = f.activity ?? {};
  const svc = healthOf(f, app);
  const now = fmt.parseDate(f.generatedAt) ?? new Date();
  const verified = sum(arr(f.byKindStatus).map((r) => r.verified));

  // ---------- 1 Kopf + Kennzahlen
  root.append(pageHead({
    title: items ? `Fundus hält ${fmt.int(items)} Belege aus ${fmt.int(projects.length)} Projekten` : 'Fundus ist noch leer',
    accent: items ? 'Belege' : 'leer',
    lede: [
      `${fmt.int(items)} Einträge in sechs Arten`,
      isNum(src.documents) ? `${fmt.int(src.documents)} Quellendokumente von ${fmt.int(totals.publisher)} Verlagen und Herausgebern` : null,
      isNum(q.verifiedShare) ? `${fmt.pct(q.verifiedShare * 100, 1)} verifiziert` : null,
      isNum(fc.byOutcome?.due) ? `${fmt.int(fc.byOutcome.due)} Prognosen fällig` : null,
    ].filter(Boolean).join(' · ') + `. Stand ${fmt.dateTime(f.generatedAt)} (${fmt.ago(f.generatedAt)})${isNum(f.db?.bytes) ? `, Datenbank ${fmt.bytes(f.db.bytes)}` : ''}.`,
  }));
  const ins = ctx.insight('fundus', { quiet: true }); if (ins) root.append(ins);

  const callsByDay = arr(act.toolCallsByDay);
  const today = String(f.generatedAt ?? '').slice(0, 10);
  const callsToday = isNum(app?.metrics?.toolCallsToday) ? app.metrics.toolCallsToday : (callsByDay.find((d) => d.date === today)?.calls ?? (callsByDay.length ? 0 : null));
  const grid = el('div.grid.grid--kpi.fx-kpis');
  grid.append(kpi({ label: 'Einträge gesamt', value: fmt.int(items), foot: `aus ${fmt.int(projects.length)} Projekten · ${fmt.int(totals.series)} Zeitreihen`, state: items ? 'ok' : 'unknown' }));
  grid.append(kpi({ label: 'Reihen mit ≥ 2 Perioden', value: fmt.int(ser.multiPeriod), foot: `von ${fmt.int(ser.total)} Reihen · ${fmt.int(ser.dueForCheck)} zur Aktualisierung fällig`, state: isNum(ser.dueForCheck) && ser.dueForCheck > 0 ? 'warn' : isNum(ser.multiPeriod) ? 'ok' : 'unknown' }));
  grid.append(kpi({ label: 'Anteil verifiziert', value: isNum(q.verifiedShare) ? fmt.num1(q.verifiedShare * 100) : '–', unit: '%', meter: isNum(q.verifiedShare) ? q.verifiedShare : null, foot: `${fmt.int(verified)} von ${fmt.int(items)} · ${fmt.int(q.disputed)} umstritten`, state: isNum(q.verifiedShare) ? 'ok' : 'unknown' }));
  const dsm = f.datasets && !f.datasets.error ? f.datasets : null;
  if (dsm) {
    grid.append(kpi({ label: 'Datensätze', value: fmt.int(dsm.datasets), spark: arr(dsm.callsByDay).map((d) => d.add + d.search + d.use),
      foot: dsm.datasets ? `${fmt.int(dsm.tables)} Tabellen · ${fmt.int(dsm.rows)} Zeilen · ${fmt.bytes(dsm.bytes_stored)}${arr(dsm.updateDue).length ? ` · ${fmt.int(dsm.updateDue.length)} Update fällig` : ''}` : 'noch keine abgelegt',
      state: dsm.failed || arr(dsm.updateDue).length ? 'warn' : dsm.datasets ? 'ok' : 'unknown' }));
  }
  const cl = f.cleanup && !f.cleanup.error ? f.cleanup : null;
  if (cl) {
    const sc = isNum(cl.score) ? cl.score : null;
    grid.append(kpi({ label: 'Datenqualität', value: sc === null ? '–' : fmt.num1(sc * 100), unit: '%', meter: sc,
      foot: sc === null ? 'noch nicht geprüft' : `${fmt.int(cl.affected)} von ${fmt.int(cl.total)} Datensätzen mit offenem Befund · ${fmt.int(cl.open_decisions)} zur Entscheidung`,
      state: sc === null ? 'unknown' : sc >= 0.98 ? 'ok' : sc >= 0.9 ? 'warn' : 'crit' }));
  }
  grid.append(kpi({ label: 'Fällige Prognosen', value: fmt.int(fc.byOutcome?.due), foot: `von ${fmt.int(fc.total)} · ${fmt.int(fc.pastTargetUnresolved)} mit Zieljahr vor ${now.getFullYear()} noch offen`, state: isNum(fc.byOutcome?.due) ? (fc.byOutcome.due > 0 ? 'warn' : 'ok') : 'unknown' }));
  grid.append(kpi({ label: 'Quellendokumente', value: fmt.int(src.documents), foot: `${fmt.int(totals.publisher)} Verlage · Top 10 liefern ${isNum(src.top10Share) ? fmt.pct(src.top10Share * 100) : '–'} der Einträge`, state: isNum(src.documents) ? 'ok' : 'unknown' }));
  grid.append(kpi({ label: 'Sitzungs-Zugriffe heute', value: isNum(callsToday) ? fmt.int(callsToday) : '–', spark: callsByDay.map((d) => d.calls), foot: `${fmt.int(act.sessionWrites7d)} Schreibvorgänge in 7 Tagen${callsByDay.length ? '' : ' · Aufrufe noch nicht protokolliert'}`, state: callsByDay.length ? 'ok' : 'unknown' }));
  const ex = f.exhibits && typeof f.exhibits === 'object' ? f.exhibits : null;
  const exUse = ex?.usage ?? {};
  const exHits30 = sum(arr(exUse.searchHitsByDay).map((d) => d.searches));
  grid.append(kpi({ label: 'Folien und Grafiken', value: ex ? fmt.int(ex.logical) : '–', spark: ex ? arr(ex.versionsByDay).map((d) => d.n) : null,
    foot: ex ? `${fmt.int(ex.versions)} Versionen aus ${fmt.int(arr(ex.byProject).length)} Projekten · ${fmt.int(ex.versions7d)} neu in 7 T.` : 'noch nicht geliefert (ältere Fundus-Version)', state: ex ? (ex.logical ? 'ok' : 'unknown') : 'unknown' }));
  grid.append(kpi({ label: 'Folien geöffnet (30 T.)', value: ex ? fmt.int(exUse.views30d) : '–', spark: ex ? arr(exUse.exhibitCallsByDay).map((d) => d.calls) : null,
    foot: ex ? `${fmt.int(exHits30)} Suchen mit Folien-Treffer · ${fmt.int(exUse.kindFilterSearches30d)} gezielt nach Folien` : 'keine Nutzungsdaten', state: ex ? 'ok' : 'unknown' }));
  root.append(grid);

  // ---------- 2 Zusammensetzung · 3 Projekte
  root.append(sectionLabel('Bestand'));
  const two = el('div.grid.grid--2');
  const kindRows = KIND_ORDER.map((k) => arr(f.byKindStatus).find((r) => r.kind === k) ?? null).filter(Boolean)
    .map((r) => ({ label: KIND[r.kind] ?? r.kind, values: Object.fromEntries(STATUS.map((s) => [s.key, r[s.key] ?? 0])) }));
  const f2 = chartFrame({ title: 'Zusammensetzung nach Art und Status', accent: 'Art', sub: 'Einträge je Art, gestapelt nach Prüfstatus. Verifiziert heisst: Wert und Quelle wurden gegen das Dokument geprüft.', legend: statusLegend() });
  if (kindRows.length) hbars(f2.plot, kindRows, { segments: STATUS, format: fmt.int, sort: true, valueText: (r) => fmt.int(sum(Object.values(r.values))), sub: (r) => `${fmt.pct(share(r.values.verified, sum(Object.values(r.values))) * 100)} verifiziert` });
  else f2.plot.append(emptyNote('Keine Angaben je Art.'));
  const vis = f.visibility ?? {};
  if (isNum(vis.public) || isNum(vis.internal)) f2.plot.append(visibilityBar(vis));
  f2.table([{ key: 'label', label: 'Art' }, ...STATUS.map((s) => ({ key: s.key, label: s.label, num: true, render: (r) => fmt.int(r.values[s.key]) })), { key: 't', label: 'Gesamt', num: true, render: (r) => fmt.int(sum(Object.values(r.values))) }], kindRows);
  two.append(card({}, f2.el));

  const projRows = projects.map((p) => {
    const ver = isNum(p.verifiedShare) ? Math.round(p.verifiedShare * p.items) : null;
    return { label: p.project, values: { verified: ver ?? 0, unverified: ver === null ? p.items : p.items - ver }, items: p.items, vs: p.verifiedShare, byKind: p.byKind ?? {} };
  });
  const f3 = chartFrame({ title: 'Einträge je Projekt', accent: 'Projekt', sub: 'Herkunftsprojekt der Einträge (Zusammenführungen nicht doppelt gezählt). Grün = verifizierter Anteil, schraffiert = noch nicht verifiziert.',
    legend: legend([{ swatch: 'var(--state-ok)', label: 'verifiziert' }, { hatch: true, label: 'nicht verifiziert' }]) });
  if (projRows.length) hbars(f3.plot, projRows, { segments: [STATUS[0], { ...STATUS[1], label: 'nicht verifiziert' }], format: fmt.int, valueText: (r) => fmt.int(r.items), sub: (r) => (isNum(r.vs) ? `${fmt.pct(r.vs * 100)} verifiziert` : 'Anteil unbekannt'), tipExtra: (r) => [['Anteil verifiziert', isNum(r.vs) ? fmt.pct(r.vs * 100, 1) : null], ...KIND_ORDER.filter((k) => r.byKind[k]).map((k) => [KIND[k], fmt.int(r.byKind[k])])] });
  else f3.plot.append(emptyNote('Keine Projekte geliefert.'));
  f3.table([{ key: 'label', label: 'Projekt' }, { key: 'items', label: 'Einträge', num: true, fmt: fmt.int }, { key: 'vs', label: 'verifiziert', num: true, render: (r) => (isNum(r.vs) ? fmt.pct(r.vs * 100, 1) : '–') }], projRows);
  two.append(card({}, f3.el));
  root.append(two);

  // Projekt × Art (statt 6-farbiger Stapel: Werte lesbar, auch für kleine Projekte)
  if (projRows.length) {
    const kinds = KIND_ORDER.filter((k) => projRows.some((r) => r.byKind[k]));
    const fpk = chartFrame({ title: 'Projekt × Art', accent: 'Art', sub: 'Welche Projekte welche Arten von Belegen liefern. Je dunkler, desto mehr Einträge (Wurzel-Skala, damit kleine Projekte sichtbar bleiben); leere Zellen = keine.', legend: heatLegend() });
    heatTable(fpk.plot, { rows: projRows.map((r) => ({ label: r.label })), cols: kinds.map((k) => ({ label: KIND[k] })), values: projRows.map((r) => kinds.map((k) => r.byKind[k] ?? 0)), corner: 'Projekt', vertical: false, minWidth: 520 });
    fpk.table([{ key: 'label', label: 'Projekt' }, ...kinds.map((k) => ({ key: k, label: KIND[k], num: true, render: (r) => fmt.int(r.byKind[k] ?? 0) }))], projRows);
    root.append(card({}, fpk.el));
  }

  // ---------- 3b Folien und Grafiken (Exhibits)
  root.append(sectionLabel('Folien und Grafiken'));
  root.append(el('div.row.fx-portal', el('a.btn.btn--primary', { href: FUNDUS_PORTAL, target: '_blank', rel: 'noopener' }, 'Folien im Fundus-Portal ansehen'),
    el('span.small.muted', 'Eigenständiges Portal auf dem Spark: Vorschauen je Projekt und Foliensatz, Grossansicht, Sichtbarkeit für andere Projekte.')));
  if (ex) renderExhibits(root, ex, now);
  else root.append(card({}, emptyNote('Fundus liefert noch keine Folien-Kennzahlen (Block „exhibits“ fehlt) – vermutlich läuft auf dem Spark eine ältere Fundus-Version.')));

  if (dsm) renderDatasets(root, dsm);

  // ---------- 4 Branchen × Themen · Regionen · Branchen
  root.append(sectionLabel('Themen und Regionen'));
  const mx = f.matrix ?? {};
  const fm = chartFrame({ title: 'Branchen × Themen', accent: 'Themen', sub: `Die zehn häufigsten Branchen gegen die zwölf häufigsten Themen: Anzahl Einträge mit beiden Schlagworten. Ein Eintrag kann mehrere Themen tragen. Wortschatz: ${fmt.int(f.vocab?.industry)} Branchen, ${fmt.int(f.vocab?.topic)} Themen, ${fmt.int(f.vocab?.region)} Regionen.`, legend: heatLegend() });
  if (arr(mx.rows).length && arr(mx.cols).length) {
    heatTable(fm.plot, { rows: mx.rows, cols: mx.cols, values: arr(mx.values), corner: 'Branche', vertical: true, minWidth: 640 });
    fm.table([{ key: 'label', label: 'Branche' }, ...mx.cols.map((c, j) => ({ key: `c${j}`, label: c.label ?? c.value, num: true, render: (r) => fmt.int(r.vals[j]) }))], mx.rows.map((r, i) => ({ label: r.label ?? r.value, vals: arr(mx.values[i]) })));
  } else fm.plot.append(emptyNote('Keine Schlagwort-Matrix geliefert.'));
  root.append(card({}, fm.el));

  const four = el('div.grid.grid--2');
  four.append(card({}, simpleBars('Regionen', 'Regionen', 'Einträge je Regionen-Schlagwort (die 12 häufigsten). Länder und ihre Grossregion zählen getrennt.', arr(f.byRegion).slice(0, 12).map((r) => ({ label: r.label ?? r.value, n: r.items, sub: r.parent ? `gehört zu ${labelOf(f.byRegion, r.parent)}` : null })), 'Region').el));
  four.append(card({}, simpleBars('Branchen', 'Branchen', 'Einträge je Branchen-Schlagwort, alle Branchen. Unterbranchen (z. B. Autohandel unter Automobil) zählen getrennt.', arr(f.byIndustry).map((r) => ({ label: r.label ?? r.value, n: r.items, sub: r.parent ? `unter ${labelOf(f.byIndustry, r.parent)}` : null })), 'Branche').el));
  root.append(four);

  // ---------- 5 Zeitreihen
  root.append(sectionLabel('Zeitreihen'));
  root.append(el('div.fx-stats', [
    stat('Zeitreihen', fmt.int(ser.total), `${fmt.int(ser.withObservations)} mit Beobachtungen`),
    stat('mit ≥ 2 Perioden', fmt.int(ser.multiPeriod), isNum(share(ser.multiPeriod, ser.total)) ? `${fmt.pct(share(ser.multiPeriod, ser.total) * 100)} aller Reihen` : null),
    stat('Prüfung fällig', fmt.int(ser.dueForCheck), 'neue Periode erwartet oder nie geprüft', isNum(ser.dueForCheck) && ser.dueForCheck > 0 ? 'warn' : 'ok'),
    stat('nie geprüft', fmt.int(ser.neverChecked), 'noch kein Abgleich mit der Quelle', isNum(ser.neverChecked) && ser.neverChecked > 0 ? 'warn' : 'ok'),
  ]));
  const three = el('div.grid.grid--3');
  const hist = arr(ser.periodsHistogram).map((h) => ({ label: h.periods, value: h.series, cls: h.periods === '1' ? 'fx-col--soft' : 'fx-col' }));
  three.append(card({}, colFrame('Perioden je Reihe', 'Perioden', 'Wie viele Zeitpunkte jede Reihe hat. Blass = nur eine Periode (noch keine Reihe im eigentlichen Sinn).', hist, 'Perioden', 'Reihen',
    legend([{ swatch: 'var(--chart-series)', label: '≥ 2 Perioden' }, { swatch: 'var(--chart-series-2)', label: '1 Periode' }])).el));
  three.append(card({}, colFrame('Spannweite in Jahren', 'Spannweite', 'Abstand zwischen erster und letzter Periode, nur Reihen mit mindestens zwei Perioden.', arr(ser.spanYears).map((h) => ({ label: h.span === '0' ? '< 1' : h.span, value: h.series, cls: 'fx-col' })), 'Jahre', 'Reihen').el));
  three.append(card({}, colFrame('Aktualität der letzten Periode', 'Aktualität', `Alter der jüngsten Periode je Reihe, gemessen am Jahr ${now.getFullYear()}. Schraffiert = Reihe ohne auswertbare Periode.`,
    arr(ser.freshness).map((h) => ({ label: bucketLabel(h.bucket), value: h.series, cls: /ohne/.test(h.bucket) ? 'fx-col--hatch' : 'fx-col' })), 'Alter', 'Reihen').el));
  root.append(three);
  const topCard = card({ title: 'Längste Zeitreihen', accent: 'Längste', sub: 'Die zwölf Reihen mit den meisten Perioden.' });
  topCard.append(dataTable([
    { key: 'name', label: 'Reihe', render: (r) => el('span.fx-cell-clamp', { title: `${r.name ?? r.id}${r.id ? ` (${r.id})` : ''}` }, r.name ?? r.id) },
    { key: 'publisher', label: 'Herausgeber', render: (r) => el('span.fx-cell-clamp', { title: r.publisher ?? '' }, r.publisher ?? '–') },
    { key: 'periods', label: 'Perioden', num: true, fmt: fmt.int },
    { key: 'range', label: 'Zeitraum', render: (r) => `${r.first ?? '?'} – ${r.last ?? '?'}` },
    { key: 'unit', label: 'Einheit', render: (r) => r.unit ?? '–' },
  ], arr(ser.top), { compact: true, empty: 'Keine Reihen mit mindestens zwei Perioden.' }));
  root.append(topCard);

  // ---------- 6 Abdeckung über die Zeit
  root.append(sectionLabel('Abdeckung über die Zeit'));
  const cov = f.coverage ?? {};
  const covPanels = [
    { key: 'obs', title: 'Beobachtungen nach Periodenjahr', rows: arr(cov.observationsByYear) },
    { key: 'doc', title: 'Dokumente nach Erscheinungsjahr', rows: arr(cov.documentsByPublishedYear) },
    { key: 'fc', title: 'Prognosen nach Zieljahr', rows: arr(cov.forecastTargetYears) },
  ];
  const allYears = covPanels.flatMap((p) => p.rows.map((r) => r.year)).filter(isNum);
  const f6 = chartFrame({ title: 'Wofür Fundus Belege hat', accent: 'Belege', sub: `Drei Kleinvielfache auf derselben Jahresachse, jede mit eigener Skala (die Höchstwerte stehen rechts im Titel). Linie = ${now.getFullYear()}. Zeitpunkte vor 1990 und mehr als 15 Jahre voraus sind nicht gezählt.` });
  if (allYears.length) {
    const y0 = Math.min(...allYears), y1 = Math.max(...allYears, now.getFullYear());
    const cats = d3.range(y0, y1 + 1);
    panelColumns(f6.plot, { cats, catLabel: String, tickEvery: 5, marker: now.getFullYear(), panels: covPanels.map((p) => ({ title: p.title, values: new Map(p.rows.map((r) => [r.year, r.n])) })), tipTitle: (c) => `Jahr ${c}` });
    f6.table([{ key: 'year', label: 'Jahr' }, ...covPanels.map((p) => ({ key: p.key, label: p.title, num: true, render: (r) => fmt.int(r[p.key] ?? 0) }))],
      cats.map((y) => ({ year: y, ...Object.fromEntries(covPanels.map((p) => [p.key, p.rows.find((r) => r.year === y)?.n ?? 0])) })).filter((r) => covPanels.some((p) => r[p.key])).reverse());
  } else f6.plot.append(emptyNote('Keine Jahresangaben geliefert.'));
  root.append(card({}, f6.el));

  // ---------- 7 Prognosen
  root.append(sectionLabel('Prognosen'));
  const seven = el('div.grid.grid--2');
  const byT = arr(fc.byTargetYear).filter((r) => isNum(r.year));
  const present = OUTCOME.filter((o) => byT.some((r) => r[o.key] > 0));
  const f7 = chartFrame({ title: 'Prognosen nach Zieljahr', accent: 'Zieljahr', sub: `Je Zieljahr gestapelt nach Ergebnis. Grau hinterlegt = Zieljahr vorbei; dort sollte nichts mehr „offen“ sein.${byT.some((r) => r.year > now.getFullYear() + 12) ? ` Zieljahre nach ${now.getFullYear() + 12} sind zu „später“ zusammengefasst.` : ''}`,
    legend: legend(present.map((o) => (o.hatch ? { hatch: true, label: o.label } : { swatch: o.swatch, label: o.label }))) });
  if (byT.length) {
    const cut = now.getFullYear() + 12;
    const yMin = Math.min(...byT.map((r) => r.year)), yMax = Math.min(cut, Math.max(...byT.map((r) => r.year)));
    const cats = d3.range(yMin, yMax + 1).map(String);
    const rowsMap = new Map(cats.map((c) => [c, Object.fromEntries(OUTCOME.map((o) => [o.key, 0]))]));
    const later = byT.filter((r) => r.year > cut);
    if (later.length) { cats.push('später'); rowsMap.set('später', Object.fromEntries(OUTCOME.map((o) => [o.key, 0]))); }
    for (const r of byT) { const k = r.year > cut ? 'später' : String(r.year); const t = rowsMap.get(k); for (const o of OUTCOME) t[o.key] += r[o.key] ?? 0; }
    stackedColumns(f7.plot, { cats, rows: rowsMap, segments: present.length ? present : OUTCOME.slice(0, 2), shadeBefore: String(now.getFullYear()), shadeLabel: 'Zieljahr vorbei', tickEvery: 2, tipTitle: (c) => (c === 'später' ? `Zieljahr nach ${cut}` : `Zieljahr ${c}`) });
    f7.table([{ key: 'year', label: 'Zieljahr' }, ...OUTCOME.map((o) => ({ key: o.key, label: o.label, num: true, fmt: fmt.int }))], byT);
  } else f7.plot.append(emptyNote('Keine Prognosen mit Zieljahr.'));
  seven.append(card({}, f7.el));

  const res = card({ title: 'Auflösung', accent: 'Auflösung', sub: 'Eine Prognose wird nach dem Zieljahr gegen den eingetretenen Wert geprüft: getroffen, teilweise, verfehlt oder nicht entscheidbar.' });
  const bo = fc.byOutcome ?? {};
  res.append(el('div.fx-stats.fx-stats--tight', [
    stat('gesamt', fmt.int(fc.total), null),
    stat('offen', fmt.int(bo.open), 'Zieljahr noch nicht erreicht'),
    stat('fällig', fmt.int(bo.due), 'wartet auf Auflösung', isNum(bo.due) && bo.due > 0 ? 'warn' : 'ok'),
    stat('aufgelöst', fmt.int(fc.resolved), isNum(fc.resolved) && fc.resolved > 0 ? `${fmt.int(bo.hit)} getroffen · ${fmt.int(bo.partly)} teilweise · ${fmt.int(bo.miss)} verfehlt` : 'noch keine'),
  ]));
  if (isNum(fc.hitRate) && fc.resolved > 0) {
    res.append(el('div.compare', el('div.compare__num', fmt.pct(fc.hitRate * 100)), el('div', el('div', `Trefferquote über ${fmt.int(fc.resolved)} aufgelöste Prognosen`), el('div.small.muted', '„teilweise“ zählt halb; „nicht entscheidbar“ zählt nicht.'))));
  } else res.append(emptyNote(`Noch keine Prognose aufgelöst – die Trefferquote erscheint ab der ersten Auflösung.${isNum(fc.pastTargetUnresolved) && fc.pastTargetUnresolved ? ` ${fmt.int(fc.pastTargetUnresolved)} Prognosen haben ihr Zieljahr bereits hinter sich.` : ''}`));
  res.append(dl([['Mit Zeitreihe verknüpft', isNum(fc.linkedToSeries) ? `${fmt.int(fc.linkedToSeries)} von ${fmt.int(fc.total)} (Auflösung gegen die Reihe möglich)` : null], ['Zieljahr vorbei, unaufgelöst', isNum(fc.pastTargetUnresolved) ? fmt.int(fc.pastTargetUnresolved) : null]]));
  seven.append(res);
  root.append(seven);
  const due = card({ title: 'Fällige Prognosen', accent: 'Fällige', sub: `Öffentliche Prognosen mit Ergebnis „fällig“, älteste Zielperiode zuerst${arr(fc.dueList).length >= 12 ? ' (höchstens zwölf)' : ''}.` });
  due.append(dataTable([
    { key: 'target_period', label: 'Ziel', render: (r) => r.target_period ?? '–' },
    { key: 'statement', label: 'Aussage', render: (r) => el('span.fx-cell-clamp.fx-cell-clamp--wide', { title: r.statement ?? '' }, r.statement ?? '–') },
    { key: 'value', label: 'Wert', num: true, render: (r) => (isNum(r.value) ? `${fmt.num1(r.value).replace(/\.0$/, '')}${r.unit ? ` ${r.unit}` : ''}` : '–') },
    { key: 'who', label: 'Quelle', render: (r) => el('span.fx-cell-clamp', { title: r.who ?? '' }, r.who ?? '–') },
  ], arr(fc.dueList), { compact: true, empty: 'Keine fälligen Prognosen.' }));
  root.append(due);

  // ---------- 8 Quellen
  root.append(sectionLabel('Quellen'));
  const eight = el('div.grid.grid--2');
  const pt = arr(src.publisherTypes).map((r) => ({ label: PUB_TYPE[r.type] ?? r.type, n: r.documents, sub: `${fmt.int(r.publishers)} Verlage` }));
  eight.append(card({}, simpleBars('Dokumente je Verlagstyp', 'Verlagstyp', 'Wer die Quellendokumente herausgibt: Anzahl Dokumente je Typ, darunter die Zahl der Verlage.', pt, 'Typ', { unit: 'Dokumente' }).el));
  const tp = arr(src.topPublishers).map((r) => ({ label: r.name ?? r.id, n: r.items, sub: `${PUB_TYPE[r.type] ?? r.type ?? 'Typ unbekannt'} · ${fmt.int(r.documents)} ${r.documents === 1 ? 'Dokument' : 'Dokumente'}` }));
  eight.append(card({}, simpleBars('Ergiebigste Verlage', 'Verlage', 'Einträge je Verlag (die 15 ergiebigsten). Viele Einträge aus wenigen Dokumenten = grosse Datensammlungen.', tp, 'Verlag', { unit: 'Einträge' }).el));
  root.append(eight);
  const eight2 = el('div.grid.grid--2');
  const dk = Object.entries(src.byKind ?? {}).map(([k, n]) => ({ label: DOC_KIND[k] ?? k, n }));
  eight2.append(card({}, simpleBars('Dokumentarten', 'Dokumentarten', 'Art der Quellendokumente.', dk, 'Art', { unit: 'Dokumente' }).el));
  const ac = card({ title: 'Erschliessung und Konzentration', accent: 'Konzentration', sub: 'Wie gut die Quellen wieder auffindbar sind und wie stark sich die Belege auf wenige Verlage stützen.' });
  const doc = src.documents;
  const sb = el('div.fx-shares');
  sb.append(shareRow('mit URL', src.withUrl, doc, 'Dokumente mit Web-Adresse'));
  sb.append(shareRow('mit DOI', src.withDoi, doc, 'Dokumente mit DOI (vor allem Fachaufsätze)'));
  sb.append(shareRow('von Anbietern', src.vendor, doc, 'Dokumente von Herstellern/Anbietern – Eigeninteresse möglich'));
  sb.append(shareRow('Top-10-Verlage', isNum(src.top10Share) ? src.top10Share : null, 1, 'Anteil der Einträge aus den zehn ergiebigsten Verlagen', true));
  sb.append(shareRow('Verlage mit nur einem Dokument', src.publishersWithOneDocument, totals.publisher, 'Anteil der Verlage, von denen genau ein Dokument stammt'));
  ac.append(sb);
  eight2.append(ac);
  root.append(eight2);

  // ---------- 9 Datenqualität
  root.append(sectionLabel('Datenqualität'));
  if (cl) root.append(renderCleanup(cl, ctx));
  const nine = el('div.grid.grid--2');
  const qc = card({ title: 'Lücken und Konflikte', accent: 'Lücken', sub: 'Anzahl betroffener Einträge und Anteil an der jeweiligen Grundmenge. Balken = Anteil; ab 10 % markiert.' });
  const qrows = [
    ['Beobachtungen ohne Grundgesamtheit', q.observationsWithoutPopulation, totals.observation, 'Beobachtungen'],
    ['Zitate ohne Sprecher', q.quotesWithoutSpeaker, totals.quote, 'Zitate'],
    ['Beobachtungen ohne Periode', q.observationsWithoutPeriod, totals.observation, 'Beobachtungen'],
    ['Beobachtungen ohne Einheit', q.observationsWithoutUnit, totals.observation, 'Beobachtungen'],
    ['Einträge ohne Quelle', q.itemsWithoutSource, items, 'Einträge'],
    ['Umstrittene Einträge', q.disputed, items, 'Einträge'],
    ['Zurückgezogene Einträge', q.retracted, items, 'Einträge'],
  ];
  const ql = el('div.fx-shares');
  for (const [label, n, base, what] of qrows) ql.append(shareRow(label, n, base, `${fmt.int(n)} von ${fmt.int(base)} ${what}`, false, true));
  qc.append(ql);
  qc.append(el('p.small.muted', `${fmt.int(q.mergedDuplicates)} Dubletten wurden beim Import zusammengeführt (Herkunft bleibt je Projekt erhalten).`));
  nine.append(qc);
  const units = arr(q.unitsTop).map((u) => ({ label: u.unit, n: u.n }));
  const uf = simpleBars('Einheiten der Beobachtungen', 'Einheiten', `${fmt.int(q.distinctUnits)} verschiedene Einheiten; hier die zwölf häufigsten. Viele Schreibweisen derselben Grösse (z. B. „USD m“ und „USD bn“) erschweren Vergleiche.`, units, 'Einheit', { unit: 'Beob.' });
  nine.append(card({}, uf.el));
  root.append(nine);

  // ---------- 10 Aktivität (30 Tage)
  root.append(sectionLabel('Aktivität (30 Tage)'));
  const ten = el('div.grid.grid--2');
  const end = d3.timeDay.floor(now);
  const days = d3.timeDays(d3.timeDay.offset(end, -29), d3.timeDay.offset(end, 1)).map((d) => d3.timeFormat('%Y-%m-%d')(d));
  const actDays = arr(act.days);
  const f10 = chartFrame({ title: 'Änderungen je Tag', accent: 'Änderungen', sub: 'Einträge im Änderungsprotokoll, getrennt nach Herkunft: Importe aus den Projekten, Sitzungen über den MCP-Server, Wartung (Zusammenführen, Prüfen). Jede Zeile hat ihre eigene Skala.' });
  if (actDays.length) {
    const pick = (k) => new Map(actDays.map((d) => [d.date, d[k] ?? 0]));
    panelColumns(f10.plot, { cats: days, catLabel: (c) => fmt.dateShort(c), tickEvery: 7, panels: [{ title: 'Importe', values: pick('imports') }, { title: 'Sitzungen (MCP)', values: pick('sessions') }, { title: 'Wartung', values: pick('maintenance') }], tipTitle: (c) => fmt.date(c), panelH: 64 });
    if (actDays.length < 3) f10.setHint(`Erst ${actDays.length === 1 ? 'ein Tag' : `${actDays.length} Tage`} mit Änderungen protokolliert (seit ${fmt.date(actDays[0].date)}).`);
  } else f10.plot.append(emptyNote('Keine Änderungen in den letzten 30 Tagen.'));
  f10.table([{ key: 'date', label: 'Tag', render: (r) => fmt.date(r.date) }, { key: 'imports', label: 'Importe', num: true, fmt: fmt.int }, { key: 'sessions', label: 'Sitzungen', num: true, fmt: fmt.int }, { key: 'maintenance', label: 'Wartung', num: true, fmt: fmt.int }], [...actDays].reverse());
  ten.append(card({}, f10.el));
  const byTool = arr(act.toolCalls30d);
  const f10b = chartFrame({ title: 'MCP-Werkzeugaufrufe', accent: 'Werkzeugaufrufe', sub: 'Wie oft Sitzungen Fundus über den MCP-Server abfragen – je Tag und je Werkzeug (30 Tage).' });
  if (callsByDay.length) {
    panelColumns(f10b.plot, { cats: days, catLabel: (c) => fmt.dateShort(c), tickEvery: 7, panels: [{ title: 'Aufrufe pro Tag', values: new Map(callsByDay.map((d) => [d.date, d.calls])) }], tipTitle: (c) => fmt.date(c), panelH: 72 });
  }
  if (byTool.length) hbars(f10b.plot, byTool.map((t) => ({ label: t.tool, values: { n: t.calls } })), { segments: [{ key: 'n', label: 'Aufrufe', cls: 'fx-seg--series' }], format: fmt.int, sort: true });
  if (!callsByDay.length && !byTool.length) f10b.plot.append(emptyNote('Noch keine Werkzeugaufrufe protokolliert. Die Zählung beginnt mit dem ersten Aufruf nach Einführung der Nutzungstabelle – fehlende Balken heissen hier „nicht erfasst“, nicht „null Nutzung“.'));
  f10b.plot.append(dl([
    ['Letzter Import', act.lastImport ? `${fmt.dateTime(act.lastImport)} (${fmt.ago(act.lastImport)})` : null],
    ['Letzte Sitzungs-Schreibung', act.lastSessionWrite ? `${fmt.dateTime(act.lastSessionWrite)} (${fmt.ago(act.lastSessionWrite)})` : null],
    ['Schreibvorgänge aus Sitzungen (7 T.)', isNum(act.sessionWrites7d) ? fmt.int(act.sessionWrites7d) : null],
    ['Neue Einträge aus Sitzungen (30 T.)', isNum(act.sessionInserts30d) ? fmt.int(act.sessionInserts30d) : null],
  ]));
  f10b.table([{ key: 'tool', label: 'Werkzeug' }, { key: 'calls', label: 'Aufrufe (30 T.)', num: true, fmt: fmt.int }], byTool);
  ten.append(card({}, f10b.el));
  root.append(ten);

  // ---------- 11 Betrieb
  root.append(sectionLabel('Betrieb'));
  const st = opsState(svc);
  root.append(card({ title: 'Betrieb auf dem Spark', accent: 'Betrieb', sub: `MCP-Server, Snapshot- und Import-Timer (systemd auf Hero), Sicherungskopie und Datenbank. Gesamt: ${{ ok: 'alles läuft', warn: 'Achtung', crit: 'kritisch', unknown: 'keine Angaben' }[st] ?? st}.` }, opsList(svc)));
}

/* ---------------------------------------------------------------- Folien und Grafiken */
const EX_KIND = { chart: 'Charts', slide: 'Text- und Strukturfolien', section: 'Abschnittsfolien', poster: 'Poster' };
const EX_KIND_SUB = { chart: 'enthalten eine Datengrafik', slide: 'Text, Framework, Diagramm', section: 'Kapiteltrenner, Agenda, Titel', poster: 'nicht 16:9' };
const EX_PALETTE = { hsg: 'HSG-Grün', blue: 'Blau' };
const EX_POLICY = {
  share_everything: ['alles geteilt', 'Jede Folie ist in allen Projekten sichtbar und übernehmbar.'],
  internal_default: ['intern, ausser freigegeben', 'Folien sind nur im eigenen Projekt sichtbar, bis sie einzeln freigegeben werden.'],
  auto_sources: ['nach Quellen', 'Öffentlich, wenn alle Quellen öffentlich sind und kein interner Beleg verknüpft ist.'],
};
const EX_DENSITY = [
  { key: 'OK', label: 'OK', cls: 'fx-seg--ok', swatch: 'var(--state-ok)' },
  { key: 'CHECK', label: 'prüfen', cls: 'fx-seg--warn', swatch: 'var(--state-warn)' },
  { key: 'SPLIT', label: 'aufteilen', cls: 'fx-seg--crit', swatch: 'var(--state-crit)' },
];

const DS_PORTAL = `${FUNDUS_PORTAL}#daten`;
function renderDatasets(root, d) {
  root.append(sectionLabel('Datensätze'));
  root.append(el('div.row.fx-portal', el('a.btn.btn--primary', { href: DS_PORTAL, target: '_blank', rel: 'noopener' }, 'Datensätze im Fundus-Portal ansehen'),
    el('span.small.muted', 'Grosse Tabellen für Analysen: gespeichert als Original und Parquet, vom lokalen Modell beschrieben, über MCP auffindbar und abfragbar.')));
  if (!d.datasets) { root.append(card({}, emptyNote('Noch keine Datensätze. Sessions legen sie mit fundus_dataset_add ab, im Portal unter Bibliothek → Datensätze.'))); return; }
  const top = el('div.grid.grid--2');
  const st = card({ title: 'Bestand', accent: 'Bestand', sub: 'Aktuelle Versionen; „gespeichert“ zählt alle Versionen samt Parquet-Kopien auf dem Spark.' });
  st.append(el('div.fx-stats',
    stat('Datensätze', fmt.int(d.datasets), `${fmt.int(d.with_versions)} mit mehreren Versionen${d.removed ? ` · ${fmt.int(d.removed)} gelöscht (30 Tage)` : ''}`),
    stat('Tabellen', fmt.int(d.tables), `${fmt.int(d.documents)} Begleitdokumente`),
    stat('Zeilen', fmt.int(d.rows), `${fmt.int(d.columns)} Spalten`),
    stat('Speicher', fmt.bytes(d.bytes_stored), `davon aktuelle Dateien ${fmt.bytes(d.bytes_current)}`),
    stat('Beschrieben', `${fmt.int(d.described)} von ${fmt.int(d.datasets)}`, d.pending ? `${fmt.int(d.pending)} warten auf das Modell` : 'vom lokalen Modell', d.failed ? 'warn' : d.pending ? 'unknown' : 'ok'),
    stat('Intern', fmt.int(d.internal), 'Kunden- oder eigene Daten, nur für ihr Projekt'),
    stat('Update fällig', fmt.int(arr(d.updateDue).length), `von ${fmt.int(d.tracked ?? 0)} Datensätzen mit Rhythmus`, arr(d.updateDue).length ? 'warn' : (d.tracked ? 'ok' : null))));
  if (arr(d.updateDue).length) {
    st.append(el('p.small', el('strong', 'Neue Ausgabe fällig: '), ...arr(d.updateDue).flatMap((x, i) => [i ? ' · ' : '',
      el('a', { href: `${FUNDUS_PORTAL}#daten?d=${x.slug}`, target: '_blank', rel: 'noopener', title: x.update_hint ?? '' }, x.title), x.next_expected ? ` (${x.next_expected})` : ''])));
  }
  top.append(st);
  const lg = card({ title: 'Grösste Datensätze', accent: 'Grösste', sub: 'Nach Zeilen der aktuellen Version.' });
  lg.append(dataTable([
    { key: 'title', label: 'Datensatz', render: (r) => el('a.fx-cell-clamp', { href: `${FUNDUS_PORTAL}#daten?d=${r.slug}`, target: '_blank', rel: 'noopener', title: r.title }, r.title) },
    { key: 'rows', label: 'Zeilen', num: true, render: (r) => fmt.int(r.rows) },
    { key: 'bytes', label: 'Grösse', num: true, render: (r) => fmt.bytes(r.bytes) },
  ], arr(d.largest), { compact: true }));
  top.append(lg);
  root.append(top);
  const mid = el('div.grid.grid--2');
  mid.append(card({}, simpleBars('Nach Projekt', 'Projekt', 'Datensätze je Projekt.', arr(d.byProject).map((r) => ({ label: r.project, n: r.datasets })), 'Projekt', { unit: 'Datensätze' }).el));
  mid.append(card({}, simpleBars('Formate', 'Formate', 'Dateien der aktuellen Versionen nach Format (Tabellen und Begleitdokumente; Parquet = so geliefert).', arr(d.formats).map((r) => ({ label: r.format, sub: fmt.bytes(r.bytes), n: r.files })), 'Format', { unit: 'Dateien' }).el));
  root.append(mid);
  const days = arr(d.callsByDay).map((x) => x.date);
  const fu = chartFrame({ title: 'Nutzung durch Sessions', accent: 'Nutzung', sub: 'Neue Datensätze und Versionen sowie MCP-Aufrufe je Tag (30 Tage): ablegen, suchen, öffnen und abfragen.' });
  if (days.length) {
    panelColumns(fu.plot, { cats: days, catLabel: (c) => fmt.dateShort(c), tickEvery: 7, panelH: 52, tipTitle: (c) => fmt.date(c), panels: [
      { title: 'Neu abgelegt (Datensätze, Versionen)', values: new Map(arr(d.addedByDay).map((x) => [x.date, x.n])) },
      { title: 'Gesucht (fundus_dataset_search)', values: new Map(arr(d.callsByDay).map((x) => [x.date, x.search])) },
      { title: 'Geöffnet, abgefragt (fundus_dataset)', values: new Map(arr(d.callsByDay).map((x) => [x.date, x.use])) }] });
    const added = new Map(arr(d.addedByDay).map((x) => [x.date, x.n]));
    fu.table([{ key: 'date', label: 'Tag', render: (r) => fmt.date(r.date) }, { key: 'added', label: 'Neu', num: true }, { key: 'search', label: 'Gesucht', num: true },
      { key: 'use', label: 'Geöffnet/abgefragt', num: true }], [...arr(d.callsByDay)].reverse().map((x) => ({ ...x, added: added.get(x.date) ?? 0 })));
  }
  const nw = card({ title: 'Zuletzt abgelegt', accent: 'Zuletzt' });
  nw.append(dataTable([
    { key: 'title', label: 'Datensatz', render: (r) => el('a.fx-cell-clamp', { href: `${FUNDUS_PORTAL}#daten?d=${r.slug}`, target: '_blank', rel: 'noopener', title: r.title }, r.title) },
    { key: 'created_at', label: 'Abgelegt', render: (r) => fmt.dateTime(String(r.created_at).replace(' ', 'T')) },
    { key: 'by', label: 'Von', render: (r) => (r.by ?? '–').replace(/^mcp:?/, 'Session ') },
  ], arr(d.newest), { compact: true }));
  const bot = el('div.grid.grid--2');
  bot.append(card({}, fu.el), nw);
  root.append(bot);
}

function renderExhibits(root, ex, now) {
  const projects = arr(ex.byProject);
  const decks = ex.decks ?? {};
  const dens = ex.density ?? {};
  const use = ex.usage ?? {};
  const pol = EX_POLICY[ex.policy] ?? [ex.policy ?? 'unbekannt', null];
  root.append(el('div.fx-stats', [
    stat('Folien', fmt.int(ex.logical), 'logische Folien (jede Fassung zählt einmal)'),
    stat('Versionen', fmt.int(ex.versions), isNum(ex.logical) && ex.logical > 0 ? `Ø ${fmt.num1(ex.versions / ex.logical)} je Folie` : null),
    stat('Decks', fmt.int(decks.count), isNum(decks.memberships) ? `${fmt.int(decks.memberships)} Folienplätze in Decks` : null),
    stat('mit Skript', isNum(ex.withScriptShare) ? fmt.pct(ex.withScriptShare * 100) : '–', 'erzeugendes Skript gefunden – übernehmbar statt nachbauen'),
    stat('Freigabe', pol[0], `${fmt.int(ex.shared)} geteilt · ${fmt.int(ex.internal)} intern`, ex.policy === 'share_everything' ? 'ok' : null),
  ]));

  const two = el('div.grid.grid--2');
  const projRows = projects.map((p) => ({ label: p.project, values: { logical: p.logical ?? 0, more: Math.max(0, (p.versions ?? 0) - (p.logical ?? 0)) }, p }))
    .sort((a, b) => b.values.logical - a.values.logical);   // nach Folien, nicht nach Versionen (Zahl rechts = Folien)
  const fp = chartFrame({ title: 'Folien je Projekt', accent: 'Projekt', sub: 'Blau = logische Folien, blass = zusätzliche Versionen (Überarbeitungen, Palettenvarianten). Daneben die Zahl der Decks.',
    legend: legend([{ swatch: 'var(--chart-series)', label: 'Folien' }, { swatch: 'var(--chart-series-2)', label: 'weitere Versionen' }]) });
  if (projRows.length) hbars(fp.plot, projRows, { segments: [{ key: 'logical', label: 'Folien', cls: 'fx-seg--series' }, { key: 'more', label: 'weitere Versionen', cls: 'fx-seg--soft' }], format: fmt.int,
    valueText: (r) => fmt.int(r.p.logical), sub: (r) => `${fmt.int(r.p.versions)} Versionen · ${fmt.int(r.p.decks)} ${r.p.decks === 1 ? 'Deck' : 'Decks'}` });
  else fp.plot.append(emptyNote('Noch keine Folien eingelagert.'));
  fp.table([{ key: 'label', label: 'Projekt' }, { key: 'l', label: 'Folien', num: true, render: (r) => fmt.int(r.p.logical) }, { key: 'v', label: 'Versionen', num: true, render: (r) => fmt.int(r.p.versions) }, { key: 'd', label: 'Decks', num: true, render: (r) => fmt.int(r.p.decks) }], projRows);
  two.append(card({}, fp.el));

  const end = d3.timeDay.floor(now);
  const days = d3.timeDays(d3.timeDay.offset(end, -29), d3.timeDay.offset(end, 1)).map((d) => d3.timeFormat('%Y-%m-%d')(d));
  const vbd = arr(ex.versionsByDay);
  const fv = chartFrame({ title: 'Neue Versionen je Tag', accent: 'Versionen', sub: 'Eingelagerte Versionen nach Datum des Inhalts (30 Tage). Neue Renders registriert der mck-exhibit-Skill automatisch; die Erstbefüllung trägt das Datum der Originaldateien.' });
  if (vbd.length) {
    panelColumns(fv.plot, { cats: days, catLabel: (c) => fmt.dateShort(c), tickEvery: 7, panels: [{ title: 'neue Versionen', values: new Map(vbd.map((d) => [d.date, d.n])) }], tipTitle: (c) => fmt.date(c), panelH: 96 });
    fv.plot.append(dl([['Neu in 7 Tagen', fmt.int(ex.versions7d)], ['Neu in 30 Tagen', fmt.int(ex.versions30d)]]));
  } else fv.plot.append(emptyNote('Keine Versionen in den letzten 30 Tagen.'));
  fv.table([{ key: 'date', label: 'Tag', render: (r) => fmt.date(r.date) }, { key: 'n', label: 'neue Versionen', num: true, fmt: fmt.int }], [...vbd].filter((d) => d.n).reverse());
  two.append(card({}, fv.el));
  root.append(two);

  const three = el('div.grid.grid--3');
  const kinds = Object.entries(ex.byKind ?? {}).map(([k, n]) => ({ label: EX_KIND[k] ?? k, n, sub: EX_KIND_SUB[k] ?? null }));
  three.append(card({}, simpleBars('Art', 'Art', 'Was eingelagert ist. Charts enthalten eine Datengrafik; reine Zeichnungen (Diagramm, Schema) zählen als Folie.', kinds, 'Art', { unit: 'Folien' }).el));
  const ct = arr(ex.topChartTypes).map((r) => ({ label: r.type, n: r.n }));
  three.append(card({}, simpleBars('Grafiktypen', 'Grafiktypen', 'Erkannte Grafikbausteine (mck_charts und ältere SVG-/HTML-Grafiken), die zehn häufigsten. Eine Folie kann mehrere enthalten.', ct, 'Typ', { unit: 'Folien' }).el));
  const dc = chartFrame({ title: 'Dichteprüfung', accent: 'Dichte', sub: `Urteil des Komplexitäts-Checks (density.py) über ${fmt.int(dens.measured)} vermessene Folien: „aufteilen“ heisst, der Inhalt gehört auf zwei Folien.`,
    legend: legend(EX_DENSITY.map((d) => ({ swatch: d.swatch, label: d.label }))) });
  if (isNum(dens.measured) && dens.measured > 0) {
    const sh = el('div.fx-shares');
    for (const d of EX_DENSITY) {
      const row = shareRow(d.label, dens[d.key], dens.measured, `${fmt.int(dens[d.key])} von ${fmt.int(dens.measured)} Folien`);
      row.querySelector('.fx-bar__seg')?.classList.replace('fx-seg--series', d.cls);
      sh.append(row);
    }
    dc.plot.append(sh);
    const pal = ex.byPalette ?? {};
    dc.plot.append(dl([
      ['Palette', Object.entries(pal).map(([k, n]) => `${EX_PALETTE[k] ?? k} ${fmt.int(n)}`).join(' · ') || null],
      ['Sprache', Object.entries(ex.byLanguage ?? {}).map(([k, n]) => `${k === 'unknown' ? 'unbekannt' : k.toUpperCase()} ${fmt.int(n)}`).join(' · ') || null],
    ]));
  } else dc.plot.append(emptyNote('Noch keine Folie vermessen.'));
  dc.table([{ key: 'label', label: 'Urteil' }, { key: 'n', label: 'Folien', num: true, fmt: fmt.int }], EX_DENSITY.map((d) => ({ label: d.label, n: dens[d.key] ?? 0 })));
  three.append(card({}, dc.el));
  root.append(three);

  const two2 = el('div.grid.grid--2');
  const fu = chartFrame({ title: 'Wiederverwendung', accent: 'Wiederverwendung', sub: 'Ob Sitzungen vorhandene Folien finden und öffnen: Abrufe über fundus_exhibit und Suchen, deren erste zehn Treffer eine Folie enthielten (30 Tage). Gezählt wird erst seit der Einführung am 25.10.2026.' });
  const calls = arr(use.exhibitCallsByDay), hits = arr(use.searchHitsByDay);
  if (calls.length || hits.length) {
    panelColumns(fu.plot, { cats: days, catLabel: (c) => fmt.dateShort(c), tickEvery: 7, panels: [{ title: 'Folien-Abrufe (fundus_exhibit)', values: new Map(calls.map((d) => [d.date, d.calls])) }, { title: 'Suchen mit Folien-Treffer', values: new Map(hits.map((d) => [d.date, d.searches])) }], tipTitle: (c) => fmt.date(c), panelH: 56 });
  }
  fu.plot.append(dl([
    ['Folien geöffnet (30 T.)', isNum(use.views30d) ? `${fmt.int(use.views30d)} (Abrufe + Bild-/HTML-Downloads)` : null],
    ['Suchen gezielt nach Folien', isNum(use.kindFilterSearches30d) ? fmt.int(use.kindFilterSearches30d) : null],
  ]));
  fu.table([{ key: 'date', label: 'Tag', render: (r) => fmt.date(r.date) }, { key: 'calls', label: 'Abrufe', num: true, fmt: fmt.int }, { key: 'searches', label: 'Suchen mit Treffer', num: true, fmt: fmt.int }],
    days.map((d) => ({ date: d, calls: calls.find((x) => x.date === d)?.calls ?? 0, searches: hits.find((x) => x.date === d)?.searches ?? 0 })).filter((r) => r.calls || r.searches).reverse());
  two2.append(card({}, fu.el));

  const tv = card({ title: 'Meistgeöffnete Folien', accent: 'Meistgeöffnete', sub: `Die fünf am häufigsten geöffneten Folien der letzten 30 Tage. Freigabe-Regel: ${pol[0]}${pol[1] ? ` – ${pol[1]}` : ''}` });
  tv.append(dataTable([
    { key: 'title', label: 'Folie', render: (r) => el('span.fx-cell-clamp.fx-cell-clamp--wide', { title: `${r.title ?? ''} (#${r.id})` }, r.title || `#${r.id}`) },
    { key: 'project', label: 'Projekt', render: (r) => r.project ?? '–' },
    { key: 'kind', label: 'Art', render: (r) => EX_KIND[r.kind] ?? r.kind ?? '–' },
    { key: 'views', label: 'geöffnet', num: true, fmt: fmt.int },
  ], arr(use.topViewed), { compact: true, empty: 'Noch keine Folie geöffnet.' }));
  two2.append(tv);
  root.append(two2);
}

/* ---------------------------------------------------------------- Fehler-/Leerzustand */
// ---------- Aufräumen (fundus/cleanup.py, REST /api/cleanup)
const CLEAN_CLASSES = [
  ['variant', 'Schreibvarianten', 'Regionen nicht als Code (z. B. „United States“ statt „US“), Einheiten, überzählige Leerzeichen', true],
  ['safe', 'Sichere Dubletten', 'identische Datenpunkte derselben Quelle, Dokumente mit gleicher DOI oder gleicher URL und gleichem Titel, Herausgeber mit gleichem Namen', true],
  ['orphan', 'Verwaiste Herausgeber', 'Herausgeber ohne Dokument und ohne Reihe', true],
  ['probable', 'Wahrscheinliche Dubletten', 'gleicher Titel und Herausgeber, ähnliche Formulierung bei gleichem Wert, gleiche Reihen-Definition, Reihen ohne Werte – du entscheidest', false],
  ['contradiction', 'Widersprüche', 'dieselbe Quelle, Kennzahl, Zeitraum und Region mit verschiedenen Werten – du entscheidest', false],
];
const RUN_KIND = { apply: 'Sichere Korrekturen', decide: 'Entscheidung', undo: 'Rückgängig', analyze: 'Prüfung' };

async function cleanupPost(body) {
  const r = await fetch('/api/cleanup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}

function renderCleanup(c, ctx) {
  const cls = c.classes ?? {};
  const autoGroups = sum(CLEAN_CLASSES.filter((x) => x[3]).map(([k]) => cls[k]?.groups));
  const box = card({ title: 'Aufräumen', accent: 'Aufräumen', sub: `Dubletten, Schreibvarianten, Widersprüche und verwaiste Einträge. Die Prüfung läuft jede Nacht automatisch${c.last_analysis ? ` (zuletzt ${fmt.dateTime(c.last_analysis.replace(' ', 'T'))})` : ''}; geändert wird nur auf Knopfdruck. Jeder Lauf ist rückgängig zu machen.` });
  const rows = el('div.fx-shares');
  for (const [k, label, sub, auto] of CLEAN_CLASSES) {
    const v = cls[k] ?? {};
    rows.append(el('div.fx-cleanrow', mark(v.groups ? (auto ? 'warn' : 'info') : 'ok'),
      el('span.fx-cleanrow__label', el('strong', label), el('span.small.muted', ` – ${sub}`)),
      el('span.fx-cleanrow__n', v.groups ? `${fmt.int(v.groups)} Befunde · ${fmt.int(v.records)} Datensätze` : 'keine'),
      el('span.small.muted', auto ? 'automatisch' : 'Entscheidung')));
  }
  box.append(rows);
  const status = el('p.small.fx-cleanstatus', { role: 'status', 'aria-live': 'polite' });
  const check = el('button.btn', { type: 'button' }, 'Jetzt prüfen');
  const apply = el('button.btn.btn--primary', { type: 'button', disabled: !c.auto_records }, c.auto_records ? `Sichere Korrekturen ausführen (${fmt.int(c.auto_records)} Datensätze)` : 'Keine sicheren Korrekturen offen');
  const decide = el('a.btn', { href: '#datenqualitaet' }, `Vorschläge entscheiden (${fmt.int(c.open_decisions ?? 0)})`);
  const busy = (on) => { check.disabled = on; apply.disabled = on || !c.auto_records; };
  const watch = () => {
    const tick = async () => {
      try {
        const s = await fetch('/api/cleanup', { cache: 'no-store' }).then((r) => r.json());
        const j = s.job ?? {};
        if (j.state === 'running') { status.textContent = `${RUN_KIND[j.action] ?? j.action} läuft seit ${j.started?.slice(11)} …`; setTimeout(tick, 2000); return; }
        busy(false);
        if (j.state === 'failed') { status.textContent = `Fehlgeschlagen: ${j.error}`; return; }
        if (j.state === 'done') { ctx.toast?.(`${RUN_KIND[j.action] ?? j.action} fertig${isNum(j.result?.score) ? ` – Datenqualität ${fmt.num1(j.result.score * 100)} %` : ''}.`); ctx.reload?.(); }
      } catch (e) { status.textContent = `Stand nicht abrufbar: ${e.message}`; busy(false); }
    };
    tick();
  };
  const run = async (body) => {
    busy(true); status.textContent = 'Gestartet …';
    try { await cleanupPost(body); watch(); } catch (e) { status.textContent = e.message; busy(false); }
  };
  check.addEventListener('click', () => run({ action: 'analyze' }));
  apply.addEventListener('click', async () => {
    const ex = [cls.variant?.groups ? `${fmt.int(cls.variant.records)} Schreibvarianten vereinheitlichen` : null,
      cls.safe?.groups ? `${fmt.int(cls.safe.groups)} sichere Dubletten zusammenführen` : null,
      cls.orphan?.groups ? `${fmt.int(cls.orphan.groups)} verwaiste Herausgeber entfernen` : null].filter(Boolean).join(', ');
    if (await confirmDialog({ title: 'Sichere Korrekturen ausführen?', text: `${ex}. Vorher wird die Datenbank gesichert; der Lauf lässt sich unten rückgängig machen. Folien behalten ihre Belege (Umleitung auf den zusammengeführten Eintrag).`, confirmLabel: 'Ausführen' }))
      run({ action: 'apply' });
  });
  box.append(el('div.row.fx-cleanactions', check, apply, decide), status);
  const runs = arr(c.runs);
  if (runs.length) {
    const list = el('ul.small.fx-cleanruns');
    for (const r of runs.slice(0, 6)) {
      const s = r.summary ?? {};
      const what = r.kind === 'apply' ? Object.entries(s.done ?? {}).filter(([, v]) => v.groups).map(([k, v]) => `${fmt.int(v.records)} ${({ variant: 'Varianten', safe: 'Dubletten', orphan: 'Verwaiste' })[k] ?? k}`).join(', ')
        : r.kind === 'decide' ? `${fmt.int(s.records)} Datensätze (${s.action})` : r.kind === 'undo' ? `Lauf ${s.undone} zurückgenommen` : '';
      const li = el('li', `${fmt.dateTime(r.started_at.replace(' ', 'T'))} · ${RUN_KIND[r.kind] ?? r.kind}${what ? ` · ${what}` : ''}${r.state === 'undone' ? ' · zurückgenommen' : ''} `);
      if ((r.kind === 'apply' || r.kind === 'decide') && r.state === 'done') {
        const u = el('button.btn.btn--small', { type: 'button' }, 'Rückgängig');
        u.addEventListener('click', async () => {
          if (await confirmDialog({ title: 'Lauf rückgängig machen?', text: 'Alle Datensätze, die dieser Lauf geändert hat, gehen auf den Stand davor zurück – auch wenn sie seither noch einmal bearbeitet wurden.', confirmLabel: 'Rückgängig machen' }))
            run({ action: 'undo', run: r.id });
        });
        li.append(u);
      }
      list.append(li);
    }
    box.append(el('div', el('span.label', 'Letzte Läufe'), list));
  }
  if (arr(c.running).length) { status.textContent = 'Ein Lauf ist gerade aktiv …'; busy(true); watch(); }
  return box;
}

function renderUnavailable(root, data, app) {
  const col = data.collectors.find((c) => c.id === 'fundus') ?? null;
  const errs = arr(col?.errors).map((e) => (typeof e === 'string' ? e : e?.message ?? e?.msg ?? e?.error ?? JSON.stringify(e)));
  root.append(pageHead({ title: 'Fundus-Kennzahlen nicht verfügbar', accent: 'nicht verfügbar',
    lede: col ? `Der Kollektor hat in seinem letzten Lauf (${fmt.dateTime(col.at)}, ${fmt.ago(col.at)}) keine Kennzahlen aus Fundus geliefert. Die übrigen Ansichten sind davon nicht betroffen.` : 'landscape.json enthält weder Fundus-Kennzahlen noch einen Kollektor-Eintrag „fundus“ – vermutlich läuft eine ältere Kollektor-Version.' }));
  root.append(el('div.alert.alert--error', mark('crit'), el('div',
    el('strong', col ? (col.ok === false ? 'Kollektor „fundus“ fehlgeschlagen. ' : 'Kollektor „fundus“ ohne Ergebnis. ') : 'Keine Daten. '),
    errs.length ? el('ul.fx-errors', errs.map((t) => el('li', t))) : el('span', col ? 'Keine Fehlermeldung übermittelt.' : 'Kein Fehlertext vorhanden.'))));
  if (app?.service) root.append(card({ title: 'Betrieb auf dem Spark', accent: 'Betrieb', sub: 'Aus dem App-Eintrag, unabhängig von den Kennzahlen.' }, opsList({ ...app.service, repo: app.repo ?? null })));
  else root.append(card({}, emptyNote('Ohne Fundus-Daten keine Kennzahlen. Prüfen: fundus metrics auf Hero, danach Kollektor neu anstossen (Knopf „Aktualisieren“).')));
}

/* ---------------------------------------------------------------- Bausteine */
function healthOf(f, app) {
  const h = f.health ?? null;
  if (h) return { ...h, dbBytes: f.db?.bytes ?? app?.service?.dbBytes ?? null, dbPath: f.db?.path ?? null, repo: h.repo ?? app?.repo ?? null };
  return app?.service ? { ...app.service, repo: app.repo ?? null } : null;
}
function sectionLabel(text) { return el('h2.fx-section', text); }
function labelOf(list, value) {
  const hit = arr(list).find((r) => r.value === value)?.label;
  if (hit) return hit;
  const v = String(value ?? '');
  return v.length <= 4 ? v.toUpperCase() : v.split('-').map((w, i) => (i ? w : w.charAt(0).toUpperCase() + w.slice(1))).join(' ');
}
function bucketLabel(b) {
  const s = String(b ?? '');
  // ältere Kollektor-Stände haben „>“ aus dem Text entfernt („ 5 Jahre“)
  return /^\s+\d/.test(s) ? `> ${s.trim()}` : s.trim();
}

function kpi({ label, value, unit, state, foot, spark, meter }) {
  const tile = el('div.kpi.kpi--static.fx-kpi', { role: 'group', 'aria-label': `${label}: ${value} ${unit ?? ''} – ${foot}` });
  tile.append(el('span.kpi__label', { title: label }, label));
  tile.append(el('span.kpi__value', value, unit ? el('small', unit) : null));
  if (isNum(meter)) {
    tile.append(el('span.kpi__meter.fx-meter', { title: `${fmt.pct(meter * 100, 1)}` }, el('span.fx-meter__fill', { style: { width: `${Math.max(0, Math.min(100, meter * 100))}%` } })));
  } else if (spark) {
    const sp = el('span.kpi__spark', { 'aria-hidden': 'true' });
    sparkline(sp, spark, { width: 84, height: 30 });
    tile.append(sp);
  }
  tile.append(el('span.kpi__foot', mark(state), el('span', foot)));
  return tile;
}
function stat(label, value, sub, state) {
  return el('div.fx-stat', el('span.fx-stat__label', { title: label }, label), el('span.fx-stat__value', state ? mark(state) : null, value),
    sub ? el('span.fx-stat__sub', { title: sub }, sub) : null);
}
function statusLegend() { return legend(STATUS.map((s) => (s.hatch ? { hatch: true, label: s.label } : { swatch: s.swatch, label: s.label }))); }
function heatLegend() {
  const l = el('div.legend');
  l.append(el('span.legend__item', el('span.fx-hm__key', [0, 1, 2, 3, 4].map((i) => el(`span.fx-hm__keycell.fx-hm--${i}`))), 'wenig → viel'));
  return l;
}

/**
 * HTML-Balken: Raster [Name | Spur | Wert]; unter 420 px Containerbreite steht der Name über der Spur.
 * rows: [{label, sub?, values:{key:n}}]; segments: [{key,label,cls}].
 */
function hbars(container, rows, { segments, format = fmt.int, sort = false, valueText, sub, tipExtra, unit } = {}) {
  const tot = (r) => sum(segments.map((s) => r.values?.[s.key]));
  const list = sort ? [...rows].sort((a, b) => tot(b) - tot(a)) : rows;
  const max = Math.max(1, ...list.map(tot));
  const wrap = el('div.fx-bars', { role: 'list' });
  for (const r of list) {
    const t = tot(r);
    const s = sub ? sub(r) : r.sub;
    const row = el('div.fx-bar', { role: 'listitem', tabindex: '0', 'aria-label': `${r.label}: ${format(t)}${unit ? ` ${unit}` : ''}` });
    row.append(el('span.fx-bar__label', { title: s ? `${r.label} – ${s}` : r.label }, el('span.fx-bar__name', r.label), s ? el('span.fx-bar__sub', s) : null));
    const track = el('span.fx-bar__track');
    const fill = el('span.fx-bar__fill', { style: { width: `${(100 * t) / max}%` } });
    for (const sg of segments) {
      const v = r.values?.[sg.key];
      if (!isNum(v) || v <= 0) continue;
      fill.append(el('span.fx-bar__seg', { class: `fx-bar__seg ${sg.cls}`, style: { flexGrow: String(v) } }));
    }
    track.append(fill);
    row.append(track);
    const vt = valueText ? valueText(r) : format(t);
    row.append(el('span.fx-bar__val', { title: vt }, vt));
    tip.bindEl(row, () => ({ title: r.label, rows: [...(segments.length > 1 ? segments.map((sg) => [sg.label, format(r.values?.[sg.key] ?? 0)]) : []), [segments.length > 1 ? 'Gesamt' : (segments[0]?.label ?? 'Anzahl'), `${format(t)}${unit ? ` ${unit}` : ''}`], ...(tipExtra ? tipExtra(r) : (s ? [['', s]] : []))] }));
    wrap.append(row);
  }
  container.append(wrap);
  return wrap;
}
/** Einfache Balkenliste (eine Serie, blau) mit Rahmen, Tabelle und Leerzustand. */
function simpleBars(title, accent, subText, rows, colLabel, { unit } = {}) {
  const f = chartFrame({ title, accent, sub: subText });
  const data = rows.filter((r) => isNum(r.n));
  if (data.length) hbars(f.plot, data.map((r) => ({ label: r.label, sub: r.sub, values: { n: r.n } })), { segments: [{ key: 'n', label: unit ?? 'Einträge', cls: 'fx-seg--series' }], format: fmt.int, sort: true, unit });
  else f.plot.append(emptyNote('Keine Angaben.'));
  f.table([{ key: 'label', label: colLabel }, { key: 'n', label: unit ?? 'Einträge', num: true, fmt: fmt.int }, ...(data.some((r) => r.sub) ? [{ key: 'sub', label: 'Hinweis', render: (r) => r.sub ?? '–' }] : [])], data);
  return f;
}
/** Anteil-Zeile: Name | Spur (Anteil) | „n · x %“. */
function shareRow(label, n, base, note, isRatio = false, flag = false) {
  const p = isRatio ? (isNum(n) ? n : null) : share(n, base);
  const row = el('div.fx-bar.fx-bar--share', { tabindex: '0', role: 'group', 'aria-label': `${label}: ${p === null ? 'keine Daten' : fmt.pct(p * 100, 1)}` });
  const warn = flag && p !== null && p >= 0.1;
  row.append(el('span.fx-bar__label', { title: note ? `${label} – ${note}` : label }, el('span.fx-bar__name', flag ? mark(p === null ? 'unknown' : warn ? 'warn' : 'ok') : null, label), note ? el('span.fx-bar__sub', note) : null));
  const track = el('span.fx-bar__track.fx-bar__track--bg');
  if (p === null) track.append(el('span.fx-bar__fill.fx-bar__fill--hatch', { style: { width: '100%' } }));
  else track.append(el('span.fx-bar__fill', { style: { width: `${Math.max(p > 0 ? 0.6 : 0, Math.min(100, p * 100))}%` } }, el('span.fx-bar__seg', { class: `fx-bar__seg ${warn ? 'fx-seg--warn' : 'fx-seg--series'}`, style: { flexGrow: '1' } })));
  row.append(track);
  const pctText = (v) => (v > 0 && v < 0.001 ? '< 0.1 %' : fmt.pct(v * 100, v < 0.1 ? 1 : 0));
  const vt = p === null ? 'keine Daten' : isRatio ? fmt.pct(p * 100) : `${fmt.int(n)} · ${pctText(p)}`;
  row.append(el('span.fx-bar__val', { title: vt }, vt));
  tip.bindEl(row, () => ({ title: label, rows: [['Anteil', p === null ? 'keine Daten' : fmt.pct(p * 100, 1)], ...(isRatio ? [] : [['Anzahl', fmt.int(n)], ['Grundmenge', fmt.int(base)]])], note }));
  return row;
}
/** Sichtbarkeit: zwei Segmente (öffentlich / intern) mit Worten neben dem Balken. */
function visibilityBar(vis) {
  const pub = vis.public ?? 0, int = vis.internal ?? 0, all = pub + int || 1;
  const w = el('div.fx-vis');
  w.append(el('div.fx-vis__head', el('span.label', 'Sichtbarkeit'), el('span.small.muted', `${fmt.int(pub)} öffentlich · ${fmt.int(int)} intern (${fmt.pct((100 * int) / all, 1)})`)));
  const bar = el('div.sharebar.fx-vis__bar', { role: 'img', 'aria-label': `öffentlich ${fmt.int(pub)}, intern ${fmt.int(int)}` });
  bar.append(el('div.sharebar__seg', { style: { width: `${(100 * pub) / all}%`, background: 'var(--chart-series)' }, title: `öffentlich: ${fmt.int(pub)}` }));
  bar.append(el('div.sharebar__seg', { style: { width: `${(100 * int) / all}%`, background: 'var(--chart-series-2)' }, title: `intern: ${fmt.int(int)}` }));
  w.append(bar);
  w.append(legend([{ swatch: 'var(--chart-series)', label: 'öffentlich (in Ausgaben zitierbar)' }, { swatch: 'var(--chart-series-2)', label: 'intern' }]));
  return w;
}

/**
 * Heatmap als HTML-Tabelle (sequentiell blau, Wurzel-Skala, 5 Stufen; Stufe 0 = genau null).
 * Spaltenköpfe senkrecht (vertical) oder umbrechend; Zeilenköpfe mit „…“ + title.
 * Unter minWidth scrollt der Rahmen waagrecht (beabsichtigter Scroll-Container).
 */
function heatTable(container, { rows, cols, values, corner, vertical = true, minWidth = 560 }) {
  const max = Math.max(1, ...values.flat().filter(isNum));
  const level = (v) => (!isNum(v) || v <= 0 ? 0 : Math.max(1, Math.min(4, Math.ceil(Math.sqrt(v / max) * 4))));
  const table = el('table.fx-hm', { class: `fx-hm${vertical ? ' fx-hm--vertical' : ''}`, style: { minWidth: `${minWidth}px` } });
  table.append(el('colgroup', el('col.fx-hm__col-row'), cols.map(() => el('col'))));
  table.append(el('thead', el('tr', el('th.fx-hm__corner', { scope: 'col' }, el('span', corner)), cols.map((c) => el('th.fx-hm__colhead', { scope: 'col', title: c.label ?? c.value }, el('span.fx-hm__colhead-text', c.label ?? c.value))))));
  const tb = el('tbody');
  rows.forEach((r, i) => {
    const tr = el('tr', el('th.fx-hm__rowhead', { scope: 'row', title: r.label ?? r.value }, el('span', r.label ?? r.value)));
    cols.forEach((c, j) => {
      const v = values[i]?.[j];
      const td = el('td.fx-hm__cell', { class: `fx-hm__cell fx-hm--${level(v)}`, tabindex: '0', 'aria-label': `${r.label ?? r.value} × ${c.label ?? c.value}: ${fmt.int(v ?? 0)}` }, isNum(v) && v > 0 ? fmt.int(v) : '');
      tip.bindEl(td, () => ({ title: `${r.label ?? r.value} × ${c.label ?? c.value}`, rows: [['Einträge', fmt.int(v ?? 0)]] }));
      tr.append(td);
    });
    tb.append(tr);
  });
  table.append(tb);
  container.append(el('div.fx-hm-wrap', table));
}

/* ---------------------------------------------------------------- D3: Säulen */
function colFrame(title, accent, subText, rows, xTitle, yTitle, legendEl) {
  const f = chartFrame({ title, accent, sub: subText, legend: legendEl });
  if (rows.length && rows.some((r) => isNum(r.value))) {
    const cats = rows.map((r) => r.label);
    stackedColumns(f.plot, { cats, rows: new Map(rows.map((r) => [r.label, { v: r.value }])), segments: [{ key: 'v', label: yTitle, cls: 'fx-col' }], clsFor: (c) => rows.find((r) => r.label === c)?.cls, wrapLabels: true, height: 168, tipTitle: (c) => `${xTitle}: ${c}` });
  } else f.plot.append(emptyNote('Keine Angaben.'));
  f.table([{ key: 'label', label: xTitle }, { key: 'value', label: yTitle, num: true, fmt: fmt.int }], rows);
  return f;
}

const colPath = (x, yTop, w, h) => roundedTop(x, yTop, w, h, Math.min(4, w / 2));
function roundedTop(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  if (h <= 0 || w <= 0) return '';
  if (r <= 0) return `M${x} ${y}h${w}v${h}h${-w}z`;
  return `M${x} ${y + h}V${y + r}a${r} ${r} 0 0 1 ${r} ${-r}h${w - 2 * r}a${r} ${r} 0 0 1 ${r} ${r}V${y + h}z`;
}
/**
 * Achsenbeschriftung unten: ausgedünnt (show), gemessen, ohne Überlappung und ohne über den Rand zu
 * laufen. Die letzte Kategorie hat Vorrang: kollidiert sie, weicht die vorherige Beschriftung.
 */
function xLabels(g, cats, x, band, { label = String, show = () => true, wrap = false, y = 11, W, minX = 0 }) {
  const placed = [];
  cats.forEach((c, i) => {
    const isLast = i === cats.length - 1;
    if (!wrap && !show(c, i) && !isLast) return;
    const cx = x(c) + band / 2;
    const t = g.append('text').attr('class', 'fx-axis-label').attr('x', cx).attr('y', y).attr('text-anchor', 'middle').text(label(c));
    if (wrap) { wrapText(t.node(), Math.max(10, band - 2), { maxLines: 2, lineHeight: 1.15 }); return; }
    const w = measure(t.node());
    let x0 = cx - w / 2, x1 = cx + w / 2;
    if (x1 > W) { t.attr('text-anchor', 'end').attr('x', W - 1); x0 = W - 1 - w; x1 = W - 1; }
    if (x0 < minX) { if (isLast) { t.remove(); return; } t.attr('text-anchor', 'start').attr('x', minX); x0 = minX; x1 = minX + w; }
    if (isLast) { while (placed.length && placed[placed.length - 1].x1 + 6 > x0) placed.pop().node.remove(); }
    else if (placed.length && placed[placed.length - 1].x1 + 6 > x0) { t.remove(); return; }
    placed.push({ node: t.node(), x0, x1 });
  });
}

/**
 * Gestapelte (oder einfache) Säulen über Kategorien.
 * rows: Map(cat → {segKey: n}); segments: [{key,label,cls}]; shadeBefore: Kategorie, vor der hinterlegt wird.
 */
function stackedColumns(container, { cats, rows, segments, height = 210, tickEvery = 1, wrapLabels = false, shadeBefore = null, shadeLabel = '', clsFor = null, tipTitle = String }) {
  const total = (c) => sum(segments.map((s) => rows.get(c)?.[s.key]));
  return responsive(container, (W) => {
    const maxV = Math.max(1, ...cats.map(total));
    const y = d3.scaleLinear().domain([0, maxV]).nice(3);
    // Breite der y-Beschriftung aus den tatsächlich gezeichneten Ticks (nach nice()) messen
    const yTickW = Math.max(18, ...y.ticks(3).map((v) => measureText(fmt.int(v), 11))) + 8;
    const m = { top: shadeBefore ? 22 : 10, right: 6, bottom: wrapLabels ? 36 : 24, left: Math.ceil(yTickW) };
    const H = height;
    y.range([H - m.bottom, m.top]);
    const svg = svgPx(container, W, H, `Säulen, ${cats.length} Kategorien`);
    const x = d3.scaleBand().domain(cats).range([m.left, W - m.right]).paddingInner(cats.length > 20 ? 0.18 : 0.22).paddingOuter(0.08);
    const band = x.bandwidth();
    if (shadeBefore && cats.includes(shadeBefore)) {
      const xi = cats.indexOf(shadeBefore);
      if (xi > 0) {
        const x1 = x(shadeBefore) - (x.step() * x.paddingInner()) / 2;
        svg.append('rect').attr('class', 'fx-shade').attr('x', m.left).attr('y', m.top - 16).attr('width', Math.max(0, x1 - m.left)).attr('height', H - m.bottom - m.top + 16);
        const sl = svg.append('text').attr('class', 'chart-note').attr('x', m.left + 4).attr('y', m.top - 5).text(shadeLabel);
        fitOne(sl.node(), x1 - m.left - 8);
      }
    }
    svg.append('g').attr('class', 'axis grid').attr('transform', `translate(${m.left}, 0)`).call(d3.axisLeft(y).ticks(3).tickFormat((v) => fmt.int(v)).tickSize(-(W - m.left - m.right)));
    const gx = svg.append('g').attr('transform', `translate(0, ${H - m.bottom + 6})`);
    const every = Math.max(tickEvery, Math.ceil(28 / Math.max(1, x.step())));
    xLabels(gx, cats, x, band, { show: (c, i) => i % every === 0, wrap: wrapLabels, W, minX: m.left });
    const g = svg.append('g');
    for (const c of cats) {
      let acc = 0;
      const t = total(c);
      const col = g.append('g').attr('class', 'fx-colg').attr('tabindex', 0).attr('role', 'img').attr('aria-label', `${tipTitle(c)}: ${fmt.int(t)}`);
      col.append('rect').attr('class', 'hit').attr('x', x(c) - (x.step() - band) / 2).attr('width', x.step()).attr('y', m.top).attr('height', H - m.bottom - m.top);
      const segs = segments.filter((s) => isNum(rows.get(c)?.[s.key]) && rows.get(c)[s.key] > 0);
      segs.forEach((s, si) => {
        const v = rows.get(c)[s.key];
        const y0 = y(acc), y1 = y(acc + v);
        const top = si === segs.length - 1;
        const gap = si > 0 ? 2 : 0;
        const h = Math.max(0, y0 - y1 - gap);
        if (h > 0) col.append('path').attr('class', `fx-colseg ${clsFor?.(c) ?? s.cls}`).attr('d', top ? colPath(x(c), y1, band, h) : `M${x(c)} ${y1}h${band}v${h}h${-band}z`);
        acc += v;
      });
      tip.bind(col, () => ({ title: tipTitle(c), rows: [...(segments.length > 1 ? segments.map((s) => [s.label, fmt.int(rows.get(c)?.[s.key] ?? 0)]) : []), [segments.length > 1 ? 'Gesamt' : segments[0].label, fmt.int(t)]] }));
    }
    // selektive Direktbeschriftung: nur der Höchstwert
    const top = cats.reduce((b, c) => (total(c) > total(b) ? c : b), cats[0]);
    if (total(top) > 0) {
      const lt = svg.append('text').attr('class', 'chart-value fx-maxlabel').attr('x', x(top) + band / 2).attr('y', y(total(top)) - 4).attr('text-anchor', 'middle').text(fmt.int(total(top)));
      const w = measure(lt.node());
      const cx = x(top) + band / 2;
      if (cx + w / 2 > W) lt.attr('text-anchor', 'end').attr('x', W - 1);
      else if (cx - w / 2 < m.left) lt.attr('text-anchor', 'start').attr('x', m.left);
      if (y(total(top)) - 4 - 11 < (shadeBefore ? m.top - 4 : 0)) lt.remove();
    }
  }, { minWidth: 240, scroll: true });
}

/**
 * Kleinvielfache: N Säulenreihen übereinander auf derselben Kategorieachse, je eigene y-Skala
 * (Höchstwert rechts im Titel). Zeiger zeigt alle Reihen für die Kategorie.
 */
function panelColumns(parent, { cats, panels, catLabel = String, tickEvery = 5, marker = null, tipTitle = String, panelH = 78 }) {
  // eigener Behälter: responsive() leert ihn bei jedem Neuzeichnen, später Angehängtes im Rahmen bleibt stehen
  const container = el('div.fx-panels');
  parent.append(container);
  return responsive(container, (W) => {
    const left = 4, right = 4, titleH = 20, gapY = 12, axisH = 20;
    const H = panels.length * (titleH + panelH + gapY) - gapY + axisH;
    const svg = svgPx(container, W, H, `${panels.length} Kleinvielfache`);
    const x = d3.scaleBand().domain(cats).range([left, W - right]).paddingInner(cats.length > 40 ? 0.12 : 0.2);
    const band = x.bandwidth();
    panels.forEach((p, i) => {
      const top = i * (titleH + panelH + gapY);
      const g = svg.append('g').attr('transform', `translate(0, ${top})`);
      const vals = cats.map((c) => p.values.get(c) ?? 0);
      const maxV = Math.max(0, ...vals);
      const tot = sum(vals);
      const t = g.append('text').attr('class', 'chart-label').attr('x', left).attr('y', 13).text(p.title);
      const note = g.append('text').attr('class', 'chart-note').attr('x', W - right).attr('y', 13).attr('text-anchor', 'end').text(maxV > 0 ? `Σ ${fmt.int(tot)} · max. ${fmt.int(maxV)}` : 'keine');
      const nw = measure(note.node());
      fitOne(t.node(), Math.max(40, W - left - right - nw - 12));
      if (measure(t.node()) + nw + 12 > W - left - right) fitOne(note.node(), Math.max(0, W - left - right - measure(t.node()) - 12));
      const y = d3.scaleLinear().domain([0, Math.max(1, maxV)]).range([titleH + panelH, titleH + 4]);
      g.append('line').attr('class', 'fx-baseline').attr('x1', left).attr('x2', W - right).attr('y1', titleH + panelH + 0.5).attr('y2', titleH + panelH + 0.5);
      if (marker !== null && cats.includes(marker)) {
        const mx = x(marker) + band / 2;
        g.append('line').attr('class', 'fx-marker').attr('x1', mx).attr('x2', mx).attr('y1', titleH + 2).attr('y2', titleH + panelH);
      }
      cats.forEach((c, j) => {
        const v = vals[j];
        if (v > 0) g.append('path').attr('class', 'fx-colseg fx-col').attr('d', colPath(x(c), y(v), band, Math.max(1, titleH + panelH - y(v))));
      });
    });
    // gemeinsame x-Achse unten
    const gx = svg.append('g').attr('transform', `translate(0, ${H - axisH + 4})`);
    const raw = Math.max(1, tickEvery, Math.ceil(44 / Math.max(1, x.step())));
    const every = typeof cats[0] === 'number' ? Math.ceil(raw / 5) * 5 : raw;
    xLabels(gx, cats, x, band, { label: catLabel, show: (c, i) => (typeof c === 'number' ? c % every === 0 : i % every === 0), W });
    // Zeiger: eine Spalte über alle Reihen
    const hl = svg.append('rect').attr('class', 'fx-hover').attr('y', 0).attr('height', H - axisH).attr('width', x.step()).style('opacity', 0);
    const hits = svg.append('g').selectAll('rect').data(cats).join('rect').attr('class', 'hit').attr('x', (c) => x(c) - (x.step() - band) / 2).attr('width', x.step()).attr('y', 0).attr('height', H - axisH).attr('tabindex', -1);
    hits.on('pointerenter.hl', (e, c) => hl.attr('x', x(c) - (x.step() - band) / 2).style('opacity', 1)).on('pointerleave.hl', () => hl.style('opacity', 0));
    tip.bind(hits, (c) => ({ title: tipTitle(c), rows: panels.map((p) => [p.title, fmt.int(p.values.get(c) ?? 0)]) }));
  }, { minWidth: 300, scroll: true });
}

let measureCanvas;
function measureText(s, size = 12) {
  measureCanvas ??= document.createElement('canvas').getContext('2d');
  measureCanvas.font = `${size}px ${getComputedStyle(document.documentElement).getPropertyValue('--sans') || 'Inter, sans-serif'}`;
  return measureCanvas.measureText(String(s)).width;
}
