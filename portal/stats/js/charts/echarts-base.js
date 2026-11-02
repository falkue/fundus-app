/**
 * ECharts-Basis: lädt ECharts erst beim ersten Aufruf (dynamischer Import, nur in den
 * Ansichten, die es brauchen), registriert Locale DE und das Circle-Theme aus den Tokens,
 * zeichnet mit dem SVG-Renderer in echten Pixeln, hängt den gemeinsamen Tooltip an
 * (kein HTML-formatter) und setzt bei Dark-Mode-Wechsel das Theme neu.
 *
 *   const c = await makeChart(el, option, {height, tip: (params) => ({title, rows}), aria})
 *   c.chart (ECharts-Instanz) · c.update(option) · c.dispose()
 */
import { readTokens, echartsTheme, onThemeChange } from '../lib/theme.js';
import { trackCleanup } from '../lib/chart.js';
import * as tip from '../lib/tooltip.js';
import { reducedMotion } from '../lib/dom.js';

let echartsPromise = null;
async function load() {
  if (!echartsPromise) {
    echartsPromise = import('echarts').then(async (echarts) => {
      const { localeDE } = await import('./echarts-de.js');
      echarts.registerLocale('DE', localeDE);
      echarts.registerTheme('circle', echartsTheme(readTokens(true)));
      onThemeChange(() => echarts.registerTheme('circle', echartsTheme(readTokens(true))));
      return echarts;
    });
  }
  return echartsPromise;
}
export const loadEcharts = load;

export async function makeChart(container, option, { height = 300, tip: tipContent = null, aria = null, minWidth = 320 } = {}) {
  const echarts = await load();
  if (document.fonts?.ready) await document.fonts.ready;
  container.style.height = `${height}px`;
  container.style.minWidth = `${minWidth}px`;
  const chart = echarts.init(container, 'circle', { renderer: 'svg', locale: 'DE' });
  const base = {
    animation: !reducedMotion(), animationDuration: 250,
    tooltip: { show: false },
    aria: { enabled: true, label: { description: aria ?? undefined }, decal: { show: false } },
  };
  chart.setOption(deepMerge(base, option));
  if (tipContent) {
    chart.on('mouseover', (p) => { const c = tipContent(p); if (c) tip.show(p.event?.event ?? { clientX: 0, clientY: 0 }, c); });
    chart.on('mousemove', (p) => { if (p.event?.event) tip.move(p.event.event); });
    chart.on('mouseout', () => tip.hide());
    chart.on('globalout', () => tip.hide());
  }
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(container);
  const off = onThemeChange(() => { chart.setTheme?.('circle'); });
  const api = {
    chart,
    update(opt, notMerge = false) { chart.setOption(opt, { notMerge }); },
    dispose() { ro.disconnect(); off(); chart.dispose(); },
  };
  trackCleanup(api.dispose);
  return api;
}

function deepMerge(a, b) {
  if (Array.isArray(b) || typeof b !== 'object' || b === null) return b;
  const out = { ...a };
  for (const [k, v] of Object.entries(b)) out[k] = typeof v === 'object' && v !== null && !Array.isArray(v) && typeof out[k] === 'object' && out[k] !== null ? deepMerge(out[k], v) : v;
  return out;
}
