"""Continuous, independent data research. Never starts or changes a trading loop."""
from copy import deepcopy
from pathlib import Path
import threading
import time
import uuid
import os

from app.research.market_scanner import DataUnavailable, ScanStopped, binance_universe, number
from .analysis import (aggregate, closed_market, concentration_analysis, lagging_pairs,
                       oi_analysis, whale_analysis)
from .config import load_config, config_id
from .feeds import ResearchData
from .levels import calculate_levels
from .replay import replay_stored, summary
from .store import HistoryStore
from .ranking import public_rank, timing, rank_rows, ranking_changes
from .setups import track_publication, setup_status
from .validation import evaluate_stored, validation_summary


def unavailable(reason):
    return {"status": "unavailable", "reason": reason}


class ResearchEngine:
    def __init__(self, store, data, cfg):
        self.store, self.data, self.cfg = store, data, cfg
        self.cid = config_id(cfg)

    def auxiliary(self, family, symbol, now_ms, refresh):
        cfg, store = self.cfg, self.store
        key = {"whale": "whale_seconds", "holder": "holder_seconds"}[family]
        old = store.latest(family, symbol, self.cid)
        if old and 0 <= now_ms-old["fetched_ms"] < cfg["schedules"][key]*1000:
            return old
        asset = cfg["assets"].get(symbol, {})
        try:
            raw = refresh(asset)
            value = {"status": "available", "raw": raw}
        except ScanStopped:
            raise
        except Exception as exc:
            # DataUnavailable messages from our connectors are sanitized. Never echo arbitrary HTTP errors.
            value = unavailable(str(exc) if isinstance(exc, DataUnavailable) else "Provider payload could not be validated")
        oid = store.observe(family, symbol, now_ms, self.cid, value)
        return {"id": oid, "fetched_ms": now_ms, "data": value,
                "previous": old["data"].get("raw") if old else None}

    def cycle(self, progress=lambda _: None):
        cfg, data, store = self.cfg, self.data, self.store
        now_ms = int(data.binance("/fapi/v1/time")["serverTime"])
        anchor = time.monotonic()
        now = lambda: now_ms+int((time.monotonic()-anchor)*1000)
        catalogue = data.binance("/fapi/v1/exchangeInfo")
        universe = binance_universe(catalogue)
        warnings = []
        catalogue_status = {"status": "unavailable", "verified_ms": None, "count": 0}
        try:
            dcx = data.dcx_pairs()
            if not isinstance(dcx, list) or not dcx or any(not isinstance(p, str) for p in dcx):
                raise DataUnavailable("Invalid CoinDCX catalogue")
            dcx = set(dcx)
            catalogue_status = {"status": "verified", "verified_ms": now(), "count": len(dcx)}
            store.set_state("coindcx_catalogue", catalogue_status)
        except ScanStopped:
            raise
        except Exception as exc:
            dcx = None
            reason = str(exc) if isinstance(exc, DataUnavailable) else "Invalid catalogue response"
            catalogue_status.update(reason=reason, last_success=store.state("coindcx_catalogue"))
            warnings.append("CoinDCX catalogue unavailable; listing status unverified: " + reason)
        ticker_raw = data.binance("/fapi/v1/ticker/24hr")
        if not isinstance(ticker_raw, list):
            raise DataUnavailable("Invalid Binance ticker catalogue")
        tickers = {r["symbol"]: r for r in ticker_raw}
        try:
            funding = {r["symbol"]: r for r in data.binance("/fapi/v1/premiumIndex")}
        except Exception:
            funding = {}
            warnings.append("Funding feed unavailable")
        catalog_id = store.observe("universe", "all", now_ms, self.cid,
                                   {"catalogue": catalogue, "tickers": ticker_raw, "coindcx": sorted(dcx) if dcx is not None else None})
        eligible, rows = [], {}
        for item in universe:
            symbol = item["symbol"]
            row = {**item, "sector": cfg["sectors"].get(symbol, "Unclassified"),
                   "coindcx_listed": item["pair"] in dcx if dcx is not None else None,
                   "direction": "none", "tier": "unscanned", "composite_score": None,
                   "score_coverage_pct": 0, "last_updated": None, "plan": {},
                   "oi_state": "unavailable", "whale_flow_dir": "unavailable", "concentration_risk": None,
                   "lagging_flag": False, "evidence": {"universe": catalog_id}, "reasons": []}
            rows[symbol] = row
            try:
                ticker = tickers[symbol]
                if not -5000 <= now()-int(ticker["closeTime"]) <= 120_000:
                    raise DataUnavailable("Stale ticker")
                volume = number(ticker["quoteVolume"])
                if volume < cfg["universe"]["min_quote_volume"]:
                    row["reasons"] = ["Below configured 24h liquidity floor"]
                    continue
                row["quote_volume"] = volume
                previous = store.latest("market", symbol)
                eligible.append((symbol, volume, previous["fetched_ms"] if previous else 0))
            except (KeyError, TypeError, ValueError):
                row["reasons"] = ["Ticker missing, stale or invalid"]
        eligible.sort(key=lambda x: (-x[1], x[0]))
        budget, priority = cfg["universe"]["deep_pairs_per_cycle"], cfg["universe"]["priority_pairs"]
        selected = eligible[:priority]
        selected += sorted(eligible[priority:], key=lambda x: (x[2], -x[1], x[0]))[:budget-len(selected)]
        selected_symbols = {r[0] for r in selected}
        for symbol, _, _ in eligible:
            if symbol not in selected_symbols:
                rows[symbol]["reasons"] = ["Queued for rotating deep scan; not a fresh recommendation"]
        markets, source_data, errors = {}, {}, 0
        for index, (symbol, _, _) in enumerate(selected):
            progress(f"Analysing {index+1}/{len(selected)}: {symbol}")
            row = rows[symbol]
            try:
                market_interval = cfg.get("market_interval", "15m")
                raw = {"candles": data.binance("/fapi/v1/klines", symbol=symbol, interval=market_interval, limit=100),
                       "funding": funding.get(symbol)}
                market = closed_market(raw, symbol, now(), market_interval)
                markets[symbol] = market
                try:
                    raw["oi"] = data.binance("/futures/data/openInterestHist", symbol=symbol, period=market_interval,
                                             limit=60 if market_interval == "5m" else 20, endTime=now())
                    oi = oi_analysis(raw, market, cfg, now())
                except ScanStopped:
                    raise
                except Exception as exc:
                    oi = unavailable(str(exc) if isinstance(exc, DataUnavailable) else "OI payload invalid")
                oid = store.observe("market", symbol, now(), self.cid,
                                    {"status": "available", "raw": raw, "oi": oi, "market_interval": market_interval,
                                     "closed_through_ms": market["features"]["close_time_ms"]})
                row["evidence"]["market"] = oid
                whale = self.auxiliary("whale", symbol, now(), lambda asset: data.whale(asset, now()-86400_000, now()))
                holder = self.auxiliary("holder", symbol, now(), data.holders)
                row["evidence"].update(whale=whale["id"], holder=holder["id"])
                try:
                    wf = whale_analysis(whale["data"]["raw"], cfg, now()) if whale["data"]["status"] == "available" else whale["data"]
                except Exception:
                    wf = unavailable("Whale evidence invalid")
                try:
                    hf = concentration_analysis(holder["data"]["raw"], cfg, now(), holder.get("previous")) if holder["data"]["status"] == "available" else holder["data"]
                except Exception:
                    hf = unavailable("Holder evidence invalid or stale")
                row.update(oi_state=oi.get("state", "unavailable"), whale_flow_dir=wf.get("flow_direction", "unavailable"),
                           concentration_risk=hf.get("risk_score"), last_updated=now(),
                           analysis={"oi": oi, "whale": wf, "holder": hf}, reference_price=market["features"]["close"])
                row.update(public_rank=public_rank(market, oi, cfg), market_close_ms=market["features"]["close_time_ms"])
                source_data[symbol] = (oi, wf, hf)
            except ScanStopped:
                warnings.append("Cycle cancelled or time budget reached; remaining rows stay unscanned")
                break
            except Exception as exc:
                reason = str(exc) if isinstance(exc, DataUnavailable) else "Market payload could not be validated"
                row["reasons"] = [reason]
                errors += 1
                store.observe("market", symbol, now(), self.cid, unavailable(reason))
        laggards = lagging_pairs(markets, cfg)
        if not source_data:
            raise DataUnavailable("No valid market data completed; previous publication retained")
        for symbol, signals in source_data.items():
            row = rows[symbol]
            plan = calculate_levels(markets[symbol], symbol, cfg, laggards.get(symbol))
            row.update(aggregate(*signals, plan, cfg))
            row.update(plan=plan, direction="long" if row["tier"] != "avoid" else "avoid",
                       lagging_flag=symbol in laggards, laggard=laggards.get(symbol))
            row.update({k: plan.get(k) for k in ("entry", "stop_loss", "tp1", "tp2", "tp3", "risk_reward")})
            row["timing"] = timing(row, cfg)
        missing = sorted(dcx - {r["pair"] for r in universe}) if dcx else []
        for pair in missing:
            rows[pair] = {"pair": pair, "symbol": pair, "tier": "unscanned", "direction": "none",
                          "composite_score": None, "score_coverage_pct": 0, "plan": {}, "last_updated": None,
                          "coindcx_listed": True, "reasons": ["No exact Binance USDT perpetual mapping"],
                          "evidence": {"universe": catalog_id}}
        for row in rows.values():
            if row["tier"] == "unscanned" and not row["reasons"]:
                row["reasons"] = ["Not completed within this cycle"]
        ranked = rank_rows(rows.values())
        ranking_changes(ranked, store.latest_publication(), cfg)
        snapshot = {"id": uuid.uuid4().hex, "schema_version": 2, "mode": "research_only",
                    "published_ms": now(), "as_of_ms": now_ms, "config_id": self.cid, "config": cfg,
                    "rows": ranked, "warnings": warnings,
                    "coindcx_catalogue": catalogue_status,
                    "coverage": {"universe": len(rows), "eligible": len(eligible), "selected": len(selected),
                                 "analysed": len(source_data), "failed": errors, "reference": "Binance USDT perpetuals"},
                    "providers": {"market": "Binance public", "whale": "CoinGlass optional", "holder": "Bubblemaps optional"}}
        replay_stored(store, markets, now())
        track_publication(store, snapshot)
        replay_stored(store, markets, now())
        evaluate_stored(store, now())
        return snapshot


class ContinuousResearchService:
    def __init__(self, path, *, config_path=None, autostart=False, data_factory=ResearchData):
        self.store = HistoryStore(path)
        self.config_path, self.data_factory = config_path, data_factory
        self._lock, self._stop, self._wake = threading.Lock(), threading.Event(), threading.Event()
        self._thread = None
        self._snapshot = self.store.latest_publication()
        self._fresh_run = False
        self._message, self._error, self._busy, self._next_ms = "Stopped", None, False, None
        self.owner = uuid.uuid4().hex
        self._last_started = 0.0
        self._cycle_started_ms = None
        self._queued = False
        self._interval_seconds = (self._snapshot or {}).get("config", {}).get("schedules", {}).get("market_seconds", 900)
        self._market_interval = (self._snapshot or {}).get("config", {}).get("market_interval", "15m")
        try:
            configured = load_config(config_path)
            self._interval_seconds = configured["schedules"]["market_seconds"]
            self._market_interval = configured["market_interval"]
        except (ValueError, TypeError, OSError, KeyError) as exc:
            self._error = f"Research config invalid: {type(exc).__name__}"
        self._research_stats = self._statistics()
        if autostart:
            try:
                if load_config(config_path)["autostart"] and self.store.state("research_enabled", True):
                    self.start()
            except (ValueError, TypeError, OSError, KeyError) as exc:
                self._error = f"Research config invalid: {type(exc).__name__}"

    def status(self):
        from app.research.wallets.status import public_status
        wallet_evidence = public_status(self.store.path.with_name("wallets.sqlite3"))
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
            snapshot = deepcopy(self._snapshot)
            now_ms = int(time.time()*1000)
            cooldown = max(0.0, 60-(time.monotonic()-self._last_started)) if running and self._last_started else 0
            stale = not self._fresh_run or bool(self._error) or not running or not snapshot or time.time()*1000-snapshot["published_ms"] > snapshot["config"]["schedules"]["market_seconds"]*2000
            return {"running": running, "busy": self._busy, "message": self._message,
                    "error": self._error, "next_cycle_ms": self._next_ms,
                    "server_time_ms": now_ms, "cycle_started_ms": self._cycle_started_ms,
                    "last_completed_ms": snapshot.get("published_ms") if snapshot else None,
                    "interval_seconds": self._interval_seconds, "market_interval": self._market_interval, "queued": self._queued,
                    "refresh_available_ms": now_ms + int(cooldown*1000),
                    "stale": stale, "snapshot": snapshot, **deepcopy(self._research_stats),
                    "health": self.store.state("research_health", {}),
                    "wallet_evidence": wallet_evidence,
                    "storage": self.store.storage_status(),
                    "alerts": {c: self.store.state(f"alert_status:{c}", {"status": "no alerts sent"}) for c in ("telegram", "discord")}}

    def _statistics(self):
        # History summaries change at cycle completion, not on every browser poll.
        replay = summary(self.store.replay_results())
        validation = validation_summary(self.store.validation_results())
        replay.pop("results", None)
        validation.pop("results", None)
        return {"replay": replay, "setups": setup_status(self.store), "validation": validation}

    def start(self):
        cfg = load_config(self.config_path)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.store.set_state("research_enabled", True)
            self._stop.clear()
            self._wake.clear()
            self._fresh_run = False
            self._queued = True
            self._message, self._error, self._next_ms = "Scan queued", None, None
            self._interval_seconds = cfg["schedules"]["market_seconds"]
            self._market_interval = cfg["market_interval"]
            self._thread = threading.Thread(target=self._run, name="continuous-research", daemon=True)
            self._thread.start()

    def refresh(self):
        was_running = bool(self._thread and self._thread.is_alive())
        self.start()
        with self._lock:
            if was_running and not self._busy and not self._queued and time.monotonic()-self._last_started >= 60:
                self._queued = True
                self._message, self._next_ms = "Scan queued", None
                self._wake.set()

    def close(self, *, pause=False):
        if pause:
            self.store.set_state("research_enabled", False)
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=35)

    def _progress(self, message):
        with self._lock:
            self._message = message

    def _run(self):
        while not self._stop.is_set():
            delay = 900
            cycle_started = time.monotonic()
            data = None
            claimed = False
            with self._lock:
                self._busy, self._queued, self._error = True, False, None
                self._next_ms = None
                self._cycle_started_ms = int(time.time()*1000)
                self._message = "Starting scan"
            try:
                cfg = load_config(self.config_path)
                delay = cfg["schedules"]["market_seconds"]
                if not self.store.claim(self.owner, int(time.time()*1000)+(cfg["network"]["cycle_budget_seconds"]+60)*1000):
                    raise DataUnavailable("Another research worker owns this history store")
                claimed = True
                self.store.set_state("worker_status", {"cycle_started_ms": self._cycle_started_ms, "phase": "scanning", "owner": self.owner, "process_id": os.getpid()})
                self._last_started = time.monotonic()
                with self._lock:
                    self._interval_seconds = delay
                    self._market_interval = cfg["market_interval"]
                data = self.data_factory(cfg, self._stop)
                if hasattr(data, "cooldowns"):
                    data.cooldowns = self.store.state("provider_cooldowns", {})
                snapshot = ResearchEngine(self.store, data, cfg).cycle(self._progress)
                statistics = self._statistics()
                with self._lock:
                    self._snapshot, self._message = snapshot, "Cycle complete"
                    self._research_stats = statistics
                    self._fresh_run = True
                from .notify import publish_alerts
                publish_alerts(snapshot, self.store, cfg, cancelled=self._stop.is_set)
            except Exception as exc:
                with self._lock:
                    self._error = str(exc) if isinstance(exc, DataUnavailable) else f"Research cycle failed ({type(exc).__name__}); check config or provider data"
                    self._message = "Cycle failed; previous snapshot retained as history"
            finally:
                if data is not None and hasattr(data, "cooldowns"):
                    self.store.set_state("provider_cooldowns", data.cooldowns)
                self.store.release(self.owner)
                if delay == 300:
                    current_ms = int(time.time()*1000)
                    latest_close = current_ms//300_000*300_000
                    started_ms = self._cycle_started_ms or current_ms
                    if latest_close > started_ms//300_000*300_000 and current_ms >= latest_close+2000:
                        delay = 0
                    else:
                        next_ms = (current_ms//300_000+1)*300_000+2000
                        delay = (next_ms-current_ms)/1000
                else:
                    delay = max(60, delay-(time.monotonic()-cycle_started))
                with self._lock:
                    self._busy = False
                    self._next_ms = None if self._stop.is_set() else int(time.time()*1000)+delay*1000
                if claimed:
                    self.store.set_state("worker_status", {"phase": "waiting", "next_cycle_ms": self._next_ms,
                                                            "finished_ms": int(time.time()*1000), "owner": self.owner, "process_id": os.getpid()})
            self._wake.wait(delay)
            self._wake.clear()
        with self._lock:
            self._message, self._next_ms, self._queued = "Stopped", None, False
