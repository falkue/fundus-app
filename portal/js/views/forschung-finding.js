/**
 * Forschung › Finding (#forschung?f=<id>): the same document pattern as a hypothesis. A finding is a result without a
 * prior hypothesis – "explorativ" once in the meta line, never shown as confirmed. Sections: Befund · Belege ·
 * Interpretation · Verwandt · Technische Details (collapsed). "Hypothese daraus ableiten" creates a linked hypothesis.
 * Data: GET /api/research/findings/<id>; POST {action: 'interpret' | 'derive_hypothesis'}.
 */
import { el, segmented } from '../lib/dom.js';
import { arr, kids, api, put, KIND, STRENGTH_LABEL, FSTATUS, DIRECTION, md, linkify, day, seriesLabel, hypHref, normFinding } from './forschung-lib.js';
import { interpretationSection, relatedList, evidenceList, logList, defs } from './forschung-teile.js';

const LIST_KEY = 'fundus-forschung-liste';
/** "← Forschung", back to the list with the filters the person used last. */
export function backLink() {
  let href = '#forschung';
  try { href = sessionStorage.getItem(LIST_KEY) || href; } catch { /* privat */ }
  return el('p.fo-back', el('a', { href }, '← Forschung'));
}
export const rememberList = (hash) => { try { sessionStorage.setItem(LIST_KEY, hash); } catch { /* privat */ } };

export function render(root, ctx, id, alive) {
  const doc = el('article.fo-doc');
  root.append(doc);
  let f = null;

  async function load() {
    put(doc, el('p.fo-muted', 'Lade …'));
    try {
      f = normFinding(await api(`/findings/${encodeURIComponent(id)}`));
      if (!alive()) return;
      draw();
      document.title = `${f.code ?? 'Finding'} · Forschung · Fundus-Portal`;
    } catch (e) { put(doc, backLink(), el('p', e.status === 404 ? `Finding ${id} gibt es nicht.` : `Nicht ladbar: ${e.message}`)); }
  }

  function draw() {
    const c = f.context ?? {};
    const derived = f.links.filter((l) => l.relation === 'derived_from' && l.direction === 'in');
    const deriveB = f.status !== 'withdrawn' ? el('button.btn', { type: 'button', on: { click: deriveDialog } }, 'Hypothese daraus ableiten') : null;
    put(doc,
      backLink(),
      el('header.fo-doc__head',
        el('p.fo-doc__code', kids(f.code, 'Finding').join(' · ')),
        el('h1', f.title),
        el('p.fo-muted', kids(f.series ? seriesLabel(f.series) : 'ohne Serie', KIND[f.kind], 'explorativ', f.visibility === 'internal' ? 'intern' : null,
          f.status && f.status !== 'recorded' ? FSTATUS[f.status] : null, f.created_at ? `erfasst ${day(f.created_at)}` : null).join(' · ')),
        derived.length ? el('p.fo-muted', 'Daraus abgeleitet: ', derived.map((l, i) => el('span', i ? ', ' : '', el('a', { href: hypHref(l) }, l.code ?? 'Hypothese')))) : null,
        deriveB ? el('div.fo-doc__actions', deriveB) : null),
      el('section.fo-sec', el('h2', 'Befund'),
        f.statement ? el('p', ...linkify(f.statement)) : null,
        defs([['Stärke', STRENGTH_LABEL[f.strength] ?? null], ['Umfeld', c.setting ?? null], ['Stichprobe', c.sample ?? null], ['Zeitraum', c.period ?? null], ['Methode', f.method ?? null]]),
        f.detail ? el('details.fo-fulltext', el('summary', 'Ganzen Text anzeigen'), md(f.detail)) : null),
      el('section.fo-sec', el('h2', 'Belege'), evidenceList(f.evidence)),
      el('section.fo-sec', el('h2', 'Interpretation'), interpretationSection({ interp: f.interpretation, ctx, readOnly: f.status === 'withdrawn',
        save: async (sections) => {
          const r = await api(`/findings/${f.id}`, { action: 'interpret', interpretation: sections, author: 'Falk (Portal)' });
          if (r?.id) { f = normFinding(r); return f.interpretation; }
          return null;
        } })),
      el('section.fo-sec', el('h2', 'Verwandt'), relatedList({ links: f.links.filter((l) => !derived.includes(l)), theories: f.theories })),
      el('details.fo-tech', el('summary', 'Technische Details'), el('div.fo-tech__body',
        defs([['Quelle', f.source ?? null], ['Kennung', f.ref ?? `finding:${f.id}`]]),
        el('div', el('h3', 'Verlauf'), logList(f.log)))));
  }

  // ---------------------------------------------------------------- derive a hypothesis
  function deriveDialog() {
    const dlg = el('dialog.dialog.fo-derive');
    const title = el('input.input', { type: 'text', 'aria-label': 'Titel', placeholder: 'Kurzer Titel' });
    const stmt = el('textarea.input', { rows: 3, 'aria-label': 'Aussage', placeholder: 'Was soll geprüft werden – wer, was, in welche Richtung?' });
    stmt.value = f.statement ?? '';
    let kind = f.kind === 'quantitative' ? 'quantitative' : 'qualitative';
    const kseg = segmented(Object.entries(KIND).map(([value, label]) => ({ value, label })), kind, (v) => { kind = v; }, 'Art');
    const dir = el('select.input', { 'aria-label': 'Erwartung' }, ['+', '-', '≠', 'qualitative'].map((v) => el('option', { value: v }, DIRECTION[v])));
    dir.value = kind === 'qualitative' ? 'qualitative' : '+';
    const msg = el('p.fo-muted', `Die neue Hypothese startet als Idee und verweist auf ${f.code ?? 'dieses Finding'}. Der Plan vor der Auswertung wird danach festgelegt.`);
    const ok = el('button.btn.btn--primary', { type: 'button' }, 'Hypothese anlegen');
    const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
    const close = () => { dlg.close(); dlg.remove(); };
    cancel.addEventListener('click', close);
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });
    ok.addEventListener('click', async () => {
      if (!title.value.trim() || !stmt.value.trim()) { msg.textContent = 'Bitte Titel und Aussage ausfüllen.'; return; }
      ok.disabled = true;
      try {
        const body = { title: title.value.trim(), statement: stmt.value.trim(), kind, direction: dir.value === 'qualitative' ? null : dir.value };
        const r = await api(`/findings/${f.id}`, { action: 'derive_hypothesis', hypothesis: body, ...body });
        close();
        const hid = r?.hypothesis?.id ?? (r?.type === 'hypothesis' || r?.code ? r.id : null);
        ctx.toast(`Hypothese ${r?.hypothesis?.code ?? r?.code ?? ''} angelegt.`, 5000);
        if (hid) location.hash = hypHref(hid); else load();
      } catch (e) { ok.disabled = false; msg.textContent = `Nicht angelegt: ${e.message}`; }
    });
    const row = (l, c) => el('label.fo-form__row', el('span', l), c);
    dlg.append(el('div.dialog__body.fo-form', el('h3', 'Hypothese daraus ableiten'), row('Titel', title), row('Aussage', stmt),
      el('div.fo-form__row', el('span', 'Art'), kseg.el), row('Erwartung', dir), msg, el('div.fo-form__bar', ok, cancel)));
    document.body.append(dlg);
    dlg.showModal();
    title.focus();
  }

  load();
}
