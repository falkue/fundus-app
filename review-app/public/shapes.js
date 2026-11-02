/**
 * Markierungen auf einer Folie: Rahmen, Kreis, Pfeil, Stift. Koordinaten relativ zur Folie (0–1), damit sie auf jedem
 * Bildschirm und auf der grossen PNG passen. Gezeichnet als SVG über dem Bild (viewBox 1600×900 = 16:9, ohne Verzerrung).
 *   rect / ellipse: {tool, x, y, w, h}   arrow: {tool, x1, y1, x2, y2}   pen: {tool, pts: [x1, y1, x2, y2, …]}
 * Gleicher Code im Portal (portal/js/views/shapes.js).
 */
const NS = 'http://www.w3.org/2000/svg';
export const W = 1600, H = 900;
export const TOOLS = ['rect', 'ellipse', 'arrow', 'pen'];

function svg(tag, attrs) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

/** Ankerpunkt für die Nummer einer Markierung (oben links bzw. Anfang). */
export function anchor(s) {
  if (s.tool === 'arrow') return { x: s.x1, y: s.y1 };
  if (s.tool === 'pen') return { x: s.pts?.[0] ?? 0, y: s.pts?.[1] ?? 0 };
  return { x: s.x, y: s.y };
}

/** SVG-Gruppe einer Markierung; cls färbt (Person, Gemini, Entwurf). */
export function shapeNode(s, cls) {
  const g = svg('g', { class: cls });
  const common = { fill: 'none', 'vector-effect': 'non-scaling-stroke', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
  if (s.tool === 'rect') g.append(svg('rect', { ...common, x: s.x * W, y: s.y * H, width: s.w * W, height: s.h * H, rx: 6 }));
  else if (s.tool === 'ellipse') g.append(svg('ellipse', { ...common, cx: (s.x + s.w / 2) * W, cy: (s.y + s.h / 2) * H, rx: (s.w / 2) * W, ry: (s.h / 2) * H }));
  else if (s.tool === 'arrow') {
    const x1 = s.x1 * W, y1 = s.y1 * H, x2 = s.x2 * W, y2 = s.y2 * H;
    const a = Math.atan2(y2 - y1, x2 - x1), L = 28, sp = 0.45;
    g.append(svg('line', { ...common, x1, y1, x2, y2 }));
    g.append(svg('polyline', { ...common, points: `${x2 - L * Math.cos(a - sp)},${y2 - L * Math.sin(a - sp)} ${x2},${y2} ${x2 - L * Math.cos(a + sp)},${y2 - L * Math.sin(a + sp)}` }));
  } else if (s.tool === 'pen') {
    const p = s.pts ?? [];
    const pts = [];
    for (let i = 0; i + 1 < p.length; i += 2) pts.push(`${p[i] * W},${p[i + 1] * H}`);
    g.append(svg('polyline', { ...common, points: pts.join(' ') }));
  }
  return g;
}

export function newSvg() {
  return svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', class: 'shapes-svg', 'aria-hidden': 'true' });
}

/** Freihandlinie ausdünnen (Ramer–Douglas–Peucker) und auf 4 Stellen runden. */
export function simplify(points, eps = 0.003, max = 400) {
  if (points.length <= 2) return points;
  const dist = (p, a, b) => {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const l = dx * dx + dy * dy;
    const t = l ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l)) : 0;
    return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
  };
  const rdp = (pts) => {
    let k = 0, dm = 0;
    for (let i = 1; i < pts.length - 1; i++) { const d = dist(pts[i], pts[0], pts[pts.length - 1]); if (d > dm) { dm = d; k = i; } }
    return dm > eps ? [...rdp(pts.slice(0, k + 1)).slice(0, -1), ...rdp(pts.slice(k))] : [pts[0], pts[pts.length - 1]];
  };
  let out = rdp(points);
  while (out.length > max) out = out.filter((_, i) => i % 2 === 0 || i === out.length - 1);
  return out;
}

/** Aus Zieh-Start und -Ende bzw. Punkten die gespeicherte Form (Koordinaten 0–1, gerundet). */
export function makeShape(tool, a, b, path) {
  const r = (v) => Math.round(Math.min(1, Math.max(0, v)) * 10000) / 10000;
  if (tool === 'arrow') return { tool, x1: r(a.x), y1: r(a.y), x2: r(b.x), y2: r(b.y) };
  if (tool === 'pen') return { tool, pts: simplify(path.map((p) => [p.x, p.y])).flatMap(([x, y]) => [r(x), r(y)]) };
  return { tool, x: r(Math.min(a.x, b.x)), y: r(Math.min(a.y, b.y)), w: r(Math.abs(b.x - a.x)), h: r(Math.abs(b.y - a.y)) };
}

/** Gross genug, um als Markierung zu zählen. */
export function bigEnough(s) {
  if (s.tool === 'arrow') return Math.hypot(s.x2 - s.x1, s.y2 - s.y1) > 0.02;
  if (s.tool === 'pen') return (s.pts?.length ?? 0) >= 4;
  return s.w > 0.01 && s.h > 0.01;
}
export const isShape = (s) => !!s && TOOLS.includes(s.tool);
