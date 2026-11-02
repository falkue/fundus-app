/**
 * SVG-Text an eine Breite anpassen: misst mit getComputedTextLength() und kürzt mit „…“.
 * Der volle Text bleibt als <title> (Tooltip) und data-full erhalten.
 *
 *   fitText(selection, maxWidth)            – d3-Selektion von <text>-Elementen
 *   fitText(selection, (d, i, node) => w)   – Breite je Element
 *   fitOne(textNode, maxWidth)              – einzelnes DOM-Element, gibt true zurück, wenn gekürzt
 *   wrapText(textNode, maxWidth, {maxLines, lineHeight}) – Umbruch in <tspan>-Zeilen, letzte Zeile mit „…“
 *
 * Setzt data-truncated="true", wenn gekürzt wurde. Elemente müssen im DOM hängen (Messung).
 */
const ELLIPSIS = '…';
const SVG_NS = 'http://www.w3.org/2000/svg';

export function fitOne(node, maxWidth, { min = 2 } = {}) {
  if (!node || typeof node.getComputedTextLength !== 'function') return false;
  const full = node.dataset.full ?? node.textContent ?? '';
  node.dataset.full = full;
  node.textContent = full;
  if (!Number.isFinite(maxWidth) || maxWidth <= 0) { node.textContent = ''; node.dataset.truncated = 'true'; return true; }
  let len;
  try { len = node.getComputedTextLength(); } catch { return false; }
  if (len <= maxWidth) { delete node.dataset.truncated; removeTitle(node); return false; }
  let lo = 0, hi = full.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    node.textContent = full.slice(0, mid).trimEnd() + ELLIPSIS;
    if (node.getComputedTextLength() <= maxWidth) lo = mid; else hi = mid - 1;
  }
  node.textContent = lo < min ? '' : full.slice(0, lo).trimEnd() + ELLIPSIS;
  node.dataset.truncated = 'true';
  setTitle(node, full);
  return true;
}

export function fitText(selection, maxWidth, opts) {
  selection.each(function (d, i) {
    const w = typeof maxWidth === 'function' ? maxWidth(d, i, this) : maxWidth;
    fitOne(this, w, opts);
  });
  return selection;
}

/**
 * Umbruch an Wortgrenzen in bis zu maxLines Zeilen (tspans). Gibt die Zeilenzahl zurück.
 * Der Text bleibt vollständig als <title>, wenn gekürzt wurde.
 */
export function wrapText(node, maxWidth, { maxLines = 2, lineHeight = 1.25 } = {}) {
  if (!node || typeof node.getComputedTextLength !== 'function') return 0;
  const full = node.dataset.full ?? node.textContent ?? '';
  node.dataset.full = full;
  const x = node.getAttribute('x') ?? 0;
  const words = full.split(/\s+/).filter(Boolean);
  node.textContent = '';
  const lines = [];
  let line = [];
  const probe = document.createElementNS(SVG_NS, 'tspan');
  node.append(probe);
  const width = (txt) => { probe.textContent = txt; return probe.getComputedTextLength(); };
  for (const w of words) {
    const candidate = [...line, w].join(' ');
    if (line.length && width(candidate) > maxWidth) { lines.push(line.join(' ')); line = [w]; }
    else line = [...line, w];
    if (lines.length === maxLines) break;
  }
  let truncated = false;
  if (lines.length < maxLines) { if (line.length) lines.push(line.join(' ')); }
  else truncated = true;
  // letzte Zeile ggf. auf Breite kürzen
  if (lines.length) {
    const lastIdx = lines.length - 1;
    const rest = truncated ? [...lines[lastIdx].split(' '), ...line, ...words.slice(words.indexOf(line[line.length - 1]) + 1)] : [];
    let last = truncated ? [lines[lastIdx], ...rest.slice(lines[lastIdx].split(' ').length)].join(' ') : lines[lastIdx];
    if (width(last) > maxWidth) {
      truncated = true;
      let lo = 0, hi = last.length;
      while (lo < hi) { const mid = Math.ceil((lo + hi) / 2); if (width(last.slice(0, mid).trimEnd() + ELLIPSIS) <= maxWidth) lo = mid; else hi = mid - 1; }
      last = last.slice(0, lo).trimEnd() + ELLIPSIS;
    }
    lines[lastIdx] = last;
  }
  probe.remove();
  lines.forEach((t, i) => {
    const ts = document.createElementNS(SVG_NS, 'tspan');
    ts.setAttribute('x', x);
    ts.setAttribute('dy', i === 0 ? '0' : `${lineHeight}em`);
    ts.textContent = t;
    node.append(ts);
  });
  if (truncated) { node.dataset.truncated = 'true'; setTitle(node, full); } else { delete node.dataset.truncated; }
  return lines.length;
}

/** Breite eines Textes in Nutzer-Einheiten (0, wenn nicht messbar). */
export function measure(node) {
  try { return node.getComputedTextLength(); } catch { return 0; }
}

function setTitle(node, text) {
  let t = node.querySelector(':scope > title');
  if (!t) { t = document.createElementNS(SVG_NS, 'title'); node.append(t); }
  t.textContent = text;
}
function removeTitle(node) { node.querySelector(':scope > title')?.remove(); }

/** Rechteck-Schnitt mit Rand. Rechtecke als {x, y, w, h}. */
export function intersects(a, b, margin = 0) {
  return !(a.x + a.w + margin <= b.x || b.x + b.w + margin <= a.x || a.y + a.h + margin <= b.y || b.y + b.h + margin <= a.y);
}
