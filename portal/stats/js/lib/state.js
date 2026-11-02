/**
 * Zustände: überall gleich (Farbe + Form + Wort). Blau ist nie Zustand.
 */
export const STATES = {
  ok:      { word: 'läuft',           alt: 'aktuell',  rank: 0, symbol: 'sym-ok' },
  warn:    { word: 'Achtung',         alt: 'Achtung',  rank: 2, symbol: 'sym-warn' },
  crit:    { word: 'Kritisch',        alt: 'Kritisch', rank: 3, symbol: 'sym-crit' },
  off:     { word: 'Absichtlich aus', alt: 'aus',      rank: 0, symbol: 'sym-off' },
  unknown: { word: 'Keine Daten',     alt: 'unbekannt',rank: 1, symbol: 'sym-unknown' },
};
export const STATE_ORDER = ['crit', 'warn', 'unknown', 'ok', 'off'];
export const SEVERITY = { crit: 3, high: 2, med: 1, low: 0 };
export const SEVERITY_WORD = { crit: 'Kritisch', high: 'Hoch', med: 'Mittel', low: 'Niedrig' };
export const SEVERITY_STATE = { crit: 'crit', high: 'crit', med: 'warn', low: 'warn' };

/** Normalisiert unbekannte oder fehlende Zustände auf "unknown". */
export function norm(s) { return s && STATES[s] ? s : 'unknown'; }
export function word(s, variant = 'word') { return STATES[norm(s)][variant] ?? STATES[norm(s)].word; }

/** Schlechtester Zustand einer Liste (crit > warn > unknown > ok/off). Leere Liste → unknown. */
export function worst(states) {
  let w = null;
  for (const raw of states) {
    const s = norm(raw);
    if (w === null || STATES[s].rank > STATES[w].rank) w = s;
  }
  return w ?? 'unknown';
}

/** Zustand aus einem Verhältnis Ist/Soll (RPO): ≤1 ok, ≤2 warn, sonst crit; null → unknown. */
export function ratioState(ratio) {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return 'unknown';
  if (ratio <= 1) return 'ok';
  if (ratio <= 2) return 'warn';
  return 'crit';
}
/** Zustand aus Belegung in Prozent: ≥90 crit, ≥80 warn. */
export function fillState(pct) {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return 'unknown';
  if (pct >= 90) return 'crit';
  if (pct >= 80) return 'warn';
  return 'ok';
}
/** Zustand aus Tagen bis Supportende: <0 crit, <180 warn; null unknown. */
export function eolState(days) {
  if (days === null || days === undefined || !Number.isFinite(days)) return 'unknown';
  if (days < 0) return 'crit';
  if (days < 180) return 'warn';
  return 'ok';
}
/** Zustand aus Versionsabstand. */
export function behindState(behind, security) {
  if (!behind || behind === 'unknown') return 'unknown';
  if (behind === 'none') return 'ok';
  if (behind === 'major' || security) return 'crit';
  if (behind === 'minor') return 'warn';
  return 'warn'; // patch
}
export const BEHIND_WORD = { none: 'aktuell', patch: 'Patch', minor: 'Minor', major: 'Major', unknown: 'unbekannt' };
