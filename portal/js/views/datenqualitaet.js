/**
 * Ansicht „Datenqualität“ (#datenqualitaet, erreichbar aus der Fundus-Statistik): Befunde des Aufräumens, die Falk
 * entscheidet (fundus/cleanup.py). Je Befund die Datensätze nebeneinander; der zu behaltende ist vorgewählt (geprüft vor
 * ungeprüft, in einer Reihe, älter) und lässt sich umstellen.
 * Wahrscheinliche Dubletten: Zusammenführen | Keine Dublette (wird nie wieder vorgeschlagen) | Später.
 * Reihen ohne Werte: Löschen | Behalten. Widersprüche: Als umstritten markieren | Ignorieren.
 * Daten: GET /api/cleanup?view=candidates&class=…&state=…; POST /api/cleanup {action:'decide', ids, decision, keep}.
 */
import { el, pageHead, emptyNote, segmented, mark } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const KIND = { observation: 'Datenpunkt', document: 'Dokument', publisher: 'Herausgeber', series: 'Reihe', quote: 'Zitat',
  practice: 'Praxisbeispiel', fact: 'Fakt', forecast: 'Prognose', study: 'Studie' };
const FIELD = { metric: 'Kennzahl', value: 'Wert', value_text: 'Wert (Text)', unit: 'Einheit', period: 'Zeitraum', geo: 'Region',
  category: 'Kategorie', population: 'Grundgesamtheit', status: 'Prüfstand', series_id: 'Reihe', title: 'Titel', url: 'URL',
  publisher_id: 'Herausgeber', published: 'Erschienen', doi: 'DOI', kind: 'Art', name: 'Name', type: 'Typ', country: 'Land',
  frequency: 'Rhythmus', text: 'Text', speaker: 'Sprecher', organisation: 'Organisation', what: 'Was', statement: 'Aussage',
  target_period: 'Zieljahr', finding: 'Befund' };
const STATUS_DE = { verified: 'geprüft', unverified: 'ungeprüft', disputed: 'umstritten', retracted: 'zurückgezogen' };
const TABS = [{ value: 'probable', label: 'Wahrscheinliche Dubletten' }, { value: 'contradiction', label: 'Widersprüche' },
  { value: 'later', label: 'Später' }, { value: 'rejected', label: 'Keine Dublette' }];

async function api(body) {
  const r = await fetch('/api/cleanup', body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}

export function render(root, ctx) {
  let alive = true;
  let tab = new URLSearchParams(location.hash.split('?')[1] ?? '').get('t') || 'probable';
  const list = el('div.dq-list');
  const info = el('p.small.muted');
  const tabs = segmented(TABS, tab, (v) => { tab = v; history.replaceState(null, '', `#datenqualitaet?t=${v}`); load(); }, 'Auswahl');
  root.append(pageHead({ title: 'Datenqualität', accent: 'Datenqualität',
    lede: 'Befunde des Aufräumens, über die du entscheidest. Sichere Korrekturen (Schreibvarianten, eindeutige Dubletten) laufen gesammelt aus der Fundus-Statistik. Zusammengeführte Datensätze leben unter der Kennung des behaltenen weiter; Folien behalten ihre Belege.',
    tools: [el('a.btn', { href: '#statistik' }, '← Fundus-Statistik')] }));
  root.append(el('div.row.dq-bar', tabs.el, info), list);

  async function load() {
    info.textContent = 'Lade …';
    const p = new URLSearchParams({ view: 'candidates', limit: '100' });
    if (tab === 'later' || tab === 'rejected') p.set('state', tab); else { p.set('class', tab); p.set('state', 'open'); }
    try {
      const r = await fetch(`/api/cleanup?${p}`, { cache: 'no-store' }).then((x) => x.json());
      if (!alive) return;
      if (r.error) throw new Error(r.error);
      info.textContent = `${fmt.int(r.total)} Befunde${r.total > arr(r.items).length ? ` (die ersten ${arr(r.items).length})` : ''}`;
      list.replaceChildren(...(arr(r.items).length ? arr(r.items).map(group)
        : [emptyNote(tab === 'probable' ? 'Keine offenen Vorschläge – alles entschieden.' : tab === 'contradiction' ? 'Keine Widersprüche.' : 'Nichts hier.')]));
    } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
  }

  async function decide(g, decision, keep, box) {
    box.classList.add('dq-group--busy');
    try {
      const r = await api({ action: 'decide', ids: [g.id], decision, keep });
      if (r.job) await waitJob();
      ctx.toast({ merge: 'Zusammengeführt.', delete: 'Gelöscht.', dispute: 'Als umstritten markiert.', reject: 'Gemerkt: keine Dublette.', later: 'Auf später gelegt.', reopen: 'Wieder offen.' }[decision] ?? 'Erledigt.');
      box.remove();
      if (!list.children.length) load();
    } catch (e) { box.classList.remove('dq-group--busy'); ctx.toast(`Nicht ausgeführt: ${e.message}`, 6000); }
  }
  async function waitJob() {
    for (;;) {
      const s = await api();
      const j = s.job ?? {};
      if (j.state === 'running') { await new Promise((ok) => setTimeout(ok, 700)); continue; }
      if (j.state === 'failed') throw new Error(j.error);
      if (j.result?.failed?.length) throw new Error(j.result.failed[0].error);
      return j.result;
    }
  }

  function group(g) {
    const box = el('section.card.dq-group', { 'aria-label': g.reason });
    const recs = arr(g.records);
    let keep = g.keep;
    const head = el('div.dq-head', el('span.badge', KIND[g.kind] ?? g.kind), el('strong', g.reason),
      g.class === 'contradiction' && g.detail?.values ? el('span.small.muted', ` – Werte ${g.detail.values.map((v) => fmt.num1(v)).join(' / ')} ${g.detail.unit ?? ''}`) : null);
    const fields = [...new Set(recs.flatMap((r) => Object.keys(r).filter((k) => FIELD[k])))];
    const radios = g.class === 'probable' && g.action !== 'delete' && recs.length > 1;
    const table = el('div.dq-table');
    table.style.setProperty('--dq-cols', String(Math.max(1, recs.length)));
    table.append(el('span.dq-cell.dq-cell--h', ''), ...recs.map((r) => {
      const lab = el('span.dq-cell.dq-cell--h', `${KIND[g.kind] ?? g.kind} ${r.id}`);
      if (radios) {
        const rb = el('input', { type: 'radio', name: `keep-${g.id}`, value: String(r.id), checked: String(r.id) === String(keep), 'aria-label': `${r.id} behalten` });
        rb.addEventListener('change', () => { keep = String(r.id); });
        lab.prepend(el('label.dq-keep', rb, ' behalten '));
      }
      return lab;
    }));
    for (const f of fields) {
      const vals = recs.map((r) => (r[f] === undefined ? '' : String(r[f])));
      const differs = new Set(vals).size > 1;
      table.append(el('span.dq-cell.dq-cell--k', FIELD[f]), ...recs.map((r) => {
        const v = r[f];
        const txt = v === undefined || v === null ? '–' : f === 'status' ? (STATUS_DE[v] ?? v) : String(v);
        return el('span.dq-cell', { class: `dq-cell${differs ? ' dq-cell--diff' : ''}`, title: txt },
          f === 'url' && v ? el('a', { href: v, target: '_blank', rel: 'noopener' }, txt) : txt);
      }));
    }
    const more = [];
    if (recs.some((r) => r.document)) {
      table.append(el('span.dq-cell.dq-cell--k', 'Quelle'), ...recs.map((r) => el('span.dq-cell', { title: r.document?.title ?? '' },
        r.document ? (r.document.url ? el('a', { href: r.document.url, target: '_blank', rel: 'noopener' }, r.document.title) : r.document.title) : '–')));
    }
    if (recs.some((r) => r.slides)) {
      table.append(el('span.dq-cell.dq-cell--k', 'Auf Folien'), ...recs.map((r) => el('span.dq-cell', r.slides ? `${r.slides}` : '–')));
      more.push(el('p.small.muted', mark('info'), ' Folien, die einen zusammengeführten Datensatz zeigen, verweisen danach auf den behaltenen.'));
    }
    if (g.count > recs.length) more.push(el('p.small.muted', `… und ${g.count - recs.length} weitere`));
    const btn = (label, decision, primary) => {
      const b = el(primary ? 'button.btn.btn--primary' : 'button.btn', { type: 'button' }, label);
      b.addEventListener('click', () => decide(g, decision, decision === 'merge' ? keep : undefined, box));
      return b;
    };
    const actions = el('div.row.dq-actions');
    if (g.state === 'rejected' || g.state === 'later') actions.append(btn('Wieder öffnen', 'reopen', true));
    else if (g.class === 'contradiction') actions.append(btn('Als umstritten markieren', 'dispute', true), btn('Ignorieren', 'reject'), btn('Später', 'later'));
    else if (g.action === 'delete') actions.append(btn('Löschen', 'delete', true), btn('Behalten', 'reject'), btn('Später', 'later'));
    else actions.append(btn('Zusammenführen', 'merge', true), btn('Keine Dublette', 'reject'), btn('Später', 'later'));
    box.append(head, el('div.dq-scroll', table), ...more, actions);
    return box;
  }

  load();
  return () => { alive = false; };
}
