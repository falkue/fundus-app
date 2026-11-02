/**
 * Ansicht „Kapazität & Verlauf“: Belegung je Volumen über Zeit mit Kapazitätslinie und
 * Prognose (≥ 14 Tage Daten), Kleinvielfache für Last, RAM, Temperatur je Knoten.
 * Zeitraum-Segmente 1 W / 1 M / 3 M / Alles.
 */
import { el, pageHead, chartFrame, card, segmented, clear, emptyNote, headline, legend } from '../lib/dom.js';
import { runCleanups } from '../lib/chart.js';
import * as fmt from '../lib/fmt.js';
import { series } from '../lib/derive.js';
import { forecast } from '../charts/timeseries.js';
import { capacityChartE, smallMultiplesE } from '../charts/echarts-charts.js';
import { daysToFull } from '../charts/history.js';

const RANGES = [{ value: 7, label: '1 W' }, { value: 30, label: '1 M' }, { value: 90, label: '3 M' }, { value: null, label: 'Alles' }];

export function render(root, data, ctx) {
  const vols = data.history.series.filter((s) => s.metric === 'volUsed').map((s) => ({ ...series(data, s.id), capSeries: s.capId ? series(data, s.capId) : null }));
  const fc = vols.map((v) => ({ v, f: forecast({ points: v.points, cap: v.capSeries?.points ?? null }) })).filter((x) => x.f && x.f.t80 && x.f.t80 > new Date());
  const soonest = fc.sort((a, b) => a.f.t80 - b.f.t80)[0];
  const total = data.history.series.reduce((a, s) => a + (s.points?.length ?? 0), 0);
  const lede = total ? `${fmt.int(total)} Messpunkte in ${data.history.series.length} Reihen. ${soonest ? `${soonest.v.label} erreicht bei gleichem Wachstum am ${fmt.date(soonest.f.t80)} die 80-Prozent-Marke.` : 'Keine Reihe steuert in absehbarer Zeit auf 80 % zu.'}` : 'Keine Zeitreihen geliefert.';
  root.append(pageHead({ title: soonest ? `${soonest.v.label.split(' · ')[0]} wächst auf die Grenze zu` : 'Belegung und Verlauf', accent: soonest ? 'Grenze' : 'Verlauf', lede }));
  const ins = ctx.insight('verlauf'); if (ins) root.append(ins);

  let range = 90;
  const seg = segmented(RANGES, range, (v) => { range = v; drawAll(); }, 'Zeitraum');
  root.append(el('div.row', seg.el, el('span.small.muted', 'Zeitraum gilt für alle Diagramme dieser Ansicht.')));

  // N3 Tage bis voll
  const full = card({ title: 'Tage bis voll', accent: 'voll', sub: 'Bar-Gauge je Volumen: Füllstand mit Marken bei 80 % (Strich) und 100 % (koralle). Prognose aus dem täglichen Dauerwachstum der letzten 60 Tage; einmalige Sprünge (z. B. Erstsicherungen, Löschungen) sind herausgerechnet, frühestens nach 14 Tagen.' });
  const fullWrap = el('div');
  daysToFull(fullWrap, data.nodes.flatMap((n) => (n.volumes ?? []).map((vol) => ({ ...vol, nodeLabel: n.label ?? n.id }))));
  full.append(fullWrap);
  root.append(full);
  const volWrap = el('div.stack');
  root.append(volWrap);
  root.append(headline('h2', 'Last, RAM und Temperatur je Knoten', 'Knoten'));
  const multWrap = el('div.stack');
  root.append(multWrap);

  function drawAll() {
    clear(volWrap);
    if (!vols.length) volWrap.append(card({ title: 'Belegung', accent: 'Belegung' }, emptyNote('Keine Volumen-Zeitreihen.')));
    for (const v of vols) {
      const f = chartFrame({ title: v.label ?? v.id, accent: null, sub: 'Blaue Fläche: belegter Speicher über die Zeit. Graue Linie: Kapazität, gepunktet: 80-Prozent-Marke. Gestrichelt: Prognose des Kollektors aus dem Dauerwachstum (ohne einmalige Sprünge, frühestens nach 14 Tagen). Unten der Zoom-Regler.',
        legend: legend([{ swatch: 'var(--chart-series)', label: 'belegt' }, { line: true, swatch: 'var(--chart-cap)', label: 'Kapazität' }, { line: true, dashed: true, swatch: 'var(--chart-forecast)', label: 'Prognose' }]) });
      const node = data.nodes.find((n) => n.id === v.node);
      const volMeta = node?.volumes?.find((x) => v.id.includes(`.${x.id}.`)) ?? null;
      const cap = v.capSeries?.points?.length ? v.capSeries.points[v.capSeries.points.length - 1].v : (volMeta?.capBytes ?? null);
      const pts = range ? v.points.filter((p) => p.t >= new Date(Date.now() - range * 864e5)) : v.points;
      const plot = el('div.echart');
      f.plot.append(plot);
      if (!pts.length) plot.replaceChildren(emptyNote('Keine Punkte in diesem Zeitraum.'));
      else capacityChartE(plot, { label: 'belegt', points: v.points, cap, range, forecast: volMeta?.forecast ?? null }).catch((e) => plot.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${e.message}`)));
      const fo = volMeta?.forecast ?? null;
      f.setHint(pts.length < 2 ? `Erst ${pts.length} Messpunkt – noch kein Verlauf.` : fo && typeof fo.daysTo80 === 'number' ? `Wachstum ${fmt.bytes(fo.slopeBytesPerDay * 30)} pro Monat · 80 % in ~${fmt.int(fo.daysTo80)} Tagen · voll in ~${fmt.int(fo.daysTo100)} Tagen (Basis ${fmt.num1(fo.basisDays)} Tage)` : fo && typeof fo.slopeBytesPerDay === 'number' && fo.slopeBytesPerDay <= 0 ? 'Belegung sinkt oder stagniert – keine Prognose.' : `${pts.length} Punkte – Prognose erst ab 14 Tagen Verlauf.`);
      f.table([{ key: 't', label: 'Zeit', fmt: fmt.dateTime }, { key: 'v', label: 'belegt', num: true, fmt: fmt.bytes }], [...pts].reverse().slice(0, 40));
      volWrap.append(card({}, f.el));
    }
    clear(multWrap);
    const metrics = [{ metric: 'load1', label: 'Last (1 min)', format: fmt.load }, { metric: 'memPct', label: 'RAM', format: (v) => fmt.pct(v), yDomain: [0, 100] }, { metric: 'tempC', label: 'Temperatur', format: fmt.temp }];
    const nodes = data.nodes.length ? data.nodes : [...new Set(data.history.series.map((s) => s.node))].map((id) => ({ id, label: id }));
    for (const m of metrics) {
      const c = card({ title: m.label, accent: null, sub: 'Eine Kachel je Knoten, gleiche Höhe, eigene Skala; der Zeiger ist über alle Kacheln gekoppelt. Knoten ohne Reihe erscheinen als „noch kein Verlauf“.' });
      const rows = nodes.map((n) => {
        const s = data.history.series.find((x) => x.node === n.id && x.metric === m.metric);
        const ser = s ? series(data, s.id) : null;
        return { id: n.id, label: n.label ?? n.id, points: ser ? ser.points : [], format: m.format, yDomain: m.yDomain };
      });
      const plot = el('div.echart');
      c.append(plot);
      smallMultiplesE(plot, rows, { cols: window.matchMedia('(max-width: 760px)').matches ? 1 : 3, range }).catch((e) => plot.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${e.message}`)));
      multWrap.append(c);
    }
  }
  drawAll();
}
