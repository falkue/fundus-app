/**
 * Verlaufs-Bausteine (N1–N6) – HTML/D3 im Circle-Stil, alle Felder optional („noch kein Verlauf“).
 *
 *   heartbeat(container, collector)                      N1 Kollektor-Herzschlag (letzte 28 Läufe als Pillen)
 *   statusHistory(container, services, {days})           N2 Dienst-Verfügbarkeit 30 Tage (Status-History + %)
 *   daysToFull(container, volumesWithNode)               N3 Tage bis voll (Bar-Gauge je Volumen)
 *   runGantt(container, runs, jobs, {days})              N4 Sicherungs-Laufplan 14 Tage (Gantt, dichte Jobs als Tick-Spur)
 *   exposureChanges(container, ports, portsClosed)       N5 Expositions-Änderungen
 *   serviceHoneycomb(container, services, nodes)         N6 Dienst-Waben je Knoten
 */
import { el, mark, lu, badge, emptyNote } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import * as tip from '../lib/tooltip.js';
import { norm, STATES, worst } from '../lib/state.js';
import { responsive, svgPx } from '../lib/chart.js';
import { fitOne } from '../lib/svgtext.js';
import { isFresh, observationStart } from '../lib/derive.js';

const d3 = globalThis.d3;
const NEW_H = 6;

// ---------------------------------------------------------------- N1 Herzschlag
export function heartbeat(container, c, { max = 28 } = {}) {
  const runs = Array.isArray(c.runs) ? c.runs.slice(-max) : [];
  const row = el('div.hb');
  row.append(el('span.hb__label', { title: c.label ?? c.id }, mark(c.state ?? 'unknown'), el('span', c.id)));
  const track = el('div.hb__track', { role: 'img', 'aria-label': runs.length ? `${c.id}: ${runs.filter((r) => r.ok).length} von ${runs.length} Läufen ok` : `${c.id}: noch kein Verlauf` });
  for (let i = 0; i < max; i++) {
    const r = runs[runs.length - max + i];
    const p = el('span.hb__pill', { class: `hb__pill hb__pill--${!r ? 'none' : r.ok === false ? 'fail' : r.errorCount ? 'partial' : 'ok'}` });
    if (r) tip.bindEl(p, () => ({ title: `${c.id} · ${fmt.dateTime(r.at)}`, rows: [['Ergebnis', r.ok === false ? 'fehlgeschlagen' : r.errorCount ? `ok mit ${r.errorCount} Teilfehler(n)` : 'ok'], ['Dauer', typeof r.tookS === 'number' ? `${fmt.num1(r.tookS)} s` : null]] }));
    track.append(p);
  }
  row.append(track);
  row.append(el('span.hb__meta', runs.length ? `${runs.filter((r) => r.ok !== false).length}/${runs.length} ok · letzter ${fmt.ago(runs[runs.length - 1].at)}` : 'noch kein Verlauf'));
  container.append(row);
  return row;
}

// ---------------------------------------------------------------- N2 Status-History
export function statusHistory(container, services, { days = 30, now = new Date(), compact = false, data = null } = {}) {
  const end = d3.timeDay.floor(now);
  const dates = d3.timeDays(d3.timeDay.offset(end, -(days - 1)), d3.timeDay.offset(end, 1));
  const key = d3.timeFormat('%Y-%m-%d');
  const list = services.filter((s) => s.intended !== 'off');
  const withData = list.filter((s) => Array.isArray(s.daily) && s.daily.length);
  const wrap = el('div.sh');
  const head = el('div.sh__row.sh__row--head', el('span.sh__label.label', 'Dienst'), el('span.sh__cells'), el('span.sh__pct.label', 'verfügbar'));
  const cellsHead = head.querySelector('.sh__cells');
  dates.forEach((d, i) => {
    const last = i === dates.length - 1;
    const c = el(last ? 'span.sh__dlabel.sh__dlabel--end' : 'span.sh__dlabel');
    // Monatsanfang nur, wenn er nicht mit dem ersten/letzten Datum kollidiert (je ≥ 5 Tage Abstand)
    if (i === 0 || last || (d.getDate() === 1 && i >= 5 && i <= dates.length - 6)) c.textContent = fmt.dateShort(d);
    cellsHead.append(c);
  });
  wrap.append(head);
  const rowFor = (label, dailyByDate, sublabel, stateNow, extra) => {
    const row = el('div.sh__row');
    const lab = el('span.sh__label', mark(stateNow), el('span.sh__name', { title: label }, label));
    if (sublabel) lab.append(el('small', sublabel));
    row.append(lab);
    const cells = el('span.sh__cells');
    let okSum = 0, n = 0;
    for (const d of dates) {
      const v = dailyByDate.get(key(d));
      const c = el('span.sh__cell', { class: `sh__cell${v ? ` sh__cell--${norm(v.worst)}` : ' sh__cell--gap'}` });
      if (v && typeof v.okRatio === 'number') { c.style.setProperty('--fill', `${Math.round(v.okRatio * 100)}%`); okSum += v.okRatio; n++; }
      tip.bindEl(c, () => ({ title: `${label} · ${fmt.date(d)}`, rows: v ? [['Schlechtester Zustand', STATES[norm(v.worst)].word], ['Anteil ok', typeof v.okRatio === 'number' ? fmt.pct(v.okRatio * 100) : 'nur „aus“'], ['Stichproben', fmt.int(v.samples)]] : [['Daten', 'keine Stichprobe (Lücke)']] }));
      cells.append(c);
    }
    row.append(cells);
    row.append(el('span.sh__pct', n ? fmt.pct((100 * okSum) / n, 1) : '–'));
    if (extra) row.append(extra);
    return row;
  };
  // Zusammenfassung „Alle Dienste“
  const all = new Map();
  for (const d of dates) {
    const k = key(d);
    const vs = withData.map((s) => s.daily.find((x) => x.date === k)).filter(Boolean);
    if (vs.length) all.set(k, { worst: worst(vs.map((v) => v.worst)), okRatio: d3.mean(vs.filter((v) => typeof v.okRatio === 'number'), (v) => v.okRatio) ?? null, samples: d3.sum(vs, (v) => v.samples ?? 0) });
  }
  const allRow = rowFor('Alle Dienste', all, `${withData.length} mit Verlauf`, worst(list.map((s) => s.state)), null);
  allRow.classList.add('sh__row--all');
  wrap.append(allRow);
  if (!compact) {
    for (const s of [...withData].sort((a, b) => rank(b.state) - rank(a.state) || String(a.label).localeCompare(String(b.label), 'de'))) {
      const m = new Map(s.daily.map((v) => [v.date, v]));
      const fresh = data ? isFresh(s.changedAt, data) : false;
      wrap.append(rowFor(s.label ?? s.id, m, s.node, s.state, fresh ? el('span.zmark.zmark--new', { title: `Zustand geändert ${fmt.ago(s.changedAt)}` }, mark('warn'), 'neu') : null));
    }
  }
  if (!withData.length) wrap.append(emptyNote('Noch kein Verlauf – die Stichproben beginnen mit dem ersten v2-Lauf.'));
  const basis = d3.max(withData, (s) => s.daily.length) ?? 0;
  wrap.append(el('p.small.muted', `Stichprobe je Kollektorlauf (≈ 24/Tag). Zelle = Tag, Farbe = schlechtester Tageszustand, Füllhöhe = Anteil „läuft“. Schraffur = keine Stichprobe.${basis && basis < 3 ? ` Erst ${basis} ${basis === 1 ? 'Tag' : 'Tage'} Verlauf.` : ''}`));
  container.append(wrap);
  return wrap;
}

// ---------------------------------------------------------------- N3 Tage bis voll
export function daysToFull(container, vols) {
  const wrap = el('div.bullets');
  for (const v of vols) {
    const pct = typeof v.usedBytes === 'number' && typeof v.capBytes === 'number' && v.capBytes > 0 ? (100 * v.usedBytes) / v.capBytes : null;
    const f = v.forecast ?? null;
    const row = el('div.gauge-row');
    row.append(el('span.gauge-row__label', mark(pct === null ? 'unknown' : pct >= 90 ? 'crit' : pct >= 80 ? 'warn' : 'ok'), el('span', el('span.bullet-row__title', `${v.nodeLabel} · ${v.label ?? v.id}`), el('small', pct === null ? 'Belegung unbekannt' : `${fmt.bytes(v.usedBytes)} von ${fmt.bytes(v.capBytes)}`))));
    const g = el('div.gauge', { role: 'img', 'aria-label': pct === null ? 'keine Daten' : `${fmt.pct(pct)} belegt` });
    g.append(el('div.gauge__track'));
    if (pct !== null) g.append(el('div.gauge__fill', { class: `gauge__fill${pct >= 90 ? ' gauge__fill--crit' : pct >= 80 ? ' gauge__fill--warn' : ''}`, style: { width: `${Math.min(100, pct)}%` } }));
    else g.classList.add('gauge--unknown');
    g.append(el('div.gauge__mark', { style: { left: '80%' }, title: '80 %' }), el('div.gauge__mark.gauge__mark--100', { style: { left: '100%' }, title: '100 %' }));
    row.append(g);
    let txt, note;
    if (!f) { txt = 'noch wenig Verlauf'; note = 'Prognose erst ab 14 Tagen Messung'; }
    else if (typeof f.daysTo80 === 'number') { txt = `~${fmt.int(f.daysTo80)} Tage bis 80 %`; note = typeof f.daysTo100 === 'number' ? `voll in ~${fmt.int(f.daysTo100)} Tagen · +${fmt.bytes(f.slopeBytesPerDay * 30)} pro Monat` : null; }
    else if (typeof f.daysTo100 === 'number') { txt = `~${fmt.int(f.daysTo100)} Tage bis voll`; note = 'die 80-%-Marke ist schon überschritten'; }
    else if (typeof f.slopeBytesPerDay === 'number' && f.slopeBytesPerDay <= 0) { txt = 'Belegung sinkt'; note = `${fmt.bytes(-f.slopeBytesPerDay * 30)} pro Monat weniger`; }
    else { txt = 'keine Prognose'; note = null; }
    row.append(el('span.gauge-row__num', el('strong', txt), note ? el('small', note) : null));
    tip.bindEl(g, () => ({ title: `${v.nodeLabel} · ${v.label ?? v.id}`, rows: [['Belegt', pct === null ? null : fmt.pct(pct, 1)], ['Wachstum', f && typeof f.slopeBytesPerDay === 'number' ? `${fmt.bytes(f.slopeBytesPerDay)} / Tag` : null], ['Basis', f ? `${fmt.num1(f.basisDays)} Tage` : 'noch kein Verlauf']] }));
    wrap.append(row);
  }
  if (!vols.length) wrap.append(emptyNote('Keine Volumen.'));
  container.append(wrap);
  return wrap;
}

// ---------------------------------------------------------------- N4 Laufplan (Gantt)
export function runGantt(container, runs, jobs, { days = 14, now = new Date() } = {}) {
  const list = (runs ?? []).filter((r) => r.start);
  const jobIds = [...new Set([...jobs.map((j) => j.id), ...list.map((r) => r.job)])].filter((id) => list.some((r) => r.job === id));
  const labelOf = (id) => jobs.find((j) => j.id === id)?.label ?? id;
  const dense = new Set(jobIds.filter((id) => list.filter((r) => r.job === id).length > days * 3));
  if (!list.length) { container.append(emptyNote('Noch keine Läufe protokolliert.')); return; }
  return responsive(container, (W) => {
    const rowH = 36, top = 26, bottom = 24, labelW = Math.min(220, Math.max(140, W * 0.22));
    const H = top + jobIds.length * rowH + bottom;
    const t0 = d3.timeDay.offset(d3.timeDay.floor(now), -(days - 1)), t1 = d3.timeHour.offset(now, 1);
    const x = d3.scaleTime().domain([t0, t1]).range([labelW, W - 16]);
    const svg = svgPx(container, W, H, `Sicherungsläufe der letzten ${days} Tage`);
    svg.append('g').attr('class', 'axis grid').attr('transform', `translate(0, ${top - 6})`).call(d3.axisTop(x).ticks(d3.timeDay.every(Math.ceil(days / Math.max(3, Math.floor((W - labelW) / 90))))).tickFormat(d3.timeFormat('%d.%m.')).tickSize(-(jobIds.length * rowH + 6))).call((g) => g.selectAll('text').attr('dy', '-0.4em'));
    // Wochenenden leicht getönt
    for (const d of d3.timeDays(t0, t1)) if (d.getDay() === 0 || d.getDay() === 6) svg.insert('rect', ':first-child').attr('class', 'gantt__weekend').attr('x', x(d)).attr('y', top - 6).attr('width', x(d3.timeDay.offset(d, 1)) - x(d)).attr('height', jobIds.length * rowH + 6);
    const g = svg.selectAll('g.row').data(jobIds).join('g').attr('transform', (d, i) => `translate(0, ${top + i * rowH})`);
    g.append('text').attr('class', 'chart-label').attr('x', 0).attr('y', rowH / 2 - 2).text((id) => labelOf(id)).each(function () { fitOne(this, labelW - 14); });
    g.append('text').attr('class', 'chart-label-sub').attr('x', 0).attr('y', rowH / 2 + 13).text((id) => { const rs = list.filter((r) => r.job === id); const fails = rs.filter((r) => r.ok === false).length; return `${rs.length} Läufe${fails ? ` · ${fails} fehlgeschlagen` : ''}${dense.has(id) ? ' · dicht (Tick-Spur)' : ''}`; }).each(function () { fitOne(this, labelW - 14); });
    g.each(function (id) {
      const row = d3.select(this);
      const rs = list.filter((r) => r.job === id);
      const isDense = dense.has(id);
      const sel = row.selectAll('rect.run').data(rs).join('rect')
        .attr('class', (r) => `gantt__run gantt__run--${r.running ? 'running' : r.ok === true ? 'ok' : r.ok === false ? 'fail' : 'unknown'}${isDense ? ' gantt__run--tick' : ''}`)
        .attr('x', (r) => x(new Date(r.start)))
        .attr('y', isDense ? 11 : 8).attr('height', isDense ? 14 : 20).attr('rx', isDense ? 1 : 3)
        .attr('width', (r) => { const s = new Date(r.start), e = r.end ? new Date(r.end) : (r.running ? now : s); return Math.max(isDense ? 1.5 : 3, x(e) - x(s)); })
        .attr('tabindex', 0);
      tip.bind(sel, (r) => ({ title: `${labelOf(id)} · ${fmt.dateTime(r.start)}`, rows: [['Dauer', r.end ? fmt.duration((new Date(r.end) - new Date(r.start)) / 1000) : r.running ? 'läuft noch' : 'kein Ende protokolliert'], ['Ergebnis', r.ok === true ? 'ok' : r.ok === false ? `fehlgeschlagen (Exit ${r.exitCode ?? '?'})${r.skipped ? ' · übersprungen, lief bereits' : ''}` : 'unbekannt'], ['Hinzugefügt', typeof r.bytesAdded === 'number' ? fmt.bytes(r.bytesAdded) : null], ['Neue Dateien', typeof r.filesNew === 'number' ? fmt.int(r.filesNew) : null], ['Quelle', r.source ?? null]] }));
    });
    svg.append('line').attr('class', 'eol__today').attr('x1', x(now)).attr('x2', x(now)).attr('y1', top - 6).attr('y2', top + jobIds.length * rowH);
    svg.append('text').attr('class', 'eol__today-label').attr('x', Math.min(x(now) + 5, W - 70)).attr('y', H - 6).text('Jetzt');
  }, { minWidth: 560, scroll: true });
}

// ---------------------------------------------------------------- N5 Expositions-Änderungen
export function exposureChanges(container, ports, portsClosed, { now = new Date(), data = null } = {}) {
  const week = 7 * 864e5;
  const seen = ports.filter((p) => p.firstSeen);
  // „neu offen“ nur, wenn der Verlauf älter ist als der Port (sonst ist firstSeen bloss der erste Kollektorlauf)
  const start = data ? observationStart(data) : null;
  const fresh = seen.filter((p) => now - new Date(p.firstSeen) < week && start !== null && new Date(p.firstSeen).getTime() > start + 3.6e6).sort((a, b) => new Date(b.firstSeen) - new Date(a.firstSeen));
  const closed = (portsClosed ?? []).slice().sort((a, b) => new Date(b.lastSeen) - new Date(a.lastSeen));
  const unrated = ports.filter((p) => (p.expected === null || p.expected === undefined) && p.firstSeen).sort((a, b) => new Date(a.firstSeen) - new Date(b.firstSeen));
  const wrap = el('div.changes');
  const block = (title, tone, items, render, empty) => {
    const b = el('div.changes__block');
    b.append(el('div.row', el('span.label', title), badge(String(items.length), tone)));
    if (!items.length) b.append(el('p.small.muted', empty));
    const ul = el('ul.changes__list');
    for (const it of items.slice(0, 12)) ul.append(render(it));
    if (items.length > 12) ul.append(el('li.small.muted', `+ ${items.length - 12} weitere in der Tabelle`));
    b.append(ul);
    return b;
  };
  const portLine = (p, when, what) => el('li.changes__item', mark(p.expected === false ? 'crit' : p.expected === true ? 'ok' : 'unknown'),
    el('span.changes__text', el('strong', `${p.node} · ${p.port}/${p.proto ?? 'tcp'}`), ` ${p.process ? `(${p.process}) ` : ''}${what}`), el('span.changes__when', when));
  wrap.append(block('Neu offen seit letzter Woche', fresh.length ? 'warn' : 'outline', fresh, (p) => portLine(p, `seit ${fmt.date(p.firstSeen)}`, `${p.scope === 'all' ? 'an allen Schnittstellen' : p.scope ?? ''}${p.expected === false ? ' · unerwartet' : p.expected === true ? ' · erwartet' : ' · unbewertet'}`), seen.length ? (start !== null && now - start > week ? 'Nichts Neues in den letzten 7 Tagen.' : `Verlauf erst seit ${fmt.ago(start)} – Neues ist erst nach dem ersten Lauf erkennbar.`) : 'Noch kein Verlauf (firstSeen fehlt).'));
  wrap.append(block('Geschlossen (30 Tage)', closed.length ? 'ok' : 'outline', closed, (p) => portLine(p, `zuletzt ${fmt.date(p.lastSeen)}`, 'nicht mehr lauschend'), 'Kein Port wurde geschlossen – oder der Verlauf ist noch zu kurz.'));
  wrap.append(block('Seit X Tagen unbewertet', unrated.length ? 'unknown' : 'outline', unrated, (p) => portLine(p, `seit ${fmt.int(Math.max(0, Math.floor((now - new Date(p.firstSeen)) / 864e5)))} Tagen`, 'ohne Eintrag in ports-expected.json'), 'Alle Ports sind bewertet.'));
  container.append(wrap);
  return wrap;
}

// ---------------------------------------------------------------- N6 Dienst-Waben
export function serviceHoneycomb(container, services, nodes, { onNode, now = new Date(), data = null } = {}) {
  const groups = nodes.map((n) => ({ node: n, svcs: services.filter((s) => s.node === n.id).sort((a, b) => rank(b.state) - rank(a.state) || String(a.label).localeCompare(String(b.label), 'de')) })).filter((g) => g.svcs.length);
  if (!groups.length) { container.append(emptyNote('Keine Dienste.')); return; }
  return responsive(container, (W) => {
    const r = 13, dx = r * Math.sqrt(3), dy = r * 1.5, pad = 12, titleH = 22, gapX = 16;
    // Gruppen in Zeilen umbrechen, wenn die Breite nicht für alle reicht (mobil)
    const gpr = Math.max(1, Math.min(groups.length, Math.floor((W + gapX) / (150 + gapX))));
    const colW = Math.floor((W - (gpr - 1) * gapX) / gpr);
    const perRow = Math.max(1, Math.floor((colW - pad * 2 - r) / dx));
    const groupH = (g) => titleH + pad + Math.ceil(g.svcs.length / perRow) * dy + r + pad;
    const rowTops = [];
    let yAcc = 0;
    for (let i = 0; i < groups.length; i += gpr) { rowTops.push(yAcc); yAcc += Math.max(...groups.slice(i, i + gpr).map(groupH)) + 8; }
    const H = yAcc + 14;
    const svg = svgPx(container, W, H, 'Dienst-Waben je Knoten');
    const hex = d3.line().curve(d3.curveLinearClosed)(d3.range(6).map((i) => { const a = (Math.PI / 3) * i + Math.PI / 6; return [r * Math.cos(a), r * Math.sin(a)]; }));
    groups.forEach((g, gi) => {
      const gx = (gi % gpr) * (colW + gapX), gy = rowTops[Math.floor(gi / gpr)];
      const grp = svg.append('g').attr('transform', `translate(${gx}, ${gy})`);
      const head = grp.append('g').attr('class', 'honey__head').style('cursor', 'pointer').on('click', () => onNode?.(g.node.id));
      head.append('use').attr('href', `#${STATES[norm(g.node.state)].symbol}`).attr('x', 0).attr('y', 4).attr('width', 11).attr('height', 11).attr('class', `mark mark--${norm(g.node.state)}`);
      head.append('text').attr('class', 'chart-label').attr('x', 16).attr('y', 14).text(`${g.node.label ?? g.node.id} · ${g.svcs.length}`).each(function () { fitOne(this, colW - 20); });
      const cells = grp.selectAll('g.cell').data(g.svcs).join('g').attr('class', 'honey__cell')
        .attr('transform', (s, i) => `translate(${pad + r + (i % perRow) * dx + ((Math.floor(i / perRow) % 2) * dx) / 2}, ${titleH + pad + r + Math.floor(i / perRow) * dy})`)
        .attr('tabindex', 0).attr('role', 'img').attr('aria-label', (s) => `${s.label ?? s.id}: ${STATES[norm(s.state)].word}`);
      cells.append('path').attr('d', hex).attr('class', (s) => `honey__hex honey__hex--${norm(s.state)}`);
      cells.append('use').attr('href', (s) => `#${STATES[norm(s.state)].symbol}`).attr('x', -5).attr('y', -5).attr('width', 10).attr('height', 10).attr('class', (s) => `mark honey__mark honey__mark--${norm(s.state)}`);
      cells.filter((s) => data && isFresh(s.changedAt, data)).append('circle').attr('class', 'honey__new').attr('cx', r * 0.55).attr('cy', -r * 0.55).attr('r', 3.5);
      tip.bind(cells, (s) => ({ title: s.label ?? s.id, rows: [['Zustand', STATES[norm(s.state)].word], ['Detail', s.detail ?? null], ['Geändert', s.changedAt ? fmt.ago(s.changedAt) : null]] }));
    });
    svg.append('text').attr('class', 'chart-note').attr('x', 0).attr('y', H - 4).text('Wabe = Dienst; Form + Farbe = Zustand; leer gestrichelt = absichtlich aus; Punkt = geändert in den letzten 6 h.').each(function () { fitOne(this, W); });
  }, { minWidth: 320 });
}

function rank(s) { return { crit: 4, warn: 3, unknown: 2, ok: 1, off: 0 }[norm(s)]; }
export { lu };
