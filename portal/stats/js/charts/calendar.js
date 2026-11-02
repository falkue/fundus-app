/**
 * Kalender-Heatmap (GitHub-Stil, pixelgenau): letzte N Wochen, ein Kästchen je Tag,
 * Farbe = gesicherte Menge in fünf Stufen (Quantile). Tage ohne Lauf = Grundfarbe.
 * calendarHeatmap(container, daily:[{date, bytes}], {weeks, now, onDraw})
 * Gibt {total, runDays, days} über onDraw zurück (wird bei jeder Breite neu berechnet).
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;
const DOW = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];

export function calendarHeatmap(container, daily, opts = {}) {
  const now = opts.now ? new Date(opts.now) : new Date();
  const weeks = opts.weeks ?? 26;
  const byDay = new Map();
  for (const d of daily ?? []) {
    if (!d?.date) continue;
    const k = String(d.date).slice(0, 10);
    byDay.set(k, (byDay.get(k) ?? 0) + (typeof d.bytes === 'number' ? d.bytes : 0));
  }
  const end = d3.timeDay.floor(now);
  const start = d3.timeMonday.offset(d3.timeMonday.floor(end), -(weeks - 1));
  const days = d3.timeDays(start, d3.timeDay.offset(end, 1));
  const vals = [...byDay.values()].filter((v) => v > 0);
  const q = d3.scaleQuantile().domain(vals.length ? vals : [0, 1]).range([1, 2, 3, 4]);
  const key = d3.timeFormat('%Y-%m-%d');
  const total = d3.sum(days, (d) => byDay.get(key(d)) ?? 0);
  const runDays = days.filter((d) => byDay.has(key(d))).length;
  const result = { total, runDays, days: days.length };
  responsive(container, (W) => {
    const left = 30, top = 20, gap = 3;
    const cell = Math.max(9, Math.min(16, Math.floor((W - left - 4) / weeks) - gap));
    const H = top + 7 * (cell + gap) + 4;
    const svg = svgPx(container, left + weeks * (cell + gap) + 4, H, `Kalender der letzten ${weeks} Wochen`);
    const col = (d) => Math.floor((d3.timeMonday.floor(d) - start) / (7 * 864e5));
    const row = (d) => (d.getDay() + 6) % 7;
    svg.selectAll('text.dow').data([0, 2, 4, 6]).join('text').attr('class', 'cal__label').attr('x', 0).attr('y', (r) => top + r * (cell + gap) + cell - 2).text((r) => DOW[r]);
    const months = d3.timeMonths(d3.timeMonth.ceil(start), end);
    svg.selectAll('text.m').data(months).join('text').attr('class', 'cal__label').attr('x', (m) => left + col(m) * (cell + gap)).attr('y', 12).text((m) => fmt.month(m));
    const rects = svg.selectAll('rect').data(days).join('rect')
      .attr('class', (d) => { const v = byDay.get(key(d)); return `cal__day cal__day--${v === undefined || v <= 0 ? 0 : q(v)}`; })
      .attr('x', (d) => left + col(d) * (cell + gap)).attr('y', (d) => top + row(d) * (cell + gap)).attr('width', cell).attr('height', cell).attr('rx', 2)
      .attr('tabindex', (d) => (byDay.has(key(d)) ? 0 : null));
    tip.bind(rects, (d) => { const v = byDay.get(key(d)); return { title: fmt.date(d), rows: [['Gesichert', v === undefined ? 'kein Lauf' : fmt.bytes(v)]] }; });
  }, { minWidth: 420, scroll: true });
  return result;
}
