'use strict';
const $ = (selector) => document.querySelector(selector);
const API_BASE = ['5500', '5501'].includes(window.location.port) || window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : '';
const state = { matches: [], analyses: new Map(), source: 'flashscore', filter: 'all', busy: false };
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const threshold = () => Number($('#threshold').value);
const kickoff = match => new Date(match.kickoff);
const upcoming = match => match.status === 'scheduled' && kickoff(match) > new Date();
const time = value => new Date(value).toLocaleTimeString('ro-RO', {hour:'2-digit', minute:'2-digit'});
const day = value => new Date(value).toLocaleDateString('ro-RO', {day:'2-digit', month:'short'});
function notify(message, error = false) {
  $('#notice').textContent = message;
  $('#notice').classList.toggle('error', error);
  $('#notice').hidden = !message;
}
async function api(path, options = {}) {
  let response;
  try { response = await fetch(`${API_BASE}${path}`, {...options, headers: {'Content-Type':'application/json', ...options.headers}}); }
  catch { throw new Error('Backendul Python nu răspunde. Pornește .\\start.ps1 și deschide http://127.0.0.1:8000, sau folosește Live Server pe portul 5500/5501.'); }
  const data = await response.json().catch(() => ({detail:'Serverul nu a returnat un răspuns valid.'}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Cerere invalidă.');
  return data;
}
async function task(button, callback) {
  if (state.busy) return;
  state.busy = true;
  $('#threshold').disabled = true;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Se procesează…';
  notify('');
  try { await callback(); } catch (error) { notify(error.message, true); }
  finally { state.busy = false; button.disabled = false; button.textContent = label; $('#threshold').disabled = false; }
}
function renderMatches() {
  const query = $('#search').value.toLocaleLowerCase('ro');
  const selected = [...state.analyses.values()].filter(a => a.prediction.selection).length;
  $('#match-count').textContent = state.matches.length;
  $('#pick-count').textContent = selected;
  $('#match-caption').textContent = state.source === 'synthetic' ? 'Date demonstrative · echipe fictive' : `${state.matches.filter(upcoming).length} meciuri înainte de start`;
  $('#source-badge').textContent = state.source === 'synthetic' ? 'DEMO · SINTETIC' : 'FLASHSCORE';
  $('#source-badge').classList.toggle('demo', state.source === 'synthetic');
  $('#analyze-all').disabled = !state.matches.some(upcoming) || state.source === 'synthetic';
  const rows = state.matches.filter(m => `${m.home} ${m.away} ${m.league}`.toLocaleLowerCase('ro').includes(query))
    .filter(m => state.filter !== 'selected' || state.analyses.get(m.id)?.prediction.selection);
  if (!rows.length) {
    $('#match-list').innerHTML = `<div class="empty"><div class="empty-icon">⌁</div><h3>${state.matches.length ? 'Niciun meci pentru acest filtru.' : 'Nu sunt meciuri de afișat.'}</h3><p>${state.filter === 'selected' ? 'Analizează meciuri sau schimbă pragul. Modelul poate refuza toate selecțiile.' : 'Alege altă dată sau explorează modul demo.'}</p></div>`;
    return;
  }
  $('#match-list').innerHTML = rows.map(m => {
    const analysis = state.analyses.get(m.id), pick = analysis?.prediction.selection;
    const status = m.status === 'finished' ? `${m.home_goals}–${m.away_goals}` : m.status === 'live' ? 'LIVE' : upcoming(m) ? 'PRE-MATCH' : 'ÎNCHEIAT / INACTIV';
    const pickText = pick ? `<strong>${pct(pick.probability)}</strong><small>${escapeHtml(pick.label)}</small>` : analysis ? '<span>Fără selecție</span><small>Prag sau istoric insuficient</small>' : '<span>—</span><small>În așteptarea analizei</small>';
    return `<article class="match-row"><div class="match-time">${time(m.kickoff)}<small>${escapeHtml(status)}</small></div><div class="teams"><span class="team-name"><span class="team-initial">${escapeHtml(m.home.slice(0,2).toUpperCase())}</span>${escapeHtml(m.home)}</span><span class="team-name"><span class="team-initial">${escapeHtml(m.away.slice(0,2).toUpperCase())}</span>${escapeHtml(m.away)}</span></div><div class="league-name">${escapeHtml(m.league)}<br>${day(m.kickoff)}</div><div class="match-pick">${pickText}</div><button class="row-button" data-match="${escapeHtml(m.id)}" ${!analysis && !upcoming(m) ? 'disabled' : ''}>${analysis ? 'Detalii ↗' : 'Analizează'}</button></article>`;
  }).join('');
}
function showAnalysis(analysis) {
  const {match:m, prediction:p} = analysis, pick = p.selection;
  const savedText = state.source === 'synthetic' ? 'Demo sintetic. Nu intră în jurnalul real.' : analysis.saved ? 'Prima selecție a fost salvată în jurnal.' : pick ? 'Jurnalul păstrează prima selecție salvată. Vezi fila Rezultate.' : 'Nu a fost salvată o selecție.';
  $('#analysis-content').innerHTML = `<h2 class="analysis-title">${escapeHtml(m.home)} <span class="muted">vs</span> ${escapeHtml(m.away)}</h2><p class="analysis-sub">${escapeHtml(m.league)} · ${day(m.kickoff)} · ${time(m.kickoff)}</p><div class="selection-card"><span class="eyebrow">${pick ? 'SELECȚIA MODELULUI · ESTIMARE' : 'NICIO SELECȚIE'}</span><h3>${pick ? `${escapeHtml(pick.label)} · ${pct(pick.probability)}` : 'Uneori, cea mai bună decizie este să aștepți.'}</h3><p>${escapeHtml(p.reason)}</p></div><div class="analysis-numbers"><div><strong>${p.expected_goals.home.toFixed(2)} : ${p.expected_goals.away.toFixed(2)}</strong>Goluri estimate, gazde : oaspeți</div><div><strong>${p.sample.home} / ${p.sample.away}</strong>Meciuri istorice, gazde / oaspeți</div><div><strong>${pct(p.threshold)}</strong>Prag aplicat</div></div><h3>Probabilități pe piețe</h3>${p.markets.map(market => `<div class="market"><span>${escapeHtml(market.label)}${market.odds ? ` <small>· cotă ${market.odds.toFixed(2)}</small>` : ''}</span><progress max="1" value="${market.probability}" aria-label="${escapeHtml(market.label)}"></progress><strong>${pct(market.probability)}</strong></div>`).join('')}<h3>Scoruri probabile</h3><div class="scores">${p.scores.map(s => `<span class="score">${s.score}<small>${pct(s.probability)}</small></span>`).join('')}</div><p class="footnote">${savedText} Model necalibrat · ${escapeHtml(p.version)}</p>${analysis.warnings.map(w => `<p class="notice">${escapeHtml(w)}</p>`).join('')}`;
  $('#analysis-dialog').showModal();
}
function metricsHtml(m) {
  return `<div class="metric-strip"><div class="metric-box"><span>ACURATEȚE OBSERVATĂ</span><strong>${pct(m.accuracy)}</strong><small>${m.wins} reușite / ${m.settled} evaluate</small></div><div class="metric-box"><span>ACOPERIRE</span><strong>${pct(m.coverage)}</strong><small>${m.selected} selecții / ${m.total_matches} meciuri analizate</small></div><div class="metric-box"><span>INTERVAL WILSON 95%</span><strong>${m.interval95 ? `${Math.round(m.interval95[0]*100)}–${Math.round(m.interval95[1]*100)}%` : '—'}</strong><small>Incertitudinea ratei observate</small></div><div class="metric-box"><span>BRIER SCORE</span><strong>${m.brier == null ? '—' : m.brier.toFixed(3)}</strong><small>Mai mic = probabilități mai bune</small></div></div><p class="muted">${m.target_supported ? 'Eșantionul îndeplinește criteriul statistic definit pentru ținta de 85%.' : 'Ținta de 85% nu este încă demonstrată: sunt necesare minimum 100 de rezultate și limita inferioară a intervalului ≥85%.'}</p>`;
}
function tableHtml(rows) {
  if (!rows.length) return '<div class="empty"><h3>Nicio selecție evaluată încă.</h3><p>Rezultatele apar când există selecții eligibile și scoruri finale.</p></div>';
  return `<div class="table-scroll"><table><thead><tr><th>MECI / DATA</th><th>SELECȚIE</th><th>ESTIMARE</th><th>REZULTAT</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escapeHtml(r.match.home)} – ${escapeHtml(r.match.away)}<br><small>${day(r.match.kickoff)}</small></td><td>${escapeHtml(r.prediction.selection.label)}</td><td>${pct(r.prediction.selection.probability)}</td><td class="${r.result ? r.result.won ? 'won' : 'lost' : ''}">${r.result ? `${r.result.won ? '✓ Reușită' : '× Nereușită'} · ${escapeHtml(r.result.score)}` : 'În așteptare'}</td></tr>`).join('')}</tbody></table></div>`;
}
async function refreshResults() {
  const data = await api('/api/results');
  $('#accuracy').textContent = pct(data.metrics.accuracy);
  $('#accuracy-caption').textContent = data.metrics.settled ? `${data.metrics.settled} selecții reale evaluate` : 'Nu există încă rezultate evaluate';
  $('#results-summary').innerHTML = metricsHtml(data.metrics);
  $('#results-list').innerHTML = tableHtml(data.rows);
}
async function refreshHealth() {
  const health = await api('/api/health');
  $('#connection').textContent = health.api_configured ? 'Cheie API configurată' : 'API neconfigurată';
  $('#history-count').textContent = health.history_matches;
}
function renderBacktest(data) {
  const source = data.source === 'synthetic' ? 'DEMO · DATE SINTETICE · NU REZULTATE REALE' : data.source === 'csv' ? 'CSV IMPORTAT · EVALUARE RETROSPECTIVĂ' : 'FLASHSCORE · EVALUARE RETROSPECTIVĂ';
  $('#backtest-output').innerHTML = `<p class="notice">${source} · Prag ${pct(data.threshold)}</p>${metricsHtml(data.metrics)}<p class="muted">${escapeHtml(data.warning)} ${data.metrics.sufficient_history} meciuri cu istoric suficient.</p>${data.metrics.calibration.length ? `<h3>Calibrare observată a selecțiilor</h3><div class="table-scroll"><table><thead><tr><th>INTERVAL</th><th>VOLUM</th><th>ESTIMAT</th><th>OBSERVAT</th></tr></thead><tbody>${data.metrics.calibration.map(b => `<tr><td>${b.range}</td><td>${b.count}</td><td>${pct(b.predicted)}</td><td>${pct(b.actual)}</td></tr>`).join('')}</tbody></table></div>` : ''}<h3>Ultimele selecții evaluate</h3>${tableHtml(data.rows.slice().reverse())}`;
}
const titles = {matches:['Meciuri','O zi nouă. O perspectivă mai bună.','Explorează meciurile. Înțelege probabilitățile. Urmărește rezultatele.'],results:['Rezultate','Predicțiile trec. Rezultatele rămân.','Un jurnal real, cu selecții salvate înainte de start.'],backtest:['Backtesting','Mai întâi, pune modelul la încercare.','Acuratețe și acoperire, evaluate în ordine cronologică.'],method:['Cum calculăm','Înțelege ce stă în spatele procentului.','Un model transparent este primul pas spre o evaluare corectă.']};
titles.studio=['Ticket Lab','Construiește-ți următorul game plan.','O săptămână de planificat. Un bilet custom. Tu alegi formatul.'];
titles.plans=['Planurile mele','Toate planurile tale. Același playbook.','Biletele și cotele de la generare rămân salvate, chiar dacă închizi pagina.'];
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', async () => {
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b === button));
  document.querySelectorAll('.view').forEach(v => {v.hidden = v.id !== `view-${button.dataset.view}`;});
  const [crumb,title,description] = titles[button.dataset.view];
  $('#breadcrumb').textContent=crumb; $('#page-title').textContent=title; $('#page-description').textContent=description;
  if (button.dataset.view === 'results') try { await refreshResults(); } catch(e) { notify(e.message,true); }
}));
$('#load-button').addEventListener('click', () => task($('#load-button'), async () => {
  if (!$('#match-date').value) throw new Error('Alege o dată.');
  const data = await api(`/api/matches?day=${encodeURIComponent($('#match-date').value)}&refresh=true`);
  state.matches=data.matches; state.source='flashscore'; state.analyses.clear(); renderMatches();
  notify(`${data.matches.length} meciuri încărcate din FlashScore.${data.rejected ? ` ${data.rejected} înregistrări incomplete ignorate.` : ''}${data.settled ? ` ${data.settled} predicții decontate.` : ''}`);
  await Promise.all([refreshHealth(),refreshResults()]);
}));
$('#demo-button').addEventListener('click', () => task($('#demo-button'), async () => {
  const data = await api(`/api/demo?threshold=${threshold()}`);
  state.matches=data.matches; state.source='synthetic'; state.analyses=new Map(data.analyses.map(a => [a.match.id,a]));
  renderMatches(); notify('Mod demo: echipe și scoruri sintetice. Aceste date nu intră în jurnalul real.');
}));
$('#search').addEventListener('input',renderMatches);
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click',() => {
  state.filter=button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active',b===button)); renderMatches();
}));
$('#threshold').addEventListener('change', async () => {
  if(state.busy) return;
  if(state.source === 'synthetic') $('#demo-button').click();
  else {state.analyses.clear(); renderMatches(); notify('Prag schimbat. Reanalizează meciurile; selecțiile deja salvate în jurnal rămân neschimbate.');}
});
$('#match-list').addEventListener('click', event => {
  const button = event.target.closest('[data-match]'); if(!button) return;
  const existing = state.analyses.get(button.dataset.match);
  if(existing) {showAnalysis(existing); return;}
  task(button, async () => {
    const data = await api(`/api/analyze/${encodeURIComponent(button.dataset.match)}`,{method:'POST',body:JSON.stringify({threshold:threshold()})});
    state.analyses.set(data.match.id,data); renderMatches(); showAnalysis(data); await Promise.all([refreshHealth(),refreshResults()]);
  });
});
$('#analyze-all').addEventListener('click',() => task($('#analyze-all'), async () => {
  const matches = state.matches.filter(upcoming).filter(m => !state.analyses.has(m.id)).slice(0,10);
  for(const [index,match] of matches.entries()) {
    notify(`Analiză ${index+1}/${matches.length}: ${match.home} – ${match.away}. Maximum două cereri de istoric per meci; cache activ.`);
    const data=await api(`/api/analyze/${encodeURIComponent(match.id)}`,{method:'POST',body:JSON.stringify({threshold:threshold()})});
    state.analyses.set(match.id,data); renderMatches();
    if(data.warnings.length) {throw new Error(data.warnings.join(' '));}
  }
  await Promise.all([refreshHealth(),refreshResults()]);
  notify(`Analiză încheiată. ${[...state.analyses.values()].filter(a => a.prediction.selection).length} selecții peste prag.`);
}));
$('#close-dialog').addEventListener('click',()=>$('#analysis-dialog').close());
$('#refresh-results').addEventListener('click',()=>task($('#refresh-results'),refreshResults));
$('#run-backtest').addEventListener('click',()=>task($('#run-backtest'),async()=>renderBacktest(await api(`/api/backtest?threshold=${threshold()}`,{method:'POST'}))));
$('#demo-backtest').addEventListener('click',()=>task($('#demo-backtest'),async()=>renderBacktest(await api(`/api/demo/backtest?threshold=${threshold()}`,{method:'POST'}))));
$('#csv-file').addEventListener('change',async event=>{
  const file=event.target.files[0]; if(!file) return;
  await task($('#run-backtest'),async()=>{
    if(file.size>2_000_000) throw new Error('Fișier prea mare. Limita este 2 MB.');
    renderBacktest(await api(`/api/backtest/csv?threshold=${threshold()}`,{method:'POST',headers:{'Content-Type':'text/csv'},body:await file.text()}));
  }); event.target.value='';
});
const today=new Date(); today.setMinutes(today.getMinutes()-today.getTimezoneOffset());
$('#match-date').value=today.toISOString().slice(0,10);
Promise.all([refreshHealth(),refreshResults()]).catch(error=>{ $('#connection').textContent='Backend indisponibil'; notify(error.message,true); });
