'use strict';
// Match detail (#/meci/{id}?sport=): sport-specific analysis. GET /api/analysis first (local,
// instant), then for an upcoming match POST /api/analyze with enrich (H2H, standings, prices).

async function renderMatch(id, sport) {
  const scope = currentScope;
  const app = $('#app');
  const query = sport ? `?sport=${encodeURIComponent(sport)}` : '';
  app.innerHTML = `${backLink()}<section class="card match-hero skeleton-hero" aria-busy="true"><div class="sk sk-line w40"></div><div class="sk sk-block"></div><div class="sk sk-line w80"></div></section><div class="grid grid-2">${skeletonCards(2)}</div>`;
  let data;
  try {
    if (id.startsWith('demo-')) {
      const demo = await api('/api/demo');
      data = demo.analyses.find(a => a.match.id === id);
      if (!data) throw new Error('Meciul demo nu există.');
    } else {
      data = await api(`/api/analysis/${encodeURIComponent(id)}${query}`);
    }
  } catch (error) {
    if (!scope.alive) return;
    app.innerHTML = `${backLink()}${errorState(error, 'match-retry')}`;
    $('#match-retry')?.addEventListener('click', () => renderMatch(id, sport));
    return;
  }
  if (!scope.alive) return;
  const upcoming = isUpcoming(data.match) && !id.startsWith('demo-');
  drawMatch(data, upcoming);
  if (upcoming) enrichMatch(scope, id, query, false);
}

const backLink = () => `<a class="back" href="#/meciuri">${icon('chevron', 'flip')}Înapoi la meciuri</a>`;
const isUpcoming = m => m.status === 'scheduled' && new Date(m.kickoff) > new Date();

async function enrichMatch(scope, id, query, refresh) {
  const line = $('#enrich-line');
  if (line) line.innerHTML = `<span class="spinner small" aria-hidden="true"></span> Aducem forma, meciurile directe și toate cotele din FlashScore…`;
  try {
    const data = await post(`/api/analyze/${encodeURIComponent(id)}${query}`, {enrich: true, refresh});
    if (!scope.alive) return;
    drawMatch(data, false);
    if (refresh) toast('Analiza a fost reîmprospătată.', 'success');
  } catch (error) {
    if (!scope.alive) return;
    const el = $('#enrich-line');
    if (el) el.textContent = `Analiza completă nu a putut fi încărcată: ${error.message}`;
  }
}

function drawMatch(data, pending) {
  const app = $('#app');
  const m = data.match;
  const a = data.prediction;
  const sport = a.sport || m.sport || 'football';
  const markets = Object.fromEntries((a.markets || []).map(x => [x.key, x]));
  const groups = [...new Set((a.markets || []).map(x => x.group))];
  const firstGroup = groups[0] || 'all';
  const body = sport === 'basketball' ? basketballBody(m, a, markets)
    : sport === 'tennis' ? tennisBody(m, a, markets)
      : footballBody(m, a, markets, data.standings);
  app.innerHTML = `${backLink()}
    ${matchHero(m, a, data, pending)}
    ${warningsBox(data.warnings, 'Observații')}
    ${body}
    <section class="section">
      <div class="section-head"><div><h2>Toate piețele</h2><p class="muted small">Cotă corectă = 1 / probabilitate · EV = probabilitate × cotă − 1 · piețele fără preț real nu intră în bilete.</p></div></div>
      <div class="chip-row market-groups" role="group" aria-label="Grupuri de piețe">${chips([...groups.map(g => [g, esc(g)]), ['all', `Toate (${(a.markets || []).length})`]], firstGroup, 'mgroup')}</div>
      <div class="card table-card" id="markets-table">${marketsTable((a.markets || []).filter(x => firstGroup === 'all' || x.group === firstGroup))}</div>
    </section>
    ${qualityPanel(m, a, sport)}
    ${disclaimerBox()}`;
  hydrate(app);
  $$('[data-mgroup]', app).forEach(button => button.addEventListener('click', () => {
    $$('[data-mgroup]', app).forEach(b => { b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', String(b === button)); });
    const group = button.dataset.mgroup;
    $('#markets-table').innerHTML = marketsTable((a.markets || []).filter(x => group === 'all' || x.group === group));
    hydrate($('#markets-table'));
  }));
  if (sport === 'basketball') {
    const e = a.expected || {};
    const totalLine = headlineLine(a.markets || [], /^over_(\d+(?:\.\d+)?)$/) ?? e.total;
    densityChart($('#margin-chart'), {mean: e.margin, sd: e.margin_sd, split: 0, lowLabel: `${m.away} câștigă`, highLabel: `${m.home} câștigă`, label: 'Diferența de scor (gazde − oaspeți)'});
    densityChart($('#total-chart'), {mean: e.total, sd: e.total_sd, split: totalLine, lowLabel: `Sub ${num(totalLine, 1)}`, highLabel: `Peste ${num(totalLine, 1)}`, label: 'Total puncte'});
  }
  $('#refresh-analysis')?.addEventListener('click', async () => {
    const ok = await confirmDialog({title: 'Reîmprospătezi analiza?', text: 'Reîncărcăm din FlashScore meciurile directe, clasamentul și cotele (câteva cereri din cota API).', ok: 'Reîmprospătează'});
    if (ok) enrichMatch(currentScope, m.id, `?sport=${encodeURIComponent(sport)}`, true);
  });
}

function matchHero(m, a, data, pending) {
  const sport = a.sport || m.sport || 'football';
  const shown = m.status === 'finished' || m.status === 'live';
  const centre = shown ? `<div class="mh-score">${esc(m.home_goals ?? 0)}<span>–</span>${esc(m.away_goals ?? 0)}</div>` : `<div class="mh-vs">vs<small>${esc(fmtTime(m.kickoff))}</small></div>`;
  const status = m.status === 'finished' ? '<span class="pill pill-final">Final</span>' : m.status === 'live' ? '<span class="pill pill-live">Live</span>' : '';
  const form = a.form || {};
  return `<section class="card match-hero sport-${esc(sport)}">
    <div class="mh-top">${sportTag(sport)}${crest(m.league_logo, m.league, 'xs', 'league')}<span class="mh-league">${esc(leagueName(m.league))}</span><span class="muted">${esc(fmtDateTime(m.kickoff))}</span>${status}${data.retrospective ? '<span class="pill" title="Analiză după începerea meciului: nu intră în track record">retrospectiv</span>' : ''}</div>
    <div class="mh-teams">
      <div class="mh-team">${crest(m.home_logo, m.home, 'xl')}<h1 class="mh-name">${esc(m.home)}</h1>${formPills(form.home?.sequence)}</div>
      ${centre}
      <div class="mh-team">${crest(m.away_logo, m.away, 'xl')}<h2 class="mh-name">${esc(m.away)}</h2>${formPills(form.away?.sequence)}</div>
    </div>
    <p class="mh-summary">${esc(a.summary)}</p>
    <div class="mh-bottom">
      <div class="confidence">${gradeBadge(a.grade, a.confidence)}<span>Încredere</span><span class="meter"><span data-w="${clamp01((a.confidence || 0) / 100)}"></span></span><b>${esc(a.confidence ?? '—')}/100</b></div>
      <div id="enrich-line" class="small muted">${pending ? '' : data.saved ? 'Predicția a fost salvată în track record (prima analiză înainte de start).' : ''}</div>
      ${m.id.startsWith('demo-') || !isUpcoming(m) ? '' : `<button id="refresh-analysis" class="btn btn-ghost btn-small" type="button">${icon('refresh')}Reîmprospătează</button>`}
    </div>
  </section>`;
}

// --- football --------------------------------------------------------------------------------

function footballBody(m, a, markets, standings) {
  const c = a.components || {};
  const odds = m.odds || {};
  const marketLine = c.market_1x2
    ? `<p class="small muted">Model: 1 ${pct(c.model_1x2?.['1'])} · X ${pct(c.model_1x2?.X)} · 2 ${pct(c.model_1x2?.['2'])} · Piață fără marjă: 1 ${pct(c.market_1x2['1'])} · X ${pct(c.market_1x2.X)} · 2 ${pct(c.market_1x2['2'])} · pondere piață ${pct(c.market_weight)}</p>`
    : '<p class="small muted">Fără cote 1X2: probabilitățile vin numai din model.</p>';
  const totals = c.totals || {};
  const totalsLine = isNum(totals.market_over25)
    ? `<p class="small muted">Peste 2.5 aliniat cu piața: model calibrat ${pct(totals.calibrated_over25)} · piață fără marjă ${pct(totals.market_over25)} · final ${pct(totals.over25)} (pondere piață ${pct(totals.market_weight)}).</p>`
    : isNum(totals.calibrated_over25) ? `<p class="small muted">Peste 2.5 calibrat: ${pct(totals.model_over25)} → ${pct(totals.calibrated_over25)} (fără cotă peste/sub 2.5).</p>` : '';
  const xg = a.expected_goals || a.expected || {};
  return `
    <section class="section grid grid-2">
      <div class="card panel">
        <h2 class="panel-title">Probabilități</h2>
        ${outcomeStrip([{label: `1 · ${m.home}`, value: markets['1']?.probability, odds: odds['1']}, {label: 'X · egal', value: markets.X?.probability, odds: odds.X}, {label: `2 · ${m.away}`, value: markets['2']?.probability, odds: odds['2']}])}
        ${marketLine}
        ${totalsLine}
        <div class="kpi-grid kpi-4">
          ${kpi(`xG ${m.home}`, num(xg.home))}${kpi(`xG ${m.away}`, num(xg.away))}
          ${kpi('Peste 2.5', pct(markets.over25?.probability))}${kpi('Ambele marchează', pct(markets.btts?.probability))}
        </div>
      </div>
      <div class="card panel">${tipsBlock(a)}</div>
    </section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Scor corect</h2><p class="small muted">Rânduri: goluri ${esc(m.home)} · coloane: goluri ${esc(m.away)}</p>${heatmap(a.score_grid || [])}
        <table class="table compact"><tbody>${(a.scores || []).slice(0, 5).map(s => `<tr><td><b>${esc(s.score)}</b></td><td class="num">${pct(s.probability, 1)}</td><td class="num muted">cotă corectă ${num(1 / s.probability)}</td></tr>`).join('')}</tbody></table></div>
      <div class="card panel"><h2 class="panel-title">Total goluri</h2>${barsList((a.goal_distribution || []).map((p, i, list) => ({label: i === list.length - 1 ? `${i}+ goluri` : plural(i, 'gol', 'goluri'), value: p})))}
        <h2 class="panel-title gap">Pauză / Final</h2>${barsList((a.htft || []).slice(0, 6).map((x, i) => ({label: x.label, value: x.probability, top: i === 0})))}</div>
    </section>
    <section class="section"><div class="section-head"><div><h2>Forma echipelor</h2><p class="muted small">Toate competițiile · cel mai recent meci primul</p></div></div>
      <div class="grid grid-2">${footballTeamCard(m.home, m.home_logo, a.form?.home)}${footballTeamCard(m.away, m.away_logo, a.form?.away)}</div></section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Meciuri directe</h2>${h2hBlock(a.h2h, m)}</div>
      <div class="card panel"><h2 class="panel-title">Ce spun datele</h2>${insightsList(a.insights)}</div>
    </section>
    ${standings?.length ? standingsBlock(standings, m) : ''}`;
}

function tipsBlock(a) {
  return `<h2 class="panel-title">Ponturi</h2>
    <table class="table"><tbody>${(a.tips || []).map(t => `<tr><td class="muted small">${esc(t.category)}</td><td><b>${esc(t.label)}</b>${isNum(t.odds) ? ` <span class="muted small">@ ${num(t.odds)}</span>` : ''}</td><td class="num">${pct(t.probability, 1)}</td></tr>`).join('') || '<tr><td class="muted">Niciun pont.</td></tr>'}</tbody></table>
    ${a.selection ? `<p class="small">Selecție pentru track record: <b>${esc(a.selection.label)}</b> (${pct(a.selection.probability, 1)}, prag ${pct(a.threshold)}).</p>` : `<p class="small muted">${esc(a.reason)}</p>`}`;
}

function heatmap(grid) {
  if (!grid.length) return '<p class="muted small">Matricea de scoruri nu este disponibilă.</p>';
  const header = `<div class="axis"></div>${grid[0].map((_, j) => `<div class="axis">${j}</div>`).join('')}`;
  const rows = grid.map((row, h) => `<div class="axis">${h}</div>${row.map((p, j) => `<div class="cell" data-heat="${esc(p)}" title="Scor ${h}-${j}: ${pct(p, 1)}">${(p * 100).toFixed(1)}</div>`).join('')}`).join('');
  return `<div class="heat heat-${grid[0].length}" role="table" aria-label="Probabilitatea fiecărui scor">${header}${rows}</div>`;
}

function footballTeamCard(name, logo, form) {
  if (!form) return `<div class="card panel">${emptyState(`${name}: fără date`)}</div>`;
  const row = (label, w) => w ? `<tr><td>${esc(label)}</td><td class="num">${esc(w.played)}</td><td class="num">${num(w.points_per_game)}</td><td class="num">${num(w.scored_avg, 1)}</td><td class="num">${num(w.conceded_avg, 1)}</td><td class="num">${pct(w.over25)}</td><td class="num">${pct(w.btts)}</td></tr>` : `<tr><td>${esc(label)}</td><td colspan="6" class="muted">fără date</td></tr>`;
  const streak = form.streaks || {};
  return `<div class="card panel team-card">
    <h3 class="team-head">${crest(logo, name, 'md')}<span>${esc(name)}</span>${formPills(form.sequence)}</h3>
    <div class="table-wrap"><table class="table compact">
      <thead><tr><th></th><th class="num">M</th><th class="num">Pct/m</th><th class="num">GM</th><th class="num">GP</th><th class="num">P2.5</th><th class="num">GG</th></tr></thead>
      <tbody>${row('Ultimele 5', form.last5)}${row('Ultimele 10', form.last10)}${row('Acasă (10)', form.home10)}${row('Deplasare (10)', form.away10)}</tbody>
    </table></div>
    ${form.streaks ? `<p class="small muted">Serii: ${esc(plural(streak.unbeaten ?? 0, 'meci', 'meciuri'))} fără înfrângere · ${esc(plural(streak.winless ?? 0, 'meci', 'meciuri'))} fără victorie · marchează de ${esc(plural(streak.scoring ?? 0, 'meci', 'meciuri'))}${isNum(form.days_since_last) ? ` · ultimul meci ${esc(daysAgo(form.days_since_last))}` : ''}</p>` : ''}
    ${lastMatches(form.last, 'football')}
  </div>`;
}

// Tennis has no home/away: its rows show no venue.
function lastMatches(list, sport = 'football') {
  if (!list?.length) return '<p class="muted small">Niciun rezultat recent disponibil.</p>';
  return `<ul class="last-list${sport === 'tennis' ? ' no-venue' : ''}">${list.slice(0, 10).map(g => `<li>
      <span class="res res-${esc(g.result)}">${esc({W: 'V', D: 'E', L: 'Î'}[g.result] || g.result || '')}</span>
      <span class="muted small">${esc(fmtShortDate(g.date))}</span>
      ${sport === 'tennis' ? '' : `<span class="venue small">${g.venue === 'A' ? 'acasă' : 'depl.'}</span>`}
      ${crest(g.opponent_logo, g.opponent, 'xs')}<span class="opp">${esc(g.opponent)}</span>
      <b class="num">${esc(g.score)}</b>
      <span class="comp small muted">${esc(g.competition)}</span>
    </li>`).join('')}</ul>`;
}

function h2hBlock(h2h, m) {
  if (!h2h || !h2h.played) return '<p class="muted">Nu există meciuri directe în datele disponibile. Predicția folosește forma fiecăruia.</p>';
  const counts = isNum(h2h.home_wins) ? `<div class="h2h-counts"><span><b>${h2h.home_wins}</b>${esc(m.home)}</span>${m.sport === 'football' || !m.sport ? `<span><b>${h2h.draws}</b>egal</span>` : ''}<span><b>${h2h.away_wins}</b>${esc(m.away)}</span></div>` : '';
  return `${counts}
    ${isNum(h2h.goals_avg) ? `<p class="small muted">${num(h2h.goals_avg, 1)} goluri/meci · P2.5 ${pct(h2h.over25)} · GG ${pct(h2h.btts)}</p>` : ''}
    <table class="table compact"><tbody>${(h2h.matches || []).map(g => `<tr><td class="muted small">${esc(fmtShortDate(g.date))}</td><td class="small">${esc(g.competition)}</td><td>${esc(g.home)} – ${esc(g.away)}</td><td class="num"><b>${esc(g.score)}</b></td></tr>`).join('')}</tbody></table>`;
}

const insightsList = list => list?.length ? `<ul class="insights">${list.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : '<p class="muted">Nicio tendință puternică în datele disponibile.</p>';

function standingsBlock(rows, m) {
  const teams = new Set([m.home_id, m.away_id, m.home, m.away]);
  return `<section class="section"><div class="section-head"><h2>Clasament</h2></div><div class="card table-card"><div class="table-wrap"><table class="table">
    <thead><tr><th>#</th><th>Echipă</th><th class="num">M</th><th class="num">V</th><th class="num">E</th><th class="num">Î</th><th class="num">Goluri</th><th class="num">Pct</th></tr></thead>
    <tbody>${rows.map(r => `<tr class="${teams.has(r.team_id) || teams.has(r.name) ? 'highlight' : ''}"><td>${esc(r.position)}</td><td><span class="cell-team">${crest(r.logo, r.name, 'xs')}${esc(r.name)}</span></td><td class="num">${esc(r.played)}</td><td class="num">${esc(r.wins)}</td><td class="num">${esc(r.draws)}</td><td class="num">${esc(r.losses)}</td><td class="num">${esc(r.scored)}:${esc(r.conceded)}</td><td class="num"><b>${esc(r.points)}</b></td></tr>`).join('')}</tbody></table></div></div></section>`;
}

function marketsTable(markets) {
  if (!markets.length) return emptyState('Nicio piață în acest grup.');
  let group = '';
  const rows = markets.map(x => {
    const head = x.group !== group ? `<tr class="group-row"><th colspan="5" scope="rowgroup">${esc(x.group)}</th></tr>` : '';
    group = x.group;
    const ev = isNum(x.ev) ? `<span class="ev ${toneOf(x.ev)}">${signedPct(x.ev, 1)}</span>` : '<span class="muted">—</span>';
    return `${head}<tr${x.selectable === false ? ' class="muted-row"' : ''}><td>${esc(x.label)}${x.selectable === false ? ' <small class="muted">(doar afișare)</small>' : ''}</td><td class="num prob-cell">${probBar(x.probability, 'thin')}<b>${pct(x.probability, 1)}</b></td><td class="num">${num(x.fair_odds)}</td><td class="num">${isNum(x.odds) ? `<b>${num(x.odds)}</b>` : '—'}</td><td class="num">${ev}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="table markets"><thead><tr><th>Piață</th><th class="num">Probabilitate</th><th class="num">Cotă corectă</th><th class="num">Cotă casă</th><th class="num">EV</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// --- basketball ------------------------------------------------------------------------------

// The line of the market family closest to 50% (the board's headline line).
function headlineLine(markets, pattern) {
  let best = null;
  markets.forEach(x => {
    const m = String(x.key).match(pattern);
    if (m && (best == null || Math.abs(x.probability - 0.5) < Math.abs(best.p - 0.5))) best = {line: Number(m[1]), p: x.probability};
  });
  return best?.line ?? null;
}

// Rows of a two-sided line family: totals over_/under_ or handicaps ah_1_/ah_2_.
function linePairs(markets, kind) {
  const byKey = Object.fromEntries(markets.map(x => [x.key, x]));
  const rows = [];
  markets.forEach(x => {
    if (kind === 'total') {
      const m = String(x.key).match(/^over_(\d+(?:\.\d+)?)$/);
      if (m) rows.push({line: Number(m[1]), text: m[1], a: x, b: byKey[`under_${m[1]}`]});
    } else {
      const m = String(x.key).match(/^ah_1_([+-]?\d+(?:\.\d+)?)$/);
      if (m) {
        const line = Number(m[1]);
        const other = line === 0 ? '0' : (line > 0 ? `-${m[1].replace('+', '')}` : `+${m[1].replace('-', '')}`);
        rows.push({line, text: m[1], a: x, b: byKey[`ah_2_${other}`]});
      }
    }
  });
  return rows.sort((p, q) => p.line - q.line);
}

function pairsTable(rows, headA, headB, lineHead = 'Linie') {
  if (!rows.length) return '<p class="muted small">Nicio linie disponibilă.</p>';
  const cell = x => x ? `<td class="num">${pct(x.probability, 1)}</td><td class="num">${isNum(x.odds) ? `<b>${num(x.odds)}</b>` : `<span class="muted" title="cotă corectă">${num(x.fair_odds)}</span>`}</td>` : '<td class="num">—</td><td class="num">—</td>';
  const near = rows.reduce((best, r) => (Math.abs((r.a?.probability ?? 0) - 0.5) < Math.abs((best.a?.probability ?? 0) - 0.5) ? r : best), rows[0]);
  return `<div class="table-wrap"><table class="table compact"><thead><tr><th>${esc(lineHead)}</th><th class="num">${esc(headA)}</th><th class="num">cotă</th><th class="num">${esc(headB)}</th><th class="num">cotă</th></tr></thead>
    <tbody>${rows.map(r => `<tr class="${r === near ? 'highlight' : ''}"><td><b>${esc(r.text)}</b></td>${cell(r.a)}${cell(r.b)}</tr>`).join('')}</tbody></table></div>
    <p class="small muted">Cotele gri sunt cote corecte (fără preț real). Rândul evidențiat este linia cea mai echilibrată.</p>`;
}

function basketballBody(m, a, markets) {
  const e = a.expected || {};
  const list = a.markets || [];
  const perMinute = isNum(e.total) && e.minutes ? e.total / e.minutes : null;
  const form = a.form || {};
  return `
    <section class="section grid grid-2">
      <div class="card panel">
        <h2 class="panel-title">Câștigător (incl. prelungiri)</h2>
        ${outcomeStrip([{label: `1 · ${m.home}`, value: markets['1']?.probability, odds: markets['1']?.odds}, {label: `2 · ${m.away}`, value: markets['2']?.probability, odds: markets['2']?.odds}])}
        <div class="big-score"><span>${num(e.home, 0)}</span><small>scor estimat</small><span>${num(e.away, 0)}</span></div>
        <div class="kpi-grid kpi-4">
          ${kpi('Diferență estimată', `${e.margin > 0 ? '+' : ''}${num(e.margin, 1)}`, `± ${num(e.margin_sd, 1)} puncte`)}
          ${kpi('Total estimat', num(e.total, 1), `± ${num(e.total_sd, 1)} puncte`)}
          ${kpi('Prelungiri', pct(e.overtime, 1), 'egal la final de timp regulamentar')}
          ${kpi('Ritm', perMinute ? `${num(perMinute, 2)}/min` : '—', `${esc(e.minutes ?? '—')} minute regulamentare`)}
        </div>
      </div>
      <div class="card panel">${tipsBlock(a)}</div>
    </section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Distribuția diferenței de scor</h2><div id="margin-chart" class="chart-box"></div><p class="small muted">Suprafața din dreapta liniei: ${esc(m.home)} câștigă.</p></div>
      <div class="card panel"><h2 class="panel-title">Distribuția totalului de puncte</h2><div id="total-chart" class="chart-box"></div><p class="small muted">Linia verticală: linia de total cea mai echilibrată.</p></div>
    </section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Handicap</h2>${pairsTable(linePairs(list, 'handicap'), m.home, m.away, 'Handicap gazde')}</div>
      <div class="card panel"><h2 class="panel-title">Total puncte</h2>${pairsTable(linePairs(list, 'total'), 'Peste', 'Sub')}</div>
    </section>
    <section class="section"><div class="section-head"><h2>Formă și odihnă</h2></div>
      <div class="grid grid-2">${genericTeamCard(m.home, m.home_logo, form.home, 'basketball')}${genericTeamCard(m.away, m.away_logo, form.away, 'basketball')}</div></section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Meciuri directe</h2>${h2hBlock(a.h2h, m)}</div>
      <div class="card panel"><h2 class="panel-title">Ce spun datele</h2>${insightsList(a.insights)}</div>
    </section>`;
}

function genericTeamCard(name, logo, form, sport) {
  if (!form) return `<div class="card panel">${emptyState(`${name}: fără date`)}</div>`;
  const w5 = form.last5, w10 = form.last10;
  const rate = w => w ? (isNum(w.win_rate) ? w.win_rate : w.played ? w.wins / w.played : null) : null;
  const stats = [
    kpi('Ultimele 5', w5 ? `${w5.wins}-${w5.losses}` : '—', w5 ? `${pct(rate(w5))} victorii` : 'fără date'),
    kpi('Ultimele 10', w10 ? `${w10.wins}-${w10.losses}` : '—', w10 ? `${pct(rate(w10))} victorii` : 'fără date'),
  ];
  if (sport === 'basketball') {
    stats.push(kpi('Diferență medie (5)', isNum(form.margin_avg5) ? `${form.margin_avg5 > 0 ? '+' : ''}${num(form.margin_avg5, 1)}` : '—'));
    stats.push(kpi('Odihnă', isNum(form.rest_days) ? esc(plural(form.rest_days, 'zi', 'zile')) : '—', form.back_to_back ? 'meci și ieri (back-to-back)' : ''));
  } else {
    stats.push(kpi('Seturi câștigate/meci', w10 ? num(w10.scored_avg, 1) : '—', w10 ? `pierdute ${num(w10.conceded_avg, 1)}` : ''));
    stats.push(kpi('Ultimul meci', isNum(form.days_since_last) ? esc(daysAgo(form.days_since_last)) : '—'));
  }
  return `<div class="card panel team-card">
    <h3 class="team-head">${crest(logo, name, 'md', sport === 'tennis' ? 'flag' : 'team')}<span>${esc(name)}</span>${formPills(form.sequence)}</h3>
    <div class="kpi-grid kpi-4 small-kpis">${stats.join('')}</div>
    ${form.streak?.count ? `<p class="small muted">Serie curentă: ${esc(form.streak.result === 'W' ? plural(form.streak.count, 'victorie', 'victorii') : plural(form.streak.count, 'înfrângere', 'înfrângeri'))} la rând${isNum(form.margin_avg10) ? ` · diferență medie (10): ${form.margin_avg10 > 0 ? '+' : ''}${num(form.margin_avg10, 1)}` : ''}</p>` : ''}
    ${lastMatches(form.last, sport)}
  </div>`;
}

// --- tennis ----------------------------------------------------------------------------------

function tennisBody(m, a, markets) {
  const e = a.expected || {};
  const c = a.components || {};
  const list = a.markets || [];
  const sets = list.filter(x => /^sets_\d-\d$/.test(x.key));
  const bestSet = sets.reduce((best, x) => (!best || x.probability > best.probability ? x : best), null);
  const games = list.filter(x => /^games_(over|under)_/.test(x.key));
  const gamesRows = linePairs(games.map(x => ({...x, key: x.key.replace(/^games_/, '')})), 'total');
  const setTotals = linePairs(list.filter(x => /^(over|under)_/.test(x.key)), 'total');
  const surface = {hard: 'hard', clay: 'zgură', grass: 'iarbă', carpet: 'mochetă'}[e.surface] || e.surface || '—';
  return `
    <section class="section grid grid-2">
      <div class="card panel">
        <h2 class="panel-title">Câștigător</h2>
        ${outcomeStrip([{label: `1 · ${m.home}`, value: markets['1']?.probability, odds: markets['1']?.odds}, {label: `2 · ${m.away}`, value: markets['2']?.probability, odds: markets['2']?.odds}])}
        <div class="kpi-grid kpi-4">
          ${kpi('Suprafață', esc(surface), `cel mult ${esc(e.best_of ?? 3)} seturi`)}
          ${kpi('Șansă pe set', pct(e.set_win), esc(m.home))}
          ${kpi('Game-uri estimate', num(e.games, 1))}
          ${kpi('Scor probabil', bestSet ? esc(bestSet.key.replace('sets_', '')) : '—', bestSet ? pct(bestSet.probability) : '')}
        </div>
        <div class="elo">
          <div class="elo-side">${crest(m.home_logo, m.home, 'sm', 'flag')}<span>${esc(m.home)}</span><b>${num(c.elo_home, 0)}</b><small>Elo suprafață · general ${num(c.elo_home_overall, 0)}</small></div>
          <div class="elo-side">${crest(m.away_logo, m.away, 'sm', 'flag')}<span>${esc(m.away)}</span><b>${num(c.elo_away, 0)}</b><small>Elo suprafață · general ${num(c.elo_away_overall, 0)}</small></div>
        </div>
        ${isNum(c.market_home_win) ? `<p class="small muted">Piață fără marjă: ${pct(c.market_home_win)} · Elo: ${pct(c.model_home_win)} · pondere model ${pct(c.model_weight)}.</p>` : ''}
      </div>
      <div class="card panel">${tipsBlock(a)}</div>
    </section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Scor la seturi</h2>${sets.length ? barsList(sets.map(x => ({label: x.key.replace('sets_', ''), value: x.probability, top: x === bestSet, title: x.label}))) : '<p class="muted small">Indisponibil.</p>'}</div>
      <div class="card panel"><h2 class="panel-title">Handicap la seturi</h2>${pairsTable(linePairs(list, 'handicap'), m.home, m.away, 'Handicap jucătorul 1')}
        <h2 class="panel-title gap">Total seturi</h2>${pairsTable(setTotals, 'Peste', 'Sub')}</div>
    </section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Total game-uri</h2>${pairsTable(gamesRows, 'Peste', 'Sub')}<p class="small muted">Totalul de game-uri nu poate fi decontat din scorul la seturi, așa că nu intră în bilete.</p></div>
      <div class="card panel"><h2 class="panel-title">Meciuri directe</h2>${h2hBlock(a.h2h, m)}</div>
    </section>
    <section class="section"><div class="section-head"><h2>Formă</h2></div>
      <div class="grid grid-2">${genericTeamCard(m.home, m.home_logo, a.form?.home, 'tennis')}${genericTeamCard(m.away, m.away_logo, a.form?.away, 'tennis')}</div></section>
    <section class="section card panel"><h2 class="panel-title">Ce spun datele</h2>${insightsList(a.insights)}</section>`;
}

// --- data quality ----------------------------------------------------------------------------

function qualityPanel(m, a, sport) {
  const s = a.sample || {};
  const c = a.components || {};
  const items = [
    kpi(`Meciuri ${m.home}`, esc(s.home ?? '—')),
    kpi(`Meciuri ${m.away}`, esc(s.away ?? '—')),
    kpi('Meciuri directe', esc(s.h2h ?? '—')),
  ];
  if (sport === 'football') {
    items.push(kpi('Rezultate în rating', esc(s.league ?? '—')));
    if (c.ratings) items.push(kpi('λ rating gazde / oaspeți', `${num(c.ratings.home)} / ${num(c.ratings.away)}`));
  } else if (isNum(c.market_weight)) {
    items.push(kpi('Pondere piață', pct(c.market_weight)));
  }
  // The version name says "calibrated-goals" while analysis.calibrated (1X2) is false: spell out
  // which part is calibrated instead of printing both flags side by side.
  const calibration = a.calibrated ? 'probabilități calibrate'
    : sport === 'football' && c.totals ? '1X2 necalibrat separat; goluri calibrate' : 'necalibrat separat';
  return `<section class="section"><div class="section-head"><div><h2>Calitatea datelor și modelul</h2><p class="muted small">Versiunea modelului ${esc(a.version)} · ${esc(calibration)} · nota A–D arată câte date recente există; doar A–C intră în bilete.</p></div></div>
    <div class="card panel"><div class="kpi-grid kpi-5">${items.join('')}</div></div></section>`;
}
