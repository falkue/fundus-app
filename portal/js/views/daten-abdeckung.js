/**
 * Datensätze › Abdeckung (#daten?tab=abdeckung): (a) Matrix Thema × Region – Anzahl Datensätze je Zelle, Klick führt in den
 * gefilterten Katalog; (b) Zeitleiste der Zeiträume (Gantt), gruppiert nach Thema oder Projekt, Hover zeigt den Titel,
 * Klick öffnet den Datensatz. Inline-SVG, Farben aus den Tokens (hell und dunkel).
 */
import { el, svgEl, card, segmented, emptyNote } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, loadCatalog, facetLabel, periodText, tip, hideTip, put } from './daten-lib.js';

let groupBy = 'topic';    // Zeitleiste: topic | project (bleibt beim Wechsel der Reiter)

export function render(root, _ctx, alive) {
  const box = el('div.dk-cover', el('div.dk-skel', el('div.dk-skel__card'), el('div.dk-skel__card')));
  root.append(box);
  loadCatalog().then((cat) => {
    if (!alive()) return;
    const ds = arr(cat.datasets).filter((d) => !d.removed_at);
    put(box, heatmap(cat, ds), timeline(cat, ds, alive));
  }).catch((e) => put(box, el('div.alert.alert--error', `Katalog nicht ladbar: ${e.message}`)));
}

// ---------------------------------------------------------------- Matrix Thema × Region
function heatmap(cat, ds) {
  const wrap = el('div.dk-hm-wrap');
  let all = false;
  const toggle = el('button.btn.btn--small', { type: 'button' });
  const NONE = '__none';
  function draw() {
    const tc = new Map(), rc = new Map(), m = new Map();
    for (const d of ds) {
      const ts = arr(d.tags?.topic), rs = arr(d.tags?.region).length ? arr(d.tags.region) : [NONE];
      for (const t of ts) {
        tc.set(t, (tc.get(t) ?? 0) + 1);
        for (const r of rs) m.set(`${t}\u0000${r}`, (m.get(`${t}\u0000${r}`) ?? 0) + 1);
      }
      for (const r of rs) rc.set(r, (rc.get(r) ?? 0) + 1);
    }
    const topics = [...tc].sort((a, b) => b[1] - a[1]).map((x) => x[0]);
    const regions = [...rc].sort((a, b) => (a[0] === NONE) - (b[0] === NONE) || b[1] - a[1]).map((x) => x[0]);
    const T = all ? topics : topics.slice(0, 16), R = all ? regions : regions.slice(0, 14);
    toggle.textContent = all ? 'Nur die häufigsten' : `Alle ${topics.length} Themen und ${regions.length} Regionen`;
    toggle.hidden = topics.length <= 16 && regions.length <= 14;
    if (!T.length) { put(wrap, emptyNote('Noch keine Themen vergeben.')); return; }
    const max = Math.max(1, ...m.values());
    const lvl = (n) => (n <= 0 ? 0 : Math.min(4, 1 + Math.floor((Math.log(n) / Math.log(max + 1)) * 4)));
    const LW = 190, CW = 40, CH = 26, TOP = 104, RW = 46;
    const W = LW + R.length * CW + RW, H = TOP + T.length * CH + 8;
    const s = svgEl('svg', { class: 'dk-hm', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': 'Anzahl Datensätze je Thema und Region' });
    const rlab = (r) => (r === NONE ? 'ohne Region' : facetLabel(cat, 'region', r));
    const tlab = (t) => facetLabel(cat, 'topic', t);
    const short = (x, n) => (x.length > n ? `${x.slice(0, n - 1)}…` : x);
    const link = (q) => { location.hash = `#daten?${new URLSearchParams(q)}`; };
    R.forEach((r, j) => {
      const x = LW + j * CW + CW / 2;
      const g = svgEl('g', { class: r === NONE ? 'dk-hm__col is-none' : 'dk-hm__col', transform: `translate(${x} ${TOP - 8}) rotate(-50)`, tabindex: r === NONE ? null : '0', role: r === NONE ? null : 'link' },
        svgEl('title', {}, `${rlab(r)}: ${rc.get(r)} Datensätze`), svgEl('text', { x: 0, y: 0 }, short(rlab(r), 18)));
      if (r !== NONE) { g.addEventListener('click', () => link({ region: r })); g.addEventListener('keydown', (e) => { if (e.key === 'Enter') link({ region: r }); }); }
      s.append(g);
    });
    s.append(svgEl('text', { x: LW + R.length * CW + 8, y: TOP - 8, class: 'dk-hm__sumhead' }, 'gesamt'));
    T.forEach((t, i) => {
      const y = TOP + i * CH;
      const lab = svgEl('g', { class: 'dk-hm__row', tabindex: '0', role: 'link' }, svgEl('title', {}, `${tlab(t)}: ${tc.get(t)} Datensätze – im Katalog zeigen`),
        svgEl('text', { x: LW - 10, y: y + CH / 2 + 4, 'text-anchor': 'end' }, short(tlab(t), 26)));
      lab.addEventListener('click', () => link({ topic: t }));
      lab.addEventListener('keydown', (e) => { if (e.key === 'Enter') link({ topic: t }); });
      s.append(lab);
      R.forEach((r, j) => {
        const n = m.get(`${t}\u0000${r}`) ?? 0;
        const x = LW + j * CW;
        const g = svgEl('g', { class: `dk-hm__cell dk-hm--${lvl(n)}`, tabindex: n ? '0' : null, role: n ? 'link' : null, 'aria-label': `${tlab(t)} × ${rlab(r)}: ${n}` },
          svgEl('rect', { x: x + 1, y: y + 1, width: CW - 2, height: CH - 2, rx: 4 }),
          n ? svgEl('text', { x: x + CW / 2, y: y + CH / 2 + 4, 'text-anchor': 'middle' }, String(n)) : null);
        if (n) {
          const q = r === NONE ? { topic: t } : { topic: t, region: r };
          g.addEventListener('click', () => link(q));
          g.addEventListener('keydown', (e) => { if (e.key === 'Enter') link(q); });
          g.addEventListener('mousemove', (e) => tip(e, [`${n} ${n === 1 ? 'Datensatz' : 'Datensätze'}`, `${tlab(t)} × ${rlab(r)}`, 'Klick: im Katalog zeigen']));
          g.addEventListener('mouseleave', hideTip);
        }
        s.append(g);
      });
      s.append(svgEl('text', { x: LW + R.length * CW + 8, y: y + CH / 2 + 4, class: 'dk-hm__sum' }, String(tc.get(t))));
    });
    const key = el('div.dk-hm__key', el('span.small.muted', 'weniger'), [1, 2, 3, 4].map((k) => el('span', { class: `dk-hm__keycell dk-hm--k${k}` })), el('span.small.muted', `mehr (höchstens ${max})`));
    put(wrap, el('div.dk-hm__scroll', s), key);
  }
  toggle.addEventListener('click', () => { all = !all; draw(); });
  draw();
  return card({ title: 'Themen je Region', accent: 'Region', tools: [toggle],
    sub: 'Anzahl Datensätze je Thema und Region (Schlagworte aus dem Fundus-Vokabular; ein Datensatz zählt in jeder seiner Zellen). Klick auf eine Zelle, ein Thema oder eine Region öffnet den gefilterten Katalog.' }, wrap);
}

// ---------------------------------------------------------------- Zeitleiste der Zeiträume
function timeline(cat, ds, alive) {
  const host = el('div.dk-gantt');
  const seg = segmented([{ value: 'topic', label: 'nach Thema' }, { value: 'project', label: 'nach Projekt' }], groupBy, (v) => { groupBy = v; draw(); }, 'Gruppierung');
  const withP = ds.filter((d) => d.period_from);
  const noP = ds.length - withP.length;
  let open = null;   // offene Gruppen (beim ersten Zeichnen die drei grössten)
  function draw() {
    if (!alive()) return;
    if (!withP.length) { put(host, emptyNote('Kein Datensatz mit Zeitraum.')); return; }
    const groups = new Map();
    for (const d of withP) {
      const k = groupBy === 'project' ? (d.project || '') : (arr(d.tags?.topic)[0] || '');
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(d);
    }
    const sorted = [...groups].sort((a, b) => (a[0] === '') - (b[0] === '') || b[1].length - a[1].length);
    if (!open || open.by !== groupBy) { open = new Set(sorted.slice(0, 3).map((g) => g[0])); open.by = groupBy; }
    // Achse ab dem 5-%-Quantil der Anfangsjahre (einzelne sehr alte Reihen würden sonst alles zusammendrücken);
    // frühere Anfänge zeigt ein Pfeil am linken Rand, der Tooltip nennt den echten Zeitraum.
    const starts = withP.map((d) => d.period_from).sort((a, b) => a - b);
    const y0 = Math.floor(starts[Math.floor(starts.length * 0.05)] / 10) * 10;
    const y1 = Math.max(new Date().getFullYear(), ...withP.map((d) => d.period_to ?? d.period_from));
    const width = Math.max(320, host.clientWidth || root.clientWidth || 900);
    const LW = width < 640 ? 130 : 300;
    const PW = width - LW - 16;
    const x = (y) => LW + ((y - y0) / Math.max(1, y1 - y0 + 1)) * PW;
    const step = (y1 - y0) > 120 ? 25 : (y1 - y0) > 50 ? 10 : (y1 - y0) > 20 ? 5 : 2;
    const ticks = [];
    for (let t = Math.ceil(y0 / step) * step; t <= y1; t += step) ticks.push(t);
    const axis = svgEl('svg', { class: 'dk-gantt__axis', viewBox: `0 0 ${width} 22`, width, height: 22, 'aria-hidden': 'true' },
      ticks.map((t) => svgEl('g', {}, svgEl('line', { x1: x(t), x2: x(t), y1: 16, y2: 22 }), svgEl('text', { x: x(t), y: 12, 'text-anchor': 'middle' }, String(t)))));
    const RH = 14;
    const sections = sorted.map(([k, list]) => {
      list.sort((a, b) => a.period_from - b.period_from || (a.period_to ?? 0) - (b.period_to ?? 0));
      const label = k ? facetLabel(cat, groupBy === 'project' ? 'project' : 'topic', k) : (groupBy === 'project' ? 'ohne Projekt' : 'ohne Thema');
      const det = el('details.dk-gantt__grp', { open: open.has(k) });
      det.append(el('summary', el('span', label), el('span.muted.small', ` ${list.length}`)));
      const H = list.length * RH + 6;
      const s = svgEl('svg', { class: 'dk-gantt__svg', viewBox: `0 0 ${width} ${H}`, width, height: H, role: 'list', 'aria-label': `Zeiträume: ${label}` });
      ticks.forEach((t) => s.append(svgEl('line', { class: 'dk-gantt__grid', x1: x(t), x2: x(t), y1: 0, y2: H })));
      list.forEach((d, i) => {
        const y = 3 + i * RH;
        const cut = d.period_from < y0;
        const a = x(Math.max(y0, d.period_from)), b = x(Math.max(y0, d.period_to ?? d.period_from) + 1);
        const maxChars = Math.floor((LW - 12) / 6.1);
        const t = d.title.length > maxChars ? `${d.title.slice(0, maxChars - 1)}…` : d.title;
        const g = svgEl('a', { href: `#daten?d=${d.slug}`, class: `dk-gantt__row${d.lifecycle && d.lifecycle !== 'active' ? ' is-faded' : ''}${d.visibility === 'internal' ? ' is-internal' : ''}`, role: 'listitem', 'aria-label': `${d.title}, ${periodText(d)}` },
          svgEl('rect', { class: 'dk-gantt__hit', x: 0, y, width, height: RH }),
          svgEl('text', { x: LW - 10, y: y + RH - 3.5, 'text-anchor': 'end' }, t),
          svgEl('rect', { class: 'dk-gantt__bar', x: a, y: y + 2.5, width: Math.max(3, b - a), height: RH - 5, rx: 2 }),
          cut ? svgEl('path', { class: 'dk-gantt__cut', d: `M${a - 2},${y + RH / 2} l5,-4 v8 z` }) : null);
        g.addEventListener('mousemove', (e) => tip(e, [d.title, kids2(periodText(d), d.publisher_group, d.rows != null ? `${fmt.int(d.rows)} Zeilen` : null).join(' · '), d.ref]));
        g.addEventListener('mouseleave', hideTip);
        s.append(g);
      });
      det.append(s);
      det.addEventListener('toggle', () => { if (det.open) open.add(k); else open.delete(k); });
      return det;
    });
    put(host, el('div.dk-gantt__head', axis), ...sections);
  }
  const root = card({ title: 'Zeiträume der Datensätze', accent: 'Zeiträume', tools: [seg.el],
    sub: `Jede Leiste ist ein Datensatz von seinem ersten bis zum letzten Jahr${groupBy === 'topic' ? ' (eingeordnet unter seinem ersten Thema)' : ''}. Violett: interne Datensätze; blass: ersetzt oder zurückgezogen; ◂ beginnt vor dem Achsenanfang. ${noP ? `${noP} Datensätze ohne erkennbaren Zeitraum fehlen.` : ''}` }, host);
  requestAnimationFrame(draw);
  let w = 0;
  const ro = new ResizeObserver(() => { if (!host.isConnected) { ro.disconnect(); return; } if (Math.abs(host.clientWidth - w) > 40) { w = host.clientWidth; draw(); } });
  ro.observe(host);
  return root;
}
const kids2 = (...a) => a.filter(Boolean);
