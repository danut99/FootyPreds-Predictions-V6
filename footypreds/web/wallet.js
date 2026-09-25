'use strict';
// Virtual wallet (#/portofel): balance, deposits, reset, open and settled bets, P&L chart.
// Bets settle on every read (GET /api/wallet). Virtual money only.

const walletState = {tab: 'open'};
const LEDGER_TYPES = {deposit: 'Depunere', bet: 'Pariu', payout: 'Plată', reset: 'Resetare', refund: 'Rambursare'};

function renderWallet() {
  const scope = currentScope;
  const app = $('#app');
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">${icon('wallet')} Portofel virtual</p>
        <h1>Joacă biletele fără bani reali</h1>
        <p class="lead">Depui bani fictivi, joci biletele AI sau selecțiile tale, iar pariurile se decontează automat după rezultatele finale.</p>
      </div>
    </section>
    <div id="wallet-body">${skeletonCards(3)}</div>`;
  loadWallet(scope);
}

async function loadWallet(scope, wallet) {
  const body = $('#wallet-body');
  try {
    wallet = wallet || await api('/api/wallet');
  } catch (error) {
    if (!scope.alive) return;
    body.innerHTML = errorState(error, 'wallet-retry');
    $('#wallet-retry')?.addEventListener('click', () => loadWallet(scope));
    return;
  }
  if (!scope.alive) return;
  drawWallet(scope, wallet);
}

function drawWallet(scope, w) {
  const body = $('#wallet-body');
  const sports = new Set(chosenSports());
  const bets = (w.bets || []).filter(b => sports.size === 3 || (b.legs || []).some(l => sports.has(l.sport)));
  const open = bets.filter(b => b.status === 'pending');
  const settled = bets.filter(b => b.status !== 'pending');
  const list = walletState.tab === 'open' ? open : settled;
  const currency = w.currency || 'RON';
  body.innerHTML = `
    <div class="kpi-grid kpi-4 wallet-kpis">
      ${kpi('Sold disponibil', money(w.balance, currency), `depus ${money(w.deposited, currency)}`)}
      ${kpi('În joc', money(w.staked_open, currency), `${esc(w.open ?? open.length)} pariuri deschise`)}
      ${kpi('Profit', signedMoney(w.profit, currency), 'pe pariurile decontate', toneOf(w.profit))}
      ${kpi('Bilanț', `${esc(w.won ?? 0)} / ${esc(w.lost ?? 0)} / ${esc(w.void ?? 0)}`, 'câștigate / pierdute / anulate')}
    </div>
    <div class="grid grid-2-1">
      <div class="card panel"><h2 class="panel-title">Evoluția soldului</h2><div id="wallet-chart" class="chart-box"></div></div>
      <div class="card panel">
        <h2 class="panel-title">Depune bani virtuali</h2>
        <form id="deposit-form" class="deposit">
          <div class="chip-row" role="group" aria-label="Sume rapide">${[50, 100, 500].map(v => `<button type="button" class="chip" data-amount="${v}">${v} ${esc(currency)}</button>`).join('')}</div>
          <label class="field">Sumă (${esc(currency)})<input id="deposit-amount" type="number" min="0.01" max="1000000" step="0.01" value="100" required inputmode="decimal"></label>
          <button class="btn btn-primary" type="submit">Depune</button>
        </form>
        <hr class="sep">
        <button id="wallet-reset" class="btn btn-danger-ghost" type="button">Resetează portofelul</button>
        <p class="small muted">Resetarea șterge pariurile și soldul virtual.</p>
      </div>
    </div>
    <section class="section">
      <div class="section-head"><h2>Pariurile mele</h2>
        <div class="tabs" role="tablist" aria-label="Pariuri">
          <button type="button" role="tab" class="tab ${walletState.tab === 'open' ? 'active' : ''}" aria-selected="${walletState.tab === 'open'}" data-wtab="open">Deschise <span class="count">${open.length}</span></button>
          <button type="button" role="tab" class="tab ${walletState.tab === 'settled' ? 'active' : ''}" aria-selected="${walletState.tab === 'settled'}" data-wtab="settled">Decontate <span class="count">${settled.length}</span></button>
        </div>
      </div>
      <div class="bet-list">${list.length ? list.map(betCard).join('') : emptyState(walletState.tab === 'open' ? 'Niciun pariu deschis.' : 'Niciun pariu decontat încă.', 'Joacă un bilet AI de pe pagina principală sau generează unul.', '<a class="btn btn-primary" href="#/">Vezi biletele zilei</a>')}</div>
    </section>
    <section class="section">
      <details class="card panel"><summary class="panel-title">Jurnalul portofelului (${esc((w.history || []).length)})</summary>
        <div class="table-wrap"><table class="table compact"><thead><tr><th>Data</th><th>Tip</th><th class="num">Sumă</th><th class="num">Sold</th></tr></thead>
        <tbody>${(w.history || []).map(h => `<tr><td class="small">${esc(fmtDateTime(h.at))}</td><td>${esc(LEDGER_TYPES[h.type] || h.type)}</td><td class="num ${toneOf(h.amount)}">${signedMoney(h.amount, currency)}</td><td class="num">${money(h.balance, currency)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">Nicio operațiune.</td></tr>'}</tbody></table></div>
      </details>
    </section>
    ${disclaimerBox(w.notice || w.disclaimer)}`;
  hydrate(body);
  const points = [...(w.history || [])].reverse().map(h => ({x: h.at, y: h.balance}));
  if (points.length >= 2) lineChart($('#wallet-chart'), [{name: 'Sold', cls: 's0', points}], {yFormat: v => num(v, 0), label: 'Evoluția soldului virtual', reference: w.deposited || undefined, referenceLabel: w.deposited ? 'total depus' : ''});
  else $('#wallet-chart').innerHTML = '<p class="muted small">Graficul apare după primele operațiuni.</p>';
  $$('[data-amount]', body).forEach(b => b.addEventListener('click', () => { $('#deposit-amount').value = b.dataset.amount; }));
  $('#deposit-form').addEventListener('submit', async event => {
    event.preventDefault();
    const amount = Number($('#deposit-amount').value);
    if (!(amount > 0 && amount <= 1000000)) { toast('Suma trebuie să fie între 0.01 și 1.000.000.', 'error'); return; }
    try {
      const wallet = await post('/api/wallet/deposit', {amount});
      toast(`Ai depus ${money(amount, currency)} virtuali.`, 'success');
      if (scope.alive) drawWallet(scope, wallet);
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  $('#wallet-reset').addEventListener('click', async () => {
    const ok = await confirmDialog({title: 'Resetezi portofelul?', text: 'Toate pariurile și soldul virtual vor fi șterse. Acțiunea nu poate fi anulată.', ok: 'Resetează', danger: true});
    if (!ok) return;
    try {
      const wallet = await post('/api/wallet/reset', {});
      toast('Portofelul a fost resetat.', 'success');
      if (scope.alive) drawWallet(scope, wallet);
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  $$('[data-wtab]', body).forEach(b => b.addEventListener('click', () => { walletState.tab = b.dataset.wtab; drawWallet(scope, w); }));
}

function betCard(bet) {
  const potential = isNum(bet.stake) && isNum(bet.total_odds) ? bet.stake * bet.total_odds : null;
  const result = bet.status === 'pending'
    ? `Câștig posibil <b>${money(potential)}</b>`
    : bet.status === 'won' ? `Plătit <b class="pos">${money(bet.payout)}</b>`
      : bet.status === 'void' ? `Rambursat <b>${money(bet.payout ?? bet.stake)}</b>` : `Pierdut <b class="neg">${money(-bet.stake)}</b>`;
  return `<article class="card bet-card bet-${esc(bet.status)}">
    <header class="bet-head">
      <div><h3>${esc(bet.label || 'Pariu personalizat')}</h3><span class="muted small">${esc(fmtDateTime(bet.created))} · ${bet.source === 'ai' ? 'bilet AI' : 'personalizat'}${bet.settled ? ` · decontat ${esc(fmtDateTime(bet.settled))}` : ''}</span></div>
      ${statusBadge(bet.status)}
    </header>
    <div class="bet-money"><span>Miză <b>${money(bet.stake)}</b></span><span>Cotă <b>${num(bet.total_odds)}</b></span><span>${result}</span></div>
    <ol class="legs">${(bet.legs || []).map(leg => legRow(leg, {compact: true, reason: false})).join('')}</ol>
  </article>`;
}
