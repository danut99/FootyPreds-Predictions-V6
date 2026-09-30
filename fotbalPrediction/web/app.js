'use strict';

const matchesNode = document.querySelector('#matches');
const statusNode = document.querySelector('#status');
const picker = document.querySelector('#date-picker');
const summaryNode = document.querySelector('#summary');
// Orele și zilele se afișează mereu la ora României, indiferent de fusul orar al calculatorului.
const TIME_ZONE = 'Europe/Bucharest';
const SELECT = 'selectează';
const PAGE = 400;
const dayFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit'
});
// Grupurile modelului -> filtrul „Tip pariu”.
const KIND_OF_GROUP = {
  '1x2': '1x2', dc: 'dc', goals: 'goals', team_goals: 'team_goals', btts: 'btts', ah: 'ah',
  dnb: 'ah', cs: 'cs', ht: 'ht', corners: 'corners', team_corners: 'corners',
  corners_ah: 'corners', cards: 'cards', team_cards: 'cards', bookings: 'cards', sot: 'sot',
  team_sot: 'sot'
};
const KIND_LABELS = {
  all: 'toate piețele', '1x2': '1X2', dc: 'șansă dublă', goals: 'goluri', team_goals: 'goluri pe echipă',
  btts: 'GG/NG', ah: 'handicap', cs: 'scor exact', ht: 'pauză', corners: 'cornere',
  cards: 'cartonașe', sot: 'șuturi pe poartă'
};
// Cheile modelului de bază FootyPreds (rezervă) care au și etichete aici.
const CORE_LABELS = {
  1: 'Victorie gazde', X: 'Egal', 2: 'Victorie oaspeți', '1X': 'Gazde sau egal', X2: 'Egal sau oaspeți',
  12: 'Fără egal', over15: 'Peste 1.5 goluri', over25: 'Peste 2.5 goluri', over35: 'Peste 3.5 goluri',
  under25: 'Sub 2.5 goluri', btts: 'Ambele marchează', no_btts: 'Nu marchează ambele'
};
const CORE_GROUPS = {
  1: '1x2', X: '1x2', 2: '1x2', '1X': 'dc', X2: 'dc', 12: 'dc', over15: 'goals', over25: 'goals',
  over35: 'goals', under25: 'goals', btts: 'btts', no_btts: 'btts'
};
let selectedDay = localDay(new Date());
let loadedItems = [];
let minimumChance = 0;
let valueMode = false;
let marketType = 'all';
let dailyMode = 'single';
let matchStatus = 'all';
let sourceMode = 'estimate';
let liveItems = [];
let liveMode = 'single';
let dayRequest = 0;

function localDay(day) {
  return dayFormat.format(day);
}
function clockTime(value, options = {}) {
  return new Date(value).toLocaleTimeString('ro-RO', {
    timeZone: TIME_ZONE, hour: '2-digit', minute: '2-digit', ...options
  });
}
function shifted(iso, amount) {
  const day = new Date(`${iso}T12:00:00Z`); day.setUTCDate(day.getUTCDate() + amount);
  return day.toISOString().slice(0, 10);
}
function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}
function percent(value) { return `${Math.round(Number(value || 0) * 100)}%`; }
function price(value) { return Number(value || 0).toFixed(2); }
function prettyDay(iso, options = {}) {
  return new Intl.DateTimeFormat('ro-RO', {...options, timeZone: 'UTC'})
    .format(new Date(`${iso}T12:00:00Z`));
}
function renderDates() {
  const today = localDay(new Date());
  document.querySelector('#date-strip').innerHTML = [-2, -1, 0, 1, 2].map((offset) => {
    const day = shifted(selectedDay, offset);
    const label = day === today ? 'Azi' : prettyDay(day, {weekday: 'short'}).replace('.', '');
    return `<button type="button" class="date-pill ${day === selectedDay ? 'active' : ''}" data-day="${esc(day)}">
      <span>${esc(label)}</span><strong>${esc(prettyDay(day, {day: '2-digit'}))}</strong>
      <small>${esc(prettyDay(day, {month: 'short'}).replace('.', ''))}</small></button>`;
  }).join('');
  document.querySelectorAll('.date-pill').forEach((button) => button.addEventListener('click', () => {
    selectedDay = button.dataset.day; loadDay();
  }));
  picker.value = selectedDay;
}

// ------------------------------------------------------------------ piețe și selecții

function highMode() { return minimumChance >= 0.85; }
function isSelected(market) {
  return (highMode() ? market.decision_high : market.decision) === SELECT;
}
function offeredOdds(market) { return Number(market.odds || market.fair_odds || 0); }
function worthwhile(market) {
  // „Sigur + cotă”: selectată de regula de 80% și cu o cotă (reală sau corectă) de cel puțin 1.20.
  return market.decision === SELECT && offeredOdds(market) >= 1.2;
}
function coreMarkets(item) {
  // Rezervă: modelul de bază FootyPreds, niciodată selectabil.
  const probabilities = item.probabilities || {};
  return Object.keys(CORE_LABELS).filter((key) => Number(probabilities[key]) > 0).map((key) => ({
    key, label: CORE_LABELS[key], group: CORE_GROUPS[key], probability: Number(probabilities[key]),
    fair_odds: 1 / Number(probabilities[key]), odds: null, selectable: false,
    decision: 'fără pariu', decision_high: 'fără pariu', won: null
  }));
}
function allMarkets(item) {
  if (item.fp && item.fp.markets && item.fp.markets.length) return item.fp.markets;
  return coreMarkets(item);
}
function marketKind(market) { return KIND_OF_GROUP[market.group] || 'other'; }
function marketsFor(item) {
  let markets = allMarkets(item).filter((market) => marketType === 'all' || marketKind(market) === marketType);
  if (valueMode) markets = markets.filter(worthwhile);
  else if (minimumChance) markets = markets.filter((market) => Number(market.probability) >= minimumChance);
  return markets.slice().sort((left, right) => (isSelected(right) - isSelected(left))
    || (Number(right.probability) - Number(left.probability)));
}
function chosenMarkets(item) {
  // Piețele selectate de regulă în filtrul curent, cea mai probabilă prima.
  return marketsFor(item).filter((market) => (valueMode ? worthwhile(market) : isSelected(market)));
}
function awaitsStats(item) {
  // Meci încheiat al cărui rând football-data nu a sosit: numai golurile se pot deconta acum.
  const fp = item.fp;
  return item.match.status === 'finished' && Boolean(fp) && fp.source === 'model'
    && !fp.settled_stats && !fp.extra_time;
}
function settleOrder(item, markets) {
  // Pe un meci încheiat fără rândul football-data, selecțiile de goluri (decontabile din scorul
  // final) trec în față. Ordinea depinde doar de tipul pieței, niciodată de rezultat.
  if (!awaitsStats(item)) return markets;
  return markets.filter((market) => market.stat === 'goals')
    .concat(markets.filter((market) => market.stat !== 'goals'));
}
function primaryPick(item) { return chosenMarkets(item)[0] || null; }
function pickOf(item) {
  // Selecția afișată: prima piață selectată; pe un meci încheiat, prima care se poate deconta.
  return settleOrder(item, chosenMarkets(item))[0] || null;
}
function isRetro(item) { return Boolean(item.fp && item.fp.retro); }
function sourceLabel(item) {
  const fp = item.fp;
  if (!fp) return 'Modelul de fotbal nu a răspuns: probabilități FootyPreds de bază (fără selecții).';
  if (fp.source === 'model') {
    const parts = [`Model fotbal · ${fp.league_code} · ${fp.home_model} – ${fp.away_model}`];
    if (fp.odds_blend) parts.push('amestecat cu cotele 1X2 (pondere piață 0.9)');
    if (fp.journal) parts.push('predicție din jurnal, făcută înainte de meci');
    return parts.join(' · ');
  }
  const reason = fp.reason ? ` (${fp.reason})` : '';
  if (fp.source === 'market') {
    return `Estimare din cotele 1X2 (fără model de echipă${reason}): doar piețe de goluri`
      + `${fp.journal ? ' · estimare din jurnal, făcută înainte de meci' : ''}.`;
  }
  if (fp.source === 'core') return `Fără model fotbal${reason}: probabilități FootyPreds de bază, fără selecții.`;
  return `Fără model${reason}.`;
}
function wonMark(market) {
  if (market.won === true) return ' <b class="mark-won">✓</b>';
  if (market.won === false) return ' <b class="mark-lost">×</b>';
  return '';
}
function marketNote(market) {
  if (isSelected(market)) {
    const mode = highMode() ? '85%' : '80%';
    return market.odds ? `✓ ${mode} · cotă ${price(market.odds)}` : `✓ ${mode} · corectă ${price(market.fair_odds)}`;
  }
  if (market.selectable === false) return market.fair_odds ? `orientativ ${price(market.fair_odds)}` : 'orientativ';
  if (market.odds) return `cotă ${price(market.odds)}`;
  return market.fair_odds ? `corectă ${price(market.fair_odds)}` : 'cotă —';
}
function marketRows(item) {
  const rows = marketsFor(item).slice(0, 6);
  if (!rows.length) return '<div class="no-suggestion">Nicio piață nu trece filtrele.</div>';
  return rows.map((market) => `<div class="market-row ${isSelected(market) ? 'is-selected' : ''}">
    <span>${esc(market.label)}${wonMark(market)}</span><strong>${esc(percent(market.probability))}</strong>
    <small>${esc(marketNote(market))}</small></div>`).join('');
}
function verdictBadge(item, pick) {
  const status = item.match.status;
  if (status === 'live') return '<span class="verdict live">LIVE</span>';
  if (status !== 'finished') return '<span class="verdict pending">PROGRAMAT</span>';
  if (!pick) return '<span class="verdict nobet">FĂRĂ PARIU</span>';
  if (pick.won === true) return '<span class="verdict won">✓ NIMERIT</span>';
  if (pick.won === false) return '<span class="verdict lost">× RATAT</span>';
  return '<span class="verdict void">NEDECONTAT</span>';
}
function matchCard(item) {
  const match = item.match;
  const pick = pickOf(item);
  const center = item.result ? `final · ${esc(item.result.score)}` : esc(clockTime(match.kickoff));
  const cardState = pick && pick.won === true ? 'is-won' : pick && pick.won === false ? 'is-lost' : '';
  const recommendation = pick
    ? `<div class="recommendation"><div><small>${item.fp && item.fp.source === 'market' ? 'SELECȚIE DIN COTE' : 'SELECȚIA MODELULUI'} · ${esc(highMode() ? 'REGULA STRICTĂ 85%' : 'REGULA 80%')}</small>
        <strong>${esc(pick.label)}</strong></div><b>${esc(percent(pick.probability))}</b></div>`
    : `<div class="recommendation none"><div><small>DECIZIA MODELULUI</small>
        <strong>Fără pariu în filtrul ales</strong></div><b>—</b></div>`;
  const retro = isRetro(item)
    ? '<span class="source-note warn">Predicție calculată după antrenarea pe această zi (retroactivă): nu intră în precizia zilei.</span>'
    : '';
  const primary = primaryPick(item);
  const swapped = primary && pick && primary !== pick
    ? ` Selecția principală („${esc(primary.label)}”) așteaptă datele; se verifică prima selecție de goluri.`
    : '';
  const unsettled = awaitsStats(item)
    ? `<span class="source-note">Pauza, cornerele, cartonașele și șuturile se decontează după ce football-data publică meciul.${swapped}</span>`
    : '';
  return `<article class="match-card ${cardState}">
    <header><span>${esc(item.competition)}</span>${verdictBadge(item, pick)}</header>
    <div class="players-card"><div><strong>${esc(match.home)}</strong><small>Gazde</small></div>
      <div class="score"><b>${center}</b><span>VS</span></div>
      <div class="away"><strong>${esc(match.away)}</strong><small>Oaspeți</small></div></div>
    <div class="markets-title"><span>Posibilități de pariere</span><small>șansă estimată</small></div>
    <div class="markets-list">${marketRows(item)}</div>
    ${recommendation}${retro}${unsettled}
    <footer><span>${esc(sourceLabel(item))}</span></footer></article>`;
}

// ------------------------------------------------------------------ tabla zilei

function visibleItems() {
  return loadedItems.filter((item) => matchStatus === 'all'
    || (matchStatus === 'upcoming' && item.match.status === 'scheduled')
    || (matchStatus === 'finished' && item.match.status === 'finished'))
    .filter((item) => sourceMode === 'all' || (item.fp && (item.fp.source === 'model'
      || (sourceMode === 'estimate' && item.fp.source === 'market'))))
    .filter((item) => (!minimumChance && !valueMode) || pickOf(item));
}
function renderSummary(items) {
  const settled = items.filter((item) => !isRetro(item)).map(pickOf)
    .filter((market) => market && typeof market.won === 'boolean');
  if (!settled.length) { summaryNode.classList.add('hidden'); return; }
  const won = settled.filter((market) => market.won).length;
  const retro = items.filter(isRetro).length;
  summaryNode.innerHTML = `<div><strong>${won}/${settled.length}</strong><span>selecții nimerite</span></div>
    <div><strong>${esc(percent(won / settled.length))}</strong><span>precizia zilei (fără retroactive)</span></div>
    <div><strong>${items.length}</strong><span>meciuri afișate${retro ? ` · ${retro} retroactive` : ''}</span></div>`;
  summaryNode.classList.remove('hidden');
}
function categoryStats() {
  document.querySelectorAll('[data-market]').forEach((button) => {
    const category = button.dataset.market;
    if (category === 'all') { button.textContent = button.dataset.label; return; }
    const settled = loadedItems.filter((item) => !isRetro(item)).map((item) => allMarkets(item)
      .filter((market) => marketKind(market) === category && market.decision === SELECT)
      .sort((left, right) => Number(right.probability) - Number(left.probability))[0])
      .filter((market) => market && typeof market.won === 'boolean');
    const won = settled.filter((market) => market.won).length;
    button.textContent = `${button.dataset.label}${settled.length ? ` · ${won}/${settled.length}` : ''}`;
  });
}
function comboVerdict(combo) {
  // O combinație cu un meci retroactiv (modelul văzuse deja rezultatul) nu se verifică.
  if (combo.retro) return 'retro';
  const results = [combo.a.market.won, combo.b.market.won];
  if (results.every((won) => won === true)) return 'won';
  if (results.some((won) => won === false)) return 'lost';
  return [combo.a, combo.b].every((leg) => leg.item.match.status === 'finished') ? 'void' : 'pending';
}
const COMBO_BADGES = {
  won: '<span class="verdict won">✓ NIMERITĂ</span>',
  lost: '<span class="verdict lost">× RATATĂ</span>',
  retro: '<span class="verdict void">NEVERIFICABILĂ</span>',
  void: '<span class="verdict void">NEDECONTATĂ</span>',
  pending: '<span class="verdict pending">PROGRAMATĂ</span>'
};
function dailyDoubleCards(items) {
  const candidates = items.map((item) => ({item,
    markets: settleOrder(item, chosenMarkets(item)).slice(0, 3)}))
    .filter((entry) => entry.markets.length);
  const doubles = [];
  for (let first = 0; first < candidates.length; first += 1) {
    for (let second = first + 1; second < candidates.length; second += 1) {
      for (const marketA of candidates[first].markets) for (const marketB of candidates[second].markets) {
        doubles.push({a: {item: candidates[first].item, market: marketA},
          b: {item: candidates[second].item, market: marketB},
          probability: marketA.probability * marketB.probability,
          odds: offeredOdds(marketA) * offeredOdds(marketB),
          retro: isRetro(candidates[first].item) || isRetro(candidates[second].item)});
      }
    }
  }
  doubles.sort((left, right) => right.probability - left.probability || right.odds - left.odds);
  const selected = doubles.slice(0, 30);
  const verdicts = selected.map(comboVerdict);
  const won = verdicts.filter((verdict) => verdict === 'won').length;
  const verified = won + verdicts.filter((verdict) => verdict === 'lost').length;
  const retro = verdicts.filter((verdict) => verdict === 'retro').length;
  if (verified) {
    summaryNode.innerHTML = `<div><strong>${won}/${verified}</strong><span>nimerite / verificate</span></div>
      <div><strong>${esc(percent(won / verified))}</strong><span>precizie verificată (fără retroactive)</span></div>
      <div><strong>${selected.length - verified}</strong><span>neverificate${retro ? ` (${retro} retroactive)` : ''} · ${selected.length} total</span></div>`;
    summaryNode.classList.remove('hidden');
  } else {
    summaryNode.classList.add('hidden');
  }
  return selected.map((combo, position) => {
    const verdict = COMBO_BADGES[verdicts[position]];
    const retroNote = combo.retro
      ? '<span class="source-note warn">Include o predicție retroactivă (modelul văzuse deja meciul): nu intră în precizie.</span>'
      : '';
    return `<article class="match-card combo-card"><header><span>COMBINAȚIE · 2 MECIURI</span>${verdict}</header>
      <div class="combo-probability"><strong>${esc(percent(combo.probability))}</strong>
        <span>șansă combinată · cotă ${esc(price(combo.odds))}</span></div>
      ${[combo.a, combo.b].map((leg, index) => `<div class="combo-leg"><i>${index + 1}</i><div>
        <small>${esc(leg.item.match.home)} – ${esc(leg.item.match.away)}</small>
        <strong>${esc(leg.market.label)}</strong></div><b>${esc(percent(leg.market.probability))}</b></div>`).join('')}
      ${retroNote}<footer><span>Selecții din meciuri diferite · probabilități înmulțite · cotă reală sau corectă</span></footer></article>`;
  }).join('');
}
function renderBoard() {
  const visible = visibleItems();
  const modelCount = loadedItems.filter((item) => item.fp && item.fp.source === 'model').length;
  const chanceNote = valueMode ? ' · selectate (80%) cu cotă reală/corectă ≥ 1.20'
    : minimumChance ? ` · selecții ≥ ${percent(minimumChance)}${highMode() ? ' (regula strictă 85%)' : ''}` : '';
  const marketCount = loadedItems.filter((item) => item.fp && item.fp.source === 'market').length;
  document.querySelector('#filter-note').textContent = `${visible.length} din ${loadedItems.length} meciuri`
    + ` (${modelCount} cu model, ${marketCount} estimate din cote) · ${KIND_LABELS[marketType]}${chanceNote}`;
  if (dailyMode === 'double') {
    summaryNode.classList.add('hidden');
    const cards = dailyDoubleCards(visible);
    matchesNode.innerHTML = cards || '<div class="empty"><strong>Nu există combinații pentru filtrele alese.</strong></div>';
    return;
  }
  renderSummary(visible);
  const ordered = visible.slice().sort((left, right) => Boolean(pickOf(right)) - Boolean(pickOf(left))
    || new Date(left.match.kickoff) - new Date(right.match.kickoff));
  matchesNode.innerHTML = ordered.length ? ordered.map(matchCard).join('')
    : '<div class="empty"><strong>Niciun meci nu trece filtrul ales.</strong><span>Alege „Toate” sau alt tip de pariu.</span></div>';
}
async function fetchBoard(day) {
  const items = [];
  let total = 0;
  for (let offset = 0; offset < 2000; offset += PAGE) {
    const response = await fetch(`/core/api/predictions?day=${encodeURIComponent(day)}&sport=football&limit=${PAGE}&offset=${offset}`);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'API indisponibil');
    items.push(...(data.items || []));
    total = Number(data.total || items.length);
    if (!data.items || data.items.length < PAGE || items.length >= total) break;
  }
  return {items, total};
}
function payloadOf(item) {
  const match = item.match;
  const kickoff = new Date(match.kickoff);
  const odds = {};
  for (const [key, value] of Object.entries(match.odds || {})) {
    if (typeof value === 'number' && value > 1) odds[key] = value;
  }
  const probabilities = {};
  for (const [key, value] of Object.entries(item.probabilities || {})) {
    if (typeof value === 'number' && value >= 0 && value <= 1) probabilities[key] = value;
  }
  return {id: String(match.id), home: String(match.home || '').slice(0, 100),
    away: String(match.away || '').slice(0, 100), league: String(match.league || '').slice(0, 200),
    country: String(match.country || '').slice(0, 80),
    kickoff: Number.isNaN(kickoff.getTime()) ? null : kickoff.toISOString(),
    day: Number.isNaN(kickoff.getTime()) ? null : localDay(kickoff),
    status: match.status || null,
    home_goals: Number.isInteger(match.home_goals) ? match.home_goals : null,
    away_goals: Number.isInteger(match.away_goals) ? match.away_goals : null,
    finish_type: match.finish_type || null, odds, probabilities};
}
async function enhance(items) {
  // Modelul de fotbal primește meciurile în loturi de cel mult 400; eșecul lasă rezerva de bază.
  let changed = false;
  for (let start = 0; start < items.length; start += PAGE) {
    const chunk = items.slice(start, start + PAGE);
    try {
      const response = await fetch('/api/fotbal-probabilities', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(chunk.map(payloadOf))});
      if (!response.ok) return changed;
      const results = new Map((await response.json()).matches.map((match) => [match.id, match]));
      for (const item of chunk) item.fp = results.get(String(item.match.id));
      changed = true;
    } catch (_) { return changed; }
  }
  return changed;
}
async function loadDay() {
  const request = ++dayRequest;
  renderDates();
  matchesNode.innerHTML = '<div class="loader"><i></i><p>Analizez meciurile zilei…</p></div>';
  document.querySelector('#board-title').textContent = prettyDay(selectedDay, {weekday: 'long', day: 'numeric', month: 'long'});
  summaryNode.classList.add('hidden');
  try {
    const {items, total} = await fetchBoard(selectedDay);
    if (request !== dayRequest) return;
    document.querySelector('#day-label').textContent = `${total} MECIURI · FOTBAL`;
    loadedItems = items;
    await enhance(items);
    if (request !== dayRequest) return;
    categoryStats();
    renderBoard();
  } catch (error) {
    if (request !== dayRequest) return;
    matchesNode.innerHTML = `<div class="empty error"><strong>Nu am putut încărca meciurile.</strong><span>${esc(error.message)}</span></div>`;
  }
}

// ------------------------------------------------------------------ LIVE

function liveMarketKind(market) {
  const key = String(market.key);
  if (['1', 'X', '2'].includes(key)) return '1x2';
  if (['1X', 'X2', '12'].includes(key)) return 'dc';
  if (key.startsWith('dnb_')) return 'dnb';
  if (key.startsWith('next_goal')) return 'next';
  if (key === 'btts' || key === 'no_btts') return 'btts';
  if (key.startsWith('over') || key.startsWith('under')) return 'goals';
  return 'other';
}
function filteredLiveMarkets(item) {
  const type = document.querySelector('#live-market').value;
  const chanceValue = document.querySelector('#live-chance').value;
  const valueFilter = chanceValue === 'value';
  const chance = valueFilter ? 0.58 : Number(chanceValue);
  const direction = document.querySelector('#live-sort').value === 'desc' ? -1 : 1;
  return (item.markets || []).filter((market) => market.reliable !== false)
    .filter((market) => type === 'all' || liveMarketKind(market) === type)
    .filter((market) => Number(market.probability) >= chance)
    .filter((market) => !valueFilter || (market.selectable !== false && Number(market.fair_odds) >= 1.35))
    .sort((left, right) => direction * (Number(left.probability) - Number(right.probability)));
}
function liveStats(item) {
  const rows = (item.stats && (item.stats.match || item.stats['1st-half'])) || [];
  if (!rows.length) return '';
  return `<div class="stats-table">${rows.slice(0, 8).map((row) => `<div><strong>${esc(row.home)}</strong>
    <span>${esc(row.name)}</span><strong>${esc(row.away)}</strong></div>`).join('')}</div>`;
}
function liveCard(item) {
  const match = item.match;
  const markets = filteredLiveMarkets(item);
  const rows = markets.length ? markets.slice(0, 8).map((market) => `<div class="market-row">
    <span>${esc(market.label)} <em>${esc(market.group)}</em></span>
    <strong>${esc(percent(market.probability))}</strong><small>minim ${esc(price(market.fair_odds))}</small>
  </div>`).join('') : '<div class="no-suggestion">Nicio piață nu trece filtrele.</div>';
  const minute = item.minute ? `${item.minute}'` : (item.stage || 'LIVE');
  return `<article class="match-card live-card"><header><span>${esc(item.competition)}</span>
    <span class="verdict live">${esc(minute)}</span></header>
    <div class="players-card"><div><strong>${esc(match.home)}</strong><small>Gazde</small></div>
      <div class="score"><b>${esc(item.score?.home ?? 0)}–${esc(item.score?.away ?? 0)}</b><span>${esc(item.period || 'SCOR')}</span></div>
      <div class="away"><strong>${esc(match.away)}</strong><small>Oaspeți</small></div></div>
    <div class="markets-title"><span>Opțiuni live</span><small>cotă minimă acceptabilă</small></div>
    <div class="markets-list">${rows}</div>${liveStats(item)}<div class="live-summary">${esc(item.summary)}</div>
    <footer><span>Cotele afișate sunt cote corecte, nu cote live.</span>
      <button type="button" class="analyze-games" data-live-detail="${esc(match.id)}">
        ${item.stats ? 'Statistici actualizate' : 'Statistici live'}</button></footer></article>`;
}
async function liveDetail(matchId, button) {
  button.disabled = true; button.textContent = 'Cer statisticile…';
  try {
    const response = await fetch(`/core/api/live/${encodeURIComponent(matchId)}?sport=football&refresh=true`);
    const detail = await response.json();
    if (!response.ok) throw new Error(typeof detail.detail === 'string' ? detail.detail : 'Statisticile nu sunt disponibile');
    const position = liveItems.findIndex((item) => String(item.match.id) === String(matchId));
    if (position >= 0) liveItems[position] = detail;
    renderLive();
  } catch (error) {
    button.disabled = false; button.textContent = 'Reîncearcă';
    document.querySelector('#live-filter-note').textContent = error.message;
  }
}
function liveDoubleCards() {
  const direction = document.querySelector('#live-sort').value === 'desc' ? -1 : 1;
  const candidates = liveItems.map((item) => ({item, markets: filteredLiveMarkets(item)
    .filter((market) => market.probability <= 0.9 && market.fair_odds >= 1.1).slice(0, 4)}))
    .filter((entry) => entry.markets.length);
  const doubles = [];
  for (let first = 0; first < candidates.length; first += 1) {
    for (let second = first + 1; second < candidates.length; second += 1) {
      for (const marketA of candidates[first].markets) for (const marketB of candidates[second].markets) {
        doubles.push({a: {item: candidates[first].item, market: marketA}, b: {item: candidates[second].item, market: marketB},
          probability: marketA.probability * marketB.probability, odds: marketA.fair_odds * marketB.fair_odds});
      }
    }
  }
  doubles.sort((left, right) => direction * (left.probability - right.probability));
  return doubles.slice(0, 24).map((combo) => `<article class="match-card combo-card">
    <header><span>COMBINAȚIE · 2 MECIURI</span><span class="verdict pending">COTĂ MIN. ${esc(price(combo.odds))}</span></header>
    <div class="combo-probability"><strong>${esc(percent(combo.probability))}</strong><span>șansă combinată</span></div>
    ${[combo.a, combo.b].map((leg, index) => `<div class="combo-leg"><i>${index + 1}</i><div>
      <small>${esc(leg.item.match.home)} – ${esc(leg.item.match.away)}</small>
      <strong>${esc(leg.market.label)}</strong></div><b>${esc(percent(leg.market.probability))}</b></div>`).join('')}
    <footer><span>Selecții din meciuri diferite · probabilități înmulțite</span></footer></article>`).join('');
}
function renderLive() {
  if (!liveItems.length) return;
  const node = document.querySelector('#live-matches');
  if (liveMode === 'double') {
    node.innerHTML = liveDoubleCards() || '<div class="empty"><strong>Nu există combinații pentru filtrele alese.</strong></div>';
    document.querySelector('#live-filter-note').textContent = 'Două selecții din meciuri diferite; șansa combinată este produsul probabilităților.';
    return;
  }
  const visible = liveItems.filter((item) => filteredLiveMarkets(item).length);
  node.innerHTML = visible.length ? visible.map(liveCard).join('')
    : '<div class="empty"><strong>Nicio piață nu trece filtrele.</strong><span>Redu șansa minimă sau schimbă tipul.</span></div>';
  const valueFilter = document.querySelector('#live-chance').value === 'value';
  document.querySelector('#live-filter-note').textContent = `${visible.length} din ${liveItems.length} meciuri live${valueFilter ? ' · șansă ≥58% · cotă minimă ≥1.35' : ''}`;
}
async function refreshLive() {
  const button = document.querySelector('#refresh-live');
  const node = document.querySelector('#live-matches');
  button.disabled = true; button.textContent = 'Se actualizează…';
  node.innerHTML = '<div class="loader"><i></i><p>Cer datele live…</p></div>';
  try {
    const response = await fetch('/core/api/live?sport=football&refresh=true');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'API live indisponibil');
    liveItems = data.matches || [];
    if (liveItems.length) renderLive();
    else node.innerHTML = '<div class="empty"><strong>Nu există meciuri live acum.</strong><span>Poți actualiza din nou mai târziu.</span></div>';
    document.querySelector('#live-updated').textContent = `${data.count} meciuri · ultima cerere: ${clockTime(data.updated_at, {second: '2-digit'})}`;
  } catch (error) {
    node.innerHTML = `<div class="empty error"><strong>Actualizarea a eșuat.</strong><span>${esc(error.message)}</span></div>`;
  } finally { button.disabled = false; button.textContent = 'Actualizează live'; }
}

// ------------------------------------------------------------------ stare model și evenimente

let modelLoading = false;
async function health() {
  try {
    const response = await fetch('/api/health'); const data = await response.json();
    if (data.status === 'loading') {
      modelLoading = true;
      statusNode.textContent = 'Modelul de fotbal se antrenează…';
      setTimeout(health, 15000);
      return;
    }
    statusNode.textContent = `Model activ · ${data.teams} echipe · date până la ${data.trained_through || '—'}`;
    statusNode.classList.add('ready');
    if (modelLoading) { modelLoading = false; loadDay(); }
  } catch (_) { statusNode.textContent = 'Model indisponibil'; }
}
function activate(selector, button) {
  document.querySelectorAll(selector).forEach((node) => node.classList.toggle('active', node === button));
}
document.querySelector('#previous-day').addEventListener('click', () => { selectedDay = shifted(selectedDay, -1); loadDay(); });
document.querySelector('#next-day').addEventListener('click', () => { selectedDay = shifted(selectedDay, 1); loadDay(); });
picker.addEventListener('change', () => { if (picker.value) { selectedDay = picker.value; loadDay(); } });
document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => {
  const live = button.dataset.view === 'live';
  activate('[data-view]', button);
  document.querySelectorAll('.daily-view').forEach((node) => node.classList.toggle('hidden', live));
  document.querySelector('#live-panel').classList.toggle('hidden', !live);
}));
document.querySelector('#refresh-live').addEventListener('click', refreshLive);
document.querySelector('#live-matches').addEventListener('click', (event) => {
  const button = event.target.closest('[data-live-detail]');
  if (button) liveDetail(button.dataset.liveDetail, button);
});
document.querySelectorAll('[data-live-mode]').forEach((button) => button.addEventListener('click', () => {
  liveMode = button.dataset.liveMode; activate('[data-live-mode]', button); renderLive();
}));
for (const id of ['#live-market', '#live-chance', '#live-sort']) {
  document.querySelector(id).addEventListener('change', renderLive);
}
document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', () => {
  matchStatus = button.dataset.status; activate('[data-status]', button); renderBoard();
}));
document.querySelectorAll('[data-daily-mode]').forEach((button) => button.addEventListener('click', () => {
  dailyMode = button.dataset.dailyMode; activate('[data-daily-mode]', button); renderBoard();
}));
document.querySelectorAll('[data-source]').forEach((button) => button.addEventListener('click', () => {
  sourceMode = button.dataset.source; activate('[data-source]', button); renderBoard();
}));
document.querySelectorAll('[data-chance]').forEach((button) => button.addEventListener('click', () => {
  valueMode = button.dataset.chance === 'value';
  minimumChance = valueMode ? 0 : Number(button.dataset.chance);
  activate('[data-chance]', button); renderBoard();
}));
document.querySelectorAll('[data-market]').forEach((button) => button.addEventListener('click', () => {
  marketType = button.dataset.market; activate('[data-market]', button); renderBoard();
}));
health(); loadDay();
