/**
 * Bausteine für Prüfungen (Fundus Review) im Portal: Freigabe-Dialog, Feedback-Einträge, Quellen und Datenpunkte einer
 * Folie mit Prüfstufe, Änderungsprotokoll.
 *
 * Zwei getrennte Wege: „Mit Gemini prüfen“ (kein Link, nichts wird veröffentlicht) und „Personen einladen“ (ein Link je
 * Person, Name aus der Personenliste; die Person gibt nichts ein). Daten: GET/POST /api/reviews, /api/reviewers,
 * GET /api/slide-review/<id>, POST /api/feedback (eigener Punkt, wieder öffnen).
 * Prüfstufen: ungeprüft · verifiziert (1 unabhängige Prüfung: Person oder anderes KI-System) · bestätigt (2+) · strittig.
 */
import { el, headline, badge, mark } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

const arr = (v) => (Array.isArray(v) ? v : []);
const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false);

export async function api(path, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : { cache: 'no-store' };
  const r = await fetch(path, opt);
  let data = null; try { data = await r.json(); } catch { /* kein JSON */ }
  if (!r.ok) throw new Error(data?.error ?? `HTTP ${r.status}`);
  return data;
}

export const STATUS = {
  open: ['offen', 'warning'], in_progress: ['in Arbeit', 'accent'], incorporated: ['eingearbeitet', 'success'],
  answered: ['beantwortet', 'outline'], rejected: ['zurückgewiesen', 'outline'], decision: ['deine Entscheidung', 'danger'],
};
export const REVIEW_STATUS = { draft: ['wird veröffentlicht', 'outline'], open: ['läuft', 'accent'], completed: ['abgeschlossen, Links lesbar', 'success'], closed: ['abgeschlossen', 'off'] };
export const GEMINI = { queued: 'Gemini: vorgemerkt', running: 'Gemini prüft', done: 'Gemini: fertig', failed: 'Gemini: fehlgeschlagen' };
const KIND = { comment: 'Kommentar', shape: 'Markierung', check: 'Prüfung' };
const VERDICT = { ok: 'korrekt', doubt: 'fraglich', wrong: 'falsch' };
const SEVERITY = { high: 'hoch', medium: 'mittel', low: 'niedrig' };
const NUMBER = { sourced: ['belegt', 'success'], derived: ['abgeleitet', 'outline'], wrong: ['falsch', 'danger'], unverified: ['nicht belegbar', 'danger'] };
const FOCUS = [{ v: 'sources', l: 'Quellen' }, { v: 'numbers', l: 'Zahlen' }, { v: 'logic', l: 'Logik' }, { v: 'language', l: 'Sprache' }];

/** Prüfstand einer Folie (GET /api/feedback/counts → status): Stufe mit Konfidenz 0–3 und Begründung im Tooltip. */
export const SLIDE_STAGE = {
  not_needed: ['keine Quellen nötig', 'outline'], no_sources: ['Quellen fehlen', 'danger'], unchecked: ['ungeprüft', 'warning'], open: ['offene Punkte', 'warning'],
  changed: ['seit Prüfung geändert', 'warning'], incorporated: ['eingearbeitet', 'success'], checked: ['geprüft', 'success'],
  confirmed: ['bestätigt', 'success'],
};
export function stageWhy(s) {
  if (!s) return '';
  const n = s.numbers;
  return [
    `Konfidenz ${s.level} von 3`,
    s.no_sources ? `im HTML als ohne Quellenbedarf markiert: ${s.no_sources}` : null,
    `Quellen: ${s.sources} verknüpft${s.embedded_sources ? `, ${s.embedded_sources} in der Datei` : ''}`,
    n ? `Zahlen (${s.numbers_version >= 1 ? `v${s.numbers_version}` : 'frühere Fassung'}): ${n.sourced} belegt, ${n.derived} abgeleitet, ${n.wrong} falsch, ${n.unverified} nicht belegbar` : null,
    s.checked_version == null ? 'noch nicht geprüft' : s.checked_version >= 1 ? `zuletzt geprüft: v${s.checked_version}, aktuell v${s.version}` : 'zuletzt geprüft: an einer früheren Fassung (Kopie)',
    ...(s.shared_with ?? []).map((t) => `Prüfstand geteilt mit „${t.set_title}“ (${t.slug} v${t.version})${t.own_version === s.version ? '' : ` bis v${t.own_version}, danach hier geändert`}${t.is_current ? '' : ', dort inzwischen geändert'}`),
    `Fehler: ${s.feedback.open} offen, ${s.feedback.decision} zur Entscheidung, ${s.feedback.done} verarbeitet${s.feedback.suggestions ? ` · ${s.feedback.suggestions} Anregungen (blockieren nicht)` : ''}`,
    s.reviewed_by?.length ? `durchgesehen von: ${s.reviewed_by.join(', ')}` : null,
    s.reviewers?.length ? `geprüft von: ${s.reviewers.join(', ')}` : null,
  ].filter(Boolean).join('\n');
}
export function stageChip(s, { long = false } = {}) {
  if (!s?.stage) return null;
  const [label, tone] = SLIDE_STAGE[s.stage] ?? [s.stage, 'outline'];
  const open = s.feedback.open + s.feedback.decision;
  const meter = el('span.rv-meter', { 'aria-hidden': 'true' }, [0, 1, 2].map((i) => el('span', { class: i < s.level ? `rv-meter__on rv-meter__on--${s.level}` : null })));
  return el('span.rv-stage', { title: stageWhy(s), 'aria-label': `Prüfstand: ${label}, Konfidenz ${s.level} von 3` },
    meter, badge(`${label}${s.stage === 'open' && open ? ` (${open})` : ''}`, tone), long ? el('span.small.muted', ` Konfidenz ${s.level}/3`) : null,
    verifiedNote(s));
}

/** Fixierte geprüfte Fassung, wenn die aktuelle Version neuer und noch nicht geprüft ist. */
export function verifiedNote(s) {
  const v = s?.verified;
  if (!v || v.is_current) return null;
  return el('span.rv-verified', { title: `v${v.version} ist ${v.label ?? 'geprüft'} und bleibt fixiert; die aktuelle v${s.version} ist noch nicht geprüft.` },
    `geprüft: v${v.version}`);
}

/** Fortschritt einer Person in einer Prüfung: Balken, „7 von 12 (58 %)“, Zustand. */
export const PROGRESS_STATE = { invited: ['eingeladen', 'outline'], active: ['in Arbeit', 'accent'], done: ['abgeschlossen', 'success'] };
export function progressRow(p, { compact = false } = {}) {
  const [st, tone] = PROGRESS_STATE[p.state] ?? [p.state, 'outline'];
  const bar = el('span.rv-pbar', { role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': p.total, 'aria-valuenow': p.done, 'aria-label': `${p.name}: ${p.done} von ${p.total} Folien` },
    el('span', { style: { width: `${p.percent}%` } }));
  return el('div.rv-prow', el('span.rv-prow__name', p.name), bar,
    el('span.small.rv-prow__num', `${p.done}/${p.total} (${p.percent} %)`), badge(st, tone),
    compact ? null : el('span.small.muted', [p.contributions ? `${p.contributions} Beiträge` : null, p.checks ? `${p.checks} Quellenurteile` : null,
      p.last ? `zuletzt ${fmt.dateTime(String(p.last).replace(' ', 'T'))}` : null].filter(Boolean).join(' · ')));
}

export function statusBadge(s) { const [t, tone] = STATUS[s] ?? [s, 'outline']; return badge(t, tone); }

/** Prüfstufe einer Quelle oder eines Datenpunkts. */
export function stageBadge(v) {
  if (!v) return null;
  const who = arr(v.ok).map((x) => `${x.reviewer}${x.at ? ` (${fmt.date(x.at.replace(' ', 'T'))})` : ''}`).join(', ');
  if (v.stage === 'disputed') return el('span', { title: arr(v.wrong).map((x) => `falsch: ${x.reviewer}${x.note ? ` – ${x.note}` : ''}`).join('\n') }, badge('strittig', 'danger'));
  if (v.stage === 'confirmed') return el('span', { title: `bestätigt von ${who}` }, badge(`bestätigt (${v.confirmations})`, 'success'));
  if (v.stage === 'verified') return el('span', { title: `verifiziert von ${who}` }, badge('verifiziert (1)', 'success'));
  return badge('ungeprüft', 'outline');
}

/** In die Zwischenablage kopieren. Über http:// (Tailnet) sperren Safari/iPad die Zwischenablage – dann wird das sichtbare
 *  Feld markiert, und man kopiert über das normale Menü. Gibt true zurück, wenn kopiert wurde. */
export async function copyText(text, toast, field = null) {
  const pick = () => { if (field) { field.focus(); field.select(); field.setSelectionRange(0, text.length); } };
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); toast?.('Link kopiert.'); pick(); return true; } catch { /* weiter unten */ }
  }
  pick();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  toast?.(ok ? 'Link kopiert.' : 'Link ist markiert – jetzt kopieren (⌘C oder „Kopieren“ im Menü).', ok ? 3200 : 6000);
  return ok;
}

/** Sichtbarer Link mit Kopier-Knopf: das Feld lässt sich antippen und markiert dann den ganzen Link. */
export function linkField(url, toast, { disabled = false } = {}) {
  const input = el('input.input.rv-link__input', { type: 'text', readonly: true, value: url ?? '', 'aria-label': 'Persönlicher Link', disabled: disabled || !url });
  input.addEventListener('focus', () => input.select());
  input.addEventListener('click', () => { input.select(); input.setSelectionRange(0, input.value.length); });
  const btn = el('button.btn.btn--small', { type: 'button', disabled: disabled || !url }, 'Kopieren');
  btn.addEventListener('click', () => copyText(url, toast, input));
  return el('div.rv-link', input, btn);
}

/* ------------------------------------------------------------------ Feedback-Eintrag */
export function feedbackItem(d, { onReopen, mark: num } = {}) {
  const t = d.target;
  const target = t ? (t.kind === 'document'
    ? el('span', 'Quelle: ', t.url ? el('a', { href: t.url, target: '_blank', rel: 'noopener' }, t.title || t.url) : (t.title ?? `#${t.id}`))
    : el('span', `${t.label ?? t.name ?? `${t.kind} ${t.id}`}${t.value !== undefined ? ` · ${t.value}${t.unit ? ` ${t.unit}` : ''}` : ''}`)) : null;
  const head = el('div.rv-fb__head',
    num ? el('span.rv-fb__mark', { title: 'Markierung auf der Folie' }, String(num)) : null,
    el('strong', d.author), d.weight === 'suggestion' ? badge('Anregung', 'outline') : null, el('span.small.muted', `${KIND[d.kind] ?? d.kind}${d.verdict ? ` · ${VERDICT[d.verdict] ?? d.verdict}` : ''}${d.severity ? ` · ${SEVERITY[d.severity]}` : ''} · v${d.version}`),
    el('span.fg-tools__grow'), statusBadge(d.status));
  const body = kids(
    head,
    target ? el('p.small.rv-fb__target', target) : null,
    d.text ? el('p.rv-fb__text', d.text) : null,
    d.note ? el('p.small.rv-fb__note', el('span.label', 'Notiz '), d.note) : null,
    d.votes ? el('p.small.muted', `${d.votes.length} Stimmen: ${d.votes.join(', ')}`) : null,
    d.conflict ? el('p.small.rv-fb__conflict', mark('warn'), ` ${d.conflict}`) : null,
    d.instruction ? el('p.small.rv-fb__instr', el('span.label', 'Deine Vorgabe '), d.instruction) : null,
    d.resolution ? el('p.small.rv-fb__res', el('span.label', d.status === 'decision' ? 'Frage an dich ' : 'Antwort '),
      d.resolution, d.resolved_version ? ` (v${d.resolved_version})` : '') : null,
    d.locked ? el('p.small.muted', d.locked) : null);
  const box = el('li.rv-fb', { data: { status: d.status } }, ...body);
  if (onReopen && ['decision', 'incorporated', 'answered', 'rejected'].includes(d.status)) {
    const input = el('input.input', { type: 'text', placeholder: d.status === 'decision' ? 'Deine Entscheidung für die nächste Session …' : 'Was noch fehlt …', 'aria-label': 'Vorgabe' });
    const btn = el('button.btn.btn--small', { type: 'button' }, d.status === 'decision' ? 'Entscheiden' : 'Wieder öffnen');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await onReopen(d, input.value.trim()); } finally { btn.disabled = false; }
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') btn.click(); });
    box.append(el('div.rv-fb__act', input, btn));
  }
  return box;
}

/* ------------------------------------------------------------------ Quellen, Daten, Feedback einer Folie */
export function slidePanel(exhibitId, ctx, onChange, onShapes) {
  const root = el('div.rv-panel', el('span.small.muted', 'Prüfung und Feedback werden geladen …'));
  const reopen = async (d, instruction) => {
    try { await api('/api/feedback', { id: d.id, action: 'reopen', instruction }); ctx.toast('Punkt wieder offen – die nächste Session arbeitet ihn ein.'); }
    catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); return; }
    load(); onChange?.();
  };
  function draw(d) {
    const items = arr(d.feedback).flatMap((s) => arr(s.items));
    const openAll = items.filter((x) => ['open', 'in_progress', 'decision'].includes(x.status));
    const open = openAll.filter((x) => x.weight !== 'suggestion');
    const ideas = openAll.filter((x) => x.weight === 'suggestion');
    const shapeList = openAll.filter((x) => x.shape);
    onShapes?.(shapeList);
    const markOf = (x) => { const k = shapeList.filter((y) => y.version === x.version).indexOf(x); return k >= 0 ? k + 1 : null; };
    const done = items.filter((x) => !openAll.includes(x));
    const text = el('textarea.input.rv-own__text', { rows: 2, placeholder: 'Eigenes Feedback zu dieser Folie …', 'aria-label': 'Eigenes Feedback' });
    const sev = el('select.input.rv-own__sev', { 'aria-label': 'Gewicht' }, el('option', { value: '' }, 'Gewicht'),
      ...Object.entries(SEVERITY).map(([v, l]) => el('option', { value: v }, l)));
    const add = el('button.btn.btn--small.btn--primary', { type: 'button' }, 'Hinzufügen');
    add.addEventListener('click', async () => {
      if (!text.value.trim()) { text.focus(); return; }
      add.disabled = true;
      try { await api('/api/feedback', { exhibit: exhibitId, text: text.value.trim(), severity: sev.value || null }); ctx.toast('Feedback gespeichert.'); load(); onChange?.(); }
      catch (e) { ctx.toast(`Nicht gespeichert: ${e.message}`, 5000); add.disabled = false; }
    });
    const srcs = arr(d.sources);
    const data = arr(d.data);
    root.replaceChildren(...kids(
      el('span.label', `Fehler${open.length ? ` · ${open.length} offen` : ''}`),
      open.length ? el('ul.rv-fbs', open.map((x) => feedbackItem(x, { onReopen: reopen, mark: markOf(x) })))
        : el('p.small.muted', items.length ? 'Keine offenen Fehler.' : 'Noch kein Feedback zu dieser Folie.'),
      ideas.length ? el('details.rv-done', el('summary.small', `${ideas.length} Anregungen (blockieren nicht)`),
        el('ul.rv-fbs', ideas.map((x) => feedbackItem(x, { onReopen: reopen, mark: markOf(x) })))) : null,
      done.length ? el('details.rv-done', el('summary.small', `${done.length} verarbeitet`), el('ul.rv-fbs', done.map((x) => feedbackItem(x, { onReopen: reopen })))) : null,
      el('div.rv-own', text, el('div.row.rv-own__row', sev, add)),
      el('span.label', 'Quellen'),
      srcs.length ? el('ul.rv-src', srcs.map((s) => el('li',
        el('div.rv-src__head', s.url ? el('a', { href: s.url, target: '_blank', rel: 'noopener' }, s.title || s.url) : el('span', s.title ?? `Quelle ${s.id}`), stageBadge(s.verification)),
        el('span.small.muted', [s.publisher, s.published].filter(Boolean).join(' · ') || ' '),
        s.link ? el('span.small', { class: s.link.ok ? 'small muted' : 'small rv-src__bad' }, s.link.ok ? `Link erreichbar, geprüft ${fmt.date(s.link.checked_at.replace(' ', 'T'))}` : `Link nicht erreichbar (${s.link.status ?? s.link.error ?? 'Fehler'})`) : null,
        s.verification?.source && s.verification.source.stage !== 'unchecked' ? el('span.small', { class: s.verification.source.stage === 'disputed' ? 'small rv-src__bad' : 'small muted' },
          s.verification.source.stage === 'disputed' ? `Quelle selbst als nicht echt gemeldet: ${arr(s.verification.source.wrong).map((x) => x.reviewer).join(', ')}`
            : `Quelle selbst geprüft (echt): ${arr(s.verification.source.ok).map((x) => x.reviewer).join(', ')}`) : null,
        s.evidence?.quote ? el('blockquote.small.rv-quote', `„${s.evidence.quote}“ – ${s.evidence.by}`) : null,
        s.deep_url ? el('a.small', { href: s.deep_url, target: '_blank', rel: 'noopener' }, 'Fundstelle öffnen ↗') : null))) :
        el('p.small.muted', d.sources_text ? `Quellenzeile: ${d.sources_text} – noch keinem Fundus-Dokument zugeordnet.` : 'Keine Quellen zugeordnet.'),
      arr(d.numbers).length ? el('span.label', `Zahlen der Folie · geprüft von ${d.numbers[0].by ?? 'Gemini'} (v${d.numbers[0].version})`) : null,
      arr(d.numbers).length ? el('ul.rv-src', d.numbers.map((x) => el('li',
        el('div.rv-src__head', el('span', el('strong', x.number), x.claim ? ` – ${x.claim}` : ''), badge(...(NUMBER[x.verdict] ?? [x.verdict, 'outline']))),
        x.people && x.people.stage !== 'unchecked' ? el('span.small', stageBadge(x.people), ' ',
          [...arr(x.people.ok).map((y) => `${y.reviewer} ✓`), ...arr(x.people.wrong).map((y) => `${y.reviewer} ✗${y.note ? ` (${y.note})` : ''}`)].join(', ')) : null,
        x.note && x.verdict !== 'sourced' ? el('span.small.muted', x.note) : null,
        x.evidence ? el('blockquote.small.rv-quote', `„${x.evidence}“`) : null,
        x.deep_url ? el('a.small', { href: x.deep_url, target: '_blank', rel: 'noopener' }, 'Fundstelle öffnen ↗') : null))) : null,
      el('span.label', 'Datenpunkte'),
      data.length ? el('ul.rv-src', data.map((x) => el('li',
        el('div.rv-src__head', el('span', `${x.label ?? x.name ?? `${x.kind} ${x.id}`}${x.value !== undefined ? ` · ${x.value}${x.unit ? ` ${x.unit}` : ''}` : ''}${x.period ? ` (${x.period})` : ''}`), stageBadge(x.verification)),
        x.source?.title ? el('span.small.muted', x.source.url ? el('a', { href: x.source.url, target: '_blank', rel: 'noopener' }, x.source.title) : x.source.title) : null,
        x.quote || x.locator ? el('blockquote.small.rv-quote', x.locator ? `${x.locator}: ` : '', x.quote ?? '') : null,
        x.deep_url ? el('a.small', { href: x.deep_url, target: '_blank', rel: 'noopener' }, 'Fundstelle öffnen ↗') : null))) :
        el('p.small.muted', 'Keine Datenpunkte verknüpft. Beim Rendern mit mck.page(…, fundus=[…]) oder mit fundus_exhibit_link angeben.'),
      arr(d.changelog).length ? el('span.label', 'Änderungsprotokoll') : null,
      arr(d.changelog).length ? el('ul.rv-log', arr(d.changelog).map((c) => el('li',
        el('span.small', el('strong', c.to_version ? `v${c.from_version} → v${c.to_version}` : `v${c.from_version}`), ` ${STATUS[c.outcome]?.[0] ?? c.outcome}: ${c.note}`),
        el('span.small.muted', `auf ${c.author}: „${c.feedback}“`)))) : null));
  }
  function load() { api(`/api/slide-review/${exhibitId}`).then(draw).catch((e) => root.replaceChildren(el('span.small.muted', `Prüfung nicht ladbar: ${e.message}`))); }
  load();
  return root;
}

/* ------------------------------------------------------------------ Freigabe-Dialog */
/**
 * opts: {project, projectName, set: {id, name}|null, order: {id, label}|null (geordnetes Deck), selection: [ids],
 *        mode: 'gemini' | 'people', ctx} → Promise<review|null>
 */
export function openReviewDialog(opts) {
  return new Promise((resolve) => {
    const { ctx } = opts;
    const dlg = el('dialog.dialog.rv-dialog', { 'aria-label': 'Zur Prüfung freigeben' });
    const finish = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const scopes = [
      opts.selection.length ? { v: 'selection', l: opts.selection.length === 1 ? '1 markierte Folie' : `${fmt.int(opts.selection.length)} markierte Folien` } : null,
      opts.order ? { v: 'deck', l: `Deck „${opts.order.label}“ in seiner Reihenfolge` } : null,
      opts.set ? { v: 'set', l: `ganzer Foliensatz „${opts.set.name}“` } : null,
    ].filter(Boolean);
    let scope = scopes[0]?.v;
    const scopeBox = el('div.rv-radios', { role: 'radiogroup', 'aria-label': 'Umfang' }, scopes.map((s) => {
      const id = `rv-scope-${s.v}`;
      const r = el('input', { type: 'radio', name: 'rv-scope', id, value: s.v, checked: s.v === scope });
      r.addEventListener('change', () => { scope = s.v; });
      return el('label.rv-radio', { for: id }, r, s.l);
    }));
    const title = el('input.input', { type: 'text', id: 'rv-title', placeholder: 'Titel (optional, sonst Foliensatz und Datum)', maxlength: 160 });

    // Gemini
    const gem = el('input', { type: 'checkbox', id: 'rv-gemini', checked: opts.mode === 'gemini' });
    const focus = new Set(['sources', 'numbers', 'logic']);
    const chips = el('div.rv-chips', FOCUS.map((f) => {
      const b = el('button.chip.rv-chip', { type: 'button', 'aria-pressed': String(focus.has(f.v)) }, f.l);
      b.addEventListener('click', () => { if (focus.has(f.v)) focus.delete(f.v); else focus.add(f.v); b.setAttribute('aria-pressed', String(focus.has(f.v))); });
      return b;
    }));
    const model = el('select.input.rv-model', { id: 'rv-model', 'aria-label': 'Gemini-Modell' }, el('option', { value: '' }, 'Modell wird geladen …'));
    const modelNote = el('span.small.muted');
    api('/api/reviews/models').then((d) => {
      model.replaceChildren(...arr(d.models).map((m) => el('option', { value: m.id, selected: m.id === d.default }, m.label)));
      const note = () => { modelNote.textContent = arr(d.models).find((m) => m.id === model.value)?.note ?? ''; };
      model.addEventListener('change', note); note();
    }).catch(() => model.replaceChildren(el('option', { value: '' }, 'Standardmodell')));
    const gemBox = el('fieldset.rv-sec', el('legend', el('label.rv-check', { for: 'rv-gemini' }, gem, 'Gemini auf Vertex AI')),
      el('p.small.muted', 'Ohne Link, nichts wird veröffentlicht. Gemini prüft Folien, Zahlen und Quellen gleich nach dem Start; die Spark gibt ihm die Quelltexte mit. Rechne mit 20–60 Sekunden je Folie.'),
      el('label.rv-inline', { for: 'rv-model' }, 'Modell', model), modelNote,
      el('span.label', 'Schwerpunkte'), chips);

    // Personen
    const ppl = el('input', { type: 'checkbox', id: 'rv-people', checked: opts.mode === 'people' });
    const chosen = [];
    const list = el('ul.rv-people');
    const known = el('datalist', { id: 'rv-known' });
    const who = el('input.input', { type: 'text', id: 'rv-who', list: 'rv-known', placeholder: 'Name, Vorname – oder aus der Liste wählen', autocomplete: 'off' });
    const addBtn = el('button.btn.btn--small', { type: 'button' }, 'Hinzufügen');
    let reviewers = [];
    api('/api/reviewers').then((d) => { reviewers = arr(d.reviewers); known.replaceChildren(...reviewers.map((r) => el('option', { value: r.label }, r.org ?? ''))); }).catch(() => {});
    const drawPeople = () => list.replaceChildren(...chosen.map((p, i) => {
      const rm = el('button.btn.btn--ghost.btn--small', { type: 'button', 'aria-label': `${p} entfernen` }, '✕');
      rm.addEventListener('click', () => { chosen.splice(i, 1); drawPeople(); });
      const isKnown = reviewers.some((r) => r.label.toLowerCase() === p.toLowerCase());
      return el('li', el('span', p), el('span.small.muted', isKnown ? 'aus der Liste' : 'neu'), el('span.fg-tools__grow'), rm);
    }));
    const addPerson = () => {
      const v = who.value.trim().replace(/\s+/g, ' ');
      if (!v) return;
      if (!v.includes(',') && v.split(' ').length < 2) { ctx.toast('Bitte als „Name, Vorname“ eingeben.', 4000); return; }
      if (!chosen.some((p) => p.toLowerCase() === v.toLowerCase())) chosen.push(v);
      who.value = ''; ppl.checked = true; drawPeople(); who.focus();
    };
    addBtn.addEventListener('click', addPerson);
    who.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addPerson(); } });
    const days = el('select.input.rv-days', { id: 'rv-days', 'aria-label': 'Ablauf' }, [14, 30, 60, 90].map((d) => el('option', { value: d, selected: d === 30 }, `${d} Tage`)));
    const pplBox = el('fieldset.rv-sec', el('legend', el('label.rv-check', { for: 'rv-people' }, ppl, 'Personen per Link')),
      el('p.small.muted', 'Je Person ein eigener Link. Wer ihn öffnet, gibt nichts ein; Kommentare und Prüfungen tragen den Namen, den du hier festlegst.'),
      el('div.row.rv-who', who, addBtn), known, list, el('label.rv-inline', { for: 'rv-days' }, 'Links gültig', days));

    const err = el('p.small.rv-err', { role: 'alert' });
    const cancel = el('button.btn', { type: 'button' }, 'Abbrechen');
    const go = el('button.btn.btn--primary', { type: 'button' }, 'Prüfung starten');
    cancel.addEventListener('click', () => finish(null));
    go.addEventListener('click', async () => {
      err.textContent = '';
      if (who.value.trim()) addPerson();
      const people = ppl.checked ? chosen : [];
      if (!gem.checked && !people.length) { err.textContent = 'Wähle Gemini, Personen oder beides.'; return; }
      if (ppl.checked && !people.length) { err.textContent = 'Trage mindestens eine Person ein oder nimm „Personen per Link“ heraus.'; return; }
      if (gem.checked && !focus.size) { err.textContent = 'Wähle mindestens einen Schwerpunkt für Gemini.'; return; }
      const body = { project: opts.project, title: title.value.trim() || null, people, gemini: gem.checked, focus: gem.checked ? [...focus] : null,
        model: gem.checked ? (model.value || null) : null, expires_days: Number(days.value) };
      if (scope === 'selection') body.slides = opts.selection;
      else if (scope === 'deck') body.deck = opts.order.id;
      else body.set = opts.set.id;
      go.disabled = true;
      try { const r = await api('/api/reviews', body); finish(r); }
      catch (e) { err.textContent = `Nicht angelegt: ${e.message}`; go.disabled = false; }
    });
    dlg.append(el('div.dialog__body',
      headline('h3', 'Zur Prüfung freigeben', 'Prüfung'),
      el('p.small.muted', `Projekt ${opts.projectName}. Die Folien werden in ihrer aktuellen Version festgehalten.`),
      el('span.label', 'Umfang'), scopeBox, title, gemBox, pplBox, err,
      el('div.row.dialog__actions', cancel, go)));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(null); });
    document.body.append(dlg);
    dlg.showModal();
    (opts.mode === 'people' ? who : go).focus();
  });
}

/** Ergebnis nach dem Anlegen: Links zum Kopieren. */
export function showCreated(r, ctx) {
  const dlg = el('dialog.dialog.rv-dialog', { 'aria-label': 'Prüfung angelegt' });
  const close = el('button.btn.btn--primary', { type: 'button' }, 'Fertig');
  close.addEventListener('click', () => { dlg.close(); dlg.remove(); });
  const people = arr(r.reviewers);
  dlg.append(el('div.dialog__body',
    headline('h3', 'Prüfung angelegt', 'Prüfung'),
    el('p', `${r.title} · ${fmt.plural(r.slides, 'Folie', 'Folien')}`),
    r.gemini ? el('p.small', badge(GEMINI[r.gemini] ?? r.gemini, 'accent'), ` Modell ${r.gemini_model}. Der Befund erscheint an den Folien und bei der Session; den Fortschritt siehst du unter `,
      el('a', { href: `#pruefungen?r=${r.id}` }, 'Prüfungen'), '.') : null,
    people.length ? el('span.label', 'Links') : null,
    people.length ? el('ul.rv-people.rv-people--links', people.map((p) => el('li', el('span.rv-people__name', p.name), linkField(p.link, ctx.toast)))) : null,
    people.length ? el('p.small.muted', r.gemini ? 'Die Links gehen in Betrieb, sobald Gemini fertig ist und die Prüfung veröffentlicht wurde (meist wenige Minuten). Du findest sie jederzeit unter „Prüfungen“.'
      : 'Die Prüfung wird in den nächsten Sekunden veröffentlicht; dann funktionieren die Links. Du findest sie jederzeit unter „Prüfungen“.') : null,
    el('div.row.dialog__actions', close)));
  document.body.append(dlg);
  dlg.showModal();
  close.focus();
}
