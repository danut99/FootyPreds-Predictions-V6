'use strict';
// Daily board (#/meciuri): one card per match with the headline markets, the tip and the data
// grade; football keeps its market tabs. GET /api/predictions per sport (paged).

const BOARD_PAGE = 60;
const FOOTBALL_TABS = [['1x2', '1X2'], ['goals', 'Goluri'], ['btts', 'GG'], ['score', 'Scor corect'], ['htft', 'Pauză-Final']];
const boardState = {
  day: isoDay(0),
  competition: '',
  search: '',
  grade: remember('board.grade', 'all'),
  status: remember('board.status', 'all'),
  tab: remember('board.tab', '1x2'),
  data: {},
  syncing: false,
};

function renderBoard() {
  const scope = currentScope;
  const app = $('#app');
  const sports = chosenSports();
  const single = sports.length === 1 ? sports[0] : null;
  if (!single) boardState.competition = '';
  boardState.data = {};
  state.boardRequest += 1;
  const offsets = [-1, 0, 1, 2, 3];
  const dayChips = offsets.map(o => [isoDay(o), esc(dayChipLabel(o))]);
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">Meciuri · ${esc(sports.map(s => SPORT_LABEL[s]).join(', '))}</p>
        <h1>Programul zilei: ${esc(fmtLongDay(boardState.day))}</h1>
        <p class="lead">Probabilități pentru fiecare meci, pontul cel mai probabil și calitatea datelor (A–D). Apasă pe un meci pentru analiza completă.</p>
        ${utcDayNote() ? `<p class="muted small">${esc(utcDayNote())}</p>` : ''}
      </div>
    </section>
    <div class="toolbar card" role="search">
      <div class="chip-row" role="group" aria-label="Ziua">${chips(dayChips, boardState.day, 'bday')}<label class="chip chip-date"><span class="sr-only">Alege data</span><input id="board-date" type="date" value="${esc(boardState.day)}"></label></div>
      <div class="toolbar-fields">
        <label class="field grow">Caută<input id="board-search" type="search" placeholder="Echipă, jucător sau competiție" value="${esc(boardState.search)}"></label>
        ${single ? '<label class="field">Competiție<select id="board-competition"><option value="">Toate competițiile</option></select></label>' : ''}
        <label class="field">Calitate date<select id="board-grade"><option value="all">Toate</option><option value="ABC">A–C</option><option value="AB">Doar A și B</option></select></label>
        <label class="field">Stare<select id="board-status"><option value="all">Toate</option><option value="upcoming">Viitoare</option><option value="live">Live</option><option value="finished">Terminate</option></select></label>
      </div>
      <div class="toolbar-actions">
        <button id="board-sync" class="btn btn-ghost btn-small" type="button" title="Încarcă rezultatele zilelor trecute pentru formă">${icon('refresh')}Sincronizează istoric</button>
        ${single ? '<button id="board-excel" class="btn btn-ghost btn-small" type="button">Descarcă Excel</button>' : ''}
      </div>
      <div id="sync-progress" class="progress-line" hidden><progress id="sync-bar" max="1" value="0"></progress><span id="sync-text" class="small"></span></div>
    </div>
    ${sports.includes('football') ? `<div class="tabs" role="tablist" aria-label="Piețe fotbal">${FOOTBALL_TABS.map(([k, l]) => `<button type="button" role="tab" class="tab ${boardState.tab === k ? 'active' : ''}" aria-selected="${boardState.tab === k}" data-tab="${k}">${esc(l)}</button>`).join('')}</div>` : ''}
    <div id="board-sections">${sports.map(sport => `<section class="board-sport sport-${sport}" id="board-${sport}" aria-labelledby="bh-${sport}">
        ${sports.length > 1 ? `<div class="section-head"><h2 id="bh-${sport}">${sportTag(sport)}</h2><span class="muted small" id="bc-${sport}"></span></div>` : `<h2 class="sr-only" id="bh-${sport}">${esc(SPORT_LABEL[sport])}</h2>`}
        <div class="board-body"><div class="match-grid">${skeletonCards(6)}</div></div>
        <div class="load-more"><button class="btn btn-secondary" type="button" data-more="${sport}" hidden>Încarcă mai multe</button></div>
      </section>`).join('')}</div>
    ${disclaimerBox()}`;
  $('#board-grade').value = boardState.grade;
  $('#board-status').value = boardState.status;
  const setDay = day => { boardState.day = day; boardState.competition = ''; renderBoard(); };
  $$('[data-bday]', app).forEach(b => b.addEventListener('click', () => setDay(b.dataset.bday)));
  $('#board-date').addEventListener('change', e => { if (e.target.value) setDay(e.target.value); });
  let timer;
  $('#board-search').addEventListener('input', e => {
    clearTimeout(timer);
    timer = setTimeout(() => { boardState.search = e.target.value; sports.forEach(drawBoardSport); }, 150);
  });
  // Grade and status filters work on the whole day: missing pages are loaded first.
  const refilter = () => sports.forEach(sport => (filtering() ? loadAllPages(scope, sport) : drawBoardSport(sport)));
  $('#board-grade').addEventListener('change', e => { boardState.grade = e.target.value; persist('board.grade', e.target.value); refilter(); });
  $('#board-status').addEventListener('change', e => { boardState.status = e.target.value; persist('board.status', e.target.value); refilter(); });
  $('#board-competition')?.addEventListener('change', e => {
    boardState.competition = e.target.value;
    state.boardRequest += 1;
    loadBoardSport(scope, single, true);
  });
  $$('[data-tab]', app).forEach(b => b.addEventListener('click', () => {
    boardState.tab = b.dataset.tab;
    persist('board.tab', boardState.tab);
    $$('[data-tab]', app).forEach(t => { t.classList.toggle('active', t === b); t.setAttribute('aria-selected', String(t === b)); });
    drawBoardSport('football');
  }));
  $$('[data-more]', app).forEach(b => b.addEventListener('click', () => loadBoardSport(scope, b.dataset.more, false)));
  $('#board-sync').addEventListener('click', () => startHistorySync(sports));
  $('#board-excel')?.addEventListener('click', () => downloadExcel(single));
  sports.forEach(sport => loadBoardSport(scope, sport, true));
  resumeHistorySync();
}

async function loadBoardSport(scope, sport, reset) {
  const section = $(`#board-${sport}`);
  if (!section) return;
  const current = boardState.data[sport] || {items: [], total: 0, competitions: []};
  if (reset) {
    current.items = [];
    $('.board-body', section).innerHTML = `<div class="match-grid">${skeletonCards(6)}</div>`;
  }
  // A newer board (another day, filter or page) or a newer page of this sport wins.
  const generation = state.boardRequest;
  const request = (current.request || 0) + 1;
  current.request = request;
  boardState.data[sport] = current;
  const params = new URLSearchParams({day: boardState.day, sport, limit: BOARD_PAGE, offset: current.items.length});
  if (boardState.competition && chosenSports().length === 1) params.set('competition', boardState.competition);
  const more = $(`[data-more="${sport}"]`);
  if (more) more.disabled = true;
  try {
    const data = await api(`/api/predictions?${params}`);
    if (!scope.alive || generation !== state.boardRequest || current.request !== request) return;
    current.items = reset ? data.items : current.items.concat(data.items);
    current.total = data.total;
    current.competitions = data.competitions || [];
    fillCompetitionSelect(current.competitions);
    drawBoardSport(sport);
    if (filtering() && current.items.length < current.total && data.items.length) loadBoardSport(scope, sport, false);
  } catch (error) {
    if (!scope.alive || generation !== state.boardRequest || current.request !== request) return;
    $('.board-body', section).innerHTML = errorState(error, `retry-${sport}`);
    $(`#retry-${sport}`)?.addEventListener('click', () => loadBoardSport(scope, sport, true));
  } finally {
    if (more) more.disabled = false;
  }
}

const filtering = () => boardState.grade !== 'all' || boardState.status !== 'all';

function loadAllPages(scope, sport) {
  const current = boardState.data[sport];
  if (current && current.items.length < current.total) loadBoardSport(scope, sport, false);
  else drawBoardSport(sport);
}

function fillCompetitionSelect(list) {
  const select = $('#board-competition');
  if (!select) return;
  const options = list.filter(c => c.count).map(c => `<option value="${esc(c.id)}">${esc(c.country ? `${c.country}: ` : '')}${esc(c.name)} (${esc(c.count)})</option>`);
  select.innerHTML = `<option value="">Toate competițiile</option>${options.join('')}`;
  select.value = boardState.competition;
}

function boardVisible(items) {
  const query = boardState.search.trim().toLocaleLowerCase('ro');
  const now = Date.now();
  return items.filter(item => {
    const m = item.match;
    if (query && !`${m.home} ${m.away} ${item.competition} ${m.country} ${m.league}`.toLocaleLowerCase('ro').includes(query)) return false;
    if (boardState.grade !== 'all' && !boardState.grade.includes(item.grade)) return false;
    if (boardState.status === 'upcoming' && !(m.status === 'scheduled' && new Date(m.kickoff) > now)) return false;
    if (boardState.status === 'live' && m.status !== 'live') return false;
    if (boardState.status === 'finished' && m.status !== 'finished') return false;
    return true;
  });
}

function drawBoardSport(sport) {
  const section = $(`#board-${sport}`);
  const current = boardState.data[sport];
  if (!section || !current) return;
  const body = $('.board-body', section);
  const items = boardVisible(current.items);
  const count = $(`#bc-${sport}`);
  if (count) count.textContent = plural(current.total, 'meci', 'meciuri');
  const more = $(`[data-more="${sport}"]`);
  if (more) {
    more.hidden = current.items.length >= current.total;
    more.textContent = `Încarcă mai multe (${current.items.length}/${current.total})`;
  }
  const loading = filtering() && current.items.length < current.total;
  if (!items.length) {
    body.innerHTML = loading ? `<div class="match-grid">${skeletonCards(3)}</div>`
      : emptyState(current.items.length ? 'Niciun meci pentru aceste filtre.' : `Nu există meciuri de ${SPORT_LABEL[sport].toLowerCase()} în această zi.`, 'Schimbă data, competiția sau filtrele.');
    return;
  }
  // One grid per sport (competitions in the API's priority order); each card names its
  // competition, so days with many one-game competitions do not become a tall list of rows.
  body.innerHTML = `<div class="match-grid">${items.map(matchCard).join('')}</div>`;
  hydrate(body);
}

function footballChips(item) {
  const p = item.probabilities || {};
  const odds = item.match.odds || {};
  const tab = boardState.tab;
  if (tab === 'goals') return [['Peste 1.5', p.over15, odds.over15], ['Peste 2.5', p.over25, odds.over25], ['Peste 3.5', p.over35, odds.over35]];
  if (tab === 'btts') return [['GG', p.btts, odds.btts], ['NG', p.no_btts, odds.no_btts]];
  if (tab === 'score') return (item.scores || []).slice(0, 3).map(s => [s.score, s.probability, odds[`cs_${s.score}`]]);
  if (tab === 'htft') return [['Pauză 1', p.ht_1, odds.ht_1], ['Pauză X', p.ht_X, odds.ht_X], ['Pauză 2', p.ht_2, odds.ht_2]];
  return [['1', p['1'], odds['1']], ['X', p.X, odds.X], ['2', p['2'], odds['2']]];
}

function footballTip(item) {
  const category = {goals: 'Goluri', btts: 'Ambele marchează', score: 'Scor corect', htft: 'Pauză/Final'}[boardState.tab];
  return (category && (item.tips || []).find(t => t.category === category)) || item.tip;
}

function marketChips(rows) {
  const best = Math.max(...rows.map(r => r[1] || 0));
  return rows.map(([label, p, odds, title]) => `<span class="mchip ${p === best ? 'top' : ''}" title="${esc(title || label)}"><small>${esc(label)}</small><b>${pct(p)}</b>${isNum(odds) ? `<em>${num(odds)}</em>` : ''}</span>`).join('');
}

function matchStatus(m, item) {
  if (m.status === 'finished') return `<span class="pill pill-final">Final</span>`;
  if (m.status === 'live') return `<span class="pill pill-live">Live</span>`;
  if (m.status === 'unavailable') return '<span class="pill">Amânat</span>';
  return `<time datetime="${esc(m.kickoff)}">${esc(fmtTime(m.kickoff))}</time>`;
}

function matchCard(item) {
  const m = item.match;
  const sport = item.sport || m.sport;
  const finished = m.status === 'finished' || m.status === 'live';
  const rows = sport === 'football'
    ? footballChips(item)
    : (item.main || []).map(x => [shortMarket(x.key, x.label), x.probability, x.odds, x.label]);
  const tip = sport === 'football' ? footballTip(item) : item.tip;
  const result = item.result
    ? (item.result.tip_won == null ? statusBadge('void') : statusBadge(item.result.tip_won ? 'won' : 'lost'))
    : '';
  const form = item.form || {};
  const xg = sport === 'football' && boardState.tab === 'goals' && item.expected_goals
    ? `<span class="mc-extra">xG ${num((item.expected_goals.home || 0) + (item.expected_goals.away || 0), 1)}</span>` : '';
  return `<a class="card match-card sport-${esc(sport)} status-${esc(m.status)}" href="${matchHref(m.id, sport)}">
    <div class="mc-comp">${crest(m.league_logo, item.competition, 'xs', 'league')}<span title="${esc(m.country ? `${m.country} · ${item.competition}` : item.competition)}">${esc(m.country ? `${m.country} · ` : '')}${esc(item.competition)}</span></div>
    <div class="mc-top">${matchStatus(m, item)}${gradeBadge(item.grade, item.confidence)}</div>
    <div class="mc-teams">
      <div class="mc-team">${crest(m.home_logo, m.home, 'md')}<span class="team-name">${esc(m.home)}</span>${formPills(form.home)}${finished ? `<b class="mc-score">${esc(m.home_goals ?? '')}</b>` : ''}</div>
      <div class="mc-team">${crest(m.away_logo, m.away, 'md')}<span class="team-name">${esc(m.away)}</span>${formPills(form.away)}${finished ? `<b class="mc-score">${esc(m.away_goals ?? '')}</b>` : ''}</div>
    </div>
    <div class="mc-markets">${marketChips(rows)}${xg}</div>
    ${tip ? `<div class="mc-tip"><span>Pont</span><b>${esc(tip.label)}</b><strong>${pct(tip.probability)}</strong>${result}</div>` : ''}
  </a>`;
}

// --- history sync and Excel export ---------------------------------------------------------

async function startHistorySync(sports) {
  const ok = await confirmDialog({
    title: 'Sincronizezi istoricul?',
    text: `Încarc rezultatele ultimelor 21 de zile pentru ${sports.map(s => SPORT_LABEL[s].toLowerCase()).join(', ')} (cel mult o cerere FlashScore pe zi și sport; zilele deja salvate sunt sărite).`,
    ok: 'Sincronizează',
  });
  if (!ok) return;
  try {
    followHistorySync(await post('/api/history/sync', {days: 21, sports}));
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function resumeHistorySync() {
  try {
    const status = await api('/api/history/sync');
    if (status.status === 'running') followHistorySync(status);
  } catch { /* optional */ }
}

async function followHistorySync(status) {
  if (boardState.syncing) return;
  boardState.syncing = true;
  const show = s => {
    const box = $('#sync-progress');
    if (!box) return;
    box.hidden = false;
    $('#sync-bar').max = Math.max(1, s.total || 1);
    $('#sync-bar').value = s.done || 0;
    $('#sync-text').textContent = `${s.message || 'Sincronizare…'} ${s.done || 0}/${s.total || 0} zile · ${s.matches || 0} rezultate`;
  };
  try {
    while (status.status === 'running') {
      show(status);
      await new Promise(resolve => setTimeout(resolve, 1200));
      status = await api('/api/history/sync');
    }
    show(status);
    toast(status.status === 'failed' ? status.message : (status.message || 'Istoricul a fost sincronizat.'), status.status === 'failed' ? 'error' : 'success');
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    boardState.syncing = false;
    loadHealth();
  }
}

async function downloadExcel(sport) {
  const button = $('#board-excel');
  if (button) button.disabled = true;
  try {
    const params = new URLSearchParams({day: boardState.day, sport});
    if (boardState.competition) params.set('competition', boardState.competition);
    let response;
    try {
      response = await fetch(`${API_BASE}/api/export.xlsx?${params}`);
    } catch {
      throw new Error('Serverul nu răspunde.');
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : 'Exportul Excel a eșuat.');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `FootyPreds-${sport}-${boardState.day}.xlsx`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}
