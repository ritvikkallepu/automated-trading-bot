from pathlib import Path
import tempfile
import threading
import unittest
import json
from urllib.request import urlopen
from urllib.error import HTTPError

from app.research.continuous.analysis import STEP
from app.research.continuous.runtime import research_server
from app.research.continuous.store import HistoryStore
from app.research.continuous.validation_audit import audit_page, audit_detail


class ValidationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = HistoryStore(self.root/'research'/'research.sqlite3')
        self.start = 100*STEP
        self.now = self.start+20*STEP

    def sample(self, ident, **values):
        return {"id":ident,"symbol":"BTCUSDT","pair":"B-BTC_USDT","published_ms":self.start-1000,
                "first_ms":self.start,"horizon_ms":16*STEP,"config_id":"cfg","ranking_version":"v1",
                "candidate":True,"score":70,"threshold":65,"timing":"forming",
                "rules":{"fee_bps_per_side":6,"slippage_bps":4},**values}

    def add(self, samples):
        self.store.publish({"id":"pub","published_ms":self.start-1000,"config_id":"cfg",
                            "rows":[{"symbol":"BTCUSDT","reference_price":98}]},samples=samples)

    def complete(self, s, value):
        self.store.save_validation(s['id'],{**s,"status":"complete","terminal":True,"net_return_pct":value,
                                          "entry_price":100,"exit_price":101,"mfe_pct":3,"mae_pct":-2})

    def test_success_denominator_excludes_waiting_and_gaps_and_controls(self):
        rows=[self.sample('win'),self.sample('loss'),self.sample('flat'),self.sample('gap'),
              self.sample('wait',first_ms=self.now),self.sample('control',candidate=False)]
        self.add(rows)
        for s,v in zip(rows[:3],[1,-1,0]):self.complete(s,v)
        self.complete(rows[-1],99)
        self.store.save_validation('gap',{**rows[3],'status':'data_gap','net_return_pct':100})
        r=audit_page(self.store,now_ms=self.now)
        self.assertEqual(r['summary'],{'positive':1,'negative':1,'flat':1,'pending':1,'awaiting_check':0,'data_gap':1,'total':5,'checked':3})
        self.assertEqual(r['cohort_counts'],{'candidates':5,'controls':1})
        gap=next(s for s in r['items'] if s['id']=='gap')
        self.assertIsNone(gap['net_return_pct'])
        self.assertEqual(audit_page(self.store,cohort='controls',now_ms=self.now)['summary']['positive'],1)

    def test_pagination_filters_and_order_are_stable(self):
        rows=[self.sample(str(i),published_ms=self.start+i,pair='B-ETH_USDT' if i%2 else 'B-BTC_USDT',config_id='old' if i==29 else 'cfg') for i in range(30)]
        self.add(rows)
        a=audit_page(self.store,config_id='cfg',now_ms=self.now)
        b=audit_page(self.store,config_id='cfg',page=1,now_ms=self.now)
        self.assertEqual([a['total'],len(a['items']),len(b['items'])],[29,25,4])
        self.assertEqual(a['items'][0]['id'],'28')
        self.assertFalse({r['id'] for r in a['items']} & {r['id'] for r in b['items']})
        self.assertEqual(audit_page(self.store,query='eth',now_ms=self.now)['total'],15)
        self.assertEqual(audit_page(self.store,outcome='pending',now_ms=self.now)['summary']['awaiting_check'],30)
        for values in ({'cohort':'winners'},{'page':-1},{'page':True},{'outcome':'wrong'}):
            with self.assertRaises(ValueError):audit_page(self.store,**values)

    def test_complete_result_cannot_relabel_frozen_qualification(self):
        s=self.sample('x',candidate=False)
        self.add([s])
        self.complete({**s,'candidate':True,'score':100},1)
        r=audit_page(self.store,cohort='all',now_ms=self.now)['items'][0]
        self.assertFalse(r['candidate'])
        self.assertEqual(r['score'],70)

    def test_detail_uses_frozen_snapshot_not_latest_and_no_future_prices(self):
        s=self.sample('legacy')
        self.add([s])
        self.store.publish({'id':'new','published_ms':self.start+5000,'config_id':'cfg',
                            'rows':[{'symbol':'BTCUSDT','reference_price':999}]})
        bars=[[t,100,103,99,101,50,t+STEP-1] for t in range(self.start-STEP,self.start+18*STEP,STEP)]
        self.store.observe('market','BTCUSDT',self.now,'cfg',{'status':'available','closed_through_ms':self.now,'raw':{'candles':bars}})
        d=audit_detail(self.store,'legacy',now_ms=self.start+2*STEP)
        self.assertEqual(d['reference_price'],98)
        self.assertEqual(d['snapshot_id'],'pub')
        self.assertEqual(len(d['candles']),2)
        self.assertEqual(d['missing_open_times'],[])
        self.assertEqual(d['sample']['verdict'],'pending')
        self.assertEqual(len(audit_detail(self.store,'legacy',now_ms=self.now)['candles']),16)

    def test_gaps_remain_visible_and_reading_never_changes_saved_outcome(self):
        s=self.sample('gap')
        self.add([s])
        bars=[[t,100,103,99,101,50,t+STEP-1] for t in (self.start,self.start+2*STEP)]
        self.store.observe('market','BTCUSDT',self.now,'cfg',{'status':'available','closed_through_ms':self.now,'raw':{'candles':bars}})
        d=audit_detail(self.store,'gap',now_ms=self.now)
        self.assertEqual(len(d['missing_open_times']),14)
        self.assertEqual(d['sample']['verdict'],'awaiting_check')
        self.assertEqual(self.store.validation_results(),[])
        self.assertIsNone(audit_detail(self.store,'missing',now_ms=self.now))

    def test_read_only_dashboard_api_and_bad_filters(self):
        self.add([self.sample('sample')])
        self.store.set_state('research_enabled',False)
        server=research_server(self.root,0,None)
        worker=threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        base=f'http://127.0.0.1:{server.server_port}/api/research/continuous/validation'
        try:
            with urlopen(base) as r:self.assertEqual(json.load(r)['total'],1)
            with urlopen(base+'?sample_id=sample') as r:self.assertEqual(json.load(r)['sample']['id'],'sample')
            for suffix,code in [('?cohort=bad',400),('?page=1.5',400),('?sample_id=missing',404)]:
                with self.assertRaises(HTTPError) as err:urlopen(base+suffix)
                self.assertEqual(err.exception.code,code)
                err.exception.close()
        finally:
            server.shutdown();server.server_close();worker.join(3)


if __name__=='__main__':unittest.main()
