/**
 * Verschieben und Löschen von Folien, Foliensätzen und Projekten im Portal (fundus/trash.py).
 * moveDialog: Ziel wählen – für Folien ein Foliensatz (Projekt, dann Satz), für einen Foliensatz ein Projekt (auch ein
 * neues), für ein Projekt das Projekt, in das es übernommen wird. trashAsk: Bestätigung „in den Papierkorb“.
 * Ausführen: POST /api/exhibits/move {target, refs, to} bzw. POST /api/trash {action: 'trash', target, refs}.
 */
import { el, headline, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const pname = (p) => p.title || p.project;

async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}

/**
 * mode 'slides' | 'set' | 'project'; projects: Übersicht (GET /api/exhibits).projects; from: {project, set}.
 * Ergebnis: Ziel als String ('project/set-slug' für Folien, Projekt-Kennung sonst) oder null.
 */
export function moveDialog({ mode, projects, from, what }) {
  return new Promise((resolve) => {
    const dlg = el('dialog.dialog.mv-dialog');
    const others = arr(projects).filter((p) => mode === 'slides' || p.project !== from.project);
    const psel = el('select.input', { 'aria-label': 'Projekt' }, others.map((p) => el('option', { value: p.project }, `${pname(p)}${p.title ? ` (${p.project})` : ''}`)),
      mode === 'set' ? el('option', { value: '__new' }, '+ neues Projekt …') : null);
    psel.value = mode === 'slides' ? from.project : (others[0]?.project ?? '__new');
    const newp = el('input.input', { type: 'text', placeholder: 'Kennung des neuen Projekts, z. B. hsg-ai-studio', hidden: true, 'aria-label': 'Neues Projekt' });
    const ssel = el('select.input', { 'aria-label': 'Foliensatz' });
    const fillSets = () => {
      const p = arr(projects).find((x) => x.project === psel.value);
      ssel.replaceChildren(...arr(p?.sets).map((s) => el('option', { value: s.slug, disabled: mode === 'slides' && p.project === from.project && String(s.id) === String(from.set) },
        `${s.title || s.slug}${s.is_default ? ' (Hauptfoliensatz)' : ''}${s.locked ? ' – gesperrt' : ''}`)));
      const first = [...ssel.options].find((o) => !o.disabled);
      if (first) ssel.value = first.value;
    };
    psel.addEventListener('change', () => { newp.hidden = psel.value !== '__new'; if (mode === 'slides') fillSets(); });
    if (mode === 'slides') fillSets();
    newp.hidden = psel.value !== '__new';
    const ok = el('button.btn.btn--primary', { type: 'button' }, mode === 'project' ? 'Übernehmen' : 'Verschieben');
    const no = el('button.btn', { type: 'button' }, 'Abbrechen');
    const title = { slides: `${what} verschieben`, set: 'Foliensatz in ein anderes Projekt verschieben', project: 'Projekt in ein anderes übernehmen' }[mode];
    const text = {
      slides: 'Die Folien wechseln in den gewählten Foliensatz, auch in ein anderes Projekt. Die Dateien bleiben im Projektordner; wird die Folie dort neu gerendert, landet die neue Version am neuen Ort.',
      set: 'Der Foliensatz mit allen Folien und Decks wechselt das Projekt. Neue Renders im alten Ordner landen weiterhin in diesem Foliensatz.',
      project: 'Alle Foliensätze wechseln ins gewählte Projekt; die Folien des Hauptfoliensatzes kommen in dessen Hauptfoliensatz. Das Projekt verschwindet danach aus der Liste.',
    }[mode];
    dlg.append(el('div.dialog__body', headline('h3', title), el('p.secondary', text),
      el('label.mv-row', el('span.label', mode === 'project' ? 'Übernehmen in' : 'Projekt'), psel, newp),
      mode === 'slides' ? el('label.mv-row', el('span.label', 'Foliensatz'), ssel) : null,
      el('div.row.dialog__actions', no, ok)));
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener('click', () => {
      if (mode === 'slides') return ssel.value ? done(`${psel.value}/${ssel.value}`) : null;
      const p = psel.value === '__new' ? newp.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') : psel.value;
      return p ? done(p) : newp.focus();
    });
    no.addEventListener('click', () => done(null));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal();
    psel.focus();
  });
}

/** Bestätigen und in den Papierkorb legen; gibt die Antwort des Servers zurück oder null. */
export async function trashAsk(ctx, { target, refs, label }) {
  const t = { slide: refs.length > 1 ? `${fmt.int(refs.length)} Folien` : 'Folie', set: 'Foliensatz', project: 'Projekt' }[target];
  const extra = { slide: '', set: ' mit allen Folien und Decks', project: ' mit allen Foliensätzen, Folien und Decks (die Belege des Projekts – Datenpunkte, Dokumente – bleiben)' }[target];
  if (!await confirmDialog({ title: `${t} in den Papierkorb?`, text: `„${label}“${extra} kommt in den Papierkorb und verschwindet aus Suche, Decks und gespeicherten Suchen. Nach 30 Tagen wird es endgültig gelöscht; bis dahin lässt es sich im Papierkorb wiederherstellen.`, confirmLabel: 'In den Papierkorb' })) return null;
  try {
    const r = await post('/api/trash', { action: 'trash', target, refs });
    ctx.toast(`${t} im Papierkorb – wiederherstellen unter „Papierkorb“.`, 5000);
    ctx.refreshNav?.();
    return r;
  } catch (e) { ctx.toast(`Nicht gelöscht: ${e.message}`, 7000); return null; }
}

export async function moveRun(ctx, { target, refs, to }) {
  try {
    const r = await post('/api/exhibits/move', { target, refs, to });
    ctx.toast(target === 'slide' ? `${fmt.int(arr(r.moved).length)} Folien verschoben nach ${to}.` : `Verschoben nach ${to}.`, 5000);
    ctx.refreshNav?.();
    return r;
  } catch (e) { ctx.toast(`Nicht verschoben: ${e.message}`, 7000); return null; }
}
