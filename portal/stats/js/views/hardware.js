/**
 * Hardware-Bausteine: Schachtansicht (Laufwerke) und Volumen-Balken. HTML, keine D3.
 */
import { el, mark } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { norm, fillState, STATES } from '../lib/state.js';

export function bayStrip(disks) {
  const wrap = el('div.bays');
  for (const d of [...disks].sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0))) {
    const empty = !d.model && !d.sizeBytes;
    const smart = empty ? null : norm(d.smart);
    const bay = el('div.bay', { class: `bay${empty ? ' bay--empty' : ''}${d.type === 'nvme' ? ' bay--nvme' : ''}`,
      title: empty ? `Schacht ${d.slot}: leer` : `${d.model ?? 'Laufwerk'} · ${fmt.bytes(d.sizeBytes)} · SMART ${HEALTH[smart]} · ${fmt.temp(d.tempC)}${typeof d.powerOnHours === 'number' ? ` · ${fmt.int(d.powerOnHours)} h` : ''}` });
    bay.append(el('span.bay__slot', d.slotLabel ?? (d.type === 'nvme' ? `M.2 ${d.slot}` : `Schacht ${d.slot}`)));
    const disk = el('div.bay__disk');
    if (!empty) disk.append(mark(smart, 'mark--lg'));
    bay.append(disk);
    bay.append(el('span', empty ? 'leer' : fmt.bytes(d.sizeBytes, 0)));
    if (!empty && d.model) bay.append(el('span.bay__model', { title: d.model }, diskFamily(d.model)));
    bay.append(el('span.bay__temp', empty ? '' : fmt.temp(d.tempC)));
    wrap.append(bay);
  }
  return wrap;
}

const HEALTH = { ok: 'gesund', warn: 'Achtung', crit: 'Kritisch', off: 'aus', unknown: 'Zustand unbekannt' };
export { HEALTH };
export function volumeBars(volumes) {
  const wrap = el('div');
  for (const v of volumes) {
    const pct = typeof v.usedBytes === 'number' && typeof v.capBytes === 'number' && v.capBytes > 0 ? (100 * v.usedBytes) / v.capBytes : null;
    const level = fillState(pct);
    const row = el('div.volbar');
    row.append(el('span.volbar__label', v.label ?? v.id, el('small', [v.raid, v.health ? `${HEALTH[norm(v.health)]}` : null, v.resync ? `Resync ${v.resync}` : null].filter(Boolean).join(' · '))));
    const track = el('div.volbar__track', { role: 'img', 'aria-label': pct === null ? 'Belegung unbekannt' : `${fmt.pct(pct)} belegt` });
    if (pct === null) track.classList.add('hatched');
    else { track.append(el('div.volbar__fill', { style: { width: `${Math.min(100, pct)}%` }, 'data-level': level === 'ok' ? null : level }), el('div.volbar__mark', { title: '80 %' })); }
    row.append(track);
    row.append(el('span.volbar__num', pct === null ? 'keine Daten' : `${fmt.bytes(v.usedBytes)} / ${fmt.bytes(v.capBytes)} · ${fmt.pct(pct)}`));
    wrap.append(row);
  }
  return wrap;
}

/** Kurzname aus der Modellnummer (WD40EFZZ → „WD Red Plus“, ST8000VN004 → „IronWolf“), sonst die Nummer selbst. */
function diskFamily(m) {
  const x = String(m).toUpperCase();
  if (/^WD\d+EF(ZZ|ZX|PX|AX)/.test(x)) return 'WD Red Plus';
  if (/^WD\d+EFRX/.test(x)) return 'WD Red';
  if (/^WD\d+EFBX/.test(x)) return 'WD Red Pro';
  if (/^ST\d+VN/.test(x)) return 'IronWolf';
  if (/^ST\d+NE/.test(x)) return 'IronWolf Pro';
  return m.split(/[-\s]/)[0];
}
