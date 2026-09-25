'use strict';
// FootyPreds web UI: shared helpers, API client, feedback (toasts, dialogs) and the building
// blocks every page uses (crests, badges, legs, ticket cards, charts). Loaded first; the page
// scripts and app.js (router) share these globals. The CSP forbids inline scripts, inline
// style attributes and inline handlers: markup carries data-* hints and hydrate() applies
// widths/colours through the CSSOM and attaches listeners.

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const API_BASE = ['5500', '5501'].includes(window.location.port) || window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : '';

const SPORT_KEYS = ['football', 'basketball', 'tennis'];
const SPORT_LABEL = {football: 'Fotbal', basketball: 'Baschet', tennis: 'Tenis', all: 'Toate'};
const STATUS_LABEL = {
  pending: 'în așteptare', won: 'câștigat', lost: 'pierdut', void: 'anulat',
  unavailable: 'indisponibil', skipped: 'fără bilet', live: 'live', finished: 'final',
  scheduled: 'programat', open: 'deschis',
};
const DISCLAIMER = 'Estimări statistice, nu garanții. 18+. Joacă responsabil.';

// --- small persistence (per browser, optional) ----------------------------------------------

function remember(key, fallback) {
  try {
    const value = window.localStorage.getItem(`fp.${key}`);
    return value == null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function persist(key, value) {
  try { window.localStorage.setItem(`fp.${key}`, JSON.stringify(value)); } catch { /* optional */ }
}

// --- formatting ----------------------------------------------------------------------------

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const isNum = value => typeof value === 'number' && Number.isFinite(value);
// One convention everywhere: amounts and percentages in ro-RO (decimal comma, "12,5%",
// "1.234,50 RON"); odds and other decimals keep the dot, as bookmakers print them ("1.85").
const roFixed = (value, digits) => Number(value).toLocaleString('ro-RO', {minimumFractionDigits: digits, maximumFractionDigits: digits});
const pct = (value, digits = 0) => isNum(value) ? `${roFixed(value * 100, digits)}%` : '—';
const num = (value, digits = 2) => isNum(value) ? Number(value).toFixed(digits) : '—';
const signedPct = (value, digits = 0) => {
  if (!isNum(value)) return '—';
  const rounded = Number((value * 100).toFixed(digits));
  return rounded === 0 ? `${roFixed(0, digits)}%` : `${value > 0 ? '+' : ''}${roFixed(value * 100, digits)}%`;
};
const money = (value, currency = 'RON') => isNum(value) ? `${value.toLocaleString('ro-RO', {minimumFractionDigits: 2, maximumFractionDigits: 2})} ${currency}` : '—';
const signedMoney = (value, currency = 'RON') => isNum(value) ? `${value > 0 ? '+' : ''}${money(value, currency)}` : '—';
const clamp01 = value => Math.max(0, Math.min(1, Number(value) || 0));

// Romanian counts: "1 gol", "2 goluri", "20 de goluri", "101 goluri" (numbers >= 20 whose last
// two digits are 00 or >= 20 take "de").
function plural(count, one, many) {
  const n = Math.abs(Math.trunc(Number(count) || 0));
  if (n === 1) return `${count} ${one}`;
  const rest = n % 100;
  return `${count} ${n >= 20 && (rest === 0 || rest >= 20) ? 'de ' : ''}${many}`;
}

// "azi", "acum o zi", "acum 3 zile", "acum 21 de zile".
function daysAgo(days) {
  if (!isNum(days)) return '—';
  if (days <= 0) return 'azi';
  if (days === 1) return 'acum o zi';
  return `acum ${plural(days, 'zi', 'zile')}`;
}

// ISO dates inside server messages ("2026-04-18") -> "18 apr. 2026".
const humanDates = text => String(text ?? '').replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, (_, y, m, d) =>
  new Date(`${y}-${m}-${d}T12:00:00Z`).toLocaleDateString('ro-RO', {day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'}));
const toneOf = value => !isNum(value) || Math.abs(value) < 1e-9 ? '' : value > 0 ? 'pos' : 'neg';

function isoDay(offset = 0) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

function addDays(iso, offset) {
  const date = new Date(`${iso}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

const fmtTime = value => new Date(value).toLocaleTimeString('ro-RO', {hour: '2-digit', minute: '2-digit'});
const fmtShortDate = value => new Date(value).toLocaleDateString('ro-RO', {day: '2-digit', month: 'short'});
const fmtDayMonth = iso => new Date(`${iso}T12:00:00Z`).toLocaleDateString('ro-RO', {day: 'numeric', month: 'short', timeZone: 'UTC'});
const fmtLongDay = iso => new Date(`${iso}T12:00:00Z`).toLocaleDateString('ro-RO', {weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC'});
const fmtWeekday = iso => new Date(`${iso}T12:00:00Z`).toLocaleDateString('ro-RO', {weekday: 'short', day: 'numeric', timeZone: 'UTC'});
const fmtDateTime = value => new Date(value).toLocaleString('ro-RO', {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});

function kickoffLabel(value) {
  const date = new Date(value);
  const today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  if (date.toDateString() === today.toDateString()) return `Azi ${fmtTime(value)}`;
  if (date.toDateString() === tomorrow.toDateString()) return `Mâine ${fmtTime(value)}`;
  return `${fmtShortDate(value)} ${fmtTime(value)}`;
}

// The app's day is the UTC day. In Romania it ends at 03:00 (02:00 in winter): a note says so
// where the day's games are listed, and it is empty for a browser on UTC.
function utcDayNote() {
  const offset = -new Date().getTimezoneOffset();
  if (!offset) return '';
  const end = new Date();
  end.setUTCHours(24, 0, 0, 0);
  return `Ziua este ziua UTC: include meciurile până la ${fmtTime(end)} (ora ta) din noaptea următoare.`;
}

function dayChipLabel(offset) {
  if (offset === -1) return 'Ieri';
  if (offset === 0) return 'Azi';
  if (offset === 1) return 'Mâine';
  return fmtWeekday(isoDay(offset));
}

function initialsOf(name) {
  const clean = String(name || '?').replace(/[^\p{L}\p{N}\s/.-]/gu, ' ').trim();
  const parts = clean.split(/[\s/.-]+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

function hueOf(text) {
  let hash = 0;
  for (const char of String(text || '')) hash = (hash * 31 + char.codePointAt(0)) % 360;
  return hash;
}

// The competition part of a FlashScore league ("ENGLAND: Premier League" -> "Premier League").
const leagueName = league => String(league || '').split(':').slice(-1)[0].trim();

// Short market text for chips ("over_160.5" -> "P 160.5"); the full label stays in the title.
function shortMarket(key, label) {
  const k = String(key);
  if (['1', 'X', '2', '1X', 'X2', '12'].includes(k)) return k;
  let m = k.match(/^(home_|away_)?(over|under)_?(\d+(?:\.\d+)?)$/);
  if (m) {
    const line = m[3].includes('.') ? m[3] : (m[3].length === 2 ? `${m[3][0]}.${m[3][1]}` : m[3]);
    return `${m[1] === 'home_' ? '1 ' : m[1] === 'away_' ? '2 ' : ''}${m[2] === 'over' ? 'Peste' : 'Sub'} ${line}`;
  }
  if ((m = k.match(/^ah_([12])_([+-]?\d+(?:\.\d+)?)$/))) return `H${m[1]} ${m[2]}`;
  if ((m = k.match(/^sets_(\d)-(\d)$/))) return `Seturi ${m[1]}-${m[2]}`;
  if ((m = k.match(/^cs_(\d+)-(\d+)$/))) return `Scor ${m[1]}-${m[2]}`;
  if (k === 'btts') return 'GG';
  if (k === 'no_btts') return 'NG';
  if ((m = k.match(/^dnb_([12])$/))) return `DNB ${m[1]}`;
  if ((m = k.match(/^ht_([1X2])$/))) return `Pauză ${m[1]}`;
  return label || k;
}

// --- sport icons ---------------------------------------------------------------------------

const ICON_PATHS = {
  football: '<circle cx="12" cy="12" r="9"/><path d="M12 8.3l3.3 2.4-1.3 3.9H10l-1.3-3.9z"/><path d="M12 3.2v5.1M15.3 10.7l5-1.5M14 14.6l3 4.3M10 14.6l-3 4.3M8.7 10.7l-5-1.5"/>',
  basketball: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3v18M5.8 5.6c3.3 3.5 3.3 9.3 0 12.8M18.2 5.6c-3.3 3.5-3.3 9.3 0 12.8"/>',
  tennis: '<circle cx="12" cy="12" r="9"/><path d="M5.3 5.9c3.4 3.3 3.4 8.9 0 12.2M18.7 5.9c-3.4 3.3-3.4 8.9 0 12.2"/>',
  all: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>',
  live: '<circle cx="12" cy="12" r="3"/><path d="M6.3 6.3a8 8 0 0 0 0 11.4M17.7 6.3a8 8 0 0 1 0 11.4"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.7-4.3M4 5v4h4M4 13a8 8 0 0 0 14.7 4.3M20 19v-4h-4"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  wallet: '<rect x="3" y="6" width="18" height="13" rx="3"/><path d="M16 12.5h2M3 9h18"/>',
  spark: '<path d="M12 3l2.2 5.6L20 10l-5.8 1.6L12 17l-2.2-5.4L4 10l5.8-1.4z"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.5"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  pause: '<path d="M9 6v12M15 6v12"/>',
  play: '<path d="M8 5l11 7-11 7z"/>',
};

function icon(name, cls = '') {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON_PATHS[name] || ''}</svg>`;
}

const sportIcon = sport => `<span class="sport-dot sport-${esc(sport)}" title="${esc(SPORT_LABEL[sport] || sport)}">${icon(sport)}</span>`;

// --- API -----------------------------------------------------------------------------------

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error('Serverul nu răspunde. Pornește .\\start.ps1 și deschide http://127.0.0.1:8000.');
  }
  const data = await response.json().catch(() => ({detail: 'Serverul nu a returnat un răspuns valid.'}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Cerere invalidă.');
    error.status = response.status;
    throw error;
  }
  return data;
}

const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body ?? {})});

// Only same-origin display URLs (/api/img?u=...) are used, so the CSP img-src 'self' holds.
function safeImg(url) {
  return typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') ? url : null;
}

// --- route scope ---------------------------------------------------------------------------
// Each navigation gets a fresh scope: pollers and timers register a cleanup, and async page
// code checks scope.alive before touching the DOM so a slow answer never paints a newer page.

let currentScope = {alive: true, cleanups: []};

function newScope() {
  currentScope.alive = false;
  currentScope.cleanups.forEach(fn => { try { fn(); } catch { /* ignore */ } });
  currentScope = {alive: true, cleanups: []};
  return currentScope;
}

function onLeave(fn) { currentScope.cleanups.push(fn); }

// --- feedback: toasts and dialogs ----------------------------------------------------------

function toast(message, kind = 'info', link) {
  const box = $('#toasts');
  if (!box || !message) return;
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `<span class="toast-icon">${icon(kind === 'error' ? 'warn' : kind === 'success' ? 'spark' : 'live')}</span>
    <span class="toast-text">${esc(message)}${link ? ` <a href="${esc(link.href)}">${esc(link.label)}</a>` : ''}</span>
    <button class="toast-close" type="button" aria-label="Închide notificarea">${icon('close')}</button>`;
  const remove = () => { el.classList.add('leaving'); setTimeout(() => el.remove(), 200); };
  el.querySelector('.toast-close').addEventListener('click', remove);
  box.appendChild(el);
  setTimeout(remove, kind === 'error' ? 8000 : 5000);
}

function openDialog(html, onReady) {
  const dialog = $('#dialog');
  dialog.innerHTML = html;
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue), {once: true});
    dialog.returnValue = '';
    dialog.showModal();
    if (onReady) onReady(dialog);
  });
}

async function confirmDialog({title, text, ok = 'Continuă', cancel = 'Renunță', danger = false}) {
  const value = await openDialog(`<form method="dialog" class="dialog-body">
      <h2 class="dialog-title">${esc(title)}</h2>
      <p class="dialog-text">${esc(text)}</p>
      <div class="dialog-actions">
        <button class="btn btn-ghost" value="cancel" formnovalidate>${esc(cancel)}</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" value="ok" data-confirm>${esc(ok)}</button>
      </div></form>`);
  return value === 'ok';
}

// Stake prompt for virtual bets; resolves to a number or null.
async function stakeDialog({title, text, stake = remember('stake', 10)}) {
  let chosen = null;
  const value = await openDialog(`<form method="dialog" class="dialog-body">
      <h2 class="dialog-title">${esc(title)}</h2>
      <p class="dialog-text">${esc(text)}</p>
      <label class="field">Miză (RON)<input name="stake" type="number" min="0.01" step="0.01" required value="${esc(stake)}" inputmode="decimal"></label>
      <p class="fineprint">Bani virtuali, fără miză reală. ${esc(DISCLAIMER)}</p>
      <div class="dialog-actions">
        <button class="btn btn-ghost" value="cancel" formnovalidate>Renunță</button>
        <button class="btn btn-primary" value="ok" data-confirm>Joacă virtual</button>
      </div></form>`, dialog => {
    const input = dialog.querySelector('input');
    input.focus();
    input.select();
    dialog.querySelector('form').addEventListener('submit', () => { chosen = Number(input.value); });
  });
  if (value !== 'ok' || !(chosen > 0)) return null;
  persist('stake', chosen);
  return chosen;
}

// Modal drawer/dialog never outlive the page that opened them (route() calls this).
function closeOverlays() {
  ['#drawer', '#dialog'].forEach(selector => {
    const el = $(selector);
    if (el?.open) el.close('cancel');
  });
}

// Deposit prompt; resolves to the chosen amount or null.
async function depositDialog({needed = 0, balance = 0, currency = 'RON'} = {}) {
  let chosen = null;
  const suggested = [50, 100, 500].find(v => balance + v >= needed) || Math.ceil(needed - balance);
  const value = await openDialog(`<form method="dialog" class="dialog-body">
      <h2 class="dialog-title">Depui bani virtuali?</h2>
      <p class="dialog-text">Portofelul virtual are ${money(balance, currency)}, iar miza este ${money(needed, currency)}. Depune bani fictivi și pariul se plasează imediat.</p>
      <div class="chip-row" role="group" aria-label="Sume rapide">${[50, 100, 500].map(v => `<button type="button" class="chip" data-quick="${v}">${v} ${esc(currency)}</button>`).join('')}</div>
      <label class="field">Sumă (${esc(currency)})<input name="amount" type="number" min="0.01" max="1000000" step="0.01" required value="${esc(suggested)}" inputmode="decimal"></label>
      <p class="fineprint">Bani virtuali, fără miză reală. ${esc(DISCLAIMER)}</p>
      <div class="dialog-actions">
        <button class="btn btn-ghost" value="cancel" formnovalidate>Renunță</button>
        <button class="btn btn-primary" value="ok" data-confirm>Depune și joacă</button>
      </div></form>`, dialog => {
    const input = dialog.querySelector('input');
    dialog.querySelectorAll('[data-quick]').forEach(b => b.addEventListener('click', () => { input.value = b.dataset.quick; input.focus(); }));
    input.focus();
    input.select();
    dialog.querySelector('form').addEventListener('submit', () => { chosen = Number(input.value); });
  });
  return value === 'ok' && chosen > 0 ? chosen : null;
}

// POST /api/wallet/bet; with too little virtual money, offer a deposit and retry once.
// Resolves to the wallet, or null when the user gave up (a toast already said why).
async function placeBet(body) {
  try {
    return await post('/api/wallet/bet', body);
  } catch (error) {
    if (!(error.status === 400 && /Sold insuficient/i.test(error.message))) throw error;
    let wallet = {balance: 0, currency: 'RON'};
    try { wallet = await api('/api/wallet'); } catch { /* the dialog still works */ }
    const amount = await depositDialog({needed: body.stake, balance: wallet.balance || 0, currency: wallet.currency || 'RON'});
    if (amount == null) {
      toast('Pariul nu a fost plasat: sold virtual insuficient.', 'info', {href: '#/portofel', label: 'Portofel'});
      return null;
    }
    await post('/api/wallet/deposit', {amount});
    return post('/api/wallet/bet', body);
  }
}

function openDrawer(html) {
  const drawer = $('#drawer');
  drawer.innerHTML = `<div class="drawer-inner"><button class="icon-btn drawer-close" type="button" aria-label="Închide">${icon('close')}</button><div class="drawer-content">${html}</div></div>`;
  drawer.querySelector('.drawer-close').addEventListener('click', () => drawer.close());
  if (!drawer.open) drawer.showModal();
  hydrate(drawer);
  return drawer.querySelector('.drawer-content');
}

// --- building blocks -----------------------------------------------------------------------

function crest(url, name, size = 'md', kind = 'team') {
  const src = safeImg(url);
  return `<span class="crest crest-${size} crest-${kind}${src ? ' has-img' : ''}" data-hue="${hueOf(name)}" aria-hidden="true"><span class="crest-initials">${esc(initialsOf(name))}</span>${src ? `<img src="${esc(src)}" alt="" loading="lazy" decoding="async">` : ''}</span>`;
}

const gradeBadge = (grade, confidence) => grade ? `<span class="grade grade-${esc(grade)}" title="Calitatea datelor: ${esc(grade)}${isNum(confidence) ? ` · încredere ${confidence}/100` : ''}">${esc(grade)}</span>` : '';

function statusBadge(status, extra = '') {
  if (!status) return '';
  return `<span class="badge badge-${esc(status)}">${esc(STATUS_LABEL[status] || status)}${extra ? ` <b>${esc(extra)}</b>` : ''}</span>`;
}

const sportTag = sport => `<span class="sport-tag sport-${esc(sport)}">${icon(sport)}${esc(SPORT_LABEL[sport] || sport)}</span>`;

function formPills(sequence) {
  if (!sequence) return '<span class="form-empty">fără formă</span>';
  const map = {W: ['V', 'victorie'], D: ['E', 'egal'], L: ['Î', 'înfrângere']};
  return `<span class="form" aria-label="Formă recentă">${[...sequence].slice(0, 6).map(r => `<i class="f-${esc(r)}" title="${esc(map[r]?.[1] || r)}">${esc(map[r]?.[0] || r)}</i>`).join('')}</span>`;
}

function probBar(value, tone = '') {
  return `<span class="pbar ${tone}" role="presentation"><span data-w="${clamp01(value)}"></span></span>`;
}

function skeletonCards(count = 4, cls = '') {
  return Array.from({length: count}, () => `<div class="card skeleton-card ${cls}" aria-hidden="true"><div class="sk sk-line w40"></div><div class="sk sk-line w80"></div><div class="sk sk-block"></div><div class="sk sk-line w60"></div></div>`).join('');
}

function loadingBlock(text = 'Se încarcă…') {
  return `<div class="state state-loading" role="status"><span class="spinner" aria-hidden="true"></span><p>${esc(text)}</p></div>`;
}

function emptyState(title, text = '', action = '') {
  return `<div class="state state-empty"><div class="state-art" aria-hidden="true">${icon('spark')}</div><h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ''}${action}</div>`;
}

function errorState(error, retryId = '') {
  const message = humanDates(error?.message || String(error || 'Eroare necunoscută.'));
  // 400/404/422: the request was understood but the settings do not fit (no network problem).
  const invalid = [400, 404, 409, 422].includes(error?.status);
  const hint = error?.status === 429 ? 'Cota de cereri FlashScore a fost atinsă. Încearcă mai târziu.'
    : error?.status === 503 ? 'Cheia RAPIDAPI_KEY lipsește sau nu este validă (fișierul .env).' : '';
  const title = invalid ? 'Verifică setările' : 'Nu am putut încărca datele';
  return `<div class="state ${invalid ? 'state-invalid' : 'state-error'}" role="alert"><div class="state-art" aria-hidden="true">${icon('warn')}</div><h3>${esc(title)}</h3><p>${esc(message)}</p>${hint ? `<p class="muted">${esc(hint)}</p>` : ''}${retryId && !invalid ? `<button class="btn btn-secondary" type="button" id="${esc(retryId)}">Încearcă din nou</button>` : ''}</div>`;
}

function warningsBox(warnings, title = 'Atenție') {
  const list = (warnings || []).filter(Boolean);
  if (!list.length) return '';
  return `<div class="callout callout-warn" role="note"><div class="callout-icon">${icon('warn')}</div><div><b>${esc(title)}</b><ul>${list.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div></div>`;
}

function disclaimerBox(text) {
  return `<p class="disclaimer"><span class="age">18+</span>${esc(text || DISCLAIMER)}</p>`;
}

function kpi(label, value, sub = '', tone = '') {
  return `<div class="kpi ${tone ? `kpi-${tone}` : ''}"><small>${esc(label)}</small><b>${value}</b>${sub ? `<span>${sub}</span>` : ''}</div>`;
}

function chips(items, active, attr, cls = '') {
  return items.map(([value, label]) => `<button type="button" class="chip ${cls} ${String(value) === String(active) ? 'active' : ''}" data-${attr}="${esc(value)}" aria-pressed="${String(value) === String(active)}">${label}</button>`).join('');
}

function matchHref(id, sport) {
  return `#/meci/${encodeURIComponent(id)}${sport ? `?sport=${encodeURIComponent(sport)}` : ''}`;
}

// --- legs and tickets ----------------------------------------------------------------------

// options.shared: other tickets holding the same leg ("x5, x10"): a loss sinks them all.
function legRow(leg, {compact = false, reason = true, shared = ''} = {}) {
  const status = leg.status || 'pending';
  const score = leg.score ? `<span class="leg-score">${esc(leg.score)}</span>` : '';
  const ev = isNum(leg.ev) ? `<span class="ev ${toneOf(leg.ev)}" title="Valoare așteptată = probabilitate × cotă − 1">EV ${signedPct(leg.ev)}</span>` : '';
  return `<li class="leg leg-${esc(status)} sport-${esc(leg.sport)}">
    <div class="leg-meta">
      ${sportIcon(leg.sport)}
      ${crest(leg.league_logo, leg.competition, 'xs', 'league')}
      <span class="leg-league" title="${esc(leg.competition)}">${esc(leg.competition)}</span>
      ${leg.kickoff ? `<time datetime="${esc(leg.kickoff)}">${esc(kickoffLabel(leg.kickoff))}</time>` : ''}
    </div>
    <a class="leg-teams" href="${matchHref(leg.match_id, leg.sport)}">
      <span class="team">${crest(leg.home_logo, leg.home, 'sm')}<span class="team-name">${esc(leg.home)}</span></span>
      <span class="team">${crest(leg.away_logo, leg.away, 'sm')}<span class="team-name">${esc(leg.away)}</span></span>
    </a>
    <div class="leg-pick">
      <div class="pick-label"><b>${esc(leg.label)}</b>${compact ? '' : `<small>${esc(leg.group || '')}</small>`}</div>
      <div class="pick-prob" title="Probabilitate estimată">${probBar(leg.probability)}<span>${pct(leg.probability)}</span></div>
      <div class="pick-odds" title="Cotă"><small>cotă</small><b>${num(leg.odds)}</b></div>
    </div>
    <div class="leg-foot">${gradeBadge(leg.grade, leg.confidence)}${ev}${status !== 'pending' ? statusBadge(status) : ''}${score}${shared ? `<span class="shared-note" title="Aceeași selecție pe mai multe bilete: o pierdere le pierde pe toate">apare și în ${esc(shared)}</span>` : ''}</div>
    ${reason && leg.reason ? `<details class="reason"><summary>De ce această selecție?</summary><p>${esc(leg.reason)}</p></details>` : ''}
  </li>`;
}

// A recommendation / generated ticket (§4.4, §10.2). options.actions: html for the footer.
function ticketCard(ticket, {title, actions = '', id = '', note = true, shared = null} = {}) {
  const target = ticket.target ?? ticket.target_odds;
  const status = ticket.status || 'pending';
  const heading = title || (isNum(target) ? `Bilet cota ${num(target, target < 10 ? 1 : 0).replace(/\.0$/, '')}` : 'Bilet');
  const badge = isNum(target) ? `x${Number(target) >= 10 ? Math.round(target) : num(target, 1).replace(/\.0$/, '')}` : '×';
  const tier = !isNum(target) ? 'x' : target <= 2.5 ? 'x2' : target <= 6 ? 'x5' : target <= 20 ? 'x10' : 'x100';
  if (status === 'unavailable' || !ticket.legs?.length) {
    return `<article class="card ticket ticket-${tier} ticket-unavailable" ${id ? `id="${esc(id)}"` : ''}>
      <header class="ticket-head"><span class="ticket-badge">${esc(badge)}</span><div class="ticket-title"><h3>${esc(heading)}</h3><span>Bilet indisponibil</span></div>${statusBadge('unavailable')}</header>
      <div class="ticket-empty"><p>${esc(ticket.reason || ticket.rationale || 'Nu există selecții eligibile pentru această cotă.')}</p></div>
      ${actions ? `<footer class="ticket-actions">${actions}</footer>` : ''}
    </article>`;
  }
  const sports = [...new Set(ticket.legs.map(l => l.sport))];
  const payout = isNum(ticket.payout_odds) ? `<span class="muted">cotă plătită ${num(ticket.payout_odds)}</span>` : '';
  return `<article class="card ticket ticket-${tier} ticket-${esc(status)}" ${id ? `id="${esc(id)}"` : ''}>
    <header class="ticket-head">
      <span class="ticket-badge">${esc(badge)}</span>
      <div class="ticket-title"><h3>${esc(heading)}</h3><span>${ticket.legs.length} ${ticket.legs.length === 1 ? 'selecție' : 'selecții'} · ${sports.map(s => esc(SPORT_LABEL[s] || s)).join(', ')}</span></div>
      ${statusBadge(status)}
    </header>
    <div class="ticket-kpis">
      <div class="tk"><small>Cotă totală</small><b>${num(ticket.total_odds)}</b>${payout}</div>
      <div class="tk"><small>Șansă estimată</small><b>${pct(ticket.probability, ticket.probability < 0.1 ? 1 : 0)}</b>${probBar(ticket.probability, 'thin')}</div>
      <div class="tk"><small>Valoare (EV)</small><b class="${toneOf(ticket.ev)}">${signedPct(ticket.ev)}</b></div>
    </div>
    <ol class="legs">${ticket.legs.map(leg => legRow(leg, {shared: shared ? shared(leg) : ''})).join('')}</ol>
    ${note && (ticket.rationale || ticket.assumption) ? `<details class="ticket-why"><summary>Cum a fost construit biletul</summary>${ticket.rationale ? `<p>${esc(ticket.rationale)}</p>` : ''}${ticket.assumption ? `<p class="muted">${esc(ticket.assumption)}</p>` : ''}</details>` : ''}
    ${actions ? `<footer class="ticket-actions">${actions}</footer>` : ''}
  </article>`;
}

// --- hydration: CSSOM widths/colours and image fallbacks -----------------------------------

function hydrate(root = document) {
  $$('[data-w]', root).forEach(el => { el.style.width = `${(clamp01(el.dataset.w) * 100).toFixed(2)}%`; });
  $$('[data-hue]', root).forEach(el => { el.style.setProperty('--h', el.dataset.hue); });
  $$('[data-heat]', root).forEach(el => {
    const alpha = Math.min(1, Number(el.dataset.heat) * 5);
    el.style.setProperty('--heat', alpha.toFixed(3));
    if (alpha > 0.55) el.classList.add('hot');
  });
  $$('.crest img', root).forEach(img => {
    if (img.dataset.bound) return;
    img.dataset.bound = '1';
    const fail = () => {
      const holder = img.closest('.crest');
      if (holder) holder.classList.remove('has-img');
      img.remove();
    };
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) fail();
    else img.addEventListener('error', fail, {once: true});
  });
}

// --- SVG charts (DOM APIs only) ------------------------------------------------------------

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgEl(name, attrs = {}, parent) {
  const el = document.createElementNS(SVG_NS, name);
  Object.entries(attrs).forEach(([k, v]) => { if (v != null) el.setAttribute(k, String(v)); });
  if (parent) parent.appendChild(el);
  return el;
}

function niceTicks(min, max, count = 4) {
  if (!(max > min)) return [min];
  const span = max - min;
  const step0 = span / count;
  const magnitude = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => span / s <= count + 0.5) || step0;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

// Line chart. series: [{name, cls, points: [{x: 'YYYY-MM-DD' | Date, y}]}]; options: {yFormat,
// reference (horizontal line value), referenceLabel, label (aria)}. Re-renders on resize.
function lineChart(container, series, options = {}) {
  if (!container) return;
  const draw = () => {
    container.textContent = '';
    const width = Math.max(280, container.clientWidth || 600);
    const height = options.height || (width < 500 ? 200 : 260);
    const pad = {l: 56, r: 14, t: 14, b: 28};
    const all = series.flatMap(s => s.points).filter(p => isNum(p.y));
    if (!all.length) { container.innerHTML = '<p class="muted small">Nu există date pentru grafic.</p>'; return; }
    const xs = all.map(p => +new Date(p.x));
    let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
    if (x0 === x1) { x0 -= 86400000; x1 += 86400000; }
    let ys = all.map(p => p.y);
    if (isNum(options.reference)) ys = ys.concat(options.reference);
    let [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    const margin = (y1 - y0) * 0.08;
    y0 -= margin; y1 += margin;
    const X = x => pad.l + ((+new Date(x) - x0) / (x1 - x0)) * (width - pad.l - pad.r);
    const Y = y => pad.t + (1 - (y - y0) / (y1 - y0)) * (height - pad.t - pad.b);
    const svg = svgEl('svg', {viewBox: `0 0 ${width} ${height}`, width, height, class: 'chart', role: 'img', 'aria-label': options.label || 'Grafic'});
    const fmt = options.yFormat || (v => num(v, 0));
    niceTicks(y0, y1, 4).forEach(t => {
      svgEl('line', {x1: pad.l, x2: width - pad.r, y1: Y(t), y2: Y(t), class: 'grid'}, svg);
      svgEl('text', {x: pad.l - 8, y: Y(t) + 4, 'text-anchor': 'end', class: 'axis'}, svg).textContent = fmt(t);
    });
    const dates = [x0, x0 + (x1 - x0) / 2, x1];
    const short = x1 - x0 < 2 * 86400000;
    const when = d => short
      ? new Date(d).toLocaleString('ro-RO', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'})
      : new Date(d).toLocaleDateString('ro-RO', {day: 'numeric', month: 'short', year: x1 - x0 > 300 * 86400000 ? '2-digit' : undefined});
    dates.forEach((d, i) => {
      svgEl('text', {x: X(d), y: height - 8, 'text-anchor': i === 0 ? 'start' : i === 2 ? 'end' : 'middle', class: 'axis'}, svg).textContent = when(d);
    });
    if (isNum(options.reference)) {
      svgEl('line', {x1: pad.l, x2: width - pad.r, y1: Y(options.reference), y2: Y(options.reference), class: 'ref'}, svg);
      // Left end, just above the line: the series' latest (right-hand) points never sit on it.
      const labelY = Math.max(pad.t + 10, Y(options.reference) - 6);
      if (options.referenceLabel) svgEl('text', {x: pad.l + 6, y: labelY, 'text-anchor': 'start', class: 'axis ref-label'}, svg).textContent = options.referenceLabel;
    }
    series.forEach((s, index) => {
      const pts = s.points.filter(p => isNum(p.y));
      if (!pts.length) return;
      const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join('');
      if (index === 0 && options.area !== false) {
        const base = Y(Math.max(y0, Math.min(y1, isNum(options.reference) ? options.reference : y0)));
        svgEl('path', {d: `${d}L${X(pts[pts.length - 1].x).toFixed(1)},${base.toFixed(1)}L${X(pts[0].x).toFixed(1)},${base.toFixed(1)}Z`, class: `area ${s.cls || ''}`}, svg);
      }
      svgEl('path', {d, class: `line ${s.cls || ''}`}, svg);
      if (pts.length <= 60) pts.forEach(p => svgEl('circle', {cx: X(p.x), cy: Y(p.y), r: 2.4, class: `dot ${s.cls || ''}`}, svg));
    });
    // Hover read-out: nearest point of the first series.
    const main = series[0].points.filter(p => isNum(p.y));
    const cursor = svgEl('g', {class: 'cursor', visibility: 'hidden'}, svg);
    const vline = svgEl('line', {y1: pad.t, y2: height - pad.b, class: 'cursor-line'}, cursor);
    const mark = svgEl('circle', {r: 4.5, class: 'cursor-dot'}, cursor);
    const labelBg = svgEl('rect', {rx: 6, height: 22, class: 'cursor-bg'}, cursor);
    const label = svgEl('text', {class: 'cursor-text'}, cursor);
    svg.addEventListener('pointermove', event => {
      const box = svg.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * width;
      let best = main[0];
      main.forEach(p => { if (Math.abs(X(p.x) - px) < Math.abs(X(best.x) - px)) best = p; });
      const cx = X(best.x), cy = Y(best.y);
      vline.setAttribute('x1', cx); vline.setAttribute('x2', cx);
      mark.setAttribute('cx', cx); mark.setAttribute('cy', cy);
      label.textContent = `${short ? when(best.x) : new Date(best.x).toLocaleDateString('ro-RO', {day: 'numeric', month: 'short', year: 'numeric'})} · ${fmt(best.y)}`;
      const textWidth = label.textContent.length * 6.6 + 14;
      const lx = Math.min(width - pad.r - textWidth, Math.max(pad.l, cx - textWidth / 2));
      labelBg.setAttribute('x', lx); labelBg.setAttribute('y', pad.t); labelBg.setAttribute('width', textWidth);
      label.setAttribute('x', lx + 7); label.setAttribute('y', pad.t + 15);
      cursor.setAttribute('visibility', 'visible');
    });
    svg.addEventListener('pointerleave', () => cursor.setAttribute('visibility', 'hidden'));
    container.appendChild(svg);
  };
  draw();
  if ('ResizeObserver' in window) {
    let last = container.clientWidth;
    const observer = new ResizeObserver(() => {
      if (Math.abs(container.clientWidth - last) < 8) return;
      last = container.clientWidth;
      draw();
    });
    observer.observe(container);
    onLeave(() => observer.disconnect());
  }
}

// Normal density chart with the region above `split` highlighted (basketball margin/total).
// Drawn at the container's width (no viewBox scaling, so text keeps its CSS size on a phone);
// the low/high labels sit in the top corners, away from the curve's peak.
function densityChart(container, {mean, sd, split = 0, lowLabel, highLabel, unit = 'puncte', label}) {
  if (!container || !isNum(mean) || !(sd > 0)) return;
  const draw = () => {
    const width = Math.max(260, container.clientWidth || 560);
    const height = width < 420 ? 180 : 190;
    const pad = {l: 12, r: 12, t: 30, b: 28};
    const lo = mean - 3.2 * sd, hi = mean + 3.2 * sd;
    const pdf = x => Math.exp(-0.5 * ((x - mean) / sd) ** 2);
    const X = x => pad.l + ((x - lo) / (hi - lo)) * (width - pad.l - pad.r);
    const Y = y => pad.t + (1 - y) * (height - pad.t - pad.b);
    const svg = svgEl('svg', {viewBox: `0 0 ${width} ${height}`, width, height, class: 'chart density', role: 'img', 'aria-label': label || 'Distribuție'});
    const steps = 120;
    const points = Array.from({length: steps + 1}, (_, i) => lo + ((hi - lo) * i) / steps);
    const path = points.map((x, i) => `${i ? 'L' : 'M'}${X(x).toFixed(1)},${Y(pdf(x)).toFixed(1)}`).join('');
    const s = Math.max(lo, Math.min(hi, split));
    const area = (pts, cls) => {
      if (pts.length < 2) return;
      const d = pts.map((x, i) => `${i ? 'L' : 'M'}${X(x).toFixed(1)},${Y(pdf(x)).toFixed(1)}`).join('');
      svgEl('path', {d: `${d}L${X(pts[pts.length - 1]).toFixed(1)},${Y(0)}L${X(pts[0]).toFixed(1)},${Y(0)}Z`, class: `area ${cls}`}, svg);
    };
    area(points.filter(x => x <= s), 'low');
    area(points.filter(x => x >= s), 'high');
    svgEl('path', {d: path, class: 'line'}, svg);
    svgEl('line', {x1: X(s), x2: X(s), y1: pad.t - 4, y2: Y(0), class: 'ref'}, svg);
    svgEl('line', {x1: pad.l, x2: width - pad.r, y1: Y(0), y2: Y(0), class: 'grid'}, svg);
    [lo + 0.2 * sd, mean, hi - 0.2 * sd].forEach((v, i) => {
      svgEl('text', {x: X(v), y: height - 8, 'text-anchor': i === 0 ? 'start' : i === 2 ? 'end' : 'middle', class: 'axis'}, svg).textContent = `${v > 0 && split === 0 ? '+' : ''}${num(v, 0)}`;
    });
    svgEl('text', {x: pad.l, y: 14, 'text-anchor': 'start', class: 'axis strong side-low'}, svg).textContent = `◀ ${lowLabel || ''}`;
    svgEl('text', {x: width - pad.r, y: 14, 'text-anchor': 'end', class: 'axis strong side-high'}, svg).textContent = `${highLabel || ''} ▶`;
    svg.appendChild(svgEl('title')).textContent = `${label || ''} medie ${num(mean, 1)} ${unit}, abatere ${num(sd, 1)}`;
    container.textContent = '';
    container.appendChild(svg);
  };
  draw();
  if ('ResizeObserver' in window) {
    let last = container.clientWidth;
    const observer = new ResizeObserver(() => {
      if (Math.abs(container.clientWidth - last) < 8) return;
      last = container.clientWidth;
      draw();
    });
    observer.observe(container);
    onLeave(() => observer.disconnect());
  }
}

// Horizontal probability bars list.
function barsList(rows, digits = 1) {
  return `<div class="bars">${rows.map(r => `<div class="bar-row ${r.top ? 'top' : ''}"><span class="bar-label" title="${esc(r.title || r.label)}">${esc(r.label)}</span><span class="bar-track"><span data-w="${clamp01(r.value)}"></span></span><b>${pct(r.value, digits)}</b></div>`).join('')}</div>`;
}

// Multi-segment probability strip (1 / X / 2).
function outcomeStrip(parts) {
  const best = Math.max(...parts.map(p => p.value || 0));
  return `<div class="outcomes">
    <div class="outcome-bar">${parts.map((p, i) => `<span class="seg seg-${i}" data-w="${clamp01(p.value)}"></span>`).join('')}</div>
    <div class="outcome-legend">${parts.map((p, i) => `<span class="ol ol-${i} ${p.value === best ? 'best' : ''}"><small>${esc(p.label)}</small><b>${pct(p.value)}</b>${p.odds ? `<em>${num(p.odds)}</em>` : ''}</span>`).join('')}</div>
  </div>`;
}
