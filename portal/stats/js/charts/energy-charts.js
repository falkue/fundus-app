/**
 * Energie-Diagramme für die Ansicht „Nachhaltigkeit“ (ECharts SVG mit Circle-Theme, eigener Tooltip;
 * Sankey mit d3-sankey). Gerätefarben überall gleich (feste Reihenfolge, Hero = Akzent); Schätzungen
 * tragen zusätzlich ein Muster (Punktraster bzw. Strichraster) und werden im Tooltip als Wort genannt.
 *
 *   dailyStackedE(el, rows, devices, {unit, baseline, labelOf})   V2 gestapelte Tagessäulen + Grundlast-Linie → {api, setUnit}
 *   avgDayE(el, hours, devices, {baseW})                           V3 Durchschnittstag (Median, P10–P90, Grundlast) / 48 h / je Gerät → {api, set}
 *   pacingE(el, {...})                                             V7 Jahres-Pacing kumulativ mit Prognoseband und Haushaltsreferenz
 *   calendarHeatE(el, dayMap, {weeks})                             V8 Kalender-Heatmap kWh pro Tag (Lücken markiert)
 *   waterfallE(el, {start, items, unit})                           V6 Wasserfall der Einsparpotenziale → {api, setUnit}
 *   purposeSankey(el, {nodes, links})                              V5 Netz → Gerät → Zweck (d3-sankey)
 *   costSeriesE(el, daily, prices)                                 Kosten pro Tag mit Preiswechsel-Marken
 */
import { makeChart } from './echarts-base.js';
import { readTokens } from '../lib/theme.js';
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { fitOne } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;
export const ORDER = ['hero', 'xerox', 'carbon', 'mac', 'network'];
export const METHOD_WORD = { measured: 'gemessen', mixed: 'teilweise gemessen', estimated: 'geschätzt' };
const UNIT = { kwh: { label: 'kWh', fmt: (v) => fmt.num1(v) }, costChf: { label: 'CHF', fmt: (v) => fmt.num2(v) }, co2Kg: { label: 'kg CO₂e', fmt: (v) => fmt.num2(v) } };
export const unitLabel = (u) => UNIT[u]?.label ?? u;
export const unitFmt = (u) => UNIT[u]?.fmt ?? ((v) => fmt.num1(v));

export function deviceColors(devices) {
  const t = readTokens();
  const palette = { hero: t.blue, xerox: t['cal-3'], carbon: t['cal-2'], mac: t['cal-1'], network: t['ink-4'] };
  const extra = [t['reach-localhost'], t['ink-3'], t.grey];
  let k = 0;
  return Object.fromEntries(devices.map((d) => [d.node, palette[d.node] ?? extra[k++ % extra.length]]));
}
export const sortDevices = (devices) => [...devices].sort((a, b) => (ORDER.indexOf(a.node) + 99) % 99 - (ORDER.indexOf(b.node) + 99) % 99);
export function decalFor(method, t) {
  if (method === 'estimated') return { symbol: 'circle', dashArrayX: [1, 0], dashArrayY: [2, 5], symbolSize: 0.6, color: t.card, rotation: 0 };
  if (method === 'mixed') return { symbol: 'rect', dashArrayX: [1, 0], dashArrayY: [3, 5], symbolSize: 1, color: t.card, rotation: Math.PI / 4 };
  return null;
}
/** Geschätzte Zeilenzahl einer Legende (Inter 12 px ≈ 6.6 px/Zeichen + Symbol). */
function legendRows(labels, width) {
  const avail = Math.max(200, width - 40); let rows = 1, x = 0;
  for (const l of labels) { const w = String(l).length * 6.6 + 30; if (x + w > avail && x > 0) { rows++; x = 0; } x += w; }
  return rows;
}
const widthOf = (el, min) => Math.max(el.clientWidth || 0, el.parentElement?.clientWidth || 0, min);
const hourLabel = (h) => `${String(h).padStart(2, '0')}:00`;

/* ------------------------------------------------------------------ V2 Tagessäulen */
/** rows: [{date, total:{kwh,costChf,co2Kg}, per:{node:{kwh,costChf,co2Kg,hours,complete,partial,method}}}] (aufsteigend) */
export async function dailyStackedE(container, rows, devices, { unit = 'kwh', baseline = null, legendSelected = null, prices = [] } = {}) {
  const t = readTokens();
  const colors = deviceColors(devices);
  const labels = devices.map((d) => d.label);
  const lrows = legendRows([...labels, 'Grundlast'], widthOf(container, 440));
  const dateSet = new Set(rows.map((r) => r.date));
  const build = (u) => {
    const f = unitFmt(u);
    const series = devices.map((d) => ({
      name: d.label, type: 'bar', stack: 'day', barMaxWidth: 26, itemStyle: { color: colors[d.node], decal: decalFor(d.method, t) }, emphasis: { focus: 'series' },
      data: rows.map((r) => { const e = r.per[d.node]; if (!e || typeof e[u] !== 'number') return null; const inc = e.complete === false; return inc ? { value: e[u], itemStyle: { opacity: 0.7, borderType: 'dashed', borderColor: t['ink-3'], borderWidth: 1, decal: { symbol: 'rect', dashArrayX: [1, 0], dashArrayY: [2, 4], rotation: Math.PI / 4, color: t.card, symbolSize: 1 } } } : e[u]; }),
    }));
    // Preiswechsel als senkrechte Marke in der Kostenreihe (nur Stichtage innerhalb des Zeitraums)
    if (u === 'costChf' && series.length) {
      const marks = (prices ?? []).filter((p) => p.validFrom && dateSet.has(String(p.validFrom).slice(0, 10)))
        .map((p) => ({ xAxis: String(p.validFrom).slice(0, 10), name: `ab hier ${fmt.num1(p.chfPerKwh * 100)} Rp./kWh`, label: { formatter: (m) => m.name, position: 'insideEndTop', distance: 4, color: t['ink-2'], fontSize: 11 }, lineStyle: { color: t.orange, type: 'dashed', width: 1.2 } }));
      if (marks.length) series[0].markLine = { silent: true, symbol: 'none', animation: false, data: marks };
    }
    if (baseline && typeof baseline[u] === 'number') series.push({ name: 'Grundlast', type: 'line', step: 'middle', symbol: 'none', z: 5, data: rows.map(() => baseline[u]), lineStyle: { type: 'dashed', width: 1.5, color: t['ink-2'] }, itemStyle: { color: t['ink-2'] }, emphasis: { disabled: true } });
    return {
      grid: { left: 56, right: 20, top: 36, bottom: 66 + lrows * 18 },
      xAxis: { type: 'category', data: rows.map((r) => r.date), axisLabel: { formatter: (v) => fmt.dateShort(v), hideOverlap: true } },
      yAxis: { type: 'value', name: `${unitLabel(u)} pro Tag`, nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => f(v) } },
      legend: { bottom: 36, icon: 'roundRect', itemWidth: 12, itemHeight: 8, data: [...labels, ...(baseline ? ['Grundlast'] : [])], selected: legendSelected ?? undefined },
      dataZoom: [{ type: 'slider', xAxisIndex: 0, bottom: 6, height: 20, brushSelect: false, labelFormatter: (v, dt) => fmt.dateShort(dt) }, { type: 'inside', xAxisIndex: 0 }],
      series,
    };
  };
  let cur = unit;
  const api = await makeChart(container, build(unit), { height: 320 + lrows * 18, minWidth: 440, aria: `Verbrauch je Gerät und Tag, ${rows.length} Tage`,
    tip: (p) => {
      if (p.componentType !== 'series') return null;
      const r = rows[p.dataIndex]; if (!r) return null;
      const f = unitFmt(cur);
      if (p.seriesName === 'Grundlast') return { title: `Grundlast · ${fmt.date(r.date)}`, rows: [['Grundlast × 24 h', `${f(baseline[cur])} ${unitLabel(cur)}`], ['Tag gesamt', `${f(r.total[cur])} ${unitLabel(cur)}`]] };
      const d = devices.find((x) => x.label === p.seriesName); const e = d ? r.per[d.node] : null;
      return { title: `${p.seriesName} · ${fmt.date(r.date)}`, rows: [[unitLabel(cur), `${f(e?.[cur])}`], ['Erhebung', METHOD_WORD[e?.method ?? d?.method] ?? 'unbekannt'], ['Stunden erfasst', e ? `${fmt.int(e.hours)} von 24${e.partial ? ' (laufender Tag)' : e.complete === false ? ' – unvollständig' : ''}` : '–'], ['Tag gesamt', `${f(r.total[cur])} ${unitLabel(cur)}`]] };
    } });
  return { api, setUnit(u) { cur = u; api.update(build(u), true); } };
}

/* ------------------------------------------------------------------ V3 Durchschnittstag / 48 h */
/** hours: [{ts: Date, per:{node: W}, total: W}] (beliebig lang, stündlich). */
export async function avgDayE(container, hours, devices, { baseW = 0, mode = 'day24', perDevice = false, heroRuns = [], date = null } = {}) {
  const t = readTokens();
  const colors = deviceColors(devices);
  const nDays = new Set(hours.map((h) => d3.timeDay.floor(h.ts).getTime())).size;
  const dayKey = d3.timeFormat('%Y-%m-%d');
  const statsFor = (pick, onlyDate = null) => d3.range(24).map((hr) => {
    const vals = hours.filter((h) => h.ts.getHours() === hr && (!onlyDate || dayKey(h.ts) === onlyDate)).map(pick).filter((v) => typeof v === 'number');
    vals.sort(d3.ascending);
    return { hr, n: vals.length, med: vals.length ? d3.median(vals) : null, p10: vals.length >= 3 ? d3.quantile(vals, 0.1) : null, p90: vals.length >= 3 ? d3.quantile(vals, 0.9) : null };
  });
  const hasBand = nDays >= 3;
  const bandSeries = (st, idx, color) => [
    { name: 'P10', type: 'line', stack: `b${idx}`, xAxisIndex: idx, yAxisIndex: idx, symbol: 'none', silent: true, lineStyle: { opacity: 0 }, data: st.map((s) => (hasBand && s.p10 !== null ? s.p10 : null)), tooltip: { show: false }, emphasis: { disabled: true } },
    { name: 'P10–P90', type: 'line', stack: `b${idx}`, xAxisIndex: idx, yAxisIndex: idx, symbol: 'none', silent: true, lineStyle: { opacity: 0 }, areaStyle: { color, opacity: 0.18 }, data: st.map((s) => (hasBand && s.p90 !== null ? s.p90 - s.p10 : null)), emphasis: { disabled: true } },
  ];
  const buildDay = (single = null) => {
    const st = statsFor((h) => h.total, single);
    const band = !single && hasBand;
    const lineName = single ? `Tag ${fmt.date(single)}` : 'Median';
    const series = [
      { name: 'Grundlast', type: 'line', symbol: 'none', silent: true, data: st.map(() => baseW), lineStyle: { type: 'dashed', width: 1, color: t['ink-3'] }, areaStyle: { color: t['ink-4'], opacity: 0.18 }, emphasis: { disabled: true } },
      ...(band ? bandSeries(st, 0, t.blue) : []),
      { name: lineName, type: 'line', symbol: 'circle', symbolSize: 4, showSymbol: !!single, connectNulls: false, data: st.map((s) => s.med), lineStyle: { width: 2, color: t.blue }, itemStyle: { color: t.blue } },
    ];
    return { option: {
      grid: { left: 56, right: 20, top: 36, bottom: 56 },
      xAxis: { type: 'category', data: st.map((s) => hourLabel(s.hr)), boundaryGap: false, axisLabel: { hideOverlap: true }, axisPointer: { show: true, type: 'line', lineStyle: { color: t['chart-cap'], type: 'dashed' }, label: { show: false } } },
      yAxis: { type: 'value', name: 'Leistung in Watt', nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => fmt.int(v) } },
      legend: { bottom: 6, icon: 'roundRect', itemWidth: 12, itemHeight: 8, data: [lineName, ...(band ? ['P10–P90'] : []), 'Grundlast'] },
      series }, st, single, height: 300 };
  };
  const buildPer = () => {
    const cols = widthOf(container, 440) < 700 ? 1 : 2, h = 120, gapY = 44, top = 30;
    const grids = [], xAxes = [], yAxes = [], series = [], titles = [];
    devices.forEach((d, i) => {
      const st = statsFor((hh) => hh.per[d.node]);
      const c = i % cols, r = Math.floor(i / cols);
      const left = `${(100 / cols) * c + 4}%`, width = `${100 / cols - 7}%`, gTop = top + r * (h + gapY);
      grids.push({ left, width, top: gTop, height: h - 26 });
      xAxes.push({ type: 'category', gridIndex: i, data: st.map((s) => hourLabel(s.hr)), boundaryGap: false, axisLabel: { hideOverlap: true, interval: 5 } });
      yAxes.push({ type: 'value', gridIndex: i, min: 0, splitNumber: 2, axisLabel: { formatter: (v) => fmt.int(v), hideOverlap: true } });
      const base = typeof d.baseW === 'number' ? d.baseW : null;
      titles.push({ text: `${d.label} · ${METHOD_WORD[d.method] ?? ''}`, left, top: gTop - 24, textStyle: { fontSize: 12.5, fontWeight: 500, color: t.ink, width: Math.floor(widthOf(container, 440) / cols) - 40, overflow: 'truncate' } });
      if (base !== null) series.push({ name: 'Grundlast', type: 'line', xAxisIndex: i, yAxisIndex: i, symbol: 'none', silent: true, data: st.map(() => base), lineStyle: { type: 'dashed', width: 1, color: t['ink-3'] }, emphasis: { disabled: true } });
      series.push(...bandSeries(st, i, colors[d.node]));
      series.push({ name: d.label, type: 'line', xAxisIndex: i, yAxisIndex: i, symbol: 'none', data: st.map((s) => s.med), lineStyle: { width: 1.8, color: colors[d.node] }, itemStyle: { color: colors[d.node] } });
    });
    return { option: { grid: grids, xAxis: xAxes, yAxis: yAxes, series, title: titles, axisPointer: { link: [{ xAxisIndex: 'all' }] } }, height: top + Math.ceil(devices.length / cols) * (h + gapY) };
  };
  const build48 = () => {
    const last = hours.filter((h) => h.ts.getTime() >= Date.now() - 49 * 3600e3).sort((a, b) => a.ts - b.ts);
    const lab = devices.map((d) => d.label), lrows = legendRows(lab, widthOf(container, 440));
    const series = devices.map((d) => ({ name: d.label, type: 'line', stack: 'w', step: 'middle', showSymbol: false, areaStyle: { color: colors[d.node], opacity: 0.85 }, lineStyle: { width: 1, color: colors[d.node] }, itemStyle: { color: colors[d.node] }, emphasis: { focus: 'series' }, data: last.map((h) => [h.ts.getTime(), typeof h.per[d.node] === 'number' ? h.per[d.node] : null]) }));
    // Streifen „Modell geladen“ (Hero, modelRuns48h) als markArea oben im Diagramm
    const runs = (heroRuns ?? []).map((r) => ({ name: r.name, from: fmt.parseDate(normIso(r.from)), to: r.to ? fmt.parseDate(normIso(r.to)) : new Date() })).filter((r) => r.from && r.to && r.to > r.from);
    if (series.length && runs.length && last.length) {
      const x0 = last[0].ts.getTime();
      series[0].markArea = { silent: true, animation: false, itemStyle: { color: t.violet, opacity: 0.12 }, label: { show: true, position: 'insideTop', color: t['ink-2'], fontSize: 11, formatter: (p) => p.name },
        data: runs.map((r) => [{ name: `Modell geladen: ${r.name}`, xAxis: Math.max(x0, r.from.getTime()) }, { xAxis: r.to.getTime() }]) };
    }
    return { option: {
      grid: { left: 56, right: 20, top: 36, bottom: 44 + lrows * 18 },
      xAxis: { type: 'time', axisLabel: { formatter: (v) => fmt.time(new Date(v)), hideOverlap: true }, splitLine: { show: false }, axisPointer: { show: true, type: 'line', lineStyle: { color: t['chart-cap'], type: 'dashed' }, label: { show: false } } },
      yAxis: { type: 'value', name: 'Leistung in Watt (gestapelt)', nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => fmt.int(v) } },
      legend: { bottom: 6, icon: 'roundRect', itemWidth: 12, itemHeight: 8, data: lab },
      series }, last, height: 290 + lrows * 18 };
  };
  let state = { mode, perDevice, date }, built = null;
  const make = () => { built = state.mode === 'h48' ? build48() : state.mode === 'single' && state.date ? buildDay(state.date) : state.perDevice ? buildPer() : buildDay(); return built; };
  const first = make();
  const api = await makeChart(container, first.option, { height: first.height, minWidth: 440, aria: 'Leistungsprofil',
    tip: (p) => {
      if (p.componentType !== 'series') return null;
      if (state.mode === 'h48') { const h = built.last[p.dataIndex]; return h ? { title: `${p.seriesName} · ${fmt.dateTime(h.ts)}`, rows: [['Leistung', `${fmt.int(h.per[devices.find((d) => d.label === p.seriesName)?.node])} W`], ['Alle Geräte', `${fmt.int(h.total)} W`]] } : null; }
      if (state.perDevice) { const d = devices.find((x) => x.label === p.seriesName); if (!d) return null; const s = statsFor((hh) => hh.per[d.node])[p.dataIndex]; return { title: `${d.label} · ${hourLabel(p.dataIndex)}`, rows: [['Median', `${fmt.int(s.med)} W`], ['P10–P90', hasBand && s.p10 !== null ? `${fmt.int(s.p10)}–${fmt.int(s.p90)} W` : 'noch kein Band'], ['Grundlast', typeof d.baseW === 'number' ? `${fmt.int(d.baseW)} W` : '–'], ['Erhebung', METHOD_WORD[d.method]], ['Basis', `${fmt.int(s.n)} Stunden aus ${fmt.int(nDays)} Tagen`]] }; }
      const s = built.st?.[p.dataIndex]; if (!s) return null;
      if (built.single) return { title: `${fmt.date(built.single)} · ${hourLabel(s.hr)}`, rows: [['Leistung', s.med === null ? 'keine Daten' : `${fmt.int(s.med)} W`], ['Grundlast', `${fmt.int(baseW)} W`]] };
      return { title: `Durchschnittstag · ${hourLabel(s.hr)}`, rows: [['Median', `${fmt.int(s.med)} W`], ['P10–P90', hasBand && s.p10 !== null ? `${fmt.int(s.p10)}–${fmt.int(s.p90)} W` : 'noch kein Band (unter 3 Tagen)'], ['Grundlast', `${fmt.int(baseW)} W`], ['Basis', `${fmt.int(s.n)} Stunden aus ${fmt.int(nDays)} Tagen`]] };
    } });
  const set = (next) => { state = { ...state, ...next }; const b = make(); container.style.height = `${b.height}px`; api.update(b.option, true); api.chart.resize(); };
  return { api, set, nDays, hasBand, get state() { return state; } };
}
/** ISO-Zeitstempel mit Offset ohne Doppelpunkt (+0200) für Date() normalisieren. */
export function normIso(s) { return typeof s === 'string' ? s.replace(/([+-]\d{2})(\d{2})$/, '$1:$2') : s; }

/* ------------------------------------------------------------------ V7 Jahres-Pacing */
/** actual: [{date, kwh}] (aufsteigend, Tage seit Verlaufsbeginn); avgDay/p10Day/p90Day: kWh pro Tag; household: kWh pro Jahr */
export async function pacingE(container, { year, actual, avgDay, p10Day, p90Day, household, observedSince }) {
  const t = readTokens();
  const key = d3.timeFormat('%Y-%m-%d');
  const start = new Date(year, 0, 1), end = new Date(year, 11, 31), today = d3.timeDay.floor(new Date());
  let cum = 0; const ist = [];
  const byDate = new Map(actual.map((a) => [a.date, a.kwh]));
  for (const d of d3.timeDays(start, d3.timeDay.offset(today, 1))) { const k = key(d); if (byDate.has(k)) { cum += byDate.get(k); ist.push([d.getTime(), Math.round(cum * 10) / 10]); } }
  const cumToday = cum;
  const fc = [], lo = [], span = [];
  if (typeof avgDay === 'number') {
    let k = 0;
    for (const d of d3.timeDays(today, d3.timeDay.offset(end, 1))) { fc.push([d.getTime(), Math.round((cumToday + avgDay * k) * 10) / 10]); if (typeof p10Day === 'number') { lo.push([d.getTime(), cumToday + p10Day * k]); span.push([d.getTime(), (p90Day - p10Day) * k]); } k++; }
  }
  const hh = typeof household === 'number' ? d3.timeDays(start, d3.timeDay.offset(end, 1)).filter((d, i) => i % 7 === 0 || i === 364).map((d) => [d.getTime(), Math.round((household / 365) * (d3.timeDay.count(start, d) + 1))]) : [];
  const series = [
    ...(hh.length ? [{ name: 'Haushalt (Referenz)', type: 'line', symbol: 'none', silent: true, data: hh, lineStyle: { type: 'dotted', width: 1.2, color: t['ink-3'] }, itemStyle: { color: t['ink-3'] }, emphasis: { disabled: true } }] : []),
    ...(lo.length ? [{ name: 'P10', type: 'line', stack: 'band', symbol: 'none', silent: true, lineStyle: { opacity: 0 }, data: lo, tooltip: { show: false }, emphasis: { disabled: true } }, { name: 'Prognoseband', type: 'line', stack: 'band', symbol: 'none', silent: true, lineStyle: { opacity: 0 }, areaStyle: { color: t.blue, opacity: 0.14 }, data: span, emphasis: { disabled: true } }] : []),
    { name: 'Erfasst', type: 'line', symbol: 'none', data: ist, lineStyle: { width: 2.2, color: t.blue }, itemStyle: { color: t.blue } },
    ...(fc.length ? [{ name: 'Prognose', type: 'line', symbol: 'none', data: fc, lineStyle: { width: 1.8, type: 'dashed', color: t.blue }, itemStyle: { color: t.blue } }] : []),
  ];
  const option = {
    grid: { left: 56, right: 20, top: 36, bottom: 60 },
    xAxis: { type: 'time', min: start.getTime(), max: end.getTime(), axisLabel: { formatter: (v) => fmt.month(new Date(v)), hideOverlap: true }, splitLine: { show: false } },
    yAxis: { type: 'value', name: 'kWh kumuliert', nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => fmt.int(v) } },
    legend: { bottom: 6, icon: 'roundRect', itemWidth: 12, itemHeight: 8, data: ['Erfasst', ...(fc.length ? ['Prognose'] : []), ...(lo.length ? ['Prognoseband'] : []), ...(hh.length ? ['Haushalt (Referenz)'] : [])] },
    series,
  };
  const api = await makeChart(container, option, { height: 280, minWidth: 440, aria: `Jahresverlauf ${year} kumuliert`,
    tip: (p) => {
      if (p.componentType !== 'series' || !Array.isArray(p.value)) return null;
      const d = new Date(p.value[0]);
      const rows = [[p.seriesName, `${fmt.int(p.value[1])} kWh`]];
      if (p.seriesName === 'Prognose') { const i = fc.findIndex((x) => x[0] === p.value[0]); if (i >= 0 && lo[i]) rows.push(['Band P10–P90', `${fmt.int(lo[i][1])}–${fmt.int(lo[i][1] + span[i][1])} kWh`]); }
      if (typeof household === 'number') rows.push(['Haushalt bis hier', `${fmt.int((household / 365) * (d3.timeDay.count(start, d) + 1))} kWh`]);
      if (observedSince) rows.push(['Erfasst seit', fmt.date(observedSince)]);
      return { title: fmt.date(d), rows };
    } });
  return { api, cumToday, yearEnd: fc.length ? fc[fc.length - 1][1] : null };
}

/* ------------------------------------------------------------------ V8 Kalender */
/** dayMap: Map 'YYYY-MM-DD' → {kwh, complete, hours}; Tage ohne Eintrag = keine Daten (Punktraster). */
export async function calendarHeatE(container, dayMap, { weeks = 26, now = new Date(), since = null } = {}) {
  const t = readTokens();
  const end = d3.timeDay.floor(now);
  const start = d3.timeMonday.offset(d3.timeMonday.floor(end), -(weeks - 1));
  const vals = [...dayMap.values()].filter((v) => v.complete !== false).map((v) => v.kwh);
  const q = d3.scaleQuantile().domain(vals.length >= 4 ? vals : [0, 1]).range([1, 2, 3, 4]);
  const key = d3.timeFormat('%Y-%m-%d');
  const colors = [t['cal-0'], t['cal-1'], t['cal-2'], t['cal-3'], t['cal-4']];
  const sinceKey = since ? key(d3.timeDay.floor(new Date(since))) : null;
  const data = d3.timeDays(start, d3.timeDay.offset(end, 1)).map((d) => {
    const k = key(d), v = dayMap.get(k);
    if (!v) return { value: [k, 0], kind: sinceKey && k < sinceKey ? 'before' : 'missing', itemStyle: { color: sinceKey && k < sinceKey ? t.card : t['chart-hatch-bg'], borderType: 'dashed', borderColor: t['chart-hatch'], borderWidth: 1 } };
    return { value: [k, v.kwh], kind: v.complete === false ? 'partial' : 'ok', info: v, itemStyle: { color: colors[q(v.kwh)], ...(v.complete === false ? { borderType: 'dashed', borderColor: t['ink-3'], borderWidth: 1 } : {}) } };
  });
  const option = {
    calendar: { top: 28, left: 34, right: 8, cellSize: ['auto', 14], range: [key(start), key(end)], orient: 'horizontal', dayLabel: { firstDay: 1, nameMap: ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'], margin: 8 }, monthLabel: { nameMap: 'DE', margin: 8 }, splitLine: { show: false }, itemStyle: { borderWidth: 2, borderColor: t.card, color: t.card } },
    series: [{ type: 'heatmap', coordinateSystem: 'calendar', data, emphasis: { itemStyle: { borderColor: t.blue, borderWidth: 1.5 } } }],
  };
  const api = await makeChart(container, option, { height: 150, minWidth: 420, aria: `Kalender kWh pro Tag, ${weeks} Wochen`,
    tip: (p) => (p.data ? { title: fmt.date(p.data.value[0]), rows: [['Verbrauch', p.data.kind === 'missing' ? 'keine Daten' : p.data.kind === 'before' ? 'vor Verlaufsbeginn' : `${fmt.num1(p.data.value[1])} kWh`], ...(p.data.info ? [['Stunden erfasst', `${fmt.int(p.data.info.hours)} von ${fmt.int(p.data.info.possible ?? 24)}${p.data.kind === 'partial' ? ' – unvollständig' : ''}`]] : [])] } : null) });
  return { api, days: data.filter((d) => d.kind === 'ok' || d.kind === 'partial').length };
}

/* ------------------------------------------------------------------ V6 Wasserfall */
/** items: [{measure, kwh, costChf, co2Kg, method}], start: {kwh, costChf, co2Kg} (Jahresprognose) */
export async function waterfallE(container, { start, items, unit = 'kwh' }) {
  const t = readTokens();
  const build = (u) => {
    const f = unitFmt(u);
    const cats = ['Prognose', ...items.map((i) => i.measure), 'möglich'];
    let run = start[u]; const base = [0], val = [{ value: start[u], itemStyle: { color: t.blue } }];
    for (const it of items) { const v = Math.min(it[u], run); run -= v; base.push(run); val.push({ value: v, itemStyle: { color: t.orange, decal: it.method === 'estimated' ? decalFor('estimated', t) : null } }); }
    base.push(0); val.push({ value: run, itemStyle: { color: t['cal-3'] } });
    const maxLabel = Math.max(70, Math.floor((widthOf(container, 440) - 80) / cats.length) - 8);
    return {
      grid: { left: 56, right: 20, top: 36, bottom: 70 },
      xAxis: { type: 'category', data: cats, axisLabel: { interval: 0, width: maxLabel, overflow: 'truncate', lineHeight: 14, formatter: (v) => v } },
      yAxis: { type: 'value', name: `${unitLabel(u)} pro Jahr`, nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => f(v) } },
      series: [
        { name: 'base', type: 'bar', stack: 'w', silent: true, itemStyle: { color: 'transparent' }, emphasis: { disabled: true }, data: base },
        { name: 'Wert', type: 'bar', stack: 'w', barMaxWidth: 48, data: val, label: { show: true, position: 'top', fontSize: 11, color: t['ink-2'], formatter: (p) => (p.dataIndex === 0 || p.dataIndex === cats.length - 1 ? `≈ ${f(p.value)}` : `−${f(p.value)}`) } },
      ],
    };
  };
  let cur = unit;
  const api = await makeChart(container, build(unit), { height: 300, minWidth: 440, aria: 'Einsparpotenziale als Wasserfall',
    tip: (p) => {
      if (p.componentType !== 'series' || p.seriesName !== 'Wert') return null;
      const f = unitFmt(cur), i = p.dataIndex;
      if (i === 0) return { title: 'Jahresprognose', rows: [[unitLabel(cur), `≈ ${f(start[cur])}`], ['Hinweis', 'Hochrechnung aus den letzten 30 Tagen, kein Messwert']] };
      if (i === items.length + 1) return { title: 'Möglich nach allen Massnahmen', rows: [[unitLabel(cur), `≈ ${f(p.value)}`], ['Hinweis', 'Schätzung; die Annahmen stehen unter „So rechnen wir“']] };
      const it = items[i - 1];
      return { title: it.measure, rows: [['Einsparung', `bis zu ${f(it[cur])} ${unitLabel(cur)} pro Jahr`], ['kWh / CHF / kg', `${fmt.num1(it.kwh)} / ${fmt.num2(it.costChf)} / ${fmt.num1(it.co2Kg)}`], ['Aufwand', it.effort ?? null], ['Nebenwirkung', it.sideEffects ?? null], ['Annahme', it.assumptions ?? null], ['Erhebung', METHOD_WORD[it.method] ?? 'geschätzt']] };
    } });
  return { api, setUnit(u) { cur = u; api.update(build(u), true); } };
}

/* ------------------------------------------------------------------ V5 Sankey */
/** nodes: [{id, label, kind: 'grid'|'device'|'purpose', method}], links: [{source, target, value(kWh), costChf, co2Kg, method, rule}] */
export function purposeSankey(container, { nodes, links }) {
  if (!nodes.length || !links.length) return null;
  return responsive(container, (W) => {
    const H = Math.max(260, 70 + Math.max(nodes.filter((n) => n.kind === 'device').length, nodes.filter((n) => n.kind === 'purpose').length) * 44);
    const labelSpace = Math.min(230, Math.max(130, W * 0.26));
    const sankey = d3.sankey().nodeId((n) => n.id).nodeWidth(12).nodePadding(22).nodeAlign(d3.sankeyJustify).nodeSort(null).extent([[4, 28], [W - labelSpace, H - 8]]);
    const graph = sankey({ nodes: nodes.map((n) => ({ ...n })), links: links.map((l) => ({ ...l })) });
    const svg = svgPx(container, W, H, 'Stromfluss Netz → Gerät → Zweck');
    for (const [txt, x, a] of [['Stromnetz', 4, 'start'], ['Gerät', (W - labelSpace) / 2, 'middle'], ['Zweck', W - labelSpace, 'start']]) svg.append('text').attr('class', 'sankey__col').attr('x', x).attr('y', 12).attr('text-anchor', a).text(txt);
    const link = svg.append('g').selectAll('path').data(graph.links).join('path')
      .attr('class', (l) => `sankey__link sankey__link--ok${l.method === 'measured' ? '' : ' esankey__link--est'}`)
      .attr('d', d3.sankeyLinkHorizontal()).attr('stroke-width', (l) => Math.max(1.5, l.width)).attr('tabindex', 0)
      .attr('aria-label', (l) => `${l.source.label} → ${l.target.label}: ${fmt.num1(l.value)} kWh`);
    tip.bind(link, (l) => ({ title: `${l.source.label} → ${l.target.label}`, rows: [['Energie (30 Tage)', `${fmt.num1(l.value)} kWh`], ['Kosten', `${fmt.num2(l.costChf)} CHF`], ['CO₂e', `${fmt.num2(l.co2Kg)} kg`], ['Erhebung', METHOD_WORD[l.method] ?? 'geschätzt'], ['Zuordnung', l.rule ?? null]] }));
    svg.append('g').selectAll('rect').data(graph.nodes).join('rect')
      .attr('class', (n) => `sankey__node esankey__node--${n.kind}${n.method && n.method !== 'measured' ? ' esankey__node--est' : ''}`)
      .attr('x', (n) => n.x0).attr('y', (n) => n.y0).attr('width', (n) => n.x1 - n.x0).attr('height', (n) => Math.max(1, n.y1 - n.y0)).attr('rx', 3);
    const lab = svg.append('g').selectAll('g').data(graph.nodes).join('g').attr('transform', (n) => `translate(${n.x1 + 8}, ${(n.y0 + n.y1) / 2})`);
    lab.append('text').attr('class', 'sankey__label sankey__label--strong').attr('dy', '-0.2em').text((n) => n.label).each(function (n) { fitOne(this, n.kind === 'purpose' ? labelSpace - 12 : (W - labelSpace) / 3 - 24); });
    lab.append('text').attr('class', 'sankey__label sankey__label--sub').attr('dy', '1.1em').text((n) => `≈ ${fmt.num1(n.value)} kWh${n.method && n.method !== 'measured' ? ` · ${METHOD_WORD[n.method] ?? 'geschätzt'}` : ''}`).each(function (n) { fitOne(this, n.kind === 'purpose' ? labelSpace - 12 : (W - labelSpace) / 3 - 24); });
  }, { minWidth: 460, scroll: true });
}

/* ------------------------------------------------------------------ Kosten pro Tag */
export async function costSeriesE(container, daily, prices, { days = 90 } = {}) {
  const t = readTokens();
  const byDate = new Map();
  for (const e of daily) { const k = String(e.date).slice(0, 10); byDate.set(k, (byDate.get(k) ?? 0) + (typeof e.costChf === 'number' ? e.costChf : 0)); }
  const dates = [...byDate.keys()].sort().slice(-days);
  if (!dates.length) return null;
  const marks = (prices ?? []).filter((p) => p.validFrom && p.validFrom >= dates[0] && p.validFrom <= dates[dates.length - 1]).map((p) => ({ xAxis: p.validFrom, name: `${fmt.num1(p.chfPerKwh * 100)} Rp./kWh`, label: { formatter: (m) => m.name, position: 'end', distance: 4, color: t['ink-2'], fontSize: 11 }, lineStyle: { color: t.orange, type: 'dashed', width: 1.2 } }));
  const option = {
    grid: { left: 56, right: 20, top: 36, bottom: 40 },
    xAxis: { type: 'category', data: dates, axisLabel: { formatter: (v) => fmt.dateShort(v), hideOverlap: true } },
    yAxis: { type: 'value', name: 'CHF pro Tag', nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, axisLabel: { formatter: (v) => fmt.num2(v) } },
    series: [{ name: 'Kosten', type: 'line', showSymbol: false, data: dates.map((d) => Math.round(byDate.get(d) * 1000) / 1000), lineStyle: { width: 2, color: t.blue }, itemStyle: { color: t.blue }, areaStyle: { color: t['chart-area'] }, markLine: { silent: true, symbol: 'none', animation: false, data: marks } }],
  };
  return makeChart(container, option, { height: 220, minWidth: 440, aria: 'Stromkosten pro Tag',
    tip: (p) => (p.componentType === 'series' ? { title: fmt.date(p.name), rows: [['Kosten', `${fmt.num2(p.value)} CHF`], ['Preis', (() => { const e = daily.find((x) => String(x.date).slice(0, 10) === p.name && typeof x.priceChfPerKwh === 'number'); return e ? `${fmt.num1(e.priceChfPerKwh * 100)} Rp./kWh` : null; })()]] } : null) });
}
