/**
 * Shared building blocks of the "Forschung" view (forschung*.js): API calls, German labels, verdict marks and bars
 * (four verdicts with equal visual weight: own hue AND own shape, no good/bad colouring), status chips, a small
 * markdown renderer (DOM only, no innerHTML), links for Fundus references (dataset:N, photo:N, hypothesis:N …) and a
 * responsive SVG helper that redraws on width changes.
 *
 * REST (fundus/research.py via mcp_server): GET /api/research (board, worklist, evidence map),
 * GET /api/research/hypotheses?…, /api/research/hypotheses/<id>, GET /api/research/findings?…, /api/research/findings/<id>,
 * POST /api/research/hypotheses/<id> | /api/research/findings/<id> {action, …}.
 */
import { el, svgEl, badge } from '../lib/dom.js';
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

// ---------------------------------------------------------------- labels
export const VERDICTS = ['supported', 'partly_supported', 'not_supported', 'inconclusive'];
export const VERDICT = { supported: 'gestützt', partly_supported: 'teilweise gestützt', not_supported: 'nicht gestützt', inconclusive: 'unentschieden' };
export const VERDICT_SHORT = { supported: 'gestützt', partly_supported: 'teilweise', not_supported: 'nicht gestützt', inconclusive: 'unentschieden' };
export const STATUS = { idea: 'Idee', preregistered: 'präregistriert', running: 'läuft', result: 'Ergebnis', revised: 'überarbeitet', withdrawn: 'zurückgezogen' };
export const BOARD = ['idea', 'preregistered', 'running', 'result'];
export const KIND = { quantitative: 'quantitativ', qualitative: 'qualitativ', mixed: 'gemischt' };
export const DIRECTION = { '+': 'positiv (+)', '-': 'negativ (−)', '−': 'negativ (−)', '≠': 'ungleich null (≠)', none: 'ohne Richtung', qualitative: 'qualitativ, ohne Richtung' };
export const STRENGTH = ['single_observation', 'recurring_pattern', 'saturated', 'quantified'];
export const STRENGTH_LABEL = { single_observation: 'Einzelbeobachtung', recurring_pattern: 'wiederkehrendes Muster', saturated: 'gesättigt', quantified: 'quantifiziert' };
export const FSTATUS = { draft: 'Entwurf', recorded: 'erfasst', revised: 'überarbeitet', withdrawn: 'zurückgezogen' };
export const TIMING = { before_analysis: 'vor der Analyse', after_data_seen: 'nach Sicht auf die Daten' };
export const EVIDENCE = { dataset: 'Datensatz', observation: 'Beobachtung', document: 'Dokument', exhibit: 'Folie', photo: 'Foto', video: 'Video',
  quote: 'Zitat', note: 'Notiz', file: 'Datei' };
/** Relation labels from the point of view of the page that shows the link (out = this → other, in = other → this). */
export const RELATION = {
  sharpens: { out: 'schärft', in: 'wird geschärft von' }, replicates: { out: 'repliziert', in: 'wird repliziert von' },
  contradicts: { out: 'widerspricht', in: 'widerspricht' }, extends: { out: 'erweitert', in: 'wird erweitert von' },
  derived_from: { out: 'entstanden aus', in: 'Grundlage für' }, competes_with: { out: 'konkurriert mit', in: 'konkurriert mit' },
  revises: { out: 'überarbeitet', in: 'überarbeitet durch' },
};
export const PREREG_FIELDS = [
  ['statement', 'Aussage'], ['direction', 'Richtung'], ['unit', 'Untersuchungseinheit'], ['sample', 'Stichprobe'],
  ['measures', 'Operationalisierung und Messung'], ['data', 'Daten und Material'], ['method', 'Methode und Modell'], ['controls', 'Kontrollen'],
  ['decision_rule', 'Entscheidungsregel'], ['robustness_plan', 'Robustheitsplan']];
export const FLAG = {
  interpretation_missing: 'Interpretation fehlt', data_newer_than_result: 'Daten neuer als Ergebnis',
  prereg_after_result: 'Präregistrierung nach dem Ergebnis datiert', idea_without_prereg: 'Idee ohne Präregistrierung', imported_partial: 'nur teilweise importiert',
};

/** The seven interpretation sections; `req` = needed for "complete" (spec: summary, explanation, limits, theory). */
export const SECTIONS = [
  { key: 'summary', label: 'Was das Ergebnis sagt', req: true,
    hint: 'In zwei, drei Sätzen: Richtung, Grösse und Sicherheit des Ergebnisses – auch wenn es nicht gestützt oder unentschieden ist.' },
  { key: 'explanation', label: 'Warum es so ausfiel', req: true,
    hint: 'Mechanismus oder Gründe. Bei nicht gestützten oder unentschiedenen Ergebnissen: Was haben wir gelernt? Reichte die Präzision (Stichprobe, Breite des Intervalls)?' },
  { key: 'alternatives', label: 'Andere Erklärungen', req: false,
    hint: 'Welche anderen Mechanismen, Verzerrungen oder Zufälle erklären dasselbe Muster?' },
  { key: 'limits', label: 'Grenzen: Identifikation, Daten, Messung', req: true,
    hint: 'Was die Aussage einschränkt: kausale Identifikation, Datenqualität, Gültigkeit der Messung, Übertragbarkeit.' },
  { key: 'theory', label: 'Bedeutung für die Theorie', req: true,
    hint: 'Stützt, begrenzt oder widerlegt das Ergebnis die Theorie? Unter welchen Bedingungen gilt sie?' },
  { key: 'relations', label: 'Bezug zu anderen Hypothesen und Findings', req: false,
    hint: 'Bestätigt, widerspricht oder schärft es andere? Codes nennen (z. B. H8.2, F-1) – Verbindungen zusätzlich unter „Verbindungen“.' },
  { key: 'next', label: 'Nächste Schritte', req: false,
    hint: 'Anschlusshypothesen, bessere Daten, Replikation.' },
];
export function interpState(interp) {
  const s = interp?.sections ?? {};
  const done = SECTIONS.filter((x) => x.req && String(s[x.key] ?? '').trim()).length;
  const filled = SECTIONS.filter((x) => String(s[x.key] ?? '').trim()).length;
  const need = SECTIONS.filter((x) => x.req).length;
  return { done, need, filled, complete: done === need, state: done === need ? 'complete' : filled ? 'partial' : 'missing' };
}

// ---------------------------------------------------------------- verdict marks (shape + hue + word)
const SHAPES = {
  supported: () => svgEl('circle', { cx: 6, cy: 6, r: 4.9 }),
  partly_supported: () => svgEl('rect', { x: 1.6, y: 1.6, width: 8.8, height: 8.8, rx: 1.2 }),
  not_supported: () => svgEl('path', { d: 'M6 0.6 L11.4 6 L6 11.4 L0.6 6 Z' }),
  inconclusive: () => svgEl('path', { d: 'M6 0.9 L10.6 3.45 V8.55 L6 11.1 L1.4 8.55 V3.45 Z' }),
  finding: () => svgEl('circle', { cx: 6, cy: 6, r: 4.2, class: 'fo-ring' }),
  open: () => svgEl('circle', { cx: 6, cy: 6, r: 4.2, class: 'fo-ring fo-ring--solid' }),
};
export function vmark(v, cls = '') {
  const k = SHAPES[v] ? v : 'open';
  const s = svgEl('svg', { class: `fo-vm fo-v--${k} ${cls}`.trim(), viewBox: '0 0 12 12', 'aria-hidden': 'true', focusable: 'false' });
  s.append(SHAPES[k]());
  return s;
}
/** Verdict pill: shape + word, tinted with the verdict hue (all four equally strong). */
export function vpill(v, opts = {}) {
  if (!v) return el('span.fo-vpill.fo-vpill--none', opts.none ?? 'noch kein Ergebnis');
  return el('span.fo-vpill', { class: `fo-vpill fo-v--${v}${opts.big ? ' fo-vpill--big' : ''}` }, vmark(v), VERDICT[v] ?? v);
}
export function statusChip(s) { return el('span.fo-status', { class: `fo-status fo-status--${s}` }, STATUS[s] ?? s); }
export function kindChip(k) { return el('span.fo-chip', KIND[k] ?? k); }
export function visChip(v) { return v === 'internal' ? badge('intern', 'outline') : v === 'public' ? badge('öffentlich', 'accent') : null; }
export function exploratoryChip() { return el('span.fo-explo', { title: 'Findings sind Ergebnisse ohne vorherige Hypothese. Sie bleiben explorativ und werden nie zu bestätigten Hypothesen.' }, 'explorativ'); }
/** short: only the state word (for a table column headed „Interpretation“). */
export function interpChip(state, short = false) {
  const p = short ? '' : 'Interpretation ';
  if (state === 'complete') return el('span.fo-ip.fo-ip--complete', { title: 'Alle Pflichtteile der Interpretation sind ausgefüllt' }, `${p}vollständig`);
  if (state === 'partial' || state === 'incomplete') return el('span.fo-ip.fo-ip--partial', { title: 'Interpretation unvollständig: Pflichtteile fehlen noch' }, `${p}unvollständig`);
  return el('span.fo-ip.fo-ip--missing', { title: 'Noch keine Interpretation – steht auf der Arbeitsliste' }, `${p}fehlt`);
}

/** Stacked verdict bar with counts: equal hues, in fixed order; zero segments left out. */
export function verdictBar(counts, opts = {}) {
  const total = VERDICTS.reduce((a, v) => a + (counts?.[v] ?? 0), 0);
  const bar = el('div.fo-vbar', { role: 'img', 'aria-label': total ? VERDICTS.map((v) => `${VERDICT[v]} ${counts?.[v] ?? 0}`).join(', ') : 'noch keine Ergebnisse' });
  if (!total) { bar.classList.add('is-empty'); bar.append(el('span.fo-vbar__empty', 'noch keine Ergebnisse')); return bar; }
  for (const v of VERDICTS) {
    const n = counts?.[v] ?? 0;
    if (!n) continue;
    const seg = el(opts.href ? 'a.fo-vbar__seg' : 'span.fo-vbar__seg', { class: `fo-vbar__seg fo-v--${v}`, style: { flexGrow: String(n) },
      title: `${VERDICT[v]}: ${n} von ${total}`, href: opts.href ? opts.href(v) : null }, el('span.fo-vbar__n', String(n)));
    bar.append(seg);
  }
  return bar;
}
export function verdictLegend(counts) {
  return el('div.fo-vlegend', VERDICTS.map((v) => el('span.fo-vlegend__item', vmark(v), VERDICT[v], counts ? el('span.fo-vlegend__n', fmt.int(counts[v] ?? 0)) : null)));
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
export const hypHref = (h, tab) => `#forschung?h=${h.id ?? h}${tab ? `&t=${tab}` : ''}`;
export const findHref = (f, tab) => `#forschung?f=${f.id ?? f}${tab ? `&t=${tab}` : ''}`;
export const itemHref = (x, tab) => ((x.type ?? (String(x.ref ?? '').startsWith('finding:') ? 'finding' : 'hypothesis')) === 'finding' ? findHref(x, tab) : hypHref(x, tab));
export const seriesLabel = (s) => (!s ? 'ohne Serie' : typeof s === 'string' ? s.replace(/^serie-/, 'Serie ') : (s.slug ? s.slug.replace(/^serie-/, 'Serie ') : s.title));

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
export const shortHash = (h) => (h ? `${String(h).slice(0, 8)}…${String(h).slice(-4)}` : '–');
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
/** A flag detail (string, list of fields or list of stale inputs) as one line of text. */
export function flagText(f) {
  const d = f?.detail;
  if (d === null || d === undefined || d === '') return '';
  if (typeof d === 'string') return d === f.label ? '' : d;
  if (Array.isArray(d)) {
    return d.map((x) => (typeof x === 'string' ? (PREREG_FIELDS.find(([k]) => k === x)?.[1] ?? x)
      : x.ref ? `${x.ref} Version ${x.used ?? '?'} → ${x.current ?? '?'}` : JSON.stringify(x))).join(', ');
  }
  return typeof d === 'object' ? Object.entries(d).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ') : String(d);
}
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

/** GET /api/research → {stats, series, worklist, evidence_map: {rows}} (also accepts the earlier fixture shape). */
export function normBoard(d) {
  if (!d || d._norm) return d;
  d._norm = true;
  const series = Array.isArray(d.board) ? d.board.map((s) => ({ ...s, status: s.status ?? s.counts })) : arr(d.series);
  d.series = series;
  const verdicts = Object.fromEntries(VERDICTS.map((v) => [v, series.reduce((a, s) => a + (s.verdicts?.[v] ?? 0), 0)]));
  const wl = d.worklist ?? {};
  const wlN = wl.counts ? Object.values(wl.counts).reduce((a, n) => a + (n ?? 0), 0) : ['interpretation_missing', 'data_newer_than_result', 'idea_without_prereg'].reduce((a, k) => a + arr(wl[k]).length, 0);
  d.stats = { ...(d.stats ?? {}), verdicts: d.stats?.verdicts ?? verdicts, worklist: d.stats?.worklist ?? wlN };
  for (const x of arr(wl.data_newer_than_result)) if (Array.isArray(x.detail)) x.detail = flagText({ detail: x.detail });
  for (const x of arr(wl.prereg_after_result)) if (x.detail && typeof x.detail !== 'string') x.detail = flagText({ detail: x.detail });
  if (Array.isArray(d.evidence_map)) {
    d.evidence_map = { rows: d.evidence_map.map((r) => ({ ...r, ...(r.verdicts ?? {}), key: `${r.series ?? ''}|${r.theory}` })) };
  }
  return d;
}
