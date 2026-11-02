/**
 * Ansicht „Datensätze“ (#daten, ein Datensatz: #daten?d=<slug>): grosse Tabellen in Fundus (fundus/datasets.py).
 * Liste: suchen (Titel, Beschreibung, Schlagworte, Spaltennamen und -bedeutungen, Quelle), nach Projekt filtern,
 * hochladen (Dateien, Excel-Mappen, Begleitdokumente). Detail: Beschreibung (eigene + vom lokalen Modell), Quelle und
 * Abdeckung bearbeiten, je Tabelle das Spaltenprofil und eine blätterbare, sortierbare Vorschau, SQL-Abfrage (nur lesen),
 * Downloads (Original, Parquet), Begleitdokumente, Versionen; neu beschreiben lassen, neue Version, löschen (30 Tage).
 * Frage an den Bestand: Freitext an das lokale Modell (POST /api/datasets/ask {question}), Antwort ja/teilweise/nein mit
 * Treffern, Spalten und verwandten Datensätzen (fundus/dataset_ask.py).
 * Daten: GET /api/datasets, /api/datasets/<id>, /api/datasets/<id>/preview; POST /api/datasets/<id> {action},
 * /api/datasets/<id>/query {sql}, /api/datasets/upload (multipart).
 */
import { el, pageHead, card, emptyNote, segmented, badge, confirmDialog } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);
const VIS = [{ value: 'public', label: 'öffentlich' }, { value: 'internal', label: 'intern (Kunde/eigene)' }];
const FREQ = [['', '– kein Rhythmus –'], ['annual', 'jährlich'], ['semiannual', 'halbjährlich'], ['quarterly', 'vierteljährlich'], ['monthly', 'monatlich'], ['irregular', 'unregelmässig'], ['once', 'einmalig']];
const DUE = { overdue: 'Update fällig', never_checked: 'Update: noch nie geprüft', stale_check: 'Update: lange nicht geprüft' };

async function api(path, body) {
  const r = await fetch(`/api/datasets${path}`, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error ?? `HTTP ${r.status}`);
  return d;
}
const hp = () => new URLSearchParams(location.hash.split('?')[1] ?? '');
const cell = (v) => (v === null || v === undefined ? el('span.muted', '–') : String(v));

export function render(root, ctx) {
  let alive = true;
  const ref = hp().get('d');
  if (ref) detail(root, ctx, ref, () => alive);
  else list(root, ctx, () => alive);
  return () => { alive = false; };
}

// ---------------------------------------------------------------- Liste
function list(root, ctx, alive) {
  const st = { q: hp().get('q') ?? '', project: hp().get('p') ?? '', removed: hp().get('r') === '1' };
  const up = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen …');
  const q = el('input.input.ds-q', { type: 'search', placeholder: 'Datensätze suchen – Thema, Spalte, Quelle …', value: st.q, 'aria-label': 'Datensätze suchen' });
  const psel = el('select.input', { 'aria-label': 'Projekt' });
  const shown = segmented([{ value: '', label: 'Aktiv' }, { value: '1', label: 'Gelöscht' }], st.removed ? '1' : '', (v) => { st.removed = v === '1'; load(); }, 'Anzeige');
  const info = el('p.small.muted');
  const box = el('div.ds-list');
  root.append(pageHead({ title: 'Datensätze', accent: 'Datensätze',
    lede: 'Grosse Tabellen für Analysen – gespeichert, beschrieben und auffindbar. Das lokale Modell auf der Spark beschreibt jeden Datensatz und seine Spalten; deine Angaben haben Vorrang. Claude-Sessions übergeben Datensätze mit fundus_dataset_add und finden sie mit fundus_dataset_search.',
    tools: [up] }), askBox(ctx, alive), el('div.row.ds-tools', q, psel, shown.el), info, box);
  up.addEventListener('click', () => uploadDialog(ctx, null, (r) => { location.hash = `#daten?d=${r.slug}`; }));
  let tmr;
  q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = q.value.trim(); load(); }, 300); });
  psel.addEventListener('change', () => { st.project = psel.value; load(); });
  let projects = null;

  async function load() {
    const p = new URLSearchParams();
    if (st.q) p.set('q', st.q);
    if (st.project) p.set('project', st.project);
    if (st.removed) p.set('removed', '1');
    history.replaceState(null, '', `#daten${st.q || st.project || st.removed ? `?${new URLSearchParams({ ...(st.q ? { q: st.q } : {}), ...(st.project ? { p: st.project } : {}), ...(st.removed ? { r: '1' } : {}) })}` : ''}`);
    try {
      const d = await api(`?${p}`);
      if (!alive()) return;
      if (!projects) {
        projects = [...new Set(arr(d.datasets).map((x) => x.project).filter(Boolean))].sort();
        psel.replaceChildren(el('option', { value: '' }, 'Alle Projekte'), ...projects.map((x) => el('option', { value: x }, x)));
        psel.value = st.project;
      }
      const s = d.stats ?? {};
      info.textContent = `${fmt.int(d.total)} ${d.total === 1 ? 'Datensatz' : 'Datensätze'}${!st.q && !st.project && !st.removed ? ` · zusammen ${fmt.int(s.rows)} Zeilen, ${fmt.bytes(s.bytes)}${s.pending ? ` · ${fmt.int(s.pending)} werden gerade beschrieben` : ''}` : ''}`;
      box.replaceChildren(...(arr(d.datasets).length ? arr(d.datasets).map(item)
        : [emptyNote(st.removed ? 'Nichts gelöscht.' : st.q || st.project ? 'Kein Datensatz passt.' : 'Noch keine Datensätze. „Hochladen …“ oder in einer Claude-Session mit fundus_dataset_add.')]));
    } catch (e) { info.textContent = `Nicht ladbar: ${e.message}`; }
  }
  function item(x) {
    const a = el('a.card.ds-item', { href: `#daten?d=${x.slug}` },
      el('div.ds-item__head', el('strong', x.title), x.update_due ? badge(DUE[x.update_due], x.update_due === 'overdue' ? 'danger' : 'warn') : null,
        x.status === 'new' ? badge('wird beschrieben', 'outline') : x.status === 'failed' ? badge('Beschreibung fehlgeschlagen', 'danger') : null,
        x.visibility === 'internal' ? badge('intern', 'warn') : null,
        x.kind === 'crosswalk' ? badge('Umschlüsselung', 'outline') : null,
        arr(x.stale_inputs).length ? badge('Eingangsdaten neuer – neu aufbauen', 'warn') : null),
      x.summary ? el('p.small.ds-item__sum', x.summary) : null,
      el('p.small.muted', [x.publisher, x.period, x.geo, x.tables ? `${fmt.int(x.rows)} Zeilen × ${fmt.int(x.columns)} Spalten${x.tables > 1 ? ` in ${x.tables} Tabellen` : ''}` : null,
        fmt.bytes(x.bytes), arr(x.formats).join(', '), x.project ? `Projekt ${x.project}` : null, x.removed_at ? `gelöscht ${fmt.date(x.removed_at.replace(' ', 'T'))}` : null].filter(Boolean).join(' · ')),
      arr(x.keywords).length ? el('div.ds-kw', arr(x.keywords).slice(0, 8).map((k) => el('span.ds-chip', k))) : null);
    return a;
  }
  load();
}

// ---------------------------------------------------------------- Frage an den Bestand (lokales Modell)
const ANSWER = { ja: ['Ja', 'ok'], teilweise: ['Teilweise', 'warn'], nein: ['Nein', 'outline'] };

function askBox(ctx, alive) {
  const input = el('input.input.ds-ask__q', { type: 'search', placeholder: 'z. B. Haben wir Jobpostings zum Marketing?', 'aria-label': 'Frage an den Bestand' });
  const go = el('button.btn.btn--primary', { type: 'submit' }, 'Fragen');
  const out = el('div.ds-ask__out', { 'aria-live': 'polite' });
  const form = el('form.row.ds-ask__form', input, go);
  let run = 0;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const question = input.value.trim();
    if (question.length < 3) { input.focus(); return; }
    const my = ++run;
    go.disabled = true;
    out.replaceChildren(el('p.small.muted.ds-ask__wait', el('span.ds-ask__spin', { 'aria-hidden': 'true' }), 'Gemma liest den Katalog … (etwa 1–2 Minuten)'));
    try {
      const r = await api('/ask', { question });
      if (!alive() || my !== run) return;
      out.replaceChildren(answer(r));
    } catch (err) {
      if (my === run) out.replaceChildren(el('div.alert.alert--error', `Keine Antwort: ${err.message}`));
    } finally { if (my === run) go.disabled = false; }
  });
  return card({ title: 'Frage an den Bestand', accent: 'Frage', sub: 'Gemma 4, lokal auf der Spark – antwortet aus den Beschreibungen, nicht aus den Daten. Treffer vor der Verwendung im Datensatz prüfen.' },
    form, out);
}

function answer(r) {
  const [label, tone] = ANSWER[r.answer] ?? ANSWER.nein;
  const meta = (x) => [x.ref, x.publisher, x.period, x.geo, x.rows ? `${fmt.int(x.rows)} Zeilen` : null].filter(Boolean).join(' · ');
  const hit = (x) => el('a.card.ds-item.ds-ask__hit', { href: `#daten?d=${x.slug ?? x.id}` },
    el('div.ds-item__head', el('strong', x.title), badge(x.fit === 'direkt' ? 'direkter Treffer' : 'teilweise', x.fit === 'direkt' ? 'ok' : 'warn'),
      x.visibility === 'internal' ? badge('intern', 'warn') : null),
    x.why ? el('p.small', x.why) : null,
    arr(x.columns).length ? el('div.ds-kw', el('span.small.muted', 'Spalten:'), arr(x.columns).map((c) => el('span.ds-chip', c))) : null,
    el('p.small.muted', meta(x)));
  const rel = (x) => el('li', el('a', { href: `#daten?d=${x.slug ?? x.id}` }, x.title), x.why ? el('span.small.muted', ` – ${x.why}`) : null);
  return el('div.ds-ask__answer',
    el('div.ds-ask__verdict', badge(label, tone), r.summary ? el('p', r.summary) : null),
    arr(r.hits).length ? el('div.ds-list', arr(r.hits).map(hit)) : null,
    arr(r.related).length ? el('div.ds-ask__rel', el('h4', 'Verwandt'), el('ul', arr(r.related).map(rel))) : null,
    el('p.small.muted', `${r.candidates ?? 0} Datensätze genauer angesehen · ${r.cached ? 'aus dem Zwischenspeicher' : `${fmt.int(Math.round(r.seconds ?? 0))} s`} · ${r.model ?? ''}`));
}

// ---------------------------------------------------------------- Detail
function detail(root, ctx, ref, alive) {
  const head = el('div');
  const body = el('div.ds-detail');
  root.append(el('p', el('a', { href: '#daten' }, '← Alle Datensätze')), head, body);
  let d = null;
  let tableIdx = 0;

  async function load() {
    try {
      d = await api(`?ref=${encodeURIComponent(ref)}`);      // Kennung (slug) oder Nummer
      if (!alive()) return;
      draw();
    } catch (e) { head.replaceChildren(el('div.alert.alert--error', e.message)); }
  }

  async function save(changes, msg) {
    try { d = await api(`/${d.id}`, { action: 'update', changes }); ctx.toast(msg ?? 'Gespeichert.'); draw(); }
    catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 6000); }
  }

  function field(key, label, multi) {
    const f = el(multi ? 'textarea.input.ds-field' : 'input.input.ds-field', { 'aria-label': label, placeholder: label, rows: multi ? 4 : undefined });
    f.value = d[key] ?? '';
    f.addEventListener('change', () => save({ [key]: f.value }, `${label} gespeichert.`));
    return el('label.ds-row', el('span.label', label), f);
  }

  function draw() {
    const tabs = arr(d.tables_detail);
    tableIdx = Math.min(tableIdx, Math.max(0, tabs.length - 1));
    const title = el('input.input.ds-title', { value: d.title, 'aria-label': 'Titel' });
    title.addEventListener('change', () => { if (title.value.trim()) save({ title: title.value.trim() }, 'Titel gespeichert.'); });
    const again = el('button.btn', { type: 'button' }, 'Neu beschreiben lassen');
    again.addEventListener('click', async () => { try { await api(`/${d.id}`, { action: 'redescribe' }); ctx.toast('In der Warteschlange – das lokale Modell beschreibt den Datensatz neu.'); } catch (e) { ctx.toast(e.message, 5000); } });
    const ver = el('button.btn', { type: 'button' }, 'Neue Version …');
    ver.addEventListener('click', () => uploadDialog(ctx, d, () => load()));
    const del = el('button.btn', { type: 'button' }, d.removed_at ? 'Wiederherstellen' : 'Löschen');
    del.addEventListener('click', async () => {
      if (!d.removed_at && !await confirmDialog({ title: 'Datensatz löschen?', text: `„${d.title}“ verschwindet aus Liste und Suche und wird nach 30 Tagen endgültig gelöscht; bis dahin unter „Gelöscht“ wiederherstellbar.`, confirmLabel: 'Löschen' })) return;
      try { await api(`/${d.id}`, { action: d.removed_at ? 'restore' : 'remove' }); ctx.toast(d.removed_at ? 'Wiederhergestellt.' : 'Gelöscht – 30 Tage wiederherstellbar.'); load(); } catch (e) { ctx.toast(e.message, 5000); }
    });
    head.replaceChildren(el('div.ds-headrow', title, el('div.row.ds-actions', again, ver, del)),
      el('p.small.muted', [`dataset:${d.id}`, d.version > 1 ? `Version ${d.version}` : null, d.tables ? `${fmt.int(d.rows)} Zeilen × ${fmt.int(d.columns)} Spalten` : null,
        fmt.bytes(d.bytes), d.created_by ? `von ${d.created_by}` : null, d.removed_at ? `gelöscht ${fmt.dateTime(d.removed_at.replace(' ', 'T'))}` : null].filter(Boolean).join(' · '),
      d.status === 'new' ? el('span', ' · ', badge('wird beschrieben', 'outline')) : d.status === 'failed' ? el('span', ' · ', badge(`Beschreibung fehlgeschlagen: ${d.error ?? ''}`, 'danger')) : null));
    const vis = segmented(VIS, d.visibility, (v) => save({ visibility: v }, 'Sichtbarkeit gespeichert.'), 'Sichtbarkeit');
    const about = card({ title: 'Beschreibung', accent: 'Beschreibung', sub: d.described_at ? `Zusammenfassung vom lokalen Modell (${fmt.dateTime(d.described_at.replace(' ', 'T'))}); deine eigene Beschreibung und Angaben haben Vorrang.` : null },
      ...kids(d.summary_de || d.summary ? el('p.ds-sum', d.summary_de || d.summary) : null,
        d.unit_of_observation ? el('p.small', el('strong', 'Eine Zeile ist: '), d.unit_of_observation) : null,
        arr(d.keywords).length ? el('div.ds-kw', arr(d.keywords).map((k) => el('span.ds-chip', k))) : null,
        Object.values(d.tags ?? {}).flat().length ? el('div.ds-kw', Object.entries(d.tags).flatMap(([f, vs]) => arr(vs).map((v) => el('span.ds-chip.ds-chip--tag', { title: f }, v)))) : null,
        field('description', 'Eigene Beschreibung (Herkunft, Erhebung, Besonderheiten)', true)));
    const src = card({ title: 'Quelle und Abdeckung', accent: 'Quelle' },
      el('div.ds-grid', field('publisher', 'Herausgeber'), field('source_title', 'Titel der Quelle'), field('url', 'URL'), field('license', 'Lizenz'),
        field('published', 'Veröffentlicht'), field('period', 'Zeitraum'), field('geo', 'Region'), field('project', 'Projekt')),
      el('div.ds-row', el('span.label', 'Sichtbarkeit'), vis.el));
    body.replaceChildren(el('div.ds-two', about, src), editionsCard(), linksCard(), tablesCard(tabs), docsCard(), queryCard(tabs));
  }

  /** Verknüpfung: Umschlüsselung (crosswalk), Herkunft (Eingangsdatensätze, veraltet?), wer darauf aufbaut, Code-Systeme,
   *  Tabellen über alle Versionen. Bearbeiten über fundus_dataset(action='update') – hier nur der Tabellen-Umfang. */
  function linksCard() {
    const ins = arr(d.inputs);
    const ub = arr(d.used_by);
    const xw = d.crosswalk ?? null;
    const systems = arr(d.code_systems);
    const link = (x) => el('a', { href: `#daten?d=${x.ref.split(':')[1]}` }, `${x.ref} ${x.title ?? ''}`);
    const scope = segmented([{ value: 'version', label: 'nur aktuelle Version' }, { value: 'merged', label: 'über alle Versionen' }],
      d.table_scope ? 'merged' : 'version', (v) => save({ table_scope: v }, v === 'merged' ? 'Tabellen über alle Versionen.' : 'Nur Tabellen der aktuellen Version.'), 'Tabellen');
    const rows = kids(
      xw ? el('div.ds-row', el('span.label', 'Umschlüsselung'),
        el('div', arr(xw.pairs).map((p) => el('div.small', `${p.from_system}${p.from_version ? ` (${p.from_version})` : ''} → ${p.to_system}${p.to_version ? ` (${p.to_version})` : ''}${p.rows != null ? ` · ${fmt.int(p.rows)} Zeilen` : ''}`)),
          el('div.small.muted', [xw.method ? `Methode ${xw.method}` : null, xw.quality ? `Qualität ${xw.quality}` : null,
            xw.valid_from || xw.valid_to ? `gültig ${xw.valid_from ?? '…'} bis ${xw.valid_to ?? '…'}` : null].filter(Boolean).join(' · ')))) : null,
      ins.length ? el('div.ds-row', el('span.label', 'Aufgebaut aus'),
        el('div', ins.map((x) => el('div.small', link(x), ` · Version ${x.version}`,
          x.stale ? el('span', ' ', badge(`neuere Version ${x.current_version}`, 'warn')) : null,
          x.visibility === 'internal' ? el('span', ' ', badge('intern', 'warn')) : null, x.note ? el('span.muted', ` · ${x.note}`) : null)))) : null,
      d.stale ? el('p.small', badge('veraltet', 'warn'), ' Eine Eingangsquelle hat eine neuere Version – Datensatz neu aufbauen und als neue Version ablegen.') : null,
      ub.length ? el('div.ds-row', el('span.label', 'Darauf bauen auf'), el('div', ub.map((x) => el('div.small', link(x), x.input_version < d.version ? el('span', ' ', badge(`nutzt Version ${x.input_version}`, 'warn')) : null)))) : null,
      systems.length ? el('div.ds-row', el('span.label', 'Code-Systeme'), el('div.ds-kw', systems.map((x) => el('span.ds-chip.ds-chip--tag', x)))) : null,
      d.version > 1 ? el('div.ds-row', el('span.label', 'Tabellen'), scope.el) : null);
    if (!rows.length) return el('span');
    return card({ title: 'Verknüpfung', accent: 'Verknüpfung', sub: 'Umschlüsselungen, Herkunft und Code-Systeme – Sessions setzen sie mit fundus_dataset_add / fundus_dataset(action=\'update\'); fundus_crosswalk schlägt Codes nach.' }, ...rows);
  }

  /** Neue Ausgaben: Rhythmus, nächste Ausgabe, wo prüfen, zuletzt geprüft – fundus_update_targets erinnert daran. */
  function editionsCard() {
    const fsel = el('select.input', { 'aria-label': 'Rhythmus' }, FREQ.map(([v, l]) => el('option', { value: v }, l)));
    fsel.value = d.frequency ?? '';
    fsel.addEventListener('change', () => save({ frequency: fsel.value || null }, 'Rhythmus gespeichert.'));
    const nxt = el('input.input', { type: 'month', value: (d.next_expected ?? '').slice(0, 7), 'aria-label': 'Nächste Ausgabe' });
    nxt.addEventListener('change', () => save({ next_expected: nxt.value || null }, 'Nächste Ausgabe gespeichert.'));
    const ok = el('button.btn', { type: 'button' }, 'Heute geprüft');
    ok.addEventListener('click', async () => {
      try { d = await api(`/${d.id}`, { action: 'checked' }); ctx.toast(`Als geprüft vermerkt${d.next_expected ? ` – nächste Ausgabe ${d.next_expected}` : ''}.`); draw(); }
      catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 6000); }
    });
    return card({ title: 'Neue Ausgaben', accent: 'Ausgaben', sub: 'Für regelmässig erscheinende Daten: Fundus erinnert Sessions daran (fundus_update_targets) und zeigt „Update fällig“. „Heute geprüft“ rückt eine verstrichene nächste Ausgabe um einen Rhythmus weiter.' },
      ...kids(d.update_due ? el('p', badge(DUE[d.update_due], d.update_due === 'overdue' ? 'danger' : 'warn')) : null,
        el('div.ds-grid', el('label.ds-row', el('span.label', 'Rhythmus'), fsel), el('label.ds-row', el('span.label', 'Nächste Ausgabe erwartet'), nxt)),
        field('update_hint', 'Wo und wie prüfen (Download-URL, Skript zum Neuaufbau)', true),
        el('div.row.ds-dl', ok, el('span.small.muted', d.last_checked ? `zuletzt geprüft ${fmt.date(d.last_checked)}` : 'noch nie geprüft'),
          arr(d.tracker_series).length ? el('span.small.muted', `· verknüpfte Zeitreihen: ${d.tracker_series.join(', ')}`) : null)));
  }

  function tablesCard(tabs) {
    if (!tabs.length) return card({}, emptyNote('Dieser Datensatz hat keine Tabelle.'));
    const t = tabs[tableIdx];
    const pick = tabs.length > 1 ? segmented(tabs.map((x, i) => ({ value: String(i), label: `${x.table} (${fmt.int(x.rows)})` })), String(tableIdx),
      (v) => { tableIdx = Number(v); draw(); }, 'Tabelle').el : null;
    const prof = el('div.table-wrap', el('table.table.table--compact.ds-prof',
      el('thead', el('tr', ['Spalte', 'Typ', 'Bedeutung', 'leer', 'verschieden', 'min', 'max', 'Ø', 'Beispiele'].map((h) => el('th', h)))),
      el('tbody', arr(t.columns).map((c) => el('tr', el('td', el('strong', c.name), c.code_system ? el('div', el('span.ds-chip.ds-chip--tag', { title: 'Code-System' }, c.code_system)) : null), el('td.muted', c.type), el('td', cell(c.meaning)),
        el('td.num', c.nulls_pct != null ? fmt.pct(c.nulls_pct, 1) : '–'), el('td.num', fmt.int(c.distinct)),
        el('td', cell(c.min)), el('td', cell(c.max)), el('td', c.avg != null ? fmt.num2(Number(c.avg)) : '–'),
        el('td.small', arr(c.top).length ? arr(c.top).slice(0, 4).map((x) => `${x[0]} (${fmt.int(x[1])})`).join(' · ') : arr(c.examples).join(' · ')))))));
    const grid = el('div.ds-grid-prev');
    const pager = el('div.row.ds-pager');
    const big = (t.rows ?? 0) >= 5000000;      // Sortieren liest dann die ganze Tabelle auf dem Server (der Browser bekommt nur 100 Zeilen)
    let off = 0, order = null;
    async function page() {
      grid.replaceChildren(el('p.small.muted', order && big ? `Sortiere ${fmt.int(t.rows)} Zeilen auf dem Server …` : 'Lade …'));
      try {
        const p = await api(`/${d.id}/preview?table=${t.id}&offset=${off}&limit=100${order ? `&order=${encodeURIComponent(order)}` : ''}`);
        const ths = p.columns.map((c) => {
          const th = el('th', { title: big ? `Sortieren – durchsucht alle ${fmt.int(t.rows)} Zeilen, dauert bei dieser Grösse etwas` : 'Sortieren' }, c, order?.startsWith(`${c}:`) ? (order.endsWith(':desc') ? ' ↓' : ' ↑') : '');
          th.addEventListener('click', () => { order = order === `${c}:asc` ? `${c}:desc` : `${c}:asc`; off = 0; page(); });
          return th;
        });
        grid.replaceChildren(el('div.table-wrap.ds-scroll', el('table.table.table--compact.ds-data', el('thead', el('tr', el('th.muted', '#'), ...ths)),
          el('tbody', p.rows.map((r, i) => el('tr', el('td.muted.num', fmt.int(off + i + 1)), ...r.map((v) => el('td', { title: v == null ? '' : String(v) }, cell(v)))))))));
        const prev = el('button.btn.btn--small', { type: 'button', disabled: off === 0 }, '← zurück');
        const next = el('button.btn.btn--small', { type: 'button', disabled: off + 100 >= p.total }, 'weiter →');
        prev.addEventListener('click', () => { off = Math.max(0, off - 100); page(); });
        next.addEventListener('click', () => { off += 100; page(); });
        pager.replaceChildren(prev, el('span.small.muted', `Zeilen ${fmt.int(off + 1)}–${fmt.int(Math.min(off + 100, p.total))} von ${fmt.int(p.total)}`), next);
      } catch (e) { grid.replaceChildren(el('p.small', `Vorschau nicht ladbar: ${e.message}`)); }
    }
    page();
    return card({ title: 'Tabellen', accent: 'Tabellen', sub: `${t.file}${t.table !== t.file ? ` · Tabelle ${t.table}` : ''} · ${fmt.int(t.rows)} Zeilen · ${fmt.bytes(t.bytes)} · Spaltenprofil und Vorschau: der Browser lädt immer nur 100 Zeilen, egal wie gross die Tabelle ist (Klick auf eine Spalte sortiert).${t.sampled ? ` Beispiele und häufige Werte aus einer Stichprobe von ${fmt.int(t.sampled)} Zeilen (hochgerechnet); Zeilen, min, max und Ø sind exakt.` : ''}` },
      ...kids(pick, el('div.row.ds-dl', el('a.btn.btn--small', { href: t.download_url, download: '' }, `Original (${t.format})`),
        el('span.small.muted.ds-path', { title: 'Pfad auf dem Spark – Sessions laden ihn mit pandas.read_parquet oder DuckDB' }, t.path)),
      el('span.label', 'Spalten'), prof, el('span.label', 'Vorschau'), pager, grid));
  }

  function docsCard() {
    const docs = arr(d.documents);
    const vers = arr(d.versions);
    if (!docs.length && vers.length < 2) return null;
    return card({ title: 'Dokumente und Versionen', accent: 'Dokumente' },
      ...kids(docs.length ? el('ul.ds-docs', docs.map((x) => el('li', el('a', { href: x.download_url, download: '' }, x.file), el('span.small.muted', ` · ${x.format} · ${fmt.bytes(x.bytes)}`)))) : null,
        vers.length > 1 ? el('ul.small.ds-docs', vers.map((v) => el('li', `Version ${v.n} · ${fmt.dateTime(v.created_at.replace(' ', 'T'))}${v.created_by ? ` · ${v.created_by}` : ''}${v.note ? ` · ${v.note}` : ''}`))) : null));
  }

  function queryCard(tabs) {
    if (!tabs.length) return null;
    const ta = el('textarea.input.ds-sql', { rows: 4, spellcheck: 'false', 'aria-label': 'SQL' });
    const first = tabs[0];
    const cols = arr(first.columns).slice(0, 3).map((c) => `"${c.name}"`).join(', ');
    ta.value = `SELECT ${cols || '*'}, count(*) AS n\nFROM t\nGROUP BY ALL\nORDER BY n DESC\nLIMIT 20`;
    const go = el('button.btn.btn--primary', { type: 'button' }, 'Ausführen');
    const out = el('div.ds-qout');
    const run = async () => {
      go.disabled = true; out.replaceChildren(el('p.small.muted', 'Läuft …'));
      try {
        const r = await api(`/${d.id}/query`, { sql: ta.value });
        out.replaceChildren(el('p.small.muted', `${fmt.int(r.rows.length)} Zeilen${r.truncated ? ' (gekürzt)' : ''}`),
          el('div.table-wrap.ds-scroll', el('table.table.table--compact.ds-data', el('thead', el('tr', r.columns.map((c) => el('th', c)))),
            el('tbody', r.rows.map((row) => el('tr', row.map((v) => el('td', cell(v)))))))));
      } catch (e) { out.replaceChildren(el('p.small.ds-err', e.message)); }
      go.disabled = false;
    };
    go.addEventListener('click', run);
    ta.addEventListener('keydown', (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); } });
    const views = tabs.map((x) => x.table).join(', ');
    return card({ title: 'Abfrage', accent: 'Abfrage', sub: `SQL nur lesend (DuckDB). Tabellen: ${views}; die erste heisst auch „t“. Ausführen: Knopf oder ⌘/Strg + Enter.` },
      ta, el('div.row', go), out);
  }

  load();
}

// ---------------------------------------------------------------- Hochladen
function uploadDialog(ctx, version, onDone) {
  const dlg = el('dialog.dialog.ds-upload');
  const files = el('input', { type: 'file', multiple: true, accept: '.csv,.tsv,.txt,.xlsx,.xlsm,.json,.jsonl,.ndjson,.parquet,.pdf,.md,.docx,.doc,.html,.pptx' });
  const title = el('input.input', { type: 'text', placeholder: 'Titel (sonst aus dem Dateinamen)' });
  const descr = el('textarea.input', { rows: 3, placeholder: 'Was ist das, woher, wie erhoben? (hilft dem Modell und der Suche)' });
  const pub = el('input.input', { type: 'text', placeholder: 'Herausgeber, z. B. BFS' });
  const urlI = el('input.input', { type: 'url', placeholder: 'URL der Quelle' });
  const proj = el('input.input', { type: 'text', placeholder: 'Projekt (optional)' });
  let visibility = 'public';
  const vis = segmented(VIS, visibility, (v) => { visibility = v; }, 'Sichtbarkeit');
  const note = el('input.input', { type: 'text', placeholder: 'Notiz zu dieser Version' });
  const bar = el('progress.ph-progress', { max: 100, value: 0, hidden: true });
  const msg = el('p.small.muted', 'Tabellen: CSV, TSV, Excel (jedes Blatt), JSON, Parquet – hier bis 2 GB je Datei; grössere über eine Claude-Session (fundus_dataset_add) oder fundus datasets add im Terminal. Begleitdokumente (Codebuch, Methodik): PDF, Markdown, Word. Mehrere Dateien = ein Datensatz.');
  const ok = el('button.btn.btn--primary', { type: 'button' }, 'Hochladen');
  const cancel = el('button.btn', { type: 'button' }, 'Schliessen');
  const row = (label, c) => el('label.ph-uprow', el('span.label', label), c);
  dlg.append(el('div.dialog__body', el('h3', version ? `Neue Version von „${version.title}“` : 'Datensatz hochladen'), row('Dateien', files),
    ...(version ? [row('Notiz', note)] : [row('Titel', title), row('Beschreibung', descr), el('div.ph-upgrid', row('Herausgeber', pub), row('URL', urlI), row('Projekt', proj)),
      row('Sichtbarkeit', vis.el)]), msg, bar, el('div.row.dialog__actions', cancel, ok)));
  cancel.addEventListener('click', () => { dlg.close(); dlg.remove(); });
  ok.addEventListener('click', () => {
    if (!files.files.length) { msg.textContent = 'Bitte Dateien wählen.'; return; }
    const huge = [...files.files].filter((f) => f.size > 2 * 1024 ** 3);
    if (huge.length) { msg.textContent = `${huge.map((f) => `${f.name} (${fmt.bytes(f.size)})`).join(', ')}: Dateien über 2 GB bitte über eine Claude-Session übergeben (fundus_dataset_add mit dem Pfad auf dem Spark) oder im Terminal: fundus datasets add <Pfad> – ohne Umweg über den Browser.`; return; }
    const fd = new FormData();
    for (const f of files.files) fd.append('files', f, f.name);
    const fields = version ? [['dataset', String(version.id)], ['note', note.value]]
      : [['title', title.value], ['description', descr.value], ['publisher', pub.value], ['url', urlI.value], ['project', proj.value], ['visibility', visibility]];
    for (const [k, v] of fields) if (v?.trim()) fd.append(k, v.trim());
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/datasets/upload');
    bar.hidden = false; ok.disabled = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { bar.value = Math.round((e.loaded / e.total) * 100); msg.textContent = `Lade hoch … ${bar.value} %`; } };
    xhr.upload.onload = () => { msg.textContent = 'Hochgeladen – Tabellen werden eingelesen und profiliert …'; };
    xhr.onload = () => {
      ok.disabled = false;
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch { /* leer */ }
      if (xhr.status !== 200) { msg.textContent = `Fehler: ${d.error ?? xhr.status}`; return; }
      ctx.toast(`Gespeichert: ${arr(d.tables).length} Tabellen${arr(d.documents).length ? `, ${arr(d.documents).length} Dokumente` : ''}. Das Modell beschreibt den Datensatz jetzt.`, 6000);
      dlg.close(); dlg.remove(); onDone?.(d);
    };
    xhr.onerror = () => { ok.disabled = false; msg.textContent = 'Hochladen fehlgeschlagen (Verbindung).'; };
    xhr.send(fd);
  });
  document.body.append(dlg);
  dlg.showModal();
}
