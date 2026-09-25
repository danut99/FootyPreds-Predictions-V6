'use strict';
// Easy ticket generator (#/bilete): only the target odds, the sports and the day ->
// POST /api/tickets/generate. "Altă variantă" excludes the matches already shown. There is no
// minimum probability anywhere: the optimizer picks the likeliest legs for the odds.

const TARGET_CHOICES = [2, 3, 5, 10, 20, 50, 100];
const ticketState = {
  target: remember('tickets.target', 3),
  offset: 0,
  sports: null,
  exclude: [],
  result: null,
};

function renderTickets() {
  const scope = currentScope;
  const app = $('#app');
  ticketState.sports = chosenSports();
  ticketState.exclude = [];
  ticketState.result = null;
  const custom = !TARGET_CHOICES.includes(Number(ticketState.target));
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">${icon('spark')} Generator de bilete</p>
        <h1>Spune cota, AI-ul face biletul</h1>
        <p class="lead">Alegi doar cota dorită. Modelul caută combinația de selecții cu cea mai mare probabilitate estimată care atinge cota, cu cel mult o selecție pe meci.</p>
      </div>
    </section>
    <form id="gen-form" class="card generator" novalidate>
      <fieldset>
        <legend>Cota țintă</legend>
        <div class="chip-row" role="group" aria-label="Cota țintă">${TARGET_CHOICES.map(t => `<button type="button" class="chip chip-odds ${Number(ticketState.target) === t ? 'active' : ''}" data-target="${t}" aria-pressed="${Number(ticketState.target) === t}">x${t}</button>`).join('')}
          <label class="chip chip-input ${custom ? 'active' : ''}"><span>Altă cotă</span><input id="gen-custom" type="number" min="1.2" max="1000" step="0.1" inputmode="decimal" value="${custom ? esc(ticketState.target) : ''}" placeholder="ex. 7.5" aria-label="Cotă personalizată"></label>
        </div>
      </fieldset>
      <fieldset>
        <legend>Sporturi</legend>
        <div class="chip-row" role="group" aria-label="Sporturi">${SPORT_KEYS.map(s => `<button type="button" class="chip chip-sport ${ticketState.sports.includes(s) ? 'active' : ''}" data-gsport="${s}" aria-pressed="${ticketState.sports.includes(s)}">${icon(s)}${esc(SPORT_LABEL[s])}</button>`).join('')}</div>
      </fieldset>
      <fieldset>
        <legend>Ziua</legend>
        <div class="chip-row" role="group" aria-label="Ziua">${chips([[0, 'Azi'], [1, 'Mâine'], [2, esc(dayChipLabel(2))]], ticketState.offset, 'gday')}</div>
      </fieldset>
      <div class="generator-go">
        <button id="gen-go" class="btn btn-primary btn-large" type="submit">${icon('spark')}Generează biletul</button>
        <p class="muted small">Folosește meciurile deja încărcate; poate completa câteva analize din FlashScore în limita zilnică.</p>
      </div>
    </form>
    <div id="gen-output" class="section" aria-live="polite"></div>
    <div id="gen-disclaimer">${disclaimerBox()}</div>`;
  const form = $('#gen-form');
  const setTarget = value => {
    ticketState.target = value;
    persist('tickets.target', value);
    $$('[data-target]', form).forEach(b => { const on = Number(b.dataset.target) === Number(value); b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
    $('.chip-input', form).classList.toggle('active', !TARGET_CHOICES.includes(Number(value)));
  };
  $$('[data-target]', form).forEach(b => b.addEventListener('click', () => { $('#gen-custom').value = ''; setTarget(Number(b.dataset.target)); }));
  $('#gen-custom').addEventListener('input', e => { const v = Number(e.target.value); if (v >= 1.2 && v <= 1000) setTarget(v); });
  $$('[data-gsport]', form).forEach(b => b.addEventListener('click', () => {
    const sport = b.dataset.gsport;
    const has = ticketState.sports.includes(sport);
    if (has && ticketState.sports.length === 1) { toast('Alege cel puțin un sport.', 'error'); return; }
    ticketState.sports = has ? ticketState.sports.filter(s => s !== sport) : SPORT_KEYS.filter(s => s === sport || ticketState.sports.includes(s));
    b.classList.toggle('active', !has);
    b.setAttribute('aria-pressed', String(!has));
  }));
  $$('[data-gday]', form).forEach(b => b.addEventListener('click', () => {
    ticketState.offset = Number(b.dataset.gday);
    $$('[data-gday]', form).forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); });
  }));
  form.addEventListener('submit', event => {
    event.preventDefault();
    const custom = $('#gen-custom').value;
    if (custom && !(Number(custom) >= 1.2 && Number(custom) <= 1000)) { toast('Cota trebuie să fie între 1.2 și 1000.', 'error'); return; }
    ticketState.exclude = [];
    generateTicket(scope);
  });
}

async function generateTicket(scope) {
  const out = $('#gen-output');
  const button = $('#gen-go');
  button.disabled = true;
  out.innerHTML = `<div class="ticket-grid single-col">${skeletonCards(1, 'tall')}</div>`;
  // On a phone the form fills the screen: bring the result (first the skeleton) into view.
  out.scrollIntoView({behavior: 'smooth', block: 'start'});
  const body = {day: isoDay(ticketState.offset), target_odds: Number(ticketState.target), sports: ticketState.sports};
  if (ticketState.exclude.length) body.exclude_match_ids = ticketState.exclude.slice(-300);
  let data;
  try {
    data = await post('/api/tickets/generate', body);
  } catch (error) {
    if (!scope.alive) return;
    out.innerHTML = errorState(error, 'gen-retry');
    $('#gen-retry')?.addEventListener('click', () => generateTicket(scope));
    return;
  } finally {
    button.disabled = false;
  }
  if (!scope.alive) return;
  ticketState.result = data;
  const tickets = [data.ticket, ...(data.alternatives || [])];
  const playable = t => t.status === 'pending' && t.legs?.length;
  const actions = (t, i) => playable(t) ? `<button class="btn btn-primary" type="button" data-play="${i}">${icon('wallet')}Joacă virtual</button>` : '';
  // Unavailable after "Altă variantă": the excluded matches used up the pool, so offer a reset.
  const exhausted = !playable(data.ticket) && ticketState.exclude.length > 0;
  const more = playable(data.ticket)
    ? `<button class="btn btn-secondary" type="button" id="gen-other">${icon('refresh')}Altă variantă</button>`
    : exhausted ? `<button class="btn btn-secondary" type="button" id="gen-reset">${icon('refresh')}Resetează excluderile</button>` : '';
  out.innerHTML = `
    ${warningsBox(data.warnings, 'Avertismente')}
    <p class="muted small">${esc(plural(data.candidates ?? 0, 'selecție eligibilă', 'selecții eligibile'))} · analizate: ${esc(Object.entries(data.analyzed || {}).map(([s, n]) => `${SPORT_LABEL[s] || s} ${n}`).join(', ') || '—')}${ticketState.exclude.length ? ` · ${esc(plural(ticketState.exclude.length, 'meci exclus', 'meciuri excluse'))}` : ''}</p>
    <div class="ticket-grid single-col">${ticketCard(data.ticket, {
      id: 'generated-ticket',
      actions: `${actions(data.ticket, 0)}${more}`,
    })}</div>
    ${tickets.length > 1 ? `<div class="section-head"><div><h2>Variante pe alte meciuri</h2><p class="muted small">Aceeași cotă, meciuri complet diferite.</p></div></div>
      <div class="ticket-grid">${tickets.slice(1).map((t, i) => ticketCard(t, {title: `Varianta ${i + 2}`, actions: actions(t, i + 1)})).join('')}</div>` : ''}`;
  $('#gen-disclaimer').innerHTML = disclaimerBox(data.disclaimer);
  hydrate(out);
  $('#gen-other')?.addEventListener('click', () => {
    const used = tickets.flatMap(t => (t.legs || []).map(l => l.match_id));
    ticketState.exclude = [...new Set([...ticketState.exclude, ...used])];
    generateTicket(scope);
  });
  $('#gen-reset')?.addEventListener('click', () => {
    ticketState.exclude = [];
    generateTicket(scope);
  });
  $$('[data-play]', out).forEach(b => b.addEventListener('click', () => playTicket(tickets[Number(b.dataset.play)])));
}

async function playTicket(ticket) {
  const stake = await stakeDialog({
    title: 'Joacă biletul în portofelul virtual',
    text: `${plural(ticket.legs.length, 'selecție', 'selecții')}, cotă totală ${num(ticket.total_odds)}, șansă estimată ${pct(ticket.probability)}. Cotele se blochează acum.`,
  });
  if (stake == null) return;
  try {
    const wallet = await placeBet({
      stake,
      legs: ticket.legs.map(l => ({match_id: l.match_id, key: l.key})),
      label: `Bilet generat cota ${num(ticket.target ?? ticket.target_odds, 1)}`,
    });
    if (!wallet) return;
    toast(`Bilet jucat virtual. Sold: ${money(wallet.balance, wallet.currency)}.`, 'success', {href: '#/portofel', label: 'Vezi portofelul'});
  } catch (error) {
    toast(error.message, 'error');
  }
}
