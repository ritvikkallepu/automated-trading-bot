(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (v, digits=2) => v == null ? '-' : Number(v).toLocaleString(undefined,{maximumFractionDigits:digits});
  const stamp = ms => ms ? new Date(ms).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' IST' : 'Not available';
  let state = null, selected = null, fetching = null, displayedSnapshot = null;
  let historyPage = 0, history = [], historyEvidence = null, historyEvidenceVersion = null;
  let lastContact = null, clockOffset = 0, disconnected = false, commandPending = false, requestEpoch = 0;
  const signed = v => v == null ? '-' : `${v>0?'+':''}${num(v)}`;
  const words = s => String(s ?? '-').replaceAll('_',' ');
  const lastMarkup = new Map();
  const replace = (id, html) => { if(lastMarkup.get(id)!==html) { el(id).innerHTML=html; lastMarkup.set(id,html); } };
  const assessmentLabel = r => r.tier==='unscanned' ? 'Not checked this cycle' : ({forming:r.plan?.trigger==='pullback'?'Waiting for pullback':'Breakout forming',trigger_confirmed:'Breakout confirmed',overextended:'Too extended',invalidated:'Blocked'}[r.timing] || words(r.timing));
  const eligible = r => r.plan?.valid && ['forming','trigger_confirmed'].includes(r.timing) && ['watch','high_conviction'].includes(r.tier);
  const threshold = () => state?.snapshot?.config?.ranking?.candidate_score ?? 65;
  const qualified = r => eligible(r) && Number(r.public_rank?.score) >= threshold();
  const move = r => r.public_rank?.features?.return_pct;
  const moveText = v => v == null ? '-' : `${signed(v)}%`;
  const pairName = r => `<span class="researchPair"><strong>${esc(r.pair)}</strong><small>${r.rank ? '#'+num(r.rank,0)+' in research / ' : ''}${esc(r.sector || 'Unclassified')}</small><small>CoinDCX: ${r.coindcx_listed===true?'listed, execution unchecked':r.coindcx_listed===false?'not listed':'unverified'}</small></span>`;
  const walletLabel = c => !c.enabled ? 'Off / not configured' : c.stale ? 'No recent check' : ({limited:'Limited evidence',unavailable:'Data unavailable',not_started:'Waiting for first check'}[c.status] || words(c.status));
  const setupLabel = s => {
    const status=s.outcome?.status || s.stage;
    return ({forming:'Waiting for entry conditions',trigger_confirmed:'Trigger seen in research',pending:'Awaiting replay outcome',open:'Simulated entry open',all_targets:'All targets reached in replay',stop_loss:'Stop hit in replay',trailing_stop:'Trailing stop hit in replay',timeout:'Replay ended at time limit',not_triggered:'No simulated entry',invalidated:'Idea invalidated',gap_invalidated:'Gap invalidated entry',gap_chase_rejected:'Chasing entry rejected',fill_geometry_rejected:'Entry risk check failed',data_gap:'Price data missing',completed:'Replay complete'}[status] || words(status));
  };
  const setupVersion = s => s ? JSON.stringify([s.revision_count,s.stage,s.outcome?.status,s.outcome?.terminal,s.outcome?.entry_price,s.outcome?.net_return_pct]) : null;
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
    const list=(state?.snapshot?.rows || []).filter(r => (tier==='all' ? r.tier!=='unscanned' : tier==='qualified'?qualified(r) : tier==='watch'?eligible(r):tier==='avoid'?r.tier!=='unscanned'&&!eligible(r):r.tier===tier) && (timing==='all' || r.timing===timing) && `${r.pair} ${r.sector || ''}`.toUpperCase().includes(filter));
    replace('continuousRows',list.map(r=>`<tr>
      <td>${pairName(r)}</td>
      <td class="researchMove" data-direction="${Number(move(r))>0?'up':Number(move(r))<0?'down':'flat'}"><strong>${moveText(move(r))}</strong><small>${esc(state?.snapshot?.config?.market_interval || '5m')} closed candle</small><small>1h ${moveText(r.public_rank?.features?.hour_change_pct)} / vol ${num(r.public_rank?.features?.volume_ratio)}x</small></td>
      <td data-tier="${esc(r.tier)}" data-assessment="${esc(r.timing)}"><strong>${qualified(r)?'Qualified / '+esc(assessmentLabel(r)):esc(assessmentLabel(r))}</strong><small>${esc(r.plan?.reason || r.reasons?.[0] || '')}</small></td>
      <td><strong>${num(r.public_rank?.score)} / 100</strong><small>${eligible(r)?`Minimum ${num(threshold())}`:'No valid plan'}</small><small>Coverage ${num(r.public_rank?.coverage_pct,0)}%</small></td>
      <td>${esc(words(r.oi_state || 'unavailable'))}</td>
      <td>${eligible(r)?`Entry ${num(r.entry,8)}<small>Stop ${num(r.stop_loss,8)} / TP1 ${num(r.tp1,8)}</small><small>${num(r.risk_reward)} R:R / conditional</small>`:'-<small>No eligible entry</small>'}</td>
      <td>${r.tier==='unscanned'?'-':num(r.plan?.reference_price ?? r.reference_price,8)}<small>${r.tier==='unscanned'?'No current candle':esc(stamp(r.market_close_ms))}</small></td>
      <td><button type="button" data-plan="${esc(r.symbol)}">Details</button></td></tr>`).join('') || `<tr><td colspan="8">${tier==='qualified'?'No qualified research ideas in this scan. Check All checked pairs for movement and reasons.':'No pairs match these filters.'}</td></tr>`);
  }
  function highlights(rows) {
    if(!state?.snapshot) {
      replace('continuousHighlights','<div class="researchHighlightHead"><h3>Where to look</h3><p>Run a research scan to see qualified ideas and closed-candle movement.</p></div>');
      return;
    }
    const checked=rows.filter(r=>r.tier!=='unscanned');
    const ideas=checked.filter(qualified).sort((a,b)=>Number(b.coindcx_listed===true)-Number(a.coindcx_listed===true) || (b.public_rank?.score || 0)-(a.public_rank?.score || 0)).slice(0,3);
    const movers=checked.filter(r=>move(r)!=null && Number.isFinite(Number(move(r)))).sort((a,b)=>Math.abs(move(b))-Math.abs(move(a))).slice(0,4);
    const interval=esc(state?.snapshot?.config?.market_interval || '5m');
    replace('continuousHighlights',`<div class="researchHighlightHead"><h3>Where to look</h3><p>${stale()?'Saved scan is not current. Refresh before making decisions. ':'Research only. '}A qualified idea meets this scan's score and plan checks; it is not a buy signal or a bot trade.</p></div>
      <div class="researchHighlightColumns"><section aria-label="Qualified research ideas"><h4>Long ideas to review <span>${checked.filter(qualified).length}</span></h4><p>Score at least ${num(threshold())} / 100 and a valid conditional plan.</p>
      ${ideas.length?ideas.map(r=>`<div class="researchHighlightRow">${pairName(r)}<span>${num(r.public_rank?.score)} / 100<small>${esc(assessmentLabel(r))}</small></span><button type="button" data-plan="${esc(r.symbol)}">Details</button></div>`).join(''):'<p class="researchHighlightEmpty">None in the last completed scan. Do not treat a fast mover as an entry.</p>'}</section>
      <section aria-label="Largest recent price moves"><h4>Largest ${interval} moves among checked pairs</h4><p>Absolute price change on the last closed candle, not a prediction or entry.</p>
      ${movers.length?movers.map(r=>`<div class="researchHighlightRow">${pairName(r)}<span class="researchMove" data-direction="${Number(move(r))>0?'up':Number(move(r))<0?'down':'flat'}">${moveText(move(r))}<small>${esc(assessmentLabel(r))}</small></span><button type="button" data-plan="${esc(r.symbol)}">Details</button></div>`).join(''):'<p class="researchHighlightEmpty">No closed-candle movement data yet. Run a new scan.</p>'}</section></div>`);
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
      <dt>Calculated levels / eligibility</dt><dd>${[p.entry,p.stop_loss,p.tp1,p.tp2,p.tp3].map(v=>num(v,8)).join(' / ')}<br>${qualified(r)?'Qualified conditional research idea; not a trade approval':eligible(r)?`Valid conditional plan, but public score is below ${num(threshold())}`:'No eligible entry'}</dd>
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
    el('continuousCandleHeading').textContent=`${interval} candle close (IST)`;
    el('continuousSnapshotStatus').dataset.stale=String(!!stale());
    el('continuousSnapshotStatus').textContent=!s?'No completed scan yet':`${stale()?'SAVED RESULTS / NOT CURRENT':state.busy||state.queued?'LAST COMPLETED RESULTS / UPDATE PENDING':'LATEST COMPLETED RESULTS'} | ${stamp(s.published_ms)} | Not live prices`;
    const walletChains=Object.values(state.wallet_evidence?.chains || {}), enabled=walletChains.filter(c=>c.enabled);
    el('continuousWalletStatus').textContent=disconnected?'Connection lost. Showing saved wallet status.':state.wallet_evidence?.error || (!walletChains.length?'Wallet status unavailable.':!enabled.length?'Tracking is off on all networks. No wallet evidence is being collected.':enabled.every(c=>c.stale)?'Tracking is configured, but no recent wallet check is confirmed.':'Limited wallet transfer evidence is available. It is not a trading signal.');
  }
  function render() {
    const s=state.snapshot, c=s?.coverage;
    renderStatus();
    fail(state.error);
    const counts={}; for(const r of s?.rows || []) counts[r.tier]=(counts[r.tier]||0)+1;
    const checked=(s?.rows || []).filter(r=>r.tier!=='unscanned'), waiting=checked.filter(eligible).length;
    el('continuousMetrics').innerHTML=[['Pairs in universe',c?.universe],['Checked this scan',c?.analysed],['Qualified ideas',checked.filter(qualified).length],['Valid conditional plans',waiting],['Not checked / excluded',counts.unscanned || 0]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${num(v,0)}</strong></div>`).join('');
    highlights(s?.rows || []);
    el('continuousCoverage').textContent=s?`${c.reference} | ${c.eligible} liquidity-eligible | ${c.failed} fetch failures | Whale: ${s.config.providers.coinglass_enabled?'CoinGlass':'not configured'} | Holders: ${s.config.providers.bubblemaps_enabled?'Bubblemaps':'not configured'} | Public score: heuristic, not a probability. ${s.warnings.join('; ')}`:'No completed cycle';
    const wallets=state.wallet_evidence;
    const chainNames={ethereum:'Ethereum',bsc:'BNB Smart Chain',solana:'Solana'};
    const chainRows=Object.entries(wallets?.chains || {});
    el('continuousWalletSummary').innerHTML=[['Networks enabled',chainRows.filter(([,c])=>c.enabled).length],['Wallets watched',chainRows.reduce((n,[,c])=>n+(c.wallet_count || 0),0)],['Transfers recorded',chainRows.reduce((n,[,c])=>n+(c.event_count || 0),0)]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${num(v,0)}</strong></div>`).join('');
    replace('continuousWalletRows',chainRows.map(([name,c])=>`<tr>
      <td data-label="Network"><strong>${esc(chainNames[name] || name)}</strong></td>
      <td data-label="Tracking"><strong>${esc(walletLabel(c))}</strong><small>${esc(c.reason || '')}</small></td>
      <td data-label="Wallets watched">${num(c.wallet_count,0)}</td><td data-label="Transfers recorded">${num(c.event_count,0)}${c.stale&&c.event_count?'<small>Historical; current collection unconfirmed</small>':''}</td>
      <td data-label="Last check">${c.checked_ms?esc(stamp(c.checked_ms)):'Never checked'}</td></tr>`).join('') || '<tr><td colspan="5">No wallet networks available</td></tr>');
    replace('continuousWalletTechnicalRows',chainRows.map(([name,c])=>`<tr><td>${esc(chainNames[name] || name)}<small>RPC ${c.rpc_configured?'configured':'not configured'}</small></td><td>${num(c.finalized_head,0)} / ${num(c.next_height,0)}</td><td>${num(c.backlog_heights,0)}</td><td>${(c.limitations || []).map(esc).join('<br>')}</td></tr>`).join('') || '<tr><td colspan="4">No collection details available</td></tr>');
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
    if(historyEvidence && setupVersion(history.find(item=>item.id===historyEvidence))!==historyEvidenceVersion){
      historyEvidence=null;historyEvidenceVersion=null;el('continuousHistoryEvidence').hidden=true;
    }
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
    const entered=history.filter(s=>s.outcome?.entry_price != null);
    el('continuousHistorySummary').innerHTML=[['Ideas recorded',history.length],['Simulated entries',entered.length],['Closed simulations',entered.filter(s=>s.outcome?.terminal).length],['Unresolved / data gaps',history.filter(s=>!s.outcome?.terminal).length]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${num(v,0)}</strong></div>`).join('');
    historyPage=Math.min(historyPage,pages-1);
    el('continuousHistoryCount').textContent=`${list.length} distinct setups`;
    el('continuousHistoryPage').textContent=`${historyPage+1} / ${pages}`;
    el('continuousHistoryPrevious').disabled=historyPage===0;
    el('continuousHistoryNext').disabled=historyPage+1>=pages;
    replace('continuousHistoryRows',list.slice(historyPage*25,(historyPage+1)*25).map(s=>{const p=s.plan || {},r=s.outcome || {},filled=r.entry_price!=null;return `<tr><td data-label="Pair / first seen"><strong>${esc(s.pair)}</strong><small>${esc(stamp(s.published_ms))}</small></td><td data-label="What happened"><strong>${esc(setupLabel(s))}</strong><small>${s.revision_count} scan observations of this idea</small></td><td data-label="Original plan">Entry ${num(p.entry,8)}<small>Stop ${num(p.stop_loss,8)} / first target ${num(p.tp1,8)}</small></td><td data-label="Simulation">${filled&&r.net_return_pct!=null?`${signed(r.net_return_pct)}%`:r.terminal?'No simulated entry':'No simulated entry yet'}<small>${filled?r.terminal?'Closed replay':'Partial result so far':r.terminal?'Idea ended without a fill':'Awaiting replay outcome'}</small></td><td data-label="Evidence"><button type="button" data-setup="${esc(s.id)}">Details</button></td></tr>`;}).join('') || '<tr><td colspan="5">No research ideas match this pair</td></tr>');
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
    if(view!=='history') { historyEvidence=null; historyEvidenceVersion=null; el('continuousHistoryEvidence').hidden=true; }
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
  el('continuousHistoryRows').addEventListener('click',async e=>{const b=e.target.closest('[data-setup]');if(!b)return;const id=b.dataset.setup;historyEvidence=id;historyEvidenceVersion=setupVersion(history.find(item=>item.id===id));try{const data=await api('/history?setup_id='+encodeURIComponent(id)),s=data.setup,p=s?.plan || {},r=s?.outcome || {};if(historyEvidence!==id || el('researchHistoryPane').hidden)return;if(!s)throw new Error('Idea not found');el('continuousHistoryEvidence').innerHTML=`<h3>${esc(s.pair)} / original research idea</h3><p>${esc(setupLabel(s))}. This is a simulated plan, not a CoinDCX trade.</p><dl><dt>First seen</dt><dd>${esc(stamp(s.published_ms))}</dd><dt>Original entry / stop</dt><dd>${num(p.entry,8)} / ${num(p.stop_loss,8)}</dd><dt>Targets 1 / 2 / 3</dt><dd>${[p.tp1,p.tp2,p.tp3].map(v=>num(v,8)).join(' / ')}</dd><dt>Replay fill</dt><dd>${r.entry_price==null?'No simulated fill':num(r.entry_price,8)}</dd><dt>Replay result</dt><dd>${r.net_return_pct==null?'Not available':signed(r.net_return_pct)+'%'}${r.terminal?' / closed replay':' / incomplete'}</dd><dt>Largest rise / fall after fill</dt><dd>${r.mfe_pct==null?'-':signed(r.mfe_pct)+'%'} / ${r.mae_pct==null?'-':signed(r.mae_pct)+'%'}</dd><dt>Changes recorded</dt><dd>${(data.events || []).length}</dd></dl><details class="researchSettings"><summary>Raw setup and change events</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>`;el('continuousHistoryEvidence').hidden=false;el('continuousHistoryEvidence').scrollIntoView({behavior:'smooth',block:'start'});}catch(err){if(historyEvidence===id)fail(err.message);}});
  el('continuousRows').addEventListener('click',e=>{const b=e.target.closest('[data-plan]');if(b)details(b.dataset.plan);});
  el('continuousHighlights').addEventListener('click',e=>{const b=e.target.closest('[data-plan]');if(b){details(b.dataset.plan);el('continuousEvidence').scrollIntoView({behavior:'smooth',block:'start'});}});
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
