/**
 * Austausch zwischen Fundus-Installationen (fundus/package.py):
 * - „Erhaltene Foliensätze“ (#erhalten): Pakete anderer Personen. Liste (Absender, Titel, Datum, Folien, Status); ein
 *   Paket öffnen zeigt das Original so, wie es ankam – nur lesen, in der Grossansicht mit Prüfvermerk, Quellen, Daten
 *   und Skript. „Annehmen …“: Zielprojekt (bestehend oder neu), alle oder einzelne Folien, Fragen „neue Version?“.
 *   Danach: Link auf die eigene Kopie und der Hinweis „seit Erhalt geändert“.
 * - „Gesendet“ (#gesendet): verschickte oder für den Versand exportierte Pakete.
 * Daten: GET /api/received, /api/received/<id>; POST /api/received/<id> {action: accept | reject | install_skill};
 * GET /api/sent; Projekte für die Auswahl aus GET /api/exhibits.
 */
import { el, pageHead, emptyNote, badge, confirmDialog, headline } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { openViewer } from './viewer.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const when = (s) => (s ? fmt.dateTime(String(s).replace(' ', 'T') + (/[zZ+]/.test(s) ? '' : 'Z')) : '–');
const STATUS = { new: ['neu', 'accent'], accepted: ['angenommen', 'success'], partly: ['teilweise angenommen', 'warning'], rejected: ['abgelehnt', 'outline'] };
const CASE_TONE = { new: 'accent', same: 'outline', older: 'outline', update: 'warning', conflict: 'danger', locked: 'danger' };
const ACTION = { accept: 'übernehmen', new_version: 'als neue Version hinzufügen', variant: 'als Variante (Kopie) behalten', skip: 'überspringen' };
const STAGE_WORD = { confirmed: 'bestätigt', checked: 'geprüft', incorporated: 'eingearbeitet', open: 'offene Punkte', changed: 'seit Prüfung geändert',
  unchecked: 'ungeprüft', no_sources: 'Quellen fehlen', not_needed: 'keine Quellen nötig' };
const ROLE = { project: 'Projektdatei', extern: 'Datei ausserhalb des Projekts', skill: 'mck-exhibit-Skill', data: 'Datensatz/Fundus-Daten' };

async function api(url, body) {
  const r = await fetch(url, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}

function statusBadge(s) { const [t, tone] = STATUS[s] ?? [s, 'outline']; return badge(t, tone); }
function stageText(n) {
  if (!n?.stage) return null;
  const lvl = n.level >= 2;
  return `${STAGE_WORD[n.stage] ?? n.stage}${lvl ? ` bei ${n.origin}` : ''}`;
}

// ---------------------------------------------------------------- Erhaltene Foliensätze
export function render(root, ctx) {
  const id = new URLSearchParams(location.hash.split('?')[1] ?? '').get('id');
  return id ? renderOne(root, ctx, id) : renderList(root, ctx);
}

function renderList(root, ctx) {
  let alive = true;
  const list = el('div.tr-list');
  const info = el('p.small.muted');
  root.append(pageHead({ title: 'Erhaltene Foliensätze', accent: 'Erhaltene',
    lede: 'Folien, die dir andere Fundus-Installationen geschickt haben. Das Original bleibt unverändert und lässt sich ansehen; „Annehmen“ übernimmt die Folien in ein Projekt deiner Wahl.' }), info, list);
  (async () => {
    try {
      const d = await api('/api/received');
      if (!alive) return;
      info.textContent = d.count ? `${fmt.plural(d.count, 'Paket', 'Pakete')}${d.new ? `, ${fmt.int(d.new)} neu` : ''}` : '';
      list.replaceChildren(...(arr(d.items).length ? arr(d.items).map((p) => {
        const open = el('a.btn.btn--primary', { href: `#erhalten?id=${p.id}` }, 'Öffnen');
        return el('div.card.tr-item.rx-item',
          el('div.tr-main',
            el('div.tr-title', statusBadge(p.status), el('a.rx-title', { href: `#erhalten?id=${p.id}` }, el('strong', p.title || `Paket ${p.id}`))),
            el('p.small', `von ${p.sender || 'unbekannt'} · erhalten ${when(p.received_at)} · ${fmt.plural(p.slides ?? 0, 'Folie', 'Folien')}`),
            p.accepted_project ? el('p.small.muted', 'übernommen in ', el('a', { href: `#folien?p=${encodeURIComponent(p.accepted_project)}` }, p.accepted_project)) : null),
          el('div.row.tr-actions', open));
      }) : [emptyNote('Noch keine Foliensätze erhalten.')]));
    } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
  })();
  return () => { alive = false; };
}

function renderOne(root, ctx, id) {
  let alive = true;
  let data = null;
  const head = el('div');
  const tools = el('div.row.rx-tools');
  const notes = el('div.rx-notes');
  const grid = el('div.fg-grid.rx-grid', { role: 'list' });
  const extra = el('div.rx-extra');
  root.append(el('p.small', el('a', { href: '#erhalten' }, '← Alle erhaltenen Foliensätze')), head, tools, notes, grid, extra);

  async function load() {
    try {
      data = await api(`/api/received/${encodeURIComponent(id)}`);
      if (alive) draw();
    } catch (e) { head.replaceChildren(pageHead({ title: 'Erhaltener Foliensatz', lede: `Nicht ladbar: ${e.message}` })); }
  }

  function draw() {
    const d = data;
    const counts = {};
    d.slides.forEach((s) => { const c = s.plan?.case; if (c) counts[c] = (counts[c] ?? 0) + 1; });
    head.replaceChildren(pageHead({ title: d.title || `Paket ${d.id}`, accent: null,
      lede: `Von ${d.sender || 'unbekannt'} · erhalten ${when(d.received_at)} · ${fmt.plural(d.slides.length, 'Folie', 'Folien')}${arr(d.decks).length ? ` · ${fmt.plural(d.decks.length, 'Deck', 'Decks')}` : ''}. Das Original bleibt so, wie es ankam.` }));
    const acc = el('button.btn.btn--primary', { type: 'button' }, d.status === 'accepted' ? 'Erneut annehmen …' : 'Annehmen …');
    acc.addEventListener('click', () => acceptDialog(d, ctx, load));
    const rej = d.status === 'new' ? el('button.btn', { type: 'button' }, 'Ablehnen') : null;
    rej?.addEventListener('click', async () => {
      if (!await confirmDialog({ title: 'Paket ablehnen?', text: 'Das Paket wird als abgelehnt markiert. Das Original bleibt gespeichert; du kannst es später trotzdem annehmen.', confirmLabel: 'Ablehnen' })) return;
      try { await api(`/api/received/${d.id}`, { action: 'reject' }); ctx.toast('Paket abgelehnt.'); load(); } catch (e) { ctx.toast(`Nicht abgelehnt: ${e.message}`, 6000); }
    });
    tools.replaceChildren(...[statusBadge(d.status),
      ...Object.entries(counts).map(([c, n]) => badge(`${n} ${d.slides.find((s) => s.plan?.case === c)?.plan?.label ?? c}`, CASE_TONE[c])),
      el('span.fg-tools__grow'), rej, acc].filter(Boolean));
    const nb = [];
    if (d.accepted_project) {
      nb.push(el('p.small', `Übernommen in `, el('a', { href: `#folien?p=${encodeURIComponent(d.accepted_project)}` }, el('strong', d.accepted_project)),
        ` am ${when(d.accepted_at)}.`));
    }
    const changed = d.slides.filter((s) => s.own?.changed).length;
    if (changed) nb.push(el('p.small', badge('seit Erhalt geändert', 'warning'), ` ${fmt.plural(changed, 'eigene Kopie wurde', 'eigene Kopien wurden')} seither geändert.`));
    if (d.skill) nb.push(skillBox(d, ctx, load));
    notes.replaceChildren(...nb);
    const view = d.slides.map((s) => ({ png: s.current?.png, preview: s.current?.preview, title: s.title || s.ref,
      sub: `v${s.current_version} · von ${d.sender}`, href: s.own?.href ?? null, side: () => sidePanel(s, d) }));
    grid.replaceChildren(...d.slides.map((s, i) => {
      const t = el('button.fg-tile', { type: 'button', 'aria-label': s.title || s.ref },
        el('span.fg-tile__img', s.current?.preview ? el('img', { src: s.current.preview, alt: '', loading: 'lazy', width: 640, height: 360 }) : el('span.small.muted', 'keine Vorschau')),
        el('span.fg-tile__title', { title: s.title ?? '' }, s.title || s.ref),
        el('span.fg-tags',
          s.plan ? badge(s.plan.label, CASE_TONE[s.plan.case]) : null,
          s.stage?.level >= 2 ? badge(stageText(s.stage), 'success') : null,
          s.versions.length > 1 ? el('span.fg-tag', `${s.versions.length} Versionen`) : null,
          s.internal ? badge('intern', 'warning') : null,
          s.own?.changed ? badge('seit Erhalt geändert', 'warning') : null));
      t.addEventListener('click', () => openViewer(view, i));
      const own = s.own ? el('a.small.rx-own', { href: s.own.href }, 'Eigene Kopie öffnen') : null;
      return el('div.fg-tile-wrap.rx-wrap', { role: 'listitem' }, t, own);
    }));
    const refs = arr(d.references);
    extra.replaceChildren(...(refs.length ? [el('section.card.rx-refs', headline('h3', 'Nicht mitgeschickt (Verweise)'),
      el('ul.small', refs.map((r) => el('li', r.dataset ? `${r.title ?? r.dataset}${r.source ? ` – ${r.source}` : ''}${r.download_hint ? ` · ${r.download_hint}` : ''}` : `${(r.file ?? r.path ?? '').split('/').pop()} – ${r.reason}`))))] : []));
  }

  load();
  return () => { alive = false; };
}

function sidePanel(s, d) {
  const box = el('div.rx-side');
  box.append(el('h2.fg-lb__title', s.title || s.ref));
  if (s.so_what) box.append(el('p.fg-lb__sowhat.small', s.so_what));
  const st = s.stage;
  box.append(el('div.fg-lb__stage-info', el('strong.small', 'Prüfvermerk'),
    st ? el('span.small', `${stageText(st)}${arr(st.reviewers).length ? ` · ${st.reviewers.join(', ')}` : ''}${st.at ? ` · ${when(st.at)}` : ''}`)
      : el('span.small.muted', 'kein Prüfvermerk'),
    st?.level >= 2 ? el('span.small.muted', 'Gilt bei dir als geprüft, solange die Folie unverändert bleibt.') : null));
  if (s.plan) {
    box.append(el('p.small', badge(s.plan.label, CASE_TONE[s.plan.case]),
      s.plan.local ? el('span', ` · bei dir: `, el('a', { href: `#folien?p=${encodeURIComponent(s.plan.local.project)}` }, s.plan.local.ref)) : ''));
  }
  if (s.own) box.append(el('p.small', el('a', { href: s.own.href }, 'Eigene Kopie öffnen'), s.own.changed ? el('span', ' · ', badge('seit Erhalt geändert', 'warning')) : ''));
  box.append(el('div', el('strong.small', 'Versionen'), el('ul.small.rx-vers', s.versions.slice().reverse().map((v) => el('li',
    `v${v.n}${v.created_at ? ` · ${when(v.created_at)}` : ''} `,
    v.png ? el('a', { href: v.png, target: '_blank', rel: 'noopener' }, 'PNG') : null, v.html ? ' · ' : '',
    v.html ? el('a', { href: v.html, target: '_blank', rel: 'noopener' }, 'HTML') : null, v.script ? ' · ' : '',
    v.script ? el('a', { href: v.script, target: '_blank', rel: 'noopener' }, 'Skript') : null)))));
  const srcs = arr(s.source_list).length ? s.source_list.map((x) => (typeof x === 'string' ? x : [x.title, x.publisher, x.published].filter(Boolean).join(', ')))
    : (s.sources ? s.sources.split(' · ') : []);
  box.append(el('div', el('strong.small', 'Quellen'), srcs.length ? el('ul.small', srcs.map((x) => el('li', x))) : el('p.small.muted', 'keine Quellen')));
  if (arr(s.observations).length) {
    box.append(el('div', el('strong.small', 'Datenpunkte'), el('ul.small', s.observations.map((o) => el('li',
      `${o.metric}${o.category ? ` (${o.category})` : ''}: ${o.value ?? o.value_text ?? '–'}${o.unit ? ` ${o.unit}` : ''}${o.period ? ` · ${o.period}` : ''}`)))));
  }
  if (arr(s.datasets).length) box.append(el('div', el('strong.small', 'Datensätze (nur Verweis)'), el('ul.small', s.datasets.map((x) => el('li', `${x.title}${x.download_hint ? ` · ${x.download_hint}` : ''}`)))));
  const sf = arr(s.script_files);
  if (sf.length) {
    box.append(el('div', el('strong.small', 'Dateien des Skripts'), el('ul.small', sf.map((f) => el('li',
      `${f.path.split('/').pop()} – ${ROLE[f.role] ?? f.role}${f.included ? ', mitgeschickt' : f.reason ? `, ${f.reason}` : ''}`)))));
  }
  return box;
}

function skillBox(d, ctx, reload) {
  const sk = d.skill;
  const ver = (sk.version ?? '').slice(0, 10);
  const btn = el('button.btn.btn--small', { type: 'button' }, 'Skill installieren …');
  const state = sk.up_to_date ? 'bei dir schon installiert' : sk.local_changes ? 'bei dir mit eigenen Änderungen' : sk.local_newer ? 'bei dir neuer' : sk.installed_version ? `bei dir ${String(sk.installed_version).slice(0, 10)}` : 'bei dir nicht installiert';
  btn.disabled = !!sk.up_to_date;
  btn.addEventListener('click', async () => {
    const force = !!(sk.local_changes || sk.local_newer);
    const text = force ? `Dein mck-exhibit-Skill ${sk.local_changes ? 'hat eigene, nicht festgehaltene Änderungen' : 'ist neuer als der im Paket'}. Ersetzen? Der bisherige Ordner bleibt als Sicherung daneben.`
      : `Den mck-exhibit-Skill ${ver} aus dem Paket nach ${sk.target} installieren? Ein bestehender Ordner bleibt als Sicherung daneben. Nichts davon wird ausgeführt.`;
    if (!await confirmDialog({ title: 'Skill installieren?', text, confirmLabel: force ? 'Trotzdem ersetzen' : 'Installieren' })) return;
    try {
      const r = await api(`/api/received/${d.id}`, { action: 'install_skill', force });
      ctx.toast(r.installed ? `Skill ${String(r.version).slice(0, 10)} installiert.` : `Nicht installiert: ${r.reason}`, 6000);
      reload();
    } catch (e) { ctx.toast(`Nicht installiert: ${e.message}`, 7000); }
  });
  return el('p.small.rx-skill', `Enthält den mck-exhibit-Skill (Stand ${ver}${sk.date ? `, ${fmt.date(sk.date)}` : ''}) – ${state}. `, btn);
}

async function acceptDialog(d, ctx, reload) {
  let projects = [];
  try { projects = arr((await api('/api/exhibits')).projects); } catch { /* nur neues Projekt */ }
  const dlg = el('dialog.dialog.rv-dialog.rx-dialog', { 'aria-label': 'Foliensatz annehmen' });
  const sel = el('select.input', { 'aria-label': 'Zielprojekt' },
    el('option', { value: '' }, 'Neues Projekt …'),
    ...projects.slice().sort((a, b) => (a.title || a.project).localeCompare(b.title || b.project, 'de')).map((p) => el('option', { value: p.project }, p.title ? `${p.title} (${p.project})` : p.project)));
  const name = el('input.input', { type: 'text', placeholder: 'Name des neuen Projekts', value: d.title || '', 'aria-label': 'Name des neuen Projekts' });
  if (d.accepted_project && projects.some((p) => p.project === d.accepted_project)) { sel.value = d.accepted_project; name.hidden = true; }
  sel.addEventListener('change', () => { name.hidden = !!sel.value; });
  const rows = d.slides.map((s) => {
    const p = s.plan ?? { options: ['accept', 'skip'], default: 'accept', case: 'new', label: 'neu' };
    const take = p.options.some((o) => o !== 'skip');
    const box = el('input', { type: 'checkbox', checked: take, disabled: !take, 'aria-label': `${s.title} übernehmen` });
    const choices = p.options.filter((o) => o !== 'skip');
    const dec = choices.length > 1 || (choices.length === 1 && choices[0] !== 'accept')
      ? el('select.input.rx-dec', { 'aria-label': `Was mit „${s.title}“ geschehen soll` }, choices.map((o) => el('option', { value: o }, ACTION[o] ?? o)))
      : null;
    if (dec) dec.value = choices.includes(p.default) ? p.default : choices[0];
    const q = p.case === 'update' ? 'Neue Version?' : p.case === 'conflict' ? 'Beide geändert – was tun?' : p.case === 'locked' ? `Bei dir ${p.locked ?? 'gesperrt'} – als Kopie?` : null;
    return { s, box, dec, node: el('li.rx-row', el('label.rx-row__main', box, el('span', el('strong', s.title || s.ref), ' ', badge(p.label, CASE_TONE[p.case]))),
      q ? el('div.rx-row__q', el('span.small', q), dec) : dec) };
  });
  const all = el('button.btn.btn--ghost.btn--small', { type: 'button' }, 'Alle');
  const none = el('button.btn.btn--ghost.btn--small', { type: 'button' }, 'Keine');
  all.addEventListener('click', () => rows.forEach((r) => { if (!r.box.disabled) r.box.checked = true; }));
  none.addEventListener('click', () => rows.forEach((r) => { r.box.checked = false; }));
  const go = el('button.btn.btn--primary', { type: 'button' }, 'Annehmen');
  const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
  const msg = el('p.small.muted', 'Die Folien kommen in einen eigenen Foliensatz des Projekts; Skripte und ihre Dateien werden mitkopiert und ihre Pfade angepasst. Nichts wird ausgeführt.');
  dlg.append(el('div.dialog__body', headline('h3', 'Foliensatz annehmen'),
    el('label.rx-field', el('span.small', 'Zielprojekt'), sel), name,
    el('div.row.rx-pick', el('strong.small', 'Folien'), el('span.fg-tools__grow'), all, none),
    el('ul.rx-rows', rows.map((r) => r.node)), msg, el('div.row.dialog__actions', cancel, go)));
  const done = () => { dlg.close(); dlg.remove(); };
  cancel.addEventListener('click', done);
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(); });
  go.addEventListener('click', async () => {
    const project = sel.value || name.value.trim();
    if (!project) { name.focus(); ctx.toast('Bitte ein Projekt wählen oder einen Namen eingeben.'); return; }
    const picked = rows.filter((r) => r.box.checked);
    if (!picked.length) { ctx.toast('Keine Folie gewählt.'); return; }
    const decisions = {};
    picked.forEach((r) => { if (r.dec) decisions[r.s.global_id] = r.dec.value; });
    go.disabled = true; go.textContent = 'Übernehme …';
    try {
      const r = await api(`/api/received/${d.id}`, { action: 'accept', project, slides: picked.map((x) => x.s.global_id), decisions });
      done();
      const un = arr(r.unresolved).length;
      ctx.toast(`${fmt.plural(r.taken, 'Folie', 'Folien')} übernommen in ${r.project}${un ? ` · ${fmt.plural(un, 'Pfad', 'Pfade')} im Skript nicht auflösbar` : ''}.`, 7000);
      ctx.refreshNav?.();
      reload();
    } catch (e) { go.disabled = false; go.textContent = 'Annehmen'; ctx.toast(`Nicht angenommen: ${e.message}`, 7000); }
  });
  document.body.append(dlg);
  dlg.showModal?.();
}

// ---------------------------------------------------------------- Gesendet
export const gesendet = {
  render(root) {
    let alive = true;
    const list = el('div.tr-list');
    const info = el('p.small.muted');
    root.append(pageHead({ title: 'Gesendet', accent: 'Gesendet', lede: 'Foliensätze, die du an andere Fundus-Installationen geschickt oder als Paket exportiert hast.' }), info, list);
    (async () => {
      try {
        const d = await api('/api/sent');
        if (!alive) return;
        info.textContent = d.count ? fmt.plural(d.count, 'Paket', 'Pakete') : '';
        list.replaceChildren(...(arr(d.items).length ? d.items.map((p) => el('div.card.tr-item.rx-item',
          el('div.tr-main', el('div.tr-title', badge(p.transport === 'cloud' ? 'Fundus Cloud' : 'Datei', 'outline'), el('strong', p.title || p.package_id)),
            el('p.small', `an ${arr(p.recipients).join(', ') || '–'} · ${when(p.sent_at)} · ${fmt.plural(p.slides ?? 0, 'Folie', 'Folien')}`),
            p.path ? el('p.small.muted', p.path) : null))) : [emptyNote('Noch nichts gesendet.')]));
      } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
    })();
    return () => { alive = false; };
  },
};
