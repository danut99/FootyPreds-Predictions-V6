'use strict';

const matchesNode = document.querySelector('#matches');
const statusNode = document.querySelector('#status');
const picker = document.querySelector('#date-picker');
const summaryNode = document.querySelector('#summary');
// Orele și zilele se afișează mereu la ora României, indiferent de fusul orar al calculatorului.
const TIME_ZONE = 'Europe/Bucharest';
const dayFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit'
});
let selectedDay = localDay(new Date());
let loadedItems = [];
let minimumChance = 0;
let valueMode = false;
let marketType = 'all';
let liveItems = [];
let liveMode = 'single';
let dailyMode = 'single';
let matchStatus = 'all';
let liveView = false;
let ticketRequest = 0;

function localDay(day) {
  // Ziua calendaristică (YYYY-MM-DD) a momentului dat, la ora României.
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
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}
function percent(value) { return `${Math.round(Number(value || 0) * 100)}%`; }
function prettyDay(iso, options = {}) {
  return new Intl.DateTimeFormat('ro-RO', {...options, timeZone: 'UTC'})
    .format(new Date(`${iso}T12:00:00Z`));
}
function renderDates() {
  const today = localDay(new Date());
  document.querySelector('#date-strip').innerHTML = [-2, -1, 0, 1, 2].map((offset) => {
    const day = shifted(selectedDay, offset);
    const label = day === today ? 'Azi' : prettyDay(day, {weekday: 'short'}).replace('.', '');
    return `<button type="button" class="date-pill ${day === selectedDay ? 'active' : ''}" data-day="${day}">
      <span>${escapeHtml(label)}</span><strong>${prettyDay(day, {day: '2-digit'})}</strong>
      <small>${prettyDay(day, {month: 'short'}).replace('.', '')}</small></button>`;
  }).join('');
  document.querySelectorAll('.date-pill').forEach((button) => button.addEventListener('click', () => {
    selectedDay = button.dataset.day; loadDay();
  }));
  picker.value = selectedDay;
}
function marketKind(market) {
  if (market.key === '1' || market.key === '2') return 'winner';
  if (market.key.startsWith('over_') || market.key.startsWith('under_')) return 'total';
  if (market.key.startsWith('sets_')) return 'score';
  if (market.key.startsWith('games_')) return 'games';
  return 'other';
}
function marketsFor(item) {
  const source = (item.markets || item.main || []).filter((market) => !market.key.startsWith('ah_'));
  const markets = source.filter((market) => marketType === 'all' || marketKind(market) === marketType)
    .sort((left, right) => Number(right.probability || 0) - Number(left.probability || 0));
  if (marketType === 'games' && !markets.length && item.expected?.games) {
    return [{key: 'games_estimate', label: `Estimare total: ${Math.round(item.expected.games)} game-uri`,
      probability: null, odds: null, estimate: true}];
  }
  if (valueMode) return markets.filter(worthwhile);
  return markets;
}
function offeredOdds(market) { return Number(market.odds || market.fair_odds || 0); }
function worthwhile(market) {
  return market.selectable !== false && Number(market.probability) >= 0.58 && offeredOdds(market) >= 1.35;
}
function bestChance(item) {
  const probabilities = marketsFor(item).filter((market) => !market.estimate).map((market) => market.probability);
  return probabilities.length ? Math.max(...probabilities) : 0;
}
function marketRows(item) {
  return marketsFor(item).slice(0, 5).map((market) => `<div class="market-row ${market.estimate ? 'estimate' : ''}">
    <span>${escapeHtml(market.label)}</span><strong>${percent(market.probability)}</strong>
    <small>${market.estimate || market.selectable === false ? 'orientativ' : market.odds ? `cotă ${Number(market.odds).toFixed(2)}` : market.fair_odds ? `corectă ${Number(market.fair_odds).toFixed(2)}` : 'cotă —'}</small></div>`).join('');
}
function resultBadge(item) {
  const market = marketsFor(item)[0];
  if (!item.result) return item.match.status === 'live'
    ? '<span class="verdict live">LIVE</span>' : '<span class="verdict pending">PROGRAMAT</span>';
  if (!market || market.won === null || market.won === undefined) return '<span class="verdict void">NEVERIFICABIL</span>';
  return market.won ? '<span class="verdict won">✓ NIMERIT</span>' : '<span class="verdict lost">× RATAT</span>';
}
function tmlNote(item, shownMarket) {
  // „selectează” apare doar când modelul v3 (amestec model + cote) alege exact jucătorul din
  // recomandarea afișată; „precizie înaltă” marchează pragul strict (țintă 85%).
  const tml = item.tml;
  if (!tml?.known || !tml.pick) return '';
  const chance = tml.pick === '1' ? tml.probability : 1 - tml.probability;
  const pick = `${escapeHtml(tml.pick_name)} ${percent(chance)}`;
  const high = tml.decision_high === 'selectează' ? ' · precizie înaltă' : '';
  if (tml.decision === 'selectează' && shownMarket?.key === tml.pick) {
    return ` · model TML v3: selectează ${pick}${high}`;
  }
  if (tml.decision === 'selectează') return ` · model TML v3: ${pick} (altă selecție decât recomandarea)`;
  return ` · model TML v3: ${pick} · fără pariu${tml.validated === false ? ' (competiție nevalidată)' : ''}`;
}
function matchCard(item) {
  const match = item.match;
  const selectedMarket = marketsFor(item)[0] || item.tip;
  const time = clockTime(match.kickoff);
  const center = item.result ? `final · ${escapeHtml(item.result.score)}` : escapeHtml(time);
  return `<article class="match-card ${selectedMarket.won === true ? 'is-won' : ''} ${selectedMarket.won === false ? 'is-lost' : ''}">
    <header><span>${escapeHtml(item.competition)}</span>${resultBadge(item)}</header>
    <div class="players-card"><div><strong>${escapeHtml(match.home)}</strong><small>Jucător 1</small></div>
      <div class="score"><b>${center}</b><span>VS</span></div>
      <div class="away"><strong>${escapeHtml(match.away)}</strong><small>Jucător 2</small></div></div>
    <div class="markets-title"><span>Posibilități de pariere</span><small>șansă estimată</small></div>
    <div class="markets-list">${marketRows(item)}</div>
    <div class="recommendation"><div><small>RECOMANDAREA MODELULUI</small>
      <strong>${escapeHtml(selectedMarket.label)}</strong></div><b>${percent(selectedMarket.probability)}</b></div>
    <footer><span>Încredere ${escapeHtml(item.grade)} · ${item.confidence}%${tmlNote(item, selectedMarket)}</span>
      <span>${escapeHtml(item.summary)}</span></footer></article>`;
}
function liveMarketKind(market) {
  if (market.key.startsWith('live_set_games_')) return 'set_games_handicap';
  if (market.key.startsWith('live_games_')) return 'games_handicap';
  if (market.key === '1' || market.key === '2') return 'winner';
  if (market.key.startsWith('next_set_')) return 'current';
  if (market.key.startsWith('sets_')) return 'score';
  if (market.key.startsWith('over_') || market.key.startsWith('under_')) return 'total';
  return 'other';
}
function filteredLiveMarkets(item) {
  const type = document.querySelector('#live-market').value;
  const chanceValue = document.querySelector('#live-chance').value;
  const valueFilter = chanceValue === 'value';
  const chance = valueFilter ? 0.58 : Number(chanceValue);
  const direction = document.querySelector('#live-sort').value === 'desc' ? -1 : 1;
  return (item.markets || []).filter((market) => market.reliable !== false)
    .filter((market) => !market.key.startsWith('ah_'))
    .filter((market) => type === 'all' || liveMarketKind(market) === type)
    .filter((market) => Number(market.probability) >= chance)
    .filter((market) => !valueFilter || (market.selectable !== false && Number(market.fair_odds) >= 1.35))
    .sort((left, right) => direction * (Number(left.probability) - Number(right.probability)));
}
function liveCard(item) {
  const match = item.match;
  const markets = filteredLiveMarkets(item);
  const rows = markets.length ? markets.slice(0, 8).map((market) => `<div class="market-row">
    <span>${escapeHtml(market.label)} <em>${escapeHtml(market.group)}</em></span>
    <strong>${percent(market.probability)}</strong><small>minim ${Number(market.fair_odds).toFixed(2)}</small>
  </div>`).join('') : '<div class="no-suggestion">Nicio piață nu trece filtrele.</div>';
  return `<article class="match-card live-card"><header><span>${escapeHtml(item.competition)}</span>
    <span class="verdict live">${escapeHtml(item.stage || 'LIVE')}</span></header>
    <div class="players-card"><div><strong>${escapeHtml(match.home)}</strong><small>Jucător 1</small></div>
      <div class="score"><b>${item.score.home}–${item.score.away}</b><span>SETURI</span></div>
      <div class="away"><strong>${escapeHtml(match.away)}</strong><small>Jucător 2</small></div></div>
    <div class="markets-title"><span>Opțiuni live</span><small>cotă minimă acceptabilă</small></div>
    <div class="markets-list">${rows}</div><div class="live-summary">${escapeHtml(item.summary)}</div>
    <footer><span>Cotele afișate sunt cote corecte, nu cote live.</span>
      <button type="button" class="analyze-games" data-live-detail="${escapeHtml(match.id)}">
        ${item.stats ? 'Game-uri analizate' : 'Analizează game-urile'}</button></footer></article>`;
}
async function analyzeLiveGames(matchId, button) {
  button.disabled = true; button.textContent = 'Analizez…';
  try {
    const response = await fetch(`/core/api/live/${encodeURIComponent(matchId)}?sport=tennis&refresh=true`);
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.detail || 'Statisticile nu sunt disponibile');
    const position = liveItems.findIndex((item) => item.match.id === matchId);
    if (position >= 0) liveItems[position] = detail;
    renderLive();
  } catch (error) {
    button.disabled = false; button.textContent = 'Reîncearcă analiza';
    document.querySelector('#live-filter-note').textContent = error.message;
  }
}
function doubleCards() {
  const direction = document.querySelector('#live-sort').value === 'desc' ? -1 : 1;
  const candidates = liveItems.map((item) => ({item, markets: filteredLiveMarkets(item)
    .filter((market) => market.probability <= 0.82 && market.fair_odds >= 1.25).slice(0, 4)}))
    .filter((entry) => entry.markets.length);
  const doubles = [];
  for (let first = 0; first < candidates.length; first += 1) {
    for (let second = first + 1; second < candidates.length; second += 1) {
      for (const marketA of candidates[first].markets) for (const marketB of candidates[second].markets) {
        const a = {item: candidates[first].item, market: marketA};
        const b = {item: candidates[second].item, market: marketB};
        doubles.push({a, b, probability: marketA.probability * marketB.probability,
          odds: marketA.fair_odds * marketB.fair_odds});
      }
    }
  }
  doubles.sort((left, right) => direction * (left.probability - right.probability));
  return doubles.slice(0, 24).map((combo) => `<article class="match-card combo-card">
    <header><span>COMBINAȚIE · 2 MECIURI</span><span class="verdict pending">COTĂ MIN. ${combo.odds.toFixed(2)}</span></header>
    <div class="combo-probability"><strong>${percent(combo.probability)}</strong><span>șansă combinată</span></div>
    ${[combo.a, combo.b].map((leg, index) => `<div class="combo-leg"><i>${index + 1}</i><div>
      <small>${escapeHtml(leg.item.match.home)} – ${escapeHtml(leg.item.match.away)}</small>
      <strong>${escapeHtml(leg.market.label)}</strong></div><b>${percent(leg.market.probability)}</b></div>`).join('')}
    <footer><span>Selecții din meciuri diferite · probabilități înmulțite</span></footer></article>`).join('');
}
function dailyDoubleCards(items) {
  const comboMinimum = valueMode ? 0.58 : (minimumChance || 0.55);
  const comboMaximum = minimumChance ? 0.97 : 0.82;
  const comboMinimumOdds = valueMode ? 1.35 : (minimumChance ? 1.03 : 1.25);
  const candidates = items.map((item) => ({item, markets: marketsFor(item)
    .filter((market) => market.selectable !== false && market.probability >= comboMinimum
      && market.probability <= comboMaximum && offeredOdds(market) >= comboMinimumOdds).slice(0, 4)}))
    .filter((entry) => entry.markets.length);
  const doubles = [];
  for (let first = 0; first < candidates.length; first += 1) {
    for (let second = first + 1; second < candidates.length; second += 1) {
      for (const marketA of candidates[first].markets) for (const marketB of candidates[second].markets) {
        doubles.push({a: {item: candidates[first].item, market: marketA},
          b: {item: candidates[second].item, market: marketB},
          probability: marketA.probability * marketB.probability,
          odds: offeredOdds(marketA) * offeredOdds(marketB)});
      }
    }
  }
  doubles.sort((left, right) => right.probability - left.probability || right.odds - left.odds);
  const selected = doubles.slice(0, 30);
  const verified = selected.filter((combo) => {
    const results = [combo.a.market.won, combo.b.market.won];
    return results.every((won) => won === true) || results.some((won) => won === false);
  });
  const won = verified.filter((combo) => combo.a.market.won === true && combo.b.market.won === true).length;
  if (verified.length) {
    const unresolved = selected.length - verified.length;
    summaryNode.innerHTML = `<div><strong>${won}/${verified.length}</strong><span>nimerite / verificate</span></div>
      <div><strong>${percent(won / verified.length)}</strong><span>precizie verificată</span></div>
      <div><strong>${unresolved}</strong><span>neverificate · ${selected.length} total</span></div>`;
    summaryNode.classList.remove('hidden');
  } else {
    summaryNode.classList.add('hidden');
  }
  return selected.map((combo) => {
    const legs = [combo.a, combo.b];
    const settled = legs.map((leg) => leg.market.won);
    const allFinished = legs.every((leg) => leg.item.match.status === 'finished');
    const verdict = settled.every((won) => won === true) ? '<span class="verdict won">✓ NIMERITĂ</span>'
      : settled.some((won) => won === false) ? '<span class="verdict lost">× RATATĂ</span>'
        : allFinished ? '<span class="verdict void">NEVERIFICABILĂ</span>'
          : '<span class="verdict pending">PROGRAMATĂ</span>';
    return `<article class="match-card combo-card"><header><span>COMBINAȚIE · 2 MECIURI</span>${verdict}</header>
      <div class="combo-probability"><strong>${percent(combo.probability)}</strong>
        <span>șansă combinată · cotă ${combo.odds.toFixed(2)}</span></div>
      ${[combo.a, combo.b].map((leg, index) => `<div class="combo-leg"><i>${index + 1}</i><div>
        <small>${escapeHtml(leg.item.match.home)} – ${escapeHtml(leg.item.match.away)}</small>
        <strong>${escapeHtml(leg.market.label)}</strong></div><b>${percent(leg.market.probability)}</b></div>`).join('')}
      <footer><span>Selecțiile provin obligatoriu din meciuri diferite.</span></footer></article>`;
  }).join('');
}
function ticketChance(value) {
  const probability = Number(value || 0);
  return probability < 0.1 ? `${(probability * 100).toFixed(1)}%` : percent(probability);
}
function ticketLegs() {
  // Pe zilele cu meciuri viitoare intră doar acestea; pe o zi încheiată biletul este retroactiv
  // (arată ce ar fi ales generatorul și dacă ar fi ieșit). Filtrele de piață și șansă se aplică.
  const now = Date.now();
  const upcoming = loadedItems.filter((item) => item.match.status === 'scheduled'
    && new Date(item.match.kickoff).getTime() > now);
  const retro = !upcoming.length;
  const pool = retro ? loadedItems.filter((item) => item.match.status === 'finished') : upcoming;
  const legs = [];
  for (const item of pool) {
    for (const market of marketsFor(item)) {
      const odds = Number(market.odds || 0);
      const probability = Number(market.probability || 0);
      if (market.estimate || market.selectable === false || !(odds > 1)) continue;
      if (!(probability > 0) || probability < minimumChance) continue;
      legs.push({match_id: String(item.match.id), key: String(market.key).slice(0, 60),
        label: String(market.label || market.key).slice(0, 160), group: String(market.group || '').slice(0, 80),
        probability: Math.min(1, probability), odds, kickoff: String(item.match.kickoff || '').slice(0, 40),
        home: String(item.match.home || '').slice(0, 100), away: String(item.match.away || '').slice(0, 100),
        competition: String(item.competition || '').slice(0, 200),
        won: typeof market.won === 'boolean' ? market.won : null});
    }
  }
  return {legs: legs.slice(0, 3000), retro};
}
function ticketVerdict(ticket, retro) {
  if (ticket.status === 'won') return '<span class="verdict won">✓ CÂȘTIGAT</span>';
  if (ticket.status === 'lost') return '<span class="verdict lost">× PIERDUT</span>';
  if (retro) return '<span class="verdict void">NEVERIFICABIL</span>';
  return '<span class="verdict pending">DE JUCAT</span>';
}
function ticketCard(ticket, title, retro) {
  const count = ticket.legs.length;
  const legs = ticket.legs.map((leg, index) => `<div class="combo-leg"><i>${index + 1}</i><div>
      <small>${escapeHtml(leg.home)} – ${escapeHtml(leg.away)} · ${escapeHtml(clockTime(leg.kickoff))}</small>
      <strong>${escapeHtml(leg.label)}</strong><em>${escapeHtml(leg.competition)}</em></div>
      <b>${Number(leg.odds).toFixed(2)}<small>${percent(leg.probability)}${leg.won === true ? ' · ✓' : leg.won === false ? ' · ×' : ''}</small></b></div>`).join('');
  return `<article class="match-card combo-card ticket-card ${ticket.status === 'won' ? 'is-won' : ''} ${ticket.status === 'lost' ? 'is-lost' : ''}">
    <header><span>${escapeHtml(title)} · ${count} ${count === 1 ? 'SELECȚIE' : 'SELECȚII'}</span>${ticketVerdict(ticket, retro)}</header>
    <div class="combo-probability"><strong>${Number(ticket.total_odds).toFixed(2)}</strong>
      <span>cotă totală · șansă estimată ${escapeHtml(ticketChance(ticket.probability))}</span></div>
    ${legs}
    <footer><span>Cotă minimă cerută ${escapeHtml(Number(ticket.min_odds).toFixed(2))} · o selecție pe meci · probabilități înmulțite</span></footer></article>`;
}
async function generateTicket() {
  const request = ++ticketRequest;
  const minOdds = Number(document.querySelector('#ticket-odds').value);
  if (!(minOdds >= 1.1 && minOdds <= 1000)) {
    matchesNode.innerHTML = '<div class="empty error"><strong>Cota minimă trebuie să fie între 1.10 și 1000.</strong></div>';
    return;
  }
  const {legs, retro} = ticketLegs();
  const maxLegs = document.querySelector('#ticket-legs').value;
  document.querySelector('#filter-note').textContent = `${legs.length} selecții posibile din ${new Set(legs.map((leg) => leg.match_id)).size} meciuri${retro ? ' încheiate · bilet retroactiv' : ' care nu au început'}${minimumChance ? ` · fiecare ≥ ${percent(minimumChance)}` : ''}`;
  matchesNode.innerHTML = '<div class="loader"><i></i><p>Caut cel mai sigur bilet…</p></div>';
  try {
    const response = await fetch('/api/ticket', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({min_odds: minOdds, max_legs: maxLegs ? Number(maxLegs) : null, legs})});
    const data = await response.json();
    if (request !== ticketRequest || dailyMode !== 'ticket') return;
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Cererea biletului a eșuat');
    if (!data.ticket) {
      matchesNode.innerHTML = `<div class="empty"><strong>Nu am găsit un bilet cu cota minimă ${escapeHtml(minOdds.toFixed(2))}.</strong><span>${escapeHtml(data.reason)}</span></div>`;
      return;
    }
    const cards = [ticketCard(data.ticket, 'BILETUL CEL MAI SIGUR', retro),
      ...data.alternatives.map((ticket, index) => ticketCard(ticket, `ALTERNATIVĂ ${index + 1}`, retro))];
    matchesNode.innerHTML = cards.join('') + `<p class="filter-note">${escapeHtml(data.assumption)} ${escapeHtml(data.disclaimer)}</p>`;
  } catch (error) {
    if (request !== ticketRequest) return;
    matchesNode.innerHTML = `<div class="empty error"><strong>Nu am putut genera biletul.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}
function syncTicketControls() {
  document.querySelector('#ticket-controls').classList.toggle('hidden', liveView || dailyMode !== 'ticket');
}
function renderLive() {
  if (!liveItems.length) return;
  const node = document.querySelector('#live-matches');
  if (liveMode === 'double') {
    const cards = doubleCards();
    node.innerHTML = cards || '<div class="empty"><strong>Nu există combinații pentru filtrele alese.</strong></div>';
    document.querySelector('#live-filter-note').textContent = 'Două selecții din meciuri diferite; șansa combinată este produsul probabilităților.';
  } else {
    const visible = liveItems.filter((item) => filteredLiveMarkets(item).length);
    node.innerHTML = visible.length ? visible.map(liveCard).join('')
      : '<div class="empty"><strong>Nicio piață nu trece filtrele.</strong><span>Redu șansa minimă sau schimbă tipul.</span></div>';
    const valueFilter = document.querySelector('#live-chance').value === 'value';
    document.querySelector('#live-filter-note').textContent = `${visible.length} din ${liveItems.length} meciuri live${valueFilter ? ' · șansă ≥58% · cotă minimă ≥1.35' : ''}`;
  }
}
async function refreshLive() {
  const button = document.querySelector('#refresh-live');
  const node = document.querySelector('#live-matches');
  button.disabled = true; button.textContent = 'Se actualizează…';
  node.innerHTML = '<div class="loader"><i></i><p>Cer datele live…</p></div>';
  try {
    const response = await fetch('/core/api/live?sport=tennis&refresh=true');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'API live indisponibil');
    liveItems = data.matches;
    if (liveItems.length) renderLive();
    else node.innerHTML = '<div class="empty"><strong>Nu există meciuri live acum.</strong><span>Poți actualiza din nou mai târziu.</span></div>';
    const time = clockTime(data.updated_at, {second: '2-digit'});
    document.querySelector('#live-updated').textContent = `${data.count} meciuri · ultima cerere: ${time}`;
  } catch (error) {
    node.innerHTML = `<div class="empty error"><strong>Actualizarea a eșuat.</strong><span>${escapeHtml(error.message)}</span></div>`;
  } finally { button.disabled = false; button.textContent = 'Actualizează live'; }
}
function renderSummary(items) {
  const settled = items.map((item) => marketsFor(item)[0]).filter((market) => market && typeof market.won === 'boolean');
  if (!settled.length) { summaryNode.classList.add('hidden'); return; }
  const won = settled.filter((market) => market.won).length;
  summaryNode.innerHTML = `<div><strong>${won}/${settled.length}</strong><span>predicții nimerite</span></div>
    <div><strong>${percent(won / settled.length)}</strong><span>precizia zilei</span></div>
    <div><strong>${items.length}</strong><span>meciuri analizate</span></div>`;
  summaryNode.classList.remove('hidden');
}
function categoryStats() {
  document.querySelectorAll('[data-market]').forEach((button) => {
    const category = button.dataset.market;
    if (category === 'all') { button.textContent = button.dataset.label; return; }
    const settled = loadedItems.map((item) => {
      const candidates = (item.markets || item.main || []).filter((market) => marketKind(market) === category)
        .sort((left, right) => Number(right.probability) - Number(left.probability));
      return candidates[0];
    }).filter((market) => market && typeof market.won === 'boolean');
    const won = settled.filter((market) => market.won).length;
    const rate = settled.length ? Math.round(won / settled.length * 100) : null;
    button.textContent = `${button.dataset.label}${settled.length ? ` · ${won}/${settled.length} · ${rate}%` : ' · neverificabil'}`;
  });
}
function renderBoard() {
  const byStatus = loadedItems.filter((item) => matchStatus === 'all'
    || (matchStatus === 'upcoming' && item.match.status === 'scheduled')
    || (matchStatus === 'finished' && item.match.status === 'finished'));
  const visible = byStatus.filter((item) => marketsFor(item).length && bestChance(item) >= minimumChance)
    .sort((left, right) => bestChance(right) - bestChance(left));
  const marketLabels = {all: 'toate piețele', winner: 'câștigător', total: 'total seturi',
    score: 'scor seturi', games: 'estimări game-uri'};
  const statusLabels = {all: 'toate stările', upcoming: 'nu au început', finished: 'încheiate'};
  document.querySelector('#filter-note').textContent = `${visible.length} din ${loadedItems.length} meciuri · ${statusLabels[matchStatus]} · ${marketLabels[marketType]}${valueMode ? ' · probabilitate ≥58% și cotă reală/corectă ≥1.35' : minimumChance ? ` · minimum ${percent(minimumChance)}` : ''}`;
  if (dailyMode === 'ticket') {
    summaryNode.classList.add('hidden');
    generateTicket();
  } else if (dailyMode === 'double') {
    summaryNode.classList.add('hidden');
    const cards = dailyDoubleCards(visible);
    matchesNode.innerHTML = cards || '<div class="empty"><strong>Nu există combinații pentru filtrele alese.</strong></div>';
  } else {
    renderSummary(visible);
    matchesNode.innerHTML = visible.length ? visible.map(matchCard).join('')
      : '<div class="empty"><strong>Niciun meci nu trece filtrul ales.</strong><span>Redu pragul sau alege alt tip de pariu.</span></div>';
  }
}
async function loadDay() {
  renderDates();
  matchesNode.innerHTML = '<div class="loader"><i></i><p>Analizez meciurile zilei…</p></div>';
  document.querySelector('#board-title').textContent = prettyDay(selectedDay, {weekday: 'long', day: 'numeric', month: 'long'});
  summaryNode.classList.add('hidden');
  try {
    const response = await fetch(`/core/api/predictions?day=${selectedDay}&sport=tennis&limit=400`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'API indisponibil');
    document.querySelector('#day-label').textContent = `${data.total} MECIURI · ATP & WTA`;
    loadedItems = data.items;
    categoryStats();
    renderBoard();
    const items = loadedItems;
    if (await enhanceWithTml(items) && items === loadedItems) {
      categoryStats();
      renderBoard();
    }
  } catch (error) {
    matchesNode.innerHTML = `<div class="empty error"><strong>Nu am putut încărca meciurile.</strong><span>${escapeHtml(error.message)}</span></div>`;
  }
}
function marketOdds(item) {
  // Cotele reale 1/2 (ca two_way din footypreds); fără cote -> null. Serverul scoate marja cu
  // metoda validată. Nu se trimite niciodată probabilitatea modelului de bază: nu este un preț.
  const markets = item.markets || item.main || [];
  const first = Number(markets.find((market) => market.key === '1')?.odds || 0);
  const second = Number(markets.find((market) => market.key === '2')?.odds || 0);
  if (!(first > 1 && second > 1)) return null;
  const total = 1 / first + 1 / second;
  if (total < 0.97 || total > 1.35) return null;
  return {odds_1: first, odds_2: second, market_probability: (1 / first) / total};
}
async function enhanceWithTml(items) {
  const payload = items.map((item) => {
    const league = String(item.match.league || '').toLowerCase();
    const surface = ['clay', 'grass', 'carpet'].find((name) => league.includes(name)) || 'hard';
    const kickoff = new Date(item.match.kickoff);
    return {id: item.match.id, home: item.match.home, away: item.match.away,
      surface: surface[0].toUpperCase() + surface.slice(1), league: String(item.match.league || '').slice(0, 200),
      ...(marketOdds(item) || {}),
      day: Number.isNaN(kickoff.getTime()) ? null : localDay(kickoff)};
  });
  try {
    const response = await fetch('/api/tml-probabilities', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    if (!response.ok) return false;
    const results = new Map((await response.json()).matches.map((match) => [match.id, match]));
    for (const item of items) {
      const result = results.get(item.match.id);
      item.tml = result;
      if (!result?.known || result.probability === null) continue;
      for (const market of item.markets || []) {
        if (market.key === '1') market.probability = result.probability;
        if (market.key === '2') market.probability = 1 - result.probability;
        if (market.key === '1' || market.key === '2') market.fair_odds = 1 / market.probability;
      }
      if (item.tip.key === '1') item.tip.probability = result.probability;
      if (item.tip.key === '2') item.tip.probability = 1 - result.probability;
    }
    return true;
  } catch (_) { return false; /* Modelul de bază rămâne disponibil dacă stratul TML eșuează. */ }
}
let tmlLoading = false;
async function health() {
  try {
    const response = await fetch('/api/health'); const data = await response.json();
    if (data.status === 'loading') {
      tmlLoading = true;
      statusNode.textContent = 'Modelul v3 se antrenează…';
      setTimeout(health, 15000);
      return;
    }
    statusNode.textContent = `Model activ · ${data.players} jucători`; statusNode.classList.add('ready');
    if (tmlLoading) { tmlLoading = false; loadDay(); }
  } catch (_) { statusNode.textContent = 'Model indisponibil'; }
}
document.querySelector('#previous-day').addEventListener('click', () => { selectedDay = shifted(selectedDay, -1); loadDay(); });
document.querySelector('#next-day').addEventListener('click', () => { selectedDay = shifted(selectedDay, 1); loadDay(); });
picker.addEventListener('change', () => { if (picker.value) { selectedDay = picker.value; loadDay(); } });
document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => {
  const live = button.dataset.view === 'live';
  liveView = live;
  document.querySelectorAll('[data-view]').forEach((node) => node.classList.toggle('active', node === button));
  document.querySelectorAll('.daily-view').forEach((node) => node.classList.toggle('hidden', live));
  document.querySelector('#live-panel').classList.toggle('hidden', !live);
  syncTicketControls();
}));
document.querySelector('#ticket-generate').addEventListener('click', generateTicket);
document.querySelector('#ticket-odds').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') generateTicket();
});
document.querySelector('#ticket-odds').addEventListener('input', () => {
  const value = Number(document.querySelector('#ticket-odds').value);
  document.querySelectorAll('[data-ticket-odds]').forEach((node) => node.classList.toggle(
    'active', Number(node.dataset.ticketOdds) === value));
});
document.querySelectorAll('[data-ticket-odds]').forEach((button) => button.addEventListener('click', () => {
  document.querySelector('#ticket-odds').value = button.dataset.ticketOdds;
  document.querySelectorAll('[data-ticket-odds]').forEach((node) => node.classList.toggle('active', node === button));
  generateTicket();
}));
document.querySelector('#ticket-legs').addEventListener('change', generateTicket);
document.querySelector('#refresh-live').addEventListener('click', refreshLive);
document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', () => {
  matchStatus = button.dataset.status;
  document.querySelectorAll('[data-status]').forEach((node) => node.classList.toggle('active', node === button));
  renderBoard();
}));
document.querySelector('#live-matches').addEventListener('click', (event) => {
  const button = event.target.closest('[data-live-detail]');
  if (button) analyzeLiveGames(button.dataset.liveDetail, button);
});
document.querySelectorAll('[data-daily-mode]').forEach((button) => button.addEventListener('click', () => {
  dailyMode = button.dataset.dailyMode;
  document.querySelectorAll('[data-daily-mode]').forEach((node) => node.classList.toggle('active', node === button));
  syncTicketControls();
  renderBoard();
}));
document.querySelectorAll('[data-live-mode]').forEach((button) => button.addEventListener('click', () => {
  liveMode = button.dataset.liveMode;
  document.querySelectorAll('[data-live-mode]').forEach((node) => node.classList.toggle('active', node === button));
  renderLive();
}));
for (const id of ['#live-market', '#live-chance', '#live-sort']) {
  document.querySelector(id).addEventListener('change', renderLive);
}
document.querySelectorAll('[data-chance]').forEach((button) => button.addEventListener('click', () => {
  valueMode = button.dataset.chance === 'value';
  minimumChance = valueMode ? 0 : Number(button.dataset.chance);
  document.querySelectorAll('[data-chance]').forEach((node) => node.classList.toggle('active', node === button));
  renderBoard();
}));
document.querySelectorAll('[data-market]').forEach((button) => button.addEventListener('click', () => {
  marketType = button.dataset.market;
  valueMode = false;
  minimumChance = 0;
  document.querySelectorAll('[data-chance]').forEach((node, index) => node.classList.toggle('active', index === 0));
  document.querySelectorAll('[data-market]').forEach((node) => node.classList.toggle('active', node === button));
  renderBoard();
}));
health(); loadDay();
