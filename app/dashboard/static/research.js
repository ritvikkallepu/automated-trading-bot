(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const fmt = (value, digits = 2) => value == null ? 'Unavailable' : Number(value).toLocaleString(undefined, {maximumFractionDigits:digits});
  const stamp = ms => new Date(ms).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})+' IST';
  let snapshot = null, bucket = 'all', selected = null, busy = false;
  const fields = {
    max_pairs:['Pairs per scan',1,50,1], min_quote_volume:['Min Binance 24h volume (USDT)',0,1e12,1000000],
    min_volume_ratio:['Min closed 5m volume ratio',0,10,0.1], min_score:['Min research score',0,100,1],
    max_extension_atr:['Max extension (ATR)',0.1,20,0.1], max_spread_bps:['Max CoinDCX spread (bps)',0.1,1000,0.1],
    max_divergence_bps:['Max venue divergence (bps)',0.1,1000,0.1], min_depth_usdt:['Min visible depth (USDT)',0,1e9,100],
    funding_warning_pct:['Funding penalty threshold (%)',0.001,10,0.001],
    round_trip_cost_pct:['Assumed round-trip cost (%)',0,5,0.01]
  };
  async function api(path, options) {
    const response = await fetch(path, {...options, signal:AbortSignal.timeout(15000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  function error(message) {
    el('researchError').textContent = message || '';
    el('researchError').hidden = !message;
  }
  function renderRows() {
    const filter = el('researchPairFilter').value.trim().toUpperCase();
    const rows = (snapshot?.rows || []).filter(r => (bucket === 'all' || r.bucket === bucket) && r.pair.includes(filter));
    el('researchRows').innerHTML = rows.map(r => `<tr>
      <td><strong>${escape(r.pair)}</strong><canvas width="260" height="64" data-chart="${escape(r.pair)}" aria-label="Last 40 closed 5-minute prices" role="img"></canvas></td>
      <td class="research-${escape(r.bucket)}">${escape(r.bucket.toUpperCase())}<small>${escape(r.reasons[0] || 'Higher-timeframe trend aligned')}</small></td>
      <td>${fmt(r.score)}<small>Heuristic, not probability</small></td>
      <td>${['5m','15m','1h','4h'].map(tf => ({1:'UP', '-1':'DOWN',0:'FLAT'}[r.features?.[tf]?.trend] || '-')).join(' / ')}</td>
      <td>${fmt(r.features?.['5m']?.volume_ratio)}</td>
      <td class="execution-${escape(r.execution_check?.status || 'unverified')}">${escape((r.execution_check?.status || 'unverified').toUpperCase())}<small>${escape(r.execution_check?.reasons?.[0] || (r.execution_check?.coindcx ? `${fmt(r.execution_check.coindcx.spread_bps)} bps spread` : 'Not trade-ready'))}</small></td>
      <td><button type="button" data-evidence="${escape(r.pair)}">Details</button></td></tr>`).join('') || '<tr><td colspan="7">No pairs match this view</td></tr>';
    rows.forEach(r => {
      const canvas = [...el('researchRows').querySelectorAll('canvas')].find(c => c.dataset.chart === r.pair);
      const values = r.features?.['5m']?.closes;
      if (!canvas || !values?.length) return;
      const ctx = canvas.getContext('2d'), low = Math.min(...values), high = Math.max(...values), range = high - low || 1;
      ctx.strokeStyle = r.bucket === 'long' ? '#40c98a' : r.bucket === 'short' ? '#f06d6d' : '#e6b450';
      ctx.lineWidth = 2; ctx.beginPath();
      values.forEach((p,i) => { const x = 3+i/(values.length-1)*254, y = 58-(p-low)/range*52; i ? ctx.lineTo(x,y) : ctx.moveTo(x,y); });
      ctx.stroke();
    });
  }
  function evidence(pair) {
    const r = snapshot?.rows.find(r => r.pair === pair);
    selected = r?.pair || null;
    el('researchEvidence').hidden = !r;
    if (!r) return;
    const dcx = r.execution_check?.coindcx;
    const executionStatus = r.execution_check?.status || 'unverified';
    el('researchEvidence').innerHTML = `<h3>${escape(r.pair)} / ${escape(r.bucket.toUpperCase())}</h3>
      <p>${escape(r.reasons.join('; ') || 'Meets this snapshot\'s research filters. This is not a trade approval.')}</p>
      <dl>
        <dt>Source cut-off</dt><dd>${escape(stamp(snapshot.as_of_ms))}</dd>
        <dt>Long / short scores</dt><dd>${fmt(r.scores?.long)} / ${fmt(r.scores?.short)}</dd>
        <dt>5m / 15m extension</dt><dd>${fmt(r.features?.['5m']?.extension_atr)} / ${fmt(r.features?.['15m']?.extension_atr)} ATR (signed)</dd>
        <dt>Last funding rate</dt><dd>${fmt(r.funding_pct,4)}% | interval: ${r.funding_interval_hours == null ? 'not reported' : fmt(r.funding_interval_hours)+'h'}</dd>
        <dt>Hourly OI change</dt><dd>${fmt(r.oi_change_pct)}% | ${escape(r.oi_context || 'Unavailable')}</dd>
        <dt>CoinDCX execution check</dt><dd class="execution-${escape(executionStatus)}">${escape(executionStatus.toUpperCase())}: ${escape(r.execution_check?.reasons?.join('; ') || 'Public listing, spread, depth and timestamp checks passed for this snapshot')}</dd>
        <dt>Venue divergence</dt><dd>${fmt(r.execution_check?.divergence_bps)} bps</dd>
        <dt>CoinDCX bid / ask depth</dt><dd>${fmt(dcx?.bid_depth_usdt)} / ${fmt(dcx?.ask_depth_usdt)} USDT within 10 bps (visible book only)</dd>
        <dt>CoinDCX book time</dt><dd>${dcx ? escape(stamp(dcx.timestamp_ms)) : 'Unavailable'}</dd>
      </dl><p>Score components (${escape(r.direction || 'none')}):</p>
      <ul>${Object.entries(r.breakdown?.[r.direction] || {}).map(([k,v])=>`<li>${escape(k.replaceAll('_',' '))}: ${fmt(v)}</li>`).join('') || '<li>Insufficient data to score</li>'}</ul>
      <ul>${(r.warnings || []).map(w=>`<li>${escape(w)}</li>`).join('')}</ul>
      <p><a href="https://www.binance.com/en/futures/${encodeURIComponent(r.symbol)}" target="_blank" rel="noopener noreferrer">Binance contract</a> | <a href="https://docs.coindcx.com/" target="_blank" rel="noopener noreferrer">CoinDCX data reference</a></p>`;
  }
  function render(data) {
    const stale = data.stale || (data.snapshot && Date.now() - data.snapshot.as_of_ms > 300000);
    el('researchStatus').textContent = `${data.status.toUpperCase()} | Manual scan / not scheduled | ${data.progress}${stale ? ' | STALE SNAPSHOT' : ''}${data.status === 'running' && data.snapshot ? ' | Previous completed scan shown' : ''}${data.status !== 'running' && data.retry_after_seconds > 0 ? ` | Available again in ${data.retry_after_seconds}s` : ''}`;
    el('researchScan').disabled = data.status === 'running' || data.retry_after_seconds > 0;
    el('researchCancel').disabled = data.status !== 'running';
    error(data.error ? `${data.error}. Any displayed results are from the previous completed scan.` : (data.snapshot?.warnings || []).join('; '));
    if (!el('researchSettingsForm').children.length && data.defaults) {
      el('researchSettingsForm').innerHTML = Object.entries(fields).map(([key,[name,min,max,step]]) => `<label>${name}<input name="${key}" type="number" required min="${min}" max="${max}" step="${step}" value="${data.defaults[key]}" /></label>`).join('');
    }
    if (!data.snapshot) return;
    const changed = snapshot?.scan_id !== data.snapshot.scan_id;
    snapshot = data.snapshot;
    el('researchDownload').disabled = false;
    for (const [key,name] of [['long','Long'],['short','Short'],['avoid','Avoid']]) el(`research${name}Count`).textContent = snapshot.rows.filter(r => r.bucket === key).length;
    el('researchVerifiedCount').textContent = snapshot.rows.filter(r => r.execution_check?.status === 'verified').length;
    el('researchCoverage').textContent = `${snapshot.scanned_count} / ${snapshot.matched_count}`;
    el('researchMeta').textContent = `${stale ? 'STALE | ' : ''}Cut-off: ${stamp(snapshot.as_of_ms)} | Completed: ${stamp(snapshot.completed_ms)} | Heuristic v1 | AI not enabled | No trading permissions`;
    if (changed) {
      renderRows(); evidence(selected);
      el('researchExcludedTitle').textContent = `Not scanned (${snapshot.excluded.length})`;
      el('researchExcluded').innerHTML = snapshot.excluded.map(r=>`<li>${escape(r.pair)}: ${escape(r.reason)}</li>`).join('');
    }
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try { render(await api('/api/research')); }
    catch (exc) { error(`Research status unavailable: ${exc.message}. Displayed data is not verified current.`); }
    finally { busy = false; }
  }
  function renderOutcomeMetric(name, data) {
    const candidate = data?.candidates || {};
    const baseline = data?.all_scored_baseline || {};
    return `<article class="researchOutcomeMetric"><h4>${escape(name)}</h4><dl>
      <dt>Candidate observations</dt><dd>${fmt(candidate.observations ?? 0,0)}</dd>
      <dt>Hit rate after costs</dt><dd>${candidate.hit_rate_pct == null ? 'Insufficient data' : fmt(candidate.hit_rate_pct)+'%'}</dd>
      <dt>Average net return</dt><dd>${candidate.avg_net_return_pct == null ? '-' : fmt(candidate.avg_net_return_pct)+'%'}</dd>
      <dt>Median net return</dt><dd>${candidate.median_net_return_pct == null ? '-' : fmt(candidate.median_net_return_pct)+'%'}</dd>
      <dt>Average MFE / MAE</dt><dd>${candidate.avg_mfe_pct == null ? '-' : `${fmt(candidate.avg_mfe_pct)}% / ${fmt(candidate.avg_mae_pct)}%`}</dd>
      <dt>All-scored baseline</dt><dd>${baseline.avg_net_return_pct == null ? '-' : fmt(baseline.avg_net_return_pct)+'%'}</dd>
    </dl></article>`;
  }
  async function refreshOutcomes() {
    try {
      const data = await api('/api/research/outcomes');
      const summary = data.summary || {};
      el('researchOutcomeStatus').textContent = `${String(data.status || 'idle').toUpperCase()} | ${data.progress || 'Waiting'} | ${fmt(summary.candidate_signals || 0,0)} / 200 frozen candidate signals collected${summary.sample_status === 'review_ready' ? ' | Review-ready sample' : ' | Do not tune yet'}`;
      el('researchOutcomeError').textContent = data.error || '';
      el('researchOutcomeError').hidden = !data.error;
      const by = summary.by_horizon || {};
      el('researchOutcomeMetrics').innerHTML = [['15 minutes',by['15m']],['1 hour',by['1h']],['4 hours',by['4h']]].map(([name,value])=>renderOutcomeMetric(name,value)).join('');
      el('researchOutcomeRows').innerHTML = (summary.latest || []).map(row=>`<tr>
        <td>${escape(stamp(row.recommendation_time_ms))}</td><td>${escape(stamp(row.exit_time_ms))}</td><td>${escape(row.pair)}</td>
        <td class="research-${escape(row.direction)}">${escape(row.direction.toUpperCase())}</td><td>${escape(row.horizon)}</td>
        <td>${fmt(row.net_directional_return_pct)}%</td><td>${fmt(row.maximum_favourable_excursion_pct)}%</td>
        <td>${fmt(row.maximum_adverse_excursion_pct)}%</td><td class="${row.hit_after_costs ? 'outcome-hit' : 'outcome-miss'}">${row.hit_after_costs ? 'HIT' : 'MISS'}</td>
      </tr>`).join('') || '<tr><td colspan="9">No mature outcomes yet</td></tr>';
    } catch (exc) {
      el('researchOutcomeError').textContent = `Outcome status unavailable: ${exc.message}`;
      el('researchOutcomeError').hidden = false;
    }
  }
  el('researchScan').addEventListener('click', async () => {
    if (!el('researchSettingsForm').reportValidity()) return;
    el('researchScan').disabled = true;
    try {
      const settings = Object.fromEntries([...new FormData(el('researchSettingsForm'))].map(([k,v])=>[k,Number(v)]));
      render(await api('/api/research', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)}));
    } catch (exc) { error(exc.message); el('researchScan').disabled = false; }
  });
  el('researchCancel').addEventListener('click', async () => {
    try { await api('/api/research/cancel',{method:'POST'}); await refresh(); } catch (exc) { error(exc.message); }
  });
  document.querySelectorAll('[data-bucket]').forEach(button => button.addEventListener('click', () => {
    bucket = button.dataset.bucket;
    document.querySelectorAll('[data-bucket]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
    renderRows();
  }));
  el('researchRows').addEventListener('click', event => {
    const button = event.target.closest('[data-evidence]');
    if (button) evidence(button.dataset.evidence);
  });
  el('researchPairFilter').addEventListener('input', renderRows);
  el('researchSettingsForm').addEventListener('submit', event => event.preventDefault());
  el('researchDownload').addEventListener('click', () => {
    if (!snapshot) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot,null,2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = `research-${snapshot.scan_id}.json`; link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  el('researchOutcomeRefresh').addEventListener('click', async () => {
    try { await api('/api/research/outcomes/refresh',{method:'POST'}); await refreshOutcomes(); }
    catch (exc) { el('researchOutcomeError').textContent = exc.message; el('researchOutcomeError').hidden = false; }
  });
  window.researchView = {refresh: () => Promise.all([refresh(), refreshOutcomes()])};
  if (window.location.hash === '#research') el('tabResearch').click();
  setInterval(() => { if (!el('researchManualPane').hidden && el('researchMain').style.display !== 'none' && !document.hidden) refresh(); },3000);
  setInterval(() => { if (!el('researchManualPane').hidden && el('researchMain').style.display !== 'none' && !document.hidden) refreshOutcomes(); },15000);
})();
