'use strict';
// Simulator (#/simulator): blind walk-forward bankroll simulation with virtual money.
// Strategies: ladder (rollover), daily ticket, safest singles, value. Datasets from
// GET /api/simulate/datasets plus "recent" (last N days from the local store, prepared through
// POST /api/simulate/recent/prepare + GET /api/simulate/recent/status).

const STRATEGIES = [
  ['ladder', 'Scară', 'Un bilet pe zi la cota țintă; câștigul se rejoacă a doua zi.'],
  ['ticket', 'Bilet zilnic', 'Un bilet pe zi la cota țintă, cu miză fixă sau procentuală.'],
  ['singles', 'Cele mai sigure simple', 'Cele mai probabile selecții ale zilei, câte una pe meci.'],
  ['value', 'Value', 'Selecții simple unde modelul vede valoare (EV ≥ 2%).'],
];
const LADDER_STATUS = {lost: 'pierdută', cashed: 'încasată', open: 'în curs'};
const TIMELINE_PAGE = 30;

const simState = {
  bankroll: remember('sim.bankroll', 5),
  dataset: remember('sim.dataset', 'recent'),
  strategy: remember('sim.strategy', 'ladder'),
  days: remember('sim.days', 14),
  sports: null,
  datasets: [],
  result: null,
  shown: TIMELINE_PAGE,
  polling: false,
};

function renderSimulator() {
  const scope = currentScope;
  const app = $('#app');
  simState.sports = chosenSports();
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">Simulator · bani virtuali</p>
        <h1>Cât ar fi rezistat strategia?</h1>
        <p class="lead">Simulare oarbă, zi cu zi: pentru fiecare zi modelul vede doar rezultatele de dinainte, fixează biletele și abia apoi află scorurile.</p>
      </div>
    </section>
    <form id="sim-form" class="card sim-form" novalidate>
      <div class="form-grid">
        <label class="field">Suma de pornire (RON)<input id="sim-bankroll" type="number" min="0.01" max="10000000" step="0.01" required value="${esc(simState.bankroll)}" inputmode="decimal"></label>
        <label class="field grow">Set de date<select id="sim-dataset"><option value="recent">Ultimele zile (din aplicație)</option></select></label>
      </div>
      <p id="sim-dataset-hint" class="small muted"></p>
      <div id="sim-recent" class="recent-box" hidden>
        <div class="form-grid">
          <label class="field">Număr de zile<input id="sim-days" type="number" min="1" max="60" step="1" value="${esc(simState.days)}"></label>
          <fieldset class="field-set"><legend>Sporturi</legend><div class="chip-row" role="group" aria-label="Sporturi pentru zilele recente">${SPORT_KEYS.map(s => `<button type="button" class="chip chip-sport ${simState.sports.includes(s) ? 'active' : ''}" data-ssport="${s}" aria-pressed="${simState.sports.includes(s)}">${icon(s)}${esc(SPORT_LABEL[s])}</button>`).join('')}</div></fieldset>
          <button id="sim-prepare" class="btn btn-secondary" type="button">${icon('refresh')}Pregătește datele</button>
        </div>
        <div id="recent-status" class="recent-status" aria-live="polite"></div>
      </div>
      <div id="sim-period" class="form-grid">
        <label class="field">De la<input id="sim-start" type="date"></label>
        <label class="field">Până la<input id="sim-end" type="date"></label>
        <p class="small muted period-hint">Gol = implicit (ultimele 365 de zile ale setului).</p>
      </div>
      <fieldset class="strategy-set">
        <legend>Strategie</legend>
        <div class="strategy-grid" role="radiogroup" aria-label="Strategie">${STRATEGIES.map(([key, label, text]) => `<label class="strategy ${simState.strategy === key ? 'active' : ''}"><input type="radio" name="strategy" value="${key}" ${simState.strategy === key ? 'checked' : ''}><b>${esc(label)}</b><span>${esc(text)}</span></label>`).join('')}</div>
      </fieldset>
      <div id="sim-options"></div>
      <div class="form-actions">
        <button id="sim-run" class="btn btn-primary btn-large" type="submit">${icon('play')}Rulează simularea</button>
        <span class="muted small">Prima rulare pe un set de date calculează predicțiile și poate dura mai mult.</span>
      </div>
    </form>
    <div id="sim-output" class="section" aria-live="polite"></div>
    <div id="sim-disclaimer">${disclaimerBox('Simulare cu bani virtuali pe meciuri din trecut. Estimări statistice, nu garanții. 18+.')}</div>`;
  drawStrategyOptions();
  const form = $('#sim-form');
  $$('input[name="strategy"]', form).forEach(radio => radio.addEventListener('change', () => {
    simState.strategy = radio.value;
    persist('sim.strategy', radio.value);
    $$('.strategy', form).forEach(l => l.classList.toggle('active', l.contains(radio)));
    drawStrategyOptions();
  }));
  $('#sim-dataset').addEventListener('change', e => {
    simState.dataset = e.target.value;
    persist('sim.dataset', simState.dataset);
    drawDatasetInfo(scope);
  });
  $('#sim-bankroll').addEventListener('change', e => { simState.bankroll = Number(e.target.value) || 5; persist('sim.bankroll', simState.bankroll); });
  $('#sim-days').addEventListener('change', e => {
    simState.days = Math.max(1, Math.min(60, Math.round(Number(e.target.value) || 14)));
    e.target.value = simState.days;
    persist('sim.days', simState.days);
    refreshRecentStatus(scope);
  });
  $$('[data-ssport]', form).forEach(b => b.addEventListener('click', () => {
    const sport = b.dataset.ssport;
    const has = simState.sports.includes(sport);
    if (has && simState.sports.length === 1) { toast('Alege cel puțin un sport.', 'error'); return; }
    simState.sports = has ? simState.sports.filter(s => s !== sport) : SPORT_KEYS.filter(s => s === sport || simState.sports.includes(s));
    b.classList.toggle('active', !has);
    b.setAttribute('aria-pressed', String(!has));
    refreshRecentStatus(scope);
  }));
  $('#sim-prepare').addEventListener('click', () => prepareRecent(scope));
  form.addEventListener('submit', event => { event.preventDefault(); runSimulation(scope); });
  loadDatasets(scope);
}

function drawStrategyOptions() {
  const box = $('#sim-options');
  if (!box) return;
  const s = simState.strategy;
  const target = remember('sim.target', 2);
  const staking = `<label class="field">Miză<select id="sim-staking"><option value="flat">Fixă (sumă)</option><option value="percent">Procent din sold</option><option value="kelly">Kelly fracționat</option></select></label>
    <label class="field">Valoare miză<input id="sim-stake" type="number" min="0" step="0.01" placeholder="implicit" inputmode="decimal"></label>`;
  if (s === 'ladder') {
    box.innerHTML = `<div class="form-grid">
      <label class="field">Cotă țintă zilnică<input id="sim-target" type="number" min="1.2" max="100" step="0.05" value="${esc(target)}" inputmode="decimal"></label>
      <label class="field">Rejoacă din sold<span class="range-line"><input id="sim-reinvest" type="range" min="10" max="100" step="5" value="${esc(remember('sim.reinvest', 100))}"><output id="sim-reinvest-out">${esc(remember('sim.reinvest', 100))}%</output></span></label>
      <label class="field">Încasează după (zile)<input id="sim-maxdays" type="number" min="1" max="365" step="1" placeholder="niciodată"></label>
      <label class="check"><input id="sim-restart" type="checkbox" ${remember('sim.restart', true) ? 'checked' : ''}> Repornește cu suma inițială după o pierdere</label>
    </div>
    <p class="small muted">Scara: în fiecare zi un singur bilet la cota țintă, cu <b id="sim-reinvest-text"></b> din soldul scării. Un bilet pierdut încheie scara; un bilet anulat returnează miza și ziua contează ca zi ținută. „Încasează după” închide scara după N bilete reușite și pornește alta cu suma inițială (și fără repornire după pierdere).</p>`;
    const range = $('#sim-reinvest');
    const sync = () => {
      $('#sim-reinvest-out').textContent = `${range.value}%`;
      $('#sim-reinvest-text').textContent = `${range.value}%`;
      persist('sim.reinvest', Number(range.value));
    };
    range.addEventListener('input', sync);
    sync();
    $('#sim-restart').addEventListener('change', e => persist('sim.restart', e.target.checked));
  } else if (s === 'ticket') {
    box.innerHTML = `<div class="form-grid"><label class="field">Cotă țintă zilnică<input id="sim-target" type="number" min="1.2" max="100" step="0.05" value="${esc(target)}" inputmode="decimal"></label>${staking}</div>${stakingHint()}`;
  } else {
    box.innerHTML = `<div class="form-grid"><label class="field">Selecții pe zi<input id="sim-perday" type="number" min="1" max="20" step="1" value="3"></label>${staking}</div>${stakingHint()}`;
  }
  $('#sim-target')?.addEventListener('change', e => persist('sim.target', Number(e.target.value)));
  $('#sim-staking')?.addEventListener('change', e => {
    const input = $('#sim-stake');
    input.value = '';
    input.placeholder = {flat: 'implicit 1% din sumă', percent: '% din sold (0.1–20)', kelly: 'fracțiune 0.1–1 (0.25)'}[e.target.value];
  });
}

const stakingHint = () => '<p class="small muted">Miză fixă: suma pe fiecare pariu. Procent: procentul din soldul curent. Kelly: fracțiunea din miza Kelly (plafonată la 10% din sold).</p>';

async function loadDatasets(scope) {
  try {
    const data = await api('/api/simulate/datasets');
    if (!scope.alive) return;
    simState.datasets = data.datasets || [];
  } catch (error) {
    if (!scope.alive) return;
    simState.datasets = [];
    toast(`Seturile de date nu au putut fi încărcate: ${error.message}`, 'error');
  }
  const select = $('#sim-dataset');
  const sports = new Set(chosenSports());
  const listed = simState.datasets.filter(d => d.id !== 'recent' && (sports.size === 3 || sports.has(d.sport)));
  select.innerHTML = `<option value="recent">Ultimele zile (din aplicație) · ${esc(chosenSports().map(s => SPORT_LABEL[s]).join(', '))}</option>${listed.map(d => `<option value="${esc(d.id)}">${esc(d.label)}${d.available ? ` · ${esc(plural(d.bettable, 'meci', 'meciuri'))}` : ' · indisponibil'}</option>`).join('')}`;
  if (![...select.options].some(o => o.value === simState.dataset)) simState.dataset = 'recent';
  select.value = simState.dataset;
  drawDatasetInfo(scope);
}

function drawDatasetInfo(scope) {
  const recent = simState.dataset === 'recent';
  $('#sim-recent').hidden = !recent;
  $('#sim-period').hidden = recent;
  const hint = $('#sim-dataset-hint');
  if (recent) {
    hint.textContent = 'Meciurile terminate din ultimele zile, cu cotele de dinainte de start. Pregătește întâi datele (o cerere FlashScore pe zi și sport, zilele salvate sunt sărite).';
    refreshRecentStatus(scope);
    return;
  }
  const d = simState.datasets.find(x => x.id === simState.dataset);
  if (!d) { hint.textContent = ''; return; }
  hint.textContent = d.available
    ? `${d.label}: ${d.matches} meciuri (${d.bettable} cu cote), ${d.start ? fmtDayMonth(d.start) : '—'} – ${d.end ? fmtDayMonth(d.end) : '—'} ${d.end ? d.end.slice(0, 4) : ''}. Sursa: ${d.source}.`
    : `Indisponibil. ${d.hint || ''}`;
  ['sim-start', 'sim-end'].forEach(id => {
    const input = $(`#${id}`);
    input.min = d.start || '';
    input.max = d.end || '';
  });
}

function recentStatusView(status) {
  const running = status.status === 'running';
  const total = status.total || 0;
  const progress = total ? status.done / total : (status.status === 'done' ? 1 : 0);
  const label = {idle: 'Nepregătit', running: 'Se încarcă', done: 'Gata', partial: 'Parțial', failed: 'Eșuat', interrupted: 'Întrerupt'}[status.status] || status.status;
  const sports = (status.sports || simState.sports || []).length || 1;
  const days = status.days || simState.days;
  // days_loaded / days_total count day × sport pairs: say so instead of "42 din 42 zile".
  const window = `Fereastra: ${plural(days, 'zi', 'zile')} × ${plural(sports, 'sport', 'sporturi')}`;
  const coverage = isNum(status.days_loaded) ? ` · ${status.days_loaded}/${status.days_total ?? '?'} zile-sport încărcate` : '';
  const matches = isNum(status.matches) ? ` · ${plural(status.matches, 'meci', 'meciuri')} cu cote` : '';
  const line = running
    ? `${status.done || 0}/${total} cereri de zi${status.message ? ` · ${status.message}` : ''}`
    : `${window}${coverage}${matches}`;
  return `<div class="recent-line">
      <span class="badge badge-${running ? 'pending' : status.status === 'failed' ? 'lost' : status.status === 'done' ? 'won' : 'void'}">${esc(label)}</span>
      <span class="small">${esc(line)}</span>
    </div>
    ${running ? `<div class="progress" role="progressbar" aria-label="Pregătire zile recente" aria-valuemin="0" aria-valuemax="${esc(total)}" aria-valuenow="${esc(status.done || 0)}"><span data-w="${clamp01(progress)}"></span></div>` : status.message ? `<p class="small muted">${esc(status.message)}</p>` : ''}`;
}

async function refreshRecentStatus(scope) {
  const box = $('#recent-status');
  if (!box || simState.dataset !== 'recent') return;
  try {
    const status = await api(`/api/simulate/recent/status?days=${simState.days}&sports=${simState.sports.join(',')}`);
    if (!scope.alive) return;
    box.innerHTML = recentStatusView(status);
    hydrate(box);
    if (status.status === 'running') pollRecent(scope);
  } catch (error) {
    if (scope.alive) box.innerHTML = `<p class="small muted">Starea zilelor recente nu este disponibilă: ${esc(error.message)}</p>`;
  }
}

async function prepareRecent(scope) {
  let planned = null;
  try {
    const status = await api(`/api/simulate/recent/status?days=${simState.days}&sports=${simState.sports.join(',')}`);
    planned = isNum(status.planned) ? status.planned : null;
  } catch { /* the question stays generic */ }
  if (!scope.alive) return;
  if (planned === 0) toast('Zilele cerute sunt deja încărcate.', 'success');
  const cost = planned == null ? 'Fiecare zi nouă costă o cerere FlashScore' : `Se folosesc până la ${plural(planned, 'cerere', 'cereri')} FlashScore`;
  const ok = planned === 0 || await confirmDialog({
    title: 'Pregătești ultimele zile?',
    text: `Încarc rezultatele pentru ${plural(simState.days, 'zi', 'zile')} × ${plural(simState.sports.length, 'sport', 'sporturi')} (plus 14 zile pentru formă). ${cost}; zilele deja salvate sunt sărite.`,
    ok: 'Pregătește',
  });
  if (!ok) return;
  try {
    const status = await post('/api/simulate/recent/prepare', {days: simState.days, sports: simState.sports});
    if (!scope.alive) return;
    $('#recent-status').innerHTML = recentStatusView(status);
    hydrate($('#recent-status'));
    pollRecent(scope);
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function pollRecent(scope) {
  if (simState.polling) return;
  simState.polling = true;
  const button = $('#sim-prepare');
  if (button) button.disabled = true;
  try {
    while (scope.alive) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (!scope.alive) break;
      const status = await api(`/api/simulate/recent/status?days=${simState.days}&sports=${simState.sports.join(',')}`);
      if (!scope.alive) break;
      $('#recent-status').innerHTML = recentStatusView(status);
      hydrate($('#recent-status'));
      if (status.status !== 'running') {
        toast(status.message || 'Zilele recente sunt pregătite.', status.status === 'failed' ? 'error' : 'success');
        break;
      }
    }
  } catch (error) {
    if (scope.alive) toast(error.message, 'error');
  } finally {
    simState.polling = false;
    const again = $('#sim-prepare');
    if (again) again.disabled = false;
  }
}

function simulationRequest() {
  const bankroll = Number($('#sim-bankroll').value);
  if (!(bankroll > 0)) throw new Error('Suma de pornire trebuie să fie pozitivă.');
  const body = {bankroll, dataset: simState.dataset};
  if (simState.dataset === 'recent') {
    body.sports = simState.sports;
    body.days = simState.days;
  } else {
    if ($('#sim-start').value) body.start = $('#sim-start').value;
    if ($('#sim-end').value) body.end = $('#sim-end').value;
  }
  const s = simState.strategy;
  if (s === 'ladder' || s === 'ticket') {
    const target = Number($('#sim-target').value);
    if (!(target >= 1.2 && target <= 100)) throw new Error('Cota țintă trebuie să fie între 1.2 și 100.');
    body.target_odds = target;
  }
  if (s === 'ladder') {
    body.strategy = 'ladder';
    body.reinvest = Number($('#sim-reinvest').value) / 100;
    body.restart_on_loss = $('#sim-restart').checked;
    const maxDays = Number($('#sim-maxdays').value);
    if (maxDays) body.max_days = Math.round(maxDays);
    return body;
  }
  const staking = $('#sim-staking').value;
  body.mode = s;
  body.strategy = staking;
  body.staking = staking;
  const raw = $('#sim-stake').value;
  if (raw !== '') {
    const value = Number(raw);
    body.stake = staking === 'percent' ? value / 100 : value;
  }
  if (s !== 'ticket') body.max_bets_per_day = Math.max(1, Math.min(20, Math.round(Number($('#sim-perday').value) || 3)));
  return body;
}

async function runSimulation(scope) {
  let body;
  try {
    body = simulationRequest();
  } catch (error) {
    toast(error.message, 'error');
    return;
  }
  persist('sim.bankroll', body.bankroll);
  const out = $('#sim-output');
  const button = $('#sim-run');
  button.disabled = true;
  out.innerHTML = `<div class="card">${loadingBlock('Se simulează zi cu zi… Prima rulare pe un set de date calculează predicțiile.')}</div>`;
  try {
    const result = await post('/api/simulate', body);
    if (!scope.alive) return;
    simState.result = result;
    simState.shown = TIMELINE_PAGE;
    drawSimulation(result);
    out.scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    if (!scope.alive) return;
    out.innerHTML = errorState(error);
  } finally {
    button.disabled = false;
  }
}

function drawSimulation(r) {
  const out = $('#sim-output');
  const ladder = r.ladder;
  const hit = isNum(r.hit_rate) ? pct(r.hit_rate, 1) : '—';
  // A ladder restarts with fresh money after each loss: its honest numbers are the money put in,
  // the money taken out and the difference (initial + net can go below zero, a drawdown is
  // always 100%), so those replace the bankroll KPIs.
  const general = ladder ? `<div class="kpi-grid kpi-5">
      ${kpi('Câștig net', signedMoney(ladder.net), 'returnat − investit', toneOf(ladder.net))}
      ${kpi('Total investit', money(ladder.total_invested), `${plural(ladder.ladders?.length || 0, 'scară', 'scări')} × ${money(r.initial)}`)}
      ${kpi('Total returnat', money(ladder.total_returned), `ROI ${signedPct(r.roi, 1)} față de investit`, toneOf(r.roi))}
      ${kpi('Bilete', esc(r.bets ?? 0), isNum(r.avg_odds) ? `cotă medie ${num(r.avg_odds)}` : '')}
      ${kpi('Rată de câștig', hit, `${esc(r.won ?? 0)} câștigate · ${esc(r.lost ?? 0)} pierdute · ${esc(r.void ?? 0)} anulate`)}
    </div>` : `<div class="kpi-grid kpi-6">
      ${kpi('Sold final', money(r.final), `din ${money(r.initial)}`)}
      ${kpi('Profit', signedMoney(r.profit), '', toneOf(r.profit))}
      ${kpi('ROI', signedPct(r.roi, 1), ladder ? 'față de banii investiți' : 'față de total mizat', toneOf(r.roi))}
      ${kpi('Rată de câștig', hit, `${esc(r.won ?? 0)} câștigate · ${esc(r.lost ?? 0)} pierdute · ${esc(r.void ?? 0)} anulate`)}
      ${kpi('Scădere maximă', pct(r.max_drawdown, 1), 'de la vârf')}
      ${kpi('Pariuri', esc(r.bets ?? 0), isNum(r.avg_odds) ? `cotă medie ${num(r.avg_odds)}` : '')}
    </div>`;
  const ladderBlock = ladder ? `
    <div class="ladder-hero card">
      <div class="lh-main"><small>Prima scară</small><b>A ținut ${esc(plural(ladder.first_run_days, 'zi', 'zile'))} la rând</b><span>vârf ${money(ladder.first_run_peak)}${ladder.first_run_status ? ` · ${esc(LADDER_STATUS[ladder.first_run_status] || ladder.first_run_status)}` : ''}</span></div>
      <div class="kpi-grid kpi-4">
        ${kpi('Cea mai lungă serie', esc(plural(ladder.longest_streak, 'zi', 'zile')), `vârf ${money(ladder.longest_streak_peak)}`)}
        ${kpi('Reporniri', esc(ladder.restarts), `${esc(plural(ladder.lost_ladders ?? 0, 'scară pierdută', 'scări pierdute'))}${ladder.cashed_ladders ? ` · ${esc(ladder.cashed_ladders)} încasate` : ''}`)}
        ${kpi('Zile fără bilet', esc(ladder.days_without_ticket), 'nicio combinație la cota țintă')}
        ${kpi('Cel mai mare vârf', money(ladder.best_peak), 'soldul maxim al unei scări')}
      </div>
      <p class="small muted">O zi „ținută” este un bilet câștigat sau anulat (miza returnată).</p>
    </div>` : '';
  const b = r.baseline;
  const baseline = b ? `<div class="card panel">
      <h2 class="panel-title">Comparație cu ${esc((b.label || 'favoritul casei').toLowerCase())}</h2>
      <div class="table-wrap"><table class="table compact"><thead><tr><th></th><th class="num">${ladder ? 'Investit' : 'Sold final'}</th><th class="num">${ladder ? 'Câștig net' : 'Profit'}</th><th class="num">ROI</th><th class="num">Rată câștig</th><th class="num">Pariuri</th></tr></thead>
      <tbody><tr><td><b>Strategia AI</b></td><td class="num">${money(ladder ? ladder.total_invested : r.final)}</td><td class="num ${toneOf(r.profit)}">${signedMoney(r.profit)}</td><td class="num">${signedPct(r.roi, 1)}</td><td class="num">${hit}</td><td class="num">${esc(r.bets)}</td></tr>
      <tr><td>${esc(b.label || 'Favoritul casei')}</td><td class="num">${money(ladder && b.ladder ? b.ladder.total_invested : b.final)}</td><td class="num ${toneOf(b.profit)}">${signedMoney(b.profit)}</td><td class="num">${signedPct(b.roi, 1)}</td><td class="num">${isNum(b.hit_rate) ? pct(b.hit_rate, 1) : '—'}</td><td class="num">${esc(b.bets)}</td></tr></tbody></table></div>
      <p class="small ${isNum(b.profit) && isNum(r.profit) && b.profit > r.profit ? 'neg' : 'muted'}">${isNum(b.profit) && isNum(r.profit) ? (b.profit > r.profit ? 'Pe această perioadă, simplul pariu pe favoritul casei a mers mai bine decât strategia AI.' : 'Pe această perioadă, strategia AI a mers mai bine decât favoritul casei.') : ''}</p>
    </div>` : '';
  const ladders = ladder?.ladders?.length ? `<div class="card panel"><h2 class="panel-title">Scările (${esc(ladder.ladders.length)})</h2><div class="table-wrap scroll-box"><table class="table compact"><thead><tr><th>#</th><th>Început</th><th>Sfârșit</th><th class="num">Zile</th><th class="num">Vârf</th><th class="num">Final</th><th>Stare</th></tr></thead>
      <tbody>${ladder.ladders.map((l, i) => `<tr><td>${esc(l.index ?? i + 1)}</td><td>${esc(l.start ? fmtDayMonth(l.start) : '—')}</td><td>${esc(l.end ? fmtDayMonth(l.end) : '—')}</td><td class="num">${esc(l.days)}</td><td class="num">${money(l.peak)}</td><td class="num">${money(l.final)}</td><td><span class="badge badge-${l.status === 'lost' ? 'lost' : l.status === 'cashed' ? 'won' : 'pending'}">${esc(LADDER_STATUS[l.status] || l.status)}</span></td></tr>`).join('')}</tbody></table></div></div>` : '';
  out.innerHTML = `
    ${warningsBox(r.warnings, 'Citește înainte de a trage concluzii')}
    ${r.stopped ? stoppedNote(r) : ''}
    <div class="section-head"><div><h2>Rezultat: ${esc(r.dataset?.label || r.dataset?.id || simState.dataset)}</h2><p class="muted small">${esc(r.start ? fmtDayMonth(r.start) : '')} – ${esc(r.end ? fmtDayMonth(r.end) : '')} · ${esc(STRATEGIES.find(s => s[0] === (ladder ? 'ladder' : r.mode))?.[1] || r.mode)}${isNum(r.target_odds) ? ` · cotă țintă ${num(r.target_odds)}` : ''}</p></div></div>
    ${ladderBlock}
    ${general}
    <div class="card panel"><h2 class="panel-title">${ladder ? 'Câștig net cumulat' : 'Evoluția soldului'}</h2><div id="equity-chart" class="chart-box"></div>
      <div class="legend">${ladder ? '<span class="lg lg-0">Câștig net (returnat − investit)</span><span class="lg lg-1">Soldul scării curente</span><span class="lg lg-ref">Zero</span>' : '<span class="lg lg-0">Sold</span><span class="lg lg-ref">Suma de pornire</span>'}</div></div>
    <div class="grid grid-2">${baseline}${ladders}</div>
    <div class="section-head"><div><h2>Zi cu zi</h2><p class="muted small" id="timeline-count"></p></div></div>
    <div id="timeline" class="timeline"></div>
    <div class="load-more"><button id="timeline-more" class="btn btn-secondary" type="button" hidden>Arată mai multe zile</button></div>
    ${r.method ? `<details class="card panel method-note"><summary>Cum a fost simulat</summary><p>${esc(r.method)}</p>${r.rules ? `<p class="small muted">Reguli: cote selecții ${esc((r.rules.leg_odds || []).join('–'))}, probabilitate × cotă ${isNum(r.rules.max_value) ? `între ${esc(r.rules.min_value)} și ${esc(r.rules.max_value)}` : `≥ ${esc(r.rules.min_value)}`}, interval bilet ${esc((r.rules.window || []).join('–'))} × cota țintă.</p>` : ''}${r.cache ? `<p class="small muted">Predicții: ${esc(r.cache.units)} unități, ${esc(r.cache.computed)} calculate acum, ${num(r.cache.seconds, 1)} s.</p>` : ''}</details>` : ''}
`;
  $('#sim-disclaimer').innerHTML = disclaimerBox(r.disclaimer || 'Simulare cu bani virtuali. 18+.');
  const history = r.equity || r.history || [];
  const series = ladder
    ? [{name: 'Câștig net', cls: 's0', points: history.map(p => ({x: p.date, y: isNum(p.net) ? p.net : (isNum(p.value) ? p.value - r.initial : null)}))},
      {name: 'Scara', cls: 's1', points: history.map(p => ({x: p.date, y: p.bankroll}))}]
    : [{name: 'Sold', cls: 's0', points: history.map(p => ({x: p.date, y: p.bankroll}))}];
  lineChart($('#equity-chart'), series, ladder
    ? {reference: 0, referenceLabel: 'zero', yFormat: v => money(v).replace(' RON', ''), label: 'Câștigul net cumulat al scării'}
    : {reference: r.initial, referenceLabel: `pornire ${money(r.initial)}`, yFormat: v => money(v).replace(' RON', ''), label: 'Evoluția soldului în simulare'});
  hydrate(out);
  drawTimeline();
  $('#timeline-more').addEventListener('click', () => { simState.shown += TIMELINE_PAGE; drawTimeline(); });
}

// Why the run stopped: an exhausted bankroll, or a ladder that lost with restarts disabled
// (then money can be left: reinvest < 1, or earlier cash-outs).
function stoppedNote(r) {
  const ladder = r.ladder;
  const when = fmtDayMonth(r.stopped);
  const text = ladder
    ? `Scara s-a încheiat pe ${when} după un bilet pierdut; repornirea este dezactivată. Ai recuperat ${money(ladder.total_returned)} din ${money(ladder.total_invested)} investiți (net ${signedMoney(ladder.net)}).`
    : `Simularea s-a oprit pe ${when}: banii s-au terminat.`;
  return `<div class="callout callout-danger" role="note"><div class="callout-icon">${icon('warn')}</div><div><b>${esc(text)}</b></div></div>`;
}

function timelineEntries(r) {
  if (Array.isArray(r.days)) return r.days;
  return (r.rows || []).map(row => ({
    date: row.date, ticket: {legs: row.legs || [], total_odds: row.odds, probability: row.probability},
    stake: row.stake, result: row.result, bankroll_after: row.bankroll_after ?? row.bankroll, payout: row.payout,
  }));
}

function drawTimeline() {
  const r = simState.result;
  const box = $('#timeline');
  if (!r || !box) return;
  const entries = timelineEntries(r);
  const shown = entries.slice(0, simState.shown);
  $('#timeline-count').textContent = entries.length
    ? `${shown.length} din ${entries.length} ${Array.isArray(r.days) ? 'zile' : 'pariuri'}${!Array.isArray(r.days) && r.rows_total > entries.length ? ` (ultimele ${entries.length} din ${r.rows_total})` : ''}`
    : '';
  $('#timeline-more').hidden = shown.length >= entries.length;
  box.innerHTML = shown.length ? shown.map(dayCard).join('') : emptyState('Niciun pariu în această perioadă.', 'Încearcă altă perioadă, altă cotă sau mai multe sporturi.');
  hydrate(box);
}

function dayCard(entry) {
  const result = entry.result || 'pending';
  const where = isNum(entry.ladder_index) ? `<span class="muted small">Scara #${esc(entry.ladder_index)}${entry.streak_day ? ` · ziua ${esc(entry.streak_day)}` : ''}</span>` : '';
  if (!entry.ticket || result === 'skipped') {
    return `<article class="day-card day-skipped"><header><time datetime="${esc(entry.date)}">${esc(fmtLongDay(entry.date))}</time>${where}${statusBadge('skipped')}</header><p class="small muted">${esc(entry.reason || 'Nicio combinație la cota țintă în această zi.')}</p></article>`;
  }
  const legs = entry.ticket.legs || [];
  return `<article class="card day-card day-${esc(result)}">
    <header><time datetime="${esc(entry.date)}">${esc(fmtLongDay(entry.date))}</time>${where}${statusBadge(result)}</header>
    <div class="day-money"><span>Miză <b>${money(entry.stake)}</b></span><span>Cotă <b>${num(entry.ticket.total_odds ?? entry.odds)}</b></span>${isNum(entry.ticket.probability) ? `<span>Șansă <b>${pct(entry.ticket.probability)}</b></span>` : ''}<span>Sold după <b>${money(entry.bankroll_after)}</b></span></div>
    <ul class="day-legs">${legs.map(leg => {
      const status = leg.result || leg.status || 'pending';
      return `<li class="dl dl-${esc(status)}">
        <span class="dl-teams">${crest(leg.home_logo, leg.home, 'xs')}<span>${esc(leg.home)}</span><span class="muted">–</span>${crest(leg.away_logo, leg.away, 'xs')}<span>${esc(leg.away)}</span></span>
        <span class="dl-pick">${esc(leg.market || leg.label)} <span class="muted">@ ${num(leg.odds)} · ${pct(leg.probability)}</span></span>
        <span class="dl-res">${leg.score ? `<b>${esc(leg.score)}</b>` : ''}${statusBadge(status)}</span>
      </li>`;
    }).join('')}</ul>
  </article>`;
}
