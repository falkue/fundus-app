/**
 * Bullet-Chart als HTML (robust in WebKit, keine skalierende SVG-Breite):
 * Ist als Balken, Soll als Strich, Warnzone als Hintergrund. null → Schraffur.
 *
 * bulletRow(container, {label, sub, value, target, max, format, state, note,
 *                       valueText, targetLabel, invert})
 *   invert: true  → kleiner ist schlechter (Restlaufzeit): Warnzone links vom Soll
 *   invert: false → grösser ist schlechter (Alter, Verbrauch): Warnzone rechts vom Soll
 */
import { el, mark } from '../lib/dom.js';
import { norm, STATES } from '../lib/state.js';
import * as tip from '../lib/tooltip.js';

export function bulletRow(container, o) {
  const { label, sub, value, target, max, format = (v) => String(v), state = 'unknown', note, valueText, targetLabel, invert = false } = o;
  const hasValue = typeof value === 'number' && Number.isFinite(value);
  const hasTarget = typeof target === 'number' && Number.isFinite(target);
  const hi = Math.max(max ?? 0, hasTarget ? target * (invert ? 3 : 1.25) : 0, hasValue ? value * 1.1 : 0, 1e-9);
  const pct = (v) => `${Math.max(0, Math.min(100, (100 * v) / hi))}%`;

  const row = el('div.bullet-row');
  const labelEl = el('div.bullet-row__label', mark(state), el('span', el('span.bullet-row__title', label), sub ? el('small', sub) : null));
  const plot = el('div.bullet', { role: 'img', 'aria-label': `${label}: ${valueText ?? (hasValue ? format(value) : 'keine Daten')}` });
  plot.append(el('div.bullet__track'));
  if (hasTarget) {
    if (invert) {
      plot.append(el('div.bullet__band', { style: { left: '0', width: pct(target) } }));
      plot.append(el('div.bullet__band.bullet__band--crit', { style: { left: '0', width: pct(target / 2) } }));
    } else {
      plot.append(el('div.bullet__band', { style: { left: pct(target), width: `calc(${pct(Math.min(hi, target * 2))} - ${pct(target)})` } }));
      if (target * 2 < hi) plot.append(el('div.bullet__band.bullet__band--crit', { style: { left: pct(target * 2), right: '0' } }));
    }
  }
  if (!hasValue) plot.append(el('div.bullet__measure.bullet__measure--unknown'));
  else {
    const s = norm(state);
    plot.append(el('div.bullet__measure', { class: `bullet__measure${s === 'warn' || s === 'crit' ? ` bullet__measure--${s}` : ''}`, style: { width: pct(value) } }));
  }
  if (hasTarget) plot.append(el('div.bullet__target', { style: { left: pct(target) }, title: targetLabel ?? `Soll ${format(target)}` }));
  const num = el('div.bullet-row__num', valueText ?? (!hasValue ? 'keine Daten' : hasTarget ? `${format(value)} · Soll ${format(target)}` : format(value)));
  row.append(labelEl, plot, num);
  container.append(row);
  plot.addEventListener('pointerenter', (e) => tip.show(e, { title: label, rows: [['Ist', hasValue ? format(value) : null], [targetLabel ? targetLabel.split(' ')[0] : 'Soll', hasTarget ? format(target) : null], ['Zustand', STATES[norm(state)].word], ['Hinweis', note ?? null]] }));
  plot.addEventListener('pointermove', (e) => tip.move(e));
  plot.addEventListener('pointerleave', tip.hide);
  return row;
}
