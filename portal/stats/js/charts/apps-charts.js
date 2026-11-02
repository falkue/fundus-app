/**
 * Diagramme für Applikationsüberblick und Firebase-Monitoring.
 *   portfolioMatrix(container, apps, {onPick})         D3: Aktivität (Reads/Tag, log) × Kosten/Monat (USD)
 *   readsHeatmap(container, apps, {onPick})           D3: App × Tag (30 Tage), Lesezugriffe, log-Stufen
 *   appSeriesE(container, apps)                       ECharts: reads/writes/deletes je App, gemeinsamer dataZoom
 *   hourlyProfileE(container, apps)                   ECharts: 48-h-Stundenprofil je App (Kleinvielfache)
 * Hilfen: appStats(app) – abgeleitete Kennzahlen (Reads/Tag, Kosten/Monat, Aktivität), nie optimistisch.
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { fitOne } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';
import { norm, STATES } from '../lib/state.js';
import { readTokens } from '../lib/theme.js';
import { makeChart } from './echarts-base.js';

const d3 = globalThis.d3;
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const last = (arr, n = 1) => (Array.isArray(arr) ? arr.slice(-n) : []);
const sum = (arr) => d3.sum(arr, (p) => (isNum(p?.[1]) ? p[1] : 0));

/** Kennzahlen je App aus series/hourly/quotas; null, wenn nicht erhoben. */
export function appStats(a) {
  const s = a.series ?? null;
  const readsDay = s?.reads?.length ? sum(last(s.reads, 7)) / Math.min(7, s.reads.length) : null;
  const reads30 = s?.reads?.length ? sum(s.reads) : null;
  const writesDay = s?.writes?.length ? sum(last(s.writes, 7)) / Math.min(7, s.writes.length) : null;
  const fnDay = s?.fnExec?.length ? sum(last(s.fnExec, 7)) / Math.min(7, s.fnExec.length) : null;
  const fnErrDay = s?.fnErrors?.length ? sum(last(s.fnErrors, 7)) / Math.min(7, s.fnErrors.length) : null;
  const cost30 = s?.costUsd?.length ? sum(s.costUsd) : (isNum(a.costUsdDay) ? a.costUsdDay * 30 : null);
  const costMonth = cost30 !== null ? (cost30 / Math.max(1, s?.costUsd?.length ?? 30)) * 30 : null;
  const active = readsDay !== null ? readsDay >= 100 || (fnDay ?? 0) >= 10 : (a.hosting?.some((h) => h.status) ? true : null);
  const hasData = !!(s && (s.reads?.length || s.fnExec?.length));
  return { readsDay, reads30, writesDay, fnDay, fnErrDay, errRate: fnDay ? (fnErrDay ?? 0) / fnDay : null, costMonth, active, hasData };
}

// ---------------------------------------------------------------- Portfolio-Matrix
export function portfolioMatrix(container, apps, { onPick } = {}) {
  const rows = apps.map((a) => ({ a, st: appStats(a) }));
  return responsive(container, (W) => {
    const H = 340, m = { top: 20, right: 24, bottom: 86, left: 64 };
    const svg = svgPx(container, W, H, 'Portfolio: Aktivität gegen Kosten');
    const withData = rows.filter((r) => r.st.readsDay !== null);
    const xMax = Math.max(1000, d3.max(withData, (r) => r.st.readsDay) ?? 1000);
    const x = d3.scaleSymlog().constant(100).domain([0, xMax * 1.6]).range([m.left, W - m.right]);
    const yMax = Math.max(1, d3.max(withData, (r) => r.st.costMonth ?? 0) ?? 1);
    const y = d3.scaleLinear().domain([0, yMax * 1.25]).range([H - m.bottom, m.top]).nice();
    const ticks = [0, 100, 1000, 10000, 100000, 1000000].filter((t) => t <= xMax * 1.6);
    svg.append('g').attr('class', 'axis grid').attr('transform', `translate(0, ${H - m.bottom})`).call(d3.axisBottom(x).tickValues(ticks).tickFormat((v) => fmt.int(v)).tickSize(-(H - m.top - m.bottom)));
    svg.append('g').attr('class', 'axis grid').attr('transform', `translate(${m.left}, 0)`).call(d3.axisLeft(y).ticks(4).tickFormat((v) => `${fmt.num1(v)}`).tickSize(-(W - m.left - m.right)));
    svg.append('text').attr('class', 'axis-title').attr('x', W - m.right).attr('y', H - m.bottom + 32).attr('text-anchor', 'end').text('Aktivität: Firestore-Lesezugriffe pro Tag (Ø 7 Tage, logarithmisch)');
    svg.append('text').attr('class', 'axis-title').attr('x', m.left + 6).attr('y', m.top - 6).text('Kosten pro Monat in USD (Schätzung)');
    // Spark-Grenze 50k Reads/Tag
    svg.append('line').attr('class', 'line line--threshold').attr('x1', x(50000)).attr('x2', x(50000)).attr('y1', m.top).attr('y2', H - m.bottom);
    const sl = svg.append('text').attr('class', 'chart-note').attr('x', x(50000) + 4).attr('y', m.top + 10).text('Spark-Gratisgrenze 50’000/Tag');
    if (x(50000) + 4 + sl.node().getComputedTextLength() > W - m.right) sl.attr('x', x(50000) - 4).attr('text-anchor', 'end');
    // Punkte
    const pts = svg.selectAll('g.pt').data(withData).join('g').attr('class', 'pm-pt').attr('transform', (r) => `translate(${x(r.st.readsDay)}, ${y(r.st.costMonth ?? 0)})`).attr('tabindex', 0).attr('role', 'button').style('cursor', 'pointer')
      .attr('aria-label', (r) => `${r.a.label}: ${fmt.int(r.st.readsDay)} Reads/Tag, ${fmt.num2(r.st.costMonth ?? 0)} USD/Monat`);
    pts.append('circle').attr('r', (r) => (r.a.tier === 'Spark' ? 7 : 9)).attr('class', (r) => `pm-pt__dot pm-pt__dot--${norm(r.a.state)}${r.a.tier === 'Spark' ? ' pm-pt__dot--spark' : ''}`);
    pts.append('use').attr('href', (r) => `#${STATES[norm(r.a.state)].symbol}`).attr('x', -5).attr('y', -5).attr('width', 10).attr('height', 10).attr('class', (r) => `mark mark--${norm(r.a.state)}`);
    // Beschriftung: rechts, bei Platzmangel links; Kollisionen vertikal versetzen
    const placed = [];
    pts.each(function (r) {
      const px = x(r.st.readsDay), py = y(r.st.costMonth ?? 0);
      const nearAxis = py > H - m.bottom - 18;
      let lx = px + 12, ly = nearAxis ? py - 12 : py + 4, anchor = 'start';
      const t = d3.select(this).append('text').attr('class', 'chart-label').attr('x', 12).attr('y', ly - py).text(r.a.label);
      const w = t.node().getComputedTextLength();
      if (px + 12 + w > W - m.right) { lx = px - 12; anchor = 'end'; t.attr('x', -12).attr('text-anchor', 'end'); }
      let guard = 0;
      const dir = py > (m.top + (H - m.bottom)) / 2 ? -14 : 14; // unten: nach oben ausweichen (Achse frei halten)
      while (placed.some((p) => Math.abs(p.y - ly) < 14 && !(p.x1 < (anchor === 'start' ? lx : lx - w) || p.x0 > (anchor === 'start' ? lx + w : lx))) && guard++ < 8) { ly += dir; t.attr('y', ly - py); }
      placed.push({ x0: anchor === 'start' ? lx : lx - w, x1: anchor === 'start' ? lx + w : lx, y: ly });
    });
    tip.bind(pts, (r) => ({ title: `${r.a.label}${r.a.tier ? ` · ${r.a.tier}` : ''}`, rows: [['Reads/Tag (Ø 7 T.)', fmt.int(r.st.readsDay)], ['Kosten/Monat', `${fmt.num2(r.st.costMonth ?? 0)} USD (Schätzung)`], ['Functions/Tag', r.st.fnDay === null ? null : fmt.int(r.st.fnDay)], ['Zustand', STATES[norm(r.a.state)].word]], note: 'Klick öffnet die Details' }));
    pts.on('click', (e, r) => onPick?.(r.a)).on('keydown', (e, r) => { if (e.key === 'Enter') onPick?.(r.a); });
    // Apps ohne Nutzungsdaten: Reihe unter der Achse
    const noData = rows.filter((r) => r.st.readsDay === null);
    if (noData.length) {
      const g = svg.append('g').attr('transform', `translate(${m.left}, ${H - 34})`);
      g.append('text').attr('class', 'chart-note').attr('x', 0).attr('y', 4).text('ohne Nutzungsdaten:');
      let cx = 118, shown = 0;
      for (const r of noData) {
        const gg = g.append('g').attr('transform', `translate(${cx}, 0)`).attr('tabindex', 0).style('cursor', 'pointer').on('click', () => onPick?.(r.a));
        gg.append('circle').attr('r', 6).attr('class', 'pm-pt__dot pm-pt__dot--unknown');
        const t = gg.append('text').attr('class', 'chart-label').attr('x', 10).attr('y', 4).text(r.a.label);
        fitOne(t.node(), Math.min(120, W - m.right - m.left - cx - 14));
        const tw = t.node().getComputedTextLength();
        if (m.left + cx + 10 + tw > W - m.right || tw < 20) { gg.remove(); break; }
        tip.bind(gg, () => ({ title: r.a.label, rows: [['Nutzungsdaten', r.a.access === 'missing' ? 'nicht freigegeben' : 'nicht erhoben'], ['Hinweis', r.a.accessNote ?? null]] }));
        cx += tw + 36; shown++;
      }
      if (shown < noData.length) { const more = g.append('text').attr('class', 'chart-note').attr('x', cx).attr('y', 4).text(`+${noData.length - shown}`); if (m.left + cx + more.node().getComputedTextLength() > W - m.right) more.remove(); }
    }
  }, { minWidth: 420, scroll: true });
}

// ---------------------------------------------------------------- Heatmap App × Tag
export function readsHeatmap(container, apps, { onPick, now = new Date() } = {}) {
  const key = d3.timeFormat('%Y-%m-%d');
  const end = d3.timeDay.floor(now);
  const days = d3.timeDays(d3.timeDay.offset(end, -29), d3.timeDay.offset(end, 1));
  const rows = apps.map((a) => ({ a, map: new Map((a.series?.reads ?? []).map(([d, v]) => [String(d).slice(0, 10), v])) }));
  const vals = rows.flatMap((r) => [...r.map.values()]).filter((v) => isNum(v) && v > 0);
  const maxV = d3.max(vals) ?? 1;
  const level = (v) => (!isNum(v) ? null : v <= 0 ? 0 : Math.min(4, Math.max(1, Math.ceil((Math.log10(v) / Math.log10(Math.max(10, maxV))) * 4))));
  return responsive(container, (W) => {
    const left = Math.min(180, Math.max(120, W * 0.2)), top = 24, rowH = 24, gap = 2, right = 92;
    const cell = Math.max(8, Math.floor((W - left - right) / days.length) - gap);
    const H = top + rows.length * rowH + 24;
    const svg = svgPx(container, W, H, 'Lesezugriffe je App und Tag');
    const labelDays = days.filter((d, i) => i === 0 || d.getDate() === 1 || i % 7 === 0 || i === days.length - 1).filter((d, i, arr) => i === 0 || (days.indexOf(d) - days.indexOf(arr[i - 1])) >= 4);
    svg.selectAll('text.d').data(labelDays).join('text').attr('class', 'cal__label').attr('x', (d) => left + days.indexOf(d) * (cell + gap)).attr('y', 14).text((d) => fmt.dateShort(d));
    const g = svg.selectAll('g.row').data(rows).join('g').attr('transform', (r, i) => `translate(0, ${top + i * rowH})`);
    const lab = g.append('text').attr('class', 'chart-label').attr('x', 0).attr('y', rowH / 2 + 4).text((r) => r.a.label).style('cursor', 'pointer').on('click', (e, r) => onPick?.(r.a));
    lab.each(function () { fitOne(this, left - 12); });
    g.each(function (r) {
      const row = d3.select(this);
      const cells = row.selectAll('rect').data(days).join('rect')
        .attr('x', (d) => left + days.indexOf(d) * (cell + gap)).attr('y', 3).attr('width', cell).attr('height', rowH - 6).attr('rx', 2)
        .attr('class', (d) => { const v = r.map.get(key(d)); const l = level(v); return `cal__day ${l === null ? 'hm__gap' : `cal__day--${l}`}`; })
        .attr('tabindex', 0);
      tip.bind(cells, (d) => { const v = r.map.get(key(d)); return { title: `${r.a.label} · ${fmt.date(d)}`, rows: [['Lesezugriffe', isNum(v) ? fmt.int(v) : 'keine Daten']] }; });
      const total = sum(r.a.series?.reads ?? []);
      row.append('text').attr('class', 'chart-value').attr('x', W - 4).attr('y', rowH / 2 + 4).attr('text-anchor', 'end').text(r.a.series?.reads?.length ? fmt.int(total) : '–');
    });
    svg.append('text').attr('class', 'chart-note').attr('x', W - 4).attr('y', 14).attr('text-anchor', 'end').text('Σ 30 Tage');
    svg.append('text').attr('class', 'chart-note').attr('x', left).attr('y', H - 4).text(`Stufen logarithmisch bis ${fmt.int(maxV)} Reads/Tag; Schraffur = keine Daten. Klick auf den Namen öffnet die Details.`).each(function () { fitOne(this, W - left); });
  }, { minWidth: 520, scroll: true });
}

// ---------------------------------------------------------------- ECharts: Reads/Writes/Deletes je App
export async function appSeriesE(container, apps, { cols = 2 } = {}) {
  const t = readTokens();
  const colPx = Math.max(160, Math.floor((container.clientWidth || 800) / cols) - 40);
  const list = apps.filter((a) => a.series?.reads?.length);
  if (!list.length) return null;
  const h = 150, gapY = 52, top = 40, bottom = 70;
  const rowsN = Math.ceil(list.length / cols);
  const grids = [], xAxes = [], yAxes = [], series = [], titles = [];
  list.forEach((a, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    const left = `${(100 / cols) * c + 3}%`, width = `${100 / cols - 6}%`;
    const gTop = top + r * (h + gapY);
    grids.push({ left, width, top: gTop, height: h - 36 });
    xAxes.push({ type: 'time', gridIndex: i, axisLabel: { formatter: (v) => fmt.dateShort(new Date(v)), hideOverlap: true, alignMinLabel: 'left', alignMaxLabel: 'right' }, splitLine: { show: false }, axisPointer: { show: true, label: { show: false } } });
    yAxes.push({ type: 'value', gridIndex: i, splitNumber: 3, axisLabel: { formatter: (v) => fmt.int(v), hideOverlap: true }, min: 0 });
    titles.push({ text: `${a.label} · ${a.tier ?? ''}`.trim(), left, top: gTop - 32, textStyle: { fontSize: 12.5, fontWeight: 500, color: t.ink, width: colPx, overflow: 'truncate' } });
    const mk = (name, key, color, width = 2) => ({ name, type: 'line', xAxisIndex: i, yAxisIndex: i, showSymbol: false, data: (a.series[key] ?? []).map(([d, v]) => [new Date(d).getTime(), v]), lineStyle: { width, color }, itemStyle: { color }, areaStyle: key === 'reads' ? { color: t['chart-area'] } : undefined });
    series.push(mk('Lesezugriffe', 'reads', t.blue), mk('Schreibzugriffe', 'writes', t['ink-2'], 1.5), mk('Löschvorgänge', 'deletes', t['ink-4'], 1.5));
  });
  const option = {
    grid: grids, xAxis: xAxes, yAxis: yAxes, series, title: titles,
    legend: { bottom: 34, data: ['Lesezugriffe', 'Schreibzugriffe', 'Löschvorgänge'], icon: 'roundRect', itemWidth: 12, itemHeight: 8 },
    dataZoom: [{ type: 'slider', xAxisIndex: xAxes.map((_, i) => i), bottom: 4, height: 20, brushSelect: false, labelFormatter: (v) => fmt.dateShort(new Date(v)) }, { type: 'inside', xAxisIndex: xAxes.map((_, i) => i) }],
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t['chart-cap'], type: 'dashed' } },
  };
  return makeChart(container, option, { height: top + rowsN * (h + gapY) + bottom - gapY + 10, aria: `Firestore-Zugriffe je App, ${list.length} Apps`, minWidth: 480,
    tip: (p) => (p.componentType === 'series' ? { title: `${titles[Math.floor(p.seriesIndex / 3)].text} · ${fmt.date(new Date(p.value[0]))}`, rows: [[p.seriesName, fmt.int(p.value[1])]] } : null) });
}

// ---------------------------------------------------------------- ECharts: 48-h-Profil
export async function hourlyProfileE(container, apps, { cols = 3 } = {}) {
  const t = readTokens();
  const colPx = Math.max(140, Math.floor((container.clientWidth || 800) / cols) - 36);
  const list = apps.filter((a) => a.hourly?.reads?.length);
  if (!list.length) return null;
  const h = 110, gapY = 48, top = 36;
  const rowsN = Math.ceil(list.length / cols);
  const grids = [], xAxes = [], yAxes = [], series = [], titles = [];
  list.forEach((a, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    const left = `${(100 / cols) * c + 2.5}%`, width = `${100 / cols - 5}%`;
    const gTop = top + r * (h + gapY);
    grids.push({ left, width, top: gTop, height: h - 26 });
    xAxes.push({ type: 'time', gridIndex: i, axisLabel: { formatter: (v) => fmt.time(new Date(v)), hideOverlap: true, alignMinLabel: 'left', alignMaxLabel: 'right' }, splitLine: { show: false }, axisPointer: { show: true, label: { show: false } } });
    yAxes.push({ type: 'value', gridIndex: i, splitNumber: 2, min: 0, axisLabel: { formatter: (v) => fmt.int(v), hideOverlap: true } });
    const pts = a.hourly.reads.map(([d, v]) => [new Date(d).getTime(), v]);
    const peak = d3.max(pts, (p) => p[1]) ?? 0, night = d3.mean(pts.filter((p) => { const hr = new Date(p[0]).getHours(); return hr >= 1 && hr <= 5; }), (p) => p[1]) ?? 0;
    const flat = peak > 0 && night / peak > 0.6;
    titles.push({ text: `${a.label} · Spitze ${fmt.int(peak)}/h${flat ? ' · Dauerlast' : ''}`, left, top: gTop - 32, textStyle: { fontSize: 12.5, fontWeight: 500, color: flat ? t.orange : t.ink, width: colPx, overflow: 'truncate' } });
    series.push({ name: a.label, type: 'bar', xAxisIndex: i, yAxisIndex: i, data: pts, itemStyle: { color: flat ? t.orange : t.blue, borderRadius: [2, 2, 0, 0] }, barCategoryGap: '20%' });
  });
  const option = { grid: grids, xAxis: xAxes, yAxis: yAxes, series, title: titles, axisPointer: { link: [{ xAxisIndex: 'all' }] } };
  return makeChart(container, option, { height: top + rowsN * (h + gapY), aria: `Stundenprofil der Lesezugriffe, ${list.length} Apps`, minWidth: 420,
    tip: (p) => (p.componentType === 'series' ? { title: `${p.seriesName} · ${fmt.dateTime(new Date(p.value[0]))}`, rows: [['Lesezugriffe in dieser Stunde', fmt.int(p.value[1])]] } : null) });
}
