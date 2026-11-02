/**
 * Formatierer (de-CH): 1’234, 2.05 TB, „vor 3 h“, Datum.
 * Alle Funktionen vertragen null/undefined und liefern dann „–“.
 */
const LOCALE = 'de-CH';
const nf0 = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dfDate = new Intl.DateTimeFormat(LOCALE, { day: '2-digit', month: '2-digit', year: 'numeric' });
const dfShort = new Intl.DateTimeFormat(LOCALE, { day: '2-digit', month: '2-digit' });
const dfTime = new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit' });
const dfMonth = new Intl.DateTimeFormat(LOCALE, { month: 'short' });

export const DASH = '–';
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

export function int(v) { return isNum(v) ? nf0.format(v) : DASH; }
export function num1(v) { return isNum(v) ? nf1.format(v) : DASH; }
export function num2(v) { return isNum(v) ? nf2.format(v) : DASH; }
export function pct(v, digits = 0) { return isNum(v) ? `${(digits ? nf1 : nf0).format(v)} %` : DASH; }

/** Bytes → "2.05 TB" / "420 GB" / "77.6 MB" (dezimal, wie QNAP/df anzeigen). */
export function bytes(b, digits) {
  if (!isNum(b)) return DASH;
  const abs = Math.abs(b);
  const units = [[1e12, 'TB'], [1e9, 'GB'], [1e6, 'MB'], [1e3, 'kB']];
  for (const [f, u] of units) {
    if (abs >= f) {
      const v = b / f;
      const d = typeof digits === 'number' ? digits : (v >= 100 ? 0 : v >= 10 ? 1 : 2);
      return `${new Intl.NumberFormat(LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d }).format(v)} ${u}`;
    }
  }
  return `${nf0.format(b)} B`;
}
/** Bytes in einer festen Einheit ohne Suffix (für Achsen). */
export function bytesIn(b, unit) {
  const f = { TB: 1e12, GB: 1e9, MB: 1e6 }[unit] ?? 1;
  return isNum(b) ? (b / f) : null;
}
export function pickByteUnit(maxBytes) {
  if (!isNum(maxBytes)) return 'GB';
  return maxBytes >= 2e12 ? 'TB' : maxBytes >= 2e9 ? 'GB' : 'MB';
}

export function parseDate(v) {
  if (!v) return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}
export function date(v) { const d = parseDate(v); return d ? dfDate.format(d) : DASH; }
export function dateShort(v) { const d = parseDate(v); return d ? dfShort.format(d) : DASH; }
export function dateTime(v) { const d = parseDate(v); return d ? `${dfDate.format(d)}, ${dfTime.format(d)}` : DASH; }
export function time(v) { const d = parseDate(v); return d ? dfTime.format(d) : DASH; }
export function month(v) { const d = parseDate(v); return d ? dfMonth.format(d) : DASH; }

/** Relative Zeit: "vor 50 min", "vor 3 h", "vor 2 Tagen", "in 25 min". */
export function ago(v, now = Date.now()) {
  const d = parseDate(v);
  if (!d) return DASH;
  const diffS = (now - d.getTime()) / 1000;
  const past = diffS >= 0;
  const s = Math.abs(diffS);
  let txt;
  if (s < 60) txt = 'weniger als 1 min';
  else if (s < 3600) txt = `${Math.round(s / 60)} min`;
  else if (s < 48 * 3600) txt = `${Math.round(s / 3600)} h`;
  else if (s < 60 * 86400) { const dd = Math.round(s / 86400); txt = `${dd} ${dd === 1 ? 'Tag' : 'Tagen'}`; }
  else if (s < 2 * 365 * 86400) { const m = Math.round(s / (30.44 * 86400)); txt = `${m} ${m === 1 ? 'Monat' : 'Monaten'}`; }
  else { const y = Math.round(s / (365.25 * 86400)); txt = `${y} ${y === 1 ? 'Jahr' : 'Jahren'}`; }
  return past ? `vor ${txt}` : `in ${txt}`;
}
/** Alter in Stunden (null, wenn unbekannt). */
export function hoursSince(v, now = Date.now()) {
  const d = parseDate(v);
  return d ? (now - d.getTime()) / 3.6e6 : null;
}

/** Dauer aus Sekunden: "82 Tage", "3 h 12 min". */
export function duration(sec) {
  if (!isNum(sec)) return DASH;
  if (sec >= 2 * 86400) return `${nf0.format(Math.floor(sec / 86400))} Tage`;
  if (sec >= 86400) return `1 Tag ${Math.floor((sec % 86400) / 3600)} h`;
  if (sec >= 3600) return `${Math.floor(sec / 3600)} h ${Math.floor((sec % 3600) / 60)} min`;
  return `${Math.floor(sec / 60)} min`;
}
/** Stunden als lesbare Dauer: 18.3 → "18 h", 150 → "6.3 Tage", 0.6 → "36 min". */
export function hours(h) {
  if (!isNum(h)) return DASH;
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${nf1.format(h).replace(/\.0$/, '')} h`;
  return `${nf1.format(h / 24).replace(/\.0$/, '')} Tage`;
}
/** Tage: 955 → "955 Tage", -1237 → "seit 1’237 Tagen". */
export function days(d) {
  if (!isNum(d)) return DASH;
  if (d < 0) return `seit ${nf0.format(-d)} Tagen`;
  if (d === 0) return 'heute';
  return `in ${nf0.format(d)} Tagen`;
}
export function plural(n, one, many) { return `${int(n)} ${n === 1 ? one : many}`; }
export function temp(c) { return isNum(c) ? `${nf0.format(c)} °C` : DASH; }
export function load(v) { return isNum(v) ? nf2.format(v) : DASH; }
export function or(v, fallback = DASH) { return v === null || v === undefined || v === '' ? fallback : String(v); }
