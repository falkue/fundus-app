/**
 * Ansicht „Dienste“: C4-Container-Sicht als eingerückter Baum (Knoten → Gruppe → Dienst)
 * in EINEM gemeinsamen CSS-Raster (feste Spalten über alle Zeilen), Filter
 * Alle | Probleme | Absichtlich aus, Gruppen-Zähler, Abhängigkeiten, Anwendungen (Meli, NECTAR).
 */
import { el, pageHead, card, segmented, mark, icon, statePill, badge, emptyNote, clear, dl, headline, lu } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { STATES, norm, worst, fillState } from '../lib/state.js';
import { serviceStats } from '../lib/derive.js';
import { bulletRow } from '../charts/bullet.js';
import { statusHistory } from '../charts/history.js';

const GROUP_ORDER = ['KI', 'Anwendungen', 'Sicherung', 'Daten', 'Medien', 'Netz', 'Überwachung', 'System'];
const KIND = { systemd: 'systemd', 'systemd-user': 'systemd (user)', docker: 'Docker', qpkg: 'QPKG', process: 'Prozess', timer: 'Timer', web: 'Web' };

export function render(root, data, ctx) {
  const st = serviceStats(data);
  const lede = st.total ? `${st.running} von ${st.total} Diensten laufen${st.problems ? `, ${st.problems} ${st.problems === 1 ? 'braucht' : 'brauchen'} Aufmerksamkeit` : ''}${st.off ? ` · ${st.off} absichtlich aus` : ''}${st.unknown ? ` · ${st.unknown} ohne Daten` : ''}.` : 'Keine Dienste geliefert.';
  const title = st.problems ? `${numWord(st.problems)} ${st.problems === 1 ? 'Dienst braucht' : 'Dienste brauchen'} Aufmerksamkeit`
    : !data.services.length ? 'Keine Dienste geliefert' : st.unknown ? `${numWord(st.unknown)} ${st.unknown === 1 ? 'Dienst' : 'Dienste'} ohne Daten` : 'Alle Dienste laufen wie vorgesehen';
  root.append(pageHead({ title, accent: st.problems ? 'Aufmerksamkeit' : !data.services.length ? 'Dienste' : st.unknown ? 'Daten' : 'vorgesehen', lede }));
  const ins = ctx.insight('dienste'); if (ins) root.append(ins);

  const c = card({ title: 'Container-Sicht', accent: 'Container', sub: 'C4-Container: jede eigenständig lauffähige Einheit, nicht nur Docker. Spalten: Zustand · Dienst · Detail · Version · seit · Ports. Knoten klappbar.' });
  const treeWrap = el('div');
  let filter = 'all';
  const seg = segmented([
    { value: 'all', label: 'Alle', count: data.services.length },
    { value: 'problems', label: 'Probleme', count: st.problems + st.unknown },
    { value: 'off', label: 'Absichtlich aus', count: data.services.filter((s) => s.intended === 'off').length },
  ], filter, (v) => { filter = v; drawTree(); }, 'Filter');
  c.querySelector('.card__head').append(el('div.chart__tools', seg.el));
  c.append(treeWrap);
  root.append(c);
  drawTree();

  function drawTree() {
    clear(treeWrap);
    const pred = { all: () => true, problems: (s) => ['warn', 'crit', 'unknown'].includes(norm(s.state)), off: (s) => s.intended === 'off' }[filter];
    const list = data.services.filter(pred);
    if (!list.length) { treeWrap.append(emptyNote('Nichts in dieser Auswahl.')); return; }
    const tree = el('div.tree', { role: 'table', 'aria-label': 'Dienste' });
    // Kopfzeile des Rasters
    tree.append(el('div.tree__headrow', { role: 'row' },
      el('span.tree__cell.tree__cell--mark.label', ''), el('span.tree__cell.label', 'Dienst'), el('span.tree__cell.label', 'Zustand · Detail'),
      el('span.tree__cell.label.tree__cell--right', 'Version'), el('span.tree__cell.label.tree__cell--right', 'seit'), el('span.tree__cell.label.tree__cell--right', 'Ports')));
    const nodeIds = [...new Set([...data.nodes.map((n) => n.id), ...list.map((s) => s.node)])];
    for (const nid of nodeIds) {
      const svcs = list.filter((s) => s.node === nid);
      if (!svcs.length) continue;
      const node = data.nodes.find((n) => n.id === nid);
      const nodeState = worst(svcs.map((s) => s.state));
      const details = el('div.tree__node', { 'aria-expanded': 'true' });
      const counts = countStates(svcs);
      const head = el('button.tree__head', { type: 'button', on: { click: () => { details.setAttribute('aria-expanded', details.getAttribute('aria-expanded') === 'true' ? 'false' : 'true'); } } },
        icon('ic-caret', 'tree__caret'), mark(nodeState), el('span.tree__title', node?.label ?? nid), el('span.small.muted.tree__role', node?.role ?? ''),
        el('span.tree__counts', ...countBadges(counts, svcs.length)));
      details.append(head);
      const children = el('div.tree__children');
      const groups = [...new Set(svcs.map((s) => s.group ?? 'Sonstige'))].sort((a, b) => (GROUP_ORDER.indexOf(a) + 100) % 100 - (GROUP_ORDER.indexOf(b) + 100) % 100 || a.localeCompare(b, 'de'));
      for (const g of groups) {
        const gs = svcs.filter((s) => (s.group ?? 'Sonstige') === g);
        children.append(el('div.tree__group', el('span.label', `${g} · ${gs.length}`)));
        for (const s of gs.sort((a, b) => rank(b) - rank(a) || String(a.label).localeCompare(String(b.label), 'de'))) children.append(leaf(s));
      }
      details.append(children);
      tree.append(details);
    }
    treeWrap.append(tree);
  }

  // N2 Verfügbarkeit 30 Tage
  const av = card({ title: 'Verfügbarkeit, 30 Tage', accent: '30 Tage', sub: 'Status-History je Dienst: eine Zelle je Tag, Farbe = schlechtester Tageszustand, Füllhöhe = Anteil „läuft“. Rechts der Prozentwert über alle Tage mit Stichprobe.' });
  const avWrap = el('div');
  statusHistory(avWrap, data.services, { data });
  av.append(avWrap);
  root.append(av);

  // Abhängigkeiten zwischen Diensten
  const svcIds = new Set(data.services.map((s) => s.id));
  const deps = data.links.filter((l) => svcIds.has(l.from) && svcIds.has(l.to));
  if (deps.length) {
    const dc = card({ title: 'Abhängigkeiten', accent: 'Abhängigkeiten', sub: 'Wer braucht wen? Fällt der genannte Dienst aus, sind die Verbraucher betroffen.' });
    const byTarget = new Map();
    for (const l of deps) { if (!byTarget.has(l.to)) byTarget.set(l.to, []); byTarget.get(l.to).push(l); }
    for (const [to, ls] of byTarget) {
      const target = data.services.find((s) => s.id === to);
      const block = el('div.deps');
      block.append(el('div.row', mark(target?.state), el('strong', target?.label ?? to), el('span.small.muted', `← ${ls.length} Verbraucher`)));
      const list = el('div.row');
      for (const l of ls) { const from = data.services.find((s) => s.id === l.from); list.append(el('span.chip', { title: l.label ?? '' }, mark(from?.state), from?.label ?? l.from)); }
      block.append(list);
      dc.append(block);
    }
    root.append(dc);
  }

  root.append(headline('h2', 'Anwendungen', 'Anwendungen'));
  const ac = card({ title: `${data.apps.length} ${data.apps.length === 1 ? 'Anwendung' : 'Anwendungen'}`, accent: null, sub: 'Firebase-Projekte und Websites: Hosting-Zustand mit Antwortzeit, TLS-Resttage, genutzte Firebase-Dienste und Kontingent-Auslastung. Zeile aufklappen für Kontingente, Prüfungen und Fehler. „Nutzungsdaten nicht freigegeben“ ist kein Fehler – nur fehlender Zugriff.' });
  if (!data.apps.length) ac.append(emptyNote('Keine Anwendungen geliefert.'));
  else ac.append(appsTable(data.apps));
  root.append(ac);
}

const FB = [['firestore', 'database', 'Firestore'], ['functions', 'plug', 'Cloud Functions'], ['storage', 'archive', 'Storage'], ['auth', 'key-round', 'Authentication']];
function appsTable(apps) {
  const wrap = el('div.apps', { role: 'table', 'aria-label': 'Anwendungen' });
  wrap.append(el('div.apps__head', { role: 'row' }, el('span'), el('span.label', 'Anwendung'), el('span.label', 'Plattform'), el('span.label', 'Hosting'), el('span.label', 'TLS'), el('span.label', 'Dienste'), el('span.label', 'Kontingent'), el('span')));
  const sorted = [...apps].sort((a, b) => rankApp(b) - rankApp(a) || String(a.label).localeCompare(String(b.label), 'de'));
  for (const a of sorted) {
    const host = (a.hosting ?? a.checks ?? [])[0] ?? null;
    const hostState = host ? norm(host.state) : 'unknown';
    const tlsDays = host?.tlsDays ?? a.tls?.daysLeft ?? null;
    const tlsState = tlsDays === null ? 'unknown' : tlsDays < 14 ? 'crit' : tlsDays < 30 ? 'warn' : 'ok';
    const q = (a.quotas ?? []).map((x) => (typeof x.used === 'number' && typeof x.limit === 'number' && x.limit > 0 ? (100 * x.used) / x.limit : null)).filter((v) => v !== null);
    const qMax = q.length ? Math.max(...q) : null;
    const row = el('button.apps__row', { type: 'button', role: 'row', 'aria-expanded': 'false' });
    row.append(el('span.apps__mark', mark(a.access === 'missing' && norm(a.state) === 'unknown' ? 'unknown' : a.state)));
    row.append(el('span.apps__name', a.label ?? a.id, el('small', [a.role, a.project].filter(Boolean).join(' · '))));
    row.append(el('span', platformLabel(a.platform)));
    row.append(el('span.apps__host', { title: host?.url ?? '' }, mark(hostState), host ? `${host.status ? `HTTP ${host.status}` : 'keine Antwort'}${typeof host.ms === 'number' ? ` · ${fmt.int(host.ms)} ms` : ''}` : 'kein Hosting-Check'));
    row.append(el('span.apps__host', mark(tlsState), tlsDays === null ? '–' : `${fmt.int(tlsDays)} Tage`));
    const svc = el('span.apps__svc');
    if (a.platform === 'firebase') for (const [key, ic, name] of FB) { const on = a.services?.[key]; svc.append(el('span', { title: `${name}: ${on === undefined || on === null ? 'unbekannt' : on ? (typeof on === 'number' ? `${fmt.int(on)}` : 'genutzt') : 'nicht genutzt'}` }, lu(ic, `lu${on ? '' : ' lu--off'}`))); }
    else svc.append(el('span.muted', '–'));
    row.append(svc);
    const quota = el('span.apps__quota');
    if (qMax !== null) { quota.append(el('span.apps__quota-bar', el('span.apps__quota-fill', { style: { width: `${Math.min(100, qMax)}%` } })), el('span', fmt.pct(qMax, 1))); }
    else quota.append(el('span.muted', a.access === 'missing' ? 'nicht freigegeben' : '–'));
    row.append(quota);
    row.append(icon('ic-caret', 'apps__caret'));
    const details = el('div.apps__details', { hidden: true });
    details.append(appDetails(a));
    row.addEventListener('click', () => { const open = details.hidden; details.hidden = !open; row.setAttribute('aria-expanded', String(open)); });
    wrap.append(row, details);
  }
  return wrap;
}
function rankApp(a) { return { crit: 4, warn: 3, unknown: 1, ok: 2, off: 0 }[norm(a.state)] ?? 1; }
function appDetails(a) {
  const d = el('div.stack.stack--tight');
  if (a.stateReason) d.append(el('p.small.secondary.clamp-2', { title: a.stateReason }, shorten(a.stateReason, 200)));
  if (a.access === 'missing') d.append(el('div.apps__access', lu('eye-off', 'lu lu--sm'), a.accessNote ?? 'Nutzungsdaten nicht freigegeben – Firebase-Zugriff für den Kollektor fehlt.'));
  if (a.quotas?.length) {
    const b = el('div.bullets');
    for (const q of a.quotas) {
      const pct = typeof q.used === 'number' && typeof q.limit === 'number' && q.limit > 0 ? (100 * q.used) / q.limit : null;
      const f = q.unit === 'bytes' ? (v) => fmt.bytes(v) : (v) => fmt.int(v);
      bulletRow(b, { label: q.label ?? q.id, sub: pct === null ? (q.used === null ? 'nicht gemessen' : 'ohne Kontingent') : `${fmt.pct(pct, 1)} des Kontingents`, value: q.used ?? null, target: q.limit ?? null, max: q.limit ?? null, format: f,
        state: pct === null ? 'unknown' : fillState(pct), valueText: pct === null ? 'keine Daten' : `${f(q.used)} von ${f(q.limit)}`, targetLabel: `Kontingent ${f(q.limit ?? 0)}` });
    }
    d.append(b);
  }
  const meta = [['Kosten', typeof a.costChf === 'number' ? `${fmt.num2(a.costChf)} CHF` : typeof a.costUsdDay === 'number' ? `${fmt.num2(a.costUsdDay)} USD / Tag` : null], ['Fehler / 24 h', typeof a.errors24h === 'number' ? fmt.int(a.errors24h) : null], ['Stand', a.collected ? fmt.ago(a.collected) : null]].filter(([, v]) => v !== null);
  if (meta.length) d.append(dl(meta));
  const checks = a.hosting ?? a.checks ?? [];
  if (checks.length) {
    const t = el('table.table.table--compact');
    t.append(el('thead', el('tr', el('th', 'Adresse'), el('th', 'Antwort'), el('th', 'TLS'), el('th', 'Zustand'))));
    const tb = el('tbody');
    for (const ch of checks) tb.append(el('tr', el('td', { 'data-label': 'Adresse' }, el('span.mono', ch.url ?? '–')), el('td', { 'data-label': 'Antwort' }, ch.status ? `HTTP ${ch.status} · ${fmt.int(ch.ms)} ms` : 'keine Antwort'),
      el('td', { 'data-label': 'TLS' }, typeof ch.tlsDays === 'number' ? `${fmt.int(ch.tlsDays)} Tage${(ch.names ?? []).length ? ` · ${ch.names.join(', ')}` : ''}` : (a.tls && ch === checks[0] ? `${fmt.int(a.tls.daysLeft)} Tage` : '–')),
      el('td', { 'data-label': 'Zustand' }, el('span.row', statePill(ch.state), ch.note ? el('span.small.secondary', { title: ch.note }, shorten(ch.note, 90)) : null))));
    t.append(tb);
    d.append(el('div.table-wrap', t));
  }
  if (a.tls !== undefined && a.tls) d.append(tlsBullets([a]));
  if (Array.isArray(a.errors) && a.errors.length) {
    const ul = el('ul.changes__list');
    for (const e of a.errors.slice(0, 6)) ul.append(el('li.changes__item', mark('warn'), el('span.changes__text', { title: e.message ?? '' }, `${e.name ?? e.key ?? 'Fehler'} · ${shorten(e.message ?? e.status ?? '', 100)}`), el('span.changes__when', e.latest ? fmt.ago(e.latest) : '')));
    d.append(ul);
  }
  return d;
}

function leaf(s) {
  const st = norm(s.state);
  const row = el('div.tree__leaf', { class: `tree__leaf${st === 'off' ? ' tree__leaf--off' : ''}`, role: 'row' });
  row.append(el('span.tree__cell.tree__cell--mark', mark(st)));
  row.append(el('span.tree__cell.tree__cell--name', el('span.tree__leaf-name', { title: s.label ?? s.id }, s.label ?? s.id), el('small', [KIND[s.kind] ?? s.kind, s.unit].filter(Boolean).join(' · '))));
  const detail = [STATES[st].word, s.detail].filter(Boolean).join(' · ');
  row.append(el('span.tree__cell.tree__cell--detail', { title: detail }, detail));
  row.append(el('span.tree__cell.tree__cell--right', { 'data-label': 'Version', 'data-empty': s.version ? null : 'true' }, s.version ? badge(s.version, 'outline') : el('span.muted', '–')));
  row.append(el('span.tree__cell.tree__cell--right.tabular', { 'data-label': 'seit', 'data-empty': s.since ? null : 'true', title: s.since ? fmt.dateTime(s.since) : '' }, s.since ? fmt.ago(s.since).replace('vor ', '') : '–'));
  row.append(el('span.tree__cell.tree__cell--right.mono', { 'data-label': 'Ports', 'data-empty': s.ports?.length ? null : 'true' }, s.ports?.length ? s.ports.join(', ') : '–'));
  if (s.intended === 'off' && st !== 'off') row.querySelector('.tree__cell--detail').append(' ', badge('soll aus sein', 'warning'));
  return row;
}
function rank(s) { return { crit: 4, warn: 3, unknown: 2, ok: 1, off: 0 }[norm(s.state)]; }
function countStates(svcs) { const c = { ok: 0, warn: 0, crit: 0, off: 0, unknown: 0 }; for (const s of svcs) c[norm(s.state)]++; return c; }
function countBadges(c, total) {
  const out = [el('span', `${total}`)];
  for (const k of ['crit', 'warn', 'unknown', 'off']) if (c[k]) out.push(el('span.badge', { class: `badge badge--${k}` }, mark(k), String(c[k])));
  return out;
}

function appCard(a) {
  const c = card({ title: a.label ?? a.id, accent: null, sub: [a.role, platformLabel(a.platform), a.project, a.collected ? `Stand ${fmt.ago(a.collected)}` : null].filter(Boolean).join(' · '), tools: [statePill(a.state)] });
  if (a.stateReason) c.append(el('p.small.secondary.clamp-2', { title: a.stateReason }, shorten(a.stateReason, 160)));
  if (a.platform === 'firebase' || a.quotas) {
    const b = el('div.bullets');
    for (const q of a.quotas ?? []) {
      const pct = typeof q.used === 'number' && typeof q.limit === 'number' && q.limit > 0 ? (100 * q.used) / q.limit : null;
      const f = q.unit === 'bytes' ? (v) => fmt.bytes(v) : (v) => fmt.int(v);
      bulletRow(b, { label: q.label ?? q.id, sub: pct === null ? (q.used === null ? 'nicht gemessen' : 'ohne Kontingent') : `${fmt.pct(pct, 1)} des Kontingents`, value: q.used ?? null, target: q.limit ?? null, max: q.limit ?? null, format: f,
        state: pct === null ? 'unknown' : fillState(pct), valueText: pct === null ? 'keine Daten' : `${f(q.used)} von ${f(q.limit)}`, targetLabel: `Kontingent ${f(q.limit ?? 0)}` });
    }
    c.append(b);
    c.append(dl([['Kosten', typeof a.costChf === 'number' ? `${fmt.num2(a.costChf)} CHF` : typeof a.costUsdDay === 'number' ? `${fmt.num2(a.costUsdDay)} USD / Tag` : null], ['Fehler / 24 h', fmt.int(a.errors24h)], ['Regelprüfungen', a.rulesChecks === null || a.rulesChecks === undefined ? el('span.row', mark('unknown'), 'nicht gemessen') : fmt.int(a.rulesChecks)]]));
  }
  if (a.checks) {
    const t = el('table.table.table--compact');
    t.append(el('thead', el('tr', el('th', 'Adresse'), el('th', 'Antwort'), el('th', 'Zustand'))));
    const tb = el('tbody');
    for (const ch of a.checks) tb.append(el('tr', el('td', { 'data-label': 'Adresse' }, el('span.mono', ch.url)), el('td', { 'data-label': 'Antwort' }, ch.status ? `HTTP ${ch.status} · ${fmt.int(ch.ms)} ms` : 'keine Antwort'),
      el('td', { 'data-label': 'Zustand' }, el('span.row', statePill(ch.state), ch.note ? el('span.small.secondary', { title: ch.note }, shorten(ch.note, 90)) : null))));
    t.append(tb);
    c.append(el('div.table-wrap', t));
  }
  if (a.tls !== undefined) c.append(tlsBullets([a]));
  return c;
}

/** TLS-Restlaufzeit als Bullet: kleiner ist schlechter (invert), Warnschwelle 30 Tage. */
export function tlsBullets(apps) {
  const b = el('div.bullets');
  for (const a of apps) {
    const t = a.tls; const days = t?.daysLeft ?? null;
    const state = days === null ? 'unknown' : days < 14 ? 'crit' : days < 30 || t?.coversWww === false ? 'warn' : 'ok';
    bulletRow(b, { label: `TLS-Zertifikat ${a.label ?? a.id}`, sub: t ? `${t.issuer ?? 'Aussteller unbekannt'} · ${(t.names ?? []).join(', ') || 'keine Namen'}${t.coversWww === false ? ' · www nicht abgedeckt' : ''}${t.expires ? ` · gültig bis ${fmt.date(t.expires)}` : ''}` : 'nicht geprüft',
      value: days, target: 30, max: 90, invert: true, format: (v) => `${fmt.int(v)} Tage`, state,
      valueText: days === null ? 'keine Daten' : `noch ${fmt.int(days)} Tage gültig · Warnschwelle 30 Tage`, targetLabel: 'Warnschwelle 30 Tage', note: 'Erneuerung spätestens 30 Tage vor Ablauf' });
  }
  return b;
}
function shorten(s, n) { s = String(s ?? ''); return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s; }
function numWord(n) { return ['Keine', 'Ein', 'Zwei', 'Drei', 'Vier', 'Fünf', 'Sechs', 'Sieben', 'Acht', 'Neun', 'Zehn'][n] ?? String(n); }

function platformLabel(p) { return p === 'firebase' ? 'Firebase' : p === 'web' ? 'Web (Droplet)' : p === 'service' ? 'Spark-Dienst' : (p ?? '–'); }
