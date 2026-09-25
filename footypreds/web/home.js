'use strict';
// Home (#/): the day's AI tickets x2/x5/x10/x100, the safest singles, a "Live acum" strip and
// the disclaimer. GET /api/recommendations (stored per day; "Regenerează" adds refresh=true).

const homeState = {offset: [0, 1, 2].includes(remember('home.offset', 0)) ? remember('home.offset', 0) : 0};
const HOME_TARGETS = [2, 5, 10, 100];

function renderHome() {
  const scope = currentScope;
  const app = $('#app');
  const day = isoDay(homeState.offset);
  const sports = chosenSports();
  app.innerHTML = `
    <section class="hero hero-home">
      <div class="hero-text">
        <p class="eyebrow">${icon('spark')} Bilete AI · ${esc(fmtLongDay(day))}</p>
        <h1>Cele mai probabile bilete pentru cota ta</h1>
        <p class="lead">Pentru fiecare cotă țintă, modelul alege automat combinația de selecții cu cea mai mare probabilitate estimată, din ${esc(sports.map(s => SPORT_LABEL[s].toLowerCase()).join(', '))}.</p>
      </div>
      <div class="hero-controls">
        <div class="chip-row" role="group" aria-label="Ziua">${chips([[0, 'Azi'], [1, 'Mâine'], [2, esc(dayChipLabel(2))]], homeState.offset, 'day')}</div>
        <div class="chip-row" role="group" aria-label="Sport">${chips([['all', `${icon('all')}Toate`], ...SPORT_KEYS.map(s => [s, `${icon(s)}${esc(SPORT_LABEL[s])}`])], state.sport, 'sport-filter', 'chip-sport')}</div>
        <button id="regen" class="btn btn-ghost" type="button">${icon('refresh')}Regenerează</button>
      </div>
    </section>
    <div id="reco-warnings"></div>
    <section class="section" aria-labelledby="tickets-title">
      <div class="section-head"><div><h2 id="tickets-title">Biletele zilei</h2><p id="reco-meta" class="muted small">Se calculează biletele…</p></div></div>
      <div id="tickets" class="ticket-grid" aria-busy="true">${skeletonCards(4, 'tall')}</div>
    </section>
    <section class="section" aria-labelledby="singles-title">
      <div class="section-head"><div><h2 id="singles-title">Cele mai sigure selecții</h2><p class="muted small">Câte una pe meci, cotă de cel puțin 1.20, ordonate după probabilitate.</p></div></div>
      <div id="singles" class="singles-grid">${skeletonCards(3)}</div>
    </section>
    <section class="section" aria-labelledby="live-title">
      <div class="section-head"><div><h2 id="live-title"><span class="live-dot" aria-hidden="true"></span>Live acum</h2><p class="muted small">Probabilități în timp real; cotele afișate sunt de dinainte de meci.</p></div><a class="btn btn-ghost btn-small" href="#/live">Toate meciurile live${icon('chevron')}</a></div>
      <div id="live-strip" class="live-strip">${skeletonCards(3, 'mini')}</div>
    </section>
    <div id="reco-disclaimer">${disclaimerBox()}</div>`;
  $$('[data-day]', app).forEach(b => b.addEventListener('click', () => {
    homeState.offset = Number(b.dataset.day);
    persist('home.offset', homeState.offset);
    renderHome();
  }));
  $$('[data-sport-filter]', app).forEach(b => b.addEventListener('click', () => setSport(b.dataset.sportFilter)));
  $('#regen').addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: 'Regenerezi biletele?',
      text: 'Regenerarea reîncarcă meciurile și poate folosi cereri FlashScore din cota ta limitată. Biletele cu meciuri deja începute rămân blocate, ca istoricul să nu poată fi rescris.',
      ok: 'Regenerează',
    });
    if (ok) loadRecommendations(scope, day, sports, true);
  });
  loadRecommendations(scope, day, sports, false);
  loadLiveStrip(scope, sports);
}

async function loadRecommendations(scope, day, sports, refresh) {
  const box = $('#tickets');
  box.setAttribute('aria-busy', 'true');
  if (refresh) box.innerHTML = skeletonCards(4, 'tall');
  const params = new URLSearchParams({day, sports: sports.join(','), targets: HOME_TARGETS.join(',')});
  if (refresh) params.set('refresh', 'true');
  let data;
  try {
    data = await api(`/api/recommendations?${params}`);
  } catch (error) {
    if (!scope.alive) return;
    box.setAttribute('aria-busy', 'false');
    box.innerHTML = errorState(error, 'reco-retry');
    $('#singles').innerHTML = '';
    $('#reco-meta').textContent = '';
    $('#reco-retry')?.addEventListener('click', () => loadRecommendations(scope, day, sports, false));
    return;
  }
  if (!scope.alive) return;
  box.setAttribute('aria-busy', 'false');
  if (refresh) toast('Biletele au fost regenerate.', 'success');
  const analyzed = Object.entries(data.analyzed || {}).map(([s, n]) => `${SPORT_LABEL[s] || s} ${n}`).join(' · ');
  $('#reco-meta').textContent = `${data.generated_at ? `Generat ${fmtDateTime(data.generated_at)} · ` : ''}meciuri analizate: ${analyzed || '—'}`;
  $('#reco-warnings').innerHTML = warningsBox(data.warnings, 'Avertismente la generare');
  $('#reco-disclaimer').innerHTML = disclaimerBox(data.disclaimer);
  const tickets = data.tickets || [];
  box.innerHTML = tickets.length ? tickets.map((ticket, i) => ticketCard(ticket, {
    id: `ticket-${i}`,
    actions: ticket.status === 'pending' && ticket.legs?.length ? betForm(ticket, i) : '',
  })).join('') : emptyState('Niciun bilet pentru această zi.', 'Alege altă zi sau mai multe sporturi.');
  hydrate(box);
  $$('.bet-form', box).forEach(form => bindBetForm(form, stake => post('/api/wallet/bet', {stake, day, target: Number(form.dataset.target), sports})));
  renderSingles(data.singles || []);
}

function betForm(ticket, index) {
  const stake = remember('stake', 10);
  return `<form class="bet-form" data-target="${esc(ticket.target ?? ticket.target_odds)}" data-odds="${esc(ticket.total_odds)}">
      <label class="stake"><span>Miză</span><input type="number" name="stake" min="0.01" step="0.01" required value="${esc(stake)}" inputmode="decimal" aria-label="Miză în RON pentru biletul ${index + 1}"><span>RON</span></label>
      <button class="btn btn-primary" type="submit">${icon('wallet')}Joacă în portofelul virtual</button>
      <span class="potential" aria-live="polite">Câștig posibil <b>${money(stake * (ticket.total_odds || 0))}</b></span>
    </form>`;
}

// Inline stake form: live potential payout and a POST through `place(stake)`.
function bindBetForm(form, place) {
  const input = form.querySelector('input');
  const out = form.querySelector('.potential b');
  const odds = Number(form.dataset.odds) || 0;
  input.addEventListener('input', () => { if (out) out.textContent = money((Number(input.value) || 0) * odds); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const stake = Number(input.value);
    if (!(stake > 0)) { toast('Introdu o miză pozitivă.', 'error'); return; }
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const wallet = await place(stake);
      persist('stake', stake);
      toast(`Pariu virtual plasat. Sold: ${money(wallet.balance, wallet.currency)}.`, 'success', {href: '#/portofel', label: 'Vezi portofelul'});
    } catch (error) {
      toast(error.message, 'error', error.status === 400 && /Sold/.test(error.message) ? {href: '#/portofel', label: 'Depune bani virtuali'} : null);
    } finally {
      button.disabled = false;
    }
  });
}

function renderSingles(singles) {
  const box = $('#singles');
  if (!box) return;
  if (!singles.length) {
    box.innerHTML = emptyState('Nicio selecție sigură încă.', 'Apar când există meciuri viitoare cu cote reale și date suficiente (grad A–C).');
    return;
  }
  box.innerHTML = singles.map((leg, i) => `<article class="card single sport-${esc(leg.sport)}">
      <ol class="legs">${legRow(leg)}</ol>
      <div class="single-actions"><span class="muted small">#${i + 1} după probabilitate</span><button class="btn btn-secondary btn-small" type="button" data-single="${i}">${icon('wallet')}Joacă virtual</button></div>
    </article>`).join('');
  hydrate(box);
  $$('[data-single]', box).forEach(button => button.addEventListener('click', async () => {
    const leg = singles[Number(button.dataset.single)];
    const stake = await stakeDialog({title: 'Pariu virtual pe o selecție', text: `${leg.home} – ${leg.away}: ${leg.label} la cota ${num(leg.odds)} (${pct(leg.probability)} estimat).`});
    if (stake == null) return;
    try {
      const wallet = await post('/api/wallet/bet', {stake, legs: [{match_id: leg.match_id, key: leg.key}], label: `${leg.home} – ${leg.away}: ${leg.label}`});
      toast(`Pariu virtual plasat. Sold: ${money(wallet.balance, wallet.currency)}.`, 'success', {href: '#/portofel', label: 'Vezi portofelul'});
    } catch (error) {
      toast(error.message, 'error');
    }
  }));
}

async function loadLiveStrip(scope, sports) {
  const results = await Promise.allSettled(sports.map(sport => api(`/api/live?sport=${sport}`)));
  if (!scope.alive) return;
  const box = $('#live-strip');
  const items = results.flatMap(r => (r.status === 'fulfilled' ? r.value.matches || [] : []));
  const failed = results.find(r => r.status === 'rejected');
  if (!items.length) {
    box.innerHTML = failed && results.every(r => r.status === 'rejected')
      ? `<p class="muted small strip-note">Live indisponibil: ${esc(failed.reason.message)}</p>`
      : '<p class="muted small strip-note">Niciun meci live acum pentru sporturile alese.</p>';
    return;
  }
  box.innerHTML = items.slice(0, 14).map(liveMini).join('');
  hydrate(box);
}

function liveMini(item) {
  const m = item.match;
  const p = item.probabilities || {};
  const parts = item.sport === 'football'
    ? [{label: '1', value: p['1']}, {label: 'X', value: p.X}, {label: '2', value: p['2']}]
    : [{label: '1', value: p['1']}, {label: '2', value: p['2']}];
  return `<a class="card live-mini sport-${esc(item.sport)}" href="#/live" aria-label="${esc(m.home)} – ${esc(m.away)}, live">
      <div class="lm-top">${sportIcon(item.sport)}<span class="lm-league">${esc(item.competition || leagueName(m.league))}</span><span class="live-pill">${esc(periodLabel(item))}</span></div>
      <div class="lm-row">${crest(m.home_logo, m.home, 'sm')}<span class="team-name">${esc(m.home)}</span><b>${esc(item.score?.home ?? m.home_goals ?? '')}</b></div>
      <div class="lm-row">${crest(m.away_logo, m.away, 'sm')}<span class="team-name">${esc(m.away)}</span><b>${esc(item.score?.away ?? m.away_goals ?? '')}</b></div>
      <div class="mini-strip">${parts.map((part, i) => `<span class="seg seg-${i}" data-w="${clamp01(part.value)}" title="${esc(part.label)}: ${pct(part.value)}"></span>`).join('')}</div>
      <div class="mini-legend">${parts.map(part => `<span>${esc(part.label)} <b>${pct(part.value)}</b></span>`).join('')}</div>
    </a>`;
}
