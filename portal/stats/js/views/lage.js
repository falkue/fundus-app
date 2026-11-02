/**
 * Ansicht „Lage“: Urteil, Frische je Kollektor, vier KPI-Kacheln, Zu-tun-Liste,
 * Einordnung, Mini-Landschaft.
 */
import { el, headline, card, mark, statePill, badge, emptyNote, append, legend } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { STATES, norm, SEVERITY_WORD, SEVERITY_STATE } from '../lib/state.js';
import { verdict, serviceStats, backupStats, vulnStats, exposureStats, openFindings, series, sparkValues, aiStats, isFresh, observationStart } from '../lib/derive.js';
import { sparkline } from '../charts/sparkline.js';
import { renderLandscape } from '../charts/landscape.js';
import { heartbeat, serviceHoneycomb, daysToFull, statusHistory } from '../charts/history.js';
import { lu } from '../lib/dom.js';

const CATEGORY_VIEW = { exposure: '#exposition', patch: '#patches', eol: '#patches', backup: '#sicherung', service: '#dienste', capacity: '#verlauf', hardware: '#landschaft', ai: '#ki' };

export function render(root, data, ctx) {
  const v = verdict(data);
  const head = el('header.page-head');
  head.append(headline('h1', v.text, v.accent));
  head.append(el('p.lede', v.sub));
  // Frische
  const fresh = el('div.freshness');
  fresh.append(el('span', `Erhebung ${fmt.dateTime(data.generated)} (${fmt.ago(data.generated)})`));
  for (const c of v.collectors) {
    const item = el('span.freshness__item', { title: [c.label, c.errors.length ? c.errors.join(' · ') : null].filter(Boolean).join(' – ') },
      el('span.status-dot', { class: `status-dot status-dot--${c.state}` }), `${c.id} ${c.at ? fmt.ago(c.at) : 'ohne Zeitstempel'}${c.state === 'crit' ? ' · fehlgeschlagen' : c.state === 'warn' ? ' · veraltet' : ''}`);
    fresh.append(item);
  }
  if (!v.collectors.length) fresh.append(el('span.freshness__item', mark('unknown'), 'Keine Kollektor-Angaben'));
  head.append(fresh);
  root.append(head);

  // KPI-Kacheln
  const svc = serviceStats(data), bk = backupStats(data), vu = vulnStats(data), ex = exposureStats(data);
  const grid = el('div.grid.grid--kpi');
  grid.append(kpi({
    label: 'Dienste laufen', value: `${fmt.int(svc.running)}`, unit: `/ ${fmt.int(svc.total)}`,
    state: svc.problems ? (svc.crit ? 'crit' : 'warn') : svc.total ? 'ok' : 'unknown',
    foot: svc.total ? `${svc.problems ? `${svc.problems} mit Befund` : 'alle ohne Befund'}${svc.off ? ` · ${svc.off} absichtlich aus` : ''}` : 'keine Dienste erfasst',
    spark: sparkValues(series(data, 'landscape.servicesRunning')), href: '#dienste',
  }));
  grid.append(kpi({
    label: 'Sicherungen · schlechtester RPO', value: bk.worstRatio === null ? '–' : `${fmt.num1(bk.worstRatio)}×`, unit: bk.worstRatio === null ? '' : 'Soll',
    title: 'Alter der letzten erfolgreichen Sicherung geteilt durch das erlaubte Alter (RPO), für den Job, der seiner Grenze am nächsten ist. Unter 1 = im Rahmen, ab 1 = überfällig.',
    state: bk.state,
    foot: `${bk.worstJob ? `${bk.worstJob.label}: ${fmt.num1(bk.worstJob.ageH)} h von max. ${fmt.int(bk.worstJob.rpoH)} h` : 'kein Job mit Alter'} · Offsite ${bk.offsiteKnown ? `${bk.offsiteYes} von ${bk.offsiteKnown}` : 'unbekannt'}`,
    meter: bk.worstRatio === null ? null : { ratio: bk.worstRatio, title: `${fmt.pct(bk.worstRatio * 100)} des erlaubten Alters erreicht (ab 100 % überfällig)` },
    href: '#sicherung',
  }));
  grid.append(kpi({
    label: 'Handlungsrelevante Schwachstellen', value: fmt.int(vu.actionable), unit: '',
    state: vu.state,
    foot: [vu.security ? `${fmt.int(vu.security)} Sicherheitsupdates` : null, vu.eolNodes.length ? `${vu.eolNodes.length} EOL` : null, vu.hasPrio ? (vu.kev ? `${vu.kev} KEV` : null) : null, vu.hasPrio ? (vu.epss ? `+ ${vu.epss} CVEs mit hoher EPSS (versionsbasiert, nicht gezählt)` : null) : (vu.cves ? `${vu.cves} CVEs aussen (versionsbasiert, nicht gezählt)` : null), vu.patchUnknown ? `${vu.patchUnknown} ohne Patch-Daten` : null].filter(Boolean).join(' · ') || 'nichts bekannt',
    spark: sparkValues(series(data, 'landscape.securityUpdates')), href: '#patches',
  }));
  grid.append(kpi({
    label: 'Unerwartete offene Ports', value: fmt.int(ex.unexpected.length), unit: ex.unrated.length ? `+ ${ex.unrated.length} unbewertet` : '',
    state: ex.state,
    foot: ex.total ? `${ex.total} lauschende Sockets · ${ex.openNoAuth.length} ohne Anmeldung an allen Schnittstellen` : 'keine Port-Daten',
    spark: sparkValues(series(data, 'landscape.portsUnexpected')), href: '#exposition',
  }));
  root.append(grid);

  // Zu tun + Einordnung + Mini-Landschaft
  const ai = ctx.insight('lage', { quiet: true }); if (ai) root.append(ai);
  // N1 Kollektor-Herzschlag + N2 Kurzform
  const hbCard = card({ title: 'Kollektor-Herzschlag', accent: 'Herzschlag', sub: 'Die letzten 28 Läufe je Kollektor: grün ok, orange ok mit Teilfehlern, koralle fehlgeschlagen, schraffiert = noch kein Lauf. Stille Ausfälle fallen hier sofort auf.' });
  const hbWrap = el('div');
  for (const c of v.collectors) heartbeat(hbWrap, c);
  if (!v.collectors.length) hbWrap.append(emptyNote('Keine Kollektoren.'));
  hbCard.append(hbWrap);
  const avail = card({ title: 'Verfügbarkeit der Dienste', accent: 'Verfügbarkeit', sub: 'Alle Dienste zusammengefasst, letzte 30 Tage. Einzelne Dienste in der Ansicht Dienste.' });
  const availWrap = el('div');
  statusHistory(availWrap, data.services, { compact: true, data });
  avail.append(availWrap);
  root.append(el('div.grid.grid--2', hbCard, avail));
  const two = el('div.grid.grid--2');
  two.append(todoCard(data, ctx));
  const right = el('div.stack');
  right.append(insightCard(data));
  const mini = card({ title: 'Landschaft im Kleinen', accent: 'Kleinen', sub: 'Knoten mit Zustandsring und Kurzgrund. Verbindungen und Dienste stehen in der Landschaft. Klick öffnet sie.' });
  const plot = el('div.chart__plot');
  mini.append(plot);
  right.append(mini);
  two.append(right);
  root.append(two);
  // N6 Dienst-Waben + N3 Tage bis voll (Kurzform)
  const honey = card({ title: 'Dienste als Waben', accent: 'Waben', sub: 'Je Knoten eine Wabengruppe, Form und Farbe zeigen den Zustand. Klick auf den Knotennamen öffnet die Details.' });
  const honeyPlot = el('div');
  honey.append(honeyPlot);
  const full = card({ title: 'Tage bis voll', accent: 'voll', sub: 'Belegung je Volumen mit Marken bei 80 und 100 %; die Prognose kommt aus dem Dauerwachstum ohne einmalige Sprünge (frühestens nach 14 Tagen Verlauf).' });
  const fullWrap = el('div');
  daysToFull(fullWrap, data.nodes.flatMap((n) => (n.volumes ?? []).map((vol) => ({ ...vol, nodeLabel: n.label ?? n.id }))));
  full.append(fullWrap);
  root.append(el('div.grid.grid--2', honey, full));
  serviceHoneycomb(honeyPlot, data.services, data.nodes, { onNode: (id) => ctx.openNode(id), data });
  return renderLandscape(plot, data, { mini: true, onNode: () => ctx.navigate('#landschaft') });
}

function kpi({ label, value, unit, state, foot, spark, meter, href, title }) {
  const tile = el('button.kpi', { type: 'button', title: title ?? null, on: { click: () => { location.hash = href; } }, 'aria-label': `${label}: ${value} ${unit ?? ''} – ${foot}${title ? ` – ${title}` : ''}` });
  tile.append(el('span.kpi__label', label));
  tile.append(el('span.kpi__value', value, unit ? el('small', unit) : null));
  if (meter) {
    // Füllstand bis zur Grenze (100 % = Soll erreicht); darüber koralle
    const pct = Math.max(0, meter.ratio * 100);
    const m = el('span.kpi__meter', { title: meter.title ?? '' },
      el('span.kpi__meter-fill', { class: `kpi__meter-fill${pct >= 100 ? ' is-over' : pct >= 80 ? ' is-near' : ''}`, style: { width: `${Math.min(100, pct)}%` } }));
    tile.append(m);
  } else {
    const sp = el('span.kpi__spark', { 'aria-hidden': 'true' });
    sparkline(sp, spark, { width: 84, height: 30 });
    tile.append(sp);
  }
  tile.append(el('span.kpi__foot', mark(state), el('span', foot)));
  return tile;
}

function dailySpark(data) {
  const by = new Map();
  for (const d of data.backups.daily) if (d?.date) by.set(d.date, (by.get(d.date) ?? 0) + (d.bytes ?? 0));
  const days = [...by.keys()].sort().slice(-21);
  return days.map((k) => by.get(k));
}

function todoCard(data, ctx) {
  const open = openFindings(data);
  const c = card({ title: open.length ? `Zu tun: ${open.length} ${open.length === 1 ? 'Befund' : 'Befunde'}` : 'Zu tun', accent: 'tun',
    sub: open.length > 5 ? 'Die fünf schwersten. Alle Befunde stehen in den jeweiligen Ansichten.' : 'Nach Schwere sortiert.' });
  if (!open.length) { c.append(emptyNote(data.findings.length ? 'Alle Befunde sind quittiert.' : 'Keine Befunde geliefert – das ist kein Freispruch, solange Kollektoren fehlen.')); return c; }
  const list = el('ul.todo');
  for (const f of open.slice(0, 5)) {
    const sev = f.severity in SEVERITY_WORD ? f.severity : 'low';
    const item = el('li.todo__item');
    item.append(el('span.todo__sev', mark(SEVERITY_STATE[sev])));
    const main = el('div');
    const titleBtn = el('a.todo__title', { href: CATEGORY_VIEW[f.category] ?? '#lage' }, f.title ?? f.id);
    main.append(el('div', titleBtn, f.node ? el('span.small.muted', ` · ${f.node}`) : null));
    if (f.detail) main.append(el('div.todo__detail', f.detail));
    if (f.risk) main.append(el('div.todo__risk', lu('triangle-alert', 'lu lu--sm'), el('span', f.risk)));
    const isNew = isFresh(f.firstSeen, data);
    main.append(el('div.todo__action', badge(SEVERITY_WORD[sev], SEVERITY_STATE[sev]), isNew ? el('span.zmark.zmark--new', mark('warn'), 'neu') : null, f.action ? el('span', `Massnahme: ${f.action}`) : null));
    item.append(main);
    const meta = el('div.todo__meta');
    if (f.firstSeen || f.since) meta.append(el('span.muted', { title: f.firstSeen ? `zuerst gesehen ${fmt.dateTime(f.firstSeen)}` : '' }, `seit ${fmt.date(f.firstSeen ?? f.since)}`));
    meta.append(el('button.btn.btn--small', { type: 'button', on: { click: () => ctx.ack(f.id) }, title: 'Als bekannt quittieren (bleibt in den Daten)' }, 'Quittieren'));
    item.append(meta);
    list.append(item);
  }
  c.append(list);
  return c;
}

/** Einordnung: aus den Befunden berechnete Sätze (violette Hinweiskarte). */
function insightCard(data) {
  const open = openFindings(data);
  const ai = aiStats(data);
  const c = el('section.ai-card', { 'aria-label': 'Einordnung' });
  c.append(el('div.ai-card__tag', 'Einordnung'));
  if (!open.length) {
    c.append(el('div.ai-card__headline', 'Nichts Offenes – die Ruhe gilt nur so weit, wie die Kollektoren reichen.'));
    return c;
  }
  const byNode = new Map();
  for (const f of open) { const k = f.node ? (data.nodes.find((n) => n.id === f.node)?.label ?? f.node) : 'der Landschaft als Ganzes'; byNode.set(k, (byNode.get(k) ?? 0) + 1); }
  const top = [...byNode.entries()].sort((a, b) => b[1] - a[1]);
  const worstNode = top[0];
  const h = el('div.ai-card__headline');
  append(h, [`Die meisten Befunde liegen auf `, el('em', worstNode[0]), ` (${worstNode[1]} von ${open.length}).`]);
  c.append(h);
  const cats = new Map();
  for (const f of open) cats.set(f.category, (cats.get(f.category) ?? 0) + 1);
  const CAT = { exposure: 'Exposition', patch: 'Patches', eol: 'Lebensende', backup: 'Sicherung', service: 'Dienste', capacity: 'Kapazität', hardware: 'Hardware', ai: 'KI' };
  c.append(el('p', `Themen: ${[...cats.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => `${CAT[k] ?? k} ${n}`).join(', ')}.`));
  const crit = open.filter((f) => f.severity === 'crit');
  if (crit.length) c.append(el('p', `Zuerst: ${crit.map((f) => f.title).join('; ')}.`));
  if (ai.limit !== null && ai.loaded > ai.limit) c.append(el('p', `Der KI-Knoten hält ${ai.loaded} Modelle geladen, erlaubt sind ${ai.limit} – das kostet ${fmt.bytes(ai.loadedBytes)} Unified Memory.`));
  c.append(el('div.ai-card__foot', el('span', 'Regelbasiert aus den Befunden abgeleitet, kein Sprachmodell.'), el('span', `${open.length} offen · ${data.findings.length - open.length} quittiert`)));
  return c;
}
