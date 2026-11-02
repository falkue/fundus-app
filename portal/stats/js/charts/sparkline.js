/**
 * Sparkline (Linie + weiche Fläche + Endpunkt). Ohne Achsen.
 * sparkline(container, values, {width, height, min, max}) – values: Zahlen (null wird entfernt)
 * Bei < 2 Punkten: gestrichelte Grundlinie (keine Daten / zu wenig).
 */
const d3 = globalThis.d3;

export function sparkline(container, values, opts = {}) {
  const { width = 84, height = 30, min, max, dot = true } = opts;
  const v = (values ?? []).filter((x) => typeof x === 'number' && Number.isFinite(x));
  const svg = d3.select(container).append('svg')
    .attr('viewBox', `0 0 ${width} ${height}`).attr('width', width).attr('height', height)
    .attr('role', 'img').attr('aria-label', v.length ? `Verlauf, ${v.length} Punkte` : 'kein Verlauf');
  if (v.length < 2) {
    svg.append('line').attr('class', 'spark__empty').attr('x1', 0).attr('x2', width).attr('y1', height - 2).attr('y2', height - 2);
    if (v.length === 1 && dot) svg.append('circle').attr('class', 'spark__dot').attr('cx', width - 3).attr('cy', height - 2).attr('r', 2.5);
    return svg.node();
  }
  const x = d3.scaleLinear().domain([0, v.length - 1]).range([1, width - 3]);
  const lo = min ?? d3.min(v), hi = max ?? d3.max(v);
  const y = d3.scaleLinear().domain(lo === hi ? [lo - 1, hi + 1] : [lo, hi]).range([height - 2, 3]);
  const area = d3.area().x((d, i) => x(i)).y0(height - 1).y1((d) => y(d)).curve(d3.curveMonotoneX);
  const line = d3.line().x((d, i) => x(i)).y((d) => y(d)).curve(d3.curveMonotoneX);
  svg.append('path').attr('class', 'spark__area').attr('d', area(v));
  svg.append('path').attr('class', 'spark__line').attr('d', line(v));
  if (dot) svg.append('circle').attr('class', 'spark__dot').attr('cx', x(v.length - 1)).attr('cy', y(v[v.length - 1])).attr('r', 2.5);
  return svg.node();
}
