from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from app.research.continuous.analysis import STEP, closed_market, oi_analysis
from app.research.continuous.config import load_config, config_id
from app.research.continuous.notify import publish_alerts
from app.research.continuous.ranking import public_rank, ranking_changes, rank_rows, timing
from app.research.continuous.replay import replay_plan, replay_stored, summary
from app.research.continuous.setups import track_publication, setup_status, HORIZON
from app.research.continuous.store import HistoryStore
from app.research.continuous.validation import evaluate_sample, evaluate_stored, validation_summary
from app.research.continuous.validation_audit import audit_detail
from tests.test_continuous_research import AS_OF, plan, bar, klines


def snapshot(at=AS_OF, symbols=("BTCUSDT",), cfg=None, stage="forming", score=70):
    legacy = cfg is None
    cfg = deepcopy(cfg) if cfg is not None else load_config()
    if legacy:
        cfg["market_interval"] = "15m"
        cfg["schedules"]["market_seconds"] = 900
    rows = []
    for symbol in symbols:
        p = plan()
        p["interval"] = cfg["market_interval"]
        p["risk_reward"] = 1.5
        step = 300_000 if cfg["market_interval"] == "5m" else STEP
        p["as_of_ms"] = at//step*step-1
        p["valid"] = stage in {"forming", "trigger_confirmed"}
        rows.append({"symbol": symbol, "pair": "B-"+symbol[:-4]+"_USDT", "plan": p,
                     "tier": "watch", "timing": stage, "rank": 1,
                     "public_rank": {"score": score}, "evidence": {}})
    return {"id": uuid.uuid4().hex, "published_ms": at, "config_id": config_id(cfg),
            "config": cfg, "rows": rows}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"research.db"
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_updates_restart_and_config_change_do_not_move_original_levels(self):
        a = snapshot()
        track_publication(self.store, a)
        b = snapshot(AS_OF+STEP)
        b["rows"][0]["plan"]["entry"] = 105
        b["config"]["levels"]["tp1_r"] = 1.8
        b["config_id"] = config_id(b["config"])
        store = HistoryStore(self.path)
        track_publication(store, b)
        self.assertEqual(len(store.setups()), 1)
        s = store.setups()[0]
        self.assertEqual(s["plan"]["entry"], 100)
        self.assertEqual(s["published_ms"], AS_OF)
        self.assertEqual(s["revision_count"], 2)
        self.assertTrue(b["rows"][0]["setup"]["config_changed"])
        self.assertEqual(len(store.setup_events(s["id"])), 2)
        self.assertEqual(a["rows"][0]["setup"]["id"], b["rows"][0]["setup"]["id"])

    def test_pairs_are_isolated_and_completed_episode_requires_reset(self):
        a = snapshot(symbols=("BTCUSDT", "ETHUSDT"))
        track_publication(self.store, a)
        s = self.store.setups()[0]
        self.store.save_replay(s["id"], {"id": s["id"], "setup_id": s["id"], "terminal": True, "status": "all_targets"})
        track_publication(self.store, snapshot(AS_OF+STEP, symbols=("BTCUSDT", "ETHUSDT")))
        self.assertEqual(len(self.store.setups()), 2)
        track_publication(self.store, snapshot(AS_OF+2*STEP, stage="invalidated"))
        track_publication(self.store, snapshot(AS_OF+2*STEP+1000))
        self.assertEqual(len(self.store.setups()), 2, "same candle must not re-arm")
        track_publication(self.store, snapshot(AS_OF+3*STEP))
        self.assertEqual(len(self.store.setups()), 3)
        eth = next(s for s in self.store.setups() if s["symbol"] == "ETHUSDT")
        self.assertEqual(eth["revision_count"], 2)

    def test_unscanned_pair_is_not_cancelled(self):
        track_publication(self.store, snapshot())
        b = snapshot(AS_OF+STEP)
        b["rows"] = []
        track_publication(self.store, b)
        self.assertNotIn("cancelled_ms", self.store.setups()[0])

    def test_data_gap_cannot_cancel_and_spawn_another_episode(self):
        track_publication(self.store, snapshot())
        replay_stored(self.store, {}, AS_OF+4*STEP)
        self.assertEqual(self.store.replay_results()[0]["status"], "data_gap")
        track_publication(self.store, snapshot(AS_OF+4*STEP, stage="invalidated"))
        track_publication(self.store, snapshot(AS_OF+5*STEP))
        self.assertEqual(len(self.store.setups()), 1)
        self.assertNotIn("cancelled_ms", self.store.setups()[0])

    def test_pending_invalidation_is_terminal_not_a_loss(self):
        track_publication(self.store, snapshot())
        track_publication(self.store, snapshot(AS_OF+1000, stage="overextended"))
        replay_stored(self.store, {}, AS_OF+1000)
        r = self.store.replay_results()[0]
        self.assertEqual(r["status"], "invalidated")
        self.assertTrue(r["terminal"])
        self.assertEqual(summary([r])["groups"][0]["closed_entries"], 0)

    def test_replay_one_result_and_keeps_missing_old_episode(self):
        track_publication(self.store, snapshot())
        track_publication(self.store, snapshot(AS_OF+STEP))
        replay_stored(self.store, {}, AS_OF+4*86400_000)
        self.assertEqual(len(self.store.replay_results()), 1)
        self.assertEqual(self.store.replay_results()[0]["status"], "data_gap")
        bars = [bar(0), bar(1, 101, 131, 100, 130)]
        self.store.observe("market", "BTCUSDT", bars[-1][6]+1, "test",
                           {"status": "available", "raw": {"candles": bars}, "closed_through_ms": bars[-1][6]})
        replay_stored(self.store, {}, AS_OF+4*86400_000)
        r = self.store.replay_results()[0]
        self.assertTrue(r["terminal"])
        self.assertEqual(r["mfe_pct"], 30.000000000000004)
        self.assertEqual(summary([r])["groups"][0]["plans"], 1)
        self.assertEqual(setup_status(self.store)["items"][0]["stage"], "completed")

    def test_legacy_results_preserved_but_excluded_from_metrics(self):
        self.store.save_replay("legacy", {"id": "legacy", "terminal": True})
        self.assertEqual(summary(self.store.replay_results())["legacy_excluded"], 1)
        self.assertEqual(summary(self.store.replay_results())["groups"], [])

    def test_publication_and_history_commit_atomically(self):
        a = snapshot()
        track_publication(self.store, a)
        with self.assertRaises(Exception):
            track_publication(self.store, a)
        self.assertEqual(len(self.store.setup_events()), 1)
        self.assertEqual(self.store.setups()[0]["revision_count"], 1)

    def test_samples_all_pairs_including_invalid_but_never_overlap(self):
        track_publication(self.store, snapshot(stage="invalidated"))
        track_publication(self.store, snapshot(AS_OF+STEP))
        self.assertEqual(len(self.store.validation_samples()), 1)
        self.assertFalse(self.store.validation_samples()[0]["candidate"])
        track_publication(self.store, snapshot(AS_OF+HORIZON))
        self.assertEqual(len(self.store.validation_samples()), 2)
        self.assertTrue(self.store.validation_samples()[1]["candidate"])

    def test_five_minute_samples_do_not_reuse_legacy_fifteen_minute_candles(self):
        track_publication(self.store, snapshot())
        cfg = load_config()
        track_publication(self.store, snapshot(AS_OF+1000, cfg=cfg))
        samples = {s["interval"]: s for s in self.store.validation_samples()}
        self.assertEqual(set(samples), {"5m", "15m"})
        self.assertEqual(len(self.store.setups()), 2)
        start = samples["5m"]["first_ms"]
        bars = [[start+i*300_000, 100, 101, 99, 100, 10, start+(i+1)*300_000-1]
                for i in range(48)]
        self.store.observe("market", "BTCUSDT", bars[-1][6]+1, "test",
                           {"status": "available", "market_interval": "5m",
                            "raw": {"candles": bars}, "closed_through_ms": bars[-1][6]})
        self.assertEqual(len(self.store.candles("BTCUSDT", interval="5m")), 48)
        self.assertEqual(self.store.candles("BTCUSDT", interval="15m"), [])
        evaluate_stored(self.store, max(s["first_ms"] for s in samples.values())+HORIZON+1)
        results = {r["interval"]: r for r in self.store.validation_results()}
        self.assertEqual(results["5m"]["status"], "complete")
        self.assertEqual(results["15m"]["status"], "data_gap")
        with patch("app.research.continuous.validation.evaluate_sample", side_effect=AssertionError("unchanged evidence rechecked")):
            evaluate_stored(self.store, max(s["first_ms"] for s in samples.values())+HORIZON+1)
        legacy_start = samples["15m"]["first_ms"]
        legacy_bars = [[legacy_start+i*STEP, 100, 101, 99, 100, 10, legacy_start+(i+1)*STEP-1]
                       for i in range(16)]
        self.store.observe("market", "BTCUSDT", legacy_bars[-1][6]+1, "test",
                           {"status": "available", "raw": {"candles": legacy_bars},
                            "closed_through_ms": legacy_bars[-1][6]})
        evaluate_stored(self.store, max(s["first_ms"] for s in samples.values())+HORIZON+1)
        results = {r["interval"]: r for r in self.store.validation_results()}
        self.assertEqual(results["15m"]["status"], "complete")
        detail = audit_detail(self.store, samples["5m"]["id"], now_ms=start+HORIZON+1)
        self.assertEqual((detail["expected_candles"], len(detail["candles"])), (48, 48))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.market = closed_market({"candles": klines("15m")}, "BTCUSDT", AS_OF)

    def test_rank_responds_to_volume_acceleration_and_missing_evidence(self):
        oi = {"status": "available", "oi_change_pct": 3, "oi_acceleration_pp": 0.2, "direction": "bullish"}
        a = public_rank(self.market, oi, self.cfg)
        m = deepcopy(self.market)
        m["features"]["volume_ratio"] += 1
        b = public_rank(m, {**oi, "oi_acceleration_pp": 1.5}, self.cfg)
        self.assertGreater(b["score"], a["score"])
        c = public_rank(m, {"status": "unavailable"}, self.cfg)
        self.assertIsNone(c["contributions"]["oi"])
        self.assertEqual(c["coverage_pct"], 80)
        self.assertLess(c["score"], b["score"])
        self.assertAlmostEqual(sum(c["contributions"][k] or 0 for k in c["contributions"]), c["score"], places=2)

    def test_acceleration_uses_same_length_hours_and_no_future_point(self):
        end = AS_OF//STEP*STEP
        raw = {"candles": klines("15m"), "oi": [
            {"timestamp": end-i*STEP, "sumOpenInterest": value}
            for i, value in [(16, 80), (8, 90), (4, 100), (0, 120), (-4, 99999)]]}
        r = oi_analysis(raw, self.market, self.cfg, AS_OF)
        self.assertAlmostEqual(r["oi_acceleration_pp"], (1.2-100/90)*100)

    def test_timing_and_rotation_do_not_fake_rank_jumps(self):
        a = snapshot(symbols=("BTCUSDT", "ETHUSDT"), cfg=self.cfg)
        for r in a["rows"]:
            r["public_rank"]["features"] = {"extension_atr": 3}
        self.assertEqual(timing(a["rows"][0], self.cfg), "overextended")
        b = snapshot(symbols=("NEWUSDT", "BTCUSDT", "ETHUSDT"), cfg=self.cfg)
        b["rows"][0]["public_rank"]["score"] = 99
        b["rows"] = rank_rows(b["rows"])
        ranking_changes(b["rows"], a, self.cfg)
        self.assertIsNone(b["rows"][0]["rank_change"])
        self.assertEqual([r["rank_change"] for r in b["rows"][1:]], [0, 0])
        b["rows"][0]["plan"]["valid"] = False
        self.assertEqual(timing(b["rows"][0], self.cfg), "invalidated")

    def test_old_config_gets_ranking_defaults_invalid_weights_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"config.json"
            old = deepcopy(self.cfg)
            del old["ranking"]
            p.write_text(json.dumps(old))
            self.assertEqual(load_config(p)["ranking"], self.cfg["ranking"])
            old["ranking"] = deepcopy(self.cfg["ranking"])
            old["ranking"]["weights"]["oi"] = 2
            p.write_text(json.dumps(old))
            with self.assertRaises(ValueError):
                load_config(p)


class ValidationTests(unittest.TestCase):
    def test_later_missing_candles_do_not_erase_an_already_closed_trade(self):
        bars = [bar(0), bar(1, 101, 131, 100, 130), bar(4)]
        r = replay_plan(plan(), AS_OF, bars, bars[-1][6]+1, "BTCUSDT")
        self.assertEqual(r["status"], "all_targets")
        self.assertEqual(r["last_bar_ms"], bars[1][6])

    def test_gap_after_entry_retains_entry_and_conservative_excursions(self):
        bars = [bar(0, 101, 140, 99, 102), bar(2)]
        r = replay_plan(plan(), AS_OF, bars, bars[-1][6]+1, "BTCUSDT")
        self.assertEqual(r["status"], "data_gap")
        self.assertEqual(r["entry_price"], 100)
        self.assertEqual(r["mfe_pct"], 0)
        self.assertAlmostEqual(r["mae_pct"], -1)

    def sample(self):
        return {"id": "sample", "symbol": "BTCUSDT", "pair": "B-BTC_USDT", "published_ms": AS_OF,
                "first_ms": bar(0)[0], "horizon_ms": HORIZON, "config_id": "test", "ranking_version": "public-v1",
                "threshold": 65, "candidate": True, "rules": {"slippage_bps": 4, "fee_bps_per_side": 6}}

    def test_no_lookahead_and_same_costs_for_baseline(self):
        sample = self.sample()
        bars = [bar(i, 100, 115, 95, 110) for i in range(16)]
        end = bars[-1][6]+1
        self.assertEqual(evaluate_sample(sample, bars, end-1)["status"], "pending")
        a = evaluate_sample(sample, bars, end)
        b = evaluate_sample({**sample, "id": "control", "candidate": False}, bars, end)
        self.assertLess(a["net_return_pct"], 10)
        self.assertEqual(a["net_return_pct"], b["net_return_pct"])
        g = validation_summary([a,b])["groups"][0]
        self.assertEqual(g["candidate_minus_control_pct"], 0)
        self.assertEqual(g["all"]["count"], 2)

    def test_gap_and_nan_are_not_counted_as_winners(self):
        s = self.sample()
        bars = [bar(i) for i in range(16)]
        end = bars[-1][6]+1
        self.assertEqual(evaluate_sample(s, bars[1:], end)["status"], "data_gap")
        bars[5][2] = float("nan")
        self.assertEqual(evaluate_sample(s, bars, end)["status"], "data_gap")

    def test_old_complete_results_are_immutable_on_recheck(self):
        with tempfile.TemporaryDirectory() as d:
            store = HistoryStore(Path(d)/"r.db")
            track_publication(store, snapshot())
            s = store.validation_samples()[0]
            store.save_validation(s["id"], {"id": s["id"], "terminal": True, "status": "complete", "net_return_pct": 2})
            evaluate_stored(store, AS_OF+86400_000)
            self.assertEqual(store.validation_results()[0]["net_return_pct"], 2)


class AlertTests(unittest.TestCase):
    def test_no_paid_data_required_and_same_event_sent_only_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = HistoryStore(Path(d)/"r.db")
            cfg = load_config()
            cfg["alerts"]["telegram_enabled"] = True
            a = snapshot(cfg=cfg)
            track_publication(store, a)
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return b'{"ok":true}'
            with patch.dict("os.environ", {"RESEARCH_TELEGRAM_BOT_TOKEN": "fixture", "RESEARCH_TELEGRAM_CHAT_ID": "test"}), patch("app.research.continuous.notify.urlopen", return_value=Response()) as send:
                publish_alerts(a, store, cfg)
                publish_alerts(a, store, cfg)
                self.assertEqual(send.call_count, 1)
                b = snapshot(AS_OF+1000, cfg=cfg, stage="invalidated")
                track_publication(store, b)
                publish_alerts(b, store, cfg)
                self.assertEqual(send.call_count, 2, "invalidation must not be hidden by six-hour cooldown")


if __name__ == "__main__":
    unittest.main()
