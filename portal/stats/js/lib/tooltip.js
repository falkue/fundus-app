/**
 * Eine Tooltip-Schicht für alle Diagramme (D3, ECharts, HTML): Circle-Karte, Inhalt nur
 * per textContent, Position mit Floating UI (virtuelles Element am Zeiger bzw. an der
 * Markierung; flip/shift halten den Tip im Fenster). Esc schliesst.
 *
 *   show(event|{clientX, clientY}|Element, {title, rows:[[label, value]], note})
 *   move(event) · hide() · bind(d3Selection, content(d)) · bindEl(htmlElement, content())
 */
import { computePosition, offset, flip, shift } from '@floating-ui/dom';
import { el, clear } from './dom.js';

let tip;
let anchor = { x: 0, y: 0, el: null };
let raf = 0;
function node() { return tip ?? (tip = document.getElementById('tooltip')); }

function virtual() {
  return {
    getBoundingClientRect() {
      if (anchor.el) return anchor.el.getBoundingClientRect();
      return { x: anchor.x, y: anchor.y, left: anchor.x, top: anchor.y, right: anchor.x, bottom: anchor.y, width: 0, height: 0 };
    },
  };
}
function position() {
  raf = 0;
  const t = node();
  if (!t || t.dataset.show !== 'true') return;
  computePosition(virtual(), t, { placement: 'top', strategy: 'fixed', middleware: [offset(10), flip(), shift({ padding: 8 })] })
    .then(({ x, y }) => { t.style.left = `${x}px`; t.style.top = `${y}px`; });
}
function setAnchor(src) {
  if (src instanceof Element) anchor = { el: src, x: 0, y: 0 };
  else if (src && typeof src.clientX === 'number') anchor = { el: null, x: src.clientX, y: src.clientY };
}

export function show(src, content) {
  const t = node();
  if (!t) return;
  clear(t);
  if (typeof content === 'string') t.append(el('div.tooltip__title', content));
  else if (content) {
    if (content.title) t.append(el('div.tooltip__title', content.title));
    for (const [k, v] of content.rows ?? []) {
      if (v === undefined) continue;
      t.append(el('div.tooltip__row', el('span', k), el('span', v === null ? '–' : (v instanceof Node ? v : String(v)))));
    }
    if (content.note) t.append(el('div.tooltip__note', content.note));
  }
  t.dataset.show = 'true';
  setAnchor(src);
  position();
}
export function move(src) {
  setAnchor(src);
  if (!raf) raf = requestAnimationFrame(position);
}
export function hide() { const t = node(); if (t) t.dataset.show = 'false'; }

/** D3-Selektion: Zeiger- und Fokus-Tooltips. */
export function bind(selection, content) {
  selection
    .on('pointerenter.tip', (e, d) => show(e, content(d)))
    .on('pointermove.tip', (e) => move(e))
    .on('pointerleave.tip', hide)
    .on('focus.tip', (e, d) => show(e.target, content(d)))
    .on('blur.tip', hide);
  return selection;
}
/** HTML-Element: content() wird bei jedem Zeigen ausgewertet. */
export function bindEl(element, content) {
  element.addEventListener('pointerenter', (e) => show(e, content()));
  element.addEventListener('pointermove', (e) => move(e));
  element.addEventListener('pointerleave', hide);
  element.addEventListener('focus', () => show(element, content()));
  element.addEventListener('blur', hide);
  return element;
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
