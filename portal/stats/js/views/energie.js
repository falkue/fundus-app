/**
 * Ansicht „Nachhaltigkeit“ (#energie): Energie, Kosten und CO₂ der Heimnetz-Geräte.
 * Aufbau nach report-energy.md: Klartext-Kopfsatz · V1 Kennzahlen (Jahresprognose, CHF/Monat, CO₂/Jahr,
 * Grundlast) · V2 Tagessäulen je Gerät mit Grundlast-Linie (kWh/CHF/kg) · V3 Durchschnittstag / 48 h /
 * je Gerät · Anteil je Gerät · V4 Geräte-Bullets + Hero-Karte · V5 Wofür (Sankey) · V6 Sparen (Wasserfall) ·
 * V7 Jahresverlauf · V8 Kalender · Umrechnung & Einordnung · „So rechnen wir“ · Tarif & Faktoren (Formular).
 * Schätzungen sind immer als Wort + Muster gekennzeichnet; fehlende Werte erscheinen als „nicht erhoben“.
 * Droplet (energy.external) zählt nie zur Heimnetz-Summe.
 */
import { el, pageHead, chartFrame, card, legend, mark, badge, emptyNote, dataTable, dl, confirmDialog, clear, lu, segmented } from '../lib/dom.js';
import * as fmt from '../lib/fmt.js';
import { sparkline } from '../charts/sparkline.js';
import { bulletRow } from '../charts/bullet.js';
import { dailyStackedE, avgDayE, pacingE, calendarHeatE, waterfallE, purposeSankey, costSeriesE, deviceColors, sortDevices, normIso, METHOD_WORD } from '../charts/energy-charts.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const METHOD = { measured: { word: 'gemessen', tone: 'ok' }, mixed: { word: 'teilweise gemessen', tone: 'warning' }, estimated: { word: 'geschätzt', tone: 'outline' } };
const HOUSEHOLD = { kwh: 2190, source: 'BFE/EnergieSchweiz: 2-Personen-Haushalt in einer Mehrfamilienhaus-Wohnung, 2’190 kWh pro Jahr' };
const PURPOSE = { hero: 'KI-Inferenz', xerox: 'Speicher & Sicherung', carbon: 'Sicherung & Musik (Roon)', mac: 'Arbeitsplatz', network: 'Netzwerk' };
const rp = (chf) => (isNum(chf) ? `${fmt.num1(chf * 100)} Rp./kWh` : '–');
const fail = (box) => (err) => box.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${err?.message ?? err}`));

export function render(root, data, ctx) {
  const e = data.energy;
  if (!e) {
    root.append(pageHead({ title: 'Noch keine Energiedaten', accent: 'Energiedaten', lede: 'Der Kollektor liefert noch kein Feld energy. Sobald Leistungswerte erhoben werden, erscheinen hier Verbrauch, Kosten und CO₂.' }));
    root.append(card({}, emptyNote('Keine Energiedaten geliefert.')));
    return;
  }
  const devices = sortDevices((e.devices ?? []).filter((d) => d.included !== false && d.scope !== 'external'));
  const nodes = new Set(devices.map((d) => d.node));
  const daily = (e.daily ?? []).filter((x) => nodes.has(x.node));
  const hourlyRaw = (e.hourly ?? []).filter((x) => nodes.has(x.node));
  const cfg = e.config ?? {};
  const byFrom = (a, b) => String(a.validFrom).localeCompare(String(b.validFrom));
  const prices = [...(cfg.prices ?? [])].sort(byFrom);
  const co2Factors = [...(cfg.co2Factors ?? [])].sort(byFrom);
  const t = e.totals ?? {};
  const price = isNum(t.priceChfPerKwh) ? t.priceChfPerKwh : prices.at(-1)?.chfPerKwh ?? null;
  const co2g = isNum(t.co2GPerKwh) ? t.co2GPerKwh : co2Factors.at(-1)?.gPerKwh ?? null;
  const rows = dayRows(daily);                       // [{date, per:{node:{…}}, total:{kwh,costChf,co2Kg}, complete}]
  const days = rows.map((r) => r.date);
  const since = days[0] ?? null;
  const fullDays = rows.filter((r) => r.complete).length;
  const baseW = devices.reduce((s, d) => s + (isNum(d.baseload?.w) ? d.baseload.w : isNum(d.baseW) ? d.baseW : 0), 0);
  const estimated = devices.filter((d) => d.method === 'estimated');
  const avgW = isNum(t.avgPowerW24h) ? t.avgPowerW24h : t.powerW;
  const share = isNum(t.kwhYear) ? t.kwhYear / HOUSEHOLD.kwh : null;

  // ---------- Kopfsatz
  root.append(pageHead({
    title: isNum(avgW) ? `Das Heimnetz braucht rund ${fmt.int(avgW)} Watt – rund um die Uhr` : 'Energie des Heimnetzes',
    accent: 'rund um die Uhr',
    lede: `≈ ${fmt.int(t.kwhYear)} kWh pro Jahr · ≈ CHF ${fmt.num2(t.costChfMonth)} pro Monat · ≈ ${fmt.int(t.co2KgYear)} kg CO₂e pro Jahr`
      + (isNum(share) ? ` · etwa ${fmt.pct(share * 100)} des Stroms eines 2-Personen-Haushalts` : '')
      + `. ${estimated.length} von ${devices.length} Geräten sind geschätzt, gemessen ist ${fmt.pct((t.measuredShare ?? 0) * 100)} der Leistung (Hero-GPU).`
      + (since ? ` Verlauf seit ${fmt.date(since)} (${fullDays} vollständige Tage) – Hochrechnungen werden mit jedem Tag belastbarer.` : ''),
  }));
  const ins = ctx.insight('energie', { quiet: true }); if (ins) root.append(ins);
  if (!devices.length) { root.append(card({}, emptyNote('Keine Geräte in energy.devices.'))); return; }

  // ---------- V1 Kennzahlen
  const totals = rows.map((r) => r.total.kwh);
  const grid = el('div.grid.grid--kpi');
  grid.append(kpi({ label: 'Jahresprognose', value: fmt.int(t.kwhYear), unit: 'kWh', spark: totals.slice(-30), foot: `≈ ${fmt.num1(t.kwhDay)} kWh pro Tag · Hochrechnung`, state: 'unknown' }));
  grid.append(kpi({ label: 'Kosten pro Monat', value: fmt.num2(t.costChfMonth), unit: 'CHF', spark: rows.slice(-30).map((r) => r.total.costChf), foot: isNum(price) ? `bei ${rp(price)} · ≈ CHF ${fmt.int(t.costChfYear)} pro Jahr` : 'kein Tarif hinterlegt', state: isNum(price) ? 'ok' : 'unknown' }));
  grid.append(kpi({ label: 'CO₂e pro Jahr', value: fmt.int(t.co2KgYear), unit: 'kg', spark: rows.slice(-30).map((r) => r.total.co2Kg), foot: isNum(co2g) ? `bei ${fmt.int(co2g)} g/kWh (${co2Factors.at(-1)?.method ?? 'Verbrauchermix'}${co2Factors.at(-1)?.provisional ? ', vorläufig' : ''})` : 'kein Faktor hinterlegt', state: isNum(co2g) ? 'ok' : 'unknown' }));
  grid.append(kpi({ label: 'Grundlast', value: fmt.int(baseW), unit: 'W', spark: hourlyTotals(hourlyRaw).map((h) => h.total), foot: isNum(e.byState?.idleShare) ? `${fmt.pct(e.byState.idleShare * 100)} der Energie fliesst ohne Nutzarbeit` : 'nachts 01–05 Uhr gemessen', state: 'unknown' }));
  root.append(grid);

  // ---------- V2 Tagessäulen
  let unit = 'kwh', stacked = null;
  const unitSeg = segmented([{ value: 'kwh', label: 'kWh' }, { value: 'costChf', label: 'CHF' }, { value: 'co2Kg', label: 'kg CO₂e' }], unit, (u) => { unit = u; stacked?.setUnit(u); }, 'Einheit');
  const f2 = chartFrame({ title: 'Verbrauch je Gerät und Tag', accent: 'Tag', tools: unitSeg.el,
    sub: 'Gestapelte Säulen je Gerät; gestrichelte Linie = Grundlast (läuft auch, wenn niemand etwas tut). Gepunktete Flächen sind geschätzt, schraffierte teilweise gemessen; blasse, gestrichelt umrandete Säulen sind unvollständige Tage. Bei CHF markieren senkrechte Linien Preiswechsel.',
    legend: legend([{ swatch: 'var(--color-accent)', label: 'gemessen' }, { hatch: true, label: 'geschätzt / teilweise gemessen' }]) });
  const p2 = el('div.echart'); f2.plot.append(p2);
  const baseKwh = devices.reduce((s, d) => s + (isNum(d.baseload?.kwhDay) ? d.baseload.kwhDay : 0), 0);
  const baseline = baseKwh ? { kwh: baseKwh, costChf: isNum(price) ? baseKwh * price : null, co2Kg: isNum(co2g) ? (baseKwh * co2g) / 1000 : null } : null;
  if (rows.length) dailyStackedE(p2, rows.slice(-90), devices, { unit, baseline, prices }).then((r) => { stacked = r; }).catch(fail(p2));
  else p2.replaceChildren(emptyNote('Noch keine Tageswerte.'));
  f2.table([{ key: 'date', label: 'Tag', render: (r) => fmt.date(r.date) }, ...devices.map((d) => ({ key: d.node, label: d.label ?? d.node, num: true, render: (r) => fmt.num2(r.per[d.node]?.kwh) })), { key: 'sum', label: 'kWh', num: true, render: (r) => fmt.num2(r.total.kwh) }, { key: 'chf', label: 'CHF', num: true, render: (r) => fmt.num2(r.total.costChf) }, { key: 'cov', label: 'vollständig', render: (r) => (r.complete ? 'ja' : 'nein') }], rows.slice(-21).reverse());
  root.append(card({}, f2.el));

  // ---------- V3 Durchschnittstag / 48 h / je Gerät + Anteil
  const two = el('div.grid.grid--2');
  const hours = hourlyTotals(hourlyRaw);
  let prof = null;
  const modeSeg = segmented([{ value: 'day24', label: 'Durchschnittstag' }, { value: 'h48', label: '48 h' }, { value: 'per', label: 'je Gerät' }], 'day24',
    (m) => prof?.set(m === 'per' ? { mode: 'day24', perDevice: true } : { mode: m, perDevice: false }), 'Ansicht');
  const f3 = chartFrame({ title: 'Lastprofil über den Tag', accent: 'Tag', tools: modeSeg.el,
    sub: 'Leistung in Watt je Stunde. Linie = Median aller Tage, Band = typische Spanne (10.–90. Perzentil, ab 3 Tagen), graue Fläche = Grundlast. „48 h“ zeigt die letzten zwei Tage gestapelt je Gerät mit den Zeiten, in denen Hero ein Modell geladen hatte.' });
  const p3 = el('div.echart'); f3.plot.append(p3);
  if (hours.length) avgDayE(p3, hours, devices, { baseW, heroRuns: devices.find((d) => d.node === 'hero')?.modelRuns48h ?? [] }).then((r) => { prof = r; }).catch(fail(p3));
  else p3.replaceChildren(emptyNote('Noch keine Stundenwerte.'));
  f3.table([{ key: 'ts', label: 'Stunde', render: (r) => fmt.dateTime(r.ts) }, { key: 'total', label: 'Watt gesamt', num: true, render: (r) => fmt.int(r.total) }], hours.slice(-12).reverse());
  two.append(card({}, f3.el));
  const sc = card({ title: 'Anteil je Gerät', accent: 'Anteil', sub: 'Anteil am Verbrauch der erfassten Tage, nach Grösse sortiert. Gepunktet = geschätzt.' });
  const sh = shareByDevice(daily, devices);
  if (sh.total > 0) sc.append(shareBar(sh, devices)); else sc.append(emptyNote('Noch keine Tageswerte für den Anteil.'));
  two.append(sc);
  root.append(two);

  // ---------- V4 Geräte + Hero
  const four = el('div.grid.grid--2');
  const pc = card({ title: 'Wo jedes Gerät läuft', accent: 'Gerät', sub: 'Balken = aktuelle Leistung, Strich = Grundlast (Median der Nachtstunden 01–05 Uhr), Skala bis zum Maximum laut Datenblatt bzw. Netzteil. Die Erhebungsart steht als Wort daneben.' });
  const b = el('div.bullets.bullets--energy');
  for (const d of devices) {
    const m = METHOD[d.method] ?? { word: 'unbekannt', tone: 'outline' };
    const g = isNum(d.baseload?.w) ? d.baseload.w : d.idleW;
    const row = bulletRow(b, { label: d.label ?? d.node, sub: (d.components ?? []).map((c) => c.label ?? c.name).filter(Boolean).slice(0, 3).join(' · '), value: isNum(d.powerW) ? d.powerW : null, target: isNum(g) ? g : undefined, max: isNum(d.maxW) ? d.maxW : undefined, format: (v) => `${fmt.int(v)} W`, state: isNum(d.powerW) ? (d.method === 'measured' ? 'ok' : 'unknown') : 'unknown', valueText: isNum(d.powerW) ? `${fmt.int(d.powerW)} W · Grundlast ${fmt.int(g)} W · max. ${fmt.int(d.maxW)} W` : 'keine Daten', targetLabel: `Grundlast ${fmt.int(g)} W` });
    if (row) { row.classList.add(`bullet-row--${d.method ?? 'unknown'}`); row.querySelector('.bullet-row__label > span')?.append(badge(m.word, m.tone)); }
  }
  pc.append(b);
  const ext = e.external ?? [];
  if (ext.length) pc.append(el('p.small.muted', `Nicht im Heimnetz und nicht in der Summe: ${ext.map((x) => x.label ?? x.node).join(', ')} – ${ext[0].notes ?? 'Leistung nicht erhebbar'}`));
  four.append(pc);
  four.append(heroCard(devices.find((d) => d.node === 'hero'), daily));
  root.append(four);

  // ---------- V5 Wofür + V6 Sparen
  const five = el('div.grid.grid--2');
  const sk = card({ title: 'Wofür der Strom draufgeht', accent: 'Wofür', sub: 'Fluss Stromnetz → Gerät → Zweck, hochgerechnet auf 30 Tage. Die Grundlast jedes Geräts zählt als „Leerlauf/Bereitschaft“, der Rest zum Zweck des Geräts. Die Zuordnung ist eine Schätzung (gestrichelt).' });
  const skBox = el('div'); sk.append(skBox);
  const sg = sankeyData(devices, daily, price, co2g);
  if (!purposeSankey(skBox, sg)) skBox.append(emptyNote('Zu wenig Daten für den Fluss.'));
  five.append(sk);
  const savings = (e.savings ?? []).filter((x) => isNum(x.kwhYear));
  const pending = (e.savings ?? []).filter((x) => !isNum(x.kwhYear));
  let wf = null;
  const wfSeg = segmented([{ value: 'kwh', label: 'kWh' }, { value: 'costChf', label: 'CHF' }, { value: 'co2Kg', label: 'kg CO₂e' }], 'kwh', (u) => wf?.setUnit(u), 'Einheit');
  const f6 = chartFrame({ title: 'Was sich sparen liesse', accent: 'sparen', tools: wfSeg.el, sub: 'Von der Jahresprognose bis zum „möglich“: jede Massnahme als fallender Balken. Alles Schätzungen mit Nebenwirkungen – Details in der Tabelle.' });
  const p6 = el('div.echart'); f6.plot.append(p6);
  if (savings.length && isNum(t.kwhYear)) waterfallE(p6, { start: { kwh: t.kwhYear, costChf: t.costChfYear, co2Kg: t.co2KgYear }, items: savings.map((x) => ({ measure: x.measure, kwh: x.kwhYear, costChf: x.costChfYear, co2Kg: x.co2KgYear, method: x.method })) }).then((r) => { wf = r; }).catch(fail(p6));
  else p6.replaceChildren(emptyNote('Keine bezifferten Einsparpotenziale.'));
  f6.table([{ key: 'measure', label: 'Massnahme' }, { key: 'kwhYear', label: 'kWh/Jahr', num: true, render: (r) => (isNum(r.kwhYear) ? fmt.num1(r.kwhYear) : 'wird gemessen') }, { key: 'costChfYear', label: 'CHF/Jahr', num: true, render: (r) => fmt.num2(r.costChfYear) }, { key: 'sideEffects', label: 'Nebenwirkung' }], e.savings ?? []);
  if (pending.length) f6.setHint(`Noch ohne Zahl (wird erst gemessen): ${pending.map((x) => x.measure).join(', ')}.`);
  five.append(card({}, f6.el));
  root.append(five);

  // ---------- V7 Jahresverlauf + V8 Kalender
  const six = el('div.grid.grid--2');
  const f7 = chartFrame({ title: 'Jahresverlauf kumuliert', accent: 'Jahresverlauf', sub: `Ist-Verbrauch seit ${since ? fmt.date(since) : '–'} aufsummiert, gestrichelt die Hochrechnung bis Jahresende mit Band; Referenz: 2-Personen-Haushalt (${fmt.int(HOUSEHOLD.kwh)} kWh, BFE).` });
  const p7 = el('div.echart'); f7.plot.append(p7);
  const dayKwh = rows.map((r) => r.total.kwh).filter(isNum).sort((a, c) => a - c);
  const q = (p) => (dayKwh.length >= 5 ? dayKwh[Math.floor(p * (dayKwh.length - 1))] : null);
  if (rows.length) pacingE(p7, { year: new Date().getFullYear(), actual: rows.map((r) => ({ date: r.date, kwh: r.total.kwh })), avgDay: t.kwhDay, p10Day: q(0.1), p90Day: q(0.9), household: HOUSEHOLD.kwh, observedSince: since }).catch(fail(p7));
  else p7.replaceChildren(emptyNote('Noch kein Verlauf.'));
  six.append(card({}, f7.el));
  const f8 = chartFrame({ title: 'Kalender: kWh pro Tag', accent: 'Kalender', sub: 'Ein Kästchen je Tag, dunkler = mehr Verbrauch. Gepunktet = unvollständig erfasst, leer = vor Beginn der Aufzeichnung.' });
  const p8 = el('div.echart'); f8.plot.append(p8);
  if (rows.length) calendarHeatE(p8, new Map(rows.map((r) => [r.date, { kwh: r.total.kwh, complete: r.complete }])), { weeks: 26, since }).catch(fail(p8));
  else p8.replaceChildren(emptyNote('Noch kein Verlauf.'));
  six.append(card({}, f8.el));
  root.append(six);

  // ---------- Umrechnung & Einordnung
  const seven = el('div.grid.grid--2');
  const uc = card({ title: 'Umrechnung', accent: 'Umrechnung', sub: 'Hochrechnung aus dem Mittel der letzten 24 Stunden mit dem heute gültigen Tarif und CO₂-Faktor. Darunter, was bisher tatsächlich angefallen ist.' });
  uc.append(dataTable([{ key: 'span', label: 'Zeitraum' }, { key: 'kwh', label: 'kWh', num: true, render: (r) => fmt.num1(r.kwh) }, { key: 'chf', label: 'CHF', num: true, render: (r) => fmt.num2(r.chf) }, { key: 'co2', label: 'kg CO₂e', num: true, render: (r) => fmt.num1(r.co2) }],
    [{ span: 'Tag (Prognose)', kwh: t.kwhDay, chf: t.costChfDay, co2: t.co2KgDay }, { span: 'Monat (Prognose)', kwh: t.kwhMonth, chf: t.costChfMonth, co2: t.co2KgMonth }, { span: 'Jahr (Prognose)', kwh: t.kwhYear, chf: t.costChfYear, co2: t.co2KgYear },
      ...(e.actual?.yearToDate ? [{ span: `Bisher erfasst (${fmt.int(e.actual.yearToDate.days)} Tage)`, kwh: e.actual.yearToDate.kwh, chf: e.actual.yearToDate.costChf, co2: e.actual.yearToDate.co2Kg }] : [])], { compact: true }));
  uc.append(dl([['Strompreis', isNum(price) ? `${rp(price)} (ab ${fmt.date(prices.at(-1)?.validFrom)})` : 'nicht hinterlegt'], ['CO₂-Faktor', isNum(co2g) ? `${fmt.int(co2g)} g CO₂e/kWh · ${co2Factors.at(-1)?.method ?? ''}${co2Factors.at(-1)?.provisional ? ' · vorläufig' : ''}` : 'nicht hinterlegt'], ['Grundlast-Anteil', isNum(e.byState?.idleShare) ? `${fmt.pct(e.byState.idleShare * 100)} der Energie` : undefined]]));
  seven.append(uc);
  const vc = card({ title: 'Einordnung', accent: 'Einordnung', sub: 'Eine Vergleichsgrösse mit Quelle.' });
  if (isNum(share)) {
    vc.append(el('div.compare', el('div.compare__num', fmt.pct(share * 100)), el('div', el('div', `des Jahresverbrauchs eines 2-Personen-Haushalts in einer Wohnung (≈ ${fmt.int(HOUSEHOLD.kwh)} kWh)`), el('div.small.muted', HOUSEHOLD.source))));
    const bar = el('div.sharebar', { role: 'img', 'aria-label': `Heimnetz ${fmt.pct(share * 100)} eines Haushalts` });
    bar.append(el('div.sharebar__seg', { style: { width: `${Math.min(100, share * 100)}%`, background: 'var(--color-accent)' } }));
    vc.append(bar);
    vc.append(el('p.small.muted', 'Der Schweizer Durchschnitt von rund 5’000 kWh ist durch Elektroheizungen verzerrt und hier bewusst nicht verwendet.'));
  } else vc.append(emptyNote('Noch keine Jahresprognose.'));
  seven.append(vc);
  root.append(seven);

  // ---------- So rechnen wir
  root.append(methodSection(devices, cfg, e));

  // ---------- Tarif & Faktoren
  root.append(tariffSection({ prices, co2Factors, daily, ctx }));
}

/* ---------- Daten aufbereiten ---------- */
function dayRows(daily) {
  const by = new Map();
  for (const x of daily) {
    const k = String(x.date).slice(0, 10);
    if (!by.has(k)) by.set(k, { date: k, per: {}, total: { kwh: 0, costChf: 0, co2Kg: 0 }, complete: true });
    const r = by.get(k);
    r.per[x.node] = { kwh: x.kwh, costChf: x.costChf, co2Kg: x.co2Kg, complete: x.complete, partial: x.partial, hours: x.hours, method: x.method };
    for (const f of ['kwh', 'costChf', 'co2Kg']) if (isNum(x[f])) r.total[f] += x[f];
    if (x.complete === false) r.complete = false;
  }
  return [...by.values()].sort((a, c) => a.date.localeCompare(c.date));
}
function hourlyTotals(hourly) {
  const by = new Map();
  for (const x of hourly) {
    if (!isNum(x.powerW)) continue;
    const k = String(x.ts);
    if (!by.has(k)) by.set(k, { ts: fmt.parseDate(normIso(x.ts)), total: 0, per: {} });
    const r = by.get(k); r.per[x.node] = x.powerW; r.total += x.powerW;
  }
  return [...by.values()].filter((r) => r.ts).sort((a, c) => a.ts - c.ts);
}
function sankeyData(devices, daily, price, co2g) {
  const days = new Set(daily.map((x) => String(x.date).slice(0, 10))).size;
  const nodes = [{ id: 'grid', label: 'Stromnetz', kind: 'grid', method: 'estimated' }];
  const links = [];
  const purposes = new Map();
  const add = (pid, label) => { if (!purposes.has(pid)) purposes.set(pid, { id: pid, label, kind: 'purpose', method: 'estimated' }); };
  const cost = (k) => (isNum(price) ? k * price : null), co2 = (k) => (isNum(co2g) ? (k * co2g) / 1000 : null);
  for (const d of devices) {
    // 30-Tage-Energie: ab 7 erfassten Tagen aus den Tageswerten, sonst aus der aktuellen Leistung hochgerechnet
    const sum = daily.filter((x) => x.node === d.node && isNum(x.kwh)).reduce((s, x) => s + x.kwh, 0);
    const kwh = days >= 7 ? (sum * 30) / days : isNum(d.powerW) ? (d.powerW * 24 * 30) / 1000 : 0;
    if (!(kwh > 0)) continue;
    nodes.push({ id: d.node, label: d.label ?? d.node, kind: 'device', method: d.method });
    links.push({ source: 'grid', target: d.node, value: kwh, method: d.method, costChf: cost(kwh), co2Kg: co2(kwh) });
    const base = isNum(d.baseload?.w) ? d.baseload.w : d.idleW;
    const idleShare = d.node === 'network' ? 0 : Math.min(1, isNum(base) && isNum(d.powerW) && d.powerW > 0 ? base / d.powerW : 0.9);
    const idle = kwh * idleShare, use = kwh - idle;
    if (idle > 0.01) { add('idle', 'Leerlauf / Bereitschaft'); links.push({ source: d.node, target: 'idle', value: idle, method: 'estimated', costChf: cost(idle), co2Kg: co2(idle) }); }
    if (use > 0.01) { const pid = `p-${d.node}`; add(pid, PURPOSE[d.node] ?? 'Sonstiges'); links.push({ source: d.node, target: pid, value: use, method: 'estimated', costChf: cost(use), co2Kg: co2(use) }); }
  }
  return { nodes: [...nodes, ...purposes.values()], links };
}
function shareByDevice(daily, devices) {
  const by = new Map(devices.map((d) => [d.node, 0]));
  for (const x of daily) if (isNum(x.kwh) && by.has(x.node)) by.set(x.node, by.get(x.node) + x.kwh);
  const total = [...by.values()].reduce((s, v) => s + v, 0);
  const rows = devices.map((d) => ({ node: d.node, label: d.label ?? d.node, method: d.method, kwh: by.get(d.node), pct: total ? by.get(d.node) / total : 0 })).sort((a, c) => c.kwh - a.kwh);
  return { total, rows };
}

/* ---------- Hero-Karte ---------- */
function heroCard(h, daily) {
  const c = card({ title: 'Hero im Detail', accent: 'Hero', sub: 'Die KI-Workstation ist der grösste Einzelposten. Gemessen wird nur die GPU (nvidia-smi); CPU, Speicher, SSD, Netzwerk und Netzteilverluste sind geschätzt.' });
  if (!h) { c.append(emptyNote('Hero liefert keine Energiedaten.')); return c; }
  const idle = isNum(h.baseload?.w) && isNum(h.powerW) && h.powerW > 0 ? Math.min(1, h.baseload.w / h.powerW) : null;
  c.append(dl([
    ['Leistung jetzt', isNum(h.powerW) ? `${fmt.int(h.powerW)} W (davon GPU gemessen ${fmt.num1(h.gpuPowerW ?? h.measuredW)} W)` : '–'],
    ['Leerlaufanteil', isNum(idle) ? `etwa ${fmt.pct(idle * 100)} seiner Leistung ist Grundlast` : 'noch nicht bestimmbar'],
    ['GPU aktiv (24 h)', isNum(h.gpuActiveHours24h) ? `${fmt.int(h.gpuActiveHours24h)} von ${fmt.int(h.gpuSampledHours24h ?? 24)} erfassten Stunden` : '–'],
    ['GPU-Auslastung jetzt', isNum(h.gpuUtilPct) ? fmt.pct(h.gpuUtilPct) : '–'],
    ['Geladene Modelle', (h.modelsLoaded ?? []).length ? h.modelsLoaded.map((m) => `${m.name} (${fmt.num1(m.sizeGB)} GB)`).join(', ') : 'keines'],
    ['Wh je 1’000 Tokens', 'noch nicht erhoben – Ollama liefert die Token-Zahlen bisher nicht an den Kollektor'],
    ['Firmware', h.firmware ? Object.values(h.firmware).filter(Boolean).join(' · ') : undefined],
  ]));
  if ((h.notCollected ?? []).length) c.append(el('p.small.muted', `Nicht erhoben: ${h.notCollected.join(', ')}.`));
  return c;
}

/* ---------- So rechnen wir ---------- */
function methodSection(devices, cfg, e) {
  const det = el('details.card.method');
  det.append(el('summary', el('strong', 'So rechnen wir'), el('span.small.muted', ' – Methode je Gerät, Annahmen und Quellen')));
  det.append(dataTable([
    { key: 'label', label: 'Gerät', render: (d) => d.label ?? d.node },
    { key: 'method', label: 'Erhebung', render: (d) => METHOD_WORD[d.method] ?? 'unbekannt' },
    { key: 'components', label: 'Bestandteile', render: (d) => (d.components ?? []).map((x) => `${x.label ?? x.name}${isNum(x.w ?? x.powerW) ? ` ${fmt.num1(x.w ?? x.powerW)} W` : ''}${x.method ? ` (${METHOD_WORD[x.method] ?? x.method})` : ''}`).join(' · ') || '–' },
    { key: 'notes', label: 'Annahme', render: (d) => (Array.isArray(d.notes) ? d.notes.join(' ') : d.notes ?? '–') },
  ], devices, { compact: true }));
  const src = cfg.sources ?? [];
  if (src.length) {
    det.append(el('h4.tariff__title', 'Quellen'));
    det.append(el('ul.sources', src.map((s) => {
      if (typeof s === 'string') return el('li', s);
      const li = el('li', s.label ?? s.id ?? '');
      for (const u of [s.url, s.url2].filter(Boolean)) li.append(' ', el('a', { href: u, target: '_blank', rel: 'noopener noreferrer' }, 'Quelle'));
      return li;
    })));
  }
  if (e.honesty) det.append(el('p.small.muted', typeof e.honesty === 'string' ? e.honesty : Object.values(e.honesty).filter((x) => typeof x === 'string').join(' ')));
  det.append(el('p.small.muted', 'Grundlast = Median der Stundenmittel 01–05 Uhr der letzten 14 Tage. Prognosen = Mittel der letzten 24 Stunden hochgerechnet. Lücken werden nie mit 0 gefüllt. Für echte Messwerte braucht es messende Zwischenstecker (z. B. Shelly, myStrom, FRITZ!DECT) – die Konfiguration ist dafür vorbereitet.'));
  return det;
}

/* ---------- Tarif & Faktoren ---------- */
function tariffSection({ prices, co2Factors, daily, ctx }) {
  const sec = card({ title: 'Tarif & Faktoren', accent: 'Tarif', sub: 'Preisperioden und CO₂-Faktoren, die Falk hinterlegt hat. Jede Periode gilt ab ihrem Datum bis zur nächsten. Neue Werte wirken auf alle Tage ab „gültig ab“ – der Kollektor rechnet Kosten und CO₂ beim nächsten Lauf neu.' });
  const fc = chartFrame({ title: 'Stromkosten pro Tag', accent: 'Tag', sub: 'Summe über alle Geräte in CHF. Senkrechte Marken zeigen Preiswechsel mit dem neuen Tarif.' });
  const plot = el('div.echart'); fc.plot.append(plot);
  if (daily.some((x) => isNum(x.costChf))) costSeriesE(plot, daily, prices, { days: 60 }).catch((err) => plot.replaceChildren(emptyNote(`Diagramm konnte nicht geladen werden: ${err.message}`)));
  else plot.replaceChildren(emptyNote('Keine Kostenwerte je Tag (costChf) erhoben.'));
  fc.table([{ key: 'k', label: 'Tag', render: (r) => fmt.date(r.k) }, { key: 'v', label: 'CHF', num: true, render: (r) => fmt.num2(r.v) }], sumByDate(daily, 'costChf').slice(-14).reverse());
  sec.append(fc.el);

  const twoCol = el('div.grid.grid--2.tariff');
  twoCol.append(periodBlock({
    title: 'Preisperioden', unit: 'Rp./kWh', items: prices, ctx,
    columns: [{ key: 'validFrom', label: 'Gültig ab', render: (r) => fmt.date(r.validFrom) }, { key: 'chfPerKwh', label: 'Rp./kWh', num: true, render: (r) => fmt.num1(r.chfPerKwh * 100) }, { key: 'note', label: 'Notiz' }, { key: 'setAt', label: 'Gesetzt', render: (r) => (r.setAt ? fmt.dateShort(r.setAt) : '–') }],
    endpoint: '../api/energy-price', field: 'chfPerKwh', toValue: (v) => Math.round(v) / 100, min: 1, max: 200, step: 1, placeholder: 'z. B. 29', hint: 'Rappen pro Kilowattstunde, 1 bis 200',
    confirmText: (d, v, n) => `Ab ${fmt.date(d)} gilt ${fmt.int(v)} Rp./kWh${n ? ` („${n}“)` : ''}. Kosten werden ab diesem Tag neu berechnet.`,
  }));
  twoCol.append(periodBlock({
    title: 'CO₂-Faktoren', unit: 'g/kWh', items: co2Factors, ctx,
    columns: [{ key: 'validFrom', label: 'Gültig ab', render: (r) => fmt.date(r.validFrom) }, { key: 'gPerKwh', label: 'g CO₂/kWh', num: true, render: (r) => fmt.int(r.gPerKwh) }, { key: 'note', label: 'Notiz' }],
    endpoint: '../api/energy-co2', field: 'gPerKwh', toValue: (v) => Math.round(v), min: 0, max: 2000, step: 1, placeholder: 'z. B. 90', hint: 'Gramm CO₂eq pro Kilowattstunde, 0 bis 2000',
    confirmText: (d, v, n) => `Ab ${fmt.date(d)} gilt ${fmt.int(v)} g CO₂/kWh${n ? ` („${n}“)` : ''}. CO₂-Werte werden ab diesem Tag neu berechnet.`,
  }));
  sec.append(twoCol);
  return sec;
}

function periodBlock({ title, unit, items, columns, endpoint, field, toValue, min, max, step, placeholder, hint, confirmText, ctx }) {
  const box = el('div.tariff__block');
  box.append(el('h4.tariff__title', title));
  const tableWrap = el('div.tariff__table');
  const draw = (list) => {
    clear(tableWrap);
    const cols = [...columns, { key: '_del', label: '', render: (r) => { const btn = el('button.btn.btn--ghost.btn--small', { type: 'button', 'aria-label': `Periode ab ${fmt.date(r.validFrom)} löschen` }, lu('x'), 'Löschen'); btn.addEventListener('click', () => remove(r)); return btn; } }];
    const tb = dataTable(cols, [...list].sort((a, b) => String(b.validFrom).localeCompare(String(a.validFrom))), { compact: true, empty: 'Noch keine Periode hinterlegt.' });
    if (field === 'chfPerKwh') tb.querySelector('table')?.classList.add('table--prices');
    tableWrap.append(tb);
  };
  draw(items);
  box.append(tableWrap);

  const form = el('form.tariff__form', { novalidate: 'novalidate', 'aria-label': `${title} hinzufügen` });
  const inDate = el('input.input', { type: 'date', name: 'validFrom', required: 'required', id: `${field}-date` });
  const inVal = el('input.input.input--num', { type: 'number', name: 'value', required: 'required', min: String(min), max: String(max), step: String(step), placeholder, inputmode: 'decimal', id: `${field}-val` });
  const inNote = el('input.input', { type: 'text', name: 'note', maxlength: '80', placeholder: 'Notiz (optional)', id: `${field}-note` });
  const submit = el('button.btn.btn--primary', { type: 'submit' }, lu('plus'), 'Periode setzen');
  const err = el('div.tariff__error', { role: 'alert' });
  form.append(
    el('div.tariff__row', el('label.tariff__field', el('span.label', 'Gültig ab'), inDate), el('label.tariff__field', el('span.label', unit), inVal), el('label.tariff__field.tariff__field--wide', el('span.label', 'Notiz'), inNote)),
    el('div.tariff__row', el('span.small.muted', hint), submit), err);
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault(); err.textContent = '';
    const d = inDate.value, raw = Number(inVal.value), note = inNote.value.trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d) || Number.isNaN(new Date(d).getTime())) { err.textContent = 'Bitte ein gültiges Datum angeben.'; inDate.focus(); return; }
    if (d > new Date(Date.now() + 366 * 864e5).toISOString().slice(0, 10)) { err.textContent = 'Das Datum liegt mehr als ein Jahr in der Zukunft.'; inDate.focus(); return; }
    if (!Number.isFinite(raw) || raw < min || raw > max) { err.textContent = `Bitte einen Wert zwischen ${min} und ${max} ${unit} angeben.`; inVal.focus(); return; }
    const value = toValue(raw);
    const ok = await confirmDialog({ title: `${title.replace(/n$/, '')} ab ${fmt.date(d)} setzen?`, text: confirmText(d, raw, note), confirmLabel: 'Setzen', cancelLabel: 'Abbrechen' });
    if (!ok) return;
    await post({ validFrom: d, [field]: value, note: note || undefined }, 'Periode gespeichert.');
  });
  box.append(form);
  async function remove(r) {
    const ok = await confirmDialog({ title: `Periode ab ${fmt.date(r.validFrom)} löschen?`, text: 'Die davor liegende Periode gilt dann weiter. Kosten bzw. CO₂ werden beim nächsten Lauf neu berechnet.', confirmLabel: 'Löschen', cancelLabel: 'Abbrechen' });
    if (!ok) return;
    await post({ validFrom: r.validFrom, delete: true }, 'Periode gelöscht.');
  }
  async function post(body, okMsg) {
    submit.disabled = true;
    try {
      const r = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (r.status === 404) { ctx.toast(`Der Server kennt ${endpoint.replace('..', '')} noch nicht.`); return; }
      if (r.status === 400) { const j = await r.json().catch(() => ({})); ctx.toast(j.error ? `Abgelehnt: ${String(j.error)}` : 'Der Server hat die Eingabe abgelehnt (HTTP 400).'); return; }
      if (!r.ok) { ctx.toast(`Speichern fehlgeschlagen (HTTP ${r.status}).`); return; }
      const j = await r.json().catch(() => ({}));
      if (j && j.ok === false) { ctx.toast(j.error ? String(j.error) : 'Der Server meldet einen Fehler.'); return; }
      const list = Array.isArray(j.prices) ? j.prices : Array.isArray(j.co2Factors) ? j.co2Factors : null;
      if (list) draw(list);
      form.reset();
      ctx.toast(`${okMsg} Kollektor wird angestossen…`, 4000);
      ctx.refresh();
    } catch { ctx.toast('Kein Server erreichbar – Änderung konnte nicht gespeichert werden.'); }
    finally { submit.disabled = false; }
  }
  return box;
}

/* ---------- Bausteine ---------- */
function kpi({ label, value, unit, state, foot, spark }) {
  const tile = el('div.kpi.kpi--static', { role: 'group', 'aria-label': `${label}: ${value} ${unit ?? ''} – ${foot}` });
  tile.append(el('span.kpi__label', label));
  tile.append(el('span.kpi__value', value, unit ? el('small', unit) : null));
  const sp = el('span.kpi__spark', { 'aria-hidden': 'true' });
  sparkline(sp, spark, { width: 84, height: 30 });
  tile.append(sp);
  tile.append(el('span.kpi__foot', mark(state), el('span', foot)));
  return tile;
}

function shareBar(share, devices) {
  const colors = deviceColors(devices);
  const wrap = el('div');
  const bar = el('div.sharebar', { role: 'img', 'aria-label': share.rows.map((r) => `${r.label} ${fmt.pct(r.pct * 100)}`).join(', ') });
  for (const r of share.rows) bar.append(el('div.sharebar__seg', { class: `sharebar__seg${r.method === 'estimated' ? ' sharebar__seg--est' : ''}`, style: { width: `${r.pct * 100}%`, background: colors[r.node] }, title: `${r.label}: ${fmt.pct(r.pct * 100)}` }));
  wrap.append(bar);
  wrap.append(dataTable([
    { key: 'label', label: 'Gerät', render: (r) => el('span.row', el('span.legend__swatch', { style: { background: colors[r.node] } }), r.label) },
    { key: 'kwh', label: 'kWh (30 T.)', num: true, render: (r) => fmt.num1(r.kwh) },
    { key: 'pct', label: 'Anteil', num: true, render: (r) => fmt.pct(r.pct * 100) },
    { key: 'method', label: 'Erhebung', render: (r) => METHOD[r.method]?.word ?? 'unbekannt' },
  ], share.rows, { compact: true }));
  return wrap;
}

function sumByDate(list, field, keyFn = (x) => String(x.date).slice(0, 10)) {
  const by = new Map();
  for (const x of list) { if (!isNum(x[field])) continue; const k = keyFn(x); by.set(k, (by.get(k) ?? 0) + x[field]); }
  return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([k, v]) => ({ k, v }));
}
function pivot(daily, devices) {
  const by = new Map();
  for (const x of daily) { const k = String(x.date).slice(0, 10); if (!by.has(k)) by.set(k, { date: k, sum: 0 }); const r = by.get(k); if (isNum(x.kwh)) { r[x.node] = (r[x.node] ?? 0) + x.kwh; r.sum += x.kwh; } }
  return [...by.values()].sort((a, b) => a.date.localeCompare(b.date));
}
function findHousehold(cfg) {
  if (isNum(cfg.householdKwhYear)) return { kwh: cfg.householdKwhYear, source: cfg.householdSource ?? 'Quelle in energy.config' };
  const s = (cfg.sources ?? []).map(String).find((x) => /haushalt/i.test(x) && /kwh/i.test(x));
  if (!s) return null;
  const m = s.replace(/[’'` \s]/g, '').match(/≈?(\d{3,6})kWh/i);
  return m ? { kwh: Number(m[1]), source: s } : null;
}
const avg = (a) => (a.length ? a.reduce((s, v) => s + v, 0) / a.length : null);
const mul = (...xs) => (xs.every(isNum) ? xs.reduce((s, v) => s * v, 1) : null);
