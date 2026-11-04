/**
 * Datensätze › Steckbrief (#daten?d=<slug>&t=<reiter>): Kopf mit Titel, Kennung, Zitat (kopieren, BibTeX), Abzeichen,
 * Eckdaten und Knöpfen (Bearbeiten, Neue Version, Neu beschreiben lassen, Löschen). Reiter: Überblick · Tabellen und
 * Spalten · Vorschau · Abfrage · Herkunft · Versionen · Verwendung · Dateien – jeder wird erst beim Öffnen gebaut,
 * Vorschau und Abfrage fragen den Server erst dann. „Bearbeiten“ öffnet die Formulare (Beschreibung, Quelle und
 * Abdeckung, Sichtbarkeit, neue Ausgaben, Lebenszyklus, Herkunft); gespeichert wird jedes Feld beim Verlassen.
 * Daten: GET /api/datasets?ref=…, /api/datasets/<id>/preview; POST /api/datasets/<id> {action}, /api/datasets/<id>/query.
 */
import { el, svgEl, card, emptyNote, segmented, badge, confirmDialog, dl } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { arr, kids, hp, api, cell, sqlDate, VIS, FREQ, DUE, LABELS, badges, periodText, periodYears, loadCatalog, dropCatalog,
  uploadDialog, copyText, tip, hideTip, glyph, compact, put, isText, validity } from './daten-lib.js';
import { resultPill } from './forschung-lib.js';

const TABS = [
  { value: 'ueberblick', label: 'Überblick' }, { value: 'spalten', label: 'Tabellen und Spalten' }, { value: 'vorschau', label: 'Vorschau' },
  { value: 'abfrage', label: 'Abfrage' }, { value: 'herkunft', label: 'Herkunft' }, { value: 'versionen', label: 'Versionen' },
  { value: 'verwendung', label: 'Verwendung' }, { value: 'dateien', label: 'Dateien' }, { value: 'fassungen', label: 'Fassungen' }];
// Text mit Fassungen: keine Tabellen – statt Spalten, Vorschau, Abfrage und Versionen der Reiter „Fassungen“ (Zeitleiste)
const TEXT_TABS = ['ueberblick', 'fassungen', 'herkunft', 'verwendung', 'dateien'];
const DATA_TABS = ['ueberblick', 'spalten', 'vorschau', 'abfrage', 'herkunft', 'versionen', 'verwendung', 'dateien'];
const tabsOf = (d) => (isText(d) ? TEXT_TABS : DATA_TABS).map((v) => TABS.find((t) => t.value === v));
const ACT = [['law', 'Gesetz'], ['regulation', 'Verordnung (EU-Verordnung)'], ['directive', 'Richtlinie (EU-Richtlinie)'], ['ordinance', 'Verordnung (Ausführungsrecht)'],
  ['guideline', 'Leitlinie'], ['standard', 'Norm / Standard'], ['terms', 'Nutzungsbedingungen'], ['policy', 'Policy eines Anbieters'], ['other', 'anderer Text']];
const TOOLS = {
  show: 'Steckbrief abgerufen', preview: 'Vorschau', query: 'Abfrage', update: 'bearbeitet', path: 'Datei geladen', search_hit: 'Suchtreffer',
  ask_hit: 'Treffer bei „Frage an den Bestand“', portal_view: 'im Portal angesehen', portal_preview: 'Vorschau im Portal', portal_query: 'Abfrage im Portal',
  cli_show: 'Steckbrief (Kommandozeile)', cli_preview: 'Vorschau (Kommandozeile)', cli_query: 'Abfrage (Kommandozeile)' };
const LIFE = [{ value: 'active', label: 'aktiv' }, { value: 'superseded', label: 'ersetzt' }, { value: 'withdrawn', label: 'zurückgezogen' }];
const LOC = { ok: ['ok', 'in Ordnung'], missing: ['danger', 'fehlt'], changed: ['warn', 'verändert'] };
const TABLE_LIST_MAX = 200;
const LIN_MAX = 9;

export function render(root, ctx, ref, alive) {
  const back = el('a.dk-back', { href: '#daten' }, '← Katalog');
  const head = el('header.dk-sb');
  const edit = el('div.dk-edit', { hidden: true });
  const tabsBox = el('div.dk-dtabs');
  const body = el('div.dk-tabbody');
  root.append(el('p', back), head, edit, tabsBox, body);
  let d = null;
  let cat = null;
  let tab = TABS.find((t) => t.value === hp().get('t'))?.value ?? 'ueberblick';
  let tableIdx = 0;
  let editing = false;
  let fassung = null;   // Reiter „Fassungen“: gewählte Fassung (Nummer)
  const linOpen = new Set();   // Stammbaum: ausgeklappte Spalten
  const built = new Map();   // Reiter → bereits gebauter Inhalt (bleibt beim Hin- und Herwechseln erhalten)

  async function load() {
    put(head, el('div.dk-skel', el('div.dk-skel__card')));
    try {
      d = await api(`?ref=${encodeURIComponent(ref)}`);      // Kennung (slug) oder Nummer
      if (!alive()) return;
      if (!tabsOf(d).some((t) => t.value === tab)) tab = isText(d) && tab === 'versionen' ? 'fassungen' : 'ueberblick';
      built.clear();
      draw();
      loadCatalog().then((c) => { if (!alive()) return; cat = c; if (tab === 'herkunft' || editing) { built.delete(tab); showTab(tab); } }).catch(() => {});
    } catch (e) { put(head, el('div.alert.alert--error', e.message)); }
  }

  async function save(changes, msg) {
    try {
      d = await api(`/${d.id}`, { action: 'update', changes });
      dropCatalog();
      ctx.toast(msg ?? 'Gespeichert.');
      built.clear(); draw();
    } catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 6000); }
  }

  function draw() {
    drawHead();
    if (editing) drawEdit();
    const seg = segmented(tabsOf(d).map((t) => ({ ...t, count: tabCount(t.value) })), tab, (v) => showTab(v), 'Reiter');
    seg.el.classList.add('dk-dtabs__seg');
    put(tabsBox, seg.el);
    showTab(tab);
  }

  function tabCount(t) {
    if (t === 'spalten') return d.tables > 1 ? fmt.int(d.tables) : undefined;
    if (t === 'versionen' || t === 'fassungen') return arr(d.versions).length > 1 ? String(arr(d.versions).length) : undefined;
    if (t === 'verwendung') { const n = (d.uses?.d30 ?? 0); return n ? String(n) : undefined; }
    return undefined;
  }

  function showTab(v) {
    tab = v;
    const p = hp(); p.set('d', ref);
    if (v === 'ueberblick') p.delete('t'); else p.set('t', v);
    const h = `#daten?${p}`;
    if (location.hash !== h) history.replaceState(null, '', h);
    if (!built.has(v)) built.set(v, buildTab(v));
    put(body, built.get(v));
  }

  function buildTab(v) {
    try {
      return { ueberblick: overview, spalten: tablesTab, vorschau: previewTab, abfrage: queryTab, herkunft: provenanceTab,
        versionen: versionsTab, verwendung: usageTab, dateien: filesTab, fassungen: fassungenTab }[v]();
    } catch (e) { return el('div.alert.alert--error', `Reiter nicht darstellbar: ${e.message}`); }
  }

  // ---------------------------------------------------------------- Kopf (Steckbrief)
  function drawHead() {
    const editB = el('button.btn', { type: 'button', 'aria-pressed': String(editing) }, editing ? 'Bearbeiten schliessen' : 'Bearbeiten');
    editB.addEventListener('click', () => { editing = !editing; edit.hidden = !editing; drawHead(); if (editing) { drawEdit(); edit.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } });
    const ver = el('button.btn', { type: 'button' }, isText(d) ? 'Neue Fassung …' : 'Neue Version …');
    ver.addEventListener('click', () => uploadDialog(ctx, d, () => load()));
    const again = el('button.btn', { type: 'button' }, 'Neu beschreiben lassen');
    again.addEventListener('click', async () => { try { await api(`/${d.id}`, { action: 'redescribe' }); ctx.toast('In der Warteschlange – das lokale Modell beschreibt den Datensatz neu.'); } catch (e) { ctx.toast(e.message, 5000); } });
    const del = el('button.btn', { type: 'button' }, d.removed_at ? 'Wiederherstellen' : 'Löschen');
    del.addEventListener('click', async () => {
      if (!d.removed_at && !await confirmDialog({ title: 'Datensatz löschen?', text: `„${d.title}“ verschwindet aus Katalog und Suche und wird nach 30 Tagen endgültig gelöscht; bis dahin unter „Gelöscht“ wiederherstellbar. Wenn er nur veraltet ist: besser „ersetzt“ oder „zurückgezogen“ unter Bearbeiten › Lebenszyklus.`, confirmLabel: 'Löschen' })) return;
      try { await api(`/${d.id}`, { action: d.removed_at ? 'restore' : 'remove' }); dropCatalog(); ctx.toast(d.removed_at ? 'Wiederhergestellt.' : 'Gelöscht – 30 Tage wiederherstellbar.'); load(); } catch (e) { ctx.toast(e.message, 5000); }
    });
    const [py0, py1] = d.period ? periodYears(d.period) : [null, null];
    const brief = { ...d, period_from: d.period_from ?? py0, period_to: d.period_to ?? py1, inputs_n: arr(d.inputs).length };
    const main = arr(d.tables_detail).reduce((a, t) => (!a || (t.rows ?? 0) > (a.rows ?? 0) ? t : a), null);
    const cols = d.columns_max != null || main ? (main ? arr(main.columns).length : d.columns) : d.columns;
    const inForce = d.in_force ? arr(d.versions).find((v) => v.n === d.in_force.n) ?? d.in_force : null;
    const goFassung = (e) => { e.preventDefault(); fassung = inForce.n; built.delete('fassungen'); tab = 'fassungen'; draw(); };
    const facts = isText(d) ? kids(
      ['Kennung', d.identifier || '–'],
      ['Rechtsraum', d.jurisdiction || '–'],
      ['Textart', LABELS.act_type[d.act_type] ?? d.act_type ?? '–'],
      ['Erlassen von', d.issuer || d.publisher || '–'],
      ['Gültige Fassung', inForce ? el('span.dk-facts__in', el('a', { href: '#', on: { click: goFassung } }, inForce.version_label || `Fassung ${inForce.n}`),
        el('span.dk-facts__sub', validity(inForce))) : 'heute keine'],
      ['Fassungen', `${arr(d.versions).length}${arr(d.versions).length > 1 ? ` · seit ${fmt.date(arr(d.versions).map((v) => v.valid_from).filter(Boolean).sort()[0])}` : ''}`],
      ['Lizenz', d.license || '–'],
      ['Sichtbarkeit', d.visibility === 'internal' ? 'intern (Kunde oder eigene Daten)' : 'öffentlich'],
      ['Zuletzt genutzt', d.uses?.last ? `${fmt.ago(sqlDate(d.uses.last))}` : d.uses ? 'noch nie' : '–']) : kids(
      ['Herausgeber', d.publisher || '–'],
      ['Lizenz', d.license || '–'],
      ['Zeitraum', periodText(brief) || '–'],
      ['Region', d.geo || arr(d.tags?.region).map((r) => r.toUpperCase()).join(', ') || '–'],
      ['Umfang', d.holding === 'reference' ? 'Referenz – nicht in Fundus gelesen' : kids(`${fmt.int(d.rows)} Zeilen`, d.tables > 1 ? `${fmt.int(d.tables)} Tabellen` : '1 Tabelle', cols != null ? `${fmt.int(cols)} Spalten${d.tables > 1 ? ' (grösste Tabelle)' : ''}` : null).join(' · ')],
      ['Grösse', d.holding === 'reference' ? fmt.bytes(d.location?.bytes) : fmt.bytes(d.bytes)],
      ['Version', `${d.version}${arr(d.versions)[0]?.created_at ? ` vom ${fmt.date(sqlDate(arr(d.versions)[0].created_at))}` : ''}`],
      ['Sichtbarkeit', d.visibility === 'internal' ? 'intern (Kunde oder eigene Daten)' : 'öffentlich'],
      ['Zuletzt genutzt', d.uses?.last ? `${fmt.ago(sqlDate(d.uses.last))}` : d.uses ? 'noch nie' : '–']);
    const cite = d.citation ? el('div.dk-cite',
      el('span.label', 'Zitieren'),
      el('p.dk-cite__text', d.citation),
      el('div.row', copyBtn('Zitat kopieren', d.citation), d.citation_bibtex ? copyBtn('BibTeX kopieren', d.citation_bibtex) : null)) : null;
    const life = d.lifecycle === 'superseded' ? el('div.alert.dk-life-alert', glyph('arrow', 'ersetzt', 'warn'), el('span', 'Ersetzt',
      d.successor ? el('span', ' durch ', el('a', { href: `#daten?d=${d.successor.slug ?? d.successor.id}` }, `${d.successor.ref ?? `dataset:${d.successor.id}`} ${d.successor.title ?? ''}`)) : d.superseded_by ? ` durch dataset:${d.superseded_by}` : '',
      d.lifecycle_at ? ` (seit ${fmt.date(sqlDate(d.lifecycle_at))})` : '', d.lifecycle_note ? ` – ${d.lifecycle_note}` : '', '. Für neue Analysen den Nachfolger verwenden.'))
      : d.lifecycle === 'withdrawn' ? el('div.alert.alert--error.dk-life-alert', el('span', 'Zurückgezogen', d.lifecycle_at ? ` am ${fmt.date(sqlDate(d.lifecycle_at))}` : '', d.lifecycle_note ? ` – ${d.lifecycle_note}` : '', '. Sessions finden ihn nicht mehr; nicht mehr verwenden.'))
        : null;
    const pred = arr(d.predecessors ?? (d.predecessor ? [d.predecessor] : []));
    put(head, 
      el('div.dk-sb__top', el('span.label', kids('Steckbrief', d.ref, d.version > 1 ? `Version ${d.version}` : null).join(' · ')),
        el('div.row.dk-sb__actions', editB, ver, again, del)),
      el('h1.dk-sb__title', d.title),
      el('div.dk-badges', badges(brief, cat)),
      life,
      d.notice && !life ? el('div.alert.alert--info', d.notice) : null,
      pred.length ? el('p.small.muted', 'Nachfolger von ', pred.map((x, i) => el('span', i ? ', ' : '', el('a', { href: `#daten?d=${x.slug ?? x.id}` }, x.ref ?? `dataset:${x.id}`)))) : null,
      el('dl', { class: isText(d) ? 'dk-facts dk-facts--text' : 'dk-facts' }, facts.map(([k, v]) => el('div', el('dt', k), el('dd', { title: typeof v === 'string' ? v : null }, v)))),
      cite,
      d.status === 'failed' ? el('div.alert.alert--error', `Beschreibung fehlgeschlagen: ${d.error ?? ''}`) : null);
    back.href = lastCatalog();
  }

  function copyBtn(label, text) {
    const b = el('button.btn.btn--small', { type: 'button' }, label);
    b.addEventListener('click', async () => { ctx.toast(await copyText(text) ? `${label.replace(' kopieren', '')} kopiert.` : 'Kopieren nicht möglich – Text markieren und kopieren.'); });
    return b;
  }

  // ---------------------------------------------------------------- Bearbeiten
  function field(key, label, multi, type) {
    const f = el(multi ? 'textarea.input.ds-field' : 'input.input.ds-field', { 'aria-label': label, placeholder: label, rows: multi ? 4 : undefined, type: multi ? undefined : (type ?? 'text') });
    f.value = (type === 'date' ? String(d[key] ?? '').slice(0, 10) : d[key]) ?? '';
    f.addEventListener('change', () => save({ [key]: f.value || null }, `${label} gespeichert.`));
    return el('label.ds-row', el('span.label', label), f);
  }

  function drawEdit() {
    const title = el('input.input.ds-field', { value: d.title, 'aria-label': 'Titel' });
    title.addEventListener('change', () => { if (title.value.trim()) save({ title: title.value.trim() }, 'Titel gespeichert.'); });
    const vis = segmented(VIS, d.visibility, (v) => save({ visibility: v }, 'Sichtbarkeit gespeichert.'), 'Sichtbarkeit');
    const fsel = el('select.input', { 'aria-label': 'Rhythmus' }, FREQ.map(([v, l]) => el('option', { value: v }, l)));
    fsel.value = d.frequency ?? '';
    fsel.addEventListener('change', () => save({ frequency: fsel.value || null }, 'Rhythmus gespeichert.'));
    const nxt = el('input.input', { type: 'month', value: (d.next_expected ?? '').slice(0, 7), 'aria-label': 'Nächste Ausgabe' });
    nxt.addEventListener('change', () => save({ next_expected: nxt.value || null }, 'Nächste Ausgabe gespeichert.'));
    const scope = d.version > 1 && !isText(d) ? segmented([{ value: 'version', label: 'nur aktuelle Version' }, { value: 'merged', label: 'über alle Versionen' }],
      d.table_scope ? 'merged' : 'version', (v) => save({ table_scope: v }, v === 'merged' ? 'Tabellen über alle Versionen.' : 'Nur Tabellen der aktuellen Version.'), 'Tabellen') : null;
    put(edit, el('div.dk-edit__grid',
      card({ title: 'Beschreibung', accent: 'Beschreibung', cls: 'dk-edit__card' },
        el('label.ds-row', el('span.label', 'Titel'), title),
        field('description', 'Eigene Beschreibung (Herkunft, Erhebung, Besonderheiten)', true),
        el('div.ds-row', el('span.label', 'Sichtbarkeit'), vis.el)),
      card({ title: 'Quelle und Abdeckung', accent: 'Quelle', cls: 'dk-edit__card' },
        el('div.ds-grid', field('publisher', 'Herausgeber'), field('source_title', 'Titel der Quelle'), field('url', 'URL'), field('license', 'Lizenz'),
          field('published', 'Veröffentlicht'), field('period', 'Zeitraum'), field('geo', 'Region'), field('project', 'Projekt'))),
      card({ title: 'Herkunft', accent: 'Herkunft', cls: 'dk-edit__card', sub: 'Wann und womit die Daten geholt wurden – erscheint im Zitat und im Reiter „Herkunft“.' },
        el('div.ds-grid', field('retrieved_at', 'Abgerufen am', false, 'date'), field('script', 'Skript (Pfad)')),
        field('update_hint', 'Wo und wie neue Ausgaben prüfen (Download-URL, Skript zum Neuaufbau)', true),
        scope ? el('div.ds-row', el('span.label', 'Tabellen'), scope.el) : null),
      card({ title: 'Neue Ausgaben', accent: 'Ausgaben', cls: 'dk-edit__card', sub: 'Für regelmässig erscheinende Daten: Fundus erinnert Sessions daran (fundus_update_targets) und zeigt „Update fällig“.' },
        el('div.ds-grid', el('label.ds-row', el('span.label', 'Rhythmus'), fsel), el('label.ds-row', el('span.label', 'Nächste Ausgabe erwartet'), nxt))),
      isText(d) ? textForm() : null,
      lifecycleForm()));
  }

  function textForm() {
    const sel = el('select.input', { 'aria-label': 'Textart' }, ACT.map(([v, l]) => el('option', { value: v }, l)));
    sel.value = d.act_type ?? 'other';
    sel.addEventListener('change', () => save({ act_type: sel.value }, 'Textart gespeichert.'));
    return card({ title: 'Text mit Fassungen', accent: 'Text', cls: 'dk-edit__card', sub: 'Amtliche Kennung und Herkunft des Textes. Die Gültigkeit einzelner Fassungen ändert eine Session: fundus_dataset(ref, action="version", version=N, changes={valid_from, valid_to, version_label}).' },
      el('div.ds-grid', field('identifier', 'Kennung (ELI, CELEX, SR-Nummer, URL)'), field('jurisdiction', 'Rechtsraum (EU, CH, DE, US …)'),
        el('label.ds-row', el('span.label', 'Textart'), sel), field('issuer', 'Erlassen von')));
  }

  function lifecycleForm() {
    let life = d.lifecycle ?? 'active';
    const succ = el('input.input', { type: 'text', list: 'dk-succ-list', placeholder: 'dataset:123 oder Titel', 'aria-label': 'Nachfolger', value: d.superseded_by ? `dataset:${d.superseded_by}${cat?.byId?.get(d.superseded_by) ? ` ${cat.byId.get(d.superseded_by).title}` : ''}` : '' });
    const list = el('datalist#dk-succ-list', arr(cat?.datasets).filter((x) => x.id !== d.id).slice(0, 400).map((x) => el('option', { value: `${x.ref} ${x.title}` })));
    const note = el('textarea.input.ds-field', { rows: 2, placeholder: 'Warum? (z. B. „neue Ausgabe 2027 als dataset:301“, „Lizenz entzogen“)', 'aria-label': 'Notiz' });
    note.value = d.lifecycle_note ?? '';
    const succRow = el('label.ds-row', el('span.label', 'Nachfolger'), succ, list);
    const seg = segmented(LIFE, life, (v) => { life = v; succRow.hidden = v !== 'superseded'; }, 'Lebenszyklus');
    succRow.hidden = life !== 'superseded';
    const go = el('button.btn.btn--primary', { type: 'button' }, 'Lebenszyklus speichern');
    go.addEventListener('click', () => {
      const changes = { lifecycle: life, lifecycle_note: note.value.trim() || null };
      if (life === 'superseded') {
        const m = succ.value.match(/dataset:(\d+)/) ?? succ.value.match(/^\s*(\d+)\s*$/);
        const byTitle = !m ? arr(cat?.datasets).find((x) => x.title === succ.value.trim()) : null;
        const id = m ? Number(m[1]) : byTitle?.id;
        if (!id || id === d.id) { ctx.toast('Bitte einen anderen Datensatz als Nachfolger wählen (dataset:123).', 5000); succ.focus(); return; }
        changes.superseded_by = id;
      } else changes.superseded_by = null;
      save(changes, 'Lebenszyklus gespeichert.');
    });
    return card({ title: 'Lebenszyklus', accent: 'Lebenszyklus', cls: 'dk-edit__card', sub: 'Ersetzt: es gibt einen Nachfolger (bleibt zitierbar). Zurückgezogen: nicht mehr verwenden (Sessions finden ihn nicht mehr). Löschen nur, wenn er nie hätte in Fundus sein sollen.' },
      seg.el, succRow, el('label.ds-row', el('span.label', 'Notiz'), note), el('div.row', go));
  }

  // ---------------------------------------------------------------- Überblick
  function overview() {
    const tags = Object.entries(d.tags ?? {}).flatMap(([f, vs]) => arr(vs).map((v) => el('span.ds-chip.ds-chip--tag', { title: { industry: 'Branche', topic: 'Thema', region: 'Region' }[f] ?? f }, v)));
    const about = card({ title: 'Beschreibung', accent: 'Beschreibung', sub: d.described_at ? `Zusammenfassung vom lokalen Modell (${fmt.dateTime(sqlDate(d.described_at))}); deine eigene Beschreibung hat Vorrang.` : null },
      ...kids(d.summary_de || d.summary ? el('p.dk-lead', d.summary_de || d.summary) : el('p.muted', d.status === 'new' ? 'Das lokale Modell beschreibt den Datensatz gerade …' : 'Noch keine Zusammenfassung.'),
        d.description ? el('div.dk-own', el('span.label', 'Eigene Beschreibung'), el('p', d.description)) : null,
        d.unit_of_observation ? el('p.small', el('strong', 'Eine Zeile ist: '), d.unit_of_observation) : null,
        arr(d.keywords).length ? el('div', el('span.label', 'Schlagworte'), el('div.ds-kw', arr(d.keywords).map((k) => el('span.ds-chip', k)))) : null,
        tags.length ? el('div', el('span.label', 'Fundus-Vokabular'), el('div.ds-kw', tags)) : null,
        d.note ? el('p.small.muted', el('strong', 'Notiz: '), d.note) : null));
    const ok = el('button.btn.btn--small', { type: 'button' }, 'Heute geprüft');
    ok.addEventListener('click', async () => {
      try { d = await api(`/${d.id}`, { action: 'checked' }); dropCatalog(); ctx.toast(`Als geprüft vermerkt${d.next_expected ? ` – nächste Ausgabe ${d.next_expected}` : ''}.`); built.clear(); draw(); }
      catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 6000); }
    });
    const ed = card({ title: 'Neue Ausgaben', accent: 'Ausgaben' },
      ...kids(d.update_due ? el('p', badge(DUE[d.update_due], d.update_due === 'overdue' ? 'danger' : 'warn')) : null,
        dl([['Rhythmus', LABELS.frequency[d.frequency ?? 'none']], ['Nächste Ausgabe', d.next_expected ?? '–'],
          ['Zuletzt geprüft', d.last_checked ? fmt.date(d.last_checked) : 'noch nie'],
          ['Zeitreihen', arr(d.tracker_series).length ? arr(d.tracker_series).join(', ') : '–']]),
        d.update_hint ? el('p.small.dk-hint', d.update_hint) : null,
        d.frequency ? el('div.row', ok, el('span.small.muted', 'rückt eine verstrichene nächste Ausgabe um einen Rhythmus weiter')) : el('p.small.muted', 'Kein Rhythmus – unter „Bearbeiten“ setzen, wenn neue Ausgaben erscheinen.')));
    const xw = d.crosswalk;
    const systems = arr(d.code_systems);
    const links = xw || systems.length || d.stale ? card({ title: 'Codes', accent: 'Codes' },
      ...kids(d.stale ? el('p.small', badge('veraltet', 'warn'), ' Eine Eingangsquelle hat eine neuere Version – neu aufbauen und als neue Version ablegen (Reiter „Herkunft“).') : null,
        xw ? el('div', el('span.label', 'Umschlüsselung'), arr(xw.pairs).map((p) => el('div.small', `${p.from_system}${p.from_version ? ` (${p.from_version})` : ''} → ${p.to_system}${p.to_version ? ` (${p.to_version})` : ''}${p.rows != null ? ` · ${fmt.int(p.rows)} Zeilen` : ''}`)),
          el('div.small.muted', kids(xw.method ? `Methode ${xw.method}` : null, xw.quality ? `Qualität ${xw.quality}` : null, xw.valid_from || xw.valid_to ? `gültig ${xw.valid_from ?? '…'} bis ${xw.valid_to ?? '…'}` : null).join(' · '))) : null,
        systems.length ? el('div', el('span.label', 'Code-Systeme'), el('div.ds-kw', systems.map((x) => el('span.ds-chip.ds-chip--tag', x)))) : null)) : null;
    return el('div.dk-ov', about, el('div.dk-ov__side', ed, links));
  }

  // ---------------------------------------------------------------- Tabellen und Spalten
  function tablesTab() {
    const tabs = arr(d.tables_detail);
    if (d.holding === 'reference') return card({}, emptyNote('Referenz: Fundus liest die Daten nicht – keine Tabellen und Spaltenprofile. Dateiliste unter „Dateien“.'));
    if (!tabs.length) return card({}, emptyNote('Dieser Datensatz hat keine Tabelle.'));
    const prof = el('div.dk-prof');
    const wrap = el('div', { class: tabs.length > 1 ? 'dk-tt' : 'dk-tt dk-tt--one' });
    if (tabs.length > 1) {
      const q = el('input.input', { type: 'search', placeholder: tabs.length > 20 ? `${fmt.int(tabs.length)} Tabellen durchsuchen – Name oder Spalte` : 'Tabelle oder Spalte suchen', 'aria-label': 'Tabellen durchsuchen' });
      const ul = el('ul.dk-tt__list', { role: 'listbox', 'aria-label': 'Tabellen' });
      const more = el('p.small.muted');
      const index = tabs.map((t) => `${t.table} ${t.file} ${arr(t.columns).map((c) => c.name).join(' ')}`.toLowerCase());
      const drawList = () => {
        const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
        const hits = [];
        for (let i = 0; i < tabs.length; i++) if (words.every((w) => index[i].includes(w))) hits.push(i);
        put(ul, ...hits.slice(0, TABLE_LIST_MAX).map((i) => {
          const t = tabs[i];
          const li = el('li', { role: 'option', 'aria-selected': String(i === tableIdx), tabindex: '0' },
            el('span.dk-tt__name', { title: t.table }, t.table), el('span.dk-tt__meta', `${compact(t.rows)} × ${arr(t.columns).length}`));
          const pick = () => { tableIdx = i; built.delete('vorschau'); drawList(); drawProfile(); };
          li.addEventListener('click', pick);
          li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
          return li;
        }));
        more.textContent = hits.length > TABLE_LIST_MAX ? `${fmt.int(hits.length)} Treffer, die ersten ${TABLE_LIST_MAX} gezeigt – Suche eingrenzen.` : `${fmt.int(hits.length)} von ${fmt.int(tabs.length)} Tabellen`;
      };
      let tmr;
      q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(drawList, 120); });
      drawList();
      wrap.append(el('div.dk-tt__side', q, more, ul));
    }
    wrap.append(prof);
    function drawProfile() {
      const t = tabs[Math.min(tableIdx, tabs.length - 1)];
      const cols = arr(t.columns);
      const filt = el('input.input.dk-colq', { type: 'search', placeholder: 'Spalte suchen', 'aria-label': 'Spalte suchen' });
      const tbody = el('tbody');
      const fill = () => {
        const w = filt.value.toLowerCase();
        put(tbody, ...cols.filter((c) => !w || `${c.name} ${c.meaning ?? ''}`.toLowerCase().includes(w)).map((c) => colRow(c, t)));
      };
      filt.addEventListener('input', fill);
      fill();
      put(prof, card({ title: t.table, sub: `${t.file}${t.table !== t.file ? ` · Tabelle ${t.table}` : ''} · ${fmt.int(t.rows)} Zeilen · ${fmt.int(cols.length)} Spalten · ${fmt.bytes(t.bytes)}${t.sampled ? ` · häufige Werte aus einer Stichprobe von ${fmt.int(t.sampled)} Zeilen (hochgerechnet); Zeilen, kleinster und grösster Wert und Mittelwert sind exakt` : ''}`, cls: 'dk-prof__card' },
        cols.length > 12 ? filt : null,
        el('div.table-wrap', el('table.table.table--compact.dk-cols',
          el('thead', el('tr', ['Spalte', 'Typ', 'Bedeutung', 'leer', 'verschieden', 'Verteilung'].map((h, i) => el('th', { class: i === 3 || i === 4 ? 'num' : null, scope: 'col' }, h)))), tbody))));
    }
    drawProfile();
    return wrap;
  }

  function colRow(c, t) {
    return el('tr',
      el('td.dk-cols__name', el('strong', c.name), c.code_system ? el('div', el('span.ds-chip.ds-chip--tag', { title: 'Code-System' }, c.code_system)) : null),
      el('td.muted.small', c.type),
      el('td.dk-cols__mean', c.meaning ? c.meaning : el('span.muted', '–')),
      el('td.num', c.nulls_pct != null ? nullBar(c.nulls_pct) : '–'),
      el('td.num', fmt.int(c.distinct)),
      el('td.dk-cols__dist', distChart(c, t)));
  }

  function nullBar(p) {
    const s = svgEl('svg', { class: 'dk-null', viewBox: '0 0 40 6', width: 40, height: 6, 'aria-hidden': 'true' },
      svgEl('rect', { x: 0, y: 0, width: 40, height: 6, rx: 2, class: 'dk-null__track' }),
      p > 0 ? svgEl('rect', { x: 0, y: 0, width: Math.max(1.5, (p / 100) * 40), height: 6, rx: 2, class: 'dk-null__bar' }) : null);
    return el('span.dk-nullcell', fmt.pct(p, 1), s);
  }

  /** Häufige Werte als kleine Balken, sonst Quartile (kleinster Wert, 25 %, Median, 75 %, grösster Wert) als Streifen. */
  function distChart(c, t) {
    const top = arr(c.top).slice(0, 4);
    const num = (v) => (v === null || v === undefined || v === '' ? null : Number(v));
    const [mn, q1, q2, q3, mx] = [c.min, c.q25, c.q50, c.q75, c.max].map(num);
    if (c.q50 != null && [mn, q1, q2, q3, mx].every((v) => Number.isFinite(v)) && !(top.length && c.distinct <= 12)) {
      const W = 170, H = 22;
      const span = mx - mn || 1;
      const x = (v) => 4 + ((v - mn) / span) * (W - 8);
      const s = svgEl('svg', { class: 'dk-q', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img',
        'aria-label': `kleinster Wert ${c.min}, 25 % ${c.q25}, Median ${c.q50}, 75 % ${c.q75}, grösster Wert ${c.max}` },
      svgEl('line', { x1: x(mn), x2: x(mx), y1: 8, y2: 8, class: 'dk-q__whisk' }),
      svgEl('rect', { x: x(q1), y: 3, width: Math.max(2, x(q3) - x(q1)), height: 10, rx: 2, class: 'dk-q__box' }),
      svgEl('line', { x1: x(q2), x2: x(q2), y1: 2, y2: 14, class: 'dk-q__med' }),
      svgEl('text', { x: 2, y: H - 1, class: 'dk-q__t' }, short(c.min)), svgEl('text', { x: W - 2, y: H - 1, 'text-anchor': 'end', class: 'dk-q__t' }, short(c.max)));
      s.addEventListener('mousemove', (e) => tip(e, ['Verteilung', `kleinster Wert ${c.min} · 25 % ${c.q25} · Median ${c.q50} · 75 % ${c.q75} · grösster Wert ${c.max}${c.avg != null ? ` · Mittelwert ${fmt.num2(Number(c.avg))}` : ''}`]));
      s.addEventListener('mouseleave', hideTip);
      return s;
    }
    if (top.length) {
      const max = Math.max(...top.map((x) => x[1] ?? 0), 1);
      const rows = t?.rows || max;
      return el('div.dk-top', top.map(([v, n]) => el('div.dk-top__row', { title: `${v}: ${fmt.int(n)} (${fmt.pct((n / rows) * 100, 1)})` },
        el('span.dk-top__v', v === null ? '(leer)' : String(v)),
        el('span.dk-top__b', el('span', { style: { width: `${Math.max(2, (n / max) * 100)}%` } })),
        el('span.dk-top__n', compact(n)))));
    }
    if (c.min != null || c.max != null) return el('span.small.muted', `${short(c.min)} … ${short(c.max)}`);
    return el('span.small.muted', arr(c.examples).slice(0, 3).join(' · ') || '–');
  }
  const short = (v) => { const s = String(v ?? ''); const n = Number(s); return s.length > 14 ? (Number.isFinite(n) ? compact(n) : `${s.slice(0, 13)}…`) : s; };

  // ---------------------------------------------------------------- Vorschau (lädt erst jetzt)
  function previewTab() {
    const tabs = arr(d.tables_detail);
    if (d.holding === 'reference') return card({}, emptyNote(`Referenz: liegt unter ${d.location?.path ?? 'ihrem Pfad'}, nicht in Fundus gespeichert – keine Vorschau.`));
    if (!tabs.length) return card({}, emptyNote('Keine Tabelle.'));
    const t = tabs[Math.min(tableIdx, tabs.length - 1)];
    const grid = el('div.ds-grid-prev');
    const pager = el('div.row.ds-pager');
    const big = (t.rows ?? 0) >= 5000000;      // Sortieren liest dann die ganze Tabelle auf dem Server (der Browser bekommt nur 100 Zeilen)
    let off = 0, order = null;
    async function page() {
      put(grid, el('p.small.muted', order && big ? `Sortiere ${fmt.int(t.rows)} Zeilen auf dem Server …` : 'Lade …'));
      try {
        const p = await api(`/${d.id}/preview?table=${t.id}&offset=${off}&limit=100${order ? `&order=${encodeURIComponent(order)}` : ''}`);
        if (!alive()) return;
        const ths = p.columns.map((c) => {
          const th = el('th', { title: big ? `Sortieren – durchsucht alle ${fmt.int(t.rows)} Zeilen, dauert bei dieser Grösse etwas` : 'Sortieren' }, c, order?.startsWith(`${c}:`) ? (order.endsWith(':desc') ? ' ↓' : ' ↑') : '');
          th.addEventListener('click', () => { order = order === `${c}:asc` ? `${c}:desc` : `${c}:asc`; off = 0; page(); });
          return th;
        });
        put(grid, el('div.table-wrap.ds-scroll', el('table.table.table--compact.ds-data', el('thead', el('tr', el('th.muted', '#'), ...ths)),
          el('tbody', p.rows.map((r, i) => el('tr', el('td.muted.num', fmt.int(off + i + 1)), ...r.map((v) => el('td', { title: v == null ? '' : String(v) }, cell(v)))))))));
        const prev = el('button.btn.btn--small', { type: 'button', disabled: off === 0 }, '← zurück');
        const next = el('button.btn.btn--small', { type: 'button', disabled: off + 100 >= p.total }, 'weiter →');
        prev.addEventListener('click', () => { off = Math.max(0, off - 100); page(); });
        next.addEventListener('click', () => { off += 100; page(); });
        put(pager, prev, el('span.small.muted', `Zeilen ${fmt.int(off + 1)}–${fmt.int(Math.min(off + 100, p.total))} von ${fmt.int(p.total)}`), next);
      } catch (e) { put(grid, el('p.small', `Vorschau nicht ladbar: ${e.message}`)); }
    }
    page();
    const pick = tabs.length > 1 ? el('p.small.muted', 'Tabelle ', el('strong', t.table), tabs.length > 1 ? ' – andere Tabelle im Reiter „Tabellen und Spalten“ wählen.' : '') : null;
    return card({ title: 'Vorschau', accent: 'Vorschau', sub: 'Der Browser lädt immer nur 100 Zeilen, egal wie gross die Tabelle ist. Klick auf eine Spalte sortiert.' }, pick, pager, grid);
  }

  // ---------------------------------------------------------------- Abfrage
  function queryTab() {
    const tabs = arr(d.tables_detail);
    if (d.holding === 'reference') return card({}, emptyNote(`Referenz: liegt unter ${d.location?.path ?? 'ihrem Pfad'}, nicht in Fundus gespeichert – keine Abfrage.`));
    if (!tabs.length) return card({}, emptyNote('Keine Tabelle.'));
    const ta = el('textarea.input.ds-sql', { rows: 5, spellcheck: 'false', 'aria-label': 'SQL' });
    const first = tabs[0];
    const cols = arr(first.columns).slice(0, 3).map((c) => `"${c.name}"`).join(', ');
    ta.value = `SELECT ${cols || '*'}, count(*) AS n\nFROM t\nGROUP BY ALL\nORDER BY n DESC\nLIMIT 20`;
    const go = el('button.btn.btn--primary', { type: 'button' }, 'Ausführen');
    const out = el('div.ds-qout');
    const run = async () => {
      go.disabled = true; put(out, el('p.small.muted', 'Läuft …'));
      try {
        const r = await api(`/${d.id}/query`, { sql: ta.value });
        put(out, el('p.small.muted', `${fmt.int(r.rows.length)} Zeilen${r.truncated ? ' (gekürzt)' : ''}`),
          el('div.table-wrap.ds-scroll', el('table.table.table--compact.ds-data', el('thead', el('tr', r.columns.map((c) => el('th', c)))),
            el('tbody', r.rows.map((row) => el('tr', row.map((v) => el('td', cell(v)))))))));
      } catch (e) { put(out, el('p.small.ds-err', e.message)); }
      go.disabled = false;
    };
    go.addEventListener('click', run);
    ta.addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); } });
    const names = tabs.slice(0, 30).map((x) => x.table).join(', ') + (tabs.length > 30 ? ` und ${fmt.int(tabs.length - 30)} weitere` : '');
    return card({ title: 'Abfrage', accent: 'Abfrage', sub: `SQL nur lesend (DuckDB). Tabellen: ${names}; die erste heisst auch „t“. Ausführen: Knopf oder ⌘/Strg + Enter.` },
      ta, el('div.row', go), out);
  }

  // ---------------------------------------------------------------- Herkunft
  function provenanceTab() {
    const lin = lineage();
    const ins = arr(d.inputs);
    const link = (x) => el('a', { href: `#daten?d=${String(x.ref).split(':')[1]}` }, `${x.ref} ${x.title ?? ''}`);
    const facts = dl(kids(
      ['Quelle', d.source_title || '–'], ['Herausgeber', d.publisher || '–'],
      ['URL', d.url ? el('a', { href: d.url, target: '_blank', rel: 'noopener' }, d.url) : '–'],
      ['Lizenz', d.license || '–'], ['Veröffentlicht', d.published || '–'],
      ['Abgerufen am', d.retrieved_at ? fmt.date(sqlDate(d.retrieved_at)) : '–'],
      ['Skript', d.script ? el('code.dk-code', d.script) : '–'],
      ['Neue Ausgaben', d.update_hint ? el('span.dk-hint', d.update_hint) : '–'],
      ['In Fundus seit', `${fmt.dateTime(sqlDate(d.created_at))}${d.created_by ? ` · ${d.created_by}` : ''}`]));
    return el('div.dk-prov',
      card({ title: 'Stammbaum', accent: 'Stammbaum', sub: 'Woraus der Datensatz aufgebaut ist (links) und was darauf aufbaut (rechts), je zwei Stufen. Klick öffnet den Datensatz.' }, lin),
      el('div.dk-ov',
        card({ title: 'Herkunftsangaben', accent: 'Herkunft' }, facts),
        ins.length ? card({ title: 'Aufgebaut aus', accent: 'aus' },
          el('ul.dk-life__list', ins.map((x) => el('li', link(x), el('div.small.muted', `Version ${x.version}`,
            x.stale ? el('span', ' ', badge(`neuere Version ${x.current_version}`, 'warn')) : null,
            x.visibility === 'internal' ? el('span', ' ', badge('intern', 'warn')) : null, x.note ? ` · ${x.note}` : ''))))) : null));
  }

  function lineage() {
    let L = d.lineage;
    if (!L || !arr(L.nodes).length) {      // ohne Server-Stammbaum: eine Stufe aus inputs / used_by
      const me = { ref: d.ref, title: d.title, depth: 0, visibility: d.visibility };
      const up = arr(d.inputs).map((x) => ({ ref: x.ref, title: x.title, depth: -1, visibility: x.visibility }));
      const down = arr(d.used_by).map((x) => ({ ref: x.ref, title: x.title, depth: 1 }));
      L = { nodes: [me, ...up, ...down], edges: [...up.map((x) => ({ from: x.ref, to: d.ref })), ...down.map((x) => ({ from: d.ref, to: x.ref }))] };
    }
    let nodes = arr(L.nodes);
    if (nodes.length <= 1) return emptyNote('Weder aus anderen Datensätzen aufgebaut noch Grundlage für andere.');
    const depths = [...new Set(nodes.map((n) => n.depth))].sort((a, b) => a - b);
    // Lange Spalten (z. B. 48 Eingangsdatensätze) kürzen: die ersten LIN_MAX, dann ein Knoten „+ N weitere“ (Klick zeigt alle)
    const hidden = new Map();
    let edges = arr(L.edges);
    const cols = new Map(depths.map((k) => {
      const all = nodes.filter((n) => n.depth === k);
      if (k === 0 || all.length <= LIN_MAX || linOpen.has(k)) return [k, all];
      const keep = all.slice(0, LIN_MAX - 1);
      const more = { ref: `more:${k}`, title: `+ ${all.length - keep.length} weitere anzeigen`, depth: k, more: true };
      for (const n of all.slice(LIN_MAX - 1)) hidden.set(n.ref, more.ref);
      return [k, [...keep, more]];
    }));
    if (hidden.size) {
      const seen = new Set();
      edges = edges.map((e) => ({ from: hidden.get(e.from) ?? e.from, to: hidden.get(e.to) ?? e.to }))
        .filter((e) => { const key = `${e.from}>${e.to}`; if (seen.has(key)) return false; seen.add(key); return true; });
    }
    nodes = [...cols.values()].flat();
    const NW = 180, NH = 40, GX = 40, GY = 10;
    const maxN = Math.max(...[...cols.values()].map((c) => c.length));
    const W = depths.length * NW + (depths.length - 1) * GX + 8;
    const H = maxN * (NH + GY) + 8;
    const pos = new Map();
    depths.forEach((k, ci) => {
      const list = cols.get(k);
      const off = (H - list.length * (NH + GY)) / 2;
      list.forEach((n, i) => pos.set(n.ref, { x: 4 + ci * (NW + GX), y: off + i * (NH + GY) + GY / 2, n }));
    });
    const s = svgEl('svg', { class: 'dk-lin', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': 'Stammbaum des Datensatzes' });
    for (const e of edges) {
      const a = pos.get(e.from), b = pos.get(e.to);
      if (!a || !b) continue;
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
      s.append(svgEl('path', { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 4},${y2}`, class: 'dk-lin__edge', 'marker-end': 'url(#dk-arrow)' }));
    }
    s.prepend(svgEl('defs', {}, svgEl('marker', { id: 'dk-arrow', viewBox: '0 0 8 8', refX: 6, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto' },
      svgEl('path', { d: 'M0,0 L8,4 L0,8 z', class: 'dk-lin__head' }))));
    for (const { x, y, n } of pos.values()) {
      const me = n.depth === 0;
      const max = 24;
      const title = String(n.title ?? '');
      if (n.more) {
        const g = svgEl('g', { class: 'dk-lin__node is-more', tabindex: '0', role: 'button' },
          svgEl('rect', { x, y, width: NW, height: NH, rx: 7 }), svgEl('text', { x: x + 10, y: y + 24, class: 'dk-lin__t' }, title));
        const open = () => { linOpen.add(n.depth); built.delete('herkunft'); showTab('herkunft'); };
        g.addEventListener('click', open);
        g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
        s.append(g);
        continue;
      }
      const g = svgEl(me ? 'g' : 'a', { class: `dk-lin__node${me ? ' is-me' : ''}${n.visibility === 'internal' ? ' is-internal' : ''}`, href: me ? null : `#daten?d=${String(n.ref).split(':')[1]}` },
        svgEl('title', {}, `${n.ref} ${title}`),
        svgEl('rect', { x, y, width: NW, height: NH, rx: 7 }),
        svgEl('text', { x: x + 10, y: y + 16, class: 'dk-lin__ref' }, `${n.ref}${n.visibility === 'internal' ? ' · intern' : ''}`),
        svgEl('text', { x: x + 10, y: y + 31, class: 'dk-lin__t' }, title.length > max ? `${title.slice(0, max - 1)}…` : title));
      s.append(g);
    }
    const lab = el('div.dk-lin__cols', depths.map((k) => el('span', { style: { width: `${NW}px` } }, k < 0 ? (k === -1 ? 'Eingangsdaten' : 'deren Eingangsdaten') : k === 0 ? 'dieser Datensatz' : k === 1 ? 'baut darauf auf' : 'darauf aufbauend')));
    lab.style.gap = `${GX}px`;
    return el('div.dk-lin__wrap', lab, s);
  }

  // ---------------------------------------------------------------- Versionen
  function versionsTab() {
    const vs = arr(d.versions);
    if (!vs.length) return card({}, emptyNote('Keine Versionen.'));
    const diff = (c) => {
      if (!c) return null;
      const items = [];
      if (arr(c.tables_added).length) items.push(el('li.dk-diff--add', `+ ${arr(c.tables_added).length} Tabellen: ${arr(c.tables_added).slice(0, 6).join(', ')}${arr(c.tables_added).length > 6 ? ' …' : ''}`));
      if (arr(c.tables_removed).length) items.push(el('li.dk-diff--del', `− ${arr(c.tables_removed).length} Tabellen: ${arr(c.tables_removed).slice(0, 6).join(', ')}${arr(c.tables_removed).length > 6 ? ' …' : ''}`));
      for (const [t, cs] of Object.entries(c.columns_added ?? {})) items.push(el('li.dk-diff--add', `+ Spalten in ${t}: ${arr(cs).join(', ')}`));
      for (const [t, cs] of Object.entries(c.columns_removed ?? {})) items.push(el('li.dk-diff--del', `− Spalten in ${t}: ${arr(cs).join(', ')}`));
      if (c.rows_delta) items.push(el('li', { class: c.rows_delta > 0 ? 'dk-diff--add' : 'dk-diff--del' }, `${c.rows_delta > 0 ? '+' : '−'} ${fmt.int(Math.abs(c.rows_delta))} Zeilen`));
      return items.length ? el('ul.dk-diff', items) : el('p.small.muted', 'Gleiche Tabellen und Spalten wie die Version davor.');
    };
    return card({ title: 'Versionen', accent: 'Versionen', sub: 'Jede neue Ausgabe oder jeder Neuaufbau ist eine Version; ältere bleiben gespeichert und zitierbar. Unterschiede aus den Spaltenprofilen.' },
      el('ol.dk-vers', vs.map((v, i) => el('li', { class: v.n === d.version ? 'is-current' : null },
        el('div.dk-vers__head', el('strong', `Version ${v.n}`), v.n === d.version ? badge('aktuell', 'accent') : null,
          el('span.small.muted', kids(fmt.dateTime(sqlDate(v.created_at)), v.created_by).join(' · '))),
        v.note ? el('p.small', v.note) : null,
        i < vs.length - 1 || v.changes ? diff(v.changes) : el('p.small.muted', 'Erste Version.')))));
  }

  // ---------------------------------------------------------------- Fassungen (Text mit Fassungen)
  function fassungenTab() {
    const vs = arr(d.versions).slice().reverse();          // älteste oben
    if (!vs.length) return card({}, emptyNote('Keine Fassungen.'));
    const cur = vs.find((v) => v.in_force)?.n ?? null;
    if (fassung === null || !vs.some((v) => v.n === fassung)) fassung = cur ?? vs[vs.length - 1].n;
    const panel = el('div.dk-fa__panel', { 'aria-live': 'polite' });
    const list = el('ol.dk-vers.dk-fa__list');
    const svgBox = timeline(vs, cur, (n) => select(n));
    function select(n) {
      fassung = n;
      for (const g of svgBox.querySelectorAll('.dk-fa__row')) g.classList.toggle('is-sel', Number(g.dataset.n) === n);
      for (const li of list.children) li.classList.toggle('is-sel', Number(li.dataset.n) === n);
      drawPanel();
    }
    function drawPanel() {
      const v = vs.find((x) => x.n === fassung);
      if (!v) { put(panel); return; }
      const files = arr(v.files);
      put(panel,
        el('div.dk-fa__phead', el('strong', v.version_label || `Fassung ${v.n}`), v.in_force ? badge('heute gültig', 'accent') : null),
        el('p.small.muted', kids(`Fassung ${v.n}`, validity(v), v.published_at ? `veröffentlicht ${fmt.date(v.published_at)}` : null,
          `abgelegt ${fmt.date(sqlDate(v.created_at))}`).join(' · ')),
        v.note ? el('p.small', v.note) : null,
        v.source_url ? el('p.small.dk-fa__src', 'Quelle: ', el('a', { href: v.source_url, target: '_blank', rel: 'noopener' }, v.source_url)) : null,
        files.length ? el('ul.ds-docs', files.map((f) => el('li', el('a', { href: f.download_url, download: '' }, f.file), el('span.small.muted', ` · ${f.format} · ${fmt.bytes(f.bytes)}`))))
          : el('p.small.muted', 'Keine Dateien in dieser Fassung.'));
    }
    put(list, ...arr(d.versions).map((v) => el('li', { 'data-n': String(v.n), class: kids(v.in_force ? 'is-current' : null, v.n === fassung ? 'is-sel' : null).join(' ') || null },
      el('button.dk-fa__item', { type: 'button', on: { click: () => { select(v.n); panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } } },
        el('span.dk-vers__head', el('strong', `Fassung ${v.n}`), v.in_force ? badge('heute gültig', 'accent') : null, el('span.small.muted', validity(v))),
        v.version_label ? el('span.small', v.version_label) : null))));
    drawPanel();
    return el('div.dk-fa',
      card({ title: 'Fassungen', accent: 'Fassungen', sub: 'Jede Fassung gilt von ihrem ersten bis zu ihrem letzten Tag; eine neue Fassung beendet die vorige. Fundus speichert die Texte so, wie sie geliefert wurden – ohne sie zu zerlegen oder zu vergleichen. Balken anklicken: Dateien der Fassung.' },
        svgBox, panel),
      card({ title: 'Alle Fassungen', accent: 'Liste' }, list));
  }

  /** Zeitleiste: je Fassung ein Balken von valid_from bis valid_to (offen: bis heute, mit Pfeil); die heute gültige hervorgehoben. */
  function timeline(vs, cur, onPick) {
    const DAY = 864e5;
    const day = (v) => (v ? Date.parse(`${String(v).slice(0, 10)}T00:00:00Z`) : null);
    const today = day(new Date().toISOString());
    const starts = vs.map((v) => day(v.valid_from)).filter((t) => t !== null);
    const ends = vs.map((v) => (v.valid_to ? day(v.valid_to) + DAY : today));
    let t0 = Math.min(...starts, today);
    let t1 = Math.max(...ends, today);
    const pad = Math.max((t1 - t0) * 0.04, 30 * DAY);
    t0 -= pad; t1 += pad * 2;
    // in Pixeln der Kartenbreite gezeichnet (nicht skaliert: die Schrift bleibt bei 1000 px lesbar); schmaler wird gescrollt
    const W = Math.max(560, Math.round((body.clientWidth || 940) - 52)), LW = 92, RW = 12, AX = 30, RH = 32, BH = 20;
    const H = AX + vs.length * RH + 6;
    const x = (t) => LW + ((t - t0) / (t1 - t0)) * (W - LW - RW);
    const s = svgEl('svg', { class: 'dk-fa__svg', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'group', 'aria-label': `Zeitleiste der ${vs.length} Fassungen` });
    const y0 = new Date(t0).getUTCFullYear();
    const y1 = new Date(t1).getUTCFullYear();
    const step = Math.max(1, Math.ceil((y1 - y0 + 1) / 10));
    for (let y = y0; y <= y1 + 1; y += step) {
      const t = Date.UTC(y, 0, 1);
      if (t < t0 || t > t1) continue;
      s.append(svgEl('line', { x1: x(t), x2: x(t), y1: AX - 8, y2: H - 2, class: 'dk-fa__grid' }));
      if (x(t) + 34 < W && Math.abs(x(t) - x(today)) > 22) s.append(svgEl('text', { x: x(t) + 4, y: AX - 12, class: 'dk-fa__tick' }, String(y)));
    }
    s.append(svgEl('line', { x1: x(today), x2: x(today), y1: 14, y2: H - 2, class: 'dk-fa__today' }),
      svgEl('text', { x: x(today), y: 10, 'text-anchor': 'middle', class: 'dk-fa__todayt' }, 'heute'));
    vs.forEach((v, i) => {
      const y = AX + i * RH + (RH - BH) / 2;
      const a = day(v.valid_from);
      const open = !v.valid_to;
      const g = svgEl('g', { class: `dk-fa__row${v.n === cur ? ' is-current' : ''}${v.n === fassung ? ' is-sel' : ''}`, 'data-n': String(v.n), tabindex: '0', role: 'button',
        'aria-label': `Fassung ${v.n}: ${v.version_label ?? ''}, ${validity(v)}${v.n === cur ? ', heute gültig' : ''}` });
      g.append(svgEl('rect', { x: 0, y: AX + i * RH, width: W, height: RH, class: 'dk-fa__hit' }),
        svgEl('text', { x: 0, y: y + BH / 2 + 4, class: 'dk-fa__lab' }, `Fassung ${v.n}`));
      if (a === null) {
        g.append(svgEl('text', { x: LW, y: y + BH / 2 + 4, class: 'dk-fa__none' }, 'ohne Gültigkeitsangabe'));
      } else {
        const xa = x(a);
        const xb = Math.max(x(open ? today : day(v.valid_to) + DAY), xa + 4);
        g.append(svgEl('rect', { x: xa, y, width: xb - xa, height: BH, rx: 3, class: 'dk-fa__bar' }));
        if (open) g.append(svgEl('path', { d: `M${xb + 2} ${y + 3} l8 ${BH / 2 - 3} l-8 ${BH / 2 - 3} z`, class: 'dk-fa__open' }));
        const lbl = `${fmt.date(v.valid_from)} – ${open ? 'heute' : fmt.date(v.valid_to)}`;
        const tw = lbl.length * 6.6;                       // ungefähre Breite bei 11.5 px
        if (xb - xa > tw + 16) g.append(svgEl('text', { x: xa + 8, y: y + BH / 2 + 4, class: 'dk-fa__in' }, lbl));
        else if (xb + (open ? 14 : 6) + tw < W) g.append(svgEl('text', { x: xb + (open ? 14 : 6), y: y + BH / 2 + 4, class: 'dk-fa__out' }, lbl));
        else g.append(svgEl('text', { x: xa - 6, y: y + BH / 2 + 4, 'text-anchor': 'end', class: 'dk-fa__out' }, lbl));
      }
      g.addEventListener('click', () => onPick(v.n));
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(v.n); } });
      g.addEventListener('mousemove', (e) => tip(e, [v.version_label || `Fassung ${v.n}`, validity(v), `${arr(v.files).length} Dateien – anklicken`]));
      g.addEventListener('mouseleave', hideTip);
      s.append(g);
    });
    return el('div.dk-fa__wrap', s);
  }

  // ---------------------------------------------------------------- Verwendung
  function usageTab() {
    const u = d.uses;
    const slides = arr(d.used_on_slides);
    const ub = arr(d.used_by);
    const parts = [];
    if (u) {
      const daily = arr(u.daily_90);
      parts.push(card({ title: 'Nutzung', accent: 'Nutzung', sub: 'Gezählt werden Abrufe des Steckbriefs, Vorschauen, Abfragen und geladene Dateien durch Claude-Sessions, Kommandozeile und Portal; Suchtreffer und Ansichten im Portal stehen getrennt.' },
        el('div.dk-use__nums',
          el('div', el('span.label', 'insgesamt'), el('strong', fmt.int(u.total))),
          el('div', el('span.label', 'letzte 30 Tage'), el('strong', fmt.int(u.d30))),
          el('div', el('span.label', 'zuletzt'), el('strong', u.last ? fmt.ago(sqlDate(u.last)) : 'nie'))),
        daily.length ? spark(daily) : null,
        Object.keys(u.by_tool ?? {}).length ? toolBars(u.by_tool) : null,
        arr(u.recent).length ? el('div', el('span.label', 'Zuletzt'), el('ul.dk-recent', arr(u.recent).map((r) => el('li',
          el('span.dk-recent__ts', fmt.dateTime(sqlDate(r.ts))), el('span', TOOLS[r.tool] ?? r.tool), el('span.muted', kids(r.actor, r.project).join(' · ')))))) : null));
    } else parts.push(card({ title: 'Nutzung', accent: 'Nutzung' }, el('p.small.muted', 'Der Server zählt Nutzungen noch nicht (nach dem nächsten Neustart).')));
    parts.push(card({ title: 'Auf Folien', accent: 'Folien', sub: slides.length ? `${slides.length} ${slides.length === 1 ? 'Folie nennt' : 'Folien nennen'} diesen Datensatz als Quelle.` : null },
      slides.length ? el('ul.dk-life__list', slides.map((s) => el('li', el('a', { href: `#folien?p=${encodeURIComponent(String(s.ref ?? '').split('/')[0])}` }, s.title ?? s.ref), el('div.small.muted', s.ref ?? '')))) : el('p.small.muted', 'Keine Folie nennt ihn bisher.')));
    if (Array.isArray(d.research)) {
      // Hypotheses and findings that use this dataset (GET /api/datasets/<id> → research [{ref, code, title, verdict, version}])
      const rs = d.research;
      parts.push(card({ title: 'Hypothesen und Findings, die diesen Datensatz nutzen', accent: 'Hypothesen', sub: rs.length ? `${rs.length} in der Forschung` : null },
        rs.length ? el('ul.dk-life__list.fo-dsuse', rs.map((x) => {
          const isF = String(x.ref ?? '').startsWith('finding:');
          const id = String(x.ref ?? '').split(':')[1] ?? x.id;
          return el('li', el('a', { href: isF ? `#forschung?f=${id}` : `#forschung?h=${id}` }, el('strong', x.code ?? x.ref), ' ', x.title ?? ''),
            el('div.row', isF ? el('span.fo-pill', 'Finding') : resultPill(x.verdict),
              x.version != null && x.version < d.version ? badge(`nutzt Version ${x.version}`, 'warn') : null));
        })) : el('p.small.muted', 'Keine Hypothese und kein Finding nutzt ihn bisher.')));
    }
    parts.push(card({ title: 'Darauf aufbauend', accent: 'aufbauend' },
      ub.length ? el('ul.dk-life__list', ub.map((x) => el('li', el('a', { href: `#daten?d=${String(x.ref).split(':')[1]}` }, `${x.ref} ${x.title ?? ''}`),
        x.input_version < d.version ? el('div', badge(`nutzt Version ${x.input_version}`, 'warn')) : null))) : el('p.small.muted', 'Kein Datensatz baut darauf auf.'),
      arr(d.tracker_series).length ? el('p.small', el('strong', 'Zeitreihen in Fundus: '), arr(d.tracker_series).join(', ')) : null));
    return el('div.dk-use', ...parts);
  }

  function spark(daily) {
    const W = 540, H = 56, n = 90;
    const map = new Map(daily.map(([day, v]) => [day, v]));
    const today = new Date();
    const days = Array.from({ length: n }, (_, i) => { const t = new Date(today); t.setDate(t.getDate() - (n - 1 - i)); return t.toISOString().slice(0, 10); });
    const vals = days.map((x) => map.get(x) ?? 0);
    const max = Math.max(1, ...vals);
    const bw = W / n;
    const s = svgEl('svg', { class: 'dk-spark', viewBox: `0 0 ${W} ${H + 14}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': `Nutzungen je Tag, letzte 90 Tage, höchstens ${max}` },
      svgEl('line', { x1: 0, x2: W, y1: H, y2: H, class: 'dk-spark__base' }),
      vals.map((v, i) => (v ? svgEl('rect', { x: i * bw + 0.5, y: H - (v / max) * (H - 4), width: Math.max(1, bw - 1), height: (v / max) * (H - 4), class: 'dk-spark__bar' }, svgEl('title', {}, `${fmt.date(days[i])}: ${v}`)) : null)),
      svgEl('text', { x: 0, y: H + 12, class: 'dk-spark__t' }, fmt.date(days[0])), svgEl('text', { x: W, y: H + 12, 'text-anchor': 'end', class: 'dk-spark__t' }, 'heute'));
    return el('div', el('span.label', 'Je Tag, letzte 90 Tage'), s);
  }

  function toolBars(by) {
    const items = Object.entries(by).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...items.map((x) => x[1]));
    return el('div', el('span.label', 'Wie genutzt'), el('div.dk-top.dk-top--wide', items.map(([k, n]) => el('div.dk-top__row',
      el('span.dk-top__v', TOOLS[k] ?? k), el('span.dk-top__b', el('span', { style: { width: `${Math.max(2, (n / max) * 100)}%` } })), el('span.dk-top__n', fmt.int(n))))));
  }

  // ---------------------------------------------------------------- Dateien
  function filesTab() {
    const parts = [];
    if (d.holding === 'reference') {
      const loc = d.location ?? {};
      const [tone, word] = LOC[loc.status] ?? ['outline', loc.status ?? 'unbekannt'];
      let man = loc.manifest;
      if (typeof man === 'string') { try { man = JSON.parse(man); } catch { man = []; } }
      man = arr(man);
      parts.push(card({ title: 'Ort der Rohdaten', accent: 'Ort', sub: 'Referenz: Fundus kopiert und liest diese Dateien nicht; nachts prüft es, ob sie noch da und unverändert sind.' },
        dl([['Pfad', el('code.dk-code', loc.path ?? '–')], ['Zustand', badge(word, tone)], ['Grösse', fmt.bytes(loc.bytes)], ['Dateien', fmt.int(loc.files)],
          ['Neueste Änderung', loc.newest_mtime ? fmt.dateTime(sqlDate(loc.newest_mtime)) : '–'], ['Zuletzt geprüft', loc.checked_at ? fmt.dateTime(sqlDate(loc.checked_at)) : '–']]),
        man.length ? el('div.table-wrap.ds-scroll', el('table.table.table--compact', el('thead', el('tr', el('th', 'Datei'), el('th.num', 'Grösse'), el('th', 'geändert'))),
          el('tbody', man.slice(0, 500).map((m) => (typeof m === 'string' || m.more ? el('tr', el('td.small.muted', { colspan: '3' }, typeof m === 'string' ? m : m.name))
            : el('tr', el('td.dk-path', m.name), el('td.num', fmt.bytes(m.bytes)), el('td.small', m.mtime ? fmt.date(typeof m.mtime === 'number' ? m.mtime * 1000 : sqlDate(m.mtime)) : '–')))))))
          : el('p.small.muted', 'Keine Dateiliste.')));
    }
    const tabs = arr(d.tables_detail);
    if (tabs.length) {
      const q = tabs.length > 30 ? el('input.input', { type: 'search', placeholder: `${fmt.int(tabs.length)} Tabellen filtern`, 'aria-label': 'Tabellen filtern' }) : null;
      const tbody = el('tbody');
      const fix = (t) => (t.verified_ok == null ? el('span.small.muted', 'ungeprüft')
        : t.verified_ok ? el('span.dk-fixok', { title: `geprüft ${fmt.dateTime(sqlDate(t.verified_at))}` }, glyph('check', 'in Ordnung', 'ok'), ` ${fmt.date(sqlDate(t.verified_at))}`)
          : badge(`fehlerhaft (${fmt.date(sqlDate(t.verified_at))})`, 'danger'));
      const pq = (t) => (String(t.path ?? '').endsWith('.parquet') && t.format !== 'parquet'
        ? el('a.btn.btn--small', { href: `/datasets/${String(t.path).split('/').pop()}?name=${encodeURIComponent(`${t.table}.parquet`)}`, download: '' }, 'Parquet') : null);
      const fill = () => {
        const w = (q?.value ?? '').toLowerCase();
        const list = tabs.filter((t) => !w || `${t.table} ${t.file}`.toLowerCase().includes(w));
        put(tbody, ...list.slice(0, 300).map((t) => el('tr',
          el('td', el('strong', t.table), t.table !== t.file ? el('div.small.muted', t.file) : null, el('div.dk-path', { title: 'Pfad auf dem Spark – Sessions laden ihn mit pandas.read_parquet oder DuckDB' }, t.path)),
          el('td.small', t.format), el('td.num', fmt.int(t.rows)), el('td.num', fmt.bytes(t.bytes)), el('td', fix(t)),
          el('td', el('div.row', el('a.btn.btn--small', { href: t.download_url, download: '' }, 'Original'), pq(t))))),
        list.length > 300 ? el('tr', el('td.small.muted', { colspan: '6' }, `${fmt.int(list.length - 300)} weitere – Filter eingrenzen.`)) : '');
      };
      q?.addEventListener('input', fill);
      fill();
      parts.push(card({ title: 'Tabellen', accent: 'Tabellen', sub: 'Original zum Herunterladen, Parquet-Kopie für pandas und DuckDB. „Prüfsumme“: wann Fundus die Datei zuletzt nachgerechnet hat.' },
        q, el('div.table-wrap.ds-scroll', el('table.table.table--compact.dk-files', el('thead', el('tr', ['Tabelle', 'Format', 'Zeilen', 'Grösse', 'Prüfsumme', 'Herunterladen'].map((h, i) => el('th', { class: i === 2 || i === 3 ? 'num' : null }, h)))), tbody))));
    }
    const docs = arr(d.documents);
    parts.push(card({ title: 'Begleitdokumente', accent: 'Begleitdokumente' },
      docs.length ? el('ul.ds-docs', docs.map((x) => el('li', el('a', { href: x.download_url, download: '' }, x.file), el('span.small.muted', ` · ${x.format} · ${fmt.bytes(x.bytes)}`),
        x.verified_ok === 0 || x.verified_ok === false ? el('span', ' ', badge('Prüfsumme fehlerhaft', 'danger')) : null))) : el('p.small.muted', 'Keine (Codebuch, Methodik als PDF, Markdown oder Word mit „Neue Version …“ ergänzen).')));
    if (d.how_to_use) parts.push(card({ title: 'In einer Session laden', accent: 'Session' }, el('p.small.dk-hint', d.how_to_use)));
    return el('div.dk-files__wrap', ...parts);
  }

  load();
}

/** Zurück in den Katalog mit den zuletzt gesetzten Filtern (daten-katalog.js merkt sie sich). */
function lastCatalog() {
  try { return sessionStorage.getItem('fundus-daten-katalog') || '#daten'; } catch { return '#daten'; }
}
