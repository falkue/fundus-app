/**
 * Forschung › Finding (#forschung?f=<id>&t=<reiter>): an exploratory result without a prior hypothesis – always marked
 * "explorativ", never a confirmed result. Header (code, title, statement, kind, strength, status), tabs: Überblick
 * (strength scale, context, method) · Belege (quotes with speaker role, photos/videos, datasets, documents) ·
 * Interpretation (seven sections) · Verbindungen (derived hypotheses, links, theory) · Verlauf.
 * "Hypothese ableiten …" opens a dialog; the new hypothesis keeps the link "entstanden aus Finding …".
 * Data: GET /api/research/findings/<id>; POST {action: 'interpret' | 'derive_hypothesis'}.
 */
import { el, card, segmented, badge } from '../lib/dom.js';
import { arr, kids, hp, api, put, normFinding, md, KIND, STRENGTH, STRENGTH_LABEL, FSTATUS, EVIDENCE, DIRECTION, interpState, kindChip, visChip, interpChip,
  exploratoryChip, linkify, when, day, seriesLabel, hypHref, statusChip } from './forschung-lib.js';
import { interpretationEditor, linksCard, theoryCards, logList, evidenceList, facts } from './forschung-teile.js';

const TABS = [{ value: 'ueberblick', label: 'Überblick' }, { value: 'belege', label: 'Belege' }, { value: 'interpretation', label: 'Interpretation' },
  { value: 'verbindungen', label: 'Verbindungen' }, { value: 'verlauf', label: 'Verlauf' }];

export function render(root, ctx, id, alive) {
  const back = el('a.dk-back', { href: '#forschung?tab=findings' }, '← Findings');
  const head = el('header.fo-head.fo-head--finding');
  const tabsBox = el('div.dk-dtabs');
  const body = el('div.fo-tabbody');
  root.append(el('p', back), head, tabsBox, body);
  let f = null;
  let tab = TABS.find((t) => t.value === hp().get('t'))?.value ?? 'ueberblick';
  const built = new Map();

  async function load() {
    put(head, el('div.dk-skel', el('div.dk-skel__card')));
    try {
      f = await api(`/findings/${encodeURIComponent(id)}`);
      if (!alive()) return;
      normFinding(f);
      built.clear();
      draw();
      document.title = `${f.code ?? 'Finding'} · Forschung · Fundus-Portal`;
    } catch (e) { put(head, el('div.alert.alert--error', e.status === 404 ? `Finding ${id} gibt es nicht.` : `Nicht ladbar: ${e.message}`)); }
  }

  function draw() {
    drawHead();
    const ist = interpState(f.interpretation);
    const seg = segmented(TABS.map((t) => ({ ...t, count: t.value === 'belege' ? String(f.evidence.length)
      : t.value === 'interpretation' && !ist.complete ? (ist.filled ? `${ist.done}/${ist.need}` : 'fehlt') : undefined })), tab, showTab, 'Reiter');
    seg.el.classList.add('dk-dtabs__seg');
    put(tabsBox, seg.el);
    showTab(tab);
  }
  function showTab(v) {
    tab = v;
    const p = hp(); p.set('f', id);
    if (v === 'ueberblick') p.delete('t'); else p.set('t', v);
    const s = `#forschung?${p}`;
    if (location.hash !== s) history.replaceState(null, '', s);
    if (!built.has(v)) {
      try { built.set(v, { ueberblick: overviewTab, belege: evidenceTab, interpretation: interpTab, verbindungen: linksTab, verlauf: logTab }[v]()); }
      catch (e) { built.set(v, el('div.alert.alert--error', `Reiter nicht darstellbar: ${e.message}`)); }
    }
    put(body, built.get(v));
  }

  function derived() { return f.links.filter((l) => l.relation === 'derived_from' && (l.direction === 'in' || l.type === 'hypothesis')); }

  function drawHead() {
    const ist = interpState(f.interpretation);
    const deriveB = f.status !== 'withdrawn' ? el('button.btn', { type: 'button', on: { click: deriveDialog } }, 'Hypothese ableiten …') : null;
    const dh = derived();
    put(head,
      el('div.fo-head__top', el('span.label', kids('Finding', f.code, seriesLabel(f.series)).join(' · ')),
        el('div.row.fo-head__chips', kindChip(f.kind), el('span.fo-status', FSTATUS[f.status] ?? f.status), visChip(f.visibility), deriveB)),
      el('h1.fo-head__title', f.code ? el('span.fo-head__code.fo-head__code--finding', f.code) : null, f.title),
      f.statement && f.statement !== f.title ? el('p.fo-head__stmt', ...linkify(f.statement)) : null,
      el('div.fo-head__row', exploratoryChip(), strengthScale(f.strength), el('button.fo-ipbtn', { type: 'button', on: { click: () => { tab = 'interpretation'; draw(); } } }, interpChip(ist.state))),
      el('p.small.fo-explo-note', 'Explorativ: ohne vorherige Hypothese gefunden. Ein Finding wird nie zu einer bestätigten Hypothese – es kann aber eine Hypothese begründen, die dann präregistriert geprüft wird.'),
      dh.length ? el('p.small.fo-head__from', 'Daraus abgeleitet: ', dh.map((l, i) => el('span', i ? ', ' : '', el('a', { href: hypHref(l) }, l.code ?? `Hypothese ${l.id}`), l.status ? el('span.muted', ` (${statusLabel(l.status)})`) : null))) : null,
      f.status === 'withdrawn' ? el('div.alert.fo-alert', el('strong', 'Zurückgezogen'), f.withdrawn_reason ? el('span', ` – ${f.withdrawn_reason}`) : null) : null);
  }

  function overviewTab() {
    const c = f.context ?? {};
    const counts = {};
    for (const e of f.evidence) counts[e.kind] = (counts[e.kind] ?? 0) + 1;
    return el('div.fo-stack',
      el('div.fo-two',
        card({ title: 'Kontext', accent: 'Kontext' }, facts([['Setting', c.setting ?? null], ['Stichprobe', c.sample ?? null], ['Zeitraum', c.period ?? null],
          ['Projekt', f.project ?? null], ['Erfasst', `${day(f.created_at)}${f.updated_at && f.updated_at !== f.created_at ? ` · geändert ${day(f.updated_at)}` : ''}`]])),
        card({ title: 'Methode', accent: 'Methode' }, f.method ? el('p', ...linkify(f.method)) : el('p.small.muted', 'Keine Angabe.'),
          el('div.fo-strength-legend', el('span.label', 'Stärke des Befunds'), strengthScale(f.strength, true)))),
      f.detail ? card({ title: 'Ausführlich', accent: 'Ausführlich', sub: 'Text des Befunds, wie er abgelegt wurde.' }, md(f.detail)) : null,
      card({ title: 'Belege im Überblick', accent: 'Belege', tools: el('button.btn.btn--small', { type: 'button', on: { click: () => { tab = 'belege'; draw(); } } }, 'Alle Belege') },
        Object.keys(counts).length ? el('div.fo-evsum', Object.entries(counts).map(([k, n]) => el('span.fo-chip', `${n} ${n === 1 ? (EVIDENCE[k] ?? k) : (EVIDENCE_PL[k] ?? EVIDENCE[k] ?? k)}`))) : el('p.small.muted', 'Noch keine Belege.'),
        f.evidence.filter((e) => e.kind === 'quote').slice(0, 1).map((q) => el('figure.fo-quote.fo-quote--lead', el('blockquote', `„${String(q.quote ?? '').replace(/^[„"]|[“"]$/g, '')}“`), el('figcaption', el('span.fo-quote__who', q.speaker ?? 'Rolle nicht angegeben'))))));
  }

  function evidenceTab() { return el('div.fo-stack', evidenceList(f.evidence, { empty: 'Noch keine Belege. Aus der Session: fundus_finding(action="evidence", …).' })); }

  function interpTab() {
    return el('div.fo-stack', interpretationEditor({ interp: f.interpretation, history: f.interpretations ?? (f.interpretation ? [f.interpretation] : []), ctx,
      readOnly: f.status === 'withdrawn',
      save: async (sections) => {
        const r = await api(`/findings/${f.id}`, { action: 'interpret', interpretation: sections, sections, author: 'Falk (Portal)' });
        if (r && r.id) { f = normFinding(r); built.delete('verlauf'); drawHead(); return f.interpretation; }
        return null;
      } }));
  }

  function linksTab() { return el('div.fo-stack', linksCard(f.links), el('h3.fo-subhead', 'Theorie'), theoryCards(f.theories)); }
  function logTab() { return el('div.fo-stack', card({ title: 'Verlauf', accent: 'Verlauf' }, logList(f.log))); }

  // ---------------------------------------------------------------- derive a hypothesis
  function deriveDialog() {
    const dlg = el('dialog.dialog.fo-derive');
    const title = el('input.input', { type: 'text', 'aria-label': 'Titel', placeholder: 'Kurzer Titel der Hypothese' });
    const stmt = el('textarea.input', { rows: 3, 'aria-label': 'Aussage', placeholder: 'Prüfbare Aussage: wer, was, in welche Richtung, unter welchen Bedingungen?' });
    stmt.value = f.statement ?? '';
    let kind = f.kind === 'quantitative' ? 'quantitative' : 'qualitative';
    const kseg = segmented(Object.entries(KIND).map(([value, label]) => ({ value, label })), kind, (v) => { kind = v; }, 'Art');
    const dir = el('select.input', { 'aria-label': 'Richtung' }, ['+', '-', '≠', 'qualitative'].map((v) => el('option', { value: v }, DIRECTION[v])));
    dir.value = kind === 'qualitative' ? 'qualitative' : '+';
    const msg = el('p.small.muted', 'Die neue Hypothese startet als Idee mit der Verbindung „entstanden aus ' + (f.code ?? 'diesem Finding') + '“. Präregistriert wird sie danach in einer Claude-Session – vor jeder Analyse. Das Finding bleibt explorativ.');
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Hypothese anlegen');
    const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener('click', close);
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
    ok.addEventListener('click', async () => {
      if (!title.value.trim() || !stmt.value.trim()) { msg.textContent = 'Bitte Titel und Aussage ausfüllen.'; return; }
      ok.disabled = true;
      try {
        const r = await api(`/findings/${f.id}`, { action: 'derive_hypothesis', hypothesis: { title: title.value.trim(), statement: stmt.value.trim(), kind, direction: dir.value === 'qualitative' ? null : dir.value }, title: title.value.trim(), statement: stmt.value.trim(), kind, direction: dir.value });
        close();
        const hid = r?.hypothesis?.id ?? (r?.type === 'hypothesis' || r?.code ? r.id : null);
        ctx.toast(`Hypothese ${r?.hypothesis?.code ?? r?.code ?? ''} angelegt – als Idee, noch nicht präregistriert.`, 5000);
        if (hid) location.hash = hypHref(hid); else load();
      } catch (e) { ok.disabled = false; msg.textContent = `Nicht angelegt: ${e.message}`; }
    });
    dlg.append(el('div.dialog__body', el('h3', `Hypothese aus ${f.code ?? 'Finding'} ableiten`),
      el('label.ds-row', el('span.label', 'Titel'), title), el('label.ds-row', el('span.label', 'Aussage'), stmt),
      el('div.ds-grid', el('div.ds-row', el('span.label', 'Art'), kseg.el), el('label.ds-row', el('span.label', 'Richtung'), dir)),
      msg, el('div.row.dialog__actions', cancel, ok)));
    document.body.append(dlg);
    dlg.showModal();
    title.focus();
  }

  load();
}

const EVIDENCE_PL = { dataset: 'Datensätze', observation: 'Beobachtungen', document: 'Dokumente', exhibit: 'Folien', photo: 'Fotos', video: 'Videos', quote: 'Zitate', note: 'Notizen', file: 'Dateien' };
const statusLabel = (s) => ({ idea: 'Idee', preregistered: 'präregistriert', running: 'läuft', result: 'Ergebnis', revised: 'überarbeitet', withdrawn: 'zurückgezogen' }[s] ?? s);

/** Ordinal scale of a finding's strength: four steps, filled up to the current one (neutral colour, no ranking hue). */
export function strengthScale(strength, withLabels = false) {
  const i = STRENGTH.indexOf(strength);
  return el('span.fo-strength', { class: `fo-strength${withLabels ? ' fo-strength--full' : ''}`, title: `Stärke: ${STRENGTH_LABEL[strength] ?? 'unbekannt'}`, role: 'img',
    'aria-label': `Stärke: ${STRENGTH_LABEL[strength] ?? 'unbekannt'} (Stufe ${i + 1} von ${STRENGTH.length})` },
  el('span.fo-strength__steps', STRENGTH.map((s, k) => el('span.fo-strength__step', { class: `fo-strength__step${k <= i ? ' is-on' : ''}${k === i ? ' is-cur' : ''}` },
    withLabels ? el('span.fo-strength__l', STRENGTH_LABEL[s]) : null))),
  withLabels ? null : el('span.fo-strength__word', STRENGTH_LABEL[strength] ?? '–'));
}
export { statusChip, badge };
