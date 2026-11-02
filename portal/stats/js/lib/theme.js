/**
 * Theme-Leser: löst die Circle-Tokens (auch color-mix()) zur Laufzeit in Pixelfarben auf
 * (Canvas-Probe), liefert stateStyle() für D3 und ECharts und löst `themechange` aus,
 * wenn Systemeinstellung oder <html data-theme> wechseln.
 */
const TOKENS = ['ink', 'ink-2', 'ink-3', 'ink-4', 'ink-meta', 'line', 'line-2', 'card', 'win', 'pane', 'sel', 'blue', 'green', 'orange', 'coral', 'yellow', 'violet', 'grey',
  'chart-series', 'chart-series-2', 'chart-track', 'chart-area', 'chart-cap', 'chart-forecast', 'chart-hatch', 'chart-hatch-bg', 'state-ok', 'state-warn', 'state-crit', 'state-off', 'state-unknown',
  'cal-0', 'cal-1', 'cal-2', 'cal-3', 'cal-4', 'reach-localhost', 'reach-tailnet', 'reach-lan', 'reach-all'];

let canvas, ctx, probe;
function ensure() {
  if (ctx) return;
  canvas = Object.assign(document.createElement('canvas'), { width: 1, height: 1 });
  ctx = canvas.getContext('2d', { willReadFrequently: true });
  probe = document.createElement('span');
  probe.style.display = 'none';
  document.body.append(probe);
}
/** CSS-Farbausdruck (z. B. `var(--blue)`, `color-mix(...)`) → `rgba(r,g,b,a)`. */
export function resolveColor(expr) {
  ensure();
  probe.style.color = '';
  probe.style.color = expr;
  const c = getComputedStyle(probe).color;
  ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgba(${r},${g},${b},${(a / 255).toFixed(3)})`;
}
let cache = null;
export function readTokens(force = false) {
  if (cache && !force) return cache;
  cache = Object.fromEntries(TOKENS.map((n) => [n, resolveColor(`var(--${n})`)]));
  cache.sans = getComputedStyle(document.documentElement).getPropertyValue('--sans').trim() || 'Inter, sans-serif';
  return cache;
}
export function isDark() {
  const t = document.documentElement.dataset.theme;
  if (t === 'dark') return true;
  if (t === 'light') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/** Zustandsstil für D3 und ECharts: Farbe, Wort, Form (SVG-Pfad 24×24) und Decal. */
const SHAPES = {
  ok: 'path://M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20z',
  warn: 'path://M12 3l10 17H2z',
  crit: 'path://M12 2l10 10-10 10L2 12z',
  off: 'path://M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16z M12 7a5 5 0 1 1 0 10a5 5 0 1 1 0-10z',
  unknown: 'path://M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20z M12 6a4 4 0 0 1 4 4c0 2-2 2.5-3 4h-2c0-2.5 3-2.5 3-4a2 2 0 0 0-4 0H8a4 4 0 0 1 4-4z',
};
const WORDS = { ok: 'läuft', warn: 'Achtung', crit: 'Kritisch', off: 'Absichtlich aus', unknown: 'Keine Daten' };
export function stateStyle(state) {
  const s = WORDS[state] ? state : 'unknown';
  const t = readTokens();
  return {
    state: s, word: WORDS[s], color: t[`state-${s}`], symbol: SHAPES[s],
    decal: s === 'unknown' ? { symbol: 'rect', dashArrayX: [1, 0], dashArrayY: [2, 4], rotation: Math.PI / 4, color: t['chart-hatch'], symbolSize: 1 } : s === 'off' ? { symbol: 'rect', dashArrayX: [2, 2], dashArrayY: [2, 2], color: t['ink-4'] } : null,
  };
}

/** ECharts-Theme aus den Tokens (nur Flächen/Text/Achsen – Serienfarben setzen die Diagramme selbst). */
export function echartsTheme(t = readTokens()) {
  const axisLabel = { color: t['ink-meta'], fontSize: 11, fontFamily: t.sans };
  return {
    color: [t.blue, t['chart-series-2'], t['ink-4']],
    backgroundColor: 'transparent',
    textStyle: { fontFamily: t.sans, fontSize: 12, color: t['ink-2'] },
    title: { textStyle: { color: t.ink, fontSize: 13, fontWeight: 500 } },
    legend: { textStyle: { color: t['ink-2'], fontSize: 12 } },
    categoryAxis: { axisLine: { lineStyle: { color: t['line-2'] } }, axisTick: { show: false }, axisLabel, splitLine: { show: false } },
    valueAxis: { axisLine: { show: false }, axisTick: { show: false }, axisLabel, splitLine: { lineStyle: { color: t.line } }, nameTextStyle: { color: t['ink-meta'], fontSize: 11 } },
    timeAxis: { axisLine: { lineStyle: { color: t['line-2'] } }, axisTick: { show: false }, axisLabel, splitLine: { lineStyle: { color: t.line } } },
    logAxis: { axisLabel, splitLine: { lineStyle: { color: t.line } } },
    line: { smooth: false, symbolSize: 6, lineStyle: { width: 2 } },
    tooltip: { show: false },
    dataZoom: { textStyle: { color: t['ink-meta'], fontSize: 11 }, borderColor: t['line-2'], fillerColor: t.sel, handleStyle: { color: t.card, borderColor: t['line-2'] }, dataBackground: { lineStyle: { color: t['ink-4'] }, areaStyle: { color: t['chart-track'] } }, selectedDataBackground: { lineStyle: { color: t.blue }, areaStyle: { color: t['chart-area'] } }, moveHandleStyle: { color: t['ink-4'] }, emphasis: { handleStyle: { borderColor: t.blue } } },
    visualMap: { textStyle: { color: t['ink-meta'], fontSize: 11 } },
    calendar: { itemStyle: { color: t.card, borderColor: t.card, borderWidth: 2 }, splitLine: { show: false }, dayLabel: { color: t['ink-meta'], fontSize: 11 }, monthLabel: { color: t['ink-meta'], fontSize: 11 }, yearLabel: { show: false } },
    treemap: { breadcrumb: { show: false } },
  };
}

// themechange-Ereignis: Systemeinstellung + data-theme
const mq = window.matchMedia('(prefers-color-scheme: dark)');
function fire() { cache = null; requestAnimationFrame(() => { readTokens(true); window.dispatchEvent(new CustomEvent('themechange', { detail: { dark: isDark() } })); }); }
mq.addEventListener?.('change', fire);
new MutationObserver(fire).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
export const onThemeChange = (fn) => { window.addEventListener('themechange', fn); return () => window.removeEventListener('themechange', fn); };
