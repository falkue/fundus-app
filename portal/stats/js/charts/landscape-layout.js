/**
 * Layout-Motor der Landschaftskarte: ELK.js 0.12.0 (Worker, vendor/elkjs – unverändert, EPL-2.0).
 *
 * Jedes Zonen-Band ist ein eigener ELK-Graph (layered, Richtung RIGHT → eine Reihe je Band,
 * orthogonale Kanten, Kanten-Label-Plätze für die Nummern-Marker). Die Bänder werden danach
 * untereinander gestapelt; Kanten zwischen Bändern routet landscape.js selbst (orthogonal,
 * durch den Zwischenraum). Ergebnis wird per Hash in sessionStorage gecacht, damit Karte und
 * Mini-Karte nicht springen.
 *
 *   layoutBand({id, nodes:[{id,width,height}], edges:[{id,source,target,label:{width,height}}]}, opts)
 *     → {width, height, nodes:{id:{x,y,width,height}}, edges:{id:{sections:[...], labels:[{x,y,width,height}]}}}
 */
let elk = null;
function engine() {
  if (!elk) {
    if (typeof globalThis.ELK !== 'function') throw new Error('ELK (vendor/elkjs/elk-api.js) nicht geladen');
    elk = new globalThis.ELK({ workerUrl: 'vendor/elkjs/elk-worker.min.js' });
  }
  return elk;
}

const OPTIONS = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.edgeRouting': 'ORTHOGONAL',
  'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
  'elk.layered.crossingMinimization.forceNodeModelOrder': 'true',
  'elk.spacing.nodeNode': '40',
  'elk.layered.spacing.nodeNodeBetweenLayers': '56',
  'elk.spacing.edgeNode': '28',
  'elk.spacing.edgeEdge': '16',
  'elk.layered.spacing.edgeNodeBetweenLayers': '18',
  'elk.layered.spacing.edgeEdgeBetweenLayers': '12',
  'elk.edgeLabels.placement': 'CENTER',
  'elk.layered.edgeLabels.sideSelection': 'SMART_DOWN',
  'elk.edgeLabels.inline': 'false',
  'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
  'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
  'elk.padding': '[top=16,left=16,bottom=16,right=16]',
};

function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); }

export async function layoutBand(band, opts = {}) {
  const graph = {
    id: band.id,
    layoutOptions: { ...OPTIONS, ...(opts.layoutOptions ?? {}) },
    children: band.nodes.map((n) => ({ id: n.id, width: Math.ceil(n.width), height: Math.ceil(n.height) })),
    // Keine ELK-Labels: sie würden die Schichtabstände verbreitern; die Nummern-Marker setzt landscape.js auf das mittlere Segment
    edges: band.edges.map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  };
  const key = `v2-elk-${hash(JSON.stringify(graph))}`;
  try { const c = sessionStorage.getItem(key); if (c) return JSON.parse(c); } catch { /* kein Storage */ }
  const res = await engine().layout(graph);
  const out = { width: res.width, height: res.height, nodes: {}, edges: {} };
  for (const n of res.children ?? []) out.nodes[n.id] = { x: n.x, y: n.y, width: n.width, height: n.height };
  for (const e of res.edges ?? []) out.edges[e.id] = { sections: e.sections ?? [], labels: (e.labels ?? []).map((l) => ({ x: l.x, y: l.y, width: l.width, height: l.height })) };
  try { sessionStorage.setItem(key, JSON.stringify(out)); } catch { /* voll */ }
  return out;
}

/** Orthogonaler SVG-Pfad aus ELK-Sections mit gerundeten Ecken (Radius r). */
export function sectionPath(sections, r = 8, offset = { x: 0, y: 0 }) {
  const pts = [];
  for (const s of sections) {
    pts.push(s.startPoint, ...(s.bendPoints ?? []), s.endPoint);
  }
  return roundedPath(pts.map((p) => ({ x: p.x + offset.x, y: p.y + offset.y })), r);
}
export function roundedPath(pts, r = 8) {
  if (!pts.length) return '';
  if (pts.length < 3) return `M${pts[0].x} ${pts[0].y}${pts.slice(1).map((p) => ` L${p.x} ${p.y}`).join('')}`;
  let d = `M${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
    const d1 = Math.hypot(p1.x - p0.x, p1.y - p0.y), d2 = Math.hypot(p2.x - p1.x, p2.y - p1.y);
    const rr = Math.min(r, d1 / 2, d2 / 2);
    if (rr < 0.5) { d += ` L${p1.x} ${p1.y}`; continue; }
    const a = { x: p1.x - ((p1.x - p0.x) / d1) * rr, y: p1.y - ((p1.y - p0.y) / d1) * rr };
    const b = { x: p1.x + ((p2.x - p1.x) / d2) * rr, y: p1.y + ((p2.y - p1.y) / d2) * rr };
    d += ` L${a.x} ${a.y} Q${p1.x} ${p1.y} ${b.x} ${b.y}`;
  }
  const last = pts[pts.length - 1];
  return `${d} L${last.x} ${last.y}`;
}
