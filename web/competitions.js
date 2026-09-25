'use strict';
const competitionPanel=document.createElement('section');
competitionPanel.className='competition-picker';
competitionPanel.innerHTML=`<div class="competition-heading"><label for="competition-search">LIGI & COMPETIȚII</label><button id="clear-competitions" class="text-button" type="button">Toate</button></div><p id="competition-summary" aria-live="polite">Toate competițiile · poți combina mai multe</p><input id="competition-search" type="search" placeholder="Caută ligă, cupă sau țară…"><div id="competition-list" class="competition-list" role="group" aria-label="Competiții pentru bilete"></div><div class="competition-footer"><button id="load-competitions" class="text-button" type="button">Actualizează lista ↻</button><span id="competition-note" role="status">Lista salvată local.</span></div><p id="diversity-hint" class="muted" hidden>O singură competiție: poți combina mai multe meciuri din ea.</p>`;
$('.diversity-option').before(competitionPanel);
let catalogVersion=0;
let diversityBeforeSingle=true;
function renderCompetitions() {
  const query=$('#competition-search').value.toLocaleLowerCase('ro');
  const rows=[...studio.catalog];
  for(const id of studio.competitions)if(!rows.some(c=>c.id===id))rows.unshift({id,name:id.split('|')[1],country:id.split('|')[0],count:0});
  const filtered=rows.filter(c=>`${c.name} ${c.country}`.toLocaleLowerCase('ro').includes(query));
  $('#competition-list').innerHTML=filtered.map(c=>`<label class="competition-option ${studio.competitions.has(c.id)?'selected':''}"><input type="checkbox" value="${escapeHtml(c.id)}" ${studio.competitions.has(c.id)?'checked':''}><span><b>${escapeHtml(c.name)}</b><small>${escapeHtml(c.country||'Demo')}</small></span><em title="Meciuri viitoare în prima zi">${c.count}</em></label>`).join('')||'<p class="muted">Nicio competiție găsită.</p>';
  const selected=rows.filter(c=>studio.competitions.has(c.id));
  $('#competition-summary').textContent=selected.length?selected.map(c=>c.name).join(' + '):'Toate competițiile · poți combina mai multe';
  const single=studio.competitions.size===1, diversity=$('#diverse-leagues');
  if(single&&!diversity.disabled){diversityBeforeSingle=diversity.checked;diversity.checked=false;}
  if(!single&&diversity.disabled)diversity.checked=diversityBeforeSingle;
  diversity.disabled=single;$('#diversity-hint').hidden=!single;
}
async function loadCompetitions(refresh=false) {
  const version=++catalogVersion;
  const params=new URLSearchParams({day:$('#plan-date').value,demo:String($('#plan-source').value==='demo'),refresh:String(refresh)});
  $('#competition-note').textContent='Se încarcă…';
  try {
    const data=await api(`/api/competitions?${params}`);
    if(version!==catalogVersion)return;
    studio.catalog=data.competitions;renderCompetitions();
    $('#competition-note').textContent=data.source==='local'?'Numere din istoricul local · prima zi. Actualizează pentru programul complet.':'Meciuri în prima zi · 0 = fără meciuri disponibile.';
  }catch(error){if(version===catalogVersion)$('#competition-note').textContent=error.message;}
}
$('#competition-list').addEventListener('change',event=>{
  const input=event.target.closest('input[type="checkbox"]');if(!input)return;
  if(input.checked&&studio.competitions.size>=30){input.checked=false;notify('Poți selecta maximum 30 de competiții.',true);return;}
  if(input.checked)studio.competitions.add(input.value);else studio.competitions.delete(input.value);
  renderCompetitions();
});
$('#clear-competitions').addEventListener('click',()=>{studio.competitions.clear();renderCompetitions();});
$('#competition-search').addEventListener('input',renderCompetitions);
$('#load-competitions').addEventListener('click',()=>task($('#load-competitions'),()=>loadCompetitions(true)));
$('#plan-date').addEventListener('change',()=>loadCompetitions());
$('#plan-source').addEventListener('change',()=>{studio.competitions.clear();studio.catalog=[];renderCompetitions();loadCompetitions();});
loadCompetitions();
