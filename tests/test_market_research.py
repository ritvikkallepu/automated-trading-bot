from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.dashboard.server import DashboardHTTPServer
from app.dashboard.state import DashboardDefaults
from app.data.candle_builder import interval_to_ms
from app.exchange.coindcx_rest import HTTPResponse
from app.research.market_scanner import (
    DataUnavailable, MarketScanner, PublicMarketData, RateLimited, ScanConfig,
    ScanStopped, book_features, candle_features, match_universe, rank_candidate,
)
from app.research.scan_service import ResearchScanService
from app.research.outcome_tracker import (
    HORIZONS_MS, ResearchOutcomeService, build_summary, evaluate_snapshot,
)


AS_OF = 1789171200000 + 60_000  # 2026-09-12 00:01 UTC; aligned fixture only.


def klines(interval="5m", sign=1):
    step = interval_to_ms(interval)
    end = AS_OF // step * step
    rows = []
    for i in range(100):
        close = 100 + sign * i * 0.02
        opened = end - (100 - i) * step
        rows.append([opened, close-sign*0.01, close+0.9, close-0.9, close, 100,
                     opened+step-1, 10000, 5, 50, 5000, "0"])
    return rows


def book(mid=100):
    return {"ts": AS_OF, "bids": {str(mid-0.01): "1000"}, "asks": {str(mid+0.01): "1000"}}


class FakeData:
    def binance(self, path, **params):
        if path.endswith("/time"):
            return {"serverTime": AS_OF}
        if path.endswith("exchangeInfo"):
            return {"symbols": [{"symbol": base+"USDT", "baseAsset": base, "quoteAsset": "USDT",
                                  "marginAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"}
                                 for base in ("BTC", "ETH")]}
        if path.endswith("ticker/24hr"):
            return [{"symbol": base+"USDT", "quoteVolume": 50_000_000, "closeTime": AS_OF} for base in ("BTC", "ETH")]
        if path.endswith("klines"):
            return klines(params["interval"], -1 if params["symbol"] == "ETHUSDT" else 1)
        if path.endswith("premiumIndex"):
            return [{"symbol": base+"USDT", "lastFundingRate": 0.0001, "time": AS_OF} for base in ("BTC", "ETH")]
        if path.endswith("fundingInfo"):
            return []
        if path.endswith("openInterestHist"):
            end = params["endTime"]
            return [{"timestamp": end-3600000, "sumOpenInterest": 100}, {"timestamp": end, "sumOpenInterest": 101}]
        if path.endswith("depth"):
            return book()
        raise AssertionError(path)

    def dcx_pairs(self):
        return ["B-ETH_USDT", "B-BTC_USDT", "B-UNKNOWN_USDT"]

    def dcx_book(self, pair):
        return book()


class OutcomeData:
    def __init__(self, now_ms, slopes=None):
        self.now_ms = now_ms
        self.slopes = slopes or {"BTCUSDT": 0.10, "ETHUSDT": -0.10, "XRPUSDT": 0.02}
        self.requests = []

    def binance(self, path, **params):
        self.requests.append((path, params))
        if path.endswith("/time"):
            return {"serverTime": self.now_ms}
        if path.endswith("/klines"):
            start = params["startTime"]
            count = min(params["limit"], (params["endTime"] - start) // 60_000)
            slope = self.slopes[params["symbol"]]
            rows = []
            for index in range(count):
                opened = start + index * 60_000
                op = 100 + slope * index
                close = op + slope
                rows.append([opened, op, max(op, close)+0.05, min(op, close)-0.05, close,
                             100, opened+59_999, 10000, 5, 50, 5000, "0"])
            return rows
        raise AssertionError(path)


def outcome_snapshot(completed_ms):
    return {
        "schema_version": 2, "scan_id": "a" * 32, "mode": "research_only",
        "completed_ms": completed_ms, "warnings": [],
        "config": {"round_trip_cost_pct": 0.20},
        "rows": [
            {"pair": "B-BTC_USDT", "symbol": "BTCUSDT", "direction": "long", "bucket": "long",
             "score": 80, "execution_check": {"status": "verified"}},
            {"pair": "B-ETH_USDT", "symbol": "ETHUSDT", "direction": "short", "bucket": "short",
             "score": 78, "execution_check": {"status": "unverified"}},
            {"pair": "B-XRP_USDT", "symbol": "XRPUSDT", "direction": "long", "bucket": "avoid",
             "score": 55, "execution_check": {"status": "blocked"}},
        ],
    }


class ResearchTests(unittest.TestCase):
    def test_config_accepts_only_bounded_finite_values(self):
        for value in (float("nan"), float("inf"), -1, True, 51, 1.5):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                ScanConfig.parse({"max_pairs": value})
        with self.assertRaises(ValueError):
            ScanConfig.parse({"leverage": 30})
        self.assertEqual(ScanConfig.parse({"min_volume_ratio": 0.6}).min_volume_ratio, 0.6)

    def test_matching_excludes_other_contracts_and_never_guesses_multiplier(self):
        exchange = FakeData().binance("exchangeInfo")
        exchange["symbols"].extend([
            {**exchange["symbols"][0], "baseAsset": "PEPE", "symbol": "1000PEPEUSDT"},
            {**exchange["symbols"][0], "baseAsset": "SOL", "symbol": "SOLUSDT", "contractType": "CURRENT_QUARTER"},
            {**exchange["symbols"][0], "baseAsset": "XRP", "symbol": "XRPUSDT", "marginAsset": "XRP"},
        ])
        matched, excluded = match_universe(exchange, ["B-BTC_USDT", "B-PEPE_USDT", "B-SOL_USDT", "B-XRP_USDT"])
        self.assertEqual([r["pair"] for r in matched], ["B-BTC_USDT"])
        self.assertEqual(len(excluded), 3)

    def test_duplicate_mapping_is_not_accepted(self):
        ex = FakeData().binance("exchangeInfo")
        ex["symbols"].append(ex["symbols"][0])
        matched, _ = match_universe(ex, ["B-BTC_USDT"])
        self.assertFalse(matched)

    def test_closed_candles_only_and_baseline_excludes_current(self):
        rows = klines()
        rows[-1][5] = 300
        forming = deepcopy(rows[-1])
        forming[0] += 300000
        forming[6] += 300000
        forming[5] = 1
        result = candle_features(rows+[forming], "BTCUSDT", "5m", AS_OF)
        self.assertEqual(result["volume_ratio"], 3)
        self.assertEqual(result["close_time_ms"], rows[-1][6])

    def test_unsorted_and_identical_duplicate_candles_are_deterministic(self):
        rows = klines()
        self.assertEqual(candle_features(rows, "BTC", "5m", AS_OF),
                         candle_features(list(reversed(rows))+[rows[-1]], "BTC", "5m", AS_OF))

    def test_bad_candle_histories_fail_closed(self):
        for kind in ("gap", "short", "stale", "nan", "bad_boundary", "conflict"):
            rows = klines()
            if kind == "gap": del rows[50]
            if kind == "short": rows = rows[-20:]
            if kind == "stale": rows = rows[:-2]
            if kind == "nan": rows[-1][4] = "NaN"
            if kind == "bad_boundary": rows[-1][6] -= 1
            if kind == "conflict": rows.append(deepcopy(rows[-1])); rows[-1][5] = 555
            with self.subTest(kind=kind), self.assertRaises(DataUnavailable):
                candle_features(rows, "BTC", "5m", AS_OF)

    def test_long_short_symmetry(self):
        outputs = []
        for sign in (1, -1):
            features = {tf: candle_features(klines(tf, sign), "BTC", tf, AS_OF) for tf in ("5m", "15m", "1h", "4h")}
            outputs.append(rank_candidate(features, 0, 1, ScanConfig()))
        self.assertEqual([r["bucket"] for r in outputs], ["long", "short"])
        self.assertEqual(outputs[0]["score"], outputs[1]["score"])

    def test_chase_and_countertrend_rebounds_are_avoided(self):
        features = {tf: candle_features(klines(tf), "BTC", tf, AS_OF) for tf in ("5m", "15m", "1h", "4h")}
        features["15m"]["extension_atr"] = 4
        self.assertIn("Late chase", " ".join(rank_candidate(features, 0, 1, ScanConfig())["reasons"]))
        features["15m"]["extension_atr"] = 0
        features["4h"]["trend"] = -1
        self.assertEqual(rank_candidate(features, 0, 1, ScanConfig())["bucket"], "avoid")

    def test_oi_is_context_not_a_directional_bonus(self):
        features = {tf: candle_features(klines(tf), "BTC", tf, AS_OF) for tf in ("5m", "15m", "1h", "4h")}
        falling = rank_candidate(features, 0, -5, ScanConfig())
        rising = rank_candidate(features, 0, 5, ScanConfig())
        self.assertEqual(falling["score"], rising["score"])
        self.assertIn("short covering", falling["oi_context"])

    def test_funding_penalty_is_directional(self):
        features = {tf: candle_features(klines(tf), "BTC", tf, AS_OF) for tf in ("5m", "15m", "1h", "4h")}
        result = rank_candidate(features, 0.1, 1, ScanConfig())
        self.assertEqual(result["breakdown"]["long"]["funding_penalty"], -10)
        self.assertEqual(result["breakdown"]["short"]["funding_penalty"], 0)

    def test_book_validation_and_units(self):
        result = book_features(book(), AS_OF)
        self.assertAlmostEqual(result["spread_bps"], 2)
        self.assertAlmostEqual(result["ask_depth_usdt"], 100010)
        for bad in ({**book(), "ts": AS_OF-31000}, {**book(), "bids": []}, {**book(), "ts": AS_OF+10000},
                    {"bids": {"101": "5"}, "asks": {"100": "2"}, "ts": AS_OF}):
            with self.assertRaises(DataUnavailable): book_features(bad, AS_OF)

    def test_full_scan_is_ordered_and_pair_isolated(self):
        result = MarketScanner(FakeData()).scan(ScanConfig())
        self.assertEqual([(r["pair"], r["bucket"]) for r in result["rows"]], [("B-BTC_USDT", "long"), ("B-ETH_USDT", "short")])
        self.assertFalse(result["ai_enabled"])
        self.assertEqual(result["scanned_count"], 2)
        self.assertEqual(result["coindcx_catalogue_status"], "available")
        self.assertTrue(all(r["execution_check"]["status"] == "verified" for r in result["rows"]))
        self.assertIsNone(result["rows"][0]["funding_interval_hours"])
        json.dumps(result, allow_nan=False)

    def test_bad_book_only_blocks_its_own_pair(self):
        data = FakeData()
        data.dcx_book = lambda pair: book(105 if pair == "B-BTC_USDT" else 100)
        rows = MarketScanner(data).scan(ScanConfig())["rows"]
        self.assertEqual([r["bucket"] for r in rows], ["long", "short"])
        self.assertEqual([r["execution_check"]["status"] for r in rows], ["blocked", "verified"])

    def test_missing_funding_never_produces_approved_candidate(self):
        data = FakeData()
        original = data.binance
        data.binance = lambda path, **kw: [] if path.endswith("premiumIndex") else original(path, **kw)
        rows = MarketScanner(data).scan(ScanConfig())["rows"]
        self.assertTrue(all(r["bucket"] == "avoid" and r["funding_pct"] is None for r in rows))

    def test_missing_catalogue_keeps_binance_ranking_but_never_verifies_execution(self):
        data = FakeData()
        data.dcx_pairs = Mock(side_effect=DataUnavailable("HTTP 403"))
        data.dcx_book = Mock()
        result = MarketScanner(data).scan(ScanConfig())
        self.assertEqual(result["coindcx_catalogue_status"], "unavailable")
        self.assertEqual([r["bucket"] for r in result["rows"]], ["long", "short"])
        self.assertTrue(all(r["execution_check"]["status"] == "unverified" for r in result["rows"]))
        self.assertIn("HTTP 403", result["warnings"][0])
        data.dcx_book.assert_not_called()

    def test_book_failure_does_not_erase_binance_ranking(self):
        data = FakeData()
        data.dcx_book = Mock(side_effect=DataUnavailable("HTTP 403"))
        rows = MarketScanner(data).scan(ScanConfig())["rows"]
        self.assertEqual([r["bucket"] for r in rows], ["long", "short"])
        self.assertTrue(all(r["execution_check"]["status"] == "unverified" for r in rows))

    def test_pair_limit_and_exclusions(self):
        result = MarketScanner(FakeData()).scan(ScanConfig(max_pairs=1))
        self.assertEqual(result["scanned_count"], 1)
        self.assertEqual(len(result["excluded"]), 2)

    def test_transport_is_get_only_and_has_no_auth_or_retries(self):
        transport = Mock()
        transport.request.return_value = HTTPResponse(429, '{}', {})
        client = PublicMarketData(transport=transport)
        with self.assertRaises(RateLimited): client.binance("/fapi/v1/time")
        transport.request.assert_called_once()
        self.assertEqual(transport.request.call_args.args[0], "GET")
        self.assertEqual(transport.request.call_args.args[2], {"Accept": "application/json", "User-Agent": "CoinDCXResearch/1.0 (public-market-data)"})
        with self.assertRaises(ValueError): client.binance("/fapi/v1/order")
        with self.assertRaises(ValueError): client.dcx_book("../secrets")

    def test_cancel_and_deadline_prevent_network(self):
        for client in (PublicMarketData(cancelled=lambda: True), PublicMarketData(budget_seconds=-1)):
            client.transport = Mock()
            with self.assertRaises(ScanStopped): client.binance("/fapi/v1/time")
            client.transport.request.assert_not_called()


class ResearchServiceTests(unittest.TestCase):
    def test_completed_snapshot_persisted_and_restart_not_presented_current(self):
        with TemporaryDirectory() as tmp:
            service = ResearchScanService(Path(tmp), scanner_factory=lambda: MarketScanner(FakeData()))
            service.start(ScanConfig())
            service._thread.join(5)
            state = service.status()
            self.assertEqual(state["status"], "complete")
            paths = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text())["scan_id"], state["snapshot"]["scan_id"])
            self.assertIsNone(ResearchScanService(Path(tmp)).status()["snapshot"])
            with self.assertRaises(ValueError): service.start(ScanConfig())
            service.close()

    def test_errors_preserve_previous_but_never_label_it_new(self):
        with TemporaryDirectory() as tmp:
            service = ResearchScanService(Path(tmp), scanner_factory=lambda: Mock(scan=Mock(side_effect=DataUnavailable("offline"))))
            service._state["snapshot"] = {"as_of_ms": 0, "scan_id": "previous"}
            service.start(ScanConfig())
            service._thread.join(5)
            state = service.status()
            self.assertEqual(state["status"], "error")
            self.assertTrue(state["stale"])
            self.assertEqual(state["snapshot"]["scan_id"], "previous")
            service.close()

    def test_one_worker_and_cancel(self):
        entered, release = threading.Event(), threading.Event()
        def scan(*_): entered.set(); release.wait(3); return {}
        service = ResearchScanService(scanner_factory=lambda: Mock(scan=scan))
        service.start(ScanConfig())
        entered.wait(2)
        with self.assertRaises(ValueError): service.start(ScanConfig())
        service.cancel(); release.set(); service.close()
        self.assertEqual(service.status()["status"], "cancelled")

    def test_http_routes_are_read_only_and_nonblocking(self):
        server = DashboardHTTPServer(("127.0.0.1", 0), settings=Settings(), defaults=DashboardDefaults(), outcome_autostart=False)
        server.research = Mock()
        server.research.status.return_value = {"status": "running"}
        server.research_outcomes = Mock()
        server.research_outcomes.status.return_value = {"status": "idle", "summary": {}}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("GET", "/api/research")
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read())["defaults"], asdict(ScanConfig()))
            conn.request("GET", "/api/research/outcomes")
            self.assertEqual(json.loads(conn.getresponse().read())["status"], "idle")
            conn.request("POST", "/api/research", body='{"max_pairs":2}', headers={"Content-Type":"application/json"})
            response = conn.getresponse(); response.read()
            self.assertEqual(response.status, 202)
            server.research.start.assert_called_once_with(replace(ScanConfig(), max_pairs=2))
            conn.request("POST", "/api/research", body='{"min_score":"NaN"}')
            response = conn.getresponse(); response.read()
            self.assertEqual(response.status, 400)
            conn.request("POST", "/api/research/outcomes/refresh")
            response = conn.getresponse(); response.read()
            self.assertEqual(response.status, 202)
            server.research_outcomes.request_refresh.assert_called_once()
            conn.request("GET", "/api/health")
            self.assertEqual(conn.getresponse().read(), b'{\n  "ok": true\n}')
            for asset in ("research.js", "research.css"):
                conn.request("GET", "/static/"+asset)
                response = conn.getresponse(); response.read()
                self.assertEqual(response.status, 200)
            conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


class OutcomeTrackerTests(unittest.TestCase):
    def test_forward_evaluation_uses_next_minute_and_all_horizons(self):
        completed = AS_OF - 5 * 60 * 60 * 1000
        snapshot = outcome_snapshot(completed)
        original = deepcopy(snapshot)
        entry = ((completed // 60_000) + 1) * 60_000
        data = OutcomeData(entry + HORIZONS_MS["4h"] + 60_000)
        outcome = evaluate_snapshot(snapshot, data)
        self.assertEqual(snapshot, original)
        self.assertEqual(set(outcome["records"]["B-BTC_USDT"]), {"15m", "1h", "4h"})
        btc = outcome["records"]["B-BTC_USDT"]["15m"]
        eth = outcome["records"]["B-ETH_USDT"]["15m"]
        self.assertEqual(btc["entry_time_ms"], entry)
        self.assertAlmostEqual(btc["gross_directional_return_pct"], 1.5)
        self.assertAlmostEqual(btc["net_directional_return_pct"], 1.3)
        self.assertAlmostEqual(eth["gross_directional_return_pct"], 1.5)
        self.assertTrue(btc["hit_after_costs"] and eth["hit_after_costs"])
        kline_calls = [params for path, params in data.requests if path.endswith("/klines")]
        self.assertTrue(all(params["startTime"] == entry for params in kline_calls))

    def test_only_mature_horizons_are_evaluated(self):
        completed = AS_OF - 30 * 60 * 1000
        entry = ((completed // 60_000) + 1) * 60_000
        outcome = evaluate_snapshot(outcome_snapshot(completed), OutcomeData(entry + 20 * 60_000))
        self.assertEqual(set(outcome["records"]["B-BTC_USDT"]), {"15m"})

    def test_existing_outcomes_are_immutable_and_not_recomputed(self):
        completed = AS_OF - 5 * 60 * 60 * 1000
        entry = ((completed // 60_000) + 1) * 60_000
        first = evaluate_snapshot(outcome_snapshot(completed), OutcomeData(entry + HORIZONS_MS["4h"] + 60_000))
        first_value = first["records"]["B-BTC_USDT"]["15m"]
        changed_data = OutcomeData(entry + HORIZONS_MS["4h"] + 120_000,
                                   {"BTCUSDT": -1, "ETHUSDT": 1, "XRPUSDT": -1})
        second = evaluate_snapshot(outcome_snapshot(completed), changed_data, first)
        self.assertEqual(second["records"]["B-BTC_USDT"]["15m"], first_value)
        self.assertFalse(any(path.endswith("/klines") for path, _ in changed_data.requests))

    def test_gap_and_synthetic_snapshot_fail_closed(self):
        completed = AS_OF - 60 * 60 * 1000
        entry = ((completed // 60_000) + 1) * 60_000
        data = OutcomeData(entry + HORIZONS_MS["1h"] + 60_000)
        original = data.binance
        def missing(path, **params):
            rows = original(path, **params)
            return rows[:5] + rows[6:] if path.endswith("/klines") else rows
        data.binance = missing
        result = evaluate_snapshot(outcome_snapshot(completed), data)
        self.assertIn("incomplete", result["errors"]["B-BTC_USDT"])
        synthetic = outcome_snapshot(completed)
        synthetic["warnings"] = ["SYNTHETIC TEST FIXTURE"]
        with self.assertRaisesRegex(DataUnavailable, "Synthetic"):
            evaluate_snapshot(synthetic, OutcomeData(entry + HORIZONS_MS["1h"] + 60_000))

    def test_summary_separates_candidates_sides_verified_and_baseline(self):
        completed = AS_OF - 5 * 60 * 60 * 1000
        entry = ((completed // 60_000) + 1) * 60_000
        outcome = evaluate_snapshot(outcome_snapshot(completed), OutcomeData(entry + HORIZONS_MS["4h"] + 60_000))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcome.json"
            path.write_text(json.dumps(outcome), encoding="utf-8")
            summary = build_summary(Path(tmp))
        metrics = summary["by_horizon"]["15m"]
        self.assertEqual(summary["candidate_signals"], 2)
        self.assertEqual(metrics["candidates"]["observations"], 2)
        self.assertEqual(metrics["longs"]["observations"], 1)
        self.assertEqual(metrics["shorts"]["observations"], 1)
        self.assertEqual(metrics["verified"]["observations"], 1)
        self.assertEqual(metrics["all_scored_baseline"]["observations"], 3)
        self.assertEqual(metrics["avoided"]["observations"], 1)

    def test_service_writes_sidecar_and_skips_when_nothing_new_is_due(self):
        completed = AS_OF - 5 * 60 * 60 * 1000
        entry = ((completed // 60_000) + 1) * 60_000
        with TemporaryDirectory() as tmp:
            scans, outcomes = Path(tmp) / "scans", Path(tmp) / "outcomes"
            scans.mkdir()
            (scans / ("a" * 32 + ".json")).write_text(json.dumps(outcome_snapshot(completed)), encoding="utf-8")
            factory = Mock(side_effect=lambda: OutcomeData(entry + HORIZONS_MS["4h"] + 60_000))
            service = ResearchOutcomeService(scans, outcomes, data_factory=factory, autostart=False)
            first = service.refresh_now()
            second = service.refresh_now()
            self.assertEqual(first["summary"]["candidate_signals"], 2)
            self.assertEqual(second["progress"], "Evaluated 0 due snapshot(s)")
            self.assertEqual(factory.call_count, 1)
            self.assertTrue((outcomes / ("a" * 32 + ".json")).exists())
            service.close()

    def test_service_processes_backlog_in_chronological_order(self):
        newer_completed = AS_OF - 5 * 60 * 60 * 1000
        older_completed = newer_completed - 60_000
        older = outcome_snapshot(older_completed)
        older["scan_id"] = "b" * 32
        newer = outcome_snapshot(newer_completed)
        with TemporaryDirectory() as tmp:
            scans, outcomes = Path(tmp) / "scans", Path(tmp) / "outcomes"
            scans.mkdir()
            (scans / ("a" * 32 + ".json")).write_text(json.dumps(newer), encoding="utf-8")
            (scans / ("b" * 32 + ".json")).write_text(json.dumps(older), encoding="utf-8")
            entry = ((newer_completed // 60_000) + 1) * 60_000
            service = ResearchOutcomeService(
                scans,
                outcomes,
                data_factory=lambda: OutcomeData(entry + HORIZONS_MS["4h"] + 60_000),
                max_snapshots_per_cycle=1,
                autostart=False,
            )
            service.refresh_now()
            self.assertTrue((outcomes / ("b" * 32 + ".json")).exists())
            self.assertFalse((outcomes / ("a" * 32 + ".json")).exists())
            service.close()


if __name__ == "__main__":
    unittest.main()
