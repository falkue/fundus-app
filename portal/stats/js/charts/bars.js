/**
 * Sortierte horizontale Balken, optional gestapelt (zwei Segmente, zwei Farben).
 * Pixelgenau; Labelspalte wird gemessen (bis 35 % der Breite), Rest mit „…“.
 *
 * stackedBars(container, rows, {segments:[{key,label,cls}], format, axisTitle})
 * rows: [{label, sub, values:{key: number|null}, text?, note}]
 *   - values alle null und text gesetzt → Textzeile statt Balken (z. B. „QTS 5.2.10 – nicht prüfbar“)
 *   - values alle null ohne text → Schraffur „keine Daten“
 */
import * as tip from '../lib/tooltip.js';
import { fitOne, measure } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;

export function stackedBars(container, rows, opts = {}) {
  const { segments, format = (v) => String(v), axisTitle = null, barH = 18, rowH = 40 } = opts;
  const sorted = [...rows].sort((a, b) => total(b, segments) - total(a, segments));
  return responsive(container, (W) => {
    const top = 6, bottom = axisTitle ? 34 : 14;
    const H = top + sorted.length * rowH + bottom;
    const svg = svgPx(container, W, H, `${sorted.length} Balken`);
    const g = svg.selectAll('g.row').data(sorted).join('g').attr('transform', (d, i) => `translate(0, ${top + i * rowH})`);
    const names = g.append('text').attr('class', 'chart-label').attr('x', 0).attr('y', barH / 2 + 4).text((d) => d.label);
    const subs = g.append('text').attr('class', 'chart-label-sub').attr('x', 0).attr('y', barH / 2 + 18).text((d) => d.sub ?? '');
    const maxLabel = Math.min(Math.floor(W * 0.35), Math.max(90, d3.max([...names.nodes(), ...subs.nodes()], (n) => measure(n)) ?? 90));
    names.each(function () { fitOne(this, maxLabel); });
    subs.each(function () { fitOne(this, maxLabel); });
    const left = maxLabel + 16, right = 110;
    const max = d3.max(sorted, (r) => total(r, segments)) || 1;
    const x = d3.scaleLinear().domain([0, max]).range([0, Math.max(40, W - left - right)]).nice();
    // Gitter + Achse unten
    const axis = svg.append('g').attr('class', 'axis grid').attr('transform', `translate(${left}, ${top + sorted.length * rowH})`)
      .call(d3.axisBottom(x).ticks(Math.max(2, Math.floor(x.range()[1] / 90))).tickFormat((v) => format(v)).tickSize(-(sorted.length * rowH)));
    axis.selectAll('text').attr('dy', '1.1em');
    if (axisTitle) svg.append('text').attr('class', 'axis-title').attr('x', left + x.range()[1]).attr('y', H - 4).attr('text-anchor', 'end').text(axisTitle);
    g.each(function (d) {
      const row = d3.select(this);
      const known = segments.some((s) => typeof d.values?.[s.key] === 'number');
      if (!known) {
        if (d.text) {
          const t = row.append('text').attr('class', 'chart-value chart-value--muted').attr('x', left).attr('y', barH / 2 + 4).text(d.text);
          fitOne(t.node(), W - left - 4);
        } else {
          row.append('rect').attr('x', left).attr('y', 0).attr('width', x.range()[1]).attr('height', barH).attr('rx', 4).attr('fill', 'url(#hatch)');
          row.append('text').attr('class', 'chart-value chart-value--muted').attr('x', left + x.range()[1] + 8).attr('y', barH / 2 + 4).text('keine Daten');
        }
        tip.bind(row, () => ({ title: d.label, rows: [['Daten', d.text ?? 'nicht erhoben'], ['Hinweis', d.note ?? null]] }));
        return;
      }
      let acc = 0;
      const gap = 2;
      segments.forEach((s, si) => {
        const v = d.values?.[s.key] ?? 0;
        if (v <= 0) return;
        const x0 = left + x(acc), w = Math.max(0, x(v) - (si < segments.length - 1 ? gap : 0));
        const isLast = acc + v >= total(d, segments) - 1e-9;
        row.append('path').attr('class', s.cls).attr('d', roundedRight(x0, 0, w, barH, isLast ? 4 : 0));
        acc += v;
      });
      row.append('text').attr('class', 'chart-value').attr('x', left + x(total(d, segments)) + 8).attr('y', barH / 2 + 4)
        .text(`${format(total(d, segments))}${d.unit ? ` ${d.unit}` : ''}`);
      tip.bind(row, () => ({ title: d.label, rows: [...segments.map((s) => [s.label, typeof d.values?.[s.key] === 'number' ? format(d.values[s.key]) : '–']), ['Gesamt', format(total(d, segments))], ['Hinweis', d.note ?? null]] }));
    });
  }, { minWidth: 440, scroll: true });
}
function total(r, segs) { return segs.reduce((a, s) => a + (typeof r.values?.[s.key] === 'number' ? r.values[s.key] : 0), 0); }
export function roundedRight(x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  if (r <= 0) return `M${x} ${y}h${w}v${h}h${-w}z`;
  return `M${x} ${y}h${w - r}a${r} ${r} 0 0 1 ${r} ${r}v${h - 2 * r}a${r} ${r} 0 0 1 ${-r} ${r}h${-(w - r)}z`;
}
