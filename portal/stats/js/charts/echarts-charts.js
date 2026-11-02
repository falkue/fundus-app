/**
 * ECharts-Diagramme (SVG-Renderer, Circle-Theme, gemeinsamer Tooltip ohne HTML-formatter):
 *   capacityChartE(container, {series, cap, range, forecast})    Verlauf: Belegung + Kapazität 80/100 % + Prognose + dataZoom
 *   smallMultiplesE(container, rows, {format})                   Verlauf: Kleinvielfache in EINER Instanz, axisPointer.link
 *   calendarE(container, daily, {weeks})                         Sicherung: Kalender-Heatmap
 *   treemapE(container, installed, {loaded, used})               KI: Modell-Treemap
 * Alle Farben aus den Tokens (theme.js); nie nur Farbe: Decal/Schraffur für „unbekannt“, Formen im Tooltip.
 */
import { makeChart } from './echarts-base.js';
import { readTokens } from '../lib/theme.js';
import * as fmt from '../lib/fmt.js';
import { reducedMotion } from '../lib/dom.js';

const d3 = globalThis.d3;

export async function capacityChartE(container, { label, points, cap, range = null, forecast = null, now = new Date(), unit }) {
  const t = readTokens();
  const tMin = range ? d3.timeDay.offset(now, -range) : (points[0]?.t ?? now);
  const vis = points.filter((p) => p.t >= tMin);
  const data = vis.map((p) => [p.t.getTime(), p.v]);
  const u = unit ?? fmt.pickByteUnit(Math.max(cap ?? 0, d3.max(vis, (p) => p.v) ?? 0));
  const div = (v) => fmt.bytesIn(v, u);
  const fc = [];
  if (forecast && typeof forecast.slopeBytesPerDay === 'number' && forecast.slopeBytesPerDay > 0 && vis.length) {
    const last = vis[vis.length - 1];
    const horizonDays = Math.max(45, Math.round((range ?? 365) * 0.6));
    const endDays = Math.min(horizonDays, typeof forecast.daysTo100 === 'number' ? forecast.daysTo100 : horizonDays);
    fc.push([last.t.getTime(), last.v], [last.t.getTime() + endDays * 864e5, last.v + forecast.slopeBytesPerDay * endDays]);
  }
  const option = {
    grid: { left: 56, right: 24, top: 36, bottom: 64, containLabel: false },
    xAxis: { type: 'time', axisLabel: { formatter: (v) => fmt.date(new Date(v)), hideOverlap: true }, splitLine: { show: false } },
    yAxis: { type: 'value', name: `Belegung in ${u}`, nameLocation: 'end', nameGap: 12, nameTextStyle: { align: 'left', padding: [0, 0, 0, -48] }, min: 0, max: cap ? Math.ceil(div(cap) * 1.05 * 10) / 10 : null, axisLabel: { formatter: (v) => fmt.num1(v).replace(/\.0$/, '') } },
    dataZoom: [{ type: 'slider', xAxisIndex: 0, bottom: 10, height: 22, brushSelect: false, labelFormatter: (v) => fmt.dateShort(new Date(v)) }, { type: 'inside', xAxisIndex: 0 }],
    series: [
      { name: label, type: 'line', showSymbol: false, data: data.map(([x, y]) => [x, div(y)]), lineStyle: { width: 2, color: t.blue }, itemStyle: { color: t.blue }, areaStyle: { color: t['chart-area'] }, emphasis: { focus: 'none' },
        markLine: cap ? { silent: true, symbol: 'none', animation: false, lineStyle: { color: t['chart-cap'], width: 1.2, type: 'solid' }, label: { position: 'insideEndTop', color: t['ink-2'], fontSize: 11, formatter: (p) => p.name },
          data: [{ yAxis: div(cap), name: `Kapazität ${fmt.bytes(cap)}` }, { yAxis: div(cap * 0.8), name: '80-%-Marke', lineStyle: { color: t.orange, type: 'dotted', width: 1 } }] } : undefined },
      { name: 'Prognose', type: 'line', showSymbol: false, data: fc.map(([x, y]) => [x, div(y)]), lineStyle: { width: 1.5, type: 'dashed', color: t['chart-forecast'] }, itemStyle: { color: t['chart-forecast'] }, silent: true },
    ],
  };
  const api = await makeChart(container, option, { height: 320, aria: `${label}: Belegung über Zeit, ${vis.length} Messpunkte`, minWidth: 440,
    tip: (p) => (p.componentType === 'series' && p.seriesName !== 'Prognose' ? { title: fmt.dateTime(new Date(p.value[0])), rows: [[label, `${fmt.num2(p.value[1])} ${u}`]] } : null) });
  // Achsen-Tooltip (Fadenkreuz) → eigener Tip über showTip-Ereignisse ist nicht nötig; Serie-Hover reicht, axisPointer als Linie
  api.update({ xAxis: { axisPointer: { show: true, type: 'line', lineStyle: { color: t['chart-cap'], type: 'dashed' }, label: { show: false } } } });
  return api;
}

/** Kleinvielfache: rows = [{id, label, points:[{t,v}], format, yDomain}] – ein Raster je Zeile, Zeiger gekoppelt. */
export async function smallMultiplesE(container, rows, { cols = 3, now = new Date(), range = null } = {}) {
  const t = readTokens();
  const n = rows.length;
  const rN = Math.ceil(n / cols);
  const h = 110, gapY = 44, top = 32;
  const grids = [], xAxes = [], yAxes = [], series = [], titles = [];
  rows.forEach((r, i) => {
    const c = i % cols, rr = Math.floor(i / cols);
    const left = `${(100 / cols) * c + 2.5}%`, width = `${100 / cols - 5}%`;
    grids.push({ left, width, top: top + rr * (h + gapY), height: h - 26 });
    const tMin = range ? d3.timeDay.offset(now, -range) : null;
    const pts = (tMin ? r.points.filter((p) => p.t >= tMin) : r.points);
    const empty = pts.length < 2;
    xAxes.push({ type: 'time', gridIndex: i, show: !empty, axisLabel: { formatter: (v) => fmt.dateShort(new Date(v)), hideOverlap: true, showMinLabel: true, showMaxLabel: true, alignMinLabel: 'left', alignMaxLabel: 'right' }, splitLine: { show: false }, axisPointer: { show: true, label: { show: false } } });
    yAxes.push({ type: 'value', gridIndex: i, show: !empty, min: r.yDomain?.[0], max: r.yDomain?.[1], splitNumber: 2, axisLabel: { formatter: (v) => r.format ? r.format(v) : String(v), hideOverlap: true } });
    const last = pts[pts.length - 1];
    titles.push({ text: `${r.label} · ${last ? r.format(last.v) : 'keine Daten'}`, left, top: top + rr * (h + gapY) - 30, textStyle: { fontSize: 12.5, fontWeight: 500, color: t.ink } });
    series.push({ name: r.label, type: 'line', xAxisIndex: i, yAxisIndex: i, showSymbol: false, data: pts.map((p) => [p.t.getTime(), p.v]), lineStyle: { width: 1.5, color: t.blue }, itemStyle: { color: t.blue }, areaStyle: { color: t['chart-area'] } });
    if (pts.length < 2) titles[titles.length - 1].subtext = pts.length === 1 ? 'erst ein Messpunkt' : 'noch kein Verlauf';
  });
  const option = { grid: grids, xAxis: xAxes, yAxis: yAxes, series, title: titles, axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t['chart-cap'], type: 'dashed' } } };
  return makeChart(container, option, { height: top + rN * (h + gapY), aria: `${n} Kleinvielfache`, minWidth: 420,
    tip: (p) => (p.componentType === 'series' ? { title: fmt.dateTime(new Date(p.value[0])), rows: [[p.seriesName, rows.find((r) => r.label === p.seriesName)?.format(p.value[1]) ?? String(p.value[1])]] } : null) });
}

/** Kalender-Heatmap der täglich gesicherten Menge (Quantile → 4 Blaustufen + Grundfarbe). */
export async function calendarE(container, daily, { weeks = 26, now = new Date() } = {}) {
  const t = readTokens();
  const end = d3.timeDay.floor(now);
  const start = d3.timeMonday.offset(d3.timeMonday.floor(end), -(weeks - 1));
  const by = new Map();
  for (const d of daily ?? []) { if (!d?.date) continue; const k = String(d.date).slice(0, 10); by.set(k, (by.get(k) ?? 0) + (typeof d.bytes === 'number' ? d.bytes : 0)); }
  const vals = [...by.values()].filter((v) => v > 0);
  const q = d3.scaleQuantile().domain(vals.length ? vals : [0, 1]).range([1, 2, 3, 4]);
  const key = d3.timeFormat('%Y-%m-%d');
  const data = d3.timeDays(start, d3.timeDay.offset(end, 1)).map((d) => { const v = by.get(key(d)); return { value: [key(d), v ?? 0], level: v === undefined ? 0 : q(v), bytes: v ?? null }; });
  const colors = [t['cal-0'], t['cal-1'], t['cal-2'], t['cal-3'], t['cal-4']];
  const option = {
    calendar: { top: 28, left: 34, right: 8, cellSize: ['auto', 14], range: [key(start), key(end)], orient: 'horizontal', dayLabel: { firstDay: 1, nameMap: ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'], margin: 8 }, monthLabel: { nameMap: 'DE', margin: 8 }, splitLine: { show: false }, itemStyle: { borderWidth: 2, borderColor: t.card, color: t.card } },
    series: [{ type: 'heatmap', coordinateSystem: 'calendar', data: data.map((d) => ({ ...d, itemStyle: { color: colors[d.level] } })), emphasis: { itemStyle: { borderColor: t.blue, borderWidth: 1.5 } } }],
  };
  const api = await makeChart(container, option, { height: 150, aria: `Kalender der letzten ${weeks} Wochen`, minWidth: 420, tip: (p) => (p.data ? { title: fmt.date(p.data.value[0]), rows: [['Gesichert', p.data.bytes === null ? 'kein Lauf' : fmt.bytes(p.data.bytes)]] } : null) });
  const runDays = data.filter((d) => d.bytes !== null).length;
  return { api, total: d3.sum(data, (d) => d.bytes ?? 0), runDays, days: data.length };
}

/** Treemap der installierten Modelle (geladen / genutzt / ungenutzt). */
export async function treemapE(container, installed, { loaded, used }) {
  const t = readTokens();
  const items = (installed ?? []).filter((m) => typeof m.bytes === 'number' && m.bytes > 0).map((m) => {
    const st = loaded.has(m.name) ? 'geladen' : used.has(m.name) ? 'genutzt' : 'ungenutzt';
    const color = st === 'geladen' ? t.blue : st === 'genutzt' ? t['chart-series-2'] : t['chart-track'];
    return { name: m.name, value: m.bytes, st, model: m, itemStyle: { color, borderColor: t.card, borderWidth: 2, gapWidth: 2 }, label: { color: st === 'geladen' ? t.card : t.ink } };
  });
  const option = {
    series: [{ type: 'treemap', roam: false, nodeClick: false, breadcrumb: { show: false }, width: '100%', height: '100%', top: 0, left: 0, data: items,
      label: { show: true, fontSize: 12, fontWeight: 500, lineHeight: 17, overflow: 'truncate', formatter: (p) => `${p.name}\n${fmt.bytes(p.value, 1)} · ${p.data.st}` },
      labelLayout: { hideOverlap: true }, itemStyle: { borderRadius: 4 }, emphasis: { itemStyle: { borderColor: t.blue } } }],
  };
  return makeChart(container, option, { height: 320, aria: `${items.length} installierte Modelle nach Grösse`, minWidth: 300,
    tip: (p) => (p.data ? { title: p.name, rows: [['Grösse', fmt.bytes(p.value)], ['Familie', p.data.model?.family ?? null], ['Geändert', p.data.model?.modified ? fmt.ago(p.data.model.modified) : null], ['Status', p.data.st === 'ungenutzt' ? 'ungenutzt (Aufräumkandidat)' : p.data.st]] } : null) });
}
export { reducedMotion };
