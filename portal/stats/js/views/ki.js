/**
 * Ansicht „KI-Knoten“: Speicherbalken (geladene Modelle als Segmente, Grenze),
 * installierte Modelle als Treemap, Verbraucher je Modell.
 */
import { el, pageHead, chartFrame, card, legend, mark, statePill, badge, emptyNote, dataTable, dl } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { aiStats } from '../lib/derive.js';
import { memoryBar } from '../charts/memory.js';
import { treemapE } from '../charts/echarts-charts.js';

export function render(root, data, ctx) {
  const ai = data.ai;
  const st = aiStats(data);
  const lede = ai ? `${st.loaded} ${st.loaded === 1 ? 'Modell' : 'Modelle'} geladen (${fmt.bytes(st.loadedBytes)}), Grenze ${st.limit ?? 'unbekannt'} · ${st.installed} installiert mit ${fmt.bytes(st.installedBytes)} · ${st.unused.length} ungenutzt (${fmt.bytes(st.unusedBytes)}).` : 'Keine KI-Daten geliefert.';
  const over = st.limit !== null && st.loaded > st.limit;
  const unknown = !ai || st.state === 'unknown';
  root.append(pageHead({ title: over ? `${numWord(st.loaded)} Modelle geladen, erlaubt sind ${st.limit}` : unknown ? 'KI-Knoten ohne verlässliche Daten' : 'Der KI-Knoten hält sich an die Grenze', accent: over ? 'erlaubt' : unknown ? 'Daten' : 'Grenze', lede }));
  const ins = ctx.insight('ki'); if (ins) root.append(ins);

  const f1 = chartFrame({ title: 'Unified Memory', accent: 'Memory', sub: 'Ein Balken für den gesamten Speicher: geladene Modelle als blaue Segmente (grösstes zuerst), übrige Belegung grau, Rest frei. Der senkrechte Strich markiert die erlaubte Modellzahl – ein Segment rechts davon ist zu viel und wird koralle.',
    legend: legend([{ swatch: 'var(--chart-series)', label: 'geladenes Modell' }, { swatch: 'var(--state-crit)', label: 'über der Grenze' }, { swatch: 'var(--grey)', label: 'übrige Belegung' }, { swatch: 'var(--chart-track)', label: 'frei' }]) });
  if (ai) memoryBar(f1.plot, ai);
  else f1.plot.append(emptyNote('Keine KI-Daten.'));
  f1.table([{ key: 'name', label: 'Modell' }, { key: 'bytes', label: 'Speicher', num: true, fmt: fmt.bytes }, { key: 'ctx', label: 'Kontext', num: true, fmt: fmt.int }, { key: 'expires', label: 'Läuft ab', fmt: (v) => (v ? fmt.ago(v) : '–') }], ai?.loaded ?? []);
  const c1 = card({}, f1.el);
  if (ai) c1.append(dl([['GPU-Auslastung', fmt.pct(ai.gpu?.utilPct)], ['GPU-Temperatur', fmt.temp(ai.gpu?.tempC)], ['Leistung', typeof ai.gpu?.powerW === 'number' ? `${fmt.int(ai.gpu.powerW)} W` : null], ['Speicher frei', fmt.bytes(ai.memAvailBytes)], ['Zustand', statePill(st.state, over ? `${st.loaded - st.limit} zu viel` : undefined)]]));
  root.append(c1);

  const two = el('div.grid.grid--2');
  const f2 = chartFrame({ title: 'Installierte Modelle nach Grösse', accent: 'Grösse', sub: 'Treemap: grosse Kacheln = viel Platz. Blass = von Diensten genutzt, Spur = ungenutzt (Aufräumpotenzial).',
    legend: legend([{ swatch: 'var(--chart-series)', label: 'geladen' }, { swatch: 'var(--chart-series-2)', label: 'genutzt' }, { swatch: 'var(--chart-track)', label: 'ungenutzt' }]) });
  const loaded = new Set((ai?.loaded ?? []).map((m) => m.name));
  const used = new Set((ai?.consumers ?? []).flatMap((c) => c.models ?? []));
  if (ai?.installed?.length) { const plot = el('div.echart'); f2.plot.append(plot); treemapE(plot, ai.installed, { loaded, used }).catch((e) => plot.replaceChildren(emptyNote(`Treemap konnte nicht geladen werden: ${e.message}`))); }
  else f2.plot.append(emptyNote('Keine installierten Modelle gemeldet.'));
  f2.table([{ key: 'name', label: 'Modell' }, { key: 'bytes', label: 'Grösse', num: true, fmt: fmt.bytes }, { key: 'family', label: 'Familie' }, { key: 'modified', label: 'Geändert', fmt: (v) => (v ? fmt.ago(v) : '–') }, { key: 'status', label: 'Status', render: (r) => badge(loaded.has(r.name) ? 'geladen' : used.has(r.name) ? 'genutzt' : 'ungenutzt', loaded.has(r.name) ? 'accent' : used.has(r.name) ? 'outline' : 'unknown') }], [...(ai?.installed ?? [])].sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0)));
  f2.setHint(st.unused.length ? `Aufräumpotenzial: ${st.unused.length} Modelle ohne Verbraucher, zusammen ${fmt.bytes(st.unusedBytes)}.` : '');
  two.append(card({}, f2.el));

  const c3 = card({ title: 'Wer nutzt welches Modell', accent: 'nutzt', sub: 'Aus der Konfiguration der Dienste abgeleitet.' });
  const consumers = ai?.consumers ?? [];
  const svcLabel = (id) => data.services.find((s) => s.id === id)?.label ?? id;
  c3.append(dataTable([{ key: 'service', label: 'Dienst', fmt: svcLabel }, { key: 'models', label: 'Modelle', render: (r) => el('span.row', ...(r.models ?? []).map((m) => badge(m, loaded.has(m) ? 'accent' : 'outline'))) }], consumers, { compact: true, empty: 'Keine Verbraucher gemeldet.' }));
  const orphan = (ai?.loaded ?? []).filter((m) => !used.has(m.name));
  if (orphan.length) c3.append(el('p.small.secondary', `Geladen ohne bekannten Verbraucher: ${orphan.map((m) => m.name).join(', ')}.`));
  two.append(c3);
  root.append(two);
}
function numWord(n) { return ['Keine', 'Ein', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }
