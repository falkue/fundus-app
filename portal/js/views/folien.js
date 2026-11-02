/**
 * Bereich „Folien“ des Fundus-Portals: Folien und Grafiken je Projekt und Foliensatz.
 * Daten: GET /api/exhibits (Projekte, Foliensätze), GET /api/exhibits?project=p[&set=id] (Folien, Decks),
 * GET /api/exhibits/<id> (Versionen); Sichtbarkeit: POST /api/exhibits/sharing {project|set|id, sharing}.
 *
 * Sichtbarkeit für andere Projekte, drei Ebenen: Folie > Foliensatz > Projekt > Grundregel des Archivs.
 * „für alle Projekte“ = andere Projekte und Sitzungen finden die Folie in der Fundus-Suche und dürfen sie übernehmen;
 * „nur im Projekt“ = nur das eigene Projekt sieht sie. Ohne eigene Einstellung erbt jede Ebene die nächsthöhere.
 *
 * Reihenfolge: gibt es geordnete Decks (fundus/decks.py: von der Session mitgegeben oder aus dem Deck-PDF gelesen),
 * zeigt die Ansicht das neueste davon in seiner Folge; sonst alle Folien, neueste zuerst. Auswahl: „seq:<id>“ (geordnetes
 * Deck), „deck:<ordner>“ (Deck-Ordner mit nummerierten Kopien), „all“ (neueste zuerst), leer = Standard.
 * Auswahl (Projekt, Foliensatz, Reihenfolge) steht im Hash (#folien?p=…&set=…&deck=…); Projekt und Foliensatz wählt man in der
 * Seitenleiste (schmal: Foliensatz zusätzlich hier). Eigene Namen für Projekt und Foliensatz: POST /api/names – die
 * Kennungen (Projekt-Slug, Foliensatz-Id/Slug) bleiben. Bilder der Grossansicht laden mit src=gallery: Blättern im Portal
 * zählt in Fundus nicht als Wiederverwendung.
 *
 * Prüfungen: Folien lassen sich mit dem Kästchen an der Kachel markieren (Umschalt-Klick: Bereich; der Browser merkt sich die
 * Auswahl je Projekt). „Mit Gemini prüfen“ und „Personen einladen“ geben die Auswahl, das Deck oder den Foliensatz frei
 * (review-ui.js). Kacheln zeigen offene Feedback-Punkte (GET /api/feedback/counts), die Grossansicht Feedback, Quellen,
 * Datenpunkte mit Prüfstufe und das Änderungsprotokoll (GET /api/slide-review/<id>).
 *
 * Sperre (fundus/locks.py; früher „Archiv-Sperre“ genannt): ein Foliensatz (Einstellungen) oder eine Folie (Grossansicht) lässt sich sperren –
 * dann keine neue Version, kein Verschieben, keine Änderung an Ordnern, Name und Deck-Reihenfolge. Entsperren nur hier
 * mit Bestätigung (Kennung eintippen): POST /api/locks {action: lock|unlock, set|id, confirm}. Kopien sind frei.
 *
 * Verschieben und Löschen (move-ui.js, fundus/trash.py): markierte Folien und die Folie der Grossansicht in einen anderen
 * Foliensatz (auch eines anderen Projekts) oder in den Papierkorb; Foliensatz und Projekt in den Einstellungen.
 *
 * Archiv (fundus/archive.py, Ansicht #archiv): archivierte Folien fehlen in dieser Ansicht, bleiben aber über Suche,
 * Foliensuche und Decks auffindbar. Archivieren: markierte Folien, Grossansicht, Foliensatz/Projekt in den Einstellungen.
 */
import { el, pageHead, card, emptyNote, dl, segmented, badge, mark, icon, fullscreenToggle, dialogKeys } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { openReviewDialog, showCreated, slidePanel, stageChip, stageWhy, progressRow, REVIEW_STATUS } from './review-ui.js';
import { anchor, shapeNode, newSvg, isShape } from './shapes.js';
import { STAGE_KEYS, STAGE_NAME, stageKey, symbol, tileSymbols } from './stage-icons.js';
import { moveDialog, trashAsk, moveRun } from './move-ui.js';
import { favButton } from './fav.js';
import { openShareDialog } from './cloud-ui.js';

const KIND = { chart: 'Chart', slide: 'Folie', section: 'Abschnitt', poster: 'Poster' };
const KIND_FILTER = [{ value: 'all', label: 'Alle' }, { value: 'chart', label: 'Charts' }, { value: 'slide', label: 'Folien' }, { value: 'section', label: 'Abschnitte' }, { value: 'poster', label: 'Poster' }];
const DENSITY = { OK: ['ok', 'Dichte OK'], CHECK: ['warn', 'Dichte prüfen'], SPLIT: ['crit', 'aufteilen'] };
const POLICY = { share_everything: 'alles für alle Projekte', internal_default: 'nur im Projekt, ausser freigegeben', auto_sources: 'nach Quellen' };
const STATUS = { draft: 'Entwurf', final: 'final', presented: 'präsentiert' };
const PALETTE = { hsg: 'HSG-Grün', blue: 'Blau', zhaw: 'Blau mit ZHAW-Logo' };
const VIS = { public: 'für alle Projekte sichtbar', internal: 'nur im Projekt sichtbar' };
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const arr = (v) => (Array.isArray(v) ? v : []);
/** replaceChildren() schreibt null als Text – Lücken vorher entfernen. */
const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);
const withSrc = (u) => (u ? `${u}${u.includes('?') ? '&' : '?'}src=gallery` : null);

async function api(path, body, base = '/api/exhibits') {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : { cache: 'no-store' };
  const r = await fetch(`${base}${path}`, opt);
  let data = null; try { data = await r.json(); } catch { /* kein JSON */ }
  if (!r.ok) throw new Error(data?.error ?? `HTTP ${r.status}`);
  return data;
}
function hashParams() { return new URLSearchParams((location.hash.split('?')[1]) ?? ''); }
function setHash(p) {
  const q = new URLSearchParams(Object.entries(p).filter(([, v]) => v !== null && v !== undefined && v !== ''));
  history.replaceState(null, '', `#folien${q.toString() ? `?${q}` : ''}`);
}
const nameOf = (p) => p?.title || p?.project || '';
const setName = (s) => s?.title || s?.slug || '';
const lockWhen = (l) => (l?.locked_at ? fmt.dateTime(l.locked_at.replace(' ', 'T')) : '');
/** Sperren mit einem Klick; Entsperren fragt nach der Kennung (bewusste Entscheidung, nicht aus Versehen). */
async function toggleLock(ctx, { set, id, slug, locked, what }) {
  let body = { action: locked ? 'unlock' : 'lock', ...(set ? { set } : { id }) };
  if (locked) {
    const typed = window.prompt(`${what} entsperren? Danach kann sie wieder verändert, verschoben oder neu gerendert werden.\nZum Bestätigen die Kennung „${slug}“ eintippen:`);
    if (typed === null) return false;
    if (typed.trim() !== slug) { ctx.toast('Kennung stimmt nicht – nichts geändert.', 4000); return false; }
    body = { ...body, confirm: typed.trim() };
  }
  try {
    await api('', body, '/api/locks');
    ctx.toast(`${what} ${locked ? 'entsperrt' : 'gesperrt'}.`);
    return true;
  } catch (e) { ctx.toast(`Nicht geändert: ${e.message}`, 5000); return false; }
}

/** Text in die Zwischenablage. Das Portal läuft über http im Tailnet (kein sicherer Kontext): dann fehlt
 *  navigator.clipboard, und der klassische Weg über ein unsichtbares Textfeld (execCommand) übernimmt. */
async function copyText(text) {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; } } catch { /* Rückfall */ }
  // iPad/Safari: nicht readonly (sonst keine Auswahl), 16 px (sonst zoomt Safari), fokussiert vor der Auswahl
  const ta = el('textarea', { style: { position: 'fixed', top: '0', left: '0', width: '1px', height: '1px', opacity: '0', fontSize: '16px' } });
  ta.value = text;
  (document.querySelector('dialog[open]') ?? document.body).append(ta);   // im offenen Dialog, sonst blockiert ihn showModal
  ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}
/** Kennung mit Kopier-Symbol: kopiert die eindeutige Referenz projekt/foliensatz/folie (für Sessions und fundus_exhibit). */
function refWithCopy(ctx, shown, ref) {
  const b = el('button.fg-copy', { type: 'button', title: `Kennung kopieren: ${ref}`, 'aria-label': `Kennung ${ref} kopieren` }, icon('ic-copy', 'fg-copy__ic'));
  b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const ok = await copyText(ref);
    ctx.toast(ok ? `Kennung kopiert: ${ref}` : `Nicht kopiert – Kennung: ${ref}`, ok ? 2500 : 6000);
    if (ok) { b.classList.add('is-done'); setTimeout(() => b.classList.remove('is-done'), 1200); }
  });
  return el('span.fg-ref', el('span', shown), b);
}

/** Feld für einen eigenen Namen; speichert mit Enter oder „Speichern“. onSave(text) wirft bei Fehlern. */
function nameField(value, placeholder, onSave, label) {
  const input = el('input.input.fg-name__input', { type: 'text', value: value ?? '', placeholder, maxlength: 120, 'aria-label': label });
  const btn = el('button.btn.fg-name__save', { type: 'button', disabled: true }, 'Speichern');
  let saved = value ?? '';
  const dirty = () => input.value.trim().replace(/\s+/g, ' ') !== saved;
  input.addEventListener('input', () => { btn.disabled = !dirty(); });
  const save = async () => {
    if (!dirty()) return;
    btn.disabled = true;
    try { await onSave(input.value.trim().replace(/\s+/g, ' ')); saved = input.value.trim().replace(/\s+/g, ' '); }
    catch { btn.disabled = !dirty(); }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } if (e.key === 'Escape') { input.value = saved; btn.disabled = true; } });
  btn.addEventListener('click', save);
  return el('div.fg-name', input, btn);
}

/** Auswahl für eine Sichtbarkeits-Ebene; inheritLabel nennt, was ohne eigene Einstellung gilt. */
function sharingControl(value, inheritLabel, onChange, label) {
  const opts = [{ value: 'inherit', label: inheritLabel }, { value: 'shared', label: 'für alle Projekte' }, { value: 'project', label: 'nur im Projekt' }];
  let cur = value ?? 'inherit';
  const seg = segmented(opts, cur, async (v) => {
    if (v === cur) return;
    try { await onChange(v); cur = v; } catch { seg.set(cur); }   // Fehler meldet onChange selbst (Toast)
  }, label);
  return seg;
}

export function render(root, ctx) {
  let alive = true;
  const head = pageHead({ title: 'Folien und Grafiken', accent: 'Folien', lede: 'Lade Projekte aus Fundus …' });
  const crumb = el('span.label.fg-crumb', 'Projekt');
  head.prepend(crumb);
  const tools = el('div.fg-tools');
  // Name und Sichtbarkeit (Projekt, Foliensatz) – selten gebraucht, darum im Dialog „Einstellungen“ statt auf der Seite
  const share = el('div.fg-share');
  const settingsDlg = el('dialog.dialog.fg-settings', { 'aria-label': 'Einstellungen' });
  const settingsClose = el('button.btn', { type: 'button' }, 'Schliessen');
  settingsDlg.append(el('div.dialog__body', share, el('div.row.dialog__actions', settingsClose)));
  settingsClose.addEventListener('click', () => settingsDlg.close());
  settingsDlg.addEventListener('click', (e) => { if (e.target === settingsDlg) settingsDlg.close(); });
  document.body.append(settingsDlg);
  const settingsLabel = el('span', 'Einstellungen');
  const settingsBtn = el('button.btn.fg-settings__btn', { type: 'button', hidden: true, title: 'Name und Sichtbarkeit von Projekt und Foliensatz' },
    icon('ic-gear', 'fg-settings__ic'), settingsLabel);
  settingsBtn.addEventListener('click', () => { if (typeof settingsDlg.showModal === 'function') settingsDlg.showModal(); else settingsDlg.setAttribute('open', ''); });
  head.querySelector('.page-head__row')?.append(settingsBtn);
  const running = el('div.fg-running');
  const body = el('div.fg-body', { 'aria-live': 'polite' });
  root.append(head, tools, running, body);

  const hp = hashParams();
  const st = { project: hp.get('p'), set: hp.get('set'), deck: hp.get('deck') || '', kind: 'all', q: '', data: null, overview: null,
    sel: new Set(), counts: {}, status: {}, trust: 'all', last: null, wantSettings: hp.get('settings') === '1', fav: hp.get('fav') === '1' };
  const selKey = () => `fundus-sel:${st.project}`;
  const loadSel = () => { try { st.sel = new Set(JSON.parse(localStorage.getItem(selKey()) || '[]')); } catch { st.sel = new Set(); } };
  const saveSel = () => { try { localStorage.setItem(selKey(), JSON.stringify([...st.sel])); } catch { /* privat */ } };

  const fail = (e, where) => {
    if (!alive) return;
    ctx.setStatus('crit', 'Fundus-Server antwortet nicht');
    body.replaceChildren(el('div.alert.alert--error', mark('crit'), el('div', el('strong', `${where} nicht ladbar. `),
      `${e.message}. Prüfen: systemctl --user status fundus-mcp auf dem Spark.`)));
  };

  function loadOverview() {
    return api('').then((ov) => {
      if (!alive) return;
      st.overview = ov;
      const ps = arr(ov.projects);
      const tot = ps.reduce((s, p) => s + (p.exhibits ?? 0), 0);
      ctx.setStatus('ok', `verbunden · ${fmt.int(tot)} Folien in ${fmt.int(ps.length)} Projekten`);
      if (!ps.length) { body.replaceChildren(card({}, emptyNote('Fundus enthält noch keine Folien.'))); return false; }
      if (!ps.some((p) => p.project === st.project)) st.project = ps[0].project;
      return true;
    });
  }
  function loadProject() {
    body.replaceChildren(card({}, emptyNote('Lade Folien …')));
    const q = `?project=${encodeURIComponent(st.project)}${st.set ? `&set=${encodeURIComponent(st.set)}` : ''}`;
    const counts = api(`/counts?project=${encodeURIComponent(st.project)}`, null, '/api/feedback').catch(() => ({ exhibits: {} }));
    return Promise.all([api(q), counts]).then(([d, c]) => {
      if (!alive) return;
      st.data = d;
      st.counts = c?.exhibits ?? {};
      st.status = c?.status ?? {};
      loadSel();
      const known = new Set(arr(d.exhibits).map((x) => x.id));
      st.sel = new Set([...st.sel].filter((id) => known.has(id)));
      if (arr(d.sets).length && !arr(d.sets).some((s) => String(s.id) === String(st.set))) st.set = d.set != null ? String(d.set) : null;
      if (!st.set && arr(d.sets).length === 1) st.set = String(d.sets[0].id);   // einziger Foliensatz: vorausgewählt
      if (st.deck && st.deck !== 'all' && !st.deck.includes(':')) st.deck = `deck:${st.deck}`;   // alte Links (#…&deck=<ordner>)
      if (st.deck && st.deck !== 'all' && !ordersOf().some((o) => o.key === st.deck)) st.deck = '';
      drawHead(); drawTools(); drawShare(); drawGrid(); drawRunning();
    }).catch((e) => fail(e, 'Folien'));
  }
  loadOverview().then((ok) => ok && loadProject()).catch((e) => fail(e, 'Projekte'));

  const projectOf = () => arr(st.overview?.projects).find((p) => p.project === st.project) ?? null;
  const setsOf = () => arr(st.data?.sets).length ? arr(st.data.sets) : arr(projectOf()?.sets);
  const setOf = () => setsOf().find((s) => String(s.id) === String(st.set)) ?? null;
  const decksOf = () => arr(st.data?.decks).filter((x) => !st.set || x.set_id === undefined || String(x.set_id) === String(st.set));
  /** Reihenfolgen: geordnete Decks (neueste zuerst), dann Deck-Ordner. slides: [{id, slot}] in Folienfolge. */
  const ordersOf = () => [
    ...arr(st.data?.sequences).filter((q) => !st.set || !q.set_id || String(q.set_id) === String(st.set)).map((q) => ({
      key: `seq:${q.id}`, label: q.title || q.name, kind: 'seq', source: q.source, updated: q.updated_at,
      slides: arr(q.slides).map((x) => ({ id: x.id, slot: String(x.position), version: x.version, preview: x.preview, png: x.png, html: x.html })) })),
    ...decksOf().map((x) => ({ key: `deck:${x.deck}`, label: x.deck, kind: 'deck', slides: arr(x.slides) })),
  ];
  /** Folien, die nach dem Deck entstanden und nicht darin sind: das Deck ist dann nicht nachgeführt. */
  const newerOutside = (o) => {
    if (!o?.updated) return 0;
    const inOrder = new Set(o.slides.map((s) => s.id));
    return arr(st.data?.exhibits).filter((x) => !inOrder.has(x.id) && (x.updated_at ?? '') > o.updated).length;
  };
  /** Gewählte Reihenfolge; ohne Wahl das neueste geordnete Deck, ausser es ist veraltet (neuere Folien fehlen darin);
   *  null = alle Folien, neueste zuerst. */
  const orderNow = () => {
    if (st.deck === 'all') return null;
    const os = ordersOf();
    if (st.deck) return os.find((o) => o.key === st.deck) ?? null;
    const seq = os.find((o) => o.kind === 'seq') ?? null;
    return seq && newerOutside(seq) ? null : seq;
  };

  /** Kopf: Projektname (eigener Name oder Kennung), Foliensatz, Zahlen. */
  function drawHead() {
    const d = st.data;
    const proj = { project: st.project, title: d.title ?? projectOf()?.title };
    const set = setOf();
    const items = arr(d.exhibits);
    const ver = items.reduce((s, x) => s + (x.versions ?? 1), 0);
    head.querySelector('h1').textContent = nameOf(proj);
    crumb.textContent = proj.title ? `Projekt · Kennung ${st.project}` : 'Projekt';
    const nArch = (set ? set.archived : projectOf()?.archived) ?? 0;
    head.querySelector('.lede').replaceChildren(`${set ? `Foliensatz ${setName(set)}: ` : `${fmt.int(setsOf().length)} Foliensätze: `}${fmt.int(items.length)} Folien in ${fmt.int(ver)} Versionen. Ein Klick auf eine Vorschau zeigt die Folie gross.`,
      ...(nArch ? [' ', el('a', { href: `#archiv?p=${encodeURIComponent(st.project)}` }, `${fmt.int(nArch)} archiviert`), '.'] : []));
    document.title = `${nameOf(proj)}${set && setsOf().length > 1 ? ` · ${setName(set)}` : ''} · Fundus-Portal`;
  }

  function drawTools() {
    setHash({ p: st.project, set: st.set, deck: st.deck, fav: st.fav ? '1' : '' });
    ctx.syncNav?.();
    const rows = [];
    const sets = setsOf();
    if (sets.length) {
      const opts = sets.length > 1 ? [{ value: '', label: 'alle', count: projectOf()?.exhibits }, ...sets.map((s) => ({ value: String(s.id), label: s.title || s.slug, count: s.exhibits }))]
        : sets.map((s) => ({ value: String(s.id), label: s.title || s.slug, count: s.exhibits }));
      const segS = segmented(opts, st.set ? String(st.set) : (sets.length > 1 ? '' : String(sets[0].id)),
        (v) => { st.set = v || null; st.deck = ''; loadProject(); }, 'Foliensatz');
      rows.push(el('div.fg-tools__row.fg-tools__row--sets', el('span.label', 'Foliensatz'), segS.el));   // schmal; breit: Seitenleiste
    }
    const kinds = segmented(KIND_FILTER, st.kind, (v) => { st.kind = v; drawGrid(); }, 'Art');
    const os = ordersOf();
    const seqs = os.filter((o) => o.kind === 'seq');
    const dks = os.filter((o) => o.kind === 'deck');
    const sel = el('select.input.fg-select', { 'aria-label': 'Reihenfolge' },
      seqs.length ? el('optgroup', { label: 'Deck-Reihenfolge' }, seqs.map((o) => el('option', { value: o.key },
        `${o.label} (${fmt.int(o.slides.length)})${o.source === 'pdf' ? ' · aus PDF' : ''}`))) : null,
      el('option', { value: 'all' }, 'Alle Folien, neueste zuerst'),
      dks.length ? el('optgroup', { label: 'Deck-Ordner' }, dks.map((o) => el('option', { value: o.key }, `${o.label} (${fmt.int(o.slides.length)})`))) : null);
    sel.value = orderNow()?.key ?? 'all';
    sel.addEventListener('change', () => { st.deck = sel.value; setHash({ p: st.project, set: st.set, deck: st.deck, fav: st.fav ? '1' : '' }); drawGrid(); });
    const nFav = arr(st.data?.exhibits).filter((x) => x.favorite).length;
    const favT = el('button.btn.fv-filter', { type: 'button', 'aria-pressed': String(st.fav), class: st.fav ? 'btn fv-filter is-on' : 'btn fv-filter',
      title: st.fav ? 'Wieder alle Folien zeigen' : 'Nur Folien mit Stern zeigen' }, icon('ic-star', 'fv-star__ic'), `Favoriten${nFav ? ` (${fmt.int(nFav)})` : ''}`);
    favT.addEventListener('click', () => { st.fav = !st.fav; setHash({ p: st.project, set: st.set, deck: st.deck, fav: st.fav ? '1' : '' }); drawTools(); drawGrid(); });
    const q = el('input.input.fg-search', { type: 'search', placeholder: 'Titel oder Text filtern', 'aria-label': 'Folien filtern', value: st.q });
    q.addEventListener('input', () => { st.q = q.value; drawGrid(); });
    const zoom = el('input.fg-zoom__range', { type: 'range', min: 120, max: 560, step: 10, value: tileSize(), 'aria-label': 'Grösse der Folien' });
    zoom.addEventListener('input', () => setTileSize(Number(zoom.value)));
    rows.push(el('div.fg-tools__row.fg-show', { role: 'group', 'aria-label': 'Anzeigen' }, el('span.label', 'Anzeigen'), sel, kinds.el, favT, q,
      el('span.fg-tools__grow'),
      el('span.fg-zoom', { title: 'Grösse der Vorschauen' }, el('span.fg-zoom__icon.fg-zoom__icon--small', { 'aria-hidden': 'true' }), zoom, el('span.fg-zoom__icon', { 'aria-hidden': 'true' }))));
    reviewSlot.replaceChildren(reviewRow());
    rows.push(reviewSlot);
    tools.replaceChildren(...rows);
  }

  const reviewSlot = el('div.fg-review-slot');   // neu gezeichnet mit dem Raster: „alle N sichtbaren“ folgt den Filtern

  /** Markierte Folien und die beiden Prüfwege. Markiert wird von Hand (Kästchen) oder „alle sichtbaren“ – was sichtbar
   *  ist, bestimmt die Zeile „Anzeigen“ (Reihenfolge, Art, Prüfstand, Text). */
  function reviewRow() {
    const n = st.sel.size;
    const vis = visible().length;
    const all = el('button.btn.btn--ghost.btn--small', { type: 'button', disabled: !vis }, `Alle ${fmt.int(vis)} sichtbaren markieren`);
    all.addEventListener('click', () => { visible().forEach((x) => st.sel.add(x.id)); saveSel(); drawTools(); drawGrid(); });
    const none = n ? el('button.btn.btn--ghost.btn--small', { type: 'button' }, 'Keine') : null;
    none?.addEventListener('click', () => { st.sel.clear(); saveSel(); drawTools(); drawGrid(); });
    const gem = el('button.btn', { type: 'button' }, 'Mit Gemini prüfen');
    gem.addEventListener('click', () => startReview('gemini'));
    const ppl = el('button.btn.btn--primary', { type: 'button' }, 'Personen einladen');
    ppl.addEventListener('click', () => startReview('people'));
    const cl = el('button.btn', { type: 'button', title: 'Markierte Folien, Foliensatz oder Projekt Ende-zu-Ende verschlüsselt an das Fundus einer anderen Person senden' }, 'Teilen über Fundus Cloud …');
    cl.addEventListener('click', () => startShare());
    const mv = n ? el('button.btn', { type: 'button', title: 'Markierte Folien in einen anderen Foliensatz oder ein anderes Projekt' }, 'Verschieben …') : null;
    mv?.addEventListener('click', () => moveSlides([...st.sel], `${fmt.int(n)} Folien`));
    const arc = n ? el('button.btn', { type: 'button', title: 'Markierte Folien archivieren: auffindbar, aber nicht mehr hier gelistet' }, icon('ic-archive', 'fg-lock__icon'), 'Archivieren') : null;
    arc?.addEventListener('click', () => archiveSlides([...st.sel]));
    const del = n ? el('button.btn', { type: 'button', title: 'Markierte Folien in den Papierkorb' }, icon('ic-trash', 'fg-lock__icon'), 'Löschen') : null;
    del?.addEventListener('click', () => trashSlides([...st.sel], `${fmt.int(n)} markierte Folien`));
    return el('div.fg-tools__row.fg-review', { role: 'group', 'aria-label': 'Prüfen' }, el('span.label', 'Prüfen'),
      el('span.small', { class: n ? 'small fg-review__n' : 'small muted' }, n ? `${fmt.int(n)} Folien markiert` : 'Keine Folie markiert'),
      ...kids(all, none, mv, arc, del), el('span.fg-tools__grow'), cl, gem, ppl);
  }

  /** Nach Verschieben oder Löschen: Auswahl leeren, Projekte und Folien neu laden (das Projekt kann weg sein). */
  async function afterChange(ids) {
    for (const id of ids ?? []) st.sel.delete(id);
    saveSel();
    document.querySelector('dialog.fg-lightbox')?.close();
    await loadOverview(); await loadProject(); await ctx.refreshNav?.();
  }
  async function moveSlides(ids, what) {
    const to = await moveDialog({ mode: 'slides', projects: st.overview?.projects, from: { project: st.project, set: st.set }, what });
    if (to && await moveRun(ctx, { target: 'slide', refs: ids.map(String), to })) await afterChange(ids);
  }
  async function archiveRun(target, refs, what) {
    try {
      const r = await api('', { action: 'archive', target, refs: refs.map(String) }, '/api/archive');
      ctx.toast(`${what} archiviert (${fmt.int(r.archived)} ${r.archived === 1 ? 'Folie' : 'Folien'}) – zu finden unter „Archiv“ und in der Suche.`, 5000);
      return true;
    } catch (e) { ctx.toast(`Nicht archiviert: ${e.message}`, 6000); return false; }
  }
  async function archiveSlides(ids) {
    if (await archiveRun('slide', ids, ids.length === 1 ? 'Folie' : `${fmt.int(ids.length)} Folien`)) await afterChange(ids);
  }
  async function trashSlides(ids, label) {
    if (await trashAsk(ctx, { target: 'slide', refs: ids.map(String), label })) await afterChange(ids);
  }

  async function startReview(mode) {
    const order = orderNow();
    const seq = order && order.kind === 'seq' ? { id: order.key.slice(4), label: order.label } : null;
    const set = setOf();
    const shown = visible().map((x) => x.id);
    const selection = [...shown.filter((id) => st.sel.has(id)), ...[...st.sel].filter((id) => !shown.includes(id))];
    if (!selection.length && !seq && !set) { ctx.toast('Markiere Folien oder wähle einen Foliensatz.', 4000); return; }
    const proj = { project: st.project, title: st.data.title ?? projectOf()?.title };
    const r = await openReviewDialog({ project: st.project, projectName: nameOf(proj), set: set ? { id: set.id, name: setName(set) } : null,
      order: seq, selection, mode, ctx });
    if (!r) return;
    showCreated(r, ctx);
    st.sel.clear(); saveSel();
    drawTools(); drawGrid(); ctx.refreshNav?.();
  }

  /** Teilen über Fundus Cloud (cloud-ui.js): Auswahl, Foliensatz oder Projekt an andere Fundus-Installationen. */
  async function startShare() {
    const set = setOf();
    const shown = visible().map((x) => x.id);
    const selection = [...shown.filter((id) => st.sel.has(id)), ...[...st.sel].filter((id) => !shown.includes(id))];
    const proj = { project: st.project, title: st.data.title ?? projectOf()?.title };
    await openShareDialog({ project: st.project, projectName: nameOf(proj), set: set ? { id: set.id, name: setName(set) } : null, selection, ctx });
  }

  /** Laufende Prüfungen dieses Projekts bzw. Foliensatzes: wer prüft, wie weit jede Person ist. */
  function drawRunning() {
    api(`?project=${encodeURIComponent(st.project)}`, null, '/api/reviews').then((d) => {
      if (!alive) return;
      const rs = arr(d.reviews).filter((r) => r.status !== 'closed' && arr(r.progress).length && (!st.set || !r.set_id || String(r.set_id) === String(st.set)));
      running.replaceChildren(...(rs.length ? [card({ title: 'Laufende Prüfungen', accent: 'Prüfungen', cls: 'fg-running__card',
        sub: 'Wer die Folien gerade prüft und wie weit jede Person ist (durchgesehene Folien). Details unter „Prüfungen“.' },
      ...rs.map((r) => el('div.fg-running__review',
        el('div.fg-running__head', el('a', { href: `#pruefungen?r=${r.id}` }, el('strong', r.title)),
          badge(...(REVIEW_STATUS[r.status] ?? [r.status, 'outline'])), el('span.small.muted', `${fmt.int(r.slides)} Folien · ${fmt.int(r.feedback.open)} offene Punkte`)),
        el('div.rv-progress-list', arr(r.progress).map((p) => progressRow(p, { compact: true }))))))] : []));
    }).catch(() => running.replaceChildren());
  }

  /** Name und Sichtbarkeit für andere Projekte: Projekt- und (falls gewählt) Foliensatz-Ebene. */
  function drawShare() {
    const d = st.data;
    const items = arr(d.exhibits);
    const internal = items.filter((x) => x.visibility === 'internal').length;
    const own = items.filter((x) => x.sharing).length;
    const policy = POLICY[st.overview?.policy] ?? st.overview?.policy;
    const save = async (payload, what) => {
      try {
        const r = await api('/sharing', payload);
        ctx.toast(`${what}: ${isNum(r.changed) ? `${fmt.int(r.changed)} Folien geändert` : 'gespeichert'}.`);
      } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); throw e; }
      await loadOverview(); await loadProject();
    };
    const rename = async (payload, what) => {
      let r;
      try { r = await api('', payload, '/api/names'); }
      catch (e) { ctx.toast(`Name nicht gespeichert: ${e.message}`, 5000); throw e; }
      ctx.toast(r.changed === false ? 'Name unverändert.' : `${what}: ${r.title ? `heisst jetzt „${r.title}“` : 'eigener Name entfernt'}.`);
      await loadOverview(); await loadProject(); await ctx.refreshNav?.();
    };
    const rows = [];
    const proj = { project: st.project, title: d.title ?? projectOf()?.title };
    const pc = sharingControl(d.sharing, `wie Grundregel (${policy})`, (v) => save({ project: st.project, sharing: v }, `Projekt ${nameOf(proj)}`), 'Sichtbarkeit des Projekts');
    rows.push(el('div.fg-share__row',
      el('div.fg-share__label', 'Projekt', el('small', `Kennung ${st.project}`)),
      el('div.fg-share__ctl', nameField(proj.title, st.project, (t) => rename({ project: st.project, title: t }, 'Projekt'), 'Eigener Name des Projekts'), pc.el)));
    const set = setOf();
    if (set) {
      const sc = sharingControl(set.sharing, 'wie Projekt', (v) => save({ set: set.id, sharing: v }, `Foliensatz ${setName(set)}`), 'Sichtbarkeit des Foliensatzes');
      rows.push(el('div.fg-share__row',
        el('div.fg-share__label', 'Foliensatz', el('small', `Kennung ${set.slug}${set.is_default ? ' (Standard)' : ''}`)),
        el('div.fg-share__ctl', nameField(set.title, set.slug, (t) => {
          if (!t) { ctx.toast('Ein Foliensatz braucht einen Namen.', 4000); return Promise.reject(new Error('leer')); }
          return rename({ set: set.id, title: t }, 'Foliensatz');
        }, 'Eigener Name des Foliensatzes'), sc.el)));
      const lk = set.locked;
      const lockBtn = el('button.btn', { type: 'button' }, icon('ic-lock', 'fg-lock__icon'), lk ? 'Entsperren …' : 'Foliensatz sperren');
      lockBtn.addEventListener('click', async () => {
        if (await toggleLock(ctx, { set: set.id, slug: set.slug, locked: !!lk, what: `Foliensatz „${setName(set)}“` })) {
          await loadOverview(); await loadProject();
        }
      });
      rows.push(el('div.fg-share__row',
        el('div.fg-share__label', 'Sperre', el('small', lk ? `gesperrt seit ${lockWhen(lk)}` : 'nicht gesperrt')),
        el('div.fg-share__ctl', el('p.small.muted', lk
          ? `Gesperrt${lk.note ? ` („${lk.note}“)` : ''}: keine neue Version, kein Verschieben, Ordner, Name und Deck-Reihenfolge fix. Weiterarbeiten an einer Kopie (Sessions: fundus_exhibit_copy).`
          : 'Sperren friert alle Folien des Satzes ein (keine Änderung mehr). Kopieren bleibt möglich; entsperren nur hier.'), lockBtn)));
    }
    // Verschieben und Löschen (Papierkorb) – Foliensatz und Projekt
    const projLabel = nameOf(proj);
    const pMove = el('button.btn', { type: 'button' }, 'In anderes Projekt übernehmen …');
    pMove.addEventListener('click', async () => {
      const to = await moveDialog({ mode: 'project', projects: st.overview?.projects, from: { project: st.project } });
      if (to && await moveRun(ctx, { target: 'project', refs: [st.project], to })) { settingsDlg.close(); st.project = to; st.set = null; await afterChange(); }
    });
    const pDel = el('button.btn', { type: 'button' }, icon('ic-trash', 'fg-lock__icon'), 'Projekt löschen');
    pDel.addEventListener('click', async () => {
      if (await trashAsk(ctx, { target: 'project', refs: [st.project], label: projLabel })) { settingsDlg.close(); st.project = null; st.set = null; await afterChange(); }
    });
    const sMove = set ? el('button.btn', { type: 'button' }, 'In anderes Projekt verschieben …') : null;
    sMove?.addEventListener('click', async () => {
      const to = await moveDialog({ mode: 'set', projects: st.overview?.projects, from: { project: st.project, set: set.id } });
      if (to && await moveRun(ctx, { target: 'set', refs: [String(set.id)], to })) { settingsDlg.close(); st.set = null; await afterChange(); }
    });
    const sDel = set ? el('button.btn', { type: 'button' }, icon('ic-trash', 'fg-lock__icon'), 'Foliensatz löschen') : null;
    sDel?.addEventListener('click', async () => {
      if (await trashAsk(ctx, { target: 'set', refs: [String(set.id)], label: setName(set) })) { settingsDlg.close(); st.set = null; await afterChange(); }
    });
    const sArc = set ? el('button.btn', { type: 'button' }, icon('ic-archive', 'fg-lock__icon'), 'Foliensatz archivieren') : null;
    sArc?.addEventListener('click', async () => {
      if (await archiveRun('set', [set.id], `Foliensatz „${setName(set)}“`)) { settingsDlg.close(); await afterChange(); }
    });
    const pArc = el('button.btn', { type: 'button' }, icon('ic-archive', 'fg-lock__icon'), 'Projekt archivieren');
    pArc.addEventListener('click', async () => {
      if (await archiveRun('project', [st.project], `Projekt „${projLabel}“`)) { settingsDlg.close(); await afterChange(); }
    });
    rows.push(el('div.fg-share__row',
      el('div.fg-share__label', 'Verschieben, archivieren, löschen', el('small', 'Archiviertes bleibt auffindbar; Gelöschtes liegt 30 Tage im Papierkorb')),
      el('div.fg-share__ctl', el('div.row.fg-mv', ...kids(sMove, sArc, sDel)), el('div.row.fg-mv', pMove, pArc, pDel))));
    settingsBtn.hidden = false;
    settingsLabel.textContent = 'Einstellungen';
    if (st.wantSettings) {     // aus dem Zahnrad in der Seitenleiste
      st.wantSettings = false;
      if (!settingsDlg.open) settingsBtn.click();
    }
    settingsBtn.title = set ? 'Name, Sichtbarkeit, Sperre, Archiv und Löschen von Projekt und Foliensatz' : 'Name, Sichtbarkeit, Archiv und Löschen des Projekts';
    share.replaceChildren(card({ title: set ? 'Name, Sichtbarkeit und Sperre' : 'Name und Sichtbarkeit', accent: 'Name', cls: 'fg-share__card', flat: true,
      sub: `Eigene Namen ändern nur die Anzeige, die Kennungen bleiben. Sichtbarkeit für andere Projekte gilt für alle Folien ohne eigene Einstellung; einzelne Folien lassen sich in der Grossansicht ausnehmen. Stand: ${fmt.int(items.length - internal)} von ${fmt.int(items.length)} Folien für alle Projekte sichtbar, ${fmt.int(internal)} nur im Projekt${own ? `, ${fmt.int(own)} mit eigener Einstellung` : ''}.` }, ...rows));
  }

  /** Stern auch im geladenen Projekt nachführen (Deck-Reihenfolgen zeigen Kopien der Einträge). */
  function favSync(id, on) { const o = arr(st.data?.exhibits).find((y) => y.id === id); if (o) o.favorite = on || undefined; }

  /** Sichtbare Folien in Anzeige-Reihenfolge (Deck-Reihenfolge oder neueste zuerst), gefiltert. */
  function visible(withStage = true) {
    const byId = new Map(arr(st.data.exhibits).map((x) => [x.id, x]));
    let list;
    const order = orderNow();
    if (order) {
      // die Version, die in diesem Deck steckt (ältere Decks zeigen ihre älteren Folien)
      list = order.slides.map((s) => (byId.has(s.id) ? { ...byId.get(s.id), slot: s.slot,
        ...(s.png && s.version !== byId.get(s.id).version ? { version: s.version, current: byId.get(s.id).version, preview: s.preview, png: s.png, html: s.html ?? null } : {}) } : null)).filter(Boolean);
    } else list = arr(st.data.exhibits);
    const words = st.q.toLowerCase().split(/\s+/).filter(Boolean);
    const lvl = (x) => st.status[x.id] ?? st.status[String(x.id)];
    const trustOk = (x) => !withStage || st.trust === 'all' || stageKey(x, lvl(x)) === st.trust;
    return list.filter((x) => trustOk(x) && (st.kind === 'all' || x.kind === st.kind) && (!st.fav || x.favorite)
      && words.every((w) => `${x.title ?? ''} ${x.eyebrow ?? ''} ${x.so_what ?? ''} ${x.slug} ${x.chart_types ?? ''}`.toLowerCase().includes(w)));
  }

  /** Übersicht der Prüfstände (Symbol + Anzahl) über dem Raster; ein Klick zeigt nur diese Folien, ein zweiter alle. */
  function stageBar() {
    const base = visible(false);
    const n = Object.fromEntries(STAGE_KEYS.map((k) => [k, 0]));
    for (const x of base) n[stageKey(x, st.status[x.id] ?? st.status[String(x.id)])] += 1;
    const keys = STAGE_KEYS.filter((k) => n[k] || st.trust === k);
    if (!keys.length) return null;
    return el('div.st-bar', { role: 'group', 'aria-label': 'Prüfstand filtern' }, keys.map((k) => {
      const on = st.trust === k;
      const b = el('button.st-bar__btn', { type: 'button', 'aria-pressed': String(on), class: on ? 'st-bar__btn is-on' : 'st-bar__btn',
        data: { tip: `${STAGE_NAME[k]}: ${n[k]} Folien\n${on ? 'Klick: wieder alle zeigen' : 'Klick: nur diese zeigen'}` },
        'aria-label': `${STAGE_NAME[k]}: ${n[k]} Folien` }, symbol(`st-${k}`), el('span', String(n[k])));
      b.addEventListener('click', () => { st.trust = on ? 'all' : k; drawGrid(); });
      return b;
    }));
  }

  /** Hinweis, wenn die gezeigte Deck-Reihenfolge nicht alle Folien enthält (neue Folien, Deck nicht nachgeführt). */
  function outsideNote() {
    const order = orderNow();
    if (!order) {
      const seq = st.deck ? null : ordersOf().find((o) => o.kind === 'seq');
      const n = seq ? newerOutside(seq) : 0;
      if (!n) return null;
      const btn = el('button.btn.btn--small', { type: 'button' }, 'Deck-Reihenfolge zeigen');
      btn.addEventListener('click', () => { st.deck = seq.key; drawTools(); drawGrid(); });
      return el('p.small.fg-outside', `Alle Folien, neueste zuerst: Die Deck-Reihenfolge „${seq.label}“ (${fmt.int(seq.slides.length)} Folien) ist älter als ${fmt.int(n)} Folien, die nicht darin stehen. `, btn);
    }
    const inOrder = new Set(order.slides.map((s) => s.id));
    const rest = arr(st.data.exhibits).filter((x) => !inOrder.has(x.id)).length;
    if (!rest) return null;
    const btn = el('button.btn.btn--small', { type: 'button' }, 'Alle Folien zeigen');
    btn.addEventListener('click', () => { st.deck = 'all'; drawTools(); drawGrid(); });
    return el('p.small.fg-outside', `Gezeigt wird die Reihenfolge „${order.label}“ mit ${fmt.int(order.slides.length)} Folien; ${fmt.int(rest)} weitere Folien stehen in keiner Deck-Reihenfolge. `, btn);
  }

  function drawGrid() {
    applyTileSize();
    const list = visible();
    const all = arr(st.data.exhibits).length;
    const grid = el('div.fg-grid', { role: 'list' });
    list.forEach((x, i) => grid.append(tile(x, () => openLightbox(list, i), {
      selected: st.sel.has(x.id), open: st.counts[x.id] ?? st.counts[String(x.id)], status: st.status[x.id] ?? st.status[String(x.id)],
      ctx, onFav: (on) => { favSync(x.id, on); drawTools(); if (st.fav) drawGrid(); },
      onToggle: (on, shift) => {
        if (shift && st.last !== null) {
          const [a, b] = [Math.min(st.last, i), Math.max(st.last, i)];
          list.slice(a, b + 1).forEach((y) => (on ? st.sel.add(y.id) : st.sel.delete(y.id)));
        } else if (on) st.sel.add(x.id); else st.sel.delete(x.id);
        st.last = i; saveSel(); drawTools(); drawGrid();
      } })));
    const set = setOf();
    reviewSlot.replaceChildren(reviewRow());
    body.replaceChildren(...kids(
      stageBar(),
      set?.locked ? el('p.small.fg-locked', icon('ic-lock', 'fg-lock__icon'), ` Foliensatz gesperrt seit ${lockWhen(set.locked)}${set.locked.note ? ` · ${set.locked.note}` : ''}`) : null,
      outsideNote(),
      el('p.small.muted.fg-count', `${fmt.int(list.length)} von ${fmt.int(all)} Folien${set ? ` im Foliensatz ${setName(set)}` : ''}${orderNow() ? `, Reihenfolge ${orderNow().label}` : ', neueste zuerst'}${st.fav ? ', nur Favoriten' : ''}${st.kind !== 'all' || st.q ? ' (gefiltert)' : ''}`),
      list.length ? grid : card({}, emptyNote(st.fav ? 'Keine Favoriten in dieser Auswahl. Den Stern setzt du an einer Folie (oben rechts an der Kachel oder in der Grossansicht).' : 'Keine Folie passt zu dieser Auswahl.'))));
  }

  /* ------------------------------------------------------------ Grossansicht */
  function openLightbox(list, start) {
    document.querySelector('dialog.fg-lightbox')?.remove();
    let i = start;
    const dlg = el('dialog.fg-lightbox', { 'aria-label': 'Folie gross' });
    const img = el('img.fg-lb__img', { alt: '' });
    const side = el('div.fg-lb__side');
    const prev = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Vorherige Folie' }, '←');
    const next = el('button.btn.fg-lb__nav', { type: 'button', 'aria-label': 'Nächste Folie' }, '→');
    const close = el('button.btn.fg-lb__close', { type: 'button', 'aria-label': 'Schliessen' }, '✕');
    const pos = el('span.small.muted');
    const shapesBox = el('div.fg-lb__shapes', { 'aria-hidden': 'true' });
    const shapesSvg = newSvg();
    let shapes = [];
    let shownVersion = null;
    /** Markierungen (Rahmen, Kreis, Pfeil, Stift; von Gemini oder Personen) der angezeigten Version über der Folie. */
    const drawShapes = () => {
      const list = shapes.filter((d) => d.version === shownVersion && isShape(d.shape));
      shapesSvg.replaceChildren(...list.map((d) => shapeNode(d.shape, `shape${d.source === 'ai' ? ' shape--ai' : ''}`)));
      shapesBox.replaceChildren(...list.map((d, k) => {
        const p = anchor(d.shape);
        return el('span.fg-lb__pin', { title: `${d.author}: ${d.text}`, class: `fg-lb__pin${d.source === 'ai' ? ' fg-lb__pin--ai' : ''}`,
          style: { left: `${p.x * 100}%`, top: `${p.y * 100}%` } }, String(k + 1));
      }));
    };
    const fs = fullscreenToggle(dlg, 'fg-lightbox--full');
    const frame = el('div.fg-lb__frame', img, shapesSvg, shapesBox);
    fs.corner(frame, img);
    dlg.append(el('div.fg-lb', el('div.fg-lb__bar', prev, pos, next, el('span.fg-tools__grow'), fs, close),
      el('div.fg-lb__main', el('div.fg-lb__stage', frame), side)));
    let seq = 0;
    let changed = false;

    /** Sperre der Folie: eigene Sperre umschaltbar; eine Sperre des Foliensatzes nur in dessen Einstellungen. */
    function lockBlock(x) {
      const l = x.locked;
      const bySet = l?.target === 'set';
      const btn = bySet ? null : el('button.btn', { type: 'button' }, icon('ic-lock', 'fg-lock__icon'), l ? 'Folie entsperren …' : 'Folie sperren');
      btn?.addEventListener('click', async () => {
        if (await toggleLock(ctx, { id: x.id, slug: x.slug, locked: !!l, what: 'Folie' })) {
          changed = true;
          const r = await api(`?project=${encodeURIComponent(st.project)}${st.set ? `&set=${st.set}` : ''}`).catch(() => null);
          const nx = arr(r?.exhibits).find((y) => y.id === x.id);
          x.locked = nx?.locked ?? null;
          show();
        }
      });
      return el('div.fg-lb__share', el('span.label', 'Sperre'),
        el('p.small', l ? icon('ic-lock', 'fg-lock__icon') : null, l
          ? ` Gesperrt seit ${lockWhen(l)}${bySet ? ' (mit dem Foliensatz – entsperren in dessen Einstellungen)' : ''}${l.note ? ` · ${l.note}` : ''}`
          : 'Nicht gesperrt.'), btn);
    }

    /** Verschieben in einen anderen Foliensatz, Löschen (Papierkorb). */
    function moveBlock(x) {
      const mv = el('button.btn', { type: 'button' }, 'Verschieben …');
      mv.addEventListener('click', () => moveSlides([x.id], 'Folie'));
      const arc = el('button.btn', { type: 'button', title: 'Auffindbar, aber nicht mehr im Projekt gelistet' }, icon('ic-archive', 'fg-lock__icon'), 'Archivieren');
      arc.addEventListener('click', () => archiveSlides([x.id]));
      const del = el('button.btn', { type: 'button' }, icon('ic-trash', 'fg-lock__icon'), 'Löschen');
      del.addEventListener('click', () => trashSlides([x.id], x.title || x.slug));
      return el('div.fg-lb__share', el('span.label', 'Verschieben, archivieren, löschen'), el('div.row.fg-mv', mv, arc, del));
    }

    function show() {
      const x = list[i];
      const my = ++seq;
      pos.textContent = `${i + 1} / ${list.length}`;
      prev.disabled = i === 0; next.disabled = i === list.length - 1;
      img.src = withSrc(x.png) ?? x.preview ?? '';
      shownVersion = x.version; shapes = []; drawShapes();
      img.alt = x.title ?? x.slug;
      const set = setsOf().find((s) => String(s.id) === String(x.set_id)) ?? null;
      const inherit = set ? 'wie Foliensatz' : 'wie Projekt';
      const visLine = el('p.small.fg-lb__vis', mark(x.visibility === 'internal' ? 'warn' : 'ok'), ` Jetzt: ${VIS[x.visibility] ?? x.visibility}${x.sharing ? ' (eigene Einstellung)' : ''}`);
      const ctl = sharingControl(x.sharing, inherit, async (v) => {
        try {
          const r = await api('/sharing', { id: x.id, sharing: v });
          x.sharing = v === 'inherit' ? null : v; x.visibility = r.visibility; changed = true;
          visLine.replaceChildren(mark(x.visibility === 'internal' ? 'warn' : 'ok'), ` Jetzt: ${VIS[x.visibility] ?? x.visibility}${x.sharing ? ' (eigene Einstellung)' : ''}`);
          ctx.toast('Sichtbarkeit der Folie gespeichert.');
        } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); throw e; }
      }, 'Sichtbarkeit der Folie');
      const links = el('div.row.fg-lb__links');
      /** Knöpfe PNG/HTML/Skript für die angezeigte Version (urls: {png, html, script}). */
      const drawLinks = (u) => links.replaceChildren(...kids(
        u.png ? el('a.btn', { href: withSrc(u.png), target: '_blank', rel: 'noopener' }, 'PNG öffnen') : null,
        u.html ? el('a.btn', { href: withSrc(u.html), target: '_blank', rel: 'noopener' }, 'HTML öffnen') : null,
        u.script ? el('a.btn', { href: withSrc(u.script), target: '_blank', rel: 'noopener', title: 'Erzeugendes Python-Skript' }, 'Skript') : null));
      drawLinks({ png: x.png, html: x.html });
      const stx = st.status[x.id] ?? st.status[String(x.id)];
      side.replaceChildren(...kids(
        stx?.stage ? el('div.fg-lb__stage-info', stageChip(stx, { long: true }), el('p.small.muted.fg-lb__why', stageWhy(stx))) : null,
        x.eyebrow ? el('span.label', x.eyebrow) : null,
        el('h3.fg-lb__title', x.title || x.slug),
        el('div.row.fv-lb', favButton(x, ctx, (on) => { favSync(x.id, on); changed = true; drawTools(); }, 'btn')),
        tags(x, false),
        x.so_what ? el('p.secondary.fg-lb__sowhat', x.so_what) : null,
        el('div.fg-lb__share', el('span.label', 'Sichtbarkeit für andere Projekte'), ctl.el, visLine),
        lockBlock(x),
        moveBlock(x),
        dl([['Kennung', refWithCopy(ctx, `${x.slug} (#${x.id})`, `${st.project}/${set?.slug ?? 'main'}/${x.slug}`)], ['Foliensatz', set ? set.title || set.slug : null], ['Version', isNum(x.version) ? `${x.version} von ${x.versions}${isNum(x.current) ? ` (in diesem Deck; aktuell: ${x.current})` : ''}` : null],
          ['Stand', x.updated_at ? fmt.dateTime(x.updated_at.replace(' ', 'T')) : null], ['Grafiktypen', x.chart_types ?? null],
          ['Status', STATUS[x.status] ?? x.status ?? null], ['Format', x.size ?? null]]),
        links,
        el('div.fg-lb__versions', el('span.small.muted', 'Versionen werden geladen …')),
        slidePanel(x.id, ctx, () => { changed = true; }, (list2) => { if (my === seq) { shapes = list2; drawShapes(); } })));
      api(`/${x.id}`).then((d) => {
        if (my !== seq) return;
        const box = side.querySelector('.fg-lb__versions');
        const vs = arr(d.versions);
        const cur = vs.find((v) => v.n === x.version) ?? vs[vs.length - 1];
        if (cur?.urls) drawLinks(cur.urls);
        const decks = [...new Set(arr(d.decks).map((k) => k.deck))];
        const shown = el('p.small.muted.fg-ver__shown');
        const showLine = (v) => shown.replaceChildren(`Angezeigt: Version ${v.n}${v.n === (x.current ?? x.version) ? ' (aktuell)' : ''}${isNum(x.current) && v.n === x.version ? ' (in diesem Deck)' : ''}${v.created_at ? ` vom ${fmt.dateTime(v.created_at.replace(' ', 'T'))}` : ''}`);
        if (cur) showLine(cur);
        box.replaceChildren(...kids(
          decks.length ? el('p.small', el('strong', 'In Decks: '), decks.join(' · ')) : null,
          vs.length > 1 ? el('span.label', `Versionen (${vs.length})`) : null,
          vs.length > 1 ? shown : null,
          vs.length > 1 ? el('div.fg-vers', [...vs].reverse().map((v) => {
            const b = el('button.fg-ver', { type: 'button', title: `Version ${v.n} · ${v.created_at ?? ''}${v.palette ? ` · ${PALETTE[v.palette] ?? v.palette}` : ''}`, 'aria-pressed': String(v === cur) },
              v.urls?.preview ? el('img', { src: v.urls.preview, alt: '', loading: 'lazy' }) : null, el('span', `v${v.n}${v.palette ? ` · ${PALETTE[v.palette] ?? v.palette}` : ''}`));
            b.addEventListener('click', () => {
              img.src = withSrc(v.urls?.png) ?? v.urls?.preview ?? img.src;
              box.querySelectorAll('.fg-ver').forEach((o) => o.setAttribute('aria-pressed', String(o === b)));
              if (v.urls) drawLinks(v.urls);
              showLine(v);
              shownVersion = v.n; drawShapes();
            });
            return b;
          })) : null));
      }).catch(() => { if (my === seq) side.querySelector('.fg-lb__versions').replaceChildren(el('span.small.muted', 'Versionen nicht ladbar.')); });
    }
    const go = (d) => { const j = i + d; if (j >= 0 && j < list.length) { i = j; show(); } };
    prev.addEventListener('click', () => go(-1));
    next.addEventListener('click', () => go(1));
    close.addEventListener('click', () => dlg.close());
    // Im Deck-Modus zeigt die Grossansicht Kopien der Folien: nach einer Änderung den Stand vom Server holen.
    dlg.addEventListener('close', () => { dlg.remove(); if (changed && alive) loadProject(); });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dialogKeys(dlg, (e) => {   // am Dokument: auch wenn Safari den Fokus aus dem Dialog nimmt
      // Pfeiltasten blättern – nicht in Auswahlen und nicht beim Schreiben (Feedback-Feld, Notizen)
      if (e.target.closest?.('.seg, input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); go(1); }
    });
    // Wischen auf der ganzen Bühne (iPad, Handy; im Vollbild auch auf dem Rand neben der Folie): nach links = nächste
    let touch = null;
    frame.parentElement.addEventListener('touchstart', (e) => {
      touch = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() } : null;
    }, { passive: true });
    frame.parentElement.addEventListener('touchmove', (e) => { if (e.touches.length > 1) touch = null; }, { passive: true });
    frame.parentElement.addEventListener('touchend', (e) => {
      if (!touch || window.visualViewport?.scale > 1.05) { touch = null; return; }   // gezoomt: Wischen verschiebt das Bild
      const t = e.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
      if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy) && Date.now() - touch.t < 800) go(dx < 0 ? 1 : -1);
      touch = null;
    }, { passive: true });
    document.body.append(dlg);
    dlg.showModal();
    show();
    close.focus();
  }

  return () => { alive = false; document.querySelector('dialog.fg-lightbox')?.remove(); settingsDlg.remove(); };
}

/* Grösse der Kacheln (Regler wie in PowerPoint), je Browser gemerkt */
const ZOOM_KEY = 'fundus-tile-size';
function tileSize() { try { return Number(localStorage.getItem(ZOOM_KEY)) || 250; } catch { return 250; } }
function applyTileSize(px = tileSize()) {
  document.documentElement.style.setProperty('--fg-tile', `${px}px`);
  document.documentElement.dataset.tiles = px < 190 ? 'small' : px > 420 ? 'large' : 'normal';
}
function setTileSize(px) { try { localStorage.setItem(ZOOM_KEY, String(px)); } catch { /* privat */ } applyTileSize(px); }

function tile(x, open, { selected = false, open: fb = null, status = null, onToggle, ctx = null, onFav } = {}) {
  const t = el('button.fg-tile', { type: 'button', 'aria-label': `${KIND[x.kind] ?? x.kind}: ${x.title ?? x.slug}` });
  const fig = el('span.fg-tile__img');
  if (x.preview) fig.append(el('img', { src: x.preview, alt: '', loading: 'lazy', decoding: 'async', width: 640, height: 360 }));
  else fig.append(el('span.small.muted', 'keine Vorschau'));
  if (x.slot) fig.append(el('span.fg-tile__slot', x.slot));
  t.append(fig);
  t.append(el('span.fg-tile__title', { title: x.title ?? x.slug }, x.title || x.slug));
  t.append(tags(x, true));
  t.addEventListener('click', open);
  const box = el('input.fg-check__box', { type: 'checkbox', checked: selected, 'aria-label': `${x.title ?? x.slug} markieren` });
  box.addEventListener('click', (e) => { onToggle?.(box.checked, e.shiftKey); });
  return el('div.fg-tile-wrap', { role: 'listitem', class: selected ? 'fg-tile-wrap fg-tile-wrap--sel' : 'fg-tile-wrap' },
    t, tileSymbols(x, status), favButton(x, ctx, onFav), el('label.fg-check', { title: 'Für eine Prüfung markieren' }, box));
}
function tags(x, compact) {
  const row = el('span.fg-tags');
  row.append(badge(KIND[x.kind] ?? x.kind, 'outline'));
  if (isNum(x.versions) && x.versions > 1) row.append(el('span.fg-tag', { title: `${x.versions} Versionen` }, `v${x.version ?? x.versions} von ${x.versions}`));
  const d = DENSITY[x.density];
  if (d && (x.density !== 'OK' || !compact)) row.append(el('span.fg-tag', { title: `Dichteprüfung: ${x.density}` }, mark(d[0]), d[1]));
  if (x.locked && !compact) row.append(el('span.fg-tag.fg-tag--lock', { title: `gesperrt seit ${lockWhen(x.locked)}` }, icon('ic-lock', 'fg-lock__icon'), 'gesperrt'));
  if (x.visibility === 'internal') row.append(badge(`nur im Projekt${x.sharing ? ' · eigene' : ''}`, 'warning'));
  else if (x.sharing) row.append(badge('für alle · eigene', 'outline'));
  if (!compact && x.palette) row.append(el('span.fg-tag', PALETTE[x.palette] ?? x.palette));
  return row;
}
