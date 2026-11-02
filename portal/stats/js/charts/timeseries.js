/**
 * Zeitreihen (pixelgenau): Kapazitätslinie (Belegung + Kapazität + Prognose) und
 * Kleinvielfache. Prognose: lineare Regression über den sichtbaren Zeitraum, nur bei
 * ≥ 14 Tagen Daten, gestrichelt bis 80 % / 100 % der Kapazität (Horizont begrenzt).
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { fitOne } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;

export function capacityChart(container, o) {
  const { series, now = new Date(), range = null, onDraw } = o;
  const all = series.flatMap((s) => s.points);
  if (!all.length) return null;
  const tMin = range ? d3.timeDay.offset(now, -range) : d3.min(all, (p) => p.t);
  const visible = series.map((s) => ({ ...s, points: s.points.filter((p) => p.t >= tMin) }));
  const forecasts = visible.map((s) => forecast(s, now));
  const horizon = d3.timeDay.offset(now, Math.max(45, Math.round((range ?? 365) * 0.6)));
  const fcEnd = d3.max(forecasts.filter(Boolean), (f) => f.t100 ?? f.t80) ?? null;
  const tMax = d3.max([d3.max(all, (p) => p.t), now, fcEnd ? d3.min([fcEnd, horizon]) : now]);
  const capMax = d3.max(visible, (s) => (typeof s.cap === 'number' ? s.cap : d3.max(s.cap ?? [], (c) => c.v))) ?? null;
  const yMax = d3.max([capMax ?? 0, d3.max(visible, (s) => d3.max(s.points, (p) => p.v)) ?? 0]) * 1.05 || 1;
  const unit = fmt.pickByteUnit(yMax);
  responsive(container, (W) => {
    const H = 300, m = { top: 18, right: Math.min(170, Math.max(120, W * 0.2)), bottom: 36, left: 56 };
    const x = d3.scaleTime().domain([tMin, tMax]).range([m.left, W - m.right]);
    const y = d3.scaleLinear().domain([0, yMax]).range([H - m.bottom, m.top]).nice();
    const svg = svgPx(container, W, H, 'Belegung über Zeit');
    svg.append('g').attr('class', 'axis grid').attr('transform', `translate(${m.left},0)`).call(d3.axisLeft(y).ticks(5).tickSize(-(W - m.left - m.right)).tickFormat((v) => fmt.num1(fmt.bytesIn(v, unit)).replace(/\.0$/, '')));
    svg.append('text').attr('class', 'axis-title').attr('x', m.left + 8).attr('y', m.top - 6).text(`Belegung in ${unit}`);
    // Zeitachse: so viele Ticks, wie Beschriftungen (≈ 80 px) Platz haben; Randticks vermeiden
    const nTicks = Math.max(2, Math.floor((W - m.left - m.right) / 170));
    let tv = x.ticks(nTicks);
    if (tv.length > nTicks + 1) { const k = Math.ceil(tv.length / (nTicks + 1)); tv = tv.filter((d, i) => i % k === 0); }
    tv = tv.filter((d) => x(d) >= m.left + 42 && x(d) <= W - m.right - 42);
    svg.append('g').attr('class', 'axis').attr('transform', `translate(0,${H - m.bottom})`).call(d3.axisBottom(x).tickValues(tv).tickFormat(d3.timeFormat('%d.%m.%Y')).tickSize(4));
    svg.append('text').attr('class', 'axis-title').attr('x', W - m.right).attr('y', H - 4).attr('text-anchor', 'end').text('Zeit (Messpunkte alle 6 h)');
    const line = d3.line().x((p) => x(p.t)).y((p) => y(p.v)).curve(d3.curveMonotoneX);
    const area = d3.area().x((p) => x(p.t)).y0(y(0)).y1((p) => y(p.v)).curve(d3.curveMonotoneX);
    visible.forEach((s, i) => {
      const g = svg.append('g');
      const cap = typeof s.cap === 'number' ? s.cap : (s.cap?.length ? s.cap[s.cap.length - 1].v : null);
      if (cap) {
        g.append('line').attr('class', 'line line--cap').attr('x1', m.left).attr('x2', W - m.right).attr('y1', y(cap)).attr('y2', y(cap));
        const ct = g.append('text').attr('class', 'chart-value chart-value--muted').attr('x', W - m.right + 8).attr('y', y(cap) + 4).text(`Kapazität ${fmt.bytes(cap)}`);
        fitOne(ct.node(), m.right - 10);
        g.append('line').attr('class', 'line line--threshold').attr('x1', m.left).attr('x2', W - m.right).attr('y1', y(cap * 0.8)).attr('y2', y(cap * 0.8));
        g.append('text').attr('class', 'chart-note').attr('x', W - m.right + 8).attr('y', y(cap * 0.8) + 4).text('80-%-Marke');
      }
      if (s.points.length) {
        if (i === 0) g.append('path').attr('class', 'area').attr('d', area(s.points));
        g.append('path').attr('class', `line${i > 0 ? ' line--soft' : ''}`).attr('d', line(s.points));
        const last = s.points[s.points.length - 1];
        g.append('circle').attr('class', 'dot').attr('cx', x(last.t)).attr('cy', y(last.v)).attr('r', 4);
        const lt = g.append('text').attr('class', 'chart-label').attr('x', x(last.t) + 8).attr('y', y(last.v) - 8).text(`${s.label} ${fmt.bytes(last.v)}`);
        fitOne(lt.node(), W - x(last.t) - 10);
      }
      const f = forecasts[i];
      if (f && cap && f.slopePerDay > 0) {
        const endT = d3.min([f.t100 ?? tMax, tMax, horizon]);
        const pts = [{ t: f.t0, v: f.v0 }, { t: endT, v: f.v0 + f.slopePerDay * ((endT - f.t0) / 864e5) }];
        g.append('path').attr('class', 'line line--forecast').attr('d', d3.line().x((p) => x(p.t)).y((p) => y(Math.min(p.v, yMax)))(pts));
        // Prognose-Text steht in der Statuszeile unter dem Diagramm (keine Kollision mit dem Endpunkt-Label)
      }
    });
    const cross = svg.append('line').attr('class', 'crosshair').attr('y1', m.top).attr('y2', H - m.bottom).style('display', 'none');
    const bisect = d3.bisector((p) => p.t).center;
    svg.append('rect').attr('class', 'hit').attr('x', m.left).attr('y', m.top).attr('width', W - m.left - m.right).attr('height', H - m.top - m.bottom)
      .on('pointermove', (e) => {
        const [px] = d3.pointer(e); const t = x.invert(px);
        cross.style('display', null).attr('x1', px).attr('x2', px);
        const rows = visible.filter((s) => s.points.length).map((s) => { const p = s.points[bisect(s.points, t)]; return [s.label, p ? fmt.bytes(p.v) : '–']; });
        tip.show(e, { title: fmt.dateTime(t), rows });
      })
      .on('pointerleave', () => { cross.style('display', 'none'); tip.hide(); });
    onDraw?.({ forecasts, unit });
  }, { minWidth: 440, scroll: true });
  return { forecasts, unit };
}

export function forecast(s, now = new Date()) {
  const pts = s.points;
  if (pts.length < 5) return null;
  const spanDays = (pts[pts.length - 1].t - pts[0].t) / 864e5;
  if (spanDays < 14) return null;
  const t0 = pts[0].t.getTime();
  const xs = pts.map((p) => (p.t.getTime() - t0) / 864e5), ys = pts.map((p) => p.v);
  const mx = d3.mean(xs), my = d3.mean(ys);
  const slope = d3.sum(xs.map((xv, i) => (xv - mx) * (ys[i] - my))) / (d3.sum(xs.map((xv) => (xv - mx) ** 2)) || 1);
  const last = pts[pts.length - 1];
  const cap = typeof s.cap === 'number' ? s.cap : (s.cap?.length ? s.cap[s.cap.length - 1].v : null);
  const at = (target) => (slope > 0 && cap ? new Date(last.t.getTime() + ((target - last.v) / slope) * 864e5) : null);
  void now;
  return { slopePerDay: slope, t0: last.t, v0: last.v, t80: cap ? at(cap * 0.8) : null, t100: cap ? at(cap) : null, spanDays };
}

/** Kleinvielfache: ein kleines Linien-Diagramm je Serie (gleiche Höhe, eigene Skala). */
export function smallMultiple(container, s, o = {}) {
  const { range = null, now = new Date(), format = (v) => String(v), yDomain } = o;
  const tMin = range ? d3.timeDay.offset(now, -range) : null;
  const pts = tMin ? s.points.filter((p) => p.t >= tMin) : s.points;
  responsive(container, (W) => {
    const H = 72, m = { top: 8, right: 6, bottom: 16, left: 6 };
    const svg = svgPx(container, W, H, `${s.label}: ${pts.length} Punkte`);
    if (pts.length < 2) {
      svg.append('line').attr('class', 'spark__empty').attr('x1', m.left).attr('x2', W - m.right).attr('y1', H / 2).attr('y2', H / 2);
      svg.append('text').attr('class', 'chart-note').attr('x', W / 2).attr('y', H / 2 - 8).attr('text-anchor', 'middle').text(pts.length === 1 ? 'erst ein Messpunkt' : 'keine Daten');
      if (pts.length === 1) svg.append('circle').attr('class', 'dot').attr('cx', W - m.right - 4).attr('cy', H / 2).attr('r', 3.5);
      return;
    }
    const x = d3.scaleTime().domain(d3.extent(pts, (p) => p.t)).range([m.left, W - m.right]);
    const ext = yDomain ?? d3.extent(pts, (p) => p.v);
    const y = d3.scaleLinear().domain(ext[0] === ext[1] ? [ext[0] - 1, ext[1] + 1] : ext).range([H - m.bottom, m.top]).nice();
    svg.append('path').attr('class', 'area').attr('d', d3.area().x((p) => x(p.t)).y0(H - m.bottom).y1((p) => y(p.v)).curve(d3.curveMonotoneX)(pts));
    svg.append('path').attr('class', 'line').attr('d', d3.line().x((p) => x(p.t)).y((p) => y(p.v)).curve(d3.curveMonotoneX)(pts));
    svg.append('text').attr('class', 'chart-note').attr('x', m.left).attr('y', H - 4).text(fmt.dateShort(pts[0].t));
    svg.append('text').attr('class', 'chart-note').attr('x', W - m.right).attr('y', H - 4).attr('text-anchor', 'end').text(fmt.dateShort(pts[pts.length - 1].t));
    svg.append('text').attr('class', 'chart-note').attr('x', m.left).attr('y', m.top + 6).text(`max ${format(y.domain()[1])}`);
    const cross = svg.append('line').attr('class', 'crosshair').attr('y1', m.top).attr('y2', H - m.bottom).style('display', 'none');
    const bisect = d3.bisector((p) => p.t).center;
    svg.append('rect').attr('class', 'hit').attr('x', m.left).attr('y', 0).attr('width', W - m.left - m.right).attr('height', H)
      .on('pointermove', (e) => { const [px] = d3.pointer(e); const p = pts[bisect(pts, x.invert(px))]; cross.style('display', null).attr('x1', x(p.t)).attr('x2', x(p.t)); tip.show(e, { title: fmt.dateTime(p.t), rows: [[s.label, format(p.v)]] }); })
      .on('pointerleave', () => { cross.style('display', 'none'); tip.hide(); });
  }, { minWidth: 160 });
}
