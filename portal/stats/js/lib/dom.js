/**
 * DOM-Helfer ohne innerHTML. Text nur über textContent.
 */
import { STATES, norm } from './state.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * el('div.card.flat#id', {attr: ..., on: {click: fn}, data: {...}}, ...children)
 * Kinder: Element | string (→ Textknoten) | null/false (ignoriert) | Array.
 */
export function el(spec, attrs, ...children) {
  if (attrs && (attrs instanceof Node || typeof attrs === 'string' || Array.isArray(attrs))) {
    children.unshift(attrs); attrs = null;
  }
  const [tagPart, ...rest] = spec.split(/(?=[.#])/);
  const node = document.createElement(tagPart || 'div');
  for (const r of rest) {
    if (r.startsWith('.')) node.classList.add(r.slice(1));
    else if (r.startsWith('#')) node.id = r.slice(1);
  }
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

export function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  applyAttrs(node, attrs);
  append(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'on') { for (const [evt, fn] of Object.entries(v)) node.addEventListener(evt, fn); }
    else if (k === 'data') { for (const [dk, dv] of Object.entries(v)) if (dv !== null && dv !== undefined) node.dataset[dk] = dv; }
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k === 'class') node.setAttribute('class', v);
    else if (k === 'text') node.textContent = v;
    else if (k === 'href' && node.namespaceURI === SVG_NS) node.setAttribute('href', v);
    else node.setAttribute(k, v === true ? '' : v);
  }
}

export function append(parent, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    parent.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return parent;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

/** Zustandsmarke als <svg class="mark mark--warn"><use href="#sym-warn"/></svg> */
export function mark(state, extraClass = '') {
  const s = norm(state);
  const svg = svgEl('svg', { class: `mark mark--${s} ${extraClass}`.trim(), viewBox: '0 0 12 12', 'aria-hidden': 'true', focusable: 'false' });
  svg.append(svgEl('use', { href: `#${STATES[s].symbol}` }));
  return svg;
}
/** Marke + Wort, z. B. „▲ Achtung“. */
export function stateLabel(state, wordOverride) {
  const s = norm(state);
  return el('span.state', { class: `state state--${s}` }, mark(s), el('span.state__word', wordOverride ?? STATES[s].word));
}
/** Getönte Pill mit Marke und Wort. */
export function statePill(state, wordOverride) {
  const s = norm(state);
  return el('span.badge', { class: `badge badge--${s}` }, mark(s), wordOverride ?? STATES[s].word);
}
export function badge(text, tone) { return el('span.badge', { class: `badge${tone ? ` badge--${tone}` : ''}` }, text); }

/** Lucide-Icon aus dem Sprite vendor/lucide/icons.svg (IDs lu-<name>). */
export function lu(name, cls = 'lu') {
  const svg = svgEl('svg', { class: cls, viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false' });
  svg.append(svgEl('use', { href: `#lu-${name}` }));
  return svg;
}
/** Icon aus den <symbol>-Definitionen in index.html. */
export function icon(id, cls = 'nav__icon') {
  const svg = svgEl('svg', { class: cls, viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false' });
  svg.append(svgEl('use', { href: `#${id}` }));
  return svg;
}

/**
 * Serif-Überschrift mit genau EINEM Akzentwort (erstes Vorkommen von accent in <em>).
 * headline('h1', 'Zwei Dienste brauchen Aufmerksamkeit', 'Aufmerksamkeit')
 */
export function headline(level, text, accent) {
  const h = document.createElement(level);
  const i = accent ? text.indexOf(accent) : -1;
  if (i < 0) { h.textContent = text; return h; }
  append(h, [text.slice(0, i), el('em', accent), text.slice(i + accent.length)]);
  return h;
}

/** Seitenkopf: Überschrift + Einordnungssatz + rechte Werkzeuge. */
export function pageHead({ title, accent, lede, tools }) {
  const head = el('header.page-head');
  const row = el('div.page-head__row', headline('h1', title, accent), tools ? el('div.row', tools) : null);
  head.append(row);
  if (lede) head.append(el('p.lede', lede));
  return head;
}

/** Karte mit Kopf (h3 mit Akzent, Untertitel) und Body. */
export function card({ title, accent, sub, flat, tools, cls }, ...body) {
  const c = el('section.card', { class: `card${flat ? ' card--flat' : ''}${cls ? ` ${cls}` : ''}` });
  if (title) {
    const head = el('div.card__head', headline('h3', title, accent), tools ? el('div.chart__tools', tools) : null);
    c.append(head);
  }
  if (sub) c.append(el('p.card__sub', sub));
  append(c, body);
  return c;
}

/** Segment-Control (radiogroup). onChange(value). Gibt {el, set(value)} zurück. */
export function segmented(options, value, onChange, label = 'Auswahl') {
  const group = el('div.seg', { role: 'radiogroup', 'aria-label': label });
  const buttons = options.map((o) => {
    const b = el('button.seg__btn', { type: 'button', role: 'radio', 'aria-checked': String(o.value === value) },
      o.label, o.count !== undefined ? el('span.count', String(o.count)) : null);
    b.addEventListener('click', () => { set(o.value); onChange(o.value); });
    return b;
  });
  function set(v) {
    value = v;
    buttons.forEach((b, i) => b.setAttribute('aria-checked', String(options[i].value === v)));
  }
  append(group, buttons);
  return { el: group, set, get value() { return value; } };
}

/**
 * Diagramm-Rahmen mit Grafik/Tabelle-Umschalter.
 * const f = chartFrame({title, accent, sub, tools}); parent.append(f.el);
 * f.plot → Container fürs SVG; f.table(columns, rows) füllt die Tabellenansicht.
 * columns: [{key, label, num?, fmt?}], rows: Objekte.
 */
export function chartFrame({ title, accent, sub, tools, legend, hint }) {
  const root = el('div.chart', { 'data-mode': 'chart' });
  const toggle = segmented([{ value: 'chart', label: 'Grafik' }, { value: 'table', label: 'Tabelle' }], 'chart',
    (v) => { root.dataset.mode = v; }, 'Darstellung');
  const head = el('div.chart__head', title ? headline('h3', title, accent) : null,
    el('div.chart__tools', tools ?? null, toggle.el));
  root.append(head);
  if (sub) root.append(el('p.card__sub', sub));
  const plot = el('div.chart__plot');
  const legendEl = el('div.chart__legend');
  const hintEl = el('div.hint', { 'aria-live': 'polite' }, hint ?? '');
  const tableWrap = el('div.chart__table');
  root.append(plot, legendEl, hintEl, tableWrap);
  if (legend) legendEl.append(legend);
  return {
    el: root, plot, legend: legendEl, hint: hintEl,
    setHint(t) { hintEl.textContent = t ?? ''; },
    table(columns, rows) { clear(tableWrap); tableWrap.append(dataTable(columns, rows)); },
  };
}

/** Datentabelle (mobil → Karten per data-label). */
export function dataTable(columns, rows, opts = {}) {
  const table = el('table.table', { class: `table${opts.compact ? ' table--compact' : ''}` });
  const thead = el('thead', el('tr', columns.map((c) => el('th', { class: c.num ? 'num' : null, scope: 'col' }, c.label))));
  const tbody = el('tbody');
  if (!rows.length) {
    tbody.append(el('tr', el('td', { colspan: String(columns.length), 'data-label': '' }, el('span.muted', opts.empty ?? 'Keine Daten'))));
  }
  for (const r of rows) {
    const tr = el('tr', { class: opts.rowClass ? opts.rowClass(r) : null });
    if (opts.onRow) { tr.classList.add('clickable'); tr.tabIndex = 0; tr.addEventListener('click', () => opts.onRow(r)); tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') opts.onRow(r); }); }
    for (const c of columns) {
      const v = c.render ? c.render(r) : (c.fmt ? c.fmt(r[c.key], r) : r[c.key]);
      tr.append(el('td', { class: c.num ? 'num' : null, 'data-label': c.label }, v === null || v === undefined ? '–' : v));
    }
    tbody.append(tr);
  }
  const wrap = el('div.table-wrap', append(table, [thead, tbody]));
  return wrap;
}

/** Legende aus Einträgen [{label, swatch:'css color var', line?, dashed?, hatch?, mark?:state}]. */
export function legend(items) {
  const l = el('div.legend');
  for (const it of items) {
    let key;
    if (it.mark) key = mark(it.mark);
    else if (it.hatch) key = el('span.legend__swatch.legend__hatch');
    else if (it.line) key = el('span.legend__line', { class: `legend__line${it.dashed ? ' legend__line--dashed' : ''}`, style: { color: it.swatch } });
    else key = el('span.legend__swatch', { style: { background: it.swatch } });
    l.append(el('span.legend__item', key, it.label));
  }
  return l;
}

export function emptyNote(text = 'Keine Daten') {
  return el('div.empty', mark('unknown'), text);
}

/** Kleine Definitionsliste. items: [[label, value]] – value darf Node sein. */
export function dl(items) {
  const d = el('dl.dl');
  for (const [k, v] of items) {
    if (v === undefined) continue;
    d.append(el('div', el('dt', k), el('dd', v === null || v === '' ? '–' : v)));
  }
  return d;
}

export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
export const DUR = () => (reducedMotion() ? 0 : 220);

/**
 * Bestätigung im Circle-Stil (kein window.confirm): <dialog> mit Titel, Text, zwei Knöpfen.
 * Gibt ein Promise<boolean> zurück.
 */
export function confirmDialog({ title, text, confirmLabel = 'Ja', cancelLabel = 'Abbrechen' }) {
  return new Promise((resolve) => {
    const dlg = el('dialog.dialog');
    const h = headline('h3', title ?? 'Sicher?');
    const yes = el('button.btn.btn--primary', { type: 'button' }, confirmLabel);
    const no = el('button.btn', { type: 'button' }, cancelLabel);
    dlg.append(el('div.dialog__body', h, text ? el('p.secondary', text) : null, el('div.row.dialog__actions', no, yes)));
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    yes.addEventListener('click', () => done(true));
    no.addEventListener('click', () => done(false));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(false); });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) done(false); });
    document.body.append(dlg);
    if (typeof dlg.showModal === 'function') dlg.showModal(); else { dlg.setAttribute('open', ''); }
    yes.focus();
  });
}
