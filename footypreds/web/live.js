'use strict';
// Live (#/live): in-play probabilities for the chosen sports, refreshed every 30 s (pausable),
// with a detail drawer (GET /api/live/{id}). Prices are pre-match: the UI shows fair odds and
// the minimum odds worth taking, never a bookmaker price.

const LIVE_REFRESH = 30;
const liveState = {paused: remember('live.paused', false), items: {}, notes: {}};

const PERIODS = {
  '1H': 'Repriza 1', '2H': 'Repriza 2', HT: 'Pauză', ET: 'Prelungiri', PEN: 'Penalty', BREAK: 'Pauză',
  INT: 'Întrerupt', OT: 'Prelungiri', Q1: 'Sfertul 1', Q2: 'Sfertul 2', Q3: 'Sfertul 3', Q4: 'Sfertul 4',
  S1: 'Setul 1', S2: 'Setul 2', S3: 'Setul 3', S4: 'Setul 4', S5: 'Setul 5',
};

function periodLabel(item) {
  const period = item.period || item.match?.live?.period || '';
  if ((item.sport || item.match?.sport) === 'football' && isNum(item.minute) && !['HT', 'PEN', 'BREAK'].includes(period)) return `${item.clock || item.minute}'`;
  return PERIODS[period] || item.stage || item.clock || 'Live';
}

function renderLive() {
  const scope = currentScope;
  const app = $('#app');
  const sports = chosenSports();
  liveState.items = {};
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow"><span class="live-dot" aria-hidden="true"></span>Live · ${esc(sports.map(s => SPORT_LABEL[s]).join(', '))}</p>
        <h1>Ce se poate juca acum</h1>
        <p class="lead">Probabilitățile se recalculează după scor și timpul rămas. „Cotă minimă” este prețul de la care un pariu ar merita: compar-o cu cota live din agenția ta.</p>
      </div>
      <div class="hero-controls live-controls">
        <span id="live-countdown" class="countdown" aria-live="off"></span>
        <button id="live-pause" class="btn btn-ghost" type="button" aria-pressed="${liveState.paused}">${icon(liveState.paused ? 'play' : 'pause')}${liveState.paused ? 'Reia actualizarea' : 'Pauză'}</button>
        <button id="live-now" class="btn btn-secondary" type="button">${icon('refresh')}Actualizează acum</button>
      </div>
    </section>
    <div id="live-note"></div>
    <div id="live-sections">${sports.map(sport => `<section class="section" id="live-${sport}" aria-labelledby="lh-${sport}">
        <div class="section-head"><h2 id="lh-${sport}">${sportTag(sport)}</h2><span class="muted small" id="lc-${sport}"></span></div>
        <div class="live-grid">${skeletonCards(3)}</div>
      </section>`).join('')}</div>
    ${disclaimerBox()}`;
  let remaining = LIVE_REFRESH;
  const countdown = () => {
    const el = $('#live-countdown');
    if (el) el.textContent = liveState.paused ? 'Actualizare automată oprită' : `Actualizare în ${remaining} s`;
  };
  const refresh = () => {
    remaining = LIVE_REFRESH;
    countdown();
    sports.forEach(sport => loadLiveSport(scope, sport));
  };
  const timer = setInterval(() => {
    if (liveState.paused || document.hidden) return;
    remaining -= 1;
    if (remaining <= 0) refresh();
    else countdown();
  }, 1000);
  onLeave(() => clearInterval(timer));
  $('#live-pause').addEventListener('click', event => {
    liveState.paused = !liveState.paused;
    persist('live.paused', liveState.paused);
    const button = event.currentTarget;
    button.setAttribute('aria-pressed', String(liveState.paused));
    button.innerHTML = `${icon(liveState.paused ? 'play' : 'pause')}${liveState.paused ? 'Reia actualizarea' : 'Pauză'}`;
    countdown();
  });
  $('#live-now').addEventListener('click', refresh);
  refresh();
}

async function loadLiveSport(scope, sport) {
  const section = $(`#live-${sport}`);
  if (!section) return;
  const grid = $('.live-grid', section);
  let data;
  try {
    data = await api(`/api/live?sport=${sport}`);
  } catch (error) {
    if (!scope.alive) return;
    grid.innerHTML = errorState(error);
    return;
  }
  if (!scope.alive) return;
  liveState.items[sport] = data.matches || [];
  $(`#lc-${sport}`).textContent = `${data.count ?? liveState.items[sport].length} meciuri · actualizat ${fmtTime(data.updated_at || Date.now())}`;
  if (data.odds_note) $('#live-note').innerHTML = `<div class="callout callout-info" role="note"><div class="callout-icon">${icon('live')}</div><div>${esc(data.odds_note)}</div></div>`;
  if (!liveState.items[sport].length) {
    grid.innerHTML = emptyState(`Niciun meci de ${SPORT_LABEL[sport].toLowerCase()} live acum.`, 'Pagina se actualizează automat.');
    return;
  }
  grid.innerHTML = liveState.items[sport].map((item, i) => liveCard(item, sport, i)).join('');
  hydrate(grid);
  $$('[data-live-detail]', grid).forEach(button => button.addEventListener('click', () => {
    const item = liveState.items[sport][Number(button.dataset.liveDetail)];
    if (item) openLiveDetail(item.match.id, sport);
  }));
}

function liveParts(item) {
  const p = item.probabilities || {};
  return item.sport === 'football'
    ? [{label: `1 · ${item.match.home}`, value: p['1']}, {label: 'X', value: p.X}, {label: `2 · ${item.match.away}`, value: p['2']}]
    : [{label: `1 · ${item.match.home}`, value: p['1']}, {label: `2 · ${item.match.away}`, value: p['2']}];
}

function suggestionRow(s) {
  return `<li class="suggestion kind-${esc(s.kind)}">
      <div class="sg-top"><span class="kind">${esc(s.kind === 'sigur' ? 'Sigur' : s.kind === 'echilibrat' ? 'Echilibrat' : s.kind || '')}</span><b>${esc(s.label)}</b><span class="sg-prob">${pct(s.probability)}</span></div>
      <div class="sg-odds"><span>cotă corectă <b>${num(s.fair_odds)}</b></span><span class="min-odds">cotă minimă <b>${num(s.min_odds)}</b></span></div>
      ${s.why ? `<details class="reason"><summary>De ce?</summary><p>${esc(s.why)}</p></details>` : ''}
    </li>`;
}

function liveCard(item, sport, index) {
  const m = item.match;
  const red = m.live?.red_cards || {};
  const redBadge = n => n ? `<span class="red-card" title="${n} ${n === 1 ? 'cartonaș roșu' : 'cartonașe roșii'}">${esc(n)}</span>` : '';
  return `<article class="card live-card sport-${esc(sport)}">
    <div class="lc-top">${crest(m.league_logo, item.competition, 'xs', 'league')}<span class="lc-league">${esc(item.competition || leagueName(m.league))}</span><span class="live-pill">${esc(periodLabel(item))}</span></div>
    <div class="lc-score">
      <div class="lc-team">${crest(m.home_logo, m.home, 'md', sport === 'tennis' ? 'flag' : 'team')}<span class="team-name">${esc(m.home)}</span>${redBadge(red.home)}</div>
      <div class="lc-goals"><b>${esc(item.score?.home ?? m.home_goals ?? 0)}</b><span>–</span><b>${esc(item.score?.away ?? m.away_goals ?? 0)}</b></div>
      <div class="lc-team">${crest(m.away_logo, m.away, 'md', sport === 'tennis' ? 'flag' : 'team')}<span class="team-name">${esc(m.away)}</span>${redBadge(red.away)}</div>
    </div>
    ${outcomeStrip(liveParts(item))}
    ${item.summary ? `<p class="small muted">${esc(item.summary)}</p>` : ''}
    ${item.suggestions?.length ? `<h3 class="mini-title">Sugestii</h3><ul class="suggestions">${item.suggestions.map(suggestionRow).join('')}</ul>` : '<p class="small muted">Nicio sugestie sigură acum (piețele sunt nesigure sau aproape decise).</p>'}
    ${item.notes?.length ? `<details class="notes"><summary>Note (${item.notes.length})</summary><ul>${item.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul></details>` : ''}
    <button class="btn btn-ghost btn-small lc-more" type="button" data-live-detail="${index}">Toate piețele și statistici${icon('chevron')}</button>
  </article>`;
}

async function openLiveDetail(id, sport) {
  const content = openDrawer(loadingBlock('Se încarcă detaliile meciului…'));
  let data;
  try {
    data = await api(`/api/live/${encodeURIComponent(id)}?sport=${sport}`);
  } catch (error) {
    content.innerHTML = errorState(error);
    return;
  }
  const m = data.match;
  const stats = data.stats || {};
  const periods = Object.keys(stats);
  const statNames = {match: 'Meci', '1st-half': 'Repriza 1', '2nd-half': 'Repriza 2'};
  content.innerHTML = `
    <p class="eyebrow">${sportTag(sport)} ${esc(data.competition || leagueName(m.league))}</p>
    <div class="drawer-score">
      <div>${crest(m.home_logo, m.home, 'lg', sport === 'tennis' ? 'flag' : 'team')}<b>${esc(m.home)}</b></div>
      <div class="lc-goals big"><b>${esc(data.score?.home ?? m.home_goals ?? 0)}</b><span>–</span><b>${esc(data.score?.away ?? m.away_goals ?? 0)}</b><small>${esc(periodLabel(data))}</small></div>
      <div>${crest(m.away_logo, m.away, 'lg', sport === 'tennis' ? 'flag' : 'team')}<b>${esc(m.away)}</b></div>
    </div>
    ${outcomeStrip(liveParts({...data, sport}))}
    ${data.summary ? `<p>${esc(data.summary)}</p>` : ''}
    ${data.suggestions?.length ? `<h3 class="mini-title">Sugestii</h3><ul class="suggestions">${data.suggestions.map(suggestionRow).join('')}</ul>` : ''}
    <h3 class="mini-title">Piețe (probabilități în joc)</h3>
    <div class="table-wrap"><table class="table compact"><thead><tr><th>Piață</th><th class="num">Probabilitate</th><th class="num">Cotă corectă</th></tr></thead>
      <tbody>${(data.markets || []).map(x => `<tr${x.reliable === false ? ' class="muted-row"' : ''}><td>${esc(x.label)}${x.reliable === false ? ' <small class="muted">(estimare nesigură)</small>' : ''}</td><td class="num">${pct(x.probability, 1)}</td><td class="num">${num(x.fair_odds)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">Nicio piață deschisă.</td></tr>'}</tbody></table></div>
    ${periods.length ? `<h3 class="mini-title">Statistici</h3>
      <div class="chip-row" role="group" aria-label="Perioadă">${chips(periods.map(p => [p, esc(statNames[p] || p)]), periods[0], 'stat-period')}</div>
      <div id="stats-box"></div>` : '<p class="small muted">Statisticile nu sunt disponibile pentru acest meci.</p>'}
    ${data.pre_match ? `<p class="small muted">Înainte de meci: sursa ${esc({analysis: 'analiza modelului', odds: 'cotele fără marjă', default: 'valori implicite'}[data.pre_match.source] || data.pre_match.source)}${data.pre_match.grade ? `, nota ${esc(data.pre_match.grade)}` : ''}.</p>` : ''}
    ${data.notes?.length ? `<ul class="notes-list">${data.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
    ${disclaimerBox(data.disclaimer)}`;
  const drawStats = period => {
    const box = $('#stats-box', content);
    if (!box) return;
    box.innerHTML = `<div class="stat-rows">${(stats[period] || []).map(s => {
      const h = Number(s.home_value) || 0, a = Number(s.away_value) || 0;
      const share = h + a > 0 ? h / (h + a) : 0.5;
      return `<div class="stat-row"><b>${esc(s.home)}</b><span class="stat-name">${esc(s.name)}</span><b>${esc(s.away)}</b><span class="stat-bar"><span class="sb-home" data-w="${share}"></span></span></div>`;
    }).join('')}</div>`;
    hydrate(box);
  };
  $$('[data-stat-period]', content).forEach(b => b.addEventListener('click', () => {
    $$('[data-stat-period]', content).forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); });
    drawStats(b.dataset.statPeriod);
  }));
  if (periods.length) drawStats(periods[0]);
  hydrate(content);
}
