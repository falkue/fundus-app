/**
 * Sankey Quelle → Sicherungsjob → Ziel (d3-sankey), pixelgenau. Breite = Datenmenge
 * (sourceBytes, sonst Repo-Grösse – geteilt, wenn mehrere Jobs dasselbe Repository nutzen).
 * Stil wie Falks Sankey-Referenz: Knoten 12 breit, Abstand 14, Ströme .28/.55.
 */
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { norm, STATES } from '../lib/state.js';
import { fitOne } from '../lib/svgtext.js';
import { responsive, svgPx } from '../lib/chart.js';

const d3 = globalThis.d3;

export function backupSankey(container, jobs, nodesById, opts = {}) {
  const repoShare = new Map();
  for (const j of jobs) if (j.repo) repoShare.set(j.repo, (repoShare.get(j.repo) ?? 0) + 1);
  const shareOf = (j) => (j.repo ? repoShare.get(j.repo) ?? 1 : 1);
  const sizeOf = (j) => (typeof j.sourceBytes === 'number' && j.sourceBytes > 0 ? j.sourceBytes / 1e9 : typeof j.repoBytes === 'number' && j.repoBytes > 0 ? j.repoBytes / 1e9 / shareOf(j) : null);
  const sizeText = (j) => (typeof j.sourceBytes === 'number' && j.sourceBytes > 0 ? fmt.bytes(j.sourceBytes) : typeof j.repoBytes === 'number' && j.repoBytes > 0 ? `${fmt.bytes(j.repoBytes)}${shareOf(j) > 1 ? ` (Repo ${j.repo}, geteilt mit ${shareOf(j) - 1} weiteren Jobs)` : ''}` : 'Grösse unbekannt');
  const maxSize = d3.max(jobs, sizeOf) ?? 100;
  const nodeIds = new Set(); const nodes = []; const links = [];
  const addNode = (id, label, kind, extra) => { if (!nodeIds.has(id)) { nodeIds.add(id); nodes.push({ id, label, kind, ...extra }); } };
  for (const j of jobs) {
    const src = j.source ?? 'unbekannt', tgt = j.target ?? 'unbekannt';
    addNode(`s:${src}`, nodesById[src]?.label ?? src, 'source');
    addNode(`j:${j.id}`, j.label ?? j.id, 'job', { job: j });
    addNode(`t:${tgt}`, nodesById[tgt]?.label ?? tgt, 'target');
    const v = sizeOf(j) ?? Math.max(1, maxSize * 0.03);
    links.push({ source: `s:${src}`, target: `j:${j.id}`, value: v, job: j, unknown: sizeOf(j) === null });
    links.push({ source: `j:${j.id}`, target: `t:${tgt}`, value: v, job: j, unknown: sizeOf(j) === null });
  }
  if (!nodes.length) return null;
  return responsive(container, (W) => {
    const H = Math.max(280, 90 + jobs.length * 40);
    const labelSpace = Math.min(220, Math.max(120, W * 0.22));
    const top = 28;
    const sankey = d3.sankey().nodeId((n) => n.id).nodeWidth(12).nodePadding(24).nodeAlign(d3.sankeyJustify)
      .nodeSort((a, b) => rank(a) - rank(b)).extent([[4, top], [W - labelSpace, H - 8]]);
    const graph = sankey({ nodes: nodes.map((n) => ({ ...n })), links: links.map((l) => ({ ...l })) });
    const svg = svgPx(container, W, H, 'Sicherungsflüsse Quelle → Job → Ziel');
    // Spaltenüberschriften (Lesehilfe)
    const cols = [['Quelle', 4, 'start'], ['Sicherungsjob', (W - labelSpace) / 2, 'middle'], ['Ziel', W - labelSpace, 'start']];
    for (const [t, x, a] of cols) svg.append('text').attr('class', 'sankey__col').attr('x', x).attr('y', 12).attr('text-anchor', a).text(t);
    const link = svg.append('g').selectAll('path').data(graph.links).join('path')
      .attr('class', (l) => `sankey__link sankey__link--${l.unknown ? 'unknown' : norm(l.job.state)}`)
      .attr('d', d3.sankeyLinkHorizontal()).attr('stroke-width', (l) => Math.max(1.5, l.width)).attr('tabindex', 0)
      .attr('aria-label', (l) => `${l.source.label} → ${l.target.label}: ${sizeText(l.job)}`);
    tip.bind(link, (l) => ({ title: `${l.source.label} → ${l.target.label}`, rows: [['Job', l.job.label], ['Menge', sizeText(l.job)], ['Zustand', STATES[norm(l.job.state)].word], ['Zeitplan', l.job.schedule ?? null], ['Letzter Erfolg', l.job.lastOk ? fmt.ago(l.job.lastOk) : null]] }));
    link.on('pointerenter.hint', (e, l) => opts.onHover?.(l.job)).on('pointerleave.hint', () => opts.onHover?.(null));
    svg.append('g').selectAll('rect').data(graph.nodes).join('rect')
      .attr('class', (n) => `sankey__node sankey__node--${n.kind}${n.kind === 'job' ? ` sankey__node--job-${norm(n.job.state)}` : ''}`)
      .attr('x', (n) => n.x0).attr('y', (n) => n.y0).attr('width', (n) => n.x1 - n.x0).attr('height', (n) => Math.max(1, n.y1 - n.y0)).attr('rx', 3);
    const lab = svg.append('g').selectAll('g').data(graph.nodes).join('g').attr('transform', (n) => `translate(${n.x1 + 8}, ${(n.y0 + n.y1) / 2})`);
    lab.append('text').attr('class', (n) => `sankey__label${n.kind !== 'job' ? ' sankey__label--strong' : ''}`).attr('dy', (n) => (n.kind === 'job' ? '0.35em' : '-0.2em')).text((n) => n.label)
      .each(function (n) { fitOne(this, n.kind === 'target' ? labelSpace - 12 : n.kind === 'job' ? Math.min(labelSpace, (W - labelSpace) / 3 - 20) : (W - labelSpace) / 3 - 20); });
    lab.filter((n) => n.kind !== 'job').append('text').attr('class', 'sankey__label sankey__label--sub').attr('dy', '1.1em')
      .text((n) => { const known = n.sourceLinks.concat(n.targetLinks).filter((l) => !l.unknown); return known.length ? `≈ ${fmt.bytes(d3.sum(known, (l) => l.value) * 1e9)}` : 'Grösse unbekannt'; });
  }, { minWidth: 460, scroll: true });
}
function rank(n) { return n.kind === 'job' ? (n.job.intended === 'off' ? 2 : norm(n.job.state) === 'ok' ? 0 : 1) : 0; }
