'use strict';
const studio = {mode:'week', active:null, plans:[], timer:null, pollingErrors:0, competitions:new Set(), catalog:[]};
const statusLabels = {pending:'ÎN AȘTEPTARE',won:'REUȘIT',lost:'NEREUȘIT',generating:'SE GENEREAZĂ',ready:'GENERAT',partial:'INCOMPLET',failed:'OPRIT',interrupted:'ÎNTRERUPT',waiting:'ÎN AȘTEPTARE',analyzing:'ANALIZĂ',unavailable:'FĂRĂ BILET'};
const planDay = value => new Date(`${value}T12:00:00Z`).toLocaleDateString('ro-RO',{day:'numeric',month:'short',timeZone:'UTC'});
function chooseMode(mode) {
  studio.mode=mode;
  document.querySelectorAll('[data-mode]').forEach(button=>{button.classList.toggle('selected',button.dataset.mode===mode);button.setAttribute('aria-pressed',String(button.dataset.mode===mode));});
  $('#target-context').textContent=mode==='week'?'/ FIECARE ZI':'/ UN SINGUR BILET';
  if(!studio.active || studio.active.status!=='generating') $('#generate-plan').innerHTML=`<span>✦</span> ${mode==='week'?'Generează planul':'Generează biletul'} <b>↗</b>`;
}
function setGenerating(generating) {
  $('#generate-plan').disabled=generating;
  $('#generate-plan').innerHTML=generating?'<span>◌</span> Generare în curs… <b>↗</b>':`<span>✦</span> ${studio.mode==='week'?'Generează planul':'Generează biletul'} <b>↗</b>`;
}
function renderPlan(plan) {
  studio.active=plan;
  try{localStorage.setItem('footypreds-plan',plan.id);}catch{}
  const running=plan.status==='generating';
  setGenerating(running);
  $('#board-title').textContent=plan.request.mode==='week'?`Plan 7 zile · cotă țintă ${plan.request.target_odds.toFixed(2)}`:`Bilet custom · cotă țintă ${plan.request.target_odds.toFixed(2)}`;
  $('#generation-progress').hidden=false;
  $('#generation-message').textContent=plan.message;
  $('#generation-fraction').textContent=`${plan.progress}/${plan.days.length}`;
  $('#plan-progress').max=plan.days.length; $('#plan-progress').value=plan.progress;
  $('#refresh-active-plan').disabled=running;
  const badge=plan.source==='synthetic'?'<span class="source-tag demo">DEMO · COTE SINTETICE</span>':'<span class="source-tag">FLASHSCORE · COTE INDICATIVE</span>';
  const rail=plan.days.length>1?`<div class="week-track">${plan.days.map((d,i)=>`<div class="week-step ${d.status}"><b>${d.ticket?'✓':i+1}</b><span>${planDay(d.date)}</span></div>`).join('')}</div>`:'';
  $('#plan-board').innerHTML=`<div class="plan-meta">${badge}<span>Prag/selecție ${pct(plan.request.min_probability)} · max. ${plan.request.max_legs} selecții · UTC</span></div><p class="plan-meta">${escapeHtml(plan.request.competitions?.length?plan.request.competitions.map(id=>studio.catalog.find(c=>c.id===id)?.name||id.split('|')[1]).join(' + '):'Toate competi?iile')}</p>${rail}<div class="ticket-grid">${plan.days.map((d,i)=>ticketCard(d,i)).join('')}</div>${plan.warnings.map(w=>`<p class="notice">${escapeHtml(w)}</p>`).join('')}`;
}
function ticketCard(d,index) {
  const ticket=d.ticket;
  const stats=d.diagnostics;
  const diagnostic=stats?`<div class="ticket-diagnostics">${stats.in_competitions} meciuri în competiții · ${stats.with_odds} cu cote<br>${d.analyzed??0} analizate · ${stats.insufficient_history} fără istoric suficient · ${stats.below_threshold} sub prag<br>${d.candidates??0} selecții eligibile${stats.with_odds>stats.analysis_limit?` · limită de analiză: ${stats.analysis_limit}`:''}${historyDetails(stats)}</div>`:'';
  const heading=`<div class="ticket-card-top"><span class="day-index">DAY ${String(index+1).padStart(2,'0')}</span><span class="ticket-card-date">${planDay(d.date)}</span></div>${diagnostic}`;
  if(!ticket) return `<article class="ticket-card ${d.status}">${heading}<div class="slip-body"><span class="ticket-status">${statusLabels[d.status]||d.status}</span><div class="no-ticket-icon">${d.status==='analyzing'?'⌁':d.status==='waiting'?'◷':'—'}</div><p class="unavailable-reason">${escapeHtml(d.reason||(d.status==='analyzing'?'Analizăm istoricul și cotele. Biletul apare aici când generarea este gata.':'Ziua este în coada de generare.'))}</p></div></article>`;
  return `<article class="ticket-card ${ticket.status}">${heading}<div class="slip-body"><span class="slip-label">COTĂ TOTALĂ DISPONIBILĂ</span><div class="slip-odds">${ticket.total_odds.toFixed(2)}<small>×</small></div><span class="ticket-status ${ticket.status}">${statusLabels[ticket.status]}</span><div class="slip-stats"><span>${ticket.legs.length} selecții · estimare combinată</span><b>${pct(ticket.estimated_probability)}</b></div>${ticket.legs.slice(0,2).map(leg=>`<div class="slip-preview"><div><span>${escapeHtml(leg.home)} – ${escapeHtml(leg.away)}</span><strong>${leg.odds.toFixed(2)}</strong></div><small>${escapeHtml(leg.label)}</small></div>`).join('')}${ticket.legs.length>2?`<small class="muted">+ ${ticket.legs.length-2} selecții în bilet</small>`:''}<button class="ticket-details" data-ticket-day="${index}">Vezi biletul complet ↗</button></div></article>`;
}
function historyDetails(stats) {
  if(!stats.match_details?.length)return '';
  return `<details class="history-details"><summary>Vezi istoricul per meci</summary><p>Minimum 8 rezultate/echipă în aceeași competiție, în ultimii 2 ani, cu un rezultat în ultimele 90 zile. Numărul total al meciurilor echipei poate fi mai mare.</p>${stats.deep_history_matches?`<p>Istoric extins (paginile 2–3): ${stats.deep_history_matches} meciuri.</p>`:''}${stats.match_details.map(m=>`<div class="history-match"><b>${escapeHtml(m.home)} – ${escapeHtml(m.away)}</b><span>Gazde: ${m.sample.home}/8 · Oaspeți: ${m.sample.away}/8</span><small>${m.quality!=='sufficient'?'Istoric insuficient sau fără rezultate recente':m.eligible?'Selecție eligibilă':'Pragul nu este atins pe piețele cu cote'}</small></div>`).join('')}</details>`;
}
function showTicket(index) {
  const plan=studio.active,d=plan.days[index],ticket=d.ticket;
  if(!ticket)return;
  $('#analysis-content').innerHTML=`<h2 class="analysis-title">${plan.request.mode==='week'?`Ziua ${index+1} din 7`:'Bilet custom'} · ${planDay(d.date)}</h2><p class="analysis-sub">${plan.source==='synthetic'?'DEMO · ECHIPE ȘI COTE SINTETICE':'FLASHSCORE · COTE INDICATIVE'} · ${statusLabels[ticket.status]}</p><div class="analysis-numbers"><div><strong>${ticket.total_odds.toFixed(2)}×</strong>Cotă totală</div><div><strong>${pct(ticket.estimated_probability)}</strong>Probabilitate combinată estimată</div><div><strong>${ticket.legs.length}</strong>Selecții în bilet</div></div>${ticket.legs.map((leg,i)=>`<article class="slip-detail-leg"><span class="eyebrow">SELECȚIA ${i+1} · ${statusLabels[leg.status]}</span><h4>${escapeHtml(leg.home)} – ${escapeHtml(leg.away)}</h4><p>${escapeHtml(leg.league)} · ${new Date(leg.kickoff).toLocaleString('ro-RO')}</p><div class="slip-detail-quote"><span>${escapeHtml(leg.label)}</span><b>${leg.odds.toFixed(2)}×</b></div><p>Probabilitate individuală: ${pct(leg.probability)}${leg.score?` · Scor final: ${escapeHtml(leg.score)}`:''}</p></article>`).join('')}<p class="footnote">Cotele au fost capturate la generare și pot varia. ${escapeHtml(ticket.probability_assumption)} Pragul individual din Match center nu este o promisiune pentru biletul combinat. Biletul nu a fost plasat la o casă de pariuri.</p>`;
  $('#analysis-dialog').showModal();
}
async function refreshPlans(restore=false) {
  const data=await api('/api/plans'); studio.plans=data.plans;
  const real=data.plans.filter(p=>p.source!=='synthetic');
  const tickets=real.flatMap(p=>p.days.map(d=>d.ticket).filter(Boolean));
  $('#plan-count').textContent=real.length;
  $('#ticket-count').textContent=tickets.length;
  $('#settled-ticket-count').textContent=tickets.filter(t=>t.status!=='pending').length;
  $('#saved-plans').innerHTML=data.plans.length?data.plans.map(p=>{
    const count=p.days.filter(d=>d.ticket).length;
    return `<article class="saved-plan"><div class="saved-plan-top"><span class="source-tag ${p.source==='synthetic'?'demo':''}">${p.source==='synthetic'?'DEMO':'FLASHSCORE'}</span><span class="ticket-status">${statusLabels[p.status]}</span></div><h3>${p.request.mode==='week'?'7 days / game plan':'Custom / quick build'}</h3><p>Cotă țintă ${p.request.target_odds.toFixed(2)} · ${planDay(p.request.start_date)} · ${count}/${p.days.length} bilete</p><progress value="${count}" max="${p.days.length}"></progress><div class="saved-plan-bottom"><span>${escapeHtml(p.message)}</span><button data-open-plan="${p.id}">Deschide ↗</button></div></article>`;
  }).join(''):'<div class="board-empty"><span>▦</span><h3>Primul tău plan încă nu a fost creat.</h3><p>Deschide Ticket Lab și configurează un plan de 7 zile sau un bilet custom.</p></div>';
  if(restore){
    let last;try{last=localStorage.getItem('footypreds-plan');}catch{}
    const plan=data.plans.find(p=>p.status==='generating')||data.plans.find(p=>p.id===last)||data.plans[0];
    if(plan){renderPlan(plan);if(plan.status==='generating')pollPlan(plan.id);}
  }
}
async function pollPlan(id) {
  clearTimeout(studio.timer);
  try {
    const plan=await api(`/api/plans/${encodeURIComponent(id)}`);
    studio.pollingErrors=0;renderPlan(plan);
    if(plan.status==='generating') studio.timer=setTimeout(()=>pollPlan(id),1000);
    else {await refreshPlans();if(plan.status==='failed'||plan.status==='interrupted')notify(plan.message,true);}
  } catch(error){
    studio.pollingErrors++;
    if(studio.pollingErrors<4)studio.timer=setTimeout(()=>pollPlan(id),2000);
    else {notify('Conexiunea cu generarea s-a întrerupt. Reîncarcă Planurile mele pentru status.',true);setGenerating(false);}
  }
}
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>chooseMode(button.dataset.mode)));
document.querySelectorAll('[data-odds]').forEach(button=>button.addEventListener('click',()=>{
  $('#target-odds').value=button.dataset.odds;
  document.querySelectorAll('[data-odds]').forEach(b=>b.classList.toggle('selected',b===button));
  if(Number(button.dataset.odds)>=10)$('#max-legs').value='5';
}));
$('#target-odds').addEventListener('input',()=>document.querySelectorAll('[data-odds]').forEach(b=>b.classList.toggle('selected',Number(b.dataset.odds)===Number($('#target-odds').value))));
$('#generate-plan').addEventListener('click',async()=>{
  if(studio.active?.status==='generating')return;
  if(!$('#target-odds').reportValidity()||!$('#plan-date').reportValidity())return;
  setGenerating(true);notify('');
  try {
    const body={mode:studio.mode,start_date:$('#plan-date').value,target_odds:Number($('#target-odds').value),max_legs:Number($('#max-legs').value),min_probability:Number($('#min-probability').value),diverse_leagues:$('#diverse-leagues').checked,demo:$('#plan-source').value==='demo',competitions:[...studio.competitions]};
    const plan=await api('/api/plans',{method:'POST',body:JSON.stringify(body)});
    renderPlan(plan);studio.pollingErrors=0;pollPlan(plan.id);
  } catch(error){notify(error.message,true);setGenerating(false);}
});
$('#plan-board').addEventListener('click',event=>{const b=event.target.closest('[data-ticket-day]');if(b)showTicket(Number(b.dataset.ticketDay));});
$('#saved-plans').addEventListener('click',async event=>{
  const button=event.target.closest('[data-open-plan]');if(!button)return;
  try{const plan=await api(`/api/plans/${button.dataset.openPlan}`);clearTimeout(studio.timer);renderPlan(plan);document.querySelector('[data-view="studio"]').click();if(plan.status==='generating')pollPlan(plan.id);}catch(error){notify(error.message,true);}
});
$('#reload-plans').addEventListener('click',()=>task($('#reload-plans'),()=>refreshPlans(true)));
$('#refresh-active-plan').addEventListener('click',()=>task($('#refresh-active-plan'),async()=>{
  if(!studio.active)return;
  renderPlan(await api(`/api/plans/${studio.active.id}/refresh`,{method:'POST'}));await refreshPlans();
}));
document.querySelector('[data-view="plans"]').addEventListener('click',()=>refreshPlans().catch(error=>notify(error.message,true)));
const utcToday=new Date().toISOString().slice(0,10);
$('#plan-date').value=utcToday;$('#plan-date').min=utcToday;
$('#plan-date').max=new Date(Date.now()+30*86400000).toISOString().slice(0,10);
refreshPlans(true).catch(error=>notify(error.message,true));
