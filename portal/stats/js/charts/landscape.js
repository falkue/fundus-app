/**
 * Landschaftskarte (C4-Deployment) – ELK-Layout + eigenes Rendering:
 *   - Knoten als absolut positionierte HTML-Karten (app.css, kein foreignObject)
 *   - Zonen-Bänder (Internet | Heimnetz) und Kanten als SVG darunter; Kanten orthogonal
 *     mit gerundeten Ecken aus den ELK-Sections, Kanten innerhalb eines Bandes von ELK,
 *     Kanten zwischen Bändern durch den Zwischenraum geroutet
 *   - Tailnet ist kein Band: Marke (Lucide „network“ + IP) an der Karte
 *   - nummerierte Marker auf den Kanten + Liste (Hover koppelt Liste ↔ Pfad ↔ Endpunkte)
 *   - mini: dieselbe Struktur mit kleinen Karten (Lage); mobil (< 640 px): Karten gestapelt
 * Rückgabe: Aufräumfunktion. Zeichnet nach document.fonts.ready neu (Messung).
 */
import { norm, STATES, SEVERITY_STATE } from '../lib/state.js';
import * as tip from '../lib/tooltip.js';
import { servicesOf, isFresh } from '../lib/derive.js';
import { responsive, svgPx } from '../lib/chart.js';
import { el, clear, mark, lu, badge } from '../lib/dom.js';
import { measure } from '../lib/svgtext.js';
import * as fmt from '../lib/fmt.js';
import { layoutBand, sectionPath, roundedPath } from './landscape-layout.js';

const d3 = globalThis.d3;
export const LINK_LABEL = { backup: 'Sicherung', 'ssh-collect': 'Erhebung', data: 'Datenzugriff', serves: 'bietet an' };
const ZONE = { internet: { label: 'Internet', sub: 'öffentlich erreichbar' }, lan: { label: 'Heimnetz', sub: 'LAN 192.168.1.0/24 · Tailnet-Marke an der Karte' } };
const KIND_ICON = { nas: 'hard-drive', workstation: 'cpu', vps: 'server', laptop: 'laptop', server: 'server', cloud: 'cloud' };
const NEW_H = 6;

export function renderLandscape(container, data, opts = {}) {
  let token = 0;
  container.classList.add('lmap');
  return responsive(container, (w, { avail }) => {
    const my = ++token;
    const mode = opts.mini ? 'mini' : avail < 640 ? 'mobile' : 'desktop';
    draw(container, data, { ...opts, mode, width: w, avail }, () => my === token).catch((e) => { console.error(e); container.append(el('div.empty', mark('unknown'), `Karte konnte nicht gezeichnet werden: ${e.message}`)); });
  }, { minWidth: 0 });
}

// ---------------------------------------------------------------- Modell
function model(data) {
  const nodes = data.nodes.map((n) => ({ ...n, services: servicesOf(data, n.id), zoneId: n.zone === 'internet' ? 'internet' : 'lan' }));
  const ids = new Set(nodes.map((n) => n.id));
  const serves = data.links.filter((l) => l.kind === 'serves' && ids.has(l.from));
  if (serves.length) nodes.push({ id: '__public', label: 'Öffentlichkeit', alias: 'Besucher der Website', kind: 'public', zoneId: 'internet', state: 'ok', services: [], pseudo: true });
  // Cloud (Google Firebase): EIN zusammengefasster Knoten mit einem Chip je Projekt
  const fb = data.apps.filter((a) => a.platform === 'firebase');
  if (fb.length) {
    const svc = fb.map((a) => ({ id: `app:${a.id}`, label: a.label ?? a.id, state: a.state ?? 'unknown', detail: [a.role, a.project, a.access === 'missing' ? 'Nutzungsdaten nicht freigegeben' : null].filter(Boolean).join(' · ') }));
    nodes.push({ id: '__firebase', label: 'Firebase', alias: `${fb.length} ${fb.length === 1 ? 'Projekt' : 'Projekte'} · Google Cloud`, kind: 'cloud', zoneId: 'internet', cloud: true,
      state: worstState(svc.map((s) => s.state)), stateReason: `${fb.filter((a) => a.state === 'ok').length} ok · ${fb.filter((a) => a.access === 'missing').length} ohne Nutzungsdaten`, role: 'Zone Cloud (Google Firebase) · Hosting, Firestore, Functions', services: svc, addr: null, marks: { fresh: 0, acked: 0 } });
  }
  const links = data.links.filter((l) => ids.has(l.from) && (ids.has(l.to) || l.kind === 'serves')).map((l, i) => ({ ...l, i, no: i + 1, to: l.kind === 'serves' && !ids.has(l.to) ? '__public' : l.to }));
  const zoneOf = (id) => nodes.find((n) => n.id === id)?.zoneId;
  for (const l of links) l.cross = zoneOf(l.from) !== zoneOf(l.to);
  // Marken je Knoten: neu (Wechsel nach Beobachtungsbeginn + 6 h und jünger als 6 h) und quittiert
  for (const n of nodes) {
    const fresh = n.services.filter((s) => isFresh(s.changedAt, data)).length
      + data.findings.filter((f) => f.node === n.id && !f.acked && isFresh(f.firstSeen, data)).length;
    const acked = data.findings.filter((f) => f.node === n.id && f.acked).length;
    n.marks = { fresh, acked };
  }
  return { nodes, links };
}

// ---------------------------------------------------------------- Karten (HTML)
function card(n, { mini }) {
  const s = norm(n.state);
  const a = el('article.lcard', { class: `lcard lcard--${s}${n.kind === 'laptop' || (s === 'unknown' && /nicht überwacht/i.test(n.stateReason ?? '')) ? ' lcard--unmonitored' : ''}${n.pseudo ? ' lcard--pseudo' : ''}${n.cloud ? ' lcard--cloud' : ''}${mini ? ' lcard--mini' : ''}`,
    tabindex: '0', role: 'button', 'aria-label': `${n.label ?? n.id}: ${STATES[s].word}${n.stateReason ? ` – ${n.stateReason}` : ''}`, 'data-node': n.id });
  const head = el('div.lcard__head',
    el('span.lcard__ring', { class: `lcard__ring lcard__ring--${s}` }, mark(s)),
    el('span.lcard__title', el('span.lcard__name', n.label ?? n.id), n.alias ? el('small.lcard__alias', { title: n.alias }, n.alias) : null),
    n.pseudo ? lu('globe', 'lu lcard__kind') : lu(KIND_ICON[n.kind] ?? 'server', 'lu lcard__kind'));
  a.append(head);
  const stateText = n.pseudo ? (n.alias ?? '') : STATES[s].word + (n.stateReason ? ` · ${n.stateReason}` : '');
  a.append(el('p.lcard__state', { class: `lcard__state lcard__state--${s}`, title: stateText }, stateText));
  if (mini || n.pseudo) return a;
  a.append(el('p.lcard__role', { title: [n.role, n.os?.name ? `${n.os.name} ${n.os.version ?? ''}`.trim() : null, n.hardware?.model].filter(Boolean).join(' · ') },
    [n.role, n.os?.name ? `${n.os.name} ${n.os.version ?? ''}`.trim() : null, n.hardware?.model].filter(Boolean).join(' · ') || 'keine Angaben'));
  const addr = el('p.lcard__addr');
  if (n.cloud) addr.append(el('span', lu('cloud', 'lu lu--sm'), 'Google Cloud · Region europe'));
  if (n.addr?.lan) addr.append(el('span', `LAN ${n.addr.lan}`));
  if ((n.alsoIn ?? []).includes('tailnet') || n.addr?.tailnet) addr.append(el('span.lcard__tail', { title: 'Mitglied im Tailnet (WireGuard-Overlay)' }, lu('network', 'lu lu--sm'), n.addr?.tailnet ?? 'Tailnet'));
  if (n.addr?.public) addr.append(el('span', lu('globe', 'lu lu--sm'), n.addr.public));
  if (!addr.children.length) addr.append(el('span.muted', n.cloud ? '' : 'keine Adressen erhoben'));
  a.append(addr);
  if (n.marks?.fresh || n.marks?.acked) {
    const m = el('div.lcard__marks');
    if (n.marks.fresh) m.append(el('span.zmark.zmark--new', { title: `${n.marks.fresh} Änderung(en) in den letzten ${NEW_H} h` }, mark('warn'), `${n.marks.fresh} neu`));
    if (n.marks.acked) m.append(el('span.zmark.zmark--ack', { title: `${n.marks.acked} quittierte Befunde` }, lu('check', 'lu lu--sm'), `${n.marks.acked} quittiert`));
    a.append(m);
  }
  const chips = el('div.lcard__chips');
  const sorted = [...n.services].sort((x, y) => rank(y.state) - rank(x.state) || String(x.label).localeCompare(String(y.label), 'de'));
  for (const svc of sorted) {
    const c = el('span.chip.lcard__chip', { class: `chip lcard__chip${norm(svc.state) === 'off' ? ' chip--off' : ''}`, tabindex: '0', title: svc.label ?? svc.id }, mark(svc.state), el('span.lcard__chip-text', svc.label ?? svc.id));
    tip.bindEl(c, () => ({ title: svc.label ?? svc.id, rows: [['Zustand', STATES[norm(svc.state)].word], ['Detail', svc.detail ?? null], ['Version', svc.version ?? null], ['Ports', svc.ports?.length ? svc.ports.join(', ') : null]] }));
    chips.append(c);
  }
  if (!sorted.length) chips.append(el('span.small.muted', 'keine Dienste erfasst'));
  a.append(chips);
  return a;
}
/** Chips auf max. 3 Zeilen kürzen (nach dem Layout messen), Rest als „+n weitere“. */
function trimChips(cardEl, maxRows = 3) {
  const box = cardEl.querySelector('.lcard__chips');
  if (!box) return;
  const chips = [...box.querySelectorAll('.lcard__chip')];
  if (!chips.length) return;
  const rowH = chips[0].offsetHeight + 6;
  const top0 = chips[0].offsetTop;
  let hidden = 0;
  const limit = top0 + (maxRows - 1) * rowH + 2;
  for (const c of chips) { if (c.offsetTop > limit) { c.hidden = true; hidden++; } }
  if (!hidden) return;
  const more = el('span.lcard__more', `+${hidden} weitere`);
  box.append(more);
  // passt „+n“ nicht mehr in die letzte Zeile, einen weiteren Chip opfern
  let guard = 0;
  while (more.offsetTop > limit && guard++ < 10) {
    const last = chips.filter((c) => !c.hidden).pop();
    if (!last) break;
    last.hidden = true; hidden++;
    more.textContent = `+${hidden} weitere`;
  }
}

// ---------------------------------------------------------------- Zeichnen
async function draw(container, data, o, alive) {
  const { mode, width, onNode, onLinkHover, legend } = o;
  const mini = mode === 'mini';
  const M = model(data);
  clear(container);
  container.dataset.mode = mode;

  // Karten bauen; Reihen so wählen, dass die Karte in die Breite PASST (kein seitliches Scrollen)
  const pad = mini ? 12 : 28, layerGap = 56, elkPad = 32;
  const lanNodes = M.nodes.filter((n) => n.zoneId === 'lan');
  const minCard = 236, maxCard = 360;
  const maxPerZone = Math.max(lanNodes.length, M.nodes.filter((n) => n.zoneId === 'internet').length, 1);
  const perRow = Math.max(1, Math.min(maxPerZone, Math.floor((width - 2 * pad - elkPad - 48 + layerGap) / (minCard + layerGap))));
  const cardW = mini ? 220 : Math.max(minCard, Math.min(maxCard, Math.floor((width - 2 * pad - elkPad - 48 - (perRow - 1) * layerGap) / perRow)));
  const cardsLayer = el('div.lmap__cards');
  const cards = new Map();
  for (const n of M.nodes) {
    const c = card(n, { mini });
    c.style.width = `${n.pseudo ? Math.min(cardW, 200) : cardW}px`;
    cards.set(n.id, c);
    cardsLayer.append(c);
    c.addEventListener('click', () => onNode?.(n.id));
    c.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNode?.(n.id); } });
    tip.bindEl(c, () => ({ title: `${n.label ?? n.id}${n.alias ? ` · ${n.alias}` : ''}`, rows: n.pseudo ? [['Rolle', 'Ziel der öffentlichen Dienste']] : [['Zustand', STATES[norm(n.state)].word], ['Grund', n.stateReason ?? null], ['LAN', n.addr?.lan ?? null], ['Tailnet', n.addr?.tailnet ?? null], ['Öffentlich', n.addr?.public ?? null]], note: mini ? 'Klick öffnet die Landschaft' : 'Klick öffnet Details' }));
  }
  container.append(cardsLayer);

  if (mode === 'mobile') { drawMobile(container, M, cards, legend); return; }
  if (mini) { drawMini(container, M, cards); return; }

  cardsLayer.classList.add('lmap__cards--measure');
  if (document.fonts?.ready) await document.fonts.ready;
  for (const c of cards.values()) trimChips(c);
  const size = new Map([...cards].map(([id, c]) => [id, { w: c.offsetWidth, h: c.offsetHeight }]));

  // Stufen (tiers): Internet = eine Reihe; Heimnetz = so viele Reihen wie nötig (Modellreihenfolge)
  const tiers = [];
  const internetNodes = M.nodes.filter((n) => n.zoneId === 'internet');
  for (let i = 0; i < internetNodes.length; i += perRow) tiers.push({ zone: 'internet', nodes: internetNodes.slice(i, i + perRow) });
  for (let i = 0; i < lanNodes.length; i += perRow) tiers.push({ zone: 'lan', nodes: lanNodes.slice(i, i + perRow) });
  if (!lanNodes.length) tiers.push({ zone: 'lan', nodes: [] });
  const tierOf = new Map();
  tiers.forEach((t, i) => { t.i = i; for (const n of t.nodes) tierOf.set(n.id, i); });
  const sameTier = (l) => tierOf.get(l.from) !== undefined && tierOf.get(l.from) === tierOf.get(l.to);

  // ELK je Stufe (Worker), sequentiell
  for (const t of tiers) {
    if (!t.nodes.length) { t.L = null; continue; }
    const edges = M.links.filter((l) => sameTier(l) && tierOf.get(l.from) === t.i).map((l) => ({ id: `e${l.i}`, source: l.from, target: l.to }));
    // virtuelle Kanten zwischen Nachbarn ohne Verbindung: hält die Reihe waagrecht (werden nicht gezeichnet)
    for (let k = 1; k < t.nodes.length; k++) {
      const a = t.nodes[k - 1].id, b = t.nodes[k].id;
      if (!edges.some((e) => (e.source === a && e.target === b) || (e.source === b && e.target === a))) edges.push({ id: `v${t.i}-${k}`, source: a, target: b });
    }
    t.L = await layoutBand({ id: `${t.zone}-${t.i}`, nodes: t.nodes.map((n) => ({ id: n.id, ...sizeWH(size.get(n.id)) })), edges });
  }
  if (!alive()) return;

  // Stufen stapeln: Zonen-Bänder umfassen ihre Stufen; zwischen Stufen Platz für Kreuz-Verbindungen
  const labelH = 34, tierGap = 36, bandGap = 44;
  const crossCount = (a, b) => M.links.filter((l) => !sameTier(l) && tierOf.has(l.from) && tierOf.has(l.to) && Math.min(tierOf.get(l.from), tierOf.get(l.to)) === a && Math.max(tierOf.get(l.from), tierOf.get(l.to)) === b).length;
  const W = width;
  let y = 0;
  const bands = [];
  tiers.forEach((t, i) => {
    const prev = tiers[i - 1];
    const newBand = !prev || prev.zone !== t.zone;
    const extra = prev ? Math.max(0, crossCount(i - 1, i) - 1) * 24 : 0;
    if (newBand) { if (prev) { t.laneY0 = y + bandGap / 2 - 2; y += bandGap + extra; } bands.push({ id: t.zone, y, tiers: [] }); t.oy = y + labelH; }
    else { t.laneY0 = y + tierGap / 2 - 2; y += tierGap + extra; t.oy = y; }
    const innerW = t.L ? t.L.width : 0, innerH = t.L ? t.L.height : 36;
    t.ox = Math.max(pad, Math.round((W - innerW) / 2));
    t.gapAbove = newBand ? bandGap : tierGap;
    bands[bands.length - 1].tiers.push(t);
    y = t.oy + innerH;
  });
  for (const b of bands) { const last = b.tiers[b.tiers.length - 1]; b.h = last.oy + (last.L ? last.L.height : 36) + pad - b.y; }
  const H = y + pad;
  container.style.overflowX = '';
  container.style.height = `${H}px`;
  container.style.width = '';

  // SVG-Ebene (Bänder, Kanten)
  const svg = svgPx(container, W, H, `Landschaftskarte mit ${M.nodes.filter((n) => !n.pseudo).length} Knoten in zwei Zonen`).attr('class', 'lmap__svg');
  Object.assign(svg.node().style, { position: 'absolute', left: '0', top: '0' });
  const defs = svg.append('defs');
  for (const [id, cls] of [['arrow', 'map__arrow'], ['arrow-hover', 'map__arrow map__arrow--hover']]) {
    defs.append('marker').attr('id', `${id}-${mode}`).attr('viewBox', '0 0 10 10').attr('refX', 8).attr('refY', 5).attr('markerWidth', 8).attr('markerHeight', 8).attr('orient', 'auto-start-reverse')
      .append('path').attr('d', 'M1 1 L9 5 L1 9 Z').attr('class', cls);
  }
  const gB = svg.append('g');
  for (const b of bands) {
    gB.append('rect').attr('class', `map__zone map__zone--${b.id}`).attr('x', 0).attr('y', b.y).attr('width', W).attr('height', b.h).attr('rx', 12);
    const t = gB.append('text').attr('class', 'map__zone-label').attr('x', pad).attr('y', b.y + 22).text(ZONE[b.id].label);
    gB.append('text').attr('class', 'map__zone-sub').attr('x', pad + measure(t.node()) + 12).attr('y', b.y + 22).text(ZONE[b.id].sub);
    if (!b.tiers.some((x) => x.L)) gB.append('text').attr('class', 'chart-note').attr('x', pad).attr('y', b.y + labelH + 20).text('keine Knoten in dieser Zone');
  }

  // Kartenpositionen aus ELK
  const pos = new Map();
  for (const t of tiers) {
    if (!t.L) continue;
    for (const [id, p] of Object.entries(t.L.nodes)) {
      const x = t.ox + p.x, yy = t.oy + p.y;
      pos.set(id, { x, y: yy, w: p.width, h: p.height });
      const c = cards.get(id);
      c.style.left = `${x}px`; c.style.top = `${yy}px`;
    }
  }
  cardsLayer.classList.remove('lmap__cards--measure');

  // Kanten: innerhalb einer Stufe aus ELK; zwischen Stufen orthogonal durch den Zwischenraum
  const routes = [];
  for (const t of tiers) {
    if (!t.L) continue;
    for (const l of M.links.filter((x) => sameTier(x) && tierOf.get(x.from) === t.i)) {
      const e = t.L.edges[`e${l.i}`];
      if (!e) continue;
      routes.push({ l, d: sectionPath(e.sections, 8, { x: t.ox, y: t.oy }), badge: midOf(e.sections, t) });
    }
  }
  const laneCounter = new Map();
  for (const l of M.links.filter((x) => !sameTier(x) && tierOf.has(x.from) && tierOf.has(x.to))) {
    const a = pos.get(l.from), b = pos.get(l.to);
    if (!a || !b) continue;
    const upper = a.y < b.y ? a : b, lower = a.y < b.y ? b : a;
    const lowerTier = tiers[Math.max(tierOf.get(l.from), tierOf.get(l.to))];
    const k = laneCounter.get(lowerTier.i) ?? 0; laneCounter.set(lowerTier.i, k + 1);
    const laneY = (lowerTier.laneY0 ?? lowerTier.oy - 20) + k * 24;
    const fromIsUpper = upper === a;
    const sx = upper.x + upper.w * 0.3, tx = lower.x + lower.w * 0.5;
    const pts = fromIsUpper
      ? [{ x: sx, y: upper.y + upper.h }, { x: sx, y: laneY }, { x: tx, y: laneY }, { x: tx, y: lower.y - 2 }]
      : [{ x: tx, y: lower.y }, { x: tx, y: laneY }, { x: sx, y: laneY }, { x: sx, y: upper.y + upper.h + 2 }];
    routes.push({ l, d: roundedPath(pts, 8), badge: { x: (sx + tx) / 2, y: laneY } });
  }
  routes.sort((p, q) => p.l.no - q.l.no);

  const gL = svg.append('g');
  const linkSel = gL.selectAll('g.map__link-g').data(routes).join('g').attr('class', 'map__link-g');
  linkSel.append('path').attr('class', (r) => `map__link map__link--${r.l.kind ?? 'data'}`).attr('d', (r) => r.d).attr('marker-end', `url(#arrow-${mode})`);
  const bg = linkSel.append('g').attr('class', 'map__badge').attr('transform', (r) => `translate(${r.badge.x}, ${r.badge.y})`);
  bg.append('circle').attr('r', 11);
  bg.append('text').attr('y', 3.5).attr('text-anchor', 'middle').text((r) => r.l.no);
  const hit = linkSel.append('path').attr('class', 'map__link-hit').attr('d', (r) => r.d).attr('tabindex', 0).attr('role', 'img')
    .attr('aria-label', (r) => `${r.l.no}: ${r.l.from} → ${r.l.to}, ${r.l.label ?? LINK_LABEL[r.l.kind] ?? ''}`);

  const setHover = (r) => {
    linkSel.classed('map__link-g--hover', (x) => x === r).classed('map__link-g--dim', (x) => r && x !== r);
    linkSel.select('.map__link').attr('marker-end', (x) => (x === r ? `url(#arrow-hover-${mode})` : `url(#arrow-${mode})`));
    for (const [id, c] of cards) c.classList.toggle('lcard--dim', !!r && id !== r.l.from && id !== r.l.to);
    legend?.querySelectorAll('.map-legend__item').forEach((li) => li.classList.toggle('map-legend__item--hover', !!r && li.dataset.link === String(r.l.no)));
    onLinkHover?.(r ? r.l : null);
  };
  if (legend) renderLegend(legend, routes, setHover, false);
  hit.on('pointerenter', (e, r) => { setHover(r); tip.show(e, linkTip(r, data)); })
    .on('pointermove', (e) => tip.move(e))
    .on('pointerleave', () => { setHover(null); tip.hide(); })
    .on('focus', (e, r) => setHover(r)).on('blur', () => setHover(null));
  container.insertBefore(svg.node(), cardsLayer);
}

/** Mini-Karte (Lage): kompaktes Raster je Zone, nur Karten mit Zustandsring – ohne Kanten. */
function drawMini(container, M, cards) {
  container.style.height = '';
  const layer = container.querySelector('.lmap__cards');
  layer.classList.add('lmap__cards--flow');
  for (const z of ['internet', 'lan']) {
    const zn = M.nodes.filter((n) => n.zoneId === z && !n.pseudo);
    if (!zn.length) continue;
    const band = el('section.lmap__band', { class: `lmap__band lmap__band--${z} lmap__band--mini` }, el('div.lmap__band-head', el('span.label', ZONE[z].label)));
    const grid = el('div.lmap__grid');
    for (const n of zn) { const c = cards.get(n.id); c.style.width = ''; grid.append(c); }
    band.append(grid);
    layer.append(band);
  }
  for (const n of M.nodes.filter((x) => x.pseudo)) cards.get(n.id)?.remove();
}
function drawMobile(container, M, cards, legend) {
  container.style.height = '';
  const layer = container.querySelector('.lmap__cards');
  layer.classList.add('lmap__cards--flow');
  for (const z of ['internet', 'lan']) {
    const zn = M.nodes.filter((n) => n.zoneId === z);
    if (!zn.length) continue;
    const band = el('section.lmap__band', { class: `lmap__band lmap__band--${z}` }, el('div.lmap__band-head', el('span.label', ZONE[z].label), el('span.small.muted', ZONE[z].sub)));
    for (const n of zn) { const c = cards.get(n.id); c.style.width = ''; band.append(c); }
    layer.append(band);
  }
  for (const n of M.nodes.filter((x) => x.pseudo)) cards.get(n.id)?.remove();
  if (legend) {
    clear(legend);
    const routes = M.links.map((l) => ({ l }));
    renderLegend(legend, routes, () => {}, false);
    legend.prepend(el('p.small.muted', 'Schmaler Bildschirm: Verbindungen als Liste, Karte gestapelt.'));
  }
}

function renderLegend(legend, routes, setHover, scrolled) {
  clear(legend);
  if (!routes.length) return;
  const ol = el('ol.map-legend');
  for (const r of routes) {
    const li = el('li.map-legend__item', { 'data-link': String(r.l.no) },
      el('span.map-legend__no', String(r.l.no)), mark(r.l.state),
      el('span.map-legend__text', el('strong', `${r.l.from} → ${r.l.to === '__public' ? 'Öffentlichkeit' : r.l.to}`), ` · ${r.l.label ?? LINK_LABEL[r.l.kind] ?? r.l.kind ?? ''}`),
      el('span.map-legend__kind', LINK_LABEL[r.l.kind] ?? r.l.kind ?? ''));
    li.addEventListener('pointerenter', () => setHover(r));
    li.addEventListener('pointerleave', () => setHover(null));
    ol.append(li);
  }
  legend.append(ol);
  if (scrolled) legend.append(el('p.small.muted', 'Die Karte ist breiter als das Fenster – seitlich scrollen.'));
}

function sizeWH(s) { return { width: s?.w ?? 240, height: s?.h ?? 120 }; }
/** Mitte des längsten Segments einer Kante (dort sitzt der Nummern-Marker frei). */
function midOf(sections, b) {
  const pts = [];
  for (const s of sections) pts.push(s.startPoint, ...(s.bendPoints ?? []), s.endPoint);
  let best = null;
  for (let i = 1; i < pts.length; i++) {
    const len = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    if (!best || len > best.len) best = { len, x: (pts[i].x + pts[i - 1].x) / 2, y: (pts[i].y + pts[i - 1].y) / 2 };
  }
  return { x: b.ox + (best?.x ?? 0), y: b.oy + (best?.y ?? 0) };
}
function linkTip(r, data) {
  const l = r.l; const svc = data.services.find((s) => s.id === l.service);
  return { title: `${l.no} · ${l.from} → ${l.to === '__public' ? 'Öffentlichkeit' : l.to}`, rows: [['Art', LINK_LABEL[l.kind] ?? l.kind], ['Was', l.label ?? null], ['Zustand', STATES[norm(l.state)].word], ['Dienst', svc ? svc.label : (l.service ?? null)]] };
}
function rank(s) { return { crit: 4, warn: 3, unknown: 2, ok: 1, off: 0 }[norm(s)]; }
function worstState(list) { return list.reduce((w, s) => (rank(s) > rank(w) ? norm(s) : w), 'ok'); }
export { badge, SEVERITY_STATE, fmt };
