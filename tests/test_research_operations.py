from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from app.research.continuous.config import load_config
from app.research.continuous.health import check_health
from app.research.continuous.runtime import research_server, worker_overdue, single_supervisor, supervisor_missing, ParentWatchdog
from app.research.continuous.service import ResearchEngine
from app.research.continuous.store import HistoryStore, encode, decode
from app.research.continuous.validation import validation_summary
from tests.test_continuous_research import ContinuousFixture, AS_OF


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = HistoryStore(self.root/'research'/'research.sqlite3')
        self.cfg = load_config()

    def test_compression_legacy_migration_and_backup_lossless(self):
        payload = {'raw': ['repeated evidence '*100]*100, 'value': 0.123456789012345, 'unicode': '\u20b9'}
        self.assertIsInstance(encode(payload), bytes)
        self.assertEqual(decode(encode(payload)), payload)
        with self.store.connect() as db:
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?)', ('a','market','BTC',1,'config',json.dumps(payload)))
        self.assertEqual(self.store.observation('a')['data'], payload)
        result = self.store.compact(self.root/'backup.sqlite3')
        self.assertLess(result['after_bytes'], result['before_bytes'])
        self.assertEqual(result['rows']['observations'], 1)
        self.assertEqual(self.store.observation('a')['data'], payload)
        with closing(sqlite3.connect(self.root/'backup.sqlite3')) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM observations').fetchone()[0]), payload)
        with self.assertRaises(ValueError):
            self.store.compact(self.root/'backup.sqlite3')

    def test_compaction_refuses_live_lease_and_corrupt_payload(self):
        import time
        self.store.claim('active', int(time.time()*1000)+60_000)
        with self.assertRaises(ValueError):
            self.store.compact(self.root/'backup.sqlite3')
        self.store.release('active')
        with self.store.connect() as db:
            db.execute('INSERT INTO state VALUES (?,?)', ('bad','{broken'))
        with self.assertRaises(ValueError):
            self.store.compact(self.root/'backup.sqlite3')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT payload FROM state WHERE key='bad'").fetchone()[0], '{broken')

    def test_disappearing_journal_does_not_break_dashboard_status(self):
        original = Path.stat
        def stat(path, *args, **kwargs):
            if str(path).endswith('-wal'):
                raise FileNotFoundError('SQLite closed its journal')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', stat):
            status = self.store.storage_status()
        self.assertEqual(status['wal_bytes'], 0)
        self.assertGreater(status['database_bytes'], 0)

    def test_bands_boundaries_missing_scores_and_config_isolation(self):
        rows=[]
        for i,score in enumerate([39,40,54,55,64,65,79,80,100,None]):
            rows.append({'id':str(i),'published_ms':i,'symbol':'BTC','config_id':'a','ranking_version':'v1',
                         'threshold':65,'candidate':score is not None and score>=65,'score':score,
                         'status':'complete','net_return_pct':i-5,'mfe_pct':10,'mae_pct':-1})
        rows += [{**rows[5], 'id':'pending', 'status':'pending'}, {**rows[5], 'id':'gap', 'status':'data_gap'}]
        rows += [{**rows[5], 'id':'other', 'config_id':'b'}]
        groups=validation_summary(rows)['groups']
        a,b=groups
        self.assertEqual([g['count'] for g in a['score_bands']], [1,2,2,2,2,1])
        self.assertEqual(a['progress']['complete'],4)
        self.assertEqual(a['progress']['pending'],1)
        self.assertEqual(a['progress']['data_gaps'],1)
        self.assertEqual(b['progress']['complete'],1)
        self.assertEqual(sum(g['count'] for g in a['deciles']),9)
        tied=[g for g in a['deciles'] if g['min_score'] is not None and g['min_score']<=65<=g['max_score']]
        self.assertEqual(len(tied),1)

    def test_missed_scan_recovery_retry_and_intentional_pause(self):
        self.cfg['alerts']['telegram_enabled']=True
        self.store.publish({'id':'one','published_ms':1000})
        with patch('app.research.continuous.health.send_message', return_value=True) as send:
            a=check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=500_000)
            check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=600_000)
            self.assertEqual(send.call_count,1)
            self.assertEqual(a['status'],'process_down')
            self.assertEqual(check_health(self.store,self.cfg,process_alive=True,started_ms=600_000,now_ms=650_000)['status'],'scan_overdue')
            self.store.publish({'id':'two','published_ms':850_000})
            self.assertEqual(check_health(self.store,self.cfg,process_alive=True,started_ms=600_000,now_ms=900_000)['status'],'healthy')
            self.assertEqual(send.call_count,2)
            self.store.set_state('research_enabled',False)
            self.assertEqual(check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=4_000_000)['status'],'paused')
            self.assertEqual(send.call_count,2)

    def test_missing_phone_credentials_are_not_recorded_as_delivered(self):
        self.cfg['alerts']['telegram_enabled']=True
        with patch('app.research.continuous.health.send_message', return_value=False) as send:
            check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=500_000)
            check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=600_000)
            check_health(self.store,self.cfg,process_alive=False,started_ms=1000,now_ms=900_000)
            self.assertEqual(send.call_count,2)
            self.assertIsNone(self.store.state('health_alert:telegram')['event_id'])

    def test_catalogue_failure_never_reuses_verified_listing(self):
        data=ContinuousFixture()
        first=ResearchEngine(self.store,data,self.cfg).cycle()
        self.assertEqual(first['coindcx_catalogue']['status'],'verified')
        with patch.object(data,'dcx_pairs',side_effect=ValueError('bad response')):
            second=ResearchEngine(self.store,data,self.cfg).cycle()
        self.assertEqual(second['coindcx_catalogue']['status'],'unavailable')
        self.assertTrue(all(r['coindcx_listed'] is None for r in second['rows']))
        self.assertIsNotNone(second['coindcx_catalogue']['last_success'])

    def test_worker_deadline_and_pause(self):
        self.store.set_state('worker_status', {'phase':'scanning','cycle_started_ms':1000})
        self.assertTrue(worker_overdue(self.store,self.cfg,2_000_000))
        self.store.set_state('research_enabled',False)
        self.assertFalse(worker_overdue(self.store,self.cfg,2_000_000))
        with single_supervisor(self.root/'lock'):
            with self.assertRaises(OSError):
                with single_supervisor(self.root/'lock'):
                    pass

    def test_orphan_worker_stops_without_killing_unrelated_processes(self):
        self.store.set_state('supervisor', {'child_pid':42,'heartbeat_ms':1000})
        self.assertFalse(supervisor_missing(self.store,42,60_000))
        self.assertTrue(supervisor_missing(self.store,42,100_000))
        self.assertTrue(supervisor_missing(self.store,43,60_000))

    def test_resume_grace_needs_consecutive_failed_checks(self):
        guard = ParentWatchdog()
        self.assertFalse(guard.expired(True))
        self.assertFalse(guard.expired(False))
        for _ in range(5):
            self.assertFalse(guard.expired(True))
        self.assertTrue(guard.expired(True))

    def test_research_runtime_denies_trading_and_respects_saved_pause(self):
        self.store.set_state('research_enabled',False)
        server=research_server(self.root,0,None)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/research/continuous') as response:
                self.assertFalse(json.load(response)['running'])
            with self.assertRaises(HTTPError) as err:
                urlopen(Request(base+'/api/live/start', data=b'{}',method='POST'))
            self.assertEqual(err.exception.code,403)
            err.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__=='__main__':
    unittest.main()
