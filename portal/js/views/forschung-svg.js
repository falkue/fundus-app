/**
 * The one graphic of the "Forschung" view: a small interval plot for a result with numbers – estimate, 95 % interval
 * and the zero line, one colour, no frame. Shown only when the result carries estimate and interval as numbers.
 */
import { svgEl } from '../lib/dom.js';
import { num, responsive } from './forschung-lib.js';

const T = (x, y, text, attrs = {}) => svgEl('text', { x, y, ...attrs }, text);

/** rows: [{label, estimate, ci_low, ci_high}] – first row is the main estimate. */
export function intervalPlot(rows) {
  const usable = rows.filter((r) => Number.isFinite(r.estimate));
  return responsive('fo-plot', (W) => {
    const w = Math.min(W, 560);
    const labelW = usable.length > 1 && w > 420 ? 170 : 0;
    const rowH = labelW ? 26 : 30;
    const top = 8, H = top + usable.length * rowH + 26;
    const x0 = labelW + 8, x1 = w - 12;
    const vals = usable.flatMap((r) => [r.estimate, r.ci_low, r.ci_high]).filter(Number.isFinite).concat([0]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.1;
    lo -= pad; hi += pad;
    const xs = (v) => x0 + ((v - lo) / (hi - lo)) * (x1 - x0);
    const yAxis = top + usable.length * rowH;
    const s = svgEl('svg', { class: 'fo-plot__svg', viewBox: `0 0 ${w} ${H}`, width: w, height: H, role: 'img',
      'aria-label': usable.map((r) => `${r.label}: ${num(r.estimate)}, Intervall ${num(r.ci_low)} bis ${num(r.ci_high)}`).join('; ') });
    s.append(svgEl('line', { x1: xs(0), x2: xs(0), y1: top - 4, y2: yAxis, class: 'fo-plot__zero' }),
      svgEl('line', { x1: x0, x2: x1, y1: yAxis, y2: yAxis, class: 'fo-plot__axis' }),
      T(xs(0), yAxis + 16, '0', { class: 'fo-plot__t', 'text-anchor': 'middle' }));
    for (const v of [lo + pad, hi - pad]) if (Math.abs(xs(v) - xs(0)) > 28) s.append(T(xs(v), yAxis + 16, num(v), { class: 'fo-plot__t', 'text-anchor': 'middle' }));
    usable.forEach((r, i) => {
      const y = top + i * rowH + rowH / 2;
      if (labelW) s.append(T(labelW, y + 4, r.label.length > 26 ? `${r.label.slice(0, 25)}…` : r.label, { class: 'fo-plot__l', 'text-anchor': 'end' }));
      if (Number.isFinite(r.ci_low) && Number.isFinite(r.ci_high)) s.append(svgEl('line', { x1: xs(r.ci_low), x2: xs(r.ci_high), y1: y, y2: y, class: 'fo-plot__ci' }));
      s.append(svgEl('circle', { cx: xs(r.estimate), cy: y, r: i === 0 ? 5 : 4, class: 'fo-plot__pt' }));
    });
    return s;
  }, 560);
}
