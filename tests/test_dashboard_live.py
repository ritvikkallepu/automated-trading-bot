from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from http import HTTPStatus
from unittest.mock import MagicMock, patch
from decimal import Decimal

from app.config import Settings
from app.dashboard.server import DashboardRequestHandler

class MockServer:
    def __init__(self, settings: Settings):
        from app.dashboard.server import DashboardDefaults
        self.settings = settings
        self.defaults = DashboardDefaults()
        self.lock = MagicMock()
        self.active_paper_loop = None

class TestDashboardLive(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            coindcx_api_key="secret_key_123",
            coindcx_api_secret="secret_secret_456"
        )
        self.server = MockServer(self.settings)
        
        # Bypass BaseHTTPRequestHandler init
        init_patch = patch.object(DashboardRequestHandler, "__init__", lambda s, req, client_addr, server: None)
        init_patch.start()
        self.addCleanup(init_patch.stop)
        self.handler = DashboardRequestHandler(MagicMock(), ("127.0.0.1", 8000), self.server)
        self.handler.server = self.server
        self.handler.path = ""
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = MagicMock()
        self.handler.wfile.write = MagicMock()
        
        # We will use a temporary directory for live-state files to not pollute the real ones.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patcher = patch("pathlib.Path")
        self.mock_path = self.patcher.start()
        
        def fake_path(p, *args):
            text = str(p).replace("/", "\\")
            if text.endswith("data\\live_state"):
                return Path(self.temp_dir.name) / "live_state"
            if "live_state.json" in text:
                return Path(self.temp_dir.name) / "live_state.json"
            return Path(p, *args)
        
        self.mock_path.side_effect = fake_path

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_live_state_endpoint_empty(self):
        self.handler.path = "/api/live-state"
        self.handler.do_GET()
        
        self.handler.send_response.assert_called_with(HTTPStatus.OK)
        # Should return {} if empty
        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(payload, {})

    def test_live_state_endpoint_with_data_and_decimal(self):
        state_file = Path(self.temp_dir.name) / "live_state.json"
        
        # Write some state including a dry run position and decimal-like strings
        dummy_state = {
            "positions": {
                "B-BTC_USDT": {
                    "source": "dry_run",
                    "direction": "long",
                    "quantity": "1.5",
                    "entry_price": "50000.5"
                }
            },
            "kill_switch_active": True
        }
        state_file.write_text(json.dumps(dummy_state))
        
        self.handler.path = "/api/live-state"
        self.handler.do_GET()
        
        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertTrue(payload["kill_switch_active"])
        self.assertEqual(payload["positions"]["B-BTC_USDT"]["source"], "dry_run")

    def test_live_state_endpoint_aggregates_multiple_run_files(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "sto.json").write_text(json.dumps({
            "run_id": "sto-run",
            "pair": "B-STO_USDT",
            "watchlist": ["B-STO_USDT"],
            "strategy": "fib_ma_pullback",
            "interval": "1h",
            "execution_interval": "5m",
            "running": True,
            "last_updated": "2026-06-05T22:02:00+00:00",
            "recent_diagnostics": [{"pair": "B-STO_USDT", "reason": "fib blocked"}],
        }))
        (state_dir / "nfp.json").write_text(json.dumps({
            "run_id": "nfp-run",
            "pair": "B-NFP_USDT",
            "watchlist": ["B-NFP_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-05T22:03:00+00:00",
            "recent_diagnostics": [{"pair": "B-NFP_USDT", "reason": "volume blocked"}],
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(payload["active_runs"], 2)
        self.assertEqual(set(payload["watchlist"]), {"B-STO_USDT", "B-NFP_USDT"})
        self.assertEqual(payload["strategy"], "mixed")
        self.assertEqual(payload["interval"], "mixed")
        self.assertEqual(len(payload["recent_diagnostics"]), 2)

    def test_live_state_endpoint_ignores_stale_stopped_runs_when_active_run_exists(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "pair": "B-BSB_USDT, B-XAN_USDT",
            "watchlist": ["B-BSB_USDT", "B-XAN_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T06:10:00+00:00",
            "recent_diagnostics": [{"pair": "B-BSB_USDT", "reason": "live blocked"}],
            "scanned_pairs": {
                "B-BSB_USDT": {"pair": "B-BSB_USDT"},
                "B-XAN_USDT": {"pair": "B-XAN_USDT"},
            },
        }))
        (state_dir / "stale-nfp.json").write_text(json.dumps({
            "run_id": "stale-run",
            "pair": "B-NFP_USDT",
            "watchlist": ["B-NFP_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": False,
            "last_updated": "2026-06-06T21:15:55+00:00",
            "recent_diagnostics": [{"pair": "B-NFP_USDT", "reason": "old signal"}],
            "scanned_pairs": {"B-NFP_USDT": {"pair": "B-NFP_USDT"}},
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(payload["active_runs"], 1)
        self.assertEqual(set(payload["watchlist"]), {"B-BSB_USDT", "B-XAN_USDT"})
        self.assertEqual(set(payload["scanned_pairs"]), {"B-BSB_USDT", "B-XAN_USDT"})
        self.assertEqual(payload["recent_diagnostics"], [{"pair": "B-BSB_USDT", "reason": "live blocked"}])
        self.assertEqual(len(payload["runs"]), 2)
        stale_run = next(run for run in payload["runs"] if run["run_id"] == "stale-run")
        self.assertFalse(stale_run["included_in_live_view"])

    def test_live_state_endpoint_ignores_stale_running_files(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "pair": "B-BSB_USDT, B-XAN_USDT",
            "watchlist": ["B-BSB_USDT", "B-XAN_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T06:10:00+00:00",
            "recent_diagnostics": [{"pair": "B-BSB_USDT", "reason": "current signal"}],
            "scanned_pairs": {
                "B-BSB_USDT": {"pair": "B-BSB_USDT"},
                "B-XAN_USDT": {"pair": "B-XAN_USDT"},
            },
        }))
        (state_dir / "stale-running.json").write_text(json.dumps({
            "run_id": "old-nfp-run",
            "pair": "B-NFP_USDT",
            "watchlist": ["B-NFP_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-06T21:15:55+00:00",
            "recent_diagnostics": [{"pair": "B-NFP_USDT", "reason": "old signal"}],
            "scanned_pairs": {"B-NFP_USDT": {"pair": "B-NFP_USDT"}},
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(payload["active_runs"], 1)
        self.assertEqual(set(payload["watchlist"]), {"B-BSB_USDT", "B-XAN_USDT"})
        self.assertEqual(set(payload["scanned_pairs"]), {"B-BSB_USDT", "B-XAN_USDT"})
        self.assertEqual(payload["recent_diagnostics"], [{"pair": "B-BSB_USDT", "reason": "current signal"}])
        stale_run = next(run for run in payload["runs"] if run["run_id"] == "old-nfp-run")
        self.assertTrue(stale_run["running"])
        self.assertFalse(stale_run["included_in_live_view"])

    def test_live_state_endpoint_filters_old_diagnostics_inside_active_run(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "pair": "B-BSB_USDT, B-NFP_USDT",
            "watchlist": ["B-BSB_USDT", "B-NFP_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T08:10:00+00:00",
            "session_started_at": "2026-06-07T08:00:00+00:00",
            "recent_diagnostics": [
                {
                    "time": "2026-06-07T02:30:02+00:00",
                    "pair": "B-NFP_USDT",
                    "reason": "old stale signal",
                },
                {
                    "time": "2026-06-07T08:09:58+00:00",
                    "pair": "B-BSB_USDT",
                    "reason": "fresh signal",
                },
            ],
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(
            payload["recent_diagnostics"],
            [{
                "time": "2026-06-07T08:09:58+00:00",
                "pair": "B-BSB_USDT",
                "reason": "fresh signal",
            }],
        )

    def test_live_state_endpoint_keeps_full_session_diagnostics_sorted_newest_first(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "watchlist": ["B-BSB_USDT", "B-NFP_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T12:00:00+00:00",
            "session_started_at": "2026-06-07T06:00:00+00:00",
            "recent_diagnostics": [
                {
                    "time": "2026-06-07T10:00:00+00:00",
                    "pair": "B-BSB_USDT",
                    "reason": "middle",
                },
                {
                    "time": "2026-06-07T05:59:59+00:00",
                    "pair": "B-NFP_USDT",
                    "reason": "before session",
                },
                {
                    "time": "2026-06-07T07:00:00+00:00",
                    "pair": "B-NFP_USDT",
                    "reason": "old but in session",
                },
                {
                    "time": "2026-06-07T11:00:00+00:00",
                    "pair": "B-XAN_USDT",
                    "reason": "newest",
                },
            ],
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(
            [row["reason"] for row in payload["recent_diagnostics"]],
            ["newest", "middle", "old but in session"],
        )

    def test_live_state_endpoint_sorts_diagnostics_by_candle_time_when_time_is_short(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "watchlist": ["B-BSB_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T12:00:00+00:00",
            "recent_diagnostics": [
                {
                    "time": "12:05:00",
                    "candle_time": "2026-06-07T06:35:00+00:00",
                    "pair": "B-BSB_USDT",
                    "reason": "older short time",
                },
                {
                    "time": "12:10:00",
                    "candle_time": "2026-06-07T06:40:00+00:00",
                    "pair": "B-BSB_USDT",
                    "reason": "newer short time",
                },
            ],
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(
            [row["reason"] for row in payload["recent_diagnostics"]],
            ["newer short time", "older short time"],
        )

    def test_live_state_endpoint_drops_undated_short_time_rows_after_session_start(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "watchlist": ["B-BSB_USDT"],
            "strategy": "fib_ma_pullback",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T20:35:00+00:00",
            "session_started_at": "2026-06-07T20:30:00+00:00",
            "recent_diagnostics": [
                {
                    "time": "20:25:00",
                    "pair": "B-BSB_USDT",
                    "reason": "old short-time-only row",
                },
                {
                    "time": "20:34:59",
                    "timestamp_ms": 1780864499000,
                    "pair": "B-BSB_USDT",
                    "reason": "fresh timestamp row",
                },
            ],
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(
            [row["reason"] for row in payload["recent_diagnostics"]],
            ["fresh timestamp row"],
        )

    def test_live_state_endpoint_filters_closed_or_stale_position_rows(self):
        state_dir = Path(self.temp_dir.name) / "live_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "active.json").write_text(json.dumps({
            "run_id": "active-run",
            "watchlist": ["B-BSB_USDT"],
            "strategy": "hybrid_meta_v2",
            "interval": "5m",
            "execution_interval": "1m",
            "running": True,
            "last_updated": "2026-06-07T12:00:00+00:00",
            "positions": {
                "B-LOCAL_USDT": {"active_pos": "0", "status": "closed"},
                "B-BSB_USDT": {"active_pos": "2", "status": "open"},
            },
            "portfolio_positions": {
                "B-XAN_USDT": {"active_pos": "0", "status": "closed"},
                "B-OPN_USDT": {"active_pos": "10", "status": "open"},
            },
        }))

        self.handler.path = "/api/live-state"
        self.handler.do_GET()

        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        self.assertEqual(set(payload["positions"]), {"B-BSB_USDT"})
        self.assertEqual(set(payload["portfolio_positions"]), {"B-OPN_USDT"})

    def test_status_endpoint_redacts_secrets(self):
        self.handler.path = "/api/status"
        self.handler.do_GET()
        
        written = b"".join([call.args[0] for call in self.handler.wfile.write.call_args_list])
        payload = json.loads(written.decode())
        
        # Verify secret_key_123 is NOT in the payload
        payload_str = json.dumps(payload)
        self.assertNotIn("secret_key_123", payload_str)
        self.assertNotIn("secret_secret_456", payload_str)

    def test_pairs_endpoint_uses_full_pair_catalog(self):
        self.handler.path = "/api/pairs"
        self.handler._send_pairs = MagicMock()
        self.handler._get_live_state_safe = MagicMock(
            return_value={"scanned_pairs": {"B-STO_USDT": {"pair": "B-STO_USDT"}}}
        )

        self.handler.do_GET()

        self.handler._send_pairs.assert_called_once_with()
        self.handler._get_live_state_safe.assert_not_called()

    def test_kill_switch_enable(self):
        self.handler.path = "/api/kill-switch/enable"
        self.handler.do_POST()
        
        state_file = Path(self.temp_dir.name) / "live_state.json"
        self.assertTrue(state_file.exists())
        
        state = json.loads(state_file.read_text())
        self.assertTrue(state["kill_switch_active"])

    def test_kill_switch_disable_requires_confirmation(self):
        self.handler.path = "/api/kill-switch/disable"
        
        # Missing confirmation
        self.handler._read_json_body = MagicMock(return_value={})
        self.handler.do_POST()
        self.handler.send_response.assert_called_with(HTTPStatus.BAD_REQUEST)
        
        # Wrong confirmation
        self.handler._read_json_body = MagicMock(return_value={"confirm": "yes"})
        self.handler.do_POST()
        self.handler.send_response.assert_called_with(HTTPStatus.BAD_REQUEST)
        
        # Correct confirmation
        self.handler.send_response.reset_mock()
        self.handler._read_json_body = MagicMock(return_value={"confirm": "DISABLE"})
        self.handler.do_POST()
        
        state_file = Path(self.temp_dir.name) / "live_state.json"
        state = json.loads(state_file.read_text())
        self.assertFalse(state["kill_switch_active"])
        self.handler.send_response.assert_called_with(HTTPStatus.OK)

if __name__ == "__main__":
    unittest.main()
