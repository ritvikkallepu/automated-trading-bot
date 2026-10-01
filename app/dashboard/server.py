from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.backtest.data_loader import (
    REST_RESOLUTION_BY_INTERVAL,
    load_historical_candle_series,
    load_historical_candle_series_between,
)
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.config import Settings, load_settings
from app.dashboard.state import (
    DashboardDefaults,
    build_backtest_dashboard_payload,
    build_status_payload,
)
from app.data.candle_builder import interval_to_ms
from app.data.open_interest import (
    OpenInterestFeatureSeries,
    load_binance_open_interest_proxy,
)
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.errors import CoinDCXError
from app.fees import COINDCX_INR_M_MAKER_FEE_RATE, COINDCX_INR_M_TAKER_FEE_RATE
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata
from app.research.history import record_backtest_result
from app.strategies.defaults import STRATEGY_CHOICES, strategy_engine_for_name
from app.utils.json import to_jsonable
from app.utils.logging import configure_logging


OPEN_INTEREST_STRATEGIES = {"hybrid_meta", "hybrid_meta_v2", "adaptive_hybrid"}


def _dashboard_html() -> str:
    """Load the maintained dashboard page instead of the legacy embedded copy."""

    return (
        files("app.dashboard")
        .joinpath("static")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )


def _normalize_dashboard_pair(value: object) -> str:
    """Accept dashboard display pairs and convert them to CoinDCX internal pairs.

    The searchable dropdown stores values like B-BSB_USDT, but the free-text
    watchlist is easy to fill with display labels like BSB-USDT or B-BSB-USDT.
    REST candles use the internal underscore form.
    """
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return text

    if text.startswith("B-"):
        body = text[2:]
    else:
        body = text

    if "_" in body:
        base, quote = body.split("_", 1)
    elif "-" in body:
        base, quote = body.rsplit("-", 1)
    else:
        return text if text.startswith("B-") else f"B-{text}"

    return f"B-{base}_{quote}"


def _dashboard_pair_list(value: object) -> list[str]:
    if isinstance(value, str):
        raw_values = value.split(",")
    elif isinstance(value, list):
        raw_values = value
    elif value is None:
        raw_values = []
    else:
        raw_values = [value]

    pairs: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        pair = _normalize_dashboard_pair(raw)
        if not pair or pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _read_json_dict(path: Any) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _live_state_paths() -> tuple[list[Any], Any]:
    from pathlib import Path

    state_dir = Path("data/live_state")
    legacy_path = Path("data/live_state.json")
    run_paths: list[Any] = []
    try:
        if state_dir.exists():
            run_paths = sorted(
                [path for path in state_dir.glob("*.json") if path.is_file()],
                key=lambda path: str(path),
            )
    except Exception:
        run_paths = []
    return run_paths, legacy_path


def _state_pair_list(state: dict[str, Any]) -> list[str]:
    watchlist = state.get("watchlist")
    if isinstance(watchlist, list):
        raw_values = watchlist
    elif isinstance(watchlist, str):
        raw_values = watchlist.split(",")
    else:
        raw_values = [state.get("pair")]

    pairs: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        pair = str(raw or "").strip()
        if not pair or pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _mixed_or_single(values: list[Any], default: str = "") -> str:
    clean = [str(value) for value in values if value not in (None, "")]
    unique = list(dict.fromkeys(clean))
    if not unique:
        return default
    if len(unique) == 1:
        return unique[0]
    return "mixed"


def _state_updated_key(state: dict[str, Any]) -> str:
    return str(state.get("last_updated") or state.get("updated_at") or "")


_LIVE_STATE_FRESH_WINDOW = timedelta(minutes=15)
_LIVE_DIAGNOSTIC_MAX_ROWS = 5000


def _state_updated_dt(state: dict[str, Any]) -> datetime | None:
    raw_value = _state_updated_key(state).strip()
    return _parse_iso_datetime(raw_value)


def _parse_iso_datetime(raw_value: Any) -> datetime | None:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    if raw_text.endswith("Z"):
        raw_text = f"{raw_text[:-1]}+00:00"
    try:
        updated = datetime.fromisoformat(raw_text)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated.astimezone(timezone.utc)


def _diagnostic_time(row: Any) -> datetime | None:
    if not isinstance(row, dict):
        return None
    for raw_value in (row.get("time"), row.get("candle_time")):
        parsed = _parse_iso_datetime(raw_value)
        if parsed is not None:
            return parsed
    raw_timestamp = row.get("timestamp_ms")
    if raw_timestamp is not None:
        try:
            return datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=timezone.utc)
        except Exception:
            return None
    return None


def _fresh_recent_diagnostics(state: dict[str, Any]) -> list[Any]:
    diagnostics = state.get("recent_diagnostics")
    if not isinstance(diagnostics, list):
        return []

    session_started = _parse_iso_datetime(state.get("session_started_at"))
    if session_started is None:
        return diagnostics

    fresh: list[Any] = []
    for row in diagnostics:
        row_time = _diagnostic_time(row)
        if row_time is not None and row_time >= session_started:
            fresh.append(row)
    return fresh


def _diagnostic_sort_key(row: Any) -> tuple[int, float]:
    row_time = _diagnostic_time(row)
    if row_time is None:
        return (0, 0.0)
    return (1, row_time.timestamp())


def _is_open_position_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    try:
        active_pos = Decimal(str(row.get("active_pos") or row.get("quantity") or "0"))
    except Exception:
        active_pos = Decimal("0")
    status = str(row.get("status") or row.get("state") or "open").lower()
    if status in {"closed", "close", "exited", "settled"}:
        return False
    return active_pos.copy_abs() > 0


def _open_position_rows(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, dict):
        return {}
    return {
        str(pair): row
        for pair, row in rows.items()
        if _is_open_position_row(row)
    }


def _state_sort_key(state: dict[str, Any]) -> tuple[int, float | str]:
    updated = _state_updated_dt(state)
    if updated is not None:
        return (1, updated.timestamp())
    return (0, _state_updated_key(state))


def _fresh_running_states(running_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(running_states) <= 1:
        return running_states

    newest_updated = next(
        (_state_updated_dt(state) for state in running_states if _state_updated_dt(state) is not None),
        None,
    )
    if newest_updated is None:
        return running_states[:1]

    fresh: list[dict[str, Any]] = []
    for index, state in enumerate(running_states):
        updated = _state_updated_dt(state)
        if updated is None:
            if index == 0:
                fresh.append(state)
            continue
        if newest_updated - updated <= _LIVE_STATE_FRESH_WINDOW:
            fresh.append(state)
    return fresh or running_states[:1]


def _merge_live_state_dicts(states: list[dict[str, Any]]) -> dict[str, Any]:
    if not states:
        return {}

    ordered = sorted(states, key=_state_sort_key, reverse=True)
    running_states = [state for state in ordered if bool(state.get("running", False))]
    fresh_running_states = _fresh_running_states(running_states)
    merge_pool = fresh_running_states or ordered[:1]
    newest = merge_pool[0]
    aggregate: dict[str, Any] = dict(newest)
    included_state_ids = {id(state) for state in merge_pool}

    combined_pairs: list[str] = []
    for state in merge_pool:
        for pair in _state_pair_list(state):
            if pair not in combined_pairs:
                combined_pairs.append(pair)

    aggregate["runs"] = [
        {
            "run_id": state.get("run_id", ""),
            "pair": ", ".join(_state_pair_list(state)),
            "watchlist": _state_pair_list(state),
            "strategy": state.get("strategy", ""),
            "interval": state.get("interval", ""),
            "execution_interval": state.get("execution_interval", ""),
            "running": bool(state.get("running", False)),
            "last_updated": state.get("last_updated", ""),
            "included_in_live_view": id(state) in included_state_ids,
        }
        for state in ordered
    ]
    included_running_states = [state for state in merge_pool if bool(state.get("running", False))]
    aggregate["active_runs"] = len(included_running_states)
    aggregate["running"] = bool(included_running_states)
    aggregate["pair"] = ", ".join(combined_pairs)
    aggregate["watchlist"] = combined_pairs
    aggregate["strategy"] = _mixed_or_single([state.get("strategy") for state in merge_pool])
    aggregate["interval"] = _mixed_or_single([state.get("interval") for state in merge_pool])
    aggregate["execution_interval"] = _mixed_or_single([state.get("execution_interval") for state in merge_pool])
    aggregate["execution_mode"] = _mixed_or_single([state.get("execution_mode") for state in merge_pool])
    aggregate["kill_switch_active"] = any(bool(state.get("kill_switch_active", False)) for state in merge_pool)

    merged_positions: dict[str, Any] = {}
    merged_portfolio_positions: dict[str, Any] = {}
    merged_scanned: dict[str, Any] = {}
    for state in reversed(merge_pool):
        if isinstance(state.get("positions"), dict):
            merged_positions.update(_open_position_rows(state["positions"]))
        if (
            isinstance(state.get("portfolio_positions"), dict)
            and not bool(state.get("portfolio_positions_stale", False))
        ):
            merged_portfolio_positions.update(_open_position_rows(state["portfolio_positions"]))
        if isinstance(state.get("scanned_pairs"), dict):
            merged_scanned.update(state["scanned_pairs"])
    aggregate["positions"] = merged_positions
    aggregate["portfolio_positions"] = merged_portfolio_positions
    aggregate["scanned_pairs"] = merged_scanned

    for key in ("orders", "closed_trades", "alerts", "recent_diagnostics"):
        combined: list[Any] = []
        for state in merge_pool:
            value = _fresh_recent_diagnostics(state) if key == "recent_diagnostics" else state.get(key)
            if isinstance(value, list):
                combined.extend(value)
        if key == "recent_diagnostics":
            combined.sort(key=_diagnostic_sort_key, reverse=True)
            aggregate[key] = combined[:_LIVE_DIAGNOSTIC_MAX_ROWS]
        else:
            aggregate[key] = combined[:200]

    return aggregate


def _paper_pair_overrides(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        return {}

    allowed = {
        "strategy",
        "leverage",
        "risk_pct",
        "max_daily_loss_pct",
        "max_open_positions",
        "max_margin_usage_pct",
        "allow_multi_pair_positions",
        "allow_same_pair_pyramiding",
        "trailing_stop_enabled",
        "atr_dynamic_exits_enabled",
        "profit_lock_enabled",
        "bb_trail_enabled",
        "max_entries_per_parent_candle",
    }
    overrides: dict[str, dict[str, object]] = {}
    for raw_pair, raw_settings in value.items():
        pair = _normalize_dashboard_pair(raw_pair)
        if not pair or not isinstance(raw_settings, dict):
            continue
        clean: dict[str, object] = {}
        for key in allowed:
            if key not in raw_settings:
                continue
            item = raw_settings[key]
            if item is None or item == "":
                continue
            clean[key] = item
        if clean:
            overrides[pair] = clean
    return overrides


def _paper_runtime_settings(params: dict[str, Any]) -> dict[str, Any]:
    interval = str(
        params.get("strategy_interval")
        or params.get("interval")
        or ""
    ).strip()
    execution_interval = str(params.get("execution_interval") or "").strip()
    intrabar = (
        _bool_param(params.get("paper_intrabar_enabled"), False)
        if "paper_intrabar_enabled" in params
        else None
    )
    partial_parent = (
        _bool_param(params.get("use_partial_parent_candle"), False)
        if "use_partial_parent_candle" in params
        else None
    )
    max_entries = (
        _positive_int_param(
            params.get("max_entries_per_parent_candle"),
            1,
            name="max_entries_per_parent_candle",
        )
        if "max_entries_per_parent_candle" in params
        else None
    )
    leverage = (
        _decimal_param(params.get("leverage"), Decimal("0"))
        if "leverage" in params
        else None
    )
    risk_updates: dict[str, Any] = {}
    if "live_max_order_notional" in params:
        value = _decimal_param(
            params.get("live_max_order_notional"),
            Decimal("0"),
        )
        if value > 0:
            risk_updates["live_max_order_notional"] = value
    if "live_max_margin_per_order" in params:
        value = _decimal_param(
            params.get("live_max_margin_per_order"),
            Decimal("0"),
        )
        if value > 0:
            risk_updates["live_max_margin_per_order"] = value
    if "live_require_stop_loss" in params:
        risk_updates["live_require_stop_loss"] = _bool_param(
            params.get("live_require_stop_loss"),
            True,
        )
    if "live_kill_switch" in params:
        risk_updates["live_kill_switch"] = _bool_param(
            params.get("live_kill_switch"),
            False,
        )

    if interval:
        if interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(f"Unsupported interval: {interval}")
    if execution_interval:
        if execution_interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(f"Unsupported execution interval: {execution_interval}")
        compare_interval = interval or "15m"
        if interval_to_ms(execution_interval) > interval_to_ms(compare_interval):
            raise ValueError("Execution interval cannot be larger than the strategy interval.")

    updates: dict[str, Any] = {}
    if interval:
        updates["strategy_interval"] = interval
    if execution_interval:
        updates["execution_interval"] = execution_interval
    if intrabar is not None:
        updates["paper_intrabar_enabled"] = intrabar
    if partial_parent is not None:
        updates["use_partial_parent_candle"] = partial_parent
    if max_entries is not None:
        updates["max_entries_per_parent_candle"] = max_entries
    if leverage is not None and leverage > 0:
        updates["paper_leverage"] = leverage
    if risk_updates:
        updates["risk_updates"] = risk_updates
    return updates


def _paper_quantity_unit(pair: object) -> str:
    symbol = str(pair or "")
    if symbol.startswith("B-"):
        symbol = symbol[2:]
    return symbol.split("_", 1)[0] or "contracts"


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _enrich_paper_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    """Backfill display-only fields for old paper_trades.csv rows.

    New rows are written with explicit INR/risk metadata. Older rows were logged
    before those columns existed, so the dashboard should not silently render
    zeros for notional and risk.
    """
    pair = row.get("pair", "")
    row.setdefault("quantity_unit", _paper_quantity_unit(pair))
    quantity = _decimal_or_none(row.get("position_size") or row.get("quantity"))
    entry = _decimal_or_none(row.get("entry_price"))
    quote_to_margin_rate = _decimal_or_none(row.get("quote_to_margin_rate"))
    legacy = quote_to_margin_rate is None
    if quote_to_margin_rate is None:
        quote_to_margin_rate = Decimal("1")
    unit_contract_value = _decimal_or_none(row.get("unit_contract_value")) or Decimal("1")

    if not row.get("position_notional") and quantity is not None and entry is not None:
        row["position_notional"] = str(abs(quantity * entry * quote_to_margin_rate * unit_contract_value))
    if not row.get("notional_currency"):
        row["notional_currency"] = "LEGACY" if legacy else "INR"
    if not row.get("price_quote_currency"):
        row["price_quote_currency"] = "USDT"
    if not row.get("quote_to_margin_rate"):
        row["quote_to_margin_rate"] = str(quote_to_margin_rate)
    if not row.get("unit_contract_value"):
        row["unit_contract_value"] = str(unit_contract_value)
    if not row.get("exit_fee") and row.get("fees"):
        row["exit_fee"] = row.get("fees", "")
    if not row.get("total_fees"):
        row["total_fees"] = row.get("fees", "")

    pos_notional = _decimal_or_none(row.get("position_notional") or row.get("entry_notional"))
    required_margin = _decimal_or_none(row.get("required_margin"))
    margin_used = _decimal_or_none(row.get("margin_used"))
    leverage = _decimal_or_none(row.get("leverage"))
    margin_corrected = False

    # Backfill ROE and Margin Used if missing. Older CSV rows saved
    # required_margin but not leverage/margin_used, so defaulting to 1x made
    # the dashboard display notional as margin and understated ROE.
    if required_margin is not None and required_margin > 0:
        wrong_notional_margin = (
            margin_used is not None
            and pos_notional is not None
            and margin_used == pos_notional
            and required_margin < pos_notional
        )
        if margin_used is None or margin_used <= 0 or wrong_notional_margin:
            margin_used = required_margin
            row["margin_used"] = str(margin_used)
            margin_corrected = True

    if (
        pos_notional is not None
        and pos_notional > 0
        and margin_used is not None
        and margin_used > 0
        and (leverage is None or leverage <= 0 or (leverage == 1 and margin_used != pos_notional))
    ):
        leverage = pos_notional / margin_used
        row["leverage"] = str(leverage)
    elif leverage is None or leverage <= 0:
        leverage = Decimal("1")
        row["leverage"] = "1"

    if (margin_used is None or margin_used <= 0) and pos_notional is not None:
        margin_used = pos_notional / leverage if leverage > 0 else pos_notional
        row["margin_used"] = str(margin_used)

    margin_used = _decimal_or_none(row.get("margin_used"))
    gross_pnl = _decimal_or_none(row.get("gross_pnl") or row.get("pnl"))
    net_pnl = _decimal_or_none(row.get("net_pnl") or row.get("pnl"))
    
    if (not row.get("gross_roe_pct") or margin_corrected) and gross_pnl is not None and margin_used and margin_used > 0:
        row["gross_roe_pct"] = str(gross_pnl / margin_used * 100)
    if (not row.get("net_roe_pct") or margin_corrected) and net_pnl is not None and margin_used and margin_used > 0:
        row["net_roe_pct"] = str(net_pnl / margin_used * 100)
    
    equity_at_entry = _decimal_or_none(row.get("account_equity_at_entry"))
    if not row.get("account_impact_pct") and net_pnl is not None and equity_at_entry and equity_at_entry > 0:
        row["account_impact_pct"] = str(net_pnl / equity_at_entry * 100)

    if legacy:
        row["legacy_currency_math"] = "true"
    return row


def _paper_trade_key(row: dict[str, Any]) -> tuple[str, ...]:
    fill_id = str(row.get("fill_id") or "").strip()
    if fill_id:
        return (
            "fill_id",
            fill_id,
            str(row.get("order_id") or "").strip(),
            str(row.get("timestamp") or "").strip(),
            str(row.get("pair") or "").strip(),
        )
    return (
        "composite",
        str(row.get("timestamp") or "").strip(),
        str(row.get("pair") or "").strip(),
        str(row.get("direction") or "").strip(),
        str(row.get("entry_price") or "").strip(),
        str(row.get("exit_price") or "").strip(),
        str(row.get("position_size") or row.get("quantity") or "").strip(),
        str(row.get("net_pnl") or "").strip(),
        str(row.get("exit_reason") or row.get("reason") or "").strip(),
    )


def _summarize_paper_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed_count = len(trades)
    wins = 0
    losses = 0
    gross_pnl = Decimal("0")
    net_pnl = Decimal("0")
    fees = Decimal("0")
    win_pnl = Decimal("0")
    loss_pnl = Decimal("0")
    best_trade: Decimal | None = None
    worst_trade: Decimal | None = None
    notional = Decimal("0")

    for row in trades:
        row_net = _decimal_or_none(row.get("net_pnl")) or Decimal("0")
        row_gross = _decimal_or_none(row.get("gross_pnl")) or Decimal("0")
        row_fees = (
            _decimal_or_none(row.get("total_fees"))
            or _decimal_or_none(row.get("fees"))
            or Decimal("0")
        )
        row_notional = (
            _decimal_or_none(row.get("position_notional"))
            or _decimal_or_none(row.get("notional"))
            or Decimal("0")
        )
        gross_pnl += row_gross
        net_pnl += row_net
        fees += row_fees
        notional += abs(row_notional)
        best_trade = row_net if best_trade is None else max(best_trade, row_net)
        worst_trade = row_net if worst_trade is None else min(worst_trade, row_net)
        if row_net > 0:
            wins += 1
            win_pnl += row_net
        elif row_net < 0:
            losses += 1
            loss_pnl += abs(row_net)

    win_rate = (
        (Decimal(wins) / Decimal(closed_count)) * Decimal("100")
        if closed_count
        else Decimal("0")
    )
    profit_factor: Decimal | None
    if loss_pnl == 0:
        profit_factor = None
    else:
        profit_factor = win_pnl / loss_pnl

    return {
        "closed_trades": closed_count,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": str(win_rate),
        "gross_pnl": str(gross_pnl),
        "net_pnl": str(net_pnl),
        "fees": str(fees),
        "avg_win": str(win_pnl / Decimal(wins)) if wins else "0",
        "avg_loss": str(loss_pnl / Decimal(losses)) if losses else "0",
        "best_trade": str(best_trade) if best_trade is not None else "0",
        "worst_trade": str(worst_trade) if worst_trade is not None else "0",
        "profit_factor": str(profit_factor) if profit_factor is not None else None,
        "closed_notional": str(notional),
        "fees_cost_pct": str((fees / notional) * Decimal("100")) if notional > 0 else "0",
    }


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        settings: Settings,
        defaults: DashboardDefaults,
        research_directory: Path | None = None,
        outcome_directory: Path | None = None,
        outcome_autostart: bool = True,
        continuous_autostart: bool = False,
        continuous_config: Path | None = None,
    ) -> None:
        import threading
        super().__init__(server_address, DashboardRequestHandler)
        self.settings = settings
        self.defaults = defaults
        self._paper_thread: threading.Thread | None = None
        self._paper_loop: Any = None  # PaperTradingLoop instance
        self._paper_lock = threading.Lock()
        from app.research.scan_service import ResearchScanService
        from app.research.outcome_tracker import ResearchOutcomeService
        self.research = ResearchScanService(research_directory or Path("data/research_scans"))
        self.research_outcomes = ResearchOutcomeService(
            self.research.directory,
            outcome_directory or Path("data/research_outcomes"),
            autostart=outcome_autostart,
        )
        from app.research.continuous.service import ContinuousResearchService
        self.continuous_research = ContinuousResearchService(
            self.research.directory.parent / "research" / "research.sqlite3",
            config_path=continuous_config, autostart=continuous_autostart,
        )

    def server_close(self) -> None:
        self.continuous_research.close()
        self.research_outcomes.close()
        self.research.close()
        super().server_close()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(_dashboard_html())
            return
        if parsed.path.startswith("/static/"):
            self._send_static_asset(parsed.path.removeprefix("/static/"))
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/research":
            from dataclasses import asdict
            from app.research.market_scanner import ScanConfig
            self._send_json({**self.server.research.status(), "defaults": asdict(ScanConfig())})
            return
        if parsed.path == "/api/research/outcomes":
            self._send_json(self.server.research_outcomes.status())
            return
        if parsed.path == "/api/research/continuous":
            self._send_json(self.server.continuous_research.status())
            return
        if parsed.path == "/api/research/continuous/validation":
            from app.research.continuous.validation_audit import audit_page, audit_detail
            query = parse_qs(parsed.query)
            store = self.server.continuous_research.store
            try:
                sid = query.get("sample_id", [""])[0]
                if sid:
                    if len(sid) > 64:
                        raise ValueError("Invalid sample id")
                    value = audit_detail(store, sid)
                    self._send_json(value or {"error": "Sample not found"}, status=HTTPStatus.OK if value else HTTPStatus.NOT_FOUND)
                else:
                    value = audit_page(store, config_id=query.get("config_id", [""])[0],
                                       cohort=query.get("cohort", ["candidates"])[0],
                                       outcome=query.get("outcome", ["all"])[0], query=query.get("q", [""])[0],
                                       page=int(query.get("page", ["0"])[0]))
                    self._send_json(value)
            except (ValueError, TypeError):
                self._send_json({"error": "Invalid validation filters"}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/research/continuous/history":
            from app.research.continuous.setups import setup_status
            store = self.server.continuous_research.store
            history = setup_status(store)
            setup_id = parse_qs(parsed.query).get("setup_id", [""])[0]
            if setup_id:
                item = next((s for s in history["items"] if s["id"] == setup_id), None)
                self._send_json({"setup": item, "events": store.setup_events(setup_id)} if item else {"error": "Setup not found"},
                                status=HTTPStatus.OK if item else HTTPStatus.NOT_FOUND)
            else:
                self._send_json({**history, "events": store.setup_events(), "validation": store.validation_results()})
            return
        if parsed.path == "/api/research/continuous/evidence":
            oid = parse_qs(parsed.query).get("id", [""])[0]
            value = self.server.continuous_research.store.observation(oid) if len(oid) == 32 else None
            self._send_json(value or {"error": "Evidence not found"}, status=HTTPStatus.OK if value else HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/status":
            self._send_json(
                build_status_payload(
                    settings=self.server.settings,
                    defaults=self.server.defaults,
                )
            )
            return
        if parsed.path == "/api/live-state":
            self._send_json(self._get_live_state_safe())
            return
        if parsed.path == "/api/pairs":
            self._send_pairs()
            return
        if parsed.path == "/api/positions":
            self._send_json({"positions": self._get_live_state_safe().get("positions", {})})
            return
        if parsed.path == "/api/orders":
            self._send_json({"orders": self._get_live_state_safe().get("orders", [])})
            return
        if parsed.path == "/api/risk":
            state = self._get_live_state_safe()
            self._send_json({
                "initial_equity": state.get("initial_equity", "0"),
                "allocated_capital": state.get("allocated_capital", "0"),
                "portfolio_equity": state.get("portfolio_equity", "0"),
                "wallet_balance": state.get("wallet_balance", "0"),
                "wallet_locked_collateral": state.get("wallet_locked_collateral", "0"),
                "wallet_free_collateral": state.get("wallet_free_collateral", "0"),
                "portfolio_unrealized_pnl": state.get("portfolio_unrealized_pnl", "0"),
                "usable_capital": state.get("usable_capital", "0"),
                "tradable_base": state.get("tradable_base", "0"),
                "locked_profit": state.get("locked_profit", "0"),
                "max_leveraged_notional": state.get("max_leveraged_notional", "0"),
                "effective_leverage": state.get("effective_leverage", "1"),
                "daily_loss_from_tradable_base": state.get("daily_loss_from_tradable_base", "0"),
                "kill_switch_active": state.get("kill_switch_active", False)
            })
            return
        if parsed.path == "/api/signals/latest":
            self._send_json({"signals": self._get_live_state_safe().get("recent_diagnostics", [])})
            return
        if parsed.path == "/api/trades/closed":
            self._send_json({"trades": self._get_live_state_safe().get("closed_trades", [])})
            return
        if parsed.path == "/api/alerts":
            self._send_json({"alerts": self._get_live_state_safe().get("alerts", [])})
            return
        if parsed.path == "/api/backtest":
            params = _flatten_query(parse_qs(parsed.query))
            self._send_backtest(params)
            return
        if parsed.path == "/api/paper-status":
            from app.live.paper_loop import get_active_loop, get_live_state
            state = get_live_state()
            active_loop = get_active_loop()
            state = dict(state)
            if active_loop is not None and not state.get("running"):
                state["running"] = True
                execution_mode = getattr(active_loop, "execution_mode", "paper")
                live_dry_run = getattr(active_loop, "live_dry_run", True)
                state["execution_mode"] = execution_mode if isinstance(execution_mode, str) else "paper"
                state["live_dry_run"] = live_dry_run if isinstance(live_dry_run, bool) else True
                watchlist = []
                snapshot_fn = getattr(active_loop, "_watchlist_snapshot", None)
                if callable(snapshot_fn):
                    try:
                        watchlist = list(snapshot_fn())
                    except Exception:
                        watchlist = []
                if not watchlist:
                    watchlist = getattr(active_loop, "_watchlist", None) or []
                if watchlist and not state.get("pair"):
                    state["pair"] = ", ".join(watchlist)
                interval = getattr(active_loop, "_current_interval", "")
                if interval and not state.get("interval"):
                    state["interval"] = interval
            effective_settings = getattr(active_loop, "settings", self.server.settings)
            effective_risk = getattr(effective_settings, "risk", self.server.settings.risk)
            state["live_max_order_notional"] = str(effective_risk.live_max_order_notional)
            state["live_max_margin_per_order"] = str(effective_risk.live_max_margin_per_order)
            state["live_require_stop_loss"] = bool(effective_risk.live_require_stop_loss)
            state["live_kill_switch"] = bool(effective_risk.live_kill_switch)
            self._send_json(state)
            return
        if parsed.path == "/api/paper-trades":
            self._send_paper_trades()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/api/research/continuous/start", "/api/research/continuous/stop", "/api/research/continuous/refresh"}:
            try:
                service = self.server.continuous_research
                action = parsed.path.rsplit("/", 1)[1]
                {"start": service.start, "stop": lambda: service.close(pause=True), "refresh": service.refresh}[action]()
                self._send_json(service.status(), status=HTTPStatus.ACCEPTED)
            except (ValueError, TypeError, OSError, KeyError):
                self._send_json({"error": "Invalid research configuration; check config/research.json"}, status=HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/research":
            from app.research.market_scanner import ScanConfig
            try:
                if not 0 < int(self.headers.get("Content-Length", "0")) <= 4096:
                    raise ValueError("Research settings must be a JSON object of at most 4096 bytes")
                config = ScanConfig.parse(self._read_json_body())
                self.server.research.start(config)
            except (ValueError, TypeError, OverflowError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(self.server.research.status(), status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/research/cancel":
            self.server.research.cancel()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/research/outcomes/refresh":
            self.server.research_outcomes.request_refresh()
            self._send_json(self.server.research_outcomes.status(), status=HTTPStatus.ACCEPTED)
            return
        if parsed.path == "/api/kill-switch/enable":
            self._handle_kill_switch_enable()
            return
        if parsed.path == "/api/kill-switch/disable":
            self._handle_kill_switch_disable(self._read_json_body())
            return
        if parsed.path == "/api/refresh":
            self._send_json({"success": True})
            return
        if parsed.path == "/api/backtest":
            self._send_backtest(self._read_json_body())
            return
        if parsed.path == "/api/paper-start":
            self._handle_paper_start(self._read_json_body())
            return
        if parsed.path == "/api/paper-stop":
            self._handle_paper_stop()
            return
        if parsed.path == "/api/paper-reset":
            self._handle_paper_reset()
            return
        if parsed.path == "/api/paper-add-pair":
            self._handle_paper_add_pair(self._read_json_body())
            return
        if parsed.path == "/api/paper-watchlist":
            self._handle_paper_watchlist(self._read_json_body())
            return
        if parsed.path == "/api/paper-settings":
            self._handle_paper_settings(self._read_json_body())
            return
        if parsed.path == "/api/paper-remove-pair":
            self._handle_paper_remove_pair(self._read_json_body())
            return
        if parsed.path == "/api/profit-lock/unlock":
            self._handle_profit_unlock(self._read_json_body())
            return
        if parsed.path == "/api/profit-lock/lock":
            self._handle_profit_lock(self._read_json_body())
            return
        if parsed.path == "/api/profit-lock/override-daily-loss":
            self._handle_override_daily_loss()
            return
        if parsed.path == "/api/profit-lock/clear-daily-loss-override":
            self._handle_clear_daily_loss_override()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("app.dashboard").debug(format, *args)

    def _get_live_state_safe(self) -> dict[str, Any]:
        run_paths, legacy_path = _live_state_paths()
        run_states = [
            state
            for state in (_read_json_dict(path) for path in run_paths)
            if state
        ]
        if run_states:
            state = _merge_live_state_dicts(run_states)
            control = _read_json_dict(legacy_path)
            if control.get("live_state_control") and "kill_switch_active" in control:
                state["kill_switch_active"] = bool(control.get("kill_switch_active"))
            return state

        return _read_json_dict(legacy_path)

    def _handle_kill_switch_enable(self) -> None:
        self._write_live_kill_switch_state(True)
        self._send_json({"success": True, "message": "Kill switch activated via dashboard."})

    def _handle_kill_switch_disable(self, params: dict[str, Any]) -> None:
        if params.get("confirm") != "DISABLE":
             self._send_json({"error": "Confirmation text 'DISABLE' required."}, status=HTTPStatus.BAD_REQUEST)
             return
        self._write_live_kill_switch_state(False)
        self._send_json({"success": True, "message": "Kill switch disabled via dashboard."})

    def _write_live_kill_switch_state(self, active: bool) -> None:
        run_paths, legacy_path = _live_state_paths()
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps({
            "kill_switch_active": active,
            "live_state_control": True,
        }, indent=2))

        for path in run_paths:
            state = _read_json_dict(path)
            if not state:
                continue
            state["kill_switch_active"] = active
            try:
                path.write_text(json.dumps(to_jsonable(state), indent=2))
            except Exception:
                logging.getLogger("app.dashboard").warning("Failed to update kill switch in %s", path)

    def _handle_paper_start(self, params: dict[str, Any]) -> None:
        import threading
        from app.live.paper_loop import PaperTradingLoop
        from app.live.paper_loop import get_active_loop, get_live_state
        from app.strategies.defaults import STRATEGY_CHOICES
        from app.backtest.data_loader import REST_RESOLUTION_BY_INTERVAL

        state = get_live_state()
        if state["running"] or get_active_loop() is not None:
            self._send_json({"error": "Paper loop is already running."}, 
                            status=HTTPStatus.CONFLICT)
            return

        pair_val = params.get("pair") or params.get("pairs") or self.server.settings.default_pair
        pairs = _dashboard_pair_list(pair_val)
        if not pairs:
            self._send_json({"error": "At least one paper-trading pair is required."}, 
                            status=HTTPStatus.BAD_REQUEST)
            return

        interval = str(params.get("interval") or "15m")
        strategy = str(params.get("strategy") or "hybrid_meta_v2")
        
        if interval not in REST_RESOLUTION_BY_INTERVAL:
            self._send_json({"error": f"Unsupported interval: {interval}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return
        if strategy not in STRATEGY_CHOICES:
            self._send_json({"error": f"Unknown strategy: {strategy}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return

        settings = self.server.settings
        execution_mode = str(params.get("execution_mode") or "paper").strip().lower()
        if execution_mode not in {"paper", "live_dry_run", "live_pilot"}:
            self._send_json(
                {"error": f"Unsupported execution mode: {execution_mode}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        if execution_mode == "live_pilot" and not settings.live_trading_allowed:
            self._send_json(
                {
                    "error": (
                        "Live Pilot requires TRADING_MODE=live and "
                        "LIVE_TRADING_ENABLED=true. Use Live Dry Run first."
                    )
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        from decimal import Decimal, InvalidOperation
        starting_equity = settings.paper_starting_equity
        if params.get("starting_equity"):
            try:
                starting_equity = Decimal(str(params["starting_equity"]))
            except InvalidOperation:
                pass

        try:
            leverage = Decimal(str(params.get("leverage", settings.paper_leverage)))
            risk_pct = Decimal(str(params.get("risk_pct", settings.risk.max_risk_per_trade_pct)))
            max_daily_loss_pct = Decimal(
                str(params.get("max_daily_loss_pct", settings.risk.max_daily_loss_pct))
            )
            max_open = int(params.get("max_open_positions", settings.risk.max_open_positions))
            allow_multi = _bool_param(
                params.get("allow_multi_pair_positions"),
                settings.risk.allow_multi_pair_positions,
            )
            allow_pyramid = _bool_param(
                params.get("allow_same_pair_pyramiding"),
                settings.risk.allow_same_pair_pyramiding,
            )
            max_margin_usage = Decimal(
                str(params.get("max_margin_usage_pct", settings.risk.max_margin_usage_pct))
            )
            paper_intrabar_enabled = _bool_param(
                params.get("paper_intrabar_enabled"),
                settings.paper_intrabar_enabled,
            )
            use_partial_parent_candle = _bool_param(
                params.get("use_partial_parent_candle"),
                settings.use_partial_parent_candle,
            )
            max_entries_per_parent_candle = int(
                params.get(
                    "max_entries_per_parent_candle",
                    settings.max_entries_per_parent_candle,
                )
            )
            trailing_stop_enabled = _bool_param(
                params.get("trailing_stop_enabled"),
                settings.risk.trailing_stop_enabled,
            )
            atr_exits_enabled = _bool_param(
                params.get("atr_dynamic_exits_enabled"),
                False,
            )
            profit_lock_enabled = _bool_param(
                params.get("profit_lock_enabled"),
                True,
            )
            bb_trail_enabled = _bool_param(
                params.get("bb_trail_enabled"),
                settings.risk.bb_trail_enabled,
            )
            live_max_order_notional = Decimal(
                str(params.get("live_max_order_notional", settings.risk.live_max_order_notional))
            )
            live_max_margin_per_order = Decimal(
                str(params.get("live_max_margin_per_order", settings.risk.live_max_margin_per_order))
            )
            live_require_stop_loss = _bool_param(
                params.get("live_require_stop_loss"),
                settings.risk.live_require_stop_loss,
            )
            live_kill_switch = _bool_param(
                params.get("live_kill_switch"),
                settings.risk.live_kill_switch,
            )
        except (ValueError, InvalidOperation) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        strategy_interval = str(params.get("strategy_interval") or interval)
        execution_interval = str(params.get("execution_interval") or "1m")
        if strategy_interval not in REST_RESOLUTION_BY_INTERVAL:
            self._send_json({"error": f"Unsupported strategy interval: {strategy_interval}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return
        if paper_intrabar_enabled and execution_interval not in REST_RESOLUTION_BY_INTERVAL:
            self._send_json({"error": f"Unsupported execution interval: {execution_interval}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return

        # Build modified risk settings
        new_risk = replace(
            settings.risk,
            max_risk_per_trade_pct=risk_pct,
            max_daily_loss_pct=max_daily_loss_pct,
            max_open_positions=max_open,
            allow_multi_pair_positions=allow_multi,
            allow_same_pair_pyramiding=allow_pyramid,
            max_margin_usage_pct=max_margin_usage,
            trailing_stop_enabled=trailing_stop_enabled,
            atr_stop_enabled=atr_exits_enabled,
            atr_take_profit_enabled=atr_exits_enabled,
            atr_trailing_enabled=atr_exits_enabled,
            profit_lock_enabled=profit_lock_enabled,
            bb_trail_enabled=bb_trail_enabled,
            live_max_order_notional=live_max_order_notional,
            live_max_margin_per_order=live_max_margin_per_order,
            live_require_stop_loss=live_require_stop_loss,
            live_kill_switch=live_kill_switch,
            live_risk_approval_enabled=(
                execution_mode != "paper" or settings.risk.live_risk_approval_enabled
            ),
        )

        # Build a modified settings
        from dataclasses import replace as dc_replace
        effective_settings = dc_replace(
            settings, 
            paper_starting_equity=starting_equity,
            paper_leverage=leverage,
            paper_intrabar_enabled=paper_intrabar_enabled,
            strategy_interval=strategy_interval,
            execution_interval=execution_interval,
            use_partial_parent_candle=use_partial_parent_candle,
            max_entries_per_parent_candle=max_entries_per_parent_candle,
            live_pilot_dry_run=(
                execution_mode != "live_pilot"
                if execution_mode != "paper"
                else settings.live_pilot_dry_run
            ),
            risk=new_risk
        )

        pair_overrides = _paper_pair_overrides(params.get("pair_overrides"))
        loop = PaperTradingLoop(
            effective_settings,
            strategy_name=strategy,
            pair_overrides=pair_overrides,
            execution_mode=execution_mode,
        )
        
        with self.server._paper_lock:
            self.server._paper_loop = loop

        def run_loop():
            try:
                loop.run(pairs, interval)
            except Exception as exc:
                from app.live.paper_loop import _update_live_state
                _update_live_state(running=False, error=str(exc))
                logging.getLogger(__name__).exception("Paper loop crashed: %s", exc)
            finally:
                with self.server._paper_lock:
                    if self.server._paper_loop is loop:
                        self.server._paper_loop = None

        t = threading.Thread(target=run_loop, daemon=True, name="paper-loop")
        with self.server._paper_lock:
            self.server._paper_thread = t
        t.start()
        self._send_json({
            "started": True,
            "pairs": pairs,
            "interval": interval,
            "strategy": strategy,
            "pair_overrides": pair_overrides,
            "execution_mode": execution_mode,
            "live_dry_run": execution_mode != "live_pilot",
        })

    def _handle_paper_stop(self) -> None:
        from app.live.paper_loop import get_active_loop
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, 
                            status=HTTPStatus.CONFLICT)
            return
        loop.stop()
        self._send_json({"stopped": True})

    def _handle_paper_add_pair(self, params: dict[str, Any]) -> None:
        pair = params.get("pair")
        if not pair:
            self._send_json({"error": "No pair provided"}, status=HTTPStatus.BAD_REQUEST)
            return
        
        pair = _normalize_dashboard_pair(pair)
        with self.server._paper_lock:
            loop = self.server._paper_loop
            
        if not loop:
            self._send_json({"error": "Paper loop not running"}, status=HTTPStatus.CONFLICT)
            return

        try:
            loop.update_pair_overrides(_paper_pair_overrides(params.get("pair_overrides")))
            result = loop.add_pair_to_watchlist(pair)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            logging.getLogger("app.dashboard").exception("Failed to add %s to watchlist.", pair)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if not result.get("added", False):
            self._send_json({"error": result.get("reason", f"{pair} already in watchlist")}, status=HTTPStatus.CONFLICT)
            return

        self._send_json(result)

    def _handle_paper_watchlist(self, params: dict[str, Any]) -> None:
        raw_pairs = params.get("pairs", params.get("watchlist"))
        pairs = _dashboard_pair_list(raw_pairs)
        if not pairs:
            self._send_json({"error": "At least one pair is required."}, status=HTTPStatus.BAD_REQUEST)
            return

        with self.server._paper_lock:
            loop = self.server._paper_loop

        if not loop:
            self._send_json({"error": "Paper loop not running"}, status=HTTPStatus.CONFLICT)
            return

        try:
            loop.update_pair_overrides(_paper_pair_overrides(params.get("pair_overrides")))
            result = loop.update_watchlist(pairs)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            logging.getLogger("app.dashboard").exception("Failed to update paper watchlist.")
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json(result)

    def _handle_paper_settings(self, params: dict[str, Any]) -> None:
        with self.server._paper_lock:
            loop = self.server._paper_loop

        if not loop:
            self._send_json({"error": "Paper loop not running"}, status=HTTPStatus.CONFLICT)
            return

        try:
            runtime_result = loop.update_runtime_settings(**_paper_runtime_settings(params))
            pair_overrides = _paper_pair_overrides(params.get("pair_overrides"))
            overrides = loop.update_pair_overrides(pair_overrides) if pair_overrides else {}
            pairs = _dashboard_pair_list(params.get("pairs", params.get("watchlist")))
            watchlist_result = loop.update_watchlist(pairs) if pairs else None
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            logging.getLogger("app.dashboard").exception("Failed to update paper settings.")
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json(
            {
                "updated": True,
                "runtime": runtime_result,
                "pair_overrides": overrides,
                "watchlist": watchlist_result,
            }
        )

    def _handle_paper_remove_pair(self, params: dict[str, Any]) -> None:
        pair = params.get("pair")
        if not pair:
            self._send_json({"error": "No pair provided"}, status=HTTPStatus.BAD_REQUEST)
            return

        pair = _normalize_dashboard_pair(pair)
        with self.server._paper_lock:
            loop = self.server._paper_loop

        if not loop:
            self._send_json({"error": "Paper loop not running"}, status=HTTPStatus.CONFLICT)
            return

        try:
            result = loop.remove_pair_from_watchlist(pair)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            logging.getLogger("app.dashboard").exception("Failed to remove %s from watchlist.", pair)
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if not result.get("removed", True) and not result.get("updated", False):
            self._send_json({"error": result.get("reason", f"{pair} is not in watchlist")}, status=HTTPStatus.CONFLICT)
            return

        self._send_json(result)

    def _handle_paper_reset(self) -> None:
        from app.live.paper_loop import get_active_loop, reset_live_state, set_active_loop
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        import os
        
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
            self.server._paper_loop = None
            self.server._paper_thread = None
        if loop:
            loop.stop()
        set_active_loop(None)
        
        # Clear persistent stores
        state_store = PaperStateStore()
        state_store.clear()
        state_store.close()
        PaperSessionStore().clear()
        
        # Clear summary csv
        if os.path.exists("paper_trades.csv"):
            os.remove("paper_trades.csv")
            
        # Reset every dashboard runtime field so stale values from prior sessions
        # cannot survive when new fields are added to LivePaperState.
        reset_live_state(
            execution_mode="paper",
            live_dry_run=True,
        )
        
        self._send_json({"reset": True})

    def _handle_profit_unlock(self, params: dict[str, Any]) -> None:
        from app.live.paper_loop import get_active_loop
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, status=HTTPStatus.CONFLICT)
            return
        
        amount = _decimal_or_none(params.get("amount"))
        reason = str(params.get("reason") or "Manual Dashboard Unlock")
        if amount is None or amount <= 0:
            self._send_json({"error": "Invalid amount."}, status=HTTPStatus.BAD_REQUEST)
            return
            
        loop.broker.unlock_profit(amount, reason)
        self._send_json({"success": True, "message": f"Unlocked {amount} profit."})

    def _handle_profit_lock(self, params: dict[str, Any]) -> None:
        from app.live.paper_loop import get_active_loop
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, status=HTTPStatus.CONFLICT)
            return
        
        amount = _decimal_or_none(params.get("amount"))
        reason = str(params.get("reason") or "Manual Dashboard Lock")
        if amount is None or amount <= 0:
            self._send_json({"error": "Invalid amount."}, status=HTTPStatus.BAD_REQUEST)
            return
            
        loop.broker.lock_profit(amount, reason)
        self._send_json({"success": True, "message": f"Locked {amount} profit."})

    def _handle_override_daily_loss(self) -> None:
        from app.live.paper_loop import get_active_loop
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, status=HTTPStatus.CONFLICT)
            return
            
        loop.broker.protected_profit_override_enabled = True
        loop.broker._save_state()
        self._send_json({"success": True, "message": "Daily loss override enabled."})

    def _handle_clear_daily_loss_override(self) -> None:
        from app.live.paper_loop import get_active_loop
        with self.server._paper_lock:
            loop = self.server._paper_loop or get_active_loop()
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, status=HTTPStatus.CONFLICT)
            return

        loop.broker.protected_profit_override_enabled = False
        loop.broker._save_state()
        self._send_json({"success": True, "message": "Daily loss override disabled."})


    def _send_pairs(self) -> None:
        from pathlib import Path
        import time
        cache_path = Path("data/coindcx_inr_futures_pairs_cache.json")
        now = time.time()
        
        # Try load cache
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                # Cache valid for 1 hour
                if now - cached.get("timestamp", 0) < 3600:
                    self._send_json(cached["data"])
                    return
            except Exception:
                pass
        
        # Fetch live
        client = CoinDCXFuturesClient(self.server.settings)
        try:
            # CoinDCX active_instruments returns list of strings like "B-BTC_USDT"
            # for a given margin currency.
            margin = "INR"
            raw_pairs = client.get_active_instruments(margin_currency=margin)
            
            pairs = []
            for p in sorted(raw_pairs):
                # "B-BTC_USDT" -> "BTC-USDT"
                display = p
                if p.startswith("B-"):
                    display = p[2:].replace("_", "-")
                
                pairs.append({
                    "pair": p,
                    "display_name": display,
                    "margin_currency": margin,
                    "active": True
                })
            
            data = {
                "market": "futures",
                "margin": margin,
                "source": "live",
                "pairs": pairs
            }
            
            # Save to cache
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": now, "data": data}, f, indent=2)
                
            self._send_json(data)
        except Exception as exc:
            logging.getLogger("app.dashboard").error("Failed to fetch pairs: %s", exc)
            # Fallback to empty or previous cache if available
            self._send_json({
                "market": "futures",
                "margin": "INR",
                "source": "fallback",
                "pairs": []
            })

    def _send_paper_trades(self) -> None:
        import csv
        from pathlib import Path
        path = Path("paper_trades.csv")
        if not path.exists():
            self._send_json({"trades": [], "summary": _summarize_paper_trades([])})
            return
        trades = []
        seen: set[tuple[str, ...]] = set()
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                enriched = _enrich_paper_trade_row(dict(row))
                key = _paper_trade_key(enriched)
                if key in seen:
                    continue
                seen.add(key)
                trades.append(enriched)
        self._send_json(
            {
                "trades": list(reversed(trades[-100:])),
                "summary": _summarize_paper_trades(trades),
            }
        )

    def _send_backtest(self, params: dict[str, Any]) -> None:
        try:
            payload = run_backtest_for_dashboard(
                settings=self.server.settings,
                defaults=self.server.defaults,
                params=params,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except CoinDCXError as exc:
            self._send_json(
                {"error": "CoinDCX public data request failed.", "detail": str(exc)},
                status=HTTPStatus.BAD_GATEWAY,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("Dashboard backtest failed")
            self._send_json(
                {"error": "Dashboard backtest failed.", "detail": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        else:
            self._send_json(payload)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_static_asset(self, asset_name: str) -> None:
        allowed = {"dashboard.css", "dashboard.js", "research.css", "research.js", "continuous.js", "validation.js"}
        if asset_name not in allowed:
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            encoded = (
                files("app.dashboard.static")
                .joinpath(asset_name)
                .read_bytes()
            )
        except FileNotFoundError:
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        # Pre-process payload to be JSON-safe (handles Decimals, datetimes, dataclasses)
        jsonable_payload = to_jsonable(payload)
        encoded = json.dumps(
            jsonable_payload, 
            indent=2, 
            sort_keys=True, 
            default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def run_dashboard(
    *,
    host: str,
    port: int,
    defaults: DashboardDefaults,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("app.risk.manager").setLevel(logging.WARNING)
    logging.getLogger("app.broker.paper").setLevel(logging.WARNING)

    server = DashboardHTTPServer((host, port), settings=settings, defaults=defaults, continuous_autostart=True)
    print(f"Dashboard running at http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped.", flush=True)
    finally:
        server.server_close()


def run_backtest_for_dashboard(
    *,
    settings: Settings,
    defaults: DashboardDefaults,
    params: dict[str, Any],
) -> dict[str, Any]:
    pair = _normalize_dashboard_pair(params.get("pair") or defaults.pair)
    interval = str(params.get("interval") or defaults.interval)
    strategy = str(params.get("strategy") or defaults.strategy)
    lookback = _int_param(params.get("lookback"), defaults.lookback)
    equity = _decimal_param(params.get("equity"), defaults.equity)
    leverage = _decimal_param(params.get("leverage"), defaults.leverage)
    risk_per_trade_pct = _decimal_param(
        params.get("risk_per_trade_pct"),
        settings.risk.max_risk_per_trade_pct,
    )
    max_daily_loss_pct = _decimal_param(
        params.get("max_daily_loss_pct"),
        settings.risk.max_daily_loss_pct,
    )
    compound_risk_equity = _bool_param(
        params.get("compound_risk_equity"),
        defaults.compound_risk_equity,
    )
    stop_loss_pct = _optional_decimal_param(
        params.get("stop_loss_pct"),
        name="stop_loss_pct",
    )
    take_profit_pct = _optional_decimal_param(
        params.get("take_profit_pct"),
        name="take_profit_pct",
    )
    fee_config = _fee_config_params(params, defaults)
    slippage_pct = _decimal_param(params.get("slippage_pct"), defaults.slippage_pct)
    stop_slippage_pct = _nonnegative_decimal_param(
        params.get("stop_slippage_pct"),
        defaults.stop_slippage_pct,
        name="stop_slippage_pct",
    )
    funding_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("funding_fee_pct"),
        rate=params.get("funding_fee_rate"),
        default=defaults.funding_fee_rate,
        allow_negative=True,
    )
    funding_interval_hours = _positive_int_param(
        params.get("funding_interval_hours"),
        defaults.funding_interval_hours,
        name="funding_interval_hours",
    )
    trailing_stop_enabled = _bool_param(
        params.get("trailing_stop_enabled"),
        settings.risk.trailing_stop_enabled,
    )
    trailing_stop_activation_pct = _decimal_param(
        params.get("trailing_stop_activation_pct"),
        settings.risk.trailing_stop_activation_pct,
    )
    trailing_stop_distance_pct = _decimal_param(
        params.get("trailing_stop_distance_pct"),
        settings.risk.trailing_stop_distance_pct,
    )
    atr_dynamic_exits_enabled = _bool_param(
        params.get("atr_dynamic_exits_enabled"),
        defaults.atr_dynamic_exits_enabled,
    )
    atr_stop_enabled = _bool_param(
        params.get("atr_stop_enabled"),
        defaults.atr_stop_enabled,
    )
    atr_take_profit_enabled = _bool_param(
        params.get("atr_take_profit_enabled"),
        defaults.atr_take_profit_enabled,
    )
    atr_trailing_enabled = _bool_param(
        params.get("atr_trailing_enabled"),
        defaults.atr_trailing_enabled,
    )
    atr_entry_filter_enabled = _bool_param(
        params.get("atr_entry_filter_enabled"),
        defaults.atr_entry_filter_enabled,
    )
    atr_policy_mode = _atr_policy_mode_param(
        params.get("atr_policy_mode"),
        defaults.atr_policy_mode,
    )
    atr_period = _positive_int_param(
        params.get("atr_period"),
        defaults.atr_period,
        name="atr_period",
    )
    atr_stop_multiple = _optional_decimal_param(
        params.get("atr_stop_multiple"),
        name="atr_stop_multiple",
    ) or defaults.atr_stop_multiple
    atr_take_profit_multiple = _optional_decimal_param(
        params.get("atr_take_profit_multiple"),
        name="atr_take_profit_multiple",
    ) or defaults.atr_take_profit_multiple
    atr_trailing_multiple = _optional_decimal_param(
        params.get("atr_trailing_multiple"),
        name="atr_trailing_multiple",
    ) or defaults.atr_trailing_multiple
    atr_take_profit_mode = _atr_take_profit_mode_param(
        params.get("atr_take_profit_mode"),
        defaults.atr_take_profit_mode,
    )
    execution_interval = str(
        params.get("execution_interval") or defaults.execution_interval or ""
    ).strip()
    intrabar_reentry_enabled = _bool_param(
        params.get("intrabar_reentry_enabled"),
        defaults.intrabar_reentry_enabled,
    )
    max_reentries_per_candle = _nonnegative_int_param(
        params.get("max_reentries_per_candle"),
        defaults.max_reentries_per_candle,
        name="max_reentries_per_candle",
    )
    reentry_cooldown_candles = _nonnegative_int_param(
        params.get("reentry_cooldown_candles"),
        defaults.reentry_cooldown_candles,
        name="reentry_cooldown_candles",
    )
    stop_loss_cooldown_candles = _nonnegative_int_param(
        params.get("stop_loss_cooldown_candles"),
        defaults.stop_loss_cooldown_candles,
        name="stop_loss_cooldown_candles",
    )
    max_consecutive_losses = _nonnegative_int_param(
        params.get("max_consecutive_losses"),
        defaults.max_consecutive_losses,
        name="max_consecutive_losses",
    )
    loss_cooldown_candles = _nonnegative_int_param(
        params.get("loss_cooldown_candles"),
        defaults.loss_cooldown_candles,
        name="loss_cooldown_candles",
    )
    recent_count = _int_param(params.get("recent_count"), 20)

    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise ValueError(
            f"Unsupported interval {interval}. Choose one of: "
            f"{', '.join(sorted(REST_RESOLUTION_BY_INTERVAL))}."
        )
    if intrabar_reentry_enabled and not execution_interval:
        execution_interval = _auto_execution_interval(interval)
        if not execution_interval:
            intrabar_reentry_enabled = False
    if execution_interval:
        if execution_interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(
                f"Unsupported execution interval {execution_interval}. Choose one of: "
                f"{', '.join(sorted(REST_RESOLUTION_BY_INTERVAL))}."
            )
        if interval_to_ms(execution_interval) >= interval_to_ms(interval):
            raise ValueError("Execution interval must be smaller than the strategy interval.")
        if interval_to_ms(interval) % interval_to_ms(execution_interval) != 0:
            raise ValueError(
                "Execution interval must divide the strategy interval cleanly."
            )
    if intrabar_reentry_enabled and max_reentries_per_candle <= 0:
        raise ValueError("Max Re-entries / Candle must be at least 1 when Intrabar Re-entry is on.")
    if strategy not in STRATEGY_CHOICES:
        raise ValueError(
            f"Unsupported strategy {strategy}. Choose one of: {', '.join(STRATEGY_CHOICES)}."
        )
    if lookback <= 0:
        raise ValueError("lookback must be positive.")

    client = CoinDCXFuturesClient(settings)
    series = load_historical_candle_series(
        client=client,
        pair=pair,
        interval=interval,
        lookback=lookback,
    )
    if len(series) == 0:
        raise ValueError("No candles loaded; cannot run dashboard backtest.")
    open_interest_features = _load_open_interest_proxy_safe(
        pair=pair,
        interval=interval,
        series=series,
        strategy=strategy,
    )
    execution_series = None
    if execution_interval:
        first = series[0]
        last = series[-1]
        execution_lookback = max(
            int((last.close_time_ms - first.open_time_ms + 1) / interval_to_ms(execution_interval))
            + 5,
            1,
        )
        execution_series = load_historical_candle_series_between(
            client=client,
            pair=pair,
            interval=execution_interval,
            from_ts=int(first.open_time_ms / 1000),
            to_ts=int(last.close_time_ms / 1000) + 1,
            maxlen=execution_lookback,
        )
        if len(execution_series) == 0:
            raise ValueError("No execution candles loaded; cannot run intrabar backtest.")

    config = BacktestConfig(
        pair=pair,
        interval=interval,
        starting_equity=equity,
        leverage=leverage,
        strategy_name=strategy,
        margin_currency=settings.futures_margin_currency,
        price_quote_currency=settings.price_quote_currency,
        quote_to_margin_rate=settings.quote_to_margin_rate,
        risk_per_trade_pct=risk_per_trade_pct,
        compound_risk_equity=compound_risk_equity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        maker_fee_rate=fee_config["maker_fee_rate"],
        taker_fee_rate=fee_config["taker_fee_rate"],
        fee_gst_rate=fee_config["fee_gst_rate"],
        entry_fee_type=fee_config["entry_fee_type"],
        exit_fee_type=fee_config["exit_fee_type"],
        slippage_pct=slippage_pct,
        stop_slippage_pct=stop_slippage_pct,
        funding_fee_rate=funding_fee_rate,
        funding_interval_hours=funding_interval_hours,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation_pct=trailing_stop_activation_pct,
        trailing_stop_distance_pct=trailing_stop_distance_pct,
        atr_dynamic_exits_enabled=atr_dynamic_exits_enabled,
        atr_stop_enabled=atr_stop_enabled,
        atr_take_profit_enabled=atr_take_profit_enabled,
        atr_trailing_enabled=atr_trailing_enabled,
        atr_entry_filter_enabled=atr_entry_filter_enabled,
        atr_policy_mode=atr_policy_mode,
        atr_period=atr_period,
        atr_stop_multiple=atr_stop_multiple,
        atr_take_profit_multiple=atr_take_profit_multiple,
        atr_trailing_multiple=atr_trailing_multiple,
        atr_take_profit_mode=atr_take_profit_mode,
        profit_giveback_guard_enabled=settings.risk.profit_giveback_guard_enabled,
        profit_giveback_activation_r=settings.risk.profit_giveback_activation_r,
        profit_giveback_lock_fraction=settings.risk.profit_giveback_lock_fraction,
        profit_giveback_min_lock_r=settings.risk.profit_giveback_min_lock_r,
        profit_giveback_tighten_after_r=settings.risk.profit_giveback_tighten_after_r,
        profit_giveback_tighten_fraction=settings.risk.profit_giveback_tighten_fraction,
        post_profit_reentry_guard_enabled=settings.risk.post_profit_reentry_guard_enabled,
        post_profit_reentry_cooldown_candles=settings.risk.post_profit_reentry_cooldown_candles,
        post_profit_reentry_pullback_atr=settings.risk.post_profit_reentry_pullback_atr,
        post_profit_reentry_pullback_pct=settings.risk.post_profit_reentry_pullback_pct,
        execution_interval=execution_interval or None,
        paper_intrabar_enabled=bool(execution_interval),
        strategy_interval=interval,
        intrabar_reentry_enabled=intrabar_reentry_enabled,
        max_reentries_per_candle=max_reentries_per_candle,
        reentry_cooldown_candles=reentry_cooldown_candles,
        stop_loss_cooldown_candles=stop_loss_cooldown_candles,
        max_consecutive_losses=max_consecutive_losses,
        loss_cooldown_candles=loss_cooldown_candles,
        max_daily_loss_pct=max_daily_loss_pct,
    )
    engine = BacktestEngine(
        config=config,
        strategy_engine=strategy_engine_for_name(strategy),
        risk_manager=RiskManager(
            replace(
                settings.risk,
                max_risk_per_trade_pct=risk_per_trade_pct,
                max_daily_loss_pct=max_daily_loss_pct,
                trailing_stop_enabled=trailing_stop_enabled,
                trailing_stop_activation_pct=trailing_stop_activation_pct,
                trailing_stop_distance_pct=trailing_stop_distance_pct,
            )
        ),
        instrument=_instrument_metadata(client, pair=pair, margin_currency=settings.futures_margin_currency),
        open_interest_features=open_interest_features,
    )
    result = engine.run(series, execution_candles=execution_series)
    payload = build_backtest_dashboard_payload(result, recent_count=recent_count)
    payload["history"] = record_backtest_result(result, source="dashboard")
    return payload


def _instrument_metadata(
    client: CoinDCXFuturesClient,
    *,
    pair: str,
    margin_currency: str,
) -> InstrumentMetadata:
    try:
        instrument_data = client.get_instrument(pair, margin_currency)
    except CoinDCXError:
        return InstrumentMetadata(pair=pair)
    if isinstance(instrument_data, dict):
        return InstrumentMetadata.from_mapping(pair, instrument_data)
    return InstrumentMetadata(pair=pair)


def _load_open_interest_proxy_safe(
    *,
    pair: str,
    interval: str,
    series,
    strategy: str,
) -> OpenInterestFeatureSeries:
    if strategy.strip().lower() not in OPEN_INTEREST_STRATEGIES:
        return OpenInterestFeatureSeries([])
    try:
        return load_binance_open_interest_proxy(
            pair=pair,
            interval=interval,
            candles=series,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Binance open-interest proxy unavailable for %s %s: %s",
            pair,
            interval,
            exc,
        )
        return OpenInterestFeatureSeries([])


def _auto_execution_interval(interval: str) -> str:
    return {
        "24h": "4h",
        "1d": "4h",
        "4h": "1h",
        "2h": "1h",
        "2hr": "1h",
        "1h": "15m",
        "30m": "15m",
        "30min": "15m",
        "15m": "5m",
        "5m": "1m",
    }.get(interval, "")


def _flatten_query(values: dict[str, list[str]]) -> dict[str, Any]:
    return {key: item[-1] for key, item in values.items() if item}


def _decimal_param(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def _optional_decimal_param(value: Any, *, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    parsed = _decimal_param(value, Decimal("0"))
    if parsed <= 0:
        raise ValueError(f"{name} must be positive when provided.")
    return parsed


def _nonnegative_decimal_param(value: Any, default: Decimal, *, name: str) -> Decimal:
    parsed = _decimal_param(value, default)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative.")
    return parsed


def _fee_rate_param(params: dict[str, Any], default: Decimal) -> Decimal:
    fee_type = str(params.get("fee_type") or "").strip().lower()
    if fee_type == "inr_maker":
        return COINDCX_INR_M_MAKER_FEE_RATE
    if fee_type == "inr_taker":
        return COINDCX_INR_M_TAKER_FEE_RATE
    if params.get("fee_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_pct"))
    return _normalize_fee_rate(_decimal_param(params.get("fee_rate"), default))


def _fee_config_params(params: dict[str, Any], defaults: DashboardDefaults) -> dict[str, object]:
    maker_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("maker_fee_pct"),
        rate=params.get("maker_fee_rate"),
        default=defaults.maker_fee_rate,
    )
    taker_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("taker_fee_pct"),
        rate=params.get("taker_fee_rate"),
        default=defaults.taker_fee_rate,
    )
    legacy_fee_rate = _legacy_fee_rate(params)
    if legacy_fee_rate is not None:
        maker_fee_rate = legacy_fee_rate
        taker_fee_rate = legacy_fee_rate

    fee_gst_rate = _gst_rate_param(params, defaults.fee_gst_rate)
    entry_fee_type = _fee_type_param(params.get("entry_fee_type"), defaults.entry_fee_type)
    exit_fee_type = _fee_type_param(params.get("exit_fee_type"), defaults.exit_fee_type)
    return {
        "maker_fee_rate": maker_fee_rate,
        "taker_fee_rate": taker_fee_rate,
        "fee_gst_rate": fee_gst_rate,
        "entry_fee_type": entry_fee_type,
        "exit_fee_type": exit_fee_type,
    }


def _rate_from_pct_or_rate(
    *,
    pct: Any,
    rate: Any,
    default: Decimal,
    allow_negative: bool = False,
) -> Decimal:
    if pct not in {None, ""}:
        return _pct_to_rate(pct, allow_negative=allow_negative)
    return _normalize_rate(_decimal_param(rate, default), allow_negative=allow_negative)


def _legacy_fee_rate(params: dict[str, Any]) -> Decimal | None:
    if params.get("fee_type") in {"inr_maker", "inr_taker"}:
        return _fee_rate_param(params, Decimal("0"))
    if params.get("fee_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_pct"))
    if params.get("fee_rate") not in {None, ""}:
        return _normalize_fee_rate(_decimal_param(params.get("fee_rate"), Decimal("0")))
    return None


def _fee_type_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError("fee type must be maker or taker.")
    return normalized


def _gst_rate_param(params: dict[str, Any], default: Decimal) -> Decimal:
    if params.get("fee_gst_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_gst_pct"))
    value = _decimal_param(params.get("fee_gst_rate"), default)
    if value < 0:
        raise ValueError("fee_gst_rate cannot be negative.")
    return value


def _atr_take_profit_mode_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"fixed", "entry_atr", "ratchet", "trailing_atr", "none"}:
        raise ValueError(
            "atr_take_profit_mode must be fixed, entry_atr, ratchet, trailing_atr, or none."
        )
    return normalized


def _atr_policy_mode_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"manual", "router"}:
        raise ValueError("atr_policy_mode must be manual or router.")
    return normalized


def _fee_pct_to_rate(value: Any) -> Decimal:
    return _pct_to_rate(value, allow_negative=False)


def _pct_to_rate(value: Any, *, allow_negative: bool) -> Decimal:
    fee_pct = _decimal_param(value, Decimal("0"))
    if fee_pct < 0 and not allow_negative:
        raise ValueError("fee_pct cannot be negative.")
    return fee_pct / Decimal("100")


def _normalize_fee_rate(value: Decimal) -> Decimal:
    return _normalize_rate(value, allow_negative=False)


def _normalize_rate(value: Decimal, *, allow_negative: bool) -> Decimal:
    if value < 0:
        if allow_negative:
            return value
        raise ValueError("fee_rate cannot be negative.")
    if value >= Decimal("0.01"):
        return value / Decimal("100")
    return value


def _int_param(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value: {value}") from exc


def _positive_int_param(value: Any, default: int, *, name: str) -> int:
    parsed = _int_param(value, default)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive.")
    return parsed


def _nonnegative_int_param(value: Any, default: int, *, name: str) -> int:
    parsed = _int_param(value, default)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative.")
    return parsed


def _bool_param(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")
