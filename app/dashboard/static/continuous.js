(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (v, digits=2) => v == null ? '-' : Number(v).toLocaleString(undefined,{maximumFractionDigits:digits});
  const stamp = ms => ms ? new Date(ms).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' IST' : 'Not available';
  let state = null, selected = null, fetching = null, displayedSnapshot = null;
  let historyPage = 0, history = [], historyEvidence = null;
  let lastContact = null, clockOffset = 0, disconnected = false, commandPending = false, requestEpoch = 0;
  const signed = v => v == null ? '-' : `${v>0?'+':''}${num(v)}`;
  const words = s => String(s ?? '-').replaceAll('_',' ');
  const lastMarkup = new Map();
  const replace = (id, html) => { if(lastMarkup.get(id)!==html) { el(id).innerHTML=html; lastMarkup.set(id,html); } };
  const assessmentLabel = r => r.tier==='unscanned' ? 'Not checked this cycle' : ({forming:r.plan?.trigger==='pullback'?'Waiting for pullback':'Breakout forming',trigger_confirmed:'Breakout confirmed',overextended:'Too extended',invalidated:'Blocked'}[r.timing] || words(r.timing));
  const eligible = r => r.plan?.valid && ['forming','trigger_confirmed'].includes(r.timing) && ['watch','high_conviction'].includes(r.tier);
  const stale = () => disconnected || state?.stale || !state?.snapshot || Date.now()+clockOffset-state.snapshot.published_ms > (state.interval_seconds || 300)*2000;
  function fail(msg) { el('continuousError').textContent=msg || ''; el('continuousError').hidden=!msg; }
  async function api(path, method='GET') {
    const r=await fetch(`/api/research/continuous${path}`,{method,cache:'no-store',signal:AbortSignal.timeout(40000)});
    const value=await r.json();
    if(!r.ok) throw new Error(value.error || `HTTP ${r.status}`);
    return value;
  }
  function rows() {
    const filter=el('continuousSearch').value.trim().toUpperCase(), tier=el('continuousTier').value, timing=el('continuousTiming').value;
    const list=(state?.snapshot?.rows || []).filter(r => (tier==='all' ? r.tier!=='unscanned' : tier==='watch'?eligible(r):tier==='avoid'?r.tier!=='unscanned'&&!eligible(r):r.tier===tier) && (timing==='all' || r.timing===timing) && `${r.pair} ${r.sector || ''}`.toUpperCase().includes(filter));
    replace('continuousRows',list.map(r=>`<tr>
      <td><strong>${r.rank ? '#'+num(r.rank,0)+' ' : ''}${esc(r.pair)}</strong><small title="${esc(r.rank_change_basis)}">Rank change ${signed(r.rank_change)}</small><small>${esc(r.sector || 'Unclassified')}${r.lagging_flag?' / sector laggard':''}</small><small>CoinDCX: ${r.coindcx_listed===true?'listed (execution unchecked)':r.coindcx_listed===false?'not listed':'unverified'}</small></td>
      <td data-tier="${esc(r.tier)}" data-assessment="${esc(r.timing)}"><strong>${esc(assessmentLabel(r))}</strong><small>${esc(r.plan?.reason || r.reasons?.[0] || '')}</small></td>
      <td><strong>${num(r.public_rank?.score)}</strong><small>Change ${signed(r.score_change)}</small><small>Data coverage ${num(r.public_rank?.coverage_pct,0)}%</small></td>
      <td>${esc(words(r.oi_state || 'unavailable'))}</td>
      <td>${eligible(r)?`${num(r.entry,8)}<small>SL ${num(r.stop_loss,8)}</small><small>${esc(words(r.plan.trigger))} / conditional</small>`:'-<small>No eligible entry</small>'}</td>
      <td>${eligible(r)?[r.tp1,r.tp2,r.tp3].map(v=>num(v,8)).join('<br>'):'-'}</td>
      <td>${eligible(r)?num(r.risk_reward):'-'}</td><td>${r.tier==='unscanned'?'-':num(r.plan?.reference_price ?? r.reference_price,8)}<small>${r.tier==='unscanned'?'No current candle':esc(stamp(r.market_close_ms))}</small></td>
      <td><button type="button" data-plan="${esc(r.symbol)}">Details</button></td></tr>`).join('') || '<tr><td colspan="9">No pairs in this view</td></tr>');
  }
  function details(symbol) {
    selected=symbol;
    const r=state?.snapshot?.rows.find(r=>r.symbol===symbol);
    el('continuousEvidence').hidden=!r;
    if(!r) return;
    const p=r.plan || {}, a=r.analysis || {};
    el('continuousEvidence').innerHTML=`<h3>${esc(r.pair)} / ${esc(r.tier.replaceAll('_',' '))}</h3>
      <p>${esc(r.reasons?.join('; ') || 'All configured research checks passed')}</p>
      <dl><dt>Reference venue / price</dt><dd>Binance USDT / ${num(p.reference_price ?? r.reference_price,8)}</dd>
      <dt>Whale data</dt><dd>${esc(a.whale?.reason || a.whale?.limitation || a.whale?.status || 'Unavailable')}</dd>
      <dt>OI evidence</dt><dd>${esc(a.oi?.basis || a.oi?.reason || 'Unavailable')}<br>4h price ${num(a.oi?.price_change_pct)}%; OI ${num(a.oi?.oi_change_pct)}%; funding ${num(a.oi?.funding_pct,4)}%</dd>
      <dt>Holder data</dt><dd>${esc(a.holder?.reason || a.holder?.basis || 'Unavailable')}</dd>
      <dt>Optional-data confluence</dt><dd>${num(r.composite_score)} / 100; ${num(r.score_coverage_pct,0)}% coverage<br>${esc(Object.entries(r.contributions || {}).map(([k,v])=>`${k}: ${num(v)}`).join('; '))}</dd>
      <dt>Public ranking contributions</dt><dd>${esc(Object.entries(r.public_rank?.contributions || {}).map(([k,v])=>`${k}: ${num(v)}`).join('; '))}<br>${esc(r.public_rank?.basis || '-')}</dd>
      <dt>Public ranking measurements</dt><dd>${esc(Object.entries(r.public_rank?.features || {}).map(([k,v])=>`${words(k)}: ${num(v,4)}`).join('; '))}</dd>
      <dt>Rank comparison</dt><dd>${esc(r.rank_change_basis || '-')} / ${signed(r.rank_change)}</dd>
      <dt>Current assessment</dt><dd>${esc(assessmentLabel(r))}${r.setup?.awaiting_reset?'; historical episode awaiting reset':''}</dd>
      <dt>Calculated levels / eligibility</dt><dd>${[p.entry,p.stop_loss,p.tp1,p.tp2,p.tp3].map(v=>num(v,8)).join(' / ')}<br>${eligible(r)?'Conditional research plan':'Blocked; not an eligible entry'}</dd>
      <dt>Scale-out fractions</dt><dd>${esc(p.rules?.scale_out?.map(v=>num(v*100)+'%').join(' / ') || '-')}</dd>
      <dt>Signal invalidation</dt><dd>${esc(p.signal_invalidation || '-')}</dd>
      <dt>Published / candle close</dt><dd>${esc(stamp(state.snapshot.published_ms))} / ${esc(stamp(r.market_close_ms))}</dd></dl>
      <ul class="levelBasis">${Object.entries(p.basis || {}).map(([k,v])=>`<li><strong>${esc(k)}:</strong> ${esc(v)}</li>`).join('')}</ul>
      <div>${Object.entries(r.evidence || {}).map(([k,v])=>`<button type="button" data-raw="${esc(v)}">${esc(k)} inputs</button>`).join('')}</div>
      <pre id="continuousRaw" hidden></pre>`;
  }
  function renderStatus() {
    if(!state) { if(disconnected) el('continuousStatus').textContent='DISCONNECTED / no verified results'; return; }
    if(lastContact && Date.now()-lastContact>20000) disconnected=true;
    const s=state.snapshot, now=Date.now()+clockOffset, remaining=Math.max(0,Math.ceil((state.refresh_available_ms-now)/1000)) || 0;
    const status=disconnected?'DISCONNECTED':!state.running?'STOPPED':state.busy?'SCANNING':state.queued?'QUEUED':state.error?'SCAN FAILED':'RUNNING / waiting for next scan';
    el('continuousStatus').textContent=`${status}${!disconnected&&state.message?' | '+state.message:''}`;
    el('continuousStart').disabled=commandPending || !!state.running;
    el('continuousStop').disabled=commandPending || !state.running;
    el('continuousRefresh').disabled=commandPending || !!state.busy || !!state.queued || remaining>0;
    el('continuousRefresh').textContent=state.busy?'Scanning...':state.queued?'Scan queued':remaining>0?`Ready in ${remaining}s`:'Scan Now';
    el('continuousExport').disabled=!s;
    const next=!state.running?'Stopped':state.busy?'Scan in progress':state.queued?'Scan queued':state.next_cycle_ms?`${stamp(state.next_cycle_ms)} (${Math.max(0,Math.ceil((state.next_cycle_ms-now)/1000))}s)`:'Not scheduled';
    const interval=s ? (s.config?.market_interval || '15m') : '-';
    replace('continuousFreshness', [['Last completed scan',stamp(s?.published_ms)],['Next research scan',disconnected?'Connection lost':next],['Research scan schedule',`${num((state.interval_seconds || 300)/60,0)} minutes / ${state.market_interval||interval} Binance candles`],['Dashboard connected',stamp(lastContact)]].map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join(''));
    el('continuousCandleHeading').textContent=`${interval} close / time (IST)`;
    el('continuousSnapshotStatus').dataset.stale=String(!!stale());
    el('continuousSnapshotStatus').textContent=!s?'No completed scan yet':`${stale()?'SAVED RESULTS / NOT CURRENT':state.busy||state.queued?'LAST COMPLETED RESULTS / UPDATE PENDING':'LATEST COMPLETED RESULTS'} | ${stamp(s.published_ms)} | Not live prices`;
    el('continuousWalletStatus').textContent=disconnected?'Connection lost / saved wallet status':state.wallet_evidence?.error || (state.wallet_evidence?'Read-only evidence pilot | Wallet predictions disabled':'Wallet evidence status unavailable');
  }
  function render() {
    const s=state.snapshot, c=s?.coverage;
    renderStatus();
    fail(state.error);
    const counts={}; for(const r of s?.rows || []) counts[r.tier]=(counts[r.tier]||0)+1;
    const checked=(s?.rows || []).filter(r=>r.tier!=='unscanned'), waiting=checked.filter(eligible).length;
    el('continuousMetrics').innerHTML=[['Pairs in universe',c?.universe],['Checked this scan',c?.analysed],['Waiting / confirmed',waiting],['Blocked / extended',checked.length-waiting],['Not checked / excluded',counts.unscanned || 0]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${num(v,0)}</strong></div>`).join('');
    el('continuousCoverage').textContent=s?`${c.reference} | ${c.eligible} liquidity-eligible | ${c.failed} fetch failures | Whale: ${s.config.providers.coinglass_enabled?'CoinGlass':'not configured'} | Holders: ${s.config.providers.bubblemaps_enabled?'Bubblemaps':'not configured'} | Public score: heuristic, not a probability. ${s.warnings.join('; ')}`:'No completed cycle';
    const wallets=state.wallet_evidence;
    const chainNames={ethereum:'Ethereum',bsc:'BNB Smart Chain',solana:'Solana'};
    replace('continuousWalletRows',Object.entries(wallets?.chains || {}).map(([name,c])=>`<tr>
      <td><strong>${esc(chainNames[name] || name)}</strong></td>
      <td>${esc(c.enabled&&c.stale&&c.checked_ms?'Stale / collector unconfirmed':words(c.status))}<small>RPC ${c.rpc_configured?'configured':'not configured'}</small><small>${esc(c.reason || '')}</small></td>
      <td>${num(c.wallet_count,0)} / ${num(c.asset_count,0)}</td><td>${num(c.event_count,0)}</td>
      <td>${num(c.finalized_head,0)} / ${num(c.next_height,0)}</td><td>${num(c.backlog_heights,0)}<small>blocks / slots</small></td>
      <td>${esc(stamp(c.checked_ms))}</td><td>${(c.limitations || []).map(esc).join('<br>')}</td></tr>`).join('') || '<tr><td colspan="8">Wallet evidence is not available</td></tr>');
    rows();
    if(selected && displayedSnapshot!==s?.id) details(selected);
    displayedSnapshot=s?.id;
    const replay=state.replay || {};
    el('continuousReplayStatus').textContent=(replay.unit || 'No replay results yet')+(replay.legacy_excluded?` | ${replay.legacy_excluded} legacy scan replays excluded`:'');
    el('continuousReplayRows').innerHTML=(replay.groups || []).map(g=>`<tr><td>${esc(g.config_id)}<small>${esc(g.tier)}</small></td><td>${g.plans}</td><td>${g.closed_entries}</td><td>${g.target_hits.join(' / ')}</td><td>${g.stop_exits}</td><td>${g.score_threshold} / ${g.above_threshold_tp1_hit_rate==null?'-':num(g.above_threshold_tp1_hit_rate*100)+'%'}<small>${g.above_threshold_entries} above-threshold closed entries</small></td><td>${g.pending} / ${g.unfilled} / ${g.data_gaps}</td><td>${num(g.avg_net_return_pct)}% / ${num(g.avg_mfe_pct)}% / ${num(g.avg_mae_pct)}%</td></tr>`).join('') || '<tr><td colspan="8">Awaiting eligible setups and post-publication candles</td></tr>';
    const v=state.validation || {};
    window.researchValidationView?.update(state);
    const current=(v.groups || []).find(g=>g.config_id===s?.config_id);
    const p=current?.progress || {target:30,complete:0,pending:0,data_gaps:0,remaining:30,unique_pairs:0};
    el('continuousSampleProgress').textContent=`${p.complete} / ${p.target} completed qualified observations | ${p.pending} pending | ${p.data_gaps} data gaps | ${p.unique_pairs} distinct pairs | ${p.remaining?'Collecting':'Ready for review, not trading approval'}. Current configuration: ${s?.config_id || 'awaiting scan'}. Thirty observations are a review milestone, not proof of profitability.`;
    el('continuousSampleMeter').value=Math.min(p.complete,p.target);
    const selectedConfig=el('continuousScoreConfig').value || s?.config_id;
    replace('continuousScoreConfig',(v.groups || []).map(g=>`<option value="${esc(g.config_id)}">${esc(g.config_id)}${g.config_id===s?.config_id?' (current)':''}</option>`).join(''));
    if((v.groups || []).some(g=>g.config_id===selectedConfig)) el('continuousScoreConfig').value=selectedConfig;
    renderScores();
    const h=state.health || {}, cat=s?.coindcx_catalogue;
    const healthAge=Date.now()+clockOffset-(h.checked_ms || 0);
    el('continuousHealth').textContent=`Collector: ${healthAge<60000?words(h.status):'supervisor not confirmed'} | CoinDCX: ${cat?.status==='verified'?`${cat.count} pairs verified at ${stamp(cat.verified_ms)}`:'unverified'} | Phone alerts: ${s?.config.alerts.telegram_enabled || s?.config.alerts.discord_enabled?'enabled (delivery status in history)':'not configured'}`;
    el('continuousStorage').textContent=`History: ${num((state.storage?.database_bytes || 0)/1048576)} MiB | Journal: ${num((state.storage?.wal_bytes || 0)/1048576)} MiB | ${state.storage?.encoding || '-'} | All recorded evidence retained`;
    replace('continuousHealthRows',[...(h.events || [])].reverse().map(e=>`<tr><td>${esc(stamp(e.at_ms))}</td><td>${esc(words(e.status))}</td></tr>`).join('') || '<tr><td colspan="2">No supervisor health events recorded</td></tr>');
    el('continuousValidationStatus').textContent=v.unit || 'Collecting prospective samples';
    el('continuousValidationRows').innerHTML=(v.groups || []).map(g=>`<tr><td>${esc(g.config_id)}<small>${esc(g.version)} / ${g.threshold}</small></td><td>${g.candidates.count} / ${g.controls.count} / ${g.all.count}</td><td>${num(g.candidates.mean_net_pct)}%</td><td>${num(g.controls.mean_net_pct)}%</td><td>${num(g.all.mean_net_pct)}%</td><td>${signed(g.candidate_minus_control_pct)} pp</td><td>${num(g.candidates.mean_mfe_pct)}% / ${num(g.candidates.mean_mae_pct)}%</td><td>${g.pending} / ${g.data_gaps}</td></tr>`).join('') || '<tr><td colspan="8">No mature samples yet; a full 4-hour observation window is required</td></tr>';
    history=[...(state.setups?.items || [])].sort((a,b)=>b.published_ms-a.published_ms || a.id.localeCompare(b.id));
    renderHistory();
    const pairs=Object.fromEntries(history.map(s=>[s.id,s.pair]));
    el('continuousEventRows').innerHTML=(state.setups?.events || []).filter(e=>e.type!=='updated').slice(0,50).map(e=>`<tr><td>${esc(stamp(e.at_ms))}</td><td>${esc(pairs[e.setup_id] || e.setup_id)}</td><td>${esc(words(e.type))}</td><td>${num(e.score)}</td></tr>`).join('') || '<tr><td colspan="4">No setup transitions yet</td></tr>';
    el('continuousAlertStatus').textContent=['telegram','discord'].map(c=>`${c}: ${s?.config.alerts[c+'_enabled']?(state.alerts?.[c]?.status || 'enabled'):'off'}`).join(' | ');
  }
  function renderScores() {
    const g=(state?.validation?.groups || []).find(g=>g.config_id===el('continuousScoreConfig').value);
    replace('continuousScoreRows',(g?.[el('continuousScoreGrouping').value] || []).map(b=>`<tr><td>${esc(b.label)}</td><td>${num(b.min_score)} / ${num(b.max_score)}</td><td>${b.count} / ${b.total}</td><td>${signed(b.mean_net_pct)}%</td><td>${signed(b.median_net_pct)}%</td><td>${num(b.positive_rate==null?null:b.positive_rate*100)}%</td><td>${b.pending} / ${b.data_gaps}</td></tr>`).join('') || '<tr><td colspan="7">No samples recorded for this configuration</td></tr>');
  }
  function renderHistory() {
    const query=el('continuousHistorySearch').value.trim().toUpperCase();
    const list=history.filter(s=>s.pair.toUpperCase().includes(query)), pages=Math.max(1,Math.ceil(list.length/25));
    historyPage=Math.min(historyPage,pages-1);
    el('continuousHistoryCount').textContent=`${list.length} distinct setups`;
    el('continuousHistoryPage').textContent=`${historyPage+1} / ${pages}`;
    el('continuousHistoryPrevious').disabled=historyPage===0;
    el('continuousHistoryNext').disabled=historyPage+1>=pages;
    replace('continuousHistoryRows',list.slice(historyPage*25,(historyPage+1)*25).map(s=>{const p=s.plan,r=s.outcome || {};return `<tr><td>${esc(stamp(s.published_ms))}<small>${esc(s.pair)}</small></td><td>${esc(words(r.status || s.stage))}<small>${s.revision_count} observations</small></td><td>${num(p.entry,8)}<small>SL ${num(p.stop_loss,8)}</small></td><td>${[p.tp1,p.tp2,p.tp3].map(v=>num(v,8)).join(' / ')}</td><td>${r.net_return_pct==null?'-':num(r.net_return_pct)+'%'}<small>${r.terminal?'closed':'realized portion only'}</small></td><td>${num(r.mfe_pct)}% / ${num(r.mae_pct)}%</td><td><button type="button" data-setup="${esc(s.id)}">Details</button></td></tr>`;}).join('') || '<tr><td colspan="7">No distinct setups recorded yet</td></tr>');
  }
  function accept(data) { state=data; lastContact=Date.now(); clockOffset=(data.server_time_ms || lastContact)-lastContact; disconnected=false; render(); }
  async function refresh(force=false) {
    if(commandPending) return;
    if(fetching) { if(force===true) { await fetching; return refresh(); } return fetching; }
    const epoch=requestEpoch;
    fetching=(async()=>{
      try { const data=await api(''); if(epoch===requestEpoch) accept(data); } catch(e) { if(epoch===requestEpoch){disconnected=true;fail(e.message);renderStatus();} } finally { fetching=null; }
    })();
    return fetching;
  }
  for(const [id,path] of [['continuousStart','/start'],['continuousStop','/stop'],['continuousRefresh','/refresh']]) {
    el(id).addEventListener('click',async()=>{commandPending=true;requestEpoch++;renderStatus();try{accept(await api(path,'POST'));}catch(e){disconnected=true;fail(e.message);}finally{commandPending=false;renderStatus();}});
  }
  document.querySelectorAll('[data-research-view]').forEach(b=>b.addEventListener('click',()=>{
    const view=b.dataset.researchView;
    document.querySelectorAll('[data-research-view]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));
    document.querySelectorAll('[data-research-pane]').forEach(p=>p.hidden=p.dataset.researchPane!==view);
    if(view==='manual') window.researchView?.refresh(); else refresh();
  }));
  el('continuousSearch').addEventListener('input',rows);
  el('continuousScoreConfig').addEventListener('change',renderScores);
  el('continuousScoreGrouping').addEventListener('change',renderScores);
  el('continuousTier').addEventListener('change',rows);
  el('continuousTiming').addEventListener('change',rows);
  el('continuousHistorySearch').addEventListener('input',()=>{historyPage=0;renderHistory();});
  el('continuousHistoryPrevious').addEventListener('click',()=>{historyPage--;renderHistory();});
  el('continuousHistoryNext').addEventListener('click',()=>{historyPage++;renderHistory();});
  el('continuousHistoryRows').addEventListener('click',async e=>{const b=e.target.closest('[data-setup]');if(!b)return;historyEvidence=b.dataset.setup;try{const data=await api('/history?setup_id='+encodeURIComponent(historyEvidence));el('continuousHistoryEvidence').textContent=JSON.stringify(data,null,2);el('continuousHistoryEvidence').hidden=false;}catch(err){fail(err.message);}});
  el('continuousRows').addEventListener('click',e=>{const b=e.target.closest('[data-plan]');if(b)details(b.dataset.plan);});
  el('continuousEvidence').addEventListener('click',async e=>{const b=e.target.closest('[data-raw]');if(!b)return;try{const data=await api('/evidence?id='+encodeURIComponent(b.dataset.raw));el('continuousRaw').textContent=JSON.stringify(data,null,2);el('continuousRaw').hidden=false;}catch(err){fail(err.message);}});
  function download(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  el('continuousExport').addEventListener('click',()=>{if(state?.snapshot)download(state.snapshot,`research-${state.snapshot.id}.json`);});
  el('continuousHistoryExport').addEventListener('click',async()=>{try{download(await api('/history'),'research-setup-history.json');}catch(e){fail(e.message);}});
  window.continuousResearchView={refresh:()=>refresh(true)};
  el('tabResearch').addEventListener('click',refresh);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && el('researchMain').style.display!=='none')refresh();});
  refresh();
  setInterval(()=>{if(!document.hidden && el('researchMain').style.display!=='none')refresh();},5000);
  setInterval(()=>{if(!document.hidden && el('researchMain').style.display!=='none')renderStatus();},1000);
})();
