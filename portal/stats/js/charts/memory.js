/**
 * KI-Knoten (pixelgenau): Speicherbalken des Unified Memory (geladene Modelle als Segmente,
 * Markierung „max. N Modelle“, ein Modell über der Grenze koralle) und Treemap der
 * installierten Modelle nach Grösse.
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { fitOne, measure } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;

export function memoryBar(container, ai) {
  const total = typeof ai?.memTotalBytes === 'number' ? ai.memTotalBytes : null;
  const loaded = Array.isArray(ai?.loaded) ? ai.loaded : [];
  const limit = typeof ai?.limitLoaded === 'number' ? ai.limitLoaded : null;
  return responsive(container, (W) => {
    const H = 96, barY = 34, barH = 32;
    const svg = svgPx(container, W, H, total ? `Unified Memory ${fmt.bytes(total)}, ${loaded.length} Modelle geladen` : 'Speicher unbekannt');
    if (!total) {
      svg.append('rect').attr('x', 0).attr('y', barY).attr('width', W).attr('height', barH).attr('rx', 4).attr('fill', 'url(#hatch)');
      svg.append('text').attr('class', 'chart-note').attr('x', W / 2).attr('y', barY + barH / 2 + 4).attr('text-anchor', 'middle').text('Gesamtspeicher unbekannt');
      return;
    }
    const x = d3.scaleLinear().domain([0, total]).range([0, W]);
    const gap = 2;
    const sorted = [...loaded].sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0));
    let acc = 0;
    const segs = sorted.map((m, i) => { const b = typeof m.bytes === 'number' ? m.bytes : 0; const s = { x0: acc, x1: acc + b, m, i }; acc += b; return s; });
    const avail = typeof ai.memAvailBytes === 'number' ? ai.memAvailBytes : null;
    const other = avail !== null ? Math.max(0, total - avail - acc) : null;
    svg.append('rect').attr('class', 'memseg--free').attr('x', 0).attr('y', barY).attr('width', W).attr('height', barH).attr('rx', 4);
    const seg = svg.selectAll('rect.model').data(segs).join('rect')
      .attr('class', (s) => `memseg ${limit !== null && s.i >= limit ? 'memseg--model-3' : s.i % 2 ? 'memseg--model-2' : 'memseg--model'}`)
      .attr('x', (s) => x(s.x0)).attr('y', barY).attr('width', (s) => Math.max(0, x(s.x1) - x(s.x0) - gap)).attr('height', barH).attr('rx', 4).attr('tabindex', 0)
      .attr('aria-label', (s) => `${s.m.name}: ${fmt.bytes(s.m.bytes)}`);
    tip.bind(seg, (s) => ({ title: s.m.name, rows: [['Speicher', fmt.bytes(s.m.bytes)], ['Kontext', s.m.ctx ? fmt.int(s.m.ctx) : null], ['Läuft ab', s.m.expires ? fmt.ago(s.m.expires) : null], ['Position', limit !== null && s.i >= limit ? `über der Grenze (${limit})` : `${s.i + 1} von ${limit ?? '?'}`]] }));
    svg.selectAll('text.ml').data(segs).join('text').attr('class', 'memseg-label').attr('x', (s) => x(s.x0) + 8).attr('y', barY + barH / 2 + 4)
      .text((s) => `${s.m.name} · ${fmt.bytes(s.m.bytes, 1)}`).each(function (s) { const w = x(s.x1) - x(s.x0) - 16; if (w < 30) this.textContent = ''; else if (fitOne(this, w) && measure(this) < 24) this.textContent = ''; });
    if (other !== null && other > 0) {
      const o = svg.append('rect').attr('class', 'memseg--other').attr('x', x(acc)).attr('y', barY).attr('width', Math.max(0, x(acc + other) - x(acc) - gap)).attr('height', barH).attr('rx', 4);
      tip.bind(o, () => ({ title: 'Übrige Belegung', rows: [['System, Dienste, Cache', fmt.bytes(other)]] }));
      const ot = svg.append('text').attr('class', 'memseg-label').attr('x', x(acc) + 8).attr('y', barY + barH / 2 + 4).text(`übrige Belegung ${fmt.bytes(other, 0)}`);
      fitOne(ot.node(), x(other) - 16);
    }
    const freeStart = other !== null ? acc + other : acc;
    const ft = svg.append('text').attr('class', 'memseg-label memseg-label--out').attr('x', W - 8).attr('y', barY + barH / 2 + 4).attr('text-anchor', 'end').text(`frei ${fmt.bytes(total - freeStart, 0)}`);
    fitOne(ft.node(), x(total - freeStart) - 12);
    if (limit !== null && segs.length) {
      const lim = segs[Math.min(limit, segs.length) - 1];
      const lx = x(lim.x1);
      svg.append('line').attr('class', 'mem-limit').attr('x1', lx).attr('x2', lx).attr('y1', barY - 12).attr('y2', barY + barH + 6);
      svg.append('text').attr('class', 'mem-limit-label').attr('x', lx + 6).attr('y', barY - 4).text(`Grenze: max. ${limit} Modelle geladen`);
    }
    for (const t of (W < 560 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1])) svg.append('text').attr('class', 'chart-note').attr('x', x(total * t)).attr('y', H - 6).attr('text-anchor', t === 0 ? 'start' : t === 1 ? 'end' : 'middle').text(t === 1 ? `${fmt.bytes(total, 0)} gesamt` : fmt.bytes(total * t, 0));
  }, { minWidth: 320 });
}

export function modelTreemap(container, installed, { loaded, used }) {
  const data = { children: (installed ?? []).map((m) => ({ ...m, value: typeof m.bytes === 'number' ? m.bytes : 0 })).filter((m) => m.value > 0) };
  if (!data.children.length) return null;
  return responsive(container, (W) => {
    const H = Math.max(260, Math.min(420, W * 0.45));
    const root = d3.treemap().size([W, H]).paddingInner(3).round(true)(d3.hierarchy(data).sum((d) => d.value).sort((a, b) => b.value - a.value));
    const svg = svgPx(container, W, H, `${data.children.length} installierte Modelle nach Grösse`);
    const cls = (d) => (loaded.has(d.data.name) ? 'tm__cell--loaded' : used.has(d.data.name) ? 'tm__cell--used' : 'tm__cell--unused');
    const g = svg.selectAll('g').data(root.leaves()).join('g').attr('transform', (d) => `translate(${d.x0},${d.y0})`);
    g.append('rect').attr('class', (d) => `tm__cell ${cls(d)}`).attr('width', (d) => d.x1 - d.x0).attr('height', (d) => d.y1 - d.y0).attr('rx', 4).attr('tabindex', 0)
      .attr('aria-label', (d) => `${d.data.name}: ${fmt.bytes(d.data.bytes)}`);
    g.filter((d) => d.x1 - d.x0 > 60 && d.y1 - d.y0 > 30).append('text').attr('class', (d) => `tm__label${loaded.has(d.data.name) ? ' tm__label--onblue' : ''}`).attr('x', 8).attr('y', 18)
      .text((d) => d.data.name).each(function (d) { fitOne(this, d.x1 - d.x0 - 14); });
    g.filter((d) => d.x1 - d.x0 > 60 && d.y1 - d.y0 > 46).append('text').attr('class', (d) => `tm__size${loaded.has(d.data.name) ? ' tm__size--onblue' : ''}`).attr('x', 8).attr('y', 33)
      .text((d) => `${fmt.bytes(d.data.bytes, 1)} · ${loaded.has(d.data.name) ? 'geladen' : used.has(d.data.name) ? 'genutzt' : 'ungenutzt'}`).each(function (d) { fitOne(this, d.x1 - d.x0 - 14); });
    tip.bind(g, (d) => ({ title: d.data.name, rows: [['Grösse', fmt.bytes(d.data.bytes)], ['Familie', d.data.family ?? null], ['Geändert', d.data.modified ? fmt.ago(d.data.modified) : null], ['Status', loaded.has(d.data.name) ? 'geladen' : used.has(d.data.name) ? 'von Diensten genutzt' : 'ungenutzt (Aufräumkandidat)']] }));
  }, { minWidth: 300 });
}
