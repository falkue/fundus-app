/**
 * Shared building blocks of the "Forschung" view (forschung*.js): API calls, German labels in plain words, the neutral
 * result pill (every verdict looks the same – no colours, no shapes), a small markdown renderer (DOM only, no
 * innerHTML), links for Fundus references (dataset:N, hypothesis:N …), normalizers from the server shapes and a
 * responsive SVG helper for the small interval plot.
 *
 * REST (fundus/research.py via mcp_server, see RESEARCH-API-NOTES.md): GET /api/research, /api/research/series,
 * /api/research/hypotheses?…, /api/research/hypotheses/<id>, /api/research/findings?…, /api/research/findings/<id>;
 * POST /api/research/hypotheses/<id> | /api/research/findings/<id> {action, …}.
 */
import { el } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';

export const arr = (v) => (Array.isArray(v) ? v : []);
export const kids = (...a) => a.filter((x) => x !== null && x !== undefined && x !== false && x !== '');
export const hp = () => new URLSearchParams(location.hash.split('?')[1] ?? '');
export const sqlDate = (v) => (v ? String(v).replace(' ', 'T') : null);
export function put(node, ...c) { node.replaceChildren(...c.flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false)); return node; }

export async function api(path, body) {
  const r = await fetch(`/api/research${path}`, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) { const e = new Error(d.error ?? `HTTP ${r.status}`); e.status = r.status; throw e; }
  return d;
}

// ---------------------------------------------------------------- labels (plain German)
export const VERDICTS = ['supported', 'partly_supported', 'not_supported', 'inconclusive'];
export const VERDICT = { supported: 'gestützt', partly_supported: 'teilweise gestützt', not_supported: 'nicht gestützt', inconclusive: 'unentschieden' };
export const STATUS = { idea: 'Idee', preregistered: 'Plan festgelegt', running: 'in Arbeit', result: 'Ergebnis', revised: 'überarbeitet', withdrawn: 'zurückgezogen' };
export const KIND = { quantitative: 'quantitativ', qualitative: 'qualitativ', mixed: 'gemischt' };
export const DIRECTION = { '+': 'positiver Zusammenhang erwartet', '-': 'negativer Zusammenhang erwartet', '−': 'negativer Zusammenhang erwartet',
  '≠': 'ein Unterschied erwartet (Richtung offen)', none: 'ohne Richtung', qualitative: 'qualitativ, ohne Richtung' };
export const STRENGTH = ['single_observation', 'recurring_pattern', 'saturated', 'quantified'];
export const STRENGTH_LABEL = { single_observation: 'Einzelbeobachtung', recurring_pattern: 'wiederkehrendes Muster', saturated: 'gesättigt', quantified: 'mit Zahlen belegt' };
export const FSTATUS = { draft: 'Entwurf', recorded: 'erfasst', revised: 'überarbeitet', withdrawn: 'zurückgezogen' };
export const TIMING = { before_analysis: 'vor der Auswertung', after_data_seen: 'nach Blick in die Daten' };
export const EVIDENCE = { dataset: 'Datensatz', observation: 'Beobachtung', document: 'Dokument', exhibit: 'Folie', photo: 'Foto', video: 'Video',
  quote: 'Zitat', note: 'Notiz', file: 'Datei' };
/** Relation labels from the point of view of the page that shows the link (out = this → other, in = other → this). */
export const RELATION = {
  sharpens: { out: 'schärft', in: 'wird geschärft von' }, replicates: { out: 'wiederholt', in: 'wird wiederholt von' },
  contradicts: { out: 'widerspricht', in: 'widerspricht' }, extends: { out: 'erweitert', in: 'wird erweitert von' },
  derived_from: { out: 'entstanden aus', in: 'Grundlage für' }, competes_with: { out: 'konkurriert mit', in: 'konkurriert mit' },
  revises: { out: 'überarbeitet', in: 'überarbeitet durch' },
};
export const PREREG_FIELDS = [
  ['statement', 'Aussage'], ['direction', 'Erwartung'], ['unit', 'Untersucht werden'], ['sample', 'Stichprobe'],
  ['measures', 'Messung'], ['data', 'Daten'], ['method', 'Methode'], ['controls', 'Kontrollen'],
  ['decision_rule', 'Wann gilt sie als gestützt?'], ['robustness_plan', 'Gegenproben']];

/** The seven interpretation sections; `req` = needed for "vorhanden" (summary, explanation, limits, theory). */
export const SECTIONS = [
  { key: 'summary', label: 'Was das Ergebnis sagt', req: true, hint: 'Zwei, drei Sätze: Richtung, Grösse und Sicherheit – auch wenn es nicht gestützt oder unentschieden ist.' },
  { key: 'explanation', label: 'Warum es so ausfiel', req: true, hint: 'Gründe. Bei nicht gestützten oder unentschiedenen Ergebnissen: Was haben wir gelernt? Reichten die Daten?' },
  { key: 'alternatives', label: 'Andere Erklärungen', req: false, hint: 'Was könnte dasselbe Muster sonst erklären?' },
  { key: 'limits', label: 'Grenzen', req: true, hint: 'Was die Aussage einschränkt: Ursache und Wirkung, Datenqualität, Messung, Übertragbarkeit.' },
  { key: 'theory', label: 'Bedeutung für die Theorie', req: true, hint: 'Stützt, begrenzt oder widerlegt das Ergebnis die Theorie?' },
  { key: 'relations', label: 'Bezug zu anderen Hypothesen und Findings', req: false, hint: 'Bestätigt, widerspricht oder schärft es andere? Codes nennen, z. B. H8.2.' },
  { key: 'next', label: 'Nächste Schritte', req: false, hint: 'Anschlussfragen, bessere Daten, Wiederholung.' },
];
export function interpState(interp) {
  const s = interp?.sections ?? {};
  const done = SECTIONS.filter((x) => x.req && String(s[x.key] ?? '').trim()).length;
  const filled = SECTIONS.filter((x) => String(s[x.key] ?? '').trim()).length;
  const need = SECTIONS.filter((x) => x.req).length;
  return { done, need, filled, complete: done === need, state: done === need ? 'complete' : filled ? 'partial' : 'missing' };
}
/** "vorhanden" only when the four required parts are filled; a started one says "unvollständig". */
export const interpWord = (state) => (state === 'complete' ? 'vorhanden' : state === 'partial' || state === 'incomplete' ? 'unvollständig' : 'fehlt');

/** The result as plain text in a small neutral pill – all verdicts look the same. */
export function resultPill(verdict, none = 'noch kein Ergebnis') {
  return el('span.fo-pill', { class: `fo-pill${verdict ? '' : ' fo-pill--none'}` }, verdict ? (VERDICT[verdict] ?? verdict) : none);
}

// ---------------------------------------------------------------- references → links
const REF = /\b(dataset|observation|document|exhibit|photo|video|hypothesis|finding):(\d+)(?:\s?v(\d+))?\b/g;
export function refHref(ref) {
  // photos and videos have no deep link in the Mediathek yet – the evidence list links their picture instead
  const m = String(ref ?? '').match(/^(dataset|hypothesis|finding):(\d+)/);
  if (!m) return null;
  const [, kind, id] = m;
  return { dataset: `#daten?d=${id}`, hypothesis: `#forschung?h=${id}`, finding: `#forschung?f=${id}` }[kind];
}
export function refLink(ref, label) {
  const h = refHref(ref);
  return h ? el('a.fo-ref', { href: h }, label ?? ref) : el('span.fo-ref', label ?? ref);
}
/** Text with Fundus references turned into links. */
export function linkify(text) {
  const out = [];
  let last = 0;
  const s = String(text ?? '');
  for (const m of s.matchAll(REF)) {
    if (m.index > last) out.push(s.slice(last, m.index));
    out.push(refLink(`${m[1]}:${m[2]}`, m[0]));
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
export const hypHref = (h) => `#forschung?h=${h.id ?? h}`;
export const findHref = (f) => `#forschung?f=${f.id ?? f}`;
export const isFinding = (x) => (x.type ?? (String(x.ref ?? '').startsWith('finding:') ? 'finding' : 'hypothesis')) === 'finding';
export const itemHref = (x) => (isFinding(x) ? findHref(x) : hypHref(x));
/** "Serie 8" for numbered series; a series object without a number shows its title. */
export const seriesLabel = (s) => (!s ? 'ohne Serie' : typeof s === 'string' ? s.replace(/^serie-/, 'Serie ')
  : /^serie-\d/.test(s.slug ?? '') ? s.slug.replace(/^serie-/, 'Serie ') : (s.title ?? s.slug));

// ---------------------------------------------------------------- minimal markdown (paragraphs, lists, headings, code, **bold**, `code`, links)
function inline(text) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((https?:[^)\s]+)\))/g;
  let last = 0;
  const s = String(text);
  for (const m of s.matchAll(re)) {
    if (m.index > last) out.push(...linkify(s.slice(last, m.index)));
    const t = m[0];
    if (t.startsWith('**')) out.push(el('strong', ...linkify(t.slice(2, -2))));
    else if (t.startsWith('`')) out.push(el('code', t.slice(1, -1)));
    else { const lm = t.match(/^\[([^\]]+)\]\(([^)]+)\)$/); out.push(el('a', { href: lm[2], target: '_blank', rel: 'noopener' }, lm[1])); }
    last = m.index + t.length;
  }
  if (last < s.length) out.push(...linkify(s.slice(last)));
  return out;
}
export function md(text, cls = 'fo-md') {
  const root = el(`div.${cls}`);
  const lines = String(text ?? '').replace(/\r/g, '').split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith('```')) {
      const buf = []; i++;
      while (i < lines.length && !lines[i].startsWith('```')) buf.push(lines[i++]);
      i++;
      root.append(el('pre.fo-md__code', el('code', buf.join('\n'))));
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { root.append(el(h[1].length <= 2 ? 'h4' : 'h5', inline(h[2]))); i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = el(ordered ? 'ol' : 'ul');
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ''); i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) item += ` ${lines[i++].trim()}`;
        list.append(el('li', inline(item)));
      }
      root.append(list);
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s+)/.test(lines[i])) buf.push(lines[i++].trim());
    root.append(el('p', inline(buf.join(' '))));
  }
  return root;
}

// ---------------------------------------------------------------- numbers
const nfs = (d) => new Intl.NumberFormat('de-CH', { maximumFractionDigits: d, minimumFractionDigits: 0 });
export function num(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  const s = nfs(a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3).format(v);
  return v < 0 ? s.replace('-', '−') : s;
}
export const when = (v) => fmt.dateTime(sqlDate(v));
export const day = (v) => fmt.date(sqlDate(v));

/**
 * Redraw an SVG whenever the container width changes (tabs that become visible, window resize).
 * draw(width) returns an <svg>; returns the wrapper.
 */
export function responsive(cls, draw, fallback = 640) {
  const wrap = el(`div.${cls}`);
  let w = 0;
  const paint = (width) => {
    const nw = Math.round(width || fallback);
    if (Math.abs(nw - w) < 4) return;
    w = nw;
    put(wrap, draw(w));
  };
  paint(fallback);
  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver((es) => { for (const e of es) if (e.contentRect.width > 0) paint(e.contentRect.width); });
    ro.observe(wrap);
  }
  return wrap;
}

/** Strings for search (lower case). */
export function hay(...parts) { return parts.flat(Infinity).filter(Boolean).join(' \n ').toLowerCase(); }

// ---------------------------------------------------------------- normalizers: server shapes (RESEARCH-API-NOTES.md) → view model
const SEC_KEYS = SECTIONS.map((s) => s.key);
/** Items of a list/search response ({items} or {hypotheses:[…]}). */
export const listItems = (d, kind) => (Array.isArray(d?.[kind]) ? d[kind] : arr(d?.items));
function interpOf(block) {
  // result.interpretation / finding.interpretation: {state, current {sections…, author, at}, versions [{id, author, at, state}]}
  if (!block) return { interp: null, versions: [], state: 'missing' };
  if (block.sections) return { interp: block, versions: [block], state: interpState(block).state };
  const cur = block.current;
  const versions = arr(block.versions);
  const interp = cur ? { sections: Object.fromEntries(SEC_KEYS.map((k) => [k, cur[k] ?? ''])), author: cur.author, at: cur.at, version: versions.length || 1 } : null;
  return { interp, versions: versions.map((v, i) => ({ ...v, version: versions.length - i })), state: block.state ?? (interp ? interpState(interp).state : 'missing') };
}
function flatLinks(ls) {
  return arr(ls).map((l) => (l.other ? { ...l.other, relation: l.relation, direction: l.direction, note: l.note, relation_label: l.relation_label,
    type: l.other.type ?? (String(l.other.ref ?? '').startsWith('finding') ? 'finding' : 'hypothesis') } : l));
}
function flatHistory(hs) {
  return arr(hs).map((e) => ({ at: e.ts ?? e.at, action: e.action, by: e.actor ?? e.by,
    detail: typeof e.detail === 'string' ? e.detail : e.detail ? Object.entries(e.detail).filter(([k]) => k !== 'source').map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ') : '' }));
}
const idOf = (ref) => Number(String(ref ?? '').split(':')[1]) || ref;

/** Hypothesis detail → view model (current result, its interpretation, flat links, provenance columns). */
export function normHyp(h) {
  if (!h || h._norm) return h;
  h._norm = true;
  h.results = arr(h.results);
  const byAt = [...h.results].sort((a, b) => String(a.at ?? '').localeCompare(String(b.at ?? '')));
  byAt.forEach((r, i) => { r.version ??= i + 1; r.sample ??= r.sample_run; });
  h.result = h.current_result ? (h.results.find((r) => r.id === h.current_result.id) ?? h.current_result) : (h.result ?? h.results[0] ?? null);
  if (h.result && !h.results.length) h.results = [h.result];
  const io = interpOf(h.result?.interpretation ?? h.interpretation);
  if (!h.interpretation?.sections) h.interpretation = io.interp;
  h.interpretations ??= io.versions;
  h.interpretation_state ??= io.state;
  h.theories ??= h.theory_cards;
  h.links = flatLinks(h.links);
  h.log ??= flatHistory(h.history);
  h.flags = arr(h.flags).map((f) => (typeof f === 'string' ? { flag: f } : f));
  h.deviations = arr(h.deviations);
  if (h.derived_from_finding?.ref) h.derived_from_finding = { ...h.derived_from_finding, id: idOf(h.derived_from_finding.ref) };
  if (h.prereg) {
    h.prereg.theory ??= h.prereg.extra?.theory;
    if (h.prereg.frozen === false) h.prereg.frozen_at = null;
  }
  // outputs: 'exhibit:N' strings → objects with label/project/slug from the lineage
  const nodes = new Map(arr(h.lineage?.nodes).map((n) => [n.id, n]));
  for (const r of h.results) {
    r.outputs = arr(r.outputs).map((o) => {
      if (typeof o !== 'string') return o;
      const n = nodes.get(o) ?? {};
      const kind = o.split(':')[0];
      return { kind, ref: o, title: n.label, project: n.project, slug: n.slug };
    });
    r.inputs = arr(r.inputs).map((x) => {
      const n = nodes.get(x.ref) ?? {};
      return { ...x, title: x.title ?? n.label, current_version: x.current_version ?? n.current_version, stale: x.stale ?? n.stale };
    });
  }
  return h;
}

/** Finding detail → view model. */
export function normFinding(f) {
  if (!f || f._norm) return f;
  f._norm = true;
  const io = interpOf(f.interpretation);
  f.interpretation = io.interp;
  f.interpretations = io.versions;
  f.interpretation_state ??= io.state;
  f.evidence = arr(f.evidence).map((e) => ({ ...e, speaker: e.speaker ?? e.speaker_role }));
  f.links = flatLinks(f.links);
  for (const d of arr(f.derived_hypotheses)) {
    const id = idOf(d.ref);
    if (!f.links.some((l) => l.relation === 'derived_from' && (l.id === id || l.ref === d.ref))) f.links.push({ ...d, id, type: 'hypothesis', relation: 'derived_from', direction: 'in' });
  }
  f.theories ??= f.theory_cards;
  f.log ??= flatHistory(f.history);
  f.flags = arr(f.flags).map((x) => (typeof x === 'string' ? { flag: x } : x));
  return f;
}

/** GET /api/research → {series (from board), evidence_map: {rows}} for the overview page. */
export function normBoard(d) {
  if (!d || d._norm) return d;
  d._norm = true;
  d.series = Array.isArray(d.board) ? d.board : arr(d.series);
  if (Array.isArray(d.evidence_map)) d.evidence_map = { rows: d.evidence_map.map((r) => ({ ...r, ...(r.verdicts ?? {}) })) };
  return d;
}
