from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.exchange.coindcx_rest import HTTPResponse
from app.research.market_scanner import DataUnavailable
from app.research.continuous.analysis import (STEP, aggregate, closed_market, concentration_analysis,
                                             lagging_pairs, oi_analysis, whale_analysis)
from app.research.continuous.config import load_config, config_id
from app.research.continuous.feeds import ResearchData
from app.research.continuous.levels import calculate_levels
from app.research.continuous.replay import replay_plan, summary
from app.research.continuous.service import ContinuousResearchService, ResearchEngine
from app.research.continuous.store import HistoryStore
from tests.test_market_research import AS_OF, FakeData, klines


class ContinuousFixture(FakeData):
    def __init__(self, cfg=None, stop=None, now_ms=AS_OF):
        self.now_ms, self.stop = now_ms, stop
        self.whale_calls, self.holder_calls = 0, 0

    def binance(self, path, **params):
        if self.stop and self.stop.is_set():
            from app.research.market_scanner import ScanStopped
            raise ScanStopped("Fixture cancelled")
        if path.endswith("/time"):
            return {"serverTime": self.now_ms}
        if path.endswith("openInterestHist"):
            step = 300_000 if params.get("period") == "5m" else STEP
            count = params.get("limit", 20)
            end = self.now_ms//step*step
            return [{"timestamp": end-(count-1-i)*step, "sumOpenInterest": 100+i} for i in range(count)]
        response = super().binance(path, **params)
        if path.endswith("klines"):
            step = 300_000 if params.get("interval") == "5m" else STEP
            shift = (self.now_ms//step-AS_OF//step)*step
            for row in response:
                row[0] += shift
                row[6] += shift
        if path.endswith("ticker/24hr"):
            for row in response:
                row["closeTime"] = self.now_ms
        if path.endswith("premiumIndex"):
            for row in response:
                row["time"] = self.now_ms
        return response

    def whale(self, asset, start, end):
        self.whale_calls += 1
        raise DataUnavailable("No paid access configured")

    def holders(self, asset):
        self.holder_calls += 1
        raise DataUnavailable("No paid access configured")


def plan():
    return {"valid": True, "entry": 100, "stop_loss": 90, "tp1": 115, "tp2": 120, "tp3": 130,
            "trigger": "pullback", "trigger_level": 100, "trigger_confirmed": False, "atr": 5,
            "rules": deepcopy(load_config()["levels"])}


def bar(i, op=101, hi=102, lo=100, close=101):
    t = (AS_OF//STEP+1+i)*STEP
    return [t, op, hi, lo, close, 100, t+STEP-1]


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_fingerprint(self):
        cfg = load_config()
        self.assertFalse(cfg["providers"]["coinglass_enabled"])
        self.assertEqual((cfg["market_interval"], cfg["schedules"]["market_seconds"]), ("5m", 300))
        self.assertEqual(cfg["universe"]["deep_pairs_per_cycle"], 100)
        self.assertEqual(config_id(cfg), config_id(deepcopy(cfg)))

    def test_invalid_weights_targets_units_and_limits_rejected(self):
        mutations = [("weights", "oi", 0.8), ("levels", "tp1_r", 1),
                     ("levels", "scale_out", [0.5, 0.5, 0.5]), ("levels", "atr_multiplier", float("nan")),
                     ("schedules", "market_seconds", 1), ("universe", "deep_pairs_per_cycle", 2.2)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            for section, key, value in mutations:
                cfg = load_config()
                cfg[section][key] = value
                path.write_text(json.dumps(cfg), encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(ValueError):
                    load_config(path)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.raw = {"candles": klines("15m")}
        self.market = closed_market(self.raw, "BTCUSDT", AS_OF)

    def test_four_quadrants_and_aligned_times(self):
        end = AS_OF//STEP*STEP
        for price_sign, oi_sign, expected in ((1,1,"price_up_oi_up"),(-1,1,"price_down_oi_up"),
                                               (1,-1,"price_up_oi_down"),(-1,-1,"price_down_oi_down")):
            raw = {"candles": klines("15m", price_sign), "oi": [{"timestamp": end-16*STEP, "sumOpenInterest": 100},
                                                                  {"timestamp": end, "sumOpenInterest": 100+oi_sign*10}]}
            self.cfg["thresholds"]["price_change_pct"] = 0.1
            result = oi_analysis(raw, closed_market(raw, "BTCUSDT", AS_OF), self.cfg, AS_OF)
            self.assertEqual(result["state"], expected)
            self.assertIn("not proof", result["basis"])
        raw["oi"][0]["timestamp"] += STEP
        with self.assertRaises(DataUnavailable):
            oi_analysis(raw, self.market, self.cfg, AS_OF)

    def test_oi_outlier_and_funding_penalty(self):
        end = AS_OF//STEP*STEP
        raw = {**self.raw, "oi": [{"timestamp":end-16*STEP,"sumOpenInterest":100}, {"timestamp":end,"sumOpenInterest":130}],
               "funding":{"time":AS_OF,"lastFundingRate":0.001}}
        result = oi_analysis(raw,self.market,self.cfg,AS_OF)
        self.assertTrue(result["outlier"])
        self.assertEqual(result["score"], self.cfg["oi_scores"]["flat"]-15)

    def test_whale_dedup_windows_internal_and_coverage(self):
        event = {"id":"a", "timestamp_ms":AS_OF-1000, "usd":1000000, "direction":"outflow"}
        feed = {"events":[event,event,{**event,"id":"b","direction":"internal"}],
                "minimum_usd":500000,"from_ms":AS_OF-86400_000,"to_ms":AS_OF}
        result = whale_analysis(feed,self.cfg,AS_OF)
        self.assertEqual(result["net_whale_flow_24h"],1000000)
        self.assertEqual(result["large_tx_count"],2)
        self.assertEqual(result["flow_direction"],"accumulation")
        feed["minimum_usd"]=10000000
        self.assertEqual(whale_analysis(feed,self.cfg,AS_OF)["status"],"limited")

    def test_holder_cluster_entity_link_risk_and_stale(self):
        cluster = {"members":["a"],"share_pct":20,"entity_ids":["owner"],"custodial_share_pct":0}
        feed = {"timestamp_ms":AS_OF,"coverage":"top only","clusters":[cluster,{**cluster,"members":["b"]}]}
        result = concentration_analysis(feed,self.cfg,AS_OF)
        self.assertTrue(result["excluded"])
        self.assertEqual(result["largest_non_custodial_cluster_pct"],40)
        feed["timestamp_ms"] -= 86400_000
        with self.assertRaises(DataUnavailable):
            concentration_analysis(feed,self.cfg,AS_OF)

    def test_high_conviction_requires_two_directional_families_and_rr(self):
        oi = {"score":85,"direction":"bullish"}
        whale = {"score":90,"flow_direction":"accumulation","status":"available"}
        holder = {"risk_score":10,"status":"available","excluded":False}
        self.assertEqual(aggregate(oi,whale,holder,{"valid":True,"risk_reward":1.5},self.cfg)["tier"],"high_conviction")
        self.assertEqual(aggregate(oi,whale,holder,{"valid":True,"risk_reward":1},self.cfg)["tier"],"watch")
        result = aggregate(oi,{}, {},{"valid":True},self.cfg)
        self.assertEqual(result["tier"],"watch")
        self.assertEqual(result["score_coverage_pct"],40)
        self.assertEqual(result["composite_score"],34)
        self.assertEqual(aggregate(oi,whale,holder,{"valid":False},self.cfg)["tier"],"watch")
        holder["excluded"] = True
        self.assertEqual(aggregate(oi,whale,holder,{"valid":True},self.cfg)["tier"],"avoid")

    def test_level_geometry_breathing_room_and_no_chase(self):
        result = calculate_levels(self.market,"BTCUSDT",self.cfg)
        self.assertGreaterEqual(result["entry"]-result["stop_loss"],self.market["features"]["atr"])
        self.assertFalse(result["valid"])
        self.assertIn("minimum reward",result["reason"])
        m = deepcopy(self.market)
        m["features"].update(close=130,ema21=125,ema55=115,atr=2,trend=1)
        self.cfg["levels"]["trigger"]="breakout"
        result = calculate_levels(m,"BTCUSDT",self.cfg)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["risk_reward"],1.5)
        m["features"]["close"]=150
        self.assertFalse(calculate_levels(m,"BTCUSDT",self.cfg)["valid"])

    def test_sector_catchup_uses_relative_return_not_other_price(self):
        def candles(start, scale):
            price=start
            out=[]
            for i in range(100):
                price *= 1+scale*(0.002+0.001*math.sin(i))
                out.append([i*STEP,price,price,price,price,1,(i+1)*STEP-1])
            return {"candles":out}
        markets={"AAAUSDT":candles(1,1),"BBBUSDT":candles(1000,2)}
        self.cfg["sectors"]={"AAAUSDT":"test","BBBUSDT":"test"}
        result=lagging_pairs(markets,self.cfg)["AAAUSDT"]
        self.assertLess(result["catchup_price"],10)
        self.assertEqual(result["leader"],"BBBUSDT")


class ReplayTests(unittest.TestCase):
    def test_expiry_does_not_include_later_candle_high(self):
        p=plan()
        p["rules"]["plan_hours"]=1
        bars=[bar(i,101,102,100,101) for i in range(4)]
        bars[-1]=bar(3,101,140,100,130)
        result=replay_plan(p,AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["tp_hits"],[False]*3)
        self.assertEqual(result["status"],"timeout")

    def test_open_position_gap_stop_uses_worse_open(self):
        bars=[bar(0),bar(1,80,95,79,90)]
        result=replay_plan(plan(),AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["status"],"stop_loss")
        self.assertLess(result["net_return_pct"],-20)

    def test_new_trail_cannot_stop_trade_on_same_bar(self):
        p=plan()
        p["rules"]["min_rr"]=1
        initial=klines("15m")
        bars=initial+[bar(-1),bar(0),bar(1,101,121,91,120)]
        result=replay_plan(p,AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["tp_hits"],[True,True,False])
        self.assertEqual(result["status"],"open")

    def test_never_uses_prepublication_or_forming_bar(self):
        bars=[bar(-1,100,200,1,100),bar(0)]
        result=replay_plan(plan(),AS_OF,bars,bar(0)[0]+1,"BTCUSDT")
        self.assertEqual(result["status"],"pending")

    def test_stops_first_on_ambiguous_candle(self):
        bars=[bar(0,101,140,80,105)]
        result=replay_plan(plan(),AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["status"],"stop_loss")
        self.assertEqual(result["tp_hits"],[False]*3)
        self.assertLess(result["net_return_pct"],-10)

    def test_pullback_does_not_claim_preentry_targets(self):
        bars=[bar(0,101,140,99,102),bar(1,102,116,101,110)]
        result=replay_plan(plan(),AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["tp_hits"],[True,False,False])
        self.assertAlmostEqual(result["remaining_fraction"],0.5)

    def test_scaled_targets_and_fee_accounting(self):
        bars=[bar(0),bar(1,101,131,100,130)]
        result=replay_plan(plan(),AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["status"],"all_targets")
        self.assertEqual(result["tp_hits"],[True]*3)
        self.assertAlmostEqual(result["gross_return_pct"],19.5)
        self.assertLess(result["net_return_pct"],result["gross_return_pct"])

    def test_breakout_requires_close_then_next_open(self):
        p=plan()
        p["trigger"]="breakout"
        p["rules"]["slippage_bps"]=0
        p["rules"]["min_rr"]=1.4
        bars=[bar(0,99,105,95,101),bar(1,100,110,99,105)]
        result=replay_plan(p,AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["entered_ms"],bars[1][0])
        bars[1][1]=119
        bars[1][2]=120
        self.assertEqual(replay_plan(p,AS_OF,bars,bars[-1][6]+1,"BTCUSDT")["status"],"fill_geometry_rejected")

    def test_gaps_do_not_become_wins(self):
        bars=[bar(1,101,140,100,130)]
        result=replay_plan(plan(),AS_OF,bars,bars[-1][6]+1,"BTCUSDT")
        self.assertEqual(result["status"],"data_gap")
        self.assertFalse(result["terminal"])


class EngineTests(unittest.TestCase):
    def test_only_confirmed_closes_enter_replay_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/"r.db")
            raw=[bar(0),bar(1,101,500,1,200)]
            store.observe("market","BTCUSDT",bar(1)[0]+1000,"test",
                          {"status":"available","raw":{"candles":raw},"closed_through_ms":raw[0][6]})
            self.assertEqual(store.candles("BTCUSDT"),[raw[0]])
            self.assertEqual(store.all_markets()["BTCUSDT"]["candles"],[raw[0]])
            store.observe("market","BTCUSDT",bar(1)[6]+1,"test",
                          {"status":"available","raw":{"candles":raw},"closed_through_ms":raw[1][6]})
            self.assertEqual(len(store.candles("BTCUSDT")),2)

    def test_full_universe_missing_paid_data_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/"r.db")
            cfg=load_config()
            data=ContinuousFixture()
            first=ResearchEngine(store,data,cfg).cycle()
            data.now_ms += 1000
            second=ResearchEngine(store,data,cfg).cycle()
            self.assertEqual(first["coverage"]["universe"],3)
            self.assertEqual(first["coverage"]["analysed"],2)
            self.assertEqual(data.whale_calls,2)
            self.assertEqual(data.holder_calls,2)
            self.assertFalse(any(r["tier"]=="high_conviction" for r in first["rows"]))
            self.assertEqual(len(store.publications_since(0)),2)
            self.assertEqual(store.latest_publication()["id"],second["id"])
            self.assertIsNotNone(store.observation(first["rows"][0]["evidence"]["market"]))

    def test_deep_budget_rotates_and_never_relabels_old_rows_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/"r.db")
            cfg=load_config()
            cfg["universe"].update(deep_pairs_per_cycle=1,priority_pairs=0)
            first=ResearchEngine(store,ContinuousFixture(),cfg).cycle()
            second=ResearchEngine(store,ContinuousFixture(),cfg).cycle()
            a=next(r for r in first["rows"] if r["tier"]!="unscanned")
            b=next(r for r in second["rows"] if r["tier"]!="unscanned")
            self.assertNotEqual(a["symbol"],b["symbol"])
            self.assertIsNone(next(r for r in second["rows"] if r["symbol"]==a["symbol"])["last_updated"])

    def test_worker_lease_and_service_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"r.db"
            store=HistoryStore(path)
            self.assertTrue(store.claim("a",int(time.time()*1000)+60000))
            self.assertFalse(store.claim("b",int(time.time()*1000)+60000))
            store.release("b")
            self.assertFalse(store.claim("b",int(time.time()*1000)+60000))
            store.release("a")
            service=ContinuousResearchService(path,data_factory=ContinuousFixture)
            service.start()
            deadline=time.monotonic()+3
            while not service.status()["snapshot"] and time.monotonic()<deadline:
                time.sleep(0.02)
            service.close()
            self.assertIsNotNone(service.status()["snapshot"])
            self.assertIsNone(service.status()["next_cycle_ms"])
            self.assertFalse(service.status()["queued"])
            self.assertEqual(service.status()["last_completed_ms"], service.status()["snapshot"]["published_ms"])
            restored=ContinuousResearchService(path)
            self.assertTrue(restored.status()["stale"])
            self.assertFalse(restored.status()["running"])
            restored.close()

    def test_refresh_reports_queue_and_suppresses_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=ContinuousResearchService(Path(tmp)/"r.db")
            service._thread=Mock(is_alive=Mock(return_value=True))
            service._last_started=time.monotonic()-61
            with patch.object(service,"start"):
                service.refresh()
                self.assertTrue(service.status()["queued"])
                self.assertEqual(service.status()["message"],"Scan queued")
                self.assertIsNone(service.status()["next_cycle_ms"])
                self.assertTrue(service._wake.is_set())
                service._wake.clear()
                service.refresh()
                self.assertFalse(service._wake.is_set())

    def test_refresh_exposes_cooldown_and_does_not_schedule_while_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=ContinuousResearchService(Path(tmp)/"r.db")
            service._thread=Mock(is_alive=Mock(return_value=True))
            service._last_started=time.monotonic()
            with patch.object(service,"start"):
                service.refresh()
                status=service.status()
                self.assertGreater(status["refresh_available_ms"]-status["server_time_ms"],58000)
                self.assertFalse(status["queued"])
                service._last_started=time.monotonic()-61
                service._busy=True
                service.refresh()
                self.assertFalse(service._wake.is_set())

    def test_busy_cycle_retains_last_publication_and_clears_old_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"r.db"
            first=ResearchEngine(HistoryStore(path),ContinuousFixture(),load_config()).cycle()
            entered,release=threading.Event(),threading.Event()
            def factory(cfg,stop):
                entered.set()
                release.wait(3)
                return ContinuousFixture(cfg,stop)
            service=ContinuousResearchService(path,data_factory=factory)
            service._next_ms=1234
            try:
                service.start()
                self.assertTrue(entered.wait(3))
                status=service.status()
                self.assertTrue(status["busy"])
                self.assertFalse(status["queued"])
                self.assertIsNone(status["next_cycle_ms"])
                self.assertIsNotNone(status["cycle_started_ms"])
                self.assertEqual(status["snapshot"]["id"],first["id"])
                self.assertEqual(status["interval_seconds"],300)
                self.assertEqual(status["market_interval"],"5m")
            finally:
                release.set()
                service.close()

    def test_failed_cycle_keeps_history_and_reports_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"r.db"
            first=ResearchEngine(HistoryStore(path),ContinuousFixture(),load_config()).cycle()
            service=ContinuousResearchService(path,data_factory=Mock(side_effect=DataUnavailable("Provider offline")))
            try:
                service.start()
                deadline=time.monotonic()+3
                while (service.status()["error"] is None or service.status()["busy"]) and time.monotonic()<deadline:
                    time.sleep(0.02)
                status=service.status()
                self.assertEqual(status["error"],"Provider offline")
                self.assertTrue(status["stale"])
                self.assertFalse(status["busy"])
                self.assertEqual(status["snapshot"]["id"],first["id"])
                self.assertIsNotNone(status["next_cycle_ms"])
            finally:
                service.close()


class ConnectorTests(unittest.TestCase):
    def test_bubblemaps_units_and_custodial_clusters(self):
        cfg=load_config()
        cfg["providers"]["bubblemaps_enabled"]=True
        data=ResearchData(cfg,threading.Event())
        payload={"metadata":{"ts_update":AS_OF//1000},"clusters":[{"holders":["a","b"]}],
                 "nodes":{"top_holders":[{"address":"a","holder_data":{"share":0.2},"address_details":{"is_cex":True}},
                                            {"address":"b","holder_data":{"share":0.1},"address_details":{"entity_id":"owner"}}]}}
        with patch.dict("os.environ",{"BUBBLEMAPS_API_KEY":"test-secret"}),patch.object(data,"_get",return_value=payload):
            result=data.holders({"holder_chain":"eth","holder_address":"0x1234","holder_share_unit":"fraction"})
        self.assertAlmostEqual(result["clusters"][0]["share_pct"],30)
        self.assertAlmostEqual(result["clusters"][0]["custodial_share_pct"],20)
        self.assertNotIn("test-secret",json.dumps(result))

    def test_coinglass_disabled_does_not_request_or_read_key(self):
        data=ResearchData(load_config(),threading.Event())
        with patch.object(data,"_get") as get:
            with self.assertRaisesRegex(DataUnavailable,"disabled"):
                data.whale({},0,AS_OF)
            get.assert_not_called()

    def test_coinglass_labels_and_floor(self):
        cfg=load_config()
        cfg["providers"]["coinglass_enabled"]=True
        data=ResearchData(cfg,threading.Event())
        rows=[{"transaction_hash":"x","amount_usd":12000000,"asset_symbol":"BTC",
               "blockchain_name":"bitcoin","block_timestamp":AS_OF//1000,"from":"Binance","to":"unknown wallet"}]
        with patch.dict("os.environ",{"COINGLASS_API_KEY":"test-key"}),patch.object(data,"_get",return_value={"code":"0","data":rows}):
            result=data.whale({"coinglass_symbol":"BTC","coinglass_chain":"bitcoin"},AS_OF-86400_000,AS_OF)
        self.assertEqual(result["events"][0]["direction"],"outflow")
        self.assertEqual(result["minimum_usd"],10000000)
        self.assertNotIn("test-key",json.dumps(result))

    def test_denied_provider_is_not_retried_for_each_pair(self):
        class Transport:
            calls=0
            def request(self,*args):
                self.calls+=1
                return HTTPResponse(429,'{}',{"Retry-After":"180"})
        transport=Transport()
        data=ResearchData(load_config(),threading.Event(),transport)
        for _ in range(2):
            with self.assertRaises(DataUnavailable):
                data.binance("/fapi/v1/time")
        self.assertEqual(transport.calls,1)
        self.assertGreater(data.cooldowns["https://fapi.binance.com"],time.time()+170)


if __name__ == "__main__":
    unittest.main()
