/**
 * Inline SVG graphics of the "Forschung" view (no chart library): forest-style effect plot, small confidence-interval
 * glyph for lists, evidence map (theory × verdict, findings as their own mark) and the provenance graph
 * (datasets → scripts → result → slides/datasets/files). Colours come from CSS classes (fo-v--*), so light and dark
 * mode follow the tokens.
 */
import { el, svgEl } from '../lib/dom.js';
import { VERDICTS, VERDICT, vmark, num, arr, refHref, responsive } from './forschung-lib.js';
import { tip, hideTip } from './daten-lib.js';

const T = (x, y, text, attrs = {}) => svgEl('text', { x, y, ...attrs }, text);

function niceTicks(lo, hi, n = 5) {
  const span = hi - lo || 1;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) ?? step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

/**
 * Forest-style plot: one row per estimate (main model first, then robustness), zero line, shaded side of the predicted
 * direction. rows: [{label, estimate, ci_low, ci_high, main}], opts: {verdict, direction, unit}.
 */
export function forest(rows, opts = {}) {
  const usable = rows.filter((r) => Number.isFinite(r.estimate));
  return responsive('fo-forest', (W) => {
    const narrow = W < 520;
    const labelW = narrow ? 0 : Math.min(260, Math.round(W * 0.36));
    const rowH = narrow ? 44 : 30;
    const top = 26, bottom = 34;
    const H = top + usable.length * rowH + bottom;
    const x0 = labelW + 12, x1 = W - 16;
    const vals = usable.flatMap((r) => [r.estimate, r.ci_low, r.ci_high]).filter(Number.isFinite).concat([0]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.12;
    lo -= pad; hi += pad;
    const xs = (v) => x0 + ((v - lo) / (hi - lo)) * (x1 - x0);
    const s = svgEl('svg', { class: 'fo-forest__svg', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
      'aria-label': `Effekt mit 95-%-Konfidenzintervall: ${usable.map((r) => `${r.label}: ${num(r.estimate)} [${num(r.ci_low)}, ${num(r.ci_high)}]`).join('; ')}` });
    // predicted direction
    const dir = opts.direction;
    if (dir === '+' || dir === '-' || dir === '−') {
      const pos = dir === '+';
      const a = pos ? xs(0) : x0, b = pos ? x1 : xs(0);
      s.append(svgEl('rect', { x: a, y: top - 8, width: Math.max(0, b - a), height: usable.length * rowH + 8, class: 'fo-forest__pred' }),
        T(pos ? x1 - 4 : x0 + 4, top - 12, `vorhergesagt: ${pos ? 'positiv' : 'negativ'}`, { class: 'fo-forest__predt', 'text-anchor': pos ? 'end' : 'start' }));
    }
    // axis
    const ticks = niceTicks(lo, hi, narrow ? 4 : 6);
    const yAxis = top + usable.length * rowH;
    for (const t of ticks) {
      s.append(svgEl('line', { x1: xs(t), x2: xs(t), y1: top - 8, y2: yAxis, class: t === 0 ? 'fo-forest__zero' : 'fo-forest__grid' }),
        T(xs(t), yAxis + 14, num(t), { class: 'fo-forest__tick', 'text-anchor': 'middle' }));
    }
    s.append(T((x0 + x1) / 2, yAxis + 29, opts.unit ? `Effekt (${opts.unit}), 95-%-Konfidenzintervall` : 'Effekt, 95-%-Konfidenzintervall', { class: 'fo-forest__axis', 'text-anchor': 'middle' }));
    usable.forEach((r, i) => {
      const yc = top + i * rowH + (narrow ? rowH - 14 : rowH / 2);
      const g = svgEl('g', { class: `fo-forest__row fo-v--${i === 0 ? (opts.verdict ?? 'open') : 'open'}${i === 0 ? ' is-main' : ''}` });
      if (narrow) g.append(T(x0, yc - 14, r.label, { class: 'fo-forest__label' }));
      else g.append(T(labelW, yc + 4, r.label.length > 46 ? `${r.label.slice(0, 44)}…` : r.label, { class: 'fo-forest__label', 'text-anchor': 'end' }));
      if (Number.isFinite(r.ci_low) && Number.isFinite(r.ci_high)) {
        g.append(svgEl('line', { x1: xs(r.ci_low), x2: xs(r.ci_high), y1: yc, y2: yc, class: 'fo-forest__ci' }),
          svgEl('line', { x1: xs(r.ci_low), x2: xs(r.ci_low), y1: yc - 5, y2: yc + 5, class: 'fo-forest__ci' }),
          svgEl('line', { x1: xs(r.ci_high), x2: xs(r.ci_high), y1: yc - 5, y2: yc + 5, class: 'fo-forest__ci' }));
      }
      const sz = i === 0 ? 11 : 8;
      g.append(svgEl('rect', { x: xs(r.estimate) - sz / 2, y: yc - sz / 2, width: sz, height: sz, class: 'fo-forest__pt' }));
      g.append(svgEl('title', {}, `${r.label}: ${num(r.estimate)} [${num(r.ci_low)}, ${num(r.ci_high)}]`));
      s.append(g);
    });
    return s;
  });
}

/** Tiny CI glyph for list rows: interval relative to zero; null when no numbers. */
export function ciGlyph(r) {
  if (!Number.isFinite(r?.estimate)) return null;
  const W = 92, H = 14;
  const lo0 = Number.isFinite(r.ci_low) ? r.ci_low : r.estimate, hi0 = Number.isFinite(r.ci_high) ? r.ci_high : r.estimate;
  const m = Math.max(Math.abs(lo0), Math.abs(hi0), Math.abs(r.estimate)) || 1;
  const xs = (v) => W / 2 + (v / m) * (W / 2 - 4);
  const s = svgEl('svg', { class: `fo-ci fo-v--${r.verdict ?? 'open'}`, viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
    'aria-label': `Schätzer ${num(r.estimate)}${Number.isFinite(r.ci_low) ? `, Intervall ${num(r.ci_low)} bis ${num(r.ci_high)}` : ''}` });
  s.append(svgEl('line', { x1: W / 2, x2: W / 2, y1: 1, y2: H - 1, class: 'fo-ci__zero' }));
  if (Number.isFinite(r.ci_low)) s.append(svgEl('line', { x1: xs(lo0), x2: xs(hi0), y1: H / 2, y2: H / 2, class: 'fo-ci__line' }));
  s.append(svgEl('rect', { x: xs(r.estimate) - 3, y: H / 2 - 3, width: 6, height: 6, class: 'fo-ci__pt' }));
  return s;
}

/**
 * Evidence map: rows = theories, columns = four verdicts, open hypotheses and findings. Each cell a mark whose area
 * grows with the count (same scale for every column, so no verdict looks heavier). onCell(theory, column).
 */
export const MAP_COLS = [...VERDICTS.map((v) => ({ key: v, label: VERDICT[v] })), { key: 'open', label: 'ohne Ergebnis' }, { key: 'findings', label: 'Findings (explorativ)' }];
export function evidenceMap(rows, onCell) {
  return responsive('fo-emap', (W) => {
    const narrow = W < 640;
    const labelW = narrow ? Math.min(150, W * 0.38) : Math.min(300, W * 0.32);
    const colW = Math.max(36, (W - labelW - 8) / MAP_COLS.length);
    const headH = narrow ? 92 : 46, rowH = 34;
    const H = headH + rows.length * rowH + 6;
    const max = Math.max(1, ...rows.flatMap((r) => MAP_COLS.map((c) => r[c.key] ?? 0)));
    const rad = (n) => (n ? 4 + Math.sqrt(n / max) * (Math.min(colW, rowH) / 2 - 5) : 0);
    const s = svgEl('svg', { class: 'fo-emap__svg', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
      'aria-label': 'Evidenzkarte: Theorien mal Urteil; Tabelle unter „Tabelle“' });
    MAP_COLS.forEach((c, j) => {
      const cx = labelW + j * colW + colW / 2;
      if (narrow) s.append(T(cx + 3, headH - 8, c.label, { class: 'fo-emap__col', transform: `rotate(-50 ${cx + 3} ${headH - 8})` }));
      else {
        const words = c.label.split(' ');
        const l1 = words.length > 1 ? words.slice(0, Math.ceil(words.length / 2)).join(' ') : c.label;
        const l2 = words.length > 1 ? words.slice(Math.ceil(words.length / 2)).join(' ') : '';
        s.append(T(cx, headH - (l2 ? 22 : 8), l1, { class: 'fo-emap__col', 'text-anchor': 'middle' }));
        if (l2) s.append(T(cx, headH - 8, l2, { class: 'fo-emap__col', 'text-anchor': 'middle' }));
      }
    });
    rows.forEach((r, i) => {
      const y = headH + i * rowH;
      s.append(svgEl('line', { x1: 0, x2: W, y1: y, y2: y, class: 'fo-emap__grid' }));
      const name = r.name ?? r.theory;
      const maxCh = Math.floor(labelW / 6.4);
      s.append(T(4, y + rowH / 2 + 4, name.length > maxCh ? `${name.slice(0, maxCh - 1)}…` : name, { class: 'fo-emap__row' }, ));
      MAP_COLS.forEach((c, j) => {
        const n = r[c.key] ?? 0;
        const cx = labelW + j * colW + colW / 2, cy = y + rowH / 2;
        const g = svgEl('g', { class: `fo-emap__cell fo-v--${c.key === 'findings' ? 'finding' : c.key}${n ? '' : ' is-zero'}`, tabindex: n ? '0' : null, role: n ? 'button' : null,
          'aria-label': `${name}, ${c.label}: ${n}` });
        if (n) {
          const rr = rad(n);
          if (c.key === 'findings' || c.key === 'open') g.append(svgEl('circle', { cx, cy, r: rr, class: c.key === 'findings' ? 'fo-ring' : 'fo-ring fo-ring--solid' }));
          else if (c.key === 'supported') g.append(svgEl('circle', { cx, cy, r: rr }));
          else if (c.key === 'partly_supported') g.append(svgEl('rect', { x: cx - rr * 0.89, y: cy - rr * 0.89, width: rr * 1.78, height: rr * 1.78, rx: 1.5 }));
          else if (c.key === 'not_supported') g.append(svgEl('path', { d: `M${cx} ${cy - rr * 1.12} L${cx + rr * 1.12} ${cy} L${cx} ${cy + rr * 1.12} L${cx - rr * 1.12} ${cy} Z` }));
          else g.append(svgEl('path', { d: hexPath(cx, cy, rr * 1.05) }));
          g.append(T(cx + rr + 3, cy + 4, String(n), { class: 'fo-emap__n' }));
          g.addEventListener('click', () => onCell?.(r, c.key));
          g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onCell?.(r, c.key); } });
          g.addEventListener('pointermove', (e) => tip(e, [name, `${c.label}: ${n}`, 'Klick: Liste zeigen']));
          g.addEventListener('pointerleave', hideTip);
        } else g.append(svgEl('circle', { cx, cy, r: 1.6, class: 'fo-emap__dot' }));
        s.append(g);
      });
    });
    return s;
  });
}
function hexPath(cx, cy, r) {
  const p = [];
  for (let k = 0; k < 6; k++) { const a = Math.PI / 6 + (k * Math.PI) / 3; p.push(`${(cx + r * Math.cos(a)).toFixed(1)} ${(cy + r * Math.sin(a)).toFixed(1)}`); }
  return `M${p.join(' L')} Z`;
}

/**
 * Provenance graph: four columns – datasets (with version used; stale ones marked), scripts, the result, outputs.
 * cols: {inputs:[{label, sub, href, stale}], scripts:[…], result:{label, sub, verdict}, outputs:[…]}
 */
export function provenance(cols) {
  const groups = [
    { key: 'inputs', title: 'Datensätze', items: arr(cols.inputs) },
    { key: 'scripts', title: 'Skripte', items: arr(cols.scripts) },
    { key: 'result', title: 'Ergebnis', items: cols.result ? [cols.result] : [] },
    { key: 'outputs', title: 'Folien, Daten, Dateien', items: arr(cols.outputs) },
  ];
  return responsive('fo-prov', (W) => {
    const narrow = W < 700;
    if (narrow) return provenanceList(groups);
    const colW = (W - 3 * 34) / 4, nodeH = 46, gap = 12, headH = 22;
    const maxN = Math.max(1, ...groups.map((g) => g.items.length));
    const H = headH + maxN * (nodeH + gap) + 4;
    const s = svgEl('svg', { class: 'fo-prov__svg', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': 'Herkunft: Datensätze, Skripte, Ergebnis, Ausgaben' });
    const defs = svgEl('defs', {}, svgEl('marker', { id: 'fo-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto' },
      svgEl('path', { d: 'M0 0 L8 4 L0 8 Z', class: 'fo-prov__head' })));
    s.append(defs);
    const pos = groups.map((g, j) => {
      const x = j * (colW + 34);
      const total = g.items.length * (nodeH + gap) - gap;
      const y0 = headH + Math.max(0, ((maxN * (nodeH + gap) - gap) - total) / 2);
      s.append(T(x + 2, 13, g.title, { class: 'fo-prov__col' }));
      return g.items.map((it, i) => ({ x, y: y0 + i * (nodeH + gap), it }));
    });
    // edges between consecutive columns (all-to-all would be noisy: fan in to the result, fan out from it)
    const edge = (a, b) => s.append(svgEl('path', { class: 'fo-prov__edge', 'marker-end': 'url(#fo-arrow)',
      d: `M${a.x + colW} ${a.y + nodeH / 2} C${a.x + colW + 20} ${a.y + nodeH / 2}, ${b.x - 20} ${b.y + nodeH / 2}, ${b.x - 2} ${b.y + nodeH / 2}` }));
    const [ins, scr, res, outs] = pos;
    const target = scr.length ? scr : res;
    if (target.length) for (const a of ins) edge(a, target[0]);
    if (res.length) { for (const a of scr) edge(a, res[0]); for (const b of outs) edge(res[0], b); }
    pos.forEach((col, j) => col.forEach(({ x, y, it }) => {
      const cls = `fo-prov__node${it.stale ? ' is-stale' : ''}${groups[j].key === 'result' ? ` is-result fo-v--${it.verdict ?? 'open'}` : ''}`;
      const g = svgEl(it.href ? 'a' : 'g', { class: cls, href: it.href ?? null, target: it.external ? '_blank' : null });
      g.append(svgEl('rect', { x, y, width: colW, height: nodeH, rx: 7 }));
      const maxCh = Math.floor((colW - 16) / 6.6);
      const clip = (t) => (String(t).length > maxCh ? `${String(t).slice(0, maxCh - 1)}…` : String(t));
      g.append(T(x + 8, y + 18, clip(it.label), { class: 'fo-prov__t' }));
      if (it.sub) g.append(T(x + 8, y + 35, clip(it.sub), { class: 'fo-prov__s' }));
      g.append(svgEl('title', {}, [it.label, it.sub, it.stale ? 'neuere Version vorhanden' : null].filter(Boolean).join(' · ')));
      s.append(g);
    }));
    return s;
  }, 900);
}
function provenanceList(groups) {
  return el('ol.fo-prov__list', groups.filter((g) => g.items.length).map((g) => el('li',
    el('span.label', g.title),
    el('ul', g.items.map((it) => el('li', { class: it.stale ? 'is-stale' : null },
      it.href ? el('a', { href: it.href }, it.label) : el('span', it.label), it.sub ? el('span.small.muted', ` · ${it.sub}`) : null))))));
}

export function verdictKey() {
  return el('div.fo-vlegend', VERDICTS.map((v) => el('span.fo-vlegend__item', vmark(v), VERDICT[v])),
    el('span.fo-vlegend__item', vmark('open'), 'ohne Ergebnis'), el('span.fo-vlegend__item', vmark('finding'), 'Finding (explorativ)'));
}
export { refHref };
