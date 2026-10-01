// Synthetic UI checks. No actual research history or trading state is modified.
const {chromium}=require('playwright');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const assert=require('node:assert/strict');

(async()=>{
  const process=spawn(global.process.env.RESEARCH_TEST_PYTHON||'python',['-m','tests.research_preview','--fixture'],{windowsHide:true});
  let browser;
  try{
    const url=await new Promise((resolve,reject)=>{
      let text='';const timer=setTimeout(()=>reject(Error('Preview timeout')),15000);
      process.stdout.on('data',b=>{text+=b;const m=text.match(/PREVIEW_URL=(http:\/\/[^\s]+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
      process.once('error',reject);
    });
    browser=await chromium.launch({channel:'msedge',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    const state=await(await page.request.get(url+'/api/research/continuous')).json();
    const cfg=state.snapshot.config_id,step=300000,first=Math.floor(Date.now()/step)*step-6*3600000;
    let records=[
      {id:'win',pair:'B-BTC_USDT',verdict:'positive',net_return_pct:0.8,exit_price:101},
      {id:'loss',pair:'B-ETH_USDT',verdict:'negative',net_return_pct:-1.2,exit_price:99},
      {id:'wait',pair:'B-SOL_USDT',verdict:'pending',net_return_pct:null,exit_price:null},
      {id:'gap',pair:'B-XRP_USDT',verdict:'data_gap',net_return_pct:null,exit_price:null},
      {id:'control',pair:'B-ADA_USDT',verdict:'positive',net_return_pct:2,exit_price:102,candidate:false},
    ].map(r=>({candidate:true,interval:'5m',score:70,threshold:65,first_ms:first,published_ms:first-60000,end_ms:first+14400000,horizon_ms:14400000,entry_price:r.net_return_pct==null?null:100,mfe_pct:r.net_return_pct==null?null:3,mae_pct:r.net_return_pct==null?null:-2,rules:{slippage_bps:4,fee_bps_per_side:6},...r}));
    let g=state.validation.groups[0];
    Object.assign(g,{interval:'5m',candidates:{count:2,positive_rate:0.5,mean_net_pct:-0.2},controls:{count:1,mean_net_pct:2},progress:{complete:2,pending:1,data_gaps:1,target:30,remaining:28,unique_pairs:2}});
    await page.route('**/api/research/continuous',r=>r.fulfill({json:state}));
    await page.route('**/api/research/continuous/validation?*',route=>{
      const params=new URL(route.request().url()).searchParams,id=params.get('sample_id');
      if(id){
        const s=records.find(r=>r.id===id),length=s.verdict==='data_gap'?0:s.verdict==='pending'?1:48;
        return route.fulfill({json:{sample:s,reference_price:99.5,snapshot_id:'synthetic',frozen_assessment:{fixture:true},source:'SYNTHETIC stored 5m candles',method:'Synthetic four-hour test, not real trades.',expected_candles:48,missing_open_times:s.verdict==='data_gap'?[first]:[],candles:Array.from({length},(_,i)=>({open_ms:first+i*step,close_ms:first+(i+1)*step-1,open:100,high:103,low:98,close:100+(s.id==='loss'?-1:1)*(i+1)/48}))}});
      }
      const cohort=params.get('cohort'),outcome=params.get('outcome'),q=params.get('q').toUpperCase();
      const all=records.filter(r=>(cohort==='all'||r.candidate===(cohort==='candidates'))&&r.pair.includes(q));
      const counts=Object.fromEntries(['positive','negative','flat','pending','awaiting_check','data_gap'].map(k=>[k,all.filter(r=>r.verdict===k).length]));
      const items=all.filter(r=>outcome==='all'||r.verdict===outcome);
      return route.fulfill({json:{items,total:items.length,page:0,pages:1,summary:{...counts,total:all.length,checked:counts.positive+counts.negative+counts.flat},cohort_counts:{candidates:4,controls:1}}});
    });
    await page.goto(url+'/#research');
    await page.locator('[data-research-view=validation]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#predictionRows [data-prediction]').length===4);
    assert.match(await page.locator('#predictionVerdict').textContent(),/not enough evidence/i);
    assert.match(await page.locator('#predictionOutcomeHeading').textContent(),/5m Research Rank Outcomes/);
    assert.match(await page.locator('.predictionScope').textContent(),/Weighted Hybrid trades on 5m \/ 1m: not measured here/);
    assert.match(await page.locator('#predictionVerdictDetail').textContent(),/does not measure Weighted Hybrid trade performance/);
    assert.match(await page.locator('#predictionVerdictDetail').textContent(),/1 positive out of 2 checked/);
    assert.match(await page.locator('#predictionRows').textContent(),/Cannot verify/);
    assert(!/100%|proven profitable/i.test(await page.locator('#predictionVerdictDetail').textContent()));
    await page.locator('[data-prediction=win]').click();
    await page.locator('#predictionChart').waitFor();
    assert.match(await page.locator('#predictionDetail').textContent(),/Positive after costs.*\+0.8%/s);
    const drawn=await page.locator('#predictionChart').evaluate(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=0;i<d.length;i+=4)if(d[i+1]>140&&d[i+2]>140)n++;return n;});
    assert(drawn>100,'Price chart is blank');
    fs.mkdirSync('.test-output/validation',{recursive:true});
    await page.screenshot({path:'.test-output/validation/evidence-desktop.png',fullPage:true});
    await page.locator('#predictionDetailClose').click();
    await page.locator('#predictionOutcome').selectOption('negative');
    await page.waitForFunction(()=>document.querySelectorAll('#predictionRows [data-prediction]').length===1);
    assert.match(await page.locator('#predictionRows').textContent(),/ETH/);
    await page.locator('#predictionOutcome').selectOption('data_gap');
    await page.locator('[data-prediction=gap]').waitFor();await page.locator('[data-prediction=gap]').click();
    await page.locator('#predictionChart').waitFor();
    assert.match(await page.locator('#predictionDetail').textContent(),/Cannot verify/);
    assert(!/Net after costs\s*\+100/.test(await page.locator('#predictionDetail').textContent()));
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'.test-output/validation/evidence-mobile.png',fullPage:true});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.locator('#predictionDetailClose').click();
    await page.locator('#predictionOutcome').selectOption('all');
    await page.locator('#predictionCohort').selectOption('controls');
    await page.locator('[data-prediction=control]').waitFor();
    assert.match(await page.locator('#predictionRows').textContent(),/not a call/);
    await page.locator('#predictionCohort').selectOption('candidates');
    await page.locator('#predictionSearch').fill('absent');
    await page.waitForFunction(()=>document.querySelector('#predictionRows').textContent.includes('No observations'));
    assert.deepEqual(errors,[]);
    console.log('PASS: qualified/scored-only distinction, positive/negative/waiting/gap outcomes, filters, individual price evidence, nonblank canvas, mobile layout; no JS errors.');
  }finally{
    if(browser)await browser.close();process.kill();
    if(process.exitCode===null)await new Promise(resolve=>process.once('exit',resolve));
  }
})().catch(e=>{console.error(e);process.exitCode=1;});
