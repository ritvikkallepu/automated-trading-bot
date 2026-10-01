"""Forward-only shadow evaluation for immutable market-research snapshots."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import re
from statistics import mean, median
import threading
import time
from typing import Any, Callable

from app.research.market_scanner import DataUnavailable, PublicMarketData, ScanStopped, number


MINUTE_MS = 60_000
HORIZONS_MS = {"15m": 15 * MINUTE_MS, "1h": 60 * MINUTE_MS, "4h": 240 * MINUTE_MS}


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DataUnavailable(f"{path.name}: expected a JSON object")
    return value


def _validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("schema_version") != 2 or snapshot.get("mode") != "research_only":
        raise DataUnavailable("Unsupported research snapshot schema")
    scan_id = snapshot.get("scan_id")
    if not isinstance(scan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise DataUnavailable("Invalid research scan identifier")
    if not isinstance(snapshot.get("rows"), list):
        raise DataUnavailable("Research snapshot rows are missing")
    number(snapshot.get("completed_ms"), positive=True)
    if any("SYNTHETIC" in str(warning).upper() for warning in snapshot.get("warnings", [])):
        raise DataUnavailable("Synthetic snapshots are not eligible for outcome evaluation")


def _entry_open_ms(completed_ms: int) -> int:
    # A recommendation cannot fill inside the minute in which its scan completed.
    return ((completed_ms // MINUTE_MS) + 1) * MINUTE_MS


def _parse_minutes(rows: list, symbol: str, entry_ms: int, now_ms: int) -> dict[int, dict]:
    if not isinstance(rows, list):
        raise DataUnavailable(f"{symbol}: invalid one-minute candle response")
    parsed = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise DataUnavailable(f"{symbol}: malformed one-minute candle")
        opened, closed = int(row[0]), int(row[6])
        if opened < entry_ms or closed >= now_ms:
            continue
        if opened % MINUTE_MS or closed != opened + MINUTE_MS - 1:
            raise DataUnavailable(f"{symbol}: invalid one-minute candle boundary")
        op, high, low, close = (number(value, positive=True) for value in row[1:5])
        if low > min(op, close) or high < max(op, close) or high < low:
            raise DataUnavailable(f"{symbol}: invalid one-minute OHLC")
        candle = {"open": op, "high": high, "low": low, "close": close, "close_time_ms": closed}
        if opened in parsed and parsed[opened] != candle:
            raise DataUnavailable(f"{symbol}: conflicting duplicate one-minute candle")
        parsed[opened] = candle
    return parsed


def _round(value: float) -> float:
    return round(value, 8)


def evaluate_snapshot(snapshot: dict, data=None, existing: dict | None = None) -> dict:
    """Add only newly matured outcomes; previously recorded outcomes never change."""
    _validate_snapshot(snapshot)
    if existing is not None and existing.get("scan_id") != snapshot["scan_id"]:
        raise DataUnavailable("Outcome sidecar does not match its research snapshot")
    existing_records = (existing or {}).get("records", {})
    if not isinstance(existing_records, dict):
        raise DataUnavailable("Outcome sidecar records are invalid")

    data = data or PublicMarketData()
    now_ms = int(number(data.binance("/fapi/v1/time")["serverTime"], positive=True))
    completed_ms = int(number(snapshot["completed_ms"], positive=True))
    if completed_ms > now_ms + 5000:
        raise DataUnavailable("Snapshot completion time is in the future")
    entry_ms = _entry_open_ms(completed_ms)
    cost_pct = number(snapshot.get("config", {}).get("round_trip_cost_pct", 0.20))
    if not 0 <= cost_pct <= 5:
        raise DataUnavailable("Invalid round-trip cost assumption")

    records = deepcopy(existing_records)
    errors = {}
    due = [name for name, duration in HORIZONS_MS.items() if now_ms > entry_ms + duration]
    for row in snapshot["rows"]:
        symbol, pair, direction = row.get("symbol"), row.get("pair"), row.get("direction")
        if not re.fullmatch(r"[A-Z0-9]+USDT", str(symbol)) or not re.fullmatch(r"B-[A-Z0-9]+_USDT", str(pair)):
            errors[str(pair or symbol or "unknown")] = "Invalid pair or symbol"
            continue
        if direction not in {"long", "short"} or row.get("score") is None:
            continue
        pair_records = records.setdefault(pair, {})
        if not isinstance(pair_records, dict):
            errors[pair] = "Existing outcome records are invalid"
            continue
        needed = [name for name in due if name not in pair_records]
        if not needed:
            continue

        try:
            latest_due = max(HORIZONS_MS[name] for name in needed)
            raw = data.binance(
                "/fapi/v1/klines",
                symbol=symbol,
                interval="1m",
                limit=500,
                startTime=entry_ms,
                endTime=entry_ms + latest_due,
            )
            candles = _parse_minutes(raw, symbol, entry_ms, now_ms)
            entry = candles.get(entry_ms)
            if entry is None:
                raise DataUnavailable(f"{symbol}: entry candle is missing")
            entry_price = entry["open"]

            for horizon in needed:
                count = HORIZONS_MS[horizon] // MINUTE_MS
                expected = [entry_ms + index * MINUTE_MS for index in range(count)]
                if any(opened not in candles for opened in expected):
                    raise DataUnavailable(f"{symbol}: incomplete {horizon} one-minute candle history")
                window = [candles[opened] for opened in expected]
                exit_price = window[-1]["close"]
                if direction == "long":
                    gross = (exit_price / entry_price - 1) * 100
                    mfe = (max(candle["high"] for candle in window) / entry_price - 1) * 100
                    mae = (min(candle["low"] for candle in window) / entry_price - 1) * 100
                else:
                    gross = (entry_price - exit_price) / entry_price * 100
                    mfe = (entry_price - min(candle["low"] for candle in window)) / entry_price * 100
                    mae = (entry_price - max(candle["high"] for candle in window)) / entry_price * 100
                net = gross - cost_pct
                execution_status = row.get("execution_check", {}).get("status", "unverified")
                if execution_status not in {"verified", "blocked", "unverified"}:
                    execution_status = "unverified"
                bucket = row.get("bucket") if row.get("bucket") in {"long", "short", "avoid"} else "avoid"
                pair_records[horizon] = {
                    "pair": pair,
                    "symbol": symbol,
                    "direction": direction,
                    "research_bucket": bucket,
                    "score": number(row["score"]),
                    "execution_status_at_scan": execution_status,
                    "recommendation_time_ms": completed_ms,
                    "entry_time_ms": entry_ms,
                    "entry_price": _round(entry_price),
                    "exit_time_ms": window[-1]["close_time_ms"],
                    "exit_price": _round(exit_price),
                    "gross_directional_return_pct": _round(gross),
                    "assumed_round_trip_cost_pct": _round(cost_pct),
                    "net_directional_return_pct": _round(net),
                    "maximum_favourable_excursion_pct": _round(mfe),
                    "maximum_adverse_excursion_pct": _round(mae),
                    "hit_after_costs": net > 0,
                }
        except ScanStopped:
            raise
        except Exception as exc:
            errors[pair] = str(exc)

    return {
        "schema_version": 1,
        "mode": "research_outcomes",
        "scan_id": snapshot["scan_id"],
        "scan_completed_ms": completed_ms,
        "evaluated_ms": now_ms,
        "records": records,
        "errors": errors,
    }


def _aggregate(records: list[dict]) -> dict:
    if not records:
        return {
            "observations": 0,
            "hits": 0,
            "hit_rate_pct": None,
            "avg_net_return_pct": None,
            "median_net_return_pct": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
        }
    net = [number(record["net_directional_return_pct"]) for record in records]
    mfe = [number(record["maximum_favourable_excursion_pct"]) for record in records]
    mae = [number(record["maximum_adverse_excursion_pct"]) for record in records]
    hits = sum(bool(record.get("hit_after_costs")) for record in records)
    return {
        "observations": len(records),
        "hits": hits,
        "hit_rate_pct": _round(hits / len(records) * 100),
        "avg_net_return_pct": _round(mean(net)),
        "median_net_return_pct": _round(median(net)),
        "avg_mfe_pct": _round(mean(mfe)),
        "avg_mae_pct": _round(mean(mae)),
    }


def build_summary(outcome_directory: Path) -> dict:
    by_horizon = {name: [] for name in HORIZONS_MS}
    latest = []
    candidate_signals = set()
    scan_count = 0
    if outcome_directory.exists():
        for path in sorted(outcome_directory.glob("*.json")):
            try:
                outcome = _load_json(path)
                if outcome.get("schema_version") != 1 or outcome.get("mode") != "research_outcomes":
                    continue
                scan_count += 1
                for pair_records in outcome.get("records", {}).values():
                    if not isinstance(pair_records, dict):
                        continue
                    for horizon, record in pair_records.items():
                        if horizon in by_horizon and isinstance(record, dict):
                            by_horizon[horizon].append(record)
                            latest.append({**record, "horizon": horizon, "scan_id": outcome.get("scan_id")})
                            if record.get("research_bucket") in {"long", "short"}:
                                candidate_signals.add((outcome.get("scan_id"), record.get("pair")))
            except Exception:
                continue
    result = {}
    for horizon, records in by_horizon.items():
        candidates = [record for record in records if record.get("research_bucket") in {"long", "short"}]
        result[horizon] = {
            "candidates": _aggregate(candidates),
            "longs": _aggregate([record for record in candidates if record.get("direction") == "long"]),
            "shorts": _aggregate([record for record in candidates if record.get("direction") == "short"]),
            "verified": _aggregate(
                [record for record in candidates if record.get("execution_status_at_scan") == "verified"]
            ),
            "all_scored_baseline": _aggregate(records),
            "avoided": _aggregate([record for record in records if record.get("research_bucket") == "avoid"]),
        }
    latest.sort(key=lambda row: row.get("exit_time_ms", 0), reverse=True)
    observations = sum(result[name]["candidates"]["observations"] for name in HORIZONS_MS)
    return {
        "scan_count": scan_count,
        "candidate_signals": len(candidate_signals),
        "candidate_observations": observations,
        "sample_status": "collecting" if len(candidate_signals) < 200 else "review_ready",
        "by_horizon": result,
        "latest": latest[:50],
    }


class ResearchOutcomeService:
    """Evaluates due snapshots in a bounded daemon, separate from trading."""

    def __init__(
        self,
        scan_directory: Path = Path("data/research_scans"),
        outcome_directory: Path = Path("data/research_outcomes"),
        *,
        data_factory: Callable[[], Any] | None = None,
        interval_seconds: float = 60,
        max_snapshots_per_cycle: int = 2,
        autostart: bool = True,
    ):
        self.scan_directory = scan_directory
        self.outcome_directory = outcome_directory
        self.data_factory = data_factory or PublicMarketData
        self.interval_seconds = interval_seconds
        self.max_snapshots_per_cycle = max_snapshots_per_cycle
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = {
            "status": "idle",
            "progress": "Waiting for eligible snapshots",
            "error": None,
            "summary": build_summary(outcome_directory),
        }
        self._thread = None
        if autostart:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="research-outcomes")
            self._thread.start()

    def status(self) -> dict:
        with self._lock:
            return deepcopy(self._state)

    def request_refresh(self) -> None:
        self._wake.set()

    def refresh_now(self) -> dict:
        with self._lock:
            if self._state["status"] == "running":
                return deepcopy(self._state)
            self._state.update(status="running", progress="Checking frozen research snapshots", error=None)
        processed = 0
        errors = []
        try:
            now_local_ms = int(time.time() * 1000)
            paths = sorted(self.scan_directory.glob("*.json")) if self.scan_directory.exists() else []
            snapshots = []
            for path in paths:
                try:
                    snapshot = _load_json(path)
                    _validate_snapshot(snapshot)
                    snapshots.append((int(snapshot["completed_ms"]), path, snapshot))
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
            for _, path, snapshot in sorted(snapshots, key=lambda item: item[0]):
                if processed >= self.max_snapshots_per_cycle or self._stop.is_set():
                    break
                try:
                    entry_ms = _entry_open_ms(int(snapshot["completed_ms"]))
                    due = [name for name, duration in HORIZONS_MS.items() if now_local_ms > entry_ms + duration]
                    if not due:
                        continue
                    target = self.outcome_directory / f"{snapshot['scan_id']}.json"
                    existing = _load_json(target) if target.exists() else None
                    eligible_pairs = [
                        row.get("pair")
                        for row in snapshot["rows"]
                        if row.get("direction") in {"long", "short"} and row.get("score") is not None
                    ]
                    if not any(
                        name not in (existing or {}).get("records", {}).get(pair, {})
                        for pair in eligible_pairs
                        for name in due
                    ):
                        continue
                    outcome = evaluate_snapshot(snapshot, self.data_factory(), existing)
                    _atomic_json_write(target, outcome)
                    processed += 1
                    errors.extend(f"{pair}: {message}" for pair, message in outcome["errors"].items())
                except ScanStopped:
                    raise
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
            summary = build_summary(self.outcome_directory)
            with self._lock:
                self._state.update(
                    status="complete",
                    progress=f"Evaluated {processed} due snapshot(s)",
                    error="; ".join(errors[:5]) or None,
                    summary=summary,
                )
        except Exception as exc:
            logging.getLogger(__name__).warning("Research outcome refresh failed: %s", exc)
            with self._lock:
                self._state.update(
                    status="error",
                    progress="Outcome evaluation failed",
                    error=str(exc),
                    summary=build_summary(self.outcome_directory),
                )
        return self.status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_now()
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
