'use strict';
// Router, header (navigation, sport switcher, server status) and boot. Loaded last: every
// page renderer (render*) comes from the page scripts listed before it in index.html.

// Shared UI state. state.boardRequest is the generation of the daily board (#/meciuri): every
// reload bumps it, and a slower answer for a previous day or filter never overwrites a newer
// board (board.js compares it before painting).
const state = {
  sport: SPORT_KEYS.includes(remember('sport', 'all')) ? remember('sport', 'all') : 'all',
  health: null,
  boardRequest: 0,
};

// Sports chosen in the header: 'all' means every sport, otherwise that one only.
const chosenSports = () => (state.sport === 'all' ? [...SPORT_KEYS] : [state.sport]);

function parseRoute() {
  const raw = window.location.hash.replace(/^#/, '') || '/';
  const [path, query = ''] = raw.split('?');
  const params = new URLSearchParams(query);
  const match = path.match(/^\/meci\/(.+)$/);
  if (match) return {name: 'match', id: decodeURIComponent(match[1]), sport: params.get('sport') || '', params};
  const names = {
    '/': 'home', '/meciuri': 'board', '/live': 'live', '/bilete': 'tickets', '/simulator': 'simulator',
    '/portofel': 'wallet', '/rezultate': 'record', '/metoda': 'method',
  };
  return {name: names[path.replace(/\/+$/, '') || '/'] || 'home', params};
}

const PAGES = {
  home: () => renderHome(),
  board: () => renderBoard(),
  match: current => renderMatch(current.id, current.sport),
  live: current => renderLive(current.params),
  tickets: () => renderTickets(),
  simulator: () => renderSimulator(),
  wallet: () => renderWallet(),
  record: () => renderRecord(),
  method: () => renderMethod(),
};

const TITLES = {
  home: 'Bilete AI', board: 'Meciuri', match: 'Meci', live: 'Live', tickets: 'Generator bilete',
  simulator: 'Simulator', wallet: 'Portofel virtual', record: 'Rezultate', method: 'Metodă',
};

function route({keepScroll = false} = {}) {
  const current = parseRoute();
  newScope();
  closeOverlays();
  const nav = current.name === 'match' ? 'board' : current.name;
  $$('.nav a').forEach(a => {
    const active = a.dataset.route === nav;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.body.dataset.page = current.name;
  document.title = `${TITLES[current.name] || 'FootyPreds'} · FootyPreds`;
  const app = $('#app');
  app.innerHTML = '';
  try {
    PAGES[current.name](current);
  } catch (error) {
    app.innerHTML = errorState(error);
  }
  if (!keepScroll) window.scrollTo(0, 0);
}

function paintSwitch() {
  $$('[data-switch]').forEach(button => {
    const active = button.dataset.switch === state.sport;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  document.body.dataset.sport = state.sport;
}

function setSport(sport) {
  if (!(sport === 'all' || SPORT_KEYS.includes(sport)) || sport === state.sport) return;
  state.sport = sport;
  persist('sport', sport);
  paintSwitch();
  if (parseRoute().name !== 'match') route({keepScroll: true});
}

async function loadHealth() {
  const box = $('#server-status');
  try {
    const health = await api('/api/health');
    state.health = health;
    box.className = `server-status ${health.api_configured ? 'ok' : 'warn'}`;
    const counts = health.history_by_sport || {};
    const total = Object.values(counts).reduce((a, b) => a + (b || 0), 0) || health.history_matches || 0;
    $('.status-text', box).textContent = health.api_configured ? `${total.toLocaleString('ro-RO')} rezultate` : 'Fără cheie API';
    box.title = health.api_configured
      ? `Server pornit · istoric: ${SPORT_KEYS.map(s => `${SPORT_LABEL[s]} ${counts[s] ?? 0}`).join(', ')}`
      : 'Lipsește RAPIDAPI_KEY în .env: meciurile noi nu pot fi încărcate.';
  } catch {
    box.className = 'server-status down';
    $('.status-text', box).textContent = 'Server oprit';
    box.title = 'Serverul nu răspunde.';
  }
}

async function loadSports() {
  try {
    const {sports} = await api('/api/sports');
    sports.forEach(s => { SPORT_LABEL[s.key] = s.label; });
  } catch { /* labels have Romanian defaults */ }
}

window.addEventListener('hashchange', () => route());
$$('[data-switch]').forEach(button => {
  const label = button.textContent.trim();
  button.innerHTML = `${icon(button.dataset.switch)}<span class="sw-label">${esc(label)}</span>`;
  button.setAttribute('title', label);
  button.addEventListener('click', () => setSport(button.dataset.switch));
});
paintSwitch();
route();
loadSports();
loadHealth();
