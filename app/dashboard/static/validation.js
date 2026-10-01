(() => {
  'use strict';
  const el=id=>document.getElementById(id);
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number=(v,d=2)=>v==null?'-':Number(v).toLocaleString('en-IN',{maximumFractionDigits:d});
  const pct=v=>v==null?'-':`${v>0?'+':''}${number(v)}%`;
  const stamp=ms=>ms?new Date(ms).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})+' IST':'-';
  const labels={positive:'Positive after costs',negative:'Negative after costs',flat:'Flat after costs',pending:'Waiting for 4h window',awaiting_check:'Awaiting check',data_gap:'Cannot verify'};
  let state=null,page=0,lastPublication=null,epoch=0,detailEpoch=0,detail=null,observer=null,searchTimer=null,lastFetch=0,fetching=false,pinnedConfig=false;
  const active=()=>!el('researchValidationPane').hidden;
  const interval=()=>state?.validation?.groups?.find(g=>g.config_id===el('predictionConfig').value)?.interval||state?.snapshot?.config?.market_interval||'15m';
  async function get(params){
    const r=await fetch('/api/research/continuous/validation?'+new URLSearchParams(params),{cache:'no-store',signal:AbortSignal.timeout(30000)});
    const data=await r.json(); if(!r.ok) throw new Error(data.error||`HTTP ${r.status}`); return data;
  }
  function overview(){
    const cfg=el('predictionConfig').value;
    const g=state?.validation?.groups?.find(g=>g.config_id===cfg),p=g?.progress;
    const complete=p?.complete||0, gaps=p?.data_gaps||0,pending=p?.pending||0;
    const checked=g?.candidates?.count||0, positive=Math.round((g?.candidates?.positive_rate||0)*checked);
    const timeframe=interval();
    el('predictionVerdict').textContent=complete<30?`${timeframe} research: not enough evidence yet`:`${timeframe} research milestone reached; reliability not established`;
    el('predictionOutcomeHeading').textContent=`${timeframe} Research Rank Outcomes`;
    el('predictionOutcomeDescription').textContent=`Fixed 4-hour price direction check after publication, using stored ${timeframe} Binance candles. Hypothetical costs included; no bot trade fills.`;
    el('predictionVerdictDetail').textContent=`Qualified research ideas: ${positive} positive out of ${checked} checked after four hours. ${pending} waiting; ${gaps} cannot be verified. ${complete} of 30 observations collected for review. This does not measure Weighted Hybrid trade performance.`;
    el('predictionMetrics').innerHTML=[['Positive / checked',`${positive} / ${checked}`,`${timeframe} research ideas, 4h price check`],['Unverified',gaps,'missing price evidence'],['Average 4h result',pct(g?.candidates?.mean_net_pct),'qualified research ideas, after costs'],['Other scored pairs',pct(g?.controls?.mean_net_pct),`${number(g?.controls?.count||0,0)} checked; not selected calls`]].map(([k,v,s])=>`<dl><dt>${esc(k)}</dt><dd>${esc(v)}</dd><small>${esc(s)}</small></dl>`).join('');
  }
  async function refresh(){
    if(!active() || !state) return;
    const request=++epoch;
    fetching=true;
    el('predictionAuditError').hidden=true;
    el('predictionAuditStatus').textContent='Loading recorded outcomes...';
    try{
      const data=await get({config_id:el('predictionConfig').value,cohort:el('predictionCohort').value,outcome:el('predictionOutcome').value,q:el('predictionSearch').value,page});
      if(request!==epoch)return;
      lastFetch=Date.now();
      page=data.page;
      el('predictionRows').innerHTML=data.items.map(r=>`<tr>
        <td><strong>${esc(r.pair)}</strong><small>${esc(stamp(r.published_ms))}</small></td>
        <td>${r.candidate?'Ranked long':'Scored only; not a call'}<small>Score ${number(r.score)} / threshold ${number(r.threshold)}</small></td>
        <td><span class="prediction-result" data-verdict="${esc(r.verdict)}">${esc(labels[r.verdict])}</span><small>${r.verdict==='data_gap'?'Excluded from success rate':`Window ends ${esc(stamp(r.end_ms))}`}</small></td>
        <td>${number(r.entry_price,8)} / ${number(r.exit_price,8)}<small>Slippage-adjusted prices</small></td>
        <td data-verdict="${esc(r.verdict)}">${pct(r.net_return_pct)}</td>
        <td>${pct(r.mfe_pct)} / ${pct(r.mae_pct)}</td>
        <td><button type="button" data-prediction="${esc(r.id)}">View outcome</button></td></tr>`).join('')||'<tr><td colspan="7">No observations match these filters</td></tr>';
      const s=data.summary;
      el('predictionAuditStatus').textContent=`${data.total} matching records | ${s.positive} positive, ${s.negative} negative, ${s.flat} flat, ${s.pending+s.awaiting_check} waiting, ${s.data_gap} unverified in this cohort | ${data.cohort_counts.candidates} qualified ideas and ${data.cohort_counts.controls} scored-only observations in these settings`;
      el('predictionPage').textContent=`${data.page+1} / ${data.pages}`;
      el('predictionPrevious').disabled=data.page===0;
      el('predictionNext').disabled=data.page+1>=data.pages;
    }catch(e){if(request===epoch){el('predictionAuditStatus').textContent='Results could not be refreshed';el('predictionAuditError').textContent=e.message;el('predictionAuditError').hidden=false;}}
    finally{if(request===epoch)fetching=false;}
  }
  function hideDetail(){detailEpoch++;detail=null;observer?.disconnect();el('predictionDetail').hidden=true;}
  async function showDetail(id,scroll=true){
    const request=++detailEpoch;
    detail=null;observer?.disconnect();
    el('predictionDetail').hidden=false;
    el('predictionDetail').textContent='Loading frozen evidence...';
    try{
      const d=await get({sample_id:id}); if(request!==detailEpoch)return;
      detail=d;const r=d.sample;
      el('predictionDetail').innerHTML=`<div class="predictionDetailHeader"><div><h3>${esc(r.pair)} / ${r.candidate?`${esc(r.interval||'15m')} research rank`:'scored only, not a call'}</h3><p data-verdict="${esc(r.verdict)}"><strong>${esc(labels[r.verdict])}</strong> ${pct(r.net_return_pct)} <span class="subtle">(4h price check, no bot trade)</span></p></div><button type="button" id="predictionDetailClose" aria-label="Close prediction evidence" title="Close prediction evidence">&#215;</button></div>
        <dl><dt>Published</dt><dd>${esc(stamp(r.published_ms))}</dd><dt>Observation window</dt><dd>${esc(stamp(r.first_ms))} to ${esc(stamp(r.end_ms))}</dd><dt>Original score / threshold</dt><dd>${number(r.score)} / ${number(r.threshold)}</dd><dt>Last closed price at scan</dt><dd>${number(d.reference_price,8)}</dd></dl>
        <div class="researchStats">${[['Test start, incl. slippage',number(r.entry_price,8)],['Test finish, incl. slippage',number(r.exit_price,8)],['Net after costs',pct(r.net_return_pct)],['Largest rise in window',pct(r.mfe_pct)],['Largest fall in window',pct(r.mae_pct)],['Stored price candles',`${d.candles.length} / ${d.expected_candles}`]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div>
        <h4>Observed price after publication</h4><canvas id="predictionChart" class="predictionChart" role="img" aria-label="Stored Binance prices during this four-hour observation window"></canvas><p id="predictionChartHover" class="subtle"></p>
        <p class="subtle">${esc(d.source)} | ${d.missing_open_times.length} missing closed candles | ${number(r.rules.fee_bps_per_side)} bps fee per side + ${number(r.rules.slippage_bps)} bps slippage per side</p><p class="subtle">${esc(d.method)}</p>
        <details class="researchSettings"><summary>Frozen source record</summary><pre>${esc(JSON.stringify({sample:r,snapshot_id:d.snapshot_id,original_assessment:d.frozen_assessment,missing_open_times:d.missing_open_times},null,2))}</pre></details>`;
      el('predictionDetailClose').addEventListener('click',hideDetail);
      observer?.disconnect();observer=new ResizeObserver(drawChart);observer.observe(el('predictionChart'));drawChart();
      el('predictionChart').addEventListener('pointermove',e=>{
        const rect=e.currentTarget.getBoundingClientRect(),fraction=Math.max(0,Math.min(1,(e.clientX-rect.left-55)/(rect.width-70))),at=r.first_ms+fraction*r.horizon_ms;
        const bar=d.candles.find(b=>b.open_ms<=at&&b.close_ms>=at);
        el('predictionChartHover').textContent=bar?`${stamp(bar.close_ms)} | Close ${number(bar.close,8)} | High ${number(bar.high,8)} | Low ${number(bar.low,8)}`:'No stored candle at this time';
      });
      if(scroll)el('predictionDetail').scrollIntoView({block:'start',behavior:'smooth'});
    }catch(e){if(request===detailEpoch)el('predictionDetail').textContent=`Evidence unavailable: ${e.message}`;}
  }
  function drawChart(){
    const canvas=el('predictionChart');if(!canvas||!detail)return;
    const w=canvas.getBoundingClientRect().width;if(!w)return;
    const h=240,dpr=window.devicePixelRatio||1;canvas.width=Math.round(w*dpr);canvas.height=h*dpr;
    const c=canvas.getContext('2d');c.scale(dpr,dpr);c.fillStyle='#101216';c.fillRect(0,0,w,h);
    const bars=detail.candles,s=detail.sample,prices=bars.flatMap(b=>[b.low,b.high]);
    if(detail.reference_price>0)prices.push(detail.reference_price);
    c.font='11px sans-serif';c.fillStyle='#aab2c1';
    if(!prices.length){c.fillText('No stored prices for this window',16,35);return;}
    const lo=Math.min(...prices),hi=Math.max(...prices),pad=Math.max((hi-lo)*0.1,hi*0.001),bottom=lo-pad,top=hi+pad;
    const x=ms=>55+(ms-s.first_ms)/s.horizon_ms*(w-70),y=p=>15+(top-p)/(top-bottom)*(h-48);
    c.strokeStyle='#30343d';c.lineWidth=1;
    for(let i=0;i<=3;i++){const p=bottom+(top-bottom)*i/3,yy=y(p);c.beginPath();c.moveTo(55,yy);c.lineTo(w-15,yy);c.stroke();c.fillStyle='#aab2c1';c.fillText(Number(p.toPrecision(4)).toString(),3,yy+3);}
    const step=detail.sample.interval==='5m'?300000:900000;
    for(const ms of detail.missing_open_times){c.fillStyle='#e6b45025';c.fillRect(x(ms),15,x(ms+step)-x(ms),h-48);}
    if(detail.reference_price>0){c.strokeStyle='#e6b450';c.setLineDash([4,4]);c.beginPath();c.moveTo(55,y(detail.reference_price));c.lineTo(w-15,y(detail.reference_price));c.stroke();c.setLineDash([]);}
    let prev=null;
    for(const b of bars){c.strokeStyle='#677183';c.beginPath();c.moveTo(x(b.close_ms),y(b.low));c.lineTo(x(b.close_ms),y(b.high));c.stroke();c.strokeStyle='#52b8c7';c.lineWidth=2;c.beginPath();c.moveTo(x(prev&&prev.open_ms+step===b.open_ms?prev.close_ms:b.open_ms),y(prev&&prev.open_ms+step===b.open_ms?prev.close:b.open));c.lineTo(x(b.close_ms),y(b.close));c.stroke();prev=b;}
    c.fillStyle='#aab2c1';c.fillText('Start',55,h-10);c.textAlign='right';c.fillText('+4h',w-15,h-10);
    el('predictionChartHover').textContent='Cyan: observed prices | Amber dashed: price at scan | Shaded: missing evidence';
  }
  for(const id of ['predictionConfig','predictionCohort','predictionOutcome'])el(id).addEventListener('change',()=>{if(id==='predictionConfig')pinnedConfig=true;page=0;hideDetail();overview();refresh();});
  el('predictionSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=0;hideDetail();refresh();},200);});
  el('predictionPrevious').addEventListener('click',()=>{page--;refresh();});
  el('predictionNext').addEventListener('click',()=>{page++;refresh();});
  el('predictionRows').addEventListener('click',e=>{const b=e.target.closest('[data-prediction]');if(b)showDetail(b.dataset.prediction);});
  document.querySelector('[data-research-view=validation]').addEventListener('click',()=>setTimeout(refresh,0));
  window.researchValidationView={update(data){
    state=data;
    const select=el('predictionConfig'),selected=pinnedConfig?select.value:data.snapshot?.config_id;
    const options=(data.validation?.groups||[]).map(g=>`<option value="${esc(g.config_id)}">${g.config_id===data.snapshot?.config_id?'Current settings':esc(g.config_id)}</option>`).join('');
    if(select.innerHTML!==options){select.innerHTML=options;if([...select.options].some(o=>o.value===selected))select.value=selected;}
    overview();
    if(active()&&!fetching&&(lastPublication!==data.snapshot?.id||Date.now()-lastFetch>60000)){
      const changed=lastPublication!==data.snapshot?.id;lastPublication=data.snapshot?.id;refresh();
      if(changed&&detail&&!['positive','negative','flat'].includes(detail.sample.verdict))showDetail(detail.sample.id,false);
    }
  }};
})();
