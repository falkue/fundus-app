/**
 * Detail-Schublade zu einem Knoten: Hardware, OS/EOL, Vitalwerte mit Sparkline,
 * Volumen, Schächte, Dienste, Patches.
 */
import { el, clear, dl, statePill, stateLabel, mark, append, headline } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { norm, eolState, fillState } from '../lib/state.js';
import { nodeById, servicesOf, seriesFor, sparkValues } from '../lib/derive.js';
import { sparkline } from '../charts/sparkline.js';
import { volumeBars, bayStrip } from './hardware.js';

export function openDrawer(data, nodeId, ctx) {
  const n = nodeById(data, nodeId);
  const drawer = document.getElementById('drawer');
  const body = clear(document.getElementById('drawer-body'));
  const title = document.getElementById('drawer-title');
  if (!n) { title.textContent = 'Unbekannter Knoten'; body.append(el('p.muted', `Kein Knoten mit der ID „${nodeId}“.`)); show(); return; }
  clear(title); append(title, [n.label ?? n.id, n.alias ? el('em', { style: { color: 'var(--color-headline-accent)', fontStyle: 'italic' } }, ` ${n.alias}`) : null]);

  body.append(el('div.row', statePill(n.state), el('span.secondary', n.stateReason ?? (norm(n.state) === 'unknown' ? 'keine Angabe' : ''))));
  body.append(section('Rolle & Adressen', dl([
    ['Rolle', n.role ?? null], ['Zone', zoneLabel(n)],
    ['LAN', n.addr?.lan ?? null], ['Tailnet', n.addr?.tailnet ?? null], ['Öffentlich', n.addr?.public ?? null],
  ])));
  const hw = n.hardware ?? {};
  body.append(section('Hardware', dl([
    ['Modell', hw.model ?? null], ['CPU', hw.cpu ?? null], ['RAM', fmt.bytes(hw.ramBytes)], ['GPU', hw.gpu ?? null],
  ])));
  const os = n.os ?? {};
  const eolS = eolState(os.eolDays);
  body.append(section('Betriebssystem', dl([
    ['System', os.name ? `${os.name} ${os.version ?? ''}`.trim() : null], ['Kernel', os.kernel ?? null],
    ['Supportende', os.eol ? el('span.row', fmt.date(os.eol), stateLabel(eolS, eolS === 'crit' ? 'abgelaufen' : eolS === 'warn' ? 'bald' : 'gestützt')) : el('span.row', mark('unknown'), 'unbekannt')],
    ['Abstand', fmt.days(os.eolDays)],
  ])));
  // Vitalwerte mit Sparkline aus history
  const v = n.vitals;
  const vit = el('div');
  const rows = [
    ['Uptime', fmt.duration(v?.uptimeS), null],
    ['Last (1 min)', fmt.load(v?.load1), seriesFor(data, n.id, 'load1')[0]],
    ['RAM', fmt.pct(v?.memPct), seriesFor(data, n.id, 'memPct')[0]],
    ['Temperatur', fmt.temp(v?.tempC), seriesFor(data, n.id, 'tempC')[0]],
    ['Leistung', v?.powerW !== null && v?.powerW !== undefined ? `${fmt.int(v.powerW)} W` : fmt.DASH, null],
  ];
  for (const [label, value, s] of rows) {
    const row = el('div.vital', el('span.vital__label', label), el('span.vital__value', value));
    if (s) { const sp = el('span.vital__spark'); sparkline(sp, sparkValues(s), { width: 90, height: 26 }); row.append(sp); }
    vit.append(row);
  }
  if (!v) vit.append(el('p.small.muted', 'Keine Vitalwerte erhoben.'));
  body.append(section('Vitalwerte', vit));
  if (n.volumes?.length) body.append(section('Volumen', volumeBars(n.volumes)));
  if (n.disks?.length) body.append(section('Schächte', bayStrip(n.disks)));
  if (n.snapshots) body.append(section('Snapshots', dl([
    ['Aktiv', n.snapshots.enabled === true ? 'ja' : n.snapshots.enabled === false ? 'nein' : null],
    ['Anzahl', fmt.int(n.snapshots.count)], ['Letzter', n.snapshots.latest ? fmt.ago(n.snapshots.latest) : null], ['Zeitplan', n.snapshots.schedule ?? null],
  ])));
  if (n.patches) body.append(section('Patches', dl([
    ['Ausstehend', fmt.int(n.patches.pending)], ['Sicherheit', fmt.int(n.patches.security)],
    ['Neustart nötig', n.patches.rebootRequired === true ? 'ja' : n.patches.rebootRequired === false ? 'nein' : null],
    ['Letztes Upgrade', fmt.date(n.patches.lastUpgrade)], ['Quelle', n.patches.source ?? null],
  ])));
  const svcs = servicesOf(data, n.id);
  const list = el('div.row');
  for (const s of svcs) list.append(el('span.chip', { class: `chip${norm(s.state) === 'off' ? ' chip--off' : ''}`, title: s.detail ?? '' }, mark(s.state), s.label ?? s.id));
  body.append(section(`Dienste (${svcs.length})`, svcs.length ? list : el('p.small.muted', 'Keine Dienste erfasst.'),
    el('button.btn.btn--small', { type: 'button', on: { click: () => { closeDrawer(); ctx.navigate('#dienste'); } } }, 'Zur Dienste-Ansicht')));
  show();
  function show() {
    drawer.setAttribute('aria-hidden', 'false');
    document.getElementById('drawer-backdrop').dataset.open = 'true';
    document.getElementById('drawer-close').focus();
  }
}
export function closeDrawer() {
  const d = document.getElementById('drawer');
  if (d.getAttribute('aria-hidden') === 'false') { d.setAttribute('aria-hidden', 'true'); document.getElementById('drawer-backdrop').dataset.open = 'false'; }
}
function section(title, ...content) { return el('div.drawer__section', headline('h3', title), ...content); }
function zoneLabel(n) {
  const z = { internet: 'Internet', tailnet: 'Tailnet', lan: 'Heimnetz' };
  const parts = [z[n.zone] ?? n.zone, ...(n.alsoIn ?? []).map((a) => z[a] ?? a)].filter(Boolean);
  return parts.length ? parts.join(' + ') : null;
}
