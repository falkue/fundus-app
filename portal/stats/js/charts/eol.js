/**
 * EOL-Zeitleiste (Gantt-artig, pixelgenau): je Komponente ein Balken von heute bis
 * zum Supportende. Heute-Linie mit Datum, Warnzone < 180 Tage, abgelaufen = gestrichelte
 * Linie vom Supportende bis heute, unbekanntes Ende = dezenter Marker.
 * eolTimeline(container, rows:[{node,name,version,eol,eolDays,state,vendorManaged}])
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { STATES, norm } from '../lib/state.js';
import { fitOne, measure } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;

export function eolTimeline(container, rows, opts = {}) {
  const now = opts.now ? new Date(opts.now) : new Date();
  const sorted = [...rows].sort((a, b) => (a.eolDays ?? 1e9) - (b.eolDays ?? 1e9));
  return responsive(container, (W) => {
    const rowH = 38, barH = 14, top = 30, bottom = 30;
    const H = top + sorted.length * rowH + bottom;
    const svg = svgPx(container, W, H, 'Zeitleiste der Supportenden');
    const g = svg.append('g').selectAll('g.row').data(sorted).join('g').attr('transform', (d, i) => `translate(0, ${top + i * rowH})`);
    const names = g.append('text').attr('class', 'chart-label').attr('x', 0).attr('y', barH / 2 + 4).text((d) => `${d.node} · ${d.name}`);
    const subs = g.append('text').attr('class', 'chart-label-sub').attr('x', 0).attr('y', barH / 2 + 17).text((d) => [d.version, d.vendorManaged ? 'vom Hersteller gepflegt' : null].filter(Boolean).join(' · '));
    const maxLabel = Math.min(Math.floor(W * 0.3), Math.max(120, d3.max([...names.nodes(), ...subs.nodes()], (n) => measure(n)) ?? 120));
    names.each(function () { fitOne(this, maxLabel); });
    subs.each(function () { fitOne(this, maxLabel); });
    const left = maxLabel + 16, right = 190;
    const minD = d3.timeYear.offset(now, -4);
    const maxKnown = d3.max(sorted.filter((r) => r.eol), (r) => new Date(r.eol)) ?? d3.timeYear.offset(now, 3);
    const maxD = d3.timeMonth.offset(d3.max([maxKnown, d3.timeYear.offset(now, 1)]), 3);
    const x = d3.scaleTime().domain([minD, maxD]).range([left, W - right]);
    svg.append('rect').attr('class', 'eol__warnzone').attr('x', x(now)).attr('y', top - 8).attr('width', x(d3.timeDay.offset(now, 180)) - x(now)).attr('height', sorted.length * rowH + 8);
    const axis = d3.axisTop(x).ticks(Math.max(2, Math.floor((W - left - right) / 70))).tickFormat(d3.timeFormat('%Y')).tickSize(-(sorted.length * rowH + 8));
    svg.insert('g', ':first-child').attr('class', 'axis grid').attr('transform', `translate(0, ${top - 8})`).call(axis).call((a) => a.selectAll('text').attr('dy', '-0.4em'));
    svg.append('text').attr('class', 'axis-title').attr('x', 0).attr('y', 11).text('Supportende (Jahr)');
    g.each(function (d) {
      const row = d3.select(this);
      const s = norm(d.state);
      let valueText;
      if (!d.eol) {
        row.append('rect').attr('class', 'eol__unknown').attr('x', x(now) + 6).attr('y', 1).attr('width', 28).attr('height', barH - 2).attr('rx', 4);
        valueText = 'Ende unbekannt';
      } else {
        const end = new Date(d.eol);
        if (end >= now) {
          row.append('rect').attr('class', `eol__bar eol__bar--${s}${d.vendorManaged ? ' eol__bar--vendor' : ''}`).attr('x', x(now)).attr('y', 0).attr('width', Math.max(3, x(end) - x(now))).attr('height', barH).attr('rx', 4);
          valueText = `noch ${fmt.int(d.eolDays ?? Math.round((end - now) / 864e5))} Tage · ${fmt.date(end)}`;
        } else {
          row.append('line').attr('class', 'eol__expired').attr('x1', x(d3.max([end, minD]))).attr('x2', x(now)).attr('y1', barH / 2).attr('y2', barH / 2);
          row.append('circle').attr('class', `eol__bar eol__bar--${s}${d.vendorManaged ? ' eol__bar--vendor' : ''}`).attr('cx', x(d3.max([end, minD]))).attr('cy', barH / 2).attr('r', 5);
          valueText = `seit ${fmt.int(-(d.eolDays ?? Math.round((now - end) / 864e5)))} Tagen abgelaufen (${fmt.date(end)})`;
        }
      }
      const vt = row.append('text').attr('class', `chart-value${s === 'unknown' || d.vendorManaged ? ' chart-value--muted' : ''}`).attr('x', W - right + 10).attr('y', barH / 2 + 4).text(valueText);
      fitOne(vt.node(), right - 12);
      tip.bind(row, () => ({ title: `${d.node} · ${d.name} ${d.version ?? ''}`, rows: [['Supportende', d.eol ? fmt.date(d.eol) : 'unbekannt'], ['Abstand', d.eolDays === null || d.eolDays === undefined ? '–' : fmt.days(d.eolDays)], ['Zustand', STATES[s].word], ['Pflege', d.vendorManaged ? 'vom Hersteller (z. B. QNAP) gepflegt' : null], ['Hinweis', d.note ?? null]] }));
    });
    svg.append('line').attr('class', 'eol__today').attr('x1', x(now)).attr('x2', x(now)).attr('y1', top - 8).attr('y2', top + sorted.length * rowH + 4);
    svg.append('text').attr('class', 'eol__today-label').attr('x', x(now) + 5).attr('y', H - 8).text(`Heute · ${fmt.date(now)}`);
  }, { minWidth: 600, scroll: true });
}
