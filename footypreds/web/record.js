'use strict';
// Rezultate (#/rezultate): AI ticket history, the prospective ledger and its calibration.
// Metodă (#/metoda): methodology per sport, known limitations and the football benchmark.

function renderRecord() {
  const scope = currentScope;
  const app = $('#app');
  const sports = chosenSports();
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">Rezultate</p>
        <h1>Tot ce a recomandat AI-ul, verificat cu scorul final</h1>
        <p class="lead">Biletele zilei rămân blocate după începerea meciurilor, iar selecțiile din jurnal sunt salvate înainte de start. Câștigurile și pierderile apar la fel.</p>
      </div>
    </section>
    <section class="section"><div class="section-head"><div><h2>Biletele AI</h2><p class="muted small" id="history-note">Ultimele 60 de zile · ${esc(sports.map(s => SPORT_LABEL[s]).join(', '))}</p></div></div>
      <div id="history-summary" class="summary-grid">${skeletonCards(4)}</div>
      <div id="history-days" class="history-days"></div></section>
    <section class="section"><div class="section-head"><div><h2>Jurnalul selecțiilor (prag 85%)</h2><p class="muted small">Prima selecție salvată înainte de start; decontată la încărcarea zilei după final.</p></div></div>
      <div id="ledger">${loadingBlock()}</div></section>
    ${disclaimerBox()}`;
  loadTicketHistory(scope, sports);
  loadLedger(scope);
}

async function loadTicketHistory(scope, sports) {
  let data;
  try {
    data = await api(`/api/recommendations/history?sports=${sports.join(',')}&days=60`);
  } catch (error) {
    if (!scope.alive) return;
    $('#history-summary').innerHTML = errorState(error);
    return;
  }
  if (!scope.alive) return;
  if (data.note) $('#history-note').textContent = `Ultimele 60 de zile · ${sports.map(s => SPORT_LABEL[s]).join(', ')} · ${data.note}`;
  const summary = data.summary || [];
  $('#history-summary').innerHTML = summary.length ? summary.map(s => `<div class="card summary-card">
      <div class="sc-head"><span class="ticket-badge small">x${esc(Number(s.target) >= 10 ? Math.round(s.target) : s.target)}</span><b>${esc(plural(s.tickets, 'bilet', 'bilete'))}</b></div>
      <div class="sc-row"><span class="badge badge-won">${esc(s.won)} câștigate</span><span class="badge badge-lost">${esc(s.lost)} pierdute</span>${s.void ? `<span class="badge badge-void">${esc(s.void)} anulate</span>` : ''}${s.pending ? `<span class="badge badge-pending">${esc(s.pending)} în așteptare</span>` : ''}</div>
      <div class="kpi-grid kpi-3 small-kpis">${kpi('Rată câștig', isNum(s.hit_rate) ? pct(s.hit_rate, 0) : '—')}${kpi('ROI', signedPct(s.roi, 0), '', toneOf(s.roi))}${kpi('Profit', `${isNum(s.profit) ? (s.profit > 0 ? '+' : '') + num(s.profit, 2) : '—'} u`, '', toneOf(s.profit))}</div>
    </div>`).join('') : emptyState('Nu există încă bilete AI salvate.', 'Biletele apar aici după ce deschizi pagina principală într-o zi cu meciuri.', '<a class="btn btn-primary" href="#/">Vezi biletele zilei</a>');
  hydrate($('#history-summary'));
  const days = data.days || [];
  $('#history-days').innerHTML = days.length ? `<div class="card table-card"><div class="table-wrap"><table class="table"><thead><tr><th>Ziua</th><th>Bilete</th></tr></thead><tbody>${days.map(d => `<tr><td class="nowrap">${esc(fmtLongDay(d.day))}</td><td><div class="ticket-chips">${(d.tickets || []).map(t => `<span class="tchip tchip-${esc(t.status)}" title="${esc(STATUS_LABEL[t.status] || t.status)}"><b>x${esc(Number(t.target) >= 10 ? Math.round(t.target) : t.target)}</b>${isNum(t.total_odds) ? ` ${num(t.total_odds)}` : ''} · ${esc(STATUS_LABEL[t.status] || t.status)}</span>`).join('')}</div></td></tr>`).join('')}</tbody></table></div></div>` : '';
}

async function loadLedger(scope) {
  let data;
  try {
    data = await api('/api/results');
  } catch (error) {
    if (!scope.alive) return;
    $('#ledger').innerHTML = errorState(error);
    return;
  }
  if (!scope.alive) return;
  const {metrics = {}, rows = []} = data;
  const sports = new Set(chosenSports());
  const shown = rows.filter(r => sports.has(r.match?.sport || 'football'));
  const decided = (metrics.settled || 0);
  $('#ledger').innerHTML = `
    <div class="kpi-grid kpi-4">
      ${kpi('Selecții decontate', esc(decided), `${esc(metrics.pending ?? 0)} în așteptare${metrics.void ? ` · ${esc(metrics.void)} anulate` : ''}`)}
      ${kpi('Câștigate · pierdute', `${esc(metrics.wins ?? 0)} · ${esc(decided - (metrics.wins || 0))}`)}
      ${kpi('Rată de reușită', pct(metrics.accuracy, 1), metrics.interval95 ? `interval 95%: ${pct(metrics.interval95[0], 0)}–${pct(metrics.interval95[1], 0)}` : '')}
      ${kpi('Acoperire', pct(metrics.coverage), `din ${esc(plural(metrics.total_matches ?? 0, 'meci analizat', 'meciuri analizate'))}`)}
    </div>
    <div class="card panel"><h2 class="panel-title">Știe modelul cât de sigur este?</h2><p class="small muted">Probabilitatea declarată (gri) față de cât s-a câștigat efectiv (culoare), pe benzi.</p>
      ${(metrics.calibration || []).length ? `<div class="calibration">${metrics.calibration.map(b => `<div class="cal-band">
          <b>${esc(b.range)}</b><span class="muted small">${esc(plural(b.count, 'selecție', 'selecții'))}</span>
          <div class="cal-bars"><span class="cal-track"><span class="cal-pred" data-w="${clamp01(b.predicted)}"></span></span><span class="cal-track"><span class="cal-act ${b.actual + 0.02 < b.predicted ? 'under' : ''}" data-w="${clamp01(b.actual)}"></span></span></div>
          <span class="small">model ${pct(b.predicted, 0)} · real ${pct(b.actual, 0)}</span></div>`).join('')}</div>` : emptyState('Calibrarea apare după primele selecții decontate.')}
    </div>
    <div class="card table-card"><div class="table-wrap">${shown.length ? `<table class="table"><thead><tr><th>Data</th><th>Meci</th><th>Selecție</th><th class="num">Model</th><th class="num">Scor</th><th>Rezultat</th></tr></thead><tbody>${shown.map(r => {
      const m = r.match || {};
      const sel = r.prediction?.selection || {};
      const res = r.result ? (r.result.won == null ? statusBadge('void') : statusBadge(r.result.won ? 'won' : 'lost')) : statusBadge('pending');
      return `<tr><td class="small nowrap">${esc(m.kickoff ? fmtShortDate(m.kickoff) : '')}</td><td><a href="${matchHref(m.id, m.sport)}">${esc(m.home)} – ${esc(m.away)}</a><br><span class="muted small">${sportIcon(m.sport || 'football')} ${esc(leagueName(m.league))}</span></td><td>${esc(sel.label)}</td><td class="num">${pct(sel.probability)}</td><td class="num">${esc(r.result?.score ?? '—')}</td><td>${res}</td></tr>`;
    }).join('')}</tbody></table>` : emptyState('Nicio selecție în jurnal încă.', 'Deschide meciuri viitoare: prima analiză cu o selecție peste prag este salvată aici.')}</div></div>`;
  hydrate($('#ledger'));
}

// --- methodology -----------------------------------------------------------------------------

const METHOD = {
  football: ['Fotbal', [
    ['Rating atac/apărare', 'Un model Poisson ponderat în timp (rezultatele vechi de un an contează pe jumătate) estimează puterea ofensivă și defensivă a fiecărei echipe din toate competițiile, ținând cont de adversari și de avantajul terenului.'],
    ['Formă și meciuri directe', 'Ultimele meciuri sunt comparate cu ce aștepta ratingul; meciurile directe au o pondere mică, aleasă pe sezonul de validare.'],
    ['Matricea de scoruri', 'Golurile așteptate devin o matrice Dixon-Coles din care rezultă 1X2, șansă dublă, total goluri, GG, scor corect, handicap și pauză/final.'],
    ['Piața', 'Când există cote 1X2, probabilitățile 1X2 ale modelului sunt combinate cu cele ale caselor, fără marjă (pondere aleasă pe validare). Când există cotă peste/sub 2.5, probabilitatea de peste 2.5 este chiar cea a pieței fără marjă, iar toată matricea de scoruri se rescalează ca să fie de acord cu ea.'],
    ['Calibrarea golurilor', 'Fără cotă peste/sub 2.5, probabilitatea de goluri trece printr-o hartă de calibrare potrivită pe sezonul 2024-25 (modelul vechi era prea încrezător). 1X2 nu se schimbă.'],
  ]],
  basketball: ['Baschet', [
    ['Rating de puncte', 'Puncte marcate și primite, ponderate în timp, ajustate la adversari, la avantajul terenului și la durata meciului (48 de minute NBA, 40 în rest).'],
    ['Odihnă și formă', 'Meciurile jucate în zile consecutive (back-to-back) și seriile recente modifică ușor diferența estimată.'],
    ['Distribuții', 'Diferența și totalul sunt modelate ca distribuții normale; de aici rezultă câștigătorul (cu prelungiri), handicapurile și totalurile.'],
    ['Piața', 'Liniile caselor (handicap, total) sunt combinate cu modelul; ponderea implicită 0.85 este prudentă, fără un set de cote istorice pentru reglare.'],
  ]],
  tennis: ['Tenis', [
    ['Elo pe suprafață', 'Fiecare jucător are un rating Elo general și unul pe suprafață (hard, zgură, iarbă), actualizat după fiecare meci.'],
    ['Seturi și game-uri', 'Șansa de a câștiga un set dă distribuția scorului la seturi (2-0, 2-1…), handicapul și totalul de seturi; totalul de game-uri este doar afișat.'],
    ['Piața', 'Când există cote, probabilitatea de câștig este prețul pieței fără marjă (validat); Elo decide doar meciurile fără cote.'],
    ['Abandon', 'Un abandon sau un walkover anulează toate pariurile pe acel meci.'],
  ]],
};

const METHOD_COMMON = [
  ['Bilete AI', 'Pentru o cotă țintă, un optimizator exact caută combinația de selecții (cel mult una pe meci, niciodată aceeași echipă de două ori) cu cea mai mare probabilitate combinată și cota totală în intervalul 0.93–1.12 × țintă. Selecțiile au cote reale între 1.08 și 4.0, nu pot fi rambursate și au note A–C; nota D este acceptată doar pe piețe cotate complet, unde probabilitatea se sprijină pe prețul pieței. Valoarea se măsoară față de prețul corect al casei, fără marjă (probabilitate × cotă × marja pieței): 1.00 înseamnă că modelul este de acord cu piața. Se acceptă 0.97–1.10: sub 0.97 modelul vede selecția clar mai slab decât piața, peste 1.10 o contrazice fără dovezi că ar avea dreptate (plafon prudent, nu optim măsurat).'],
  ['Live', 'Probabilitățile în joc pornesc de la estimarea de dinainte de meci și se actualizează cu scorul și timpul rămas. Cotele listei FlashScore sunt de dinainte de meci, de aceea afișăm cota corectă și cota minimă, nu cote live.'],
  ['Simulator', 'Walk-forward orb: pentru fiecare zi, modelul vede doar rezultatele din zilele anterioare, fixează miza și abia apoi află scorurile, decontate la cotele istorice. Aceleași reguli de selecție ca biletele AI. Sezonul 2024-25 de fotbal este în eșantion (calibrarea a fost potrivită pe el).'],
];

const LIMITATIONS = [
  'Modelul nu are un avantaj măsurat față de prețurile pieței: simulările arată că biletele AI și selecțiile simple pierd în jur de marja casei pe seturile de fotbal testate (2025-26, în afara eșantionului) și adesea mai mult decât pariul pe favoritul casei.',
  'Probabilitățile de goluri sunt acum calibrate (2025-26: log loss la peste 2.5 egal cu al caselor cu cotă, abatere medie de calibrare ~1,5 puncte), dar calibrat nu înseamnă profitabil: cu cotă, probabilitatea este chiar a pieței.',
  'Pe seturile istorice cu cote medii (marjă mare), selecțiile peste/sub 2.5 nu trec pragul de valoare, deci simulatorul nu le testează.',
  'La tenis, probabilitatea de câștig este prețul pieței: modelul nu are avantaj pe această piață.',
  'Probabilitatea unui bilet presupune că meciurile sunt independente.',
  'Modelul nu știe de accidentări, suspendări, loturi, vreme sau motivație.',
  'Recomandările folosesc ziua UTC: un meci la 00:30 ora României aparține zilei precedente.',
];

async function renderMethod() {
  const scope = currentScope;
  const app = $('#app');
  const sports = chosenSports();
  app.innerHTML = `
    <section class="hero hero-compact">
      <div class="hero-text">
        <p class="eyebrow">Metodă</p>
        <h1>Cum calculăm probabilitățile</h1>
        <p class="lead">Fără cutie neagră: fiecare probabilitate pornește din rezultate reale, iar modelul de fotbal este testat pe un sezon pe care nu l-a văzut.</p>
      </div>
    </section>
    <div class="method-grid">${sports.map(sport => `<article class="card panel method-card sport-${sport}">
        <h2 class="panel-title">${sportTag(sport)}</h2>
        <ol class="steps">${METHOD[sport][1].map(([title, text]) => `<li><b>${esc(title)}.</b> ${esc(text)}</li>`).join('')}</ol>
      </article>`).join('')}</div>
    <section class="section grid grid-3">${METHOD_COMMON.map(([title, text]) => `<div class="card panel"><h2 class="panel-title">${esc(title)}</h2><p>${esc(text)}</p></div>`).join('')}</section>
    <section class="section grid grid-2">
      <div class="card panel"><h2 class="panel-title">Limitări cunoscute (măsurate, nu ascunse)</h2><ul class="insights">${LIMITATIONS.map(l => `<li>${esc(l)}</li>`).join('')}</ul>
        <p class="small muted">Calitatea A–D arată câte date recente există pentru ambele echipe; la D predicția este afișată, dar nu intră în bilete.</p></div>
      <div class="card panel"><h2 class="panel-title">Benchmark fotbal</h2><div id="benchmark">${loadingBlock()}</div></div>
    </section>
    ${disclaimerBox()}`;
  try {
    const report = await api('/api/benchmark');
    if (!scope.alive) return;
    $('#benchmark').innerHTML = benchmarkView(report);
  } catch (error) {
    if (!scope.alive) return;
    $('#benchmark').innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
  }
}

// football-data season code "2526" -> "2025-26".
const seasonLabel = code => /^\d{4}$/.test(String(code)) ? `20${String(code).slice(0, 2)}-${String(code).slice(2)}` : String(code ?? '');

function benchmarkView(report) {
  const names = {model: 'Model V8', model_market: 'V8 + piață', v7: 'V7 (vechi)', league_frequency: 'Frecvența ligii', bookmaker: 'Casele de pariuri'};
  const x = report.one_x_two || {};
  const sub = report.same_odds_subset || {};
  const row = (label, m) => m ? `<tr><td>${esc(label)}</td><td class="num">${pct(m.accuracy, 1)}</td><td class="num">${num(m.log_loss, 4)}</td></tr>` : '';
  const rows = ['model', 'model_market', 'v7', 'league_frequency'].map(k => row(names[k], x[k])).join('') + row(names.bookmaker, sub.bookmaker);
  const sel = report.selected?.model_market;
  const goals = report.over25_vs_bookmaker;
  return `<p class="small muted">Sezonul ${esc(seasonLabel(report.holdout_season))}: ${esc(plural(report.holdout_matches ?? 0, 'meci', 'meciuri'))}, evaluate cronologic, fără ca modelul să vadă scorurile.</p>
    <div class="table-wrap"><table class="table compact"><thead><tr><th>1X2</th><th class="num">Acuratețe</th><th class="num">Log loss ↓</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${goals ? `<div class="table-wrap"><table class="table compact"><thead><tr><th>Peste 2.5</th><th class="num">Acuratețe</th><th class="num">Log loss ↓</th></tr></thead><tbody>${row(names.model_market, goals.model_market)}${row(names.bookmaker, goals.bookmaker)}</tbody></table></div>` : ''}
    ${sel ? `<p class="small">Selecții ≥ ${pct(report.protocol?.selection_threshold)}: <b>${esc(sel.wins)}/${esc(sel.settled)}</b> reușite (${pct(sel.accuracy, 1)}), acoperire ${pct(sel.coverage)}.</p>` : ''}
    <a class="btn btn-ghost btn-small" href="${API_BASE}/api/benchmark/markdown">Raport complet (.md)</a>`;
}
