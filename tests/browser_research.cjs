// Dashboard end-to-end tests use explicitly synthetic research data, never live trading.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const python = process.env.RESEARCH_TEST_PYTHON || 'python';
  const server = spawn(python, ['-m', 'tests.research_preview', '--fixture'], {windowsHide:true});
  let browser;
  const output = path.resolve('.test-output/research');
  fs.mkdirSync(output, {recursive:true});
  try {
    const url = await new Promise((resolve,reject) => {
      const timeout = setTimeout(()=>reject(new Error('Preview startup timeout')),15000);
      let stdout = '';
      server.stdout.on('data', chunk => {
        stdout += chunk;
        const match = stdout.match(/PREVIEW_URL=(http:\/\/[^\s]+)/);
        if (match) { clearTimeout(timeout); resolve(match[1]); }
      });
      server.once('error',reject);
      server.once('exit', code => { clearTimeout(timeout); reject(new Error(`Preview exited ${code}`)); });
    });
    browser = await chromium.launch({channel:'msedge',headless:true});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', e=>{errors.push(e.message);console.error('Browser error:',e.message);});
    const outcomeFixture = {
      status:'complete', progress:'Evaluated 1 due snapshot(s)', error:null,
      summary:{candidate_signals:2, sample_status:'collecting', by_horizon:{
        '15m':{
          candidates:{observations:2,hit_rate_pct:50,avg_net_return_pct:0.2,median_net_return_pct:0.2,avg_mfe_pct:1.1,avg_mae_pct:-0.6},
          all_scored_baseline:{observations:3,avg_net_return_pct:0.05}
        }
      }, latest:[{
        pair:'B-BTC_USDT', direction:'long', horizon:'15m', recommendation_time_ms:Date.now()-900000,
        exit_time_ms:Date.now(),
        net_directional_return_pct:0.5, maximum_favourable_excursion_pct:1.2,
        maximum_adverse_excursion_pct:-0.4, hit_after_costs:true
      }]}
    };
    await page.route('**/api/research/outcomes', route => {
      if (route.request().method() === 'GET') return route.fulfill({json:outcomeFixture});
      return route.continue();
    });
    await page.goto(url+'/#research');
    assert(await page.locator('#researchMain').isVisible());
    try { await page.waitForFunction(()=>document.querySelectorAll('#continuousRows [data-plan]').length===2); }
    catch(e) { console.error(await page.locator('#continuousError').textContent()); throw e; }
    assert.match(await page.locator('#continuousStatus').textContent(),/STOPPED/);
    assert.match(await page.locator('#continuousCoverage').textContent(),/not configured/);
    await page.locator('#continuousRows [data-plan]').first().click();
    assert(await page.locator('#continuousEvidence').isVisible());
    await page.locator('#continuousEvidence [data-raw]').first().click();
    await page.locator('#continuousRaw').waitFor({state:'visible'});
    assert.match(await page.locator('#continuousRaw').textContent(),/fetched_ms/);
    await page.locator('#continuousTier').selectOption('unscanned');
    assert.match(await page.locator('#continuousRows').textContent(),/UNKNOWN/);
    await page.locator('#continuousTier').selectOption('all');
    await page.locator('#continuousSearch').fill('BTC');
    assert.equal(await page.locator('#continuousRows [data-plan]').count(),1);
    await page.locator('#continuousSearch').fill('');
    assert(!/Confluence/.test(await page.locator('#continuousRows').textContent()));
    assert.match(await page.locator('#continuousFreshness').textContent(),/IST/);
    assert.match(await page.locator('#continuousSnapshotStatus').textContent(),/SAVED RESULTS/);
    assert.match(await page.locator('#continuousHighlights').textContent(),/Where to look/);
    assert.match(await page.locator('#continuousHighlights').textContent(),/not a buy signal/);
    await page.locator('#continuousTier').selectOption('qualified');
    assert.match(await page.locator('#continuousRows').textContent(),/No qualified research ideas|Qualified/);
    await page.locator('#continuousTier').selectOption('all');
    assert(!(await page.locator('#researchScan').isVisible()));
    await page.locator('#continuousTiming').selectOption('forming');
    assert.equal(await page.locator('#continuousRows [data-plan]').count(),1);
    await page.locator('#continuousTiming').selectOption('invalidated');
    assert.equal(await page.locator('#continuousRows [data-plan]').count(),1);
    assert.match(await page.locator('#continuousRows').textContent(),/No eligible entry/);
    assert.match(await page.locator('#continuousRows td').nth(5).textContent(),/No eligible entry/);
    await page.locator('#continuousTiming').selectOption('all');
    await page.locator('[data-research-view=wallets]').click();
    assert(await page.locator('#researchWalletPane').isVisible());
    assert.equal(await page.locator('#continuousWalletRows tr').count(),3);
    assert.match(await page.locator('#continuousWalletRows').textContent(),/Ethereum/);
    assert.match(await page.locator('#continuousWalletRows').textContent(),/BNB Smart Chain/);
    assert.match(await page.locator('#continuousWalletRows').textContent(),/Solana/);
    assert.match(await page.locator('#continuousWalletRows').textContent(),/not configured/);
    assert.match(await page.locator('#continuousWalletStatus').textContent(),/Tracking is off/);
    assert.match(await page.locator('#continuousWalletSummary').textContent(),/Wallets watched/);
    assert.equal(await page.locator('#continuousWalletTechnicalRows tr').count(),3);
    await page.screenshot({path:path.join(output,'wallets-desktop.png'),fullPage:true});
    await page.locator('[data-research-view=history]').click();
    assert.match(await page.locator('#continuousHistoryCount').textContent(),/1 distinct setups/);
    assert.match(await page.locator('#continuousHistorySummary').textContent(),/Simulated entries/);
    assert.match(await page.locator('#researchHistoryPane').textContent(),/not your portfolio PnL/);
    await page.screenshot({path:path.join(output,'history-desktop.png'),fullPage:true});
    await page.locator('#continuousHistoryRows [data-setup]').first().click();
    await page.locator('#continuousHistoryEvidence').waitFor({state:'visible'});
    assert.match(await page.locator('#continuousHistoryEvidence').textContent(),/snapshot_id/);
    assert.match(await page.locator('#continuousHistoryEvidence').textContent(),/simulated plan/);
    const historyDownload=page.waitForEvent('download');
    await page.locator('#continuousHistoryExport').click();
    assert.equal((await historyDownload).suggestedFilename(),'research-setup-history.json');
    const allHistory=await (await page.request.get(url+'/api/research/continuous/history')).json();
    assert.equal(allHistory.total,1);
    assert.equal(allHistory.events.length,1);
    await page.locator('#continuousHistorySearch').fill('MISSING');
    assert.equal(await page.locator('#continuousHistoryRows [data-setup]').count(),0);
    await page.locator('#continuousHistorySearch').fill('');
    await page.locator('[data-research-view=validation]').click();
    assert(await page.locator('#continuousHistoryEvidence').isHidden());
    await page.waitForFunction(()=>!document.getElementById('predictionAuditStatus').textContent.includes('Loading recorded outcomes'));
    assert.match(await page.locator('#predictionVerdict').textContent(),/No completed qualified checks yet/i);
    assert.match(await page.locator('.predictionScope').textContent(),/This does not test your trading bot/);
    assert.match(await page.locator('#predictionMetrics').textContent(),/0 \/ 30/);
    assert.match(await page.locator('#continuousValidationStatus').textContent(),/4h/);
    assert.match(await page.locator('#continuousSampleProgress').textContent(),/0 \/ 30 completed qualified observations/);
    assert.match(await page.locator('#continuousScoreRows').textContent(),/Below 40/);
    await page.screenshot({path:path.join(output,'validation-desktop.png'),fullPage:true});
    await page.locator('summary').filter({hasText:'How score ranges performed'}).click();
    await page.locator('#continuousScoreGrouping').selectOption('deciles');
    assert.equal(await page.locator('#continuousScoreRows tr').count(),10);
    await page.locator('#continuousScoreGrouping').selectOption('score_bands');
    assert.match(await page.locator('#continuousHealth').textContent(),/CoinDCX: .*verified/);
    const continuousDownload=page.waitForEvent('download');
    await page.locator('#continuousExport').click();
    assert.match((await continuousDownload).suggestedFilename(),/^research-[a-f0-9]+\.json$/);
    await page.locator('#continuousStart').click();
    await page.waitForFunction(()=>/RUNNING|SCANNING|QUEUED/.test(document.getElementById('continuousStatus').textContent));
    assert(await page.locator('#continuousStart').isDisabled());
    await page.locator('#continuousStop').click();
    await page.waitForFunction(()=>document.getElementById('continuousStatus').textContent.startsWith('STOPPED'));
    assert(await page.locator('#continuousStop').isDisabled());
    await page.locator('[data-research-view=manual]').click();
    await page.locator('#researchSettingsForm input').first().waitFor({state:'attached'});
    await page.getByRole('button', {name:'Scan Markets',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('researchStatus').textContent.startsWith('COMPLETE'));
    assert.match(await page.locator('#researchStatus').textContent(),/Manual scan \/ not scheduled/);
    assert(!/Next scan in/.test(await page.locator('#researchStatus').textContent()));
    assert.equal(await page.locator('#researchLongCount').textContent(), '1');
    assert.equal(await page.locator('#researchShortCount').textContent(), '1');
    assert.equal(await page.locator('#researchVerifiedCount').textContent(), '2');
    assert.equal(await page.locator('#researchRows tr').count(), 2);
    assert.match(await page.locator('#researchError').textContent(), /SYNTHETIC/);
    assert.match(await page.locator('#researchOutcomeStatus').textContent(), /2 \/ 200/);
    assert.match(await page.locator('#researchOutcomeMetrics').textContent(), /50%/);
    assert.match(await page.locator('#researchOutcomeRows').textContent(), /B-BTC_USDT/);
    assert.match(await page.locator('#researchOutcomeRows').textContent(), /HIT/);
    await page.getByRole('button',{name:'Check Due Outcomes',exact:true}).click();
    assert(await page.locator('canvas[data-chart]').first().evaluate(c=>[...c.getContext('2d').getImageData(0,0,c.width,c.height).data].some(v=>v!==0)));
    await page.getByRole('button',{name:'Short',exact:true}).click();
    assert.equal(await page.locator('#researchRows tr').count(),1);
    assert.match(await page.locator('#researchRows').textContent(),/B-ETH_USDT/);
    await page.locator('#researchRows').getByRole('button',{name:'Details',exact:true}).click();
    assert(await page.locator('#researchEvidence').isVisible());
    assert.match(await page.locator('#researchEvidence').textContent(),/Score components/);
    assert.match(await page.locator('#researchEvidence').textContent(),/VERIFIED/);
    await page.getByRole('button',{name:'All',exact:true}).click();
    await page.locator('#researchPairFilter').fill('BTC');
    assert.equal(await page.locator('#researchRows tr').count(),1);
    const download = page.waitForEvent('download');
    await page.getByRole('button',{name:'Export Snapshot',exact:true}).click();
    const file = await download;
    assert.match(file.suggestedFilename(), /^research-[a-f0-9]+\.json$/);
    await page.locator('#researchPairFilter').fill('');
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'Backtest',exact:true}).click();
    assert(!(await page.locator('#researchMain').isVisible()));
    await page.getByRole('button',{name:'Research',exact:true}).click();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth+1), 'Page overflows mobile viewport');
    await page.locator('[data-research-view=wallets]').click();
    await page.screenshot({path:path.join(output,'wallets-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Wallet view overflows mobile');
    await page.locator('[data-research-view=history]').click();
    await page.screenshot({path:path.join(output,'history-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'History view overflows mobile');
    await page.locator('[data-research-view=validation]').click();
    await page.waitForFunction(()=>!document.getElementById('predictionAuditStatus').textContent.includes('Loading recorded outcomes'));
    await page.screenshot({path:path.join(output,'validation-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Validation view overflows mobile');
    const completed = await (await page.request.get(url+'/api/research')).json();
    completed.stale = true; completed.status = 'error'; completed.error = 'Provider offline';
    await page.route('**/api/research', route=>route.fulfill({json:completed}));
    await page.locator('[data-research-view=manual]').click();
    await page.waitForFunction(()=>document.getElementById('researchStatus').textContent.includes('STALE SNAPSHOT'));
    assert.match(await page.locator('#researchStatus').textContent(),/STALE SNAPSHOT/);
    assert.match(await page.locator('#researchError').textContent(),/previous completed scan/);
    await page.screenshot({path:path.join(output,'stale-error.png'),fullPage:true});
    // A new publication updates assessments, not the frozen history episode.
    await page.locator('[data-research-view=current]').click();
    const current=await (await page.request.get(url+'/api/research/continuous')).json();
    current.running=true; current.busy=true; current.queued=false; current.stale=false;
    current.server_time_ms=Date.now(); current.snapshot.published_ms=Date.now();
    current.snapshot.id='clarity-first'; current.next_cycle_ms=null;
    current.snapshot.rows[0].setup={stage:'invalidated',awaiting_reset:true};
    let offline=false;
    await page.route('**/api/research/continuous',route=>offline?route.abort():route.fulfill({json:current}));
    await page.evaluate(()=>window.continuousResearchView.refresh());
    await page.locator('[data-research-view=history]').click();
    await page.locator('#continuousHistoryRows [data-setup]').first().click();
    await page.locator('#continuousHistoryEvidence').waitFor({state:'visible'});
    current.setups.items[0].revision_count++;
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert(await page.locator('#continuousHistoryEvidence').isHidden());
    await page.locator('[data-research-view=current]').click();
    assert.match(await page.locator('#continuousSnapshotStatus').textContent(),/UPDATE PENDING/);
    assert.match(await page.locator('#continuousFreshness').textContent(),/5 minutes \/ 5m Binance candles/);
    assert.match(await page.locator('#continuousCandleHeading').textContent(),/5m candle close/);
    assert(await page.locator('#continuousRefresh').isDisabled());
    assert(!/invalidated/.test(await page.locator('#continuousRows tr').first().textContent()));
    await page.evaluate(()=>document.querySelector('#continuousRows tr').dataset.unchanged='yes');
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert.equal(await page.locator('#continuousRows tr').first().getAttribute('data-unchanged'),'yes');
    current.busy=false; current.queued=true;
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert.match(await page.locator('#continuousStatus').textContent(),/QUEUED/);
    assert(await page.locator('#continuousRefresh').isDisabled());
    current.queued=false; current.refresh_available_ms=Date.now()+60000;
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert(await page.locator('#continuousRefresh').isDisabled());
    current.snapshot.id='clarity-next'; current.refresh_available_ms=current.server_time_ms=Date.now();
    current.message='Cycle complete'; current.next_cycle_ms=current.server_time_ms+300000;
    current.snapshot.rows[0].public_rank.score=91;
    current.snapshot.rows[0].tier='watch';
    current.snapshot.rows[0].timing='forming';
    current.snapshot.rows[0].plan.valid=true;
    current.snapshot.rows[0].plan.reason='Valid conditional pullback plan';
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert.match(await page.locator('#continuousRows tr').first().textContent(),/91/);
    assert.match(await page.locator('#continuousMetrics').textContent(),/Qualified ideas/);
    await page.locator('#continuousTier').selectOption('qualified');
    assert.equal(await page.locator('#continuousRows [data-plan]').count(),1);
    await page.locator('#continuousTier').selectOption('all');
    assert.match(await page.locator('#continuousSnapshotStatus').textContent(),/LATEST COMPLETED/);
    assert(!(await page.locator('#continuousRefresh').isDisabled()));
    offline=true;
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert.match(await page.locator('#continuousStatus').textContent(),/DISCONNECTED/);
    assert.match(await page.locator('#continuousSnapshotStatus').textContent(),/NOT CURRENT/);
    offline=false;
    await page.evaluate(()=>window.continuousResearchView.refresh());
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,'current-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('#researchMain').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,'current-mobile.png'),fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Current view overflows mobile');
    for (const row of current.snapshot.rows) {
      if (row.public_rank?.features) {
        delete row.public_rank.features.return_pct;
        delete row.public_rank.features.hour_change_pct;
      }
    }
    await page.evaluate(()=>window.continuousResearchView.refresh());
    assert.match(await page.locator('#continuousHighlights').textContent(),/No closed-candle movement data yet/);
    assert(!/NaN/.test(await page.locator('#continuousRows').textContent()));
    assert.deepEqual(errors,[]);
    console.log('PASS: scan, separated views, publication updates, blocked levels, retained table state, cooldown/queue, connection failures, history/export, validation, canvas, desktop/mobile; no JS errors.');
  } finally {
    if (browser) await browser.close();
    server.kill();
    if (server.exitCode === null) await new Promise(resolve=>server.once('exit',resolve));
  }
})().catch(e=>{console.error(e); process.exitCode=1;});
