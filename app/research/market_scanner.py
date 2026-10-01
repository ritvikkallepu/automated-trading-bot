"""Read-only cross-venue research. Scores are heuristics, never order approvals."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import math
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode

from app.data.candle_builder import OHLCVCandle, interval_to_ms
from app.data.indicators import average_true_range, exponential_moving_average
from app.exchange.coindcx_rest import UrllibTransport


class DataUnavailable(ValueError):
    pass


class ScanStopped(DataUnavailable):
    pass


class RateLimited(ScanStopped):
    pass


def number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise DataUnavailable("Invalid numeric value")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise DataUnavailable("Invalid or non-finite market value")
    return result


@dataclass(frozen=True)
class ScanConfig:
    max_pairs: int = 20
    min_quote_volume: float = 10_000_000
    min_volume_ratio: float = 0.5
    min_score: float = 65
    max_extension_atr: float = 2.5
    max_spread_bps: float = 20
    max_divergence_bps: float = 50
    min_depth_usdt: float = 5000
    funding_warning_pct: float = 0.05
    round_trip_cost_pct: float = 0.20

    @classmethod
    def parse(cls, values: dict) -> "ScanConfig":
        bounds = {
            "max_pairs": (1, 50), "min_quote_volume": (0, 1e12),
            "min_volume_ratio": (0, 10), "min_score": (0, 100),
            "max_extension_atr": (0.1, 20), "max_spread_bps": (0.1, 1000),
            "max_divergence_bps": (0.1, 1000), "min_depth_usdt": (0, 1e9),
            "funding_warning_pct": (0.001, 10),
            "round_trip_cost_pct": (0, 5),
        }
        if set(values) - set(bounds):
            raise ValueError("Unknown research setting")
        parsed = {}
        for key, value in values.items():
            value = number(value)
            lo, hi = bounds[key]
            if not lo <= value <= hi or (key == "max_pairs" and not value.is_integer()):
                raise ValueError(f"{key} must be between {lo} and {hi}")
            parsed[key] = int(value) if key == "max_pairs" else value
        return cls(**parsed)


class PublicMarketData:
    """Fixed public GET endpoints only; no account, order, or secret access."""

    def __init__(self, *, cancelled: Callable[[], bool] = lambda: False,
                 transport=None, budget_seconds: float = 240):
        self.transport = transport or UrllibTransport(timeout_seconds=8)
        self.cancelled = cancelled
        self.deadline = time.monotonic() + budget_seconds
        self.last_request = 0.0

    def check(self):
        if self.cancelled() or time.monotonic() >= self.deadline:
            raise ScanStopped("Scan cancelled or time budget reached")

    def _get(self, base: str, path: str, **params):
        self.check()
        time.sleep(max(0, 0.2 - (time.monotonic() - self.last_request)))
        self.check()
        self.last_request = time.monotonic()
        url = base + path + ("?" + urlencode(params) if params else "")
        try:
            response = self.transport.request("GET", url, {"Accept": "application/json", "User-Agent": "CoinDCXResearch/1.0 (public-market-data)"})
        except Exception as exc:
            raise DataUnavailable(f"{base}: public feed connection failed") from exc
        if response.status_code in (418, 429):
            raise RateLimited("Provider rate limit reached; scan stopped. Retry later.")
        if response.status_code != 200:
            raise DataUnavailable(f"{base}: HTTP {response.status_code}")
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise DataUnavailable("Provider returned invalid JSON") from exc

    def binance(self, path: str, **params):
        allowed = {"/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr",
                   "/fapi/v1/klines", "/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo",
                   "/fapi/v1/depth", "/futures/data/openInterestHist"}
        if path not in allowed:
            raise ValueError("Not a research endpoint")
        return self._get("https://fapi.binance.com", path, **params)

    def dcx_pairs(self):
        return self._get("https://api.coindcx.com",
                         "/exchange/v1/derivatives/futures/data/active_instruments",
                         **{"margin_currency_short_name[]": "INR"})

    def dcx_book(self, pair: str):
        if not re.fullmatch(r"B-[A-Z0-9]+_USDT", pair):
            raise ValueError("Invalid pair")
        return self._get("https://public.coindcx.com", f"/market_data/v3/orderbook/{pair}-futures/50")


def binance_universe(exchange: dict) -> list[dict]:
    if not isinstance(exchange, dict) or not isinstance(exchange.get("symbols"), list):
        raise DataUnavailable("Invalid exchange instrument catalogue")
    rows = []
    for row in exchange["symbols"]:
        if (row.get("status") == "TRADING" and row.get("contractType") == "PERPETUAL"
                and row.get("quoteAsset") == "USDT" and row.get("marginAsset") == "USDT"):
            base = row.get("baseAsset", "")
            if re.fullmatch(r"[A-Z0-9]+", base) and row.get("symbol") == base + "USDT":
                rows.append({"pair": f"B-{base}_USDT", "symbol": row["symbol"]})
    return rows


def match_universe(exchange: dict, pairs: list[str]) -> tuple[list[dict], list[dict]]:
    if not isinstance(pairs, list):
        raise DataUnavailable("Invalid CoinDCX instrument catalogue")
    symbols = {}
    for row in binance_universe(exchange):
        base = row["pair"].removeprefix("B-").removesuffix("_USDT")
        symbols.setdefault(base, []).append(row["symbol"])
    matched, excluded = [], []
    for pair in sorted(set(pairs)):
        match = re.fullmatch(r"B-([A-Z0-9]+)_USDT", pair)
        found = symbols.get(match[1], []) if match else []
        if len(found) == 1:
            matched.append({"pair": pair, "symbol": found[0]})
        else:
            excluded.append({"pair": pair, "reason": "No unambiguous matching USDT perpetual; no multiplier/alias guessed"})
    return matched, excluded


def candle_features(rows: list, symbol: str, interval: str, as_of_ms: int) -> dict:
    duration = interval_to_ms(interval)
    candles = {}
    for row in rows:
        opened, closed = int(row[0]), int(row[6])
        if closed >= as_of_ms:
            continue
        if closed != opened + duration - 1 or opened % duration:
            raise DataUnavailable(f"{interval}: invalid candle boundary")
        prices = [number(v, positive=True) for v in row[1:5]]
        op, high, low, close = prices
        volume = number(row[5])
        if volume < 0 or low > min(op, close) or high < max(op, close) or high < low:
            raise DataUnavailable(f"{interval}: invalid OHLCV")
        candle = OHLCVCandle(symbol, interval, opened, closed,
                            *(Decimal(str(p)) for p in prices), Decimal(str(volume)))
        if opened in candles and candles[opened] != candle:
            raise DataUnavailable(f"{interval}: conflicting duplicate candle")
        candles[opened] = candle
    series = [candles[k] for k in sorted(candles)]
    if len(series) < 60:
        raise DataUnavailable(f"{interval}: fewer than 60 closed candles")
    if as_of_ms - series[-1].close_time_ms > duration + 15_000:
        raise DataUnavailable(f"{interval}: stale closed candles")
    if any(b.open_time_ms - a.open_time_ms != duration for a, b in zip(series, series[1:])):
        raise DataUnavailable(f"{interval}: missing candle(s)")
    closes = [c.close for c in series]
    fast = exponential_moving_average(closes, 21)
    slow = exponential_moving_average(closes, 55)
    atr = average_true_range(series, 14)[-1]
    if atr is None or atr <= 0:
        raise DataUnavailable(f"{interval}: ATR unavailable")
    trend = (1 if closes[-1] > fast[-1] > slow[-1] and slow[-1] > slow[-4] else
             -1 if closes[-1] < fast[-1] < slow[-1] and slow[-1] < slow[-4] else 0)
    baseline = sum(c.volume for c in series[-21:-1]) / 20
    if baseline <= 0:
        raise DataUnavailable(f"{interval}: volume baseline unavailable")
    return {"trend": trend, "close": float(closes[-1]), "ema21": float(fast[-1]),
            "ema55": float(slow[-1]), "atr": float(atr),
            "extension_atr": float((closes[-1] - fast[-1]) / atr),
            "volume_ratio": float(series[-1].volume / baseline),
            "return_pct": float((closes[-1] / closes[-2] - 1) * 100),
            "close_time_ms": series[-1].close_time_ms,
            "closes": [float(v) for v in closes[-40:]]}


def book_features(book: dict, now_ms: int) -> dict:
    timestamp = book.get("ts", book.get("T", book.get("E")))
    if timestamp is None:
        raise DataUnavailable("Order book timestamp unavailable")
    timestamp = number(timestamp, positive=True)
    if timestamp < 1e12:
        timestamp *= 1000
    if not -5000 <= now_ms - timestamp <= 30_000:
        raise DataUnavailable("Order book is stale or future-dated")
    sides = []
    for key, reverse in (("bids", True), ("asks", False)):
        raw = book.get(key, [])
        levels = raw.items() if isinstance(raw, dict) else raw
        levels = sorted([(number(p, positive=True), number(q, positive=True)) for p, q in levels], reverse=reverse)
        if not levels:
            raise DataUnavailable("Empty order book")
        sides.append(levels)
    bids, asks = sides
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask:
        raise DataUnavailable("Crossed or locked order book")
    mid = (bid + ask) / 2
    bid_depth = sum(p * q for p, q in bids if p >= mid * 0.999)
    ask_depth = sum(p * q for p, q in asks if p <= mid * 1.001)
    return {"mid": mid, "spread_bps": (ask - bid) / mid * 10000,
            "bid_depth_usdt": bid_depth, "ask_depth_usdt": ask_depth,
            "timestamp_ms": int(timestamp)}


def rank_candidate(features: dict, funding_pct: float | None, oi_change: float | None,
                   config: ScanConfig) -> dict:
    weights = {"5m": 0.15, "15m": 0.2, "1h": 0.3, "4h": 0.35}
    scores, breakdown, reasons = {}, {}, []
    for side, sign in (("long", 1), ("short", -1)):
        alignment = sum(w for tf, w in weights.items() if features[tf]["trend"] == sign)
        extension = max(0, sign * features["5m"]["extension_atr"], sign * features["15m"]["extension_atr"])
        components = {
            "trend_alignment": 70 * alignment,
            "volume": 15 * min(features["5m"]["volume_ratio"] / 1.5, 1),
            "entry_direction": 15 if sign * features["5m"]["return_pct"] > 0 else 0,
            "extension_penalty": -min(30, max(0, extension - 1) * 10),
            "funding_penalty": -10 if funding_pct is not None and sign * funding_pct > config.funding_warning_pct else 0,
        }
        scores[side] = round(max(0, min(100, sum(components.values()))), 2)
        breakdown[side] = components
    side = "long" if scores["long"] >= scores["short"] else "short"
    sign = 1 if side == "long" else -1
    if any(features[tf]["trend"] != sign for tf in ("1h", "4h")):
        reasons.append("1h and 4h trends are not aligned")
    if any(features[tf]["trend"] == -sign for tf in ("5m", "15m")):
        reasons.append("Lower-timeframe trend opposes the research direction")
    extension = max(sign * features[tf]["extension_atr"] for tf in ("5m", "15m"))
    if extension > config.max_extension_atr:
        reasons.append(f"Late chase: extension {extension:.2f} ATR > {config.max_extension_atr:g}")
    if features["5m"]["volume_ratio"] < config.min_volume_ratio:
        reasons.append("Closed 5m volume is below the research threshold")
    if scores[side] < config.min_score:
        reasons.append("Heuristic score is below the research threshold")
    price_direction = features["1h"]["return_pct"]
    oi_context = "Unavailable"
    if oi_change is not None:
        if abs(oi_change) < 0.1:
            oi_context = "Open interest broadly unchanged"
        elif oi_change > 0:
            oi_context = "Price rising / OI rising" if price_direction > 0 else "Price falling / OI rising" if price_direction < 0 else "Price flat / OI rising"
        else:
            oi_context = "Price rising / OI falling: possible short covering" if price_direction > 0 else "Price falling / OI falling: possible deleveraging" if price_direction < 0 else "Price flat / OI falling"
    return {"direction": side, "bucket": "avoid" if reasons else side,
            "score": scores[side], "scores": scores, "breakdown": breakdown,
            "reasons": reasons, "oi_context": oi_context}


class MarketScanner:
    def __init__(self, data=None):
        self.data = data or PublicMarketData()

    def scan(self, config: ScanConfig, progress: Callable[[str], None] = lambda _: None) -> dict:
        data = self.data
        as_of = int(data.binance("/fapi/v1/time")["serverTime"])
        anchor = time.monotonic()
        now = lambda: as_of + int((time.monotonic() - anchor) * 1000)
        progress("Loading Binance perpetuals and CoinDCX availability")
        exchange = data.binance("/fapi/v1/exchangeInfo")
        warnings = []
        coindcx_catalogue_status = "available"
        try:
            matched, excluded = match_universe(exchange, data.dcx_pairs())
        except ScanStopped:
            raise
        except Exception as exc:
            # Continue with exact Binance symbols, but never call them CoinDCX-verified.
            matched, excluded = binance_universe(exchange), []
            coindcx_catalogue_status = "unavailable"
            warnings.append(
                f"CoinDCX active-instrument catalogue unavailable: {exc}. "
                "Binance research is shown, but CoinDCX execution is unverified."
            )
        tickers = {r["symbol"]: r for r in data.binance("/fapi/v1/ticker/24hr")}
        liquid = []
        for row in matched:
            try:
                ticker = tickers[row["symbol"]]
                volume = number(ticker["quoteVolume"])
                if abs(now() - number(ticker["closeTime"])) > 120_000:
                    raise DataUnavailable("Stale 24h ticker")
                if volume < config.min_quote_volume:
                    raise DataUnavailable("Below minimum Binance 24h quote volume")
                liquid.append({**row, "quote_volume_usdt": volume})
            except (KeyError, ValueError, TypeError) as exc:
                excluded.append({"pair": row["pair"], "reason": str(exc)})
        liquid.sort(key=lambda r: (-r["quote_volume_usdt"], r["pair"]))
        selected = liquid[:config.max_pairs]
        excluded.extend({"pair": r["pair"], "reason": "Outside this scan's liquidity-ranked pair limit"} for r in liquid[config.max_pairs:])
        funding, intervals = {}, {}
        try:
            funding = {r["symbol"]: r for r in data.binance("/fapi/v1/premiumIndex")}
            intervals = {r["symbol"]: r.get("fundingIntervalHours") for r in data.binance("/fapi/v1/fundingInfo")}
        except ScanStopped:
            raise
        except Exception:
            warnings.append("Funding data incomplete; missing values do not receive a fabricated score")
        rows = []
        for index, row in enumerate(selected):
            symbol, pair = row["symbol"], row["pair"]
            progress(f"Scanning {index + 1}/{len(selected)}: {pair}")
            result = {
                **row,
                "bucket": "avoid",
                "score": None,
                "reasons": [],
                "warnings": [],
                "features": {},
                "execution_check": {
                    "status": "unverified",
                    "reasons": ["CoinDCX active-instrument catalogue was unavailable"],
                },
            }
            try:
                features = {tf: candle_features(data.binance("/fapi/v1/klines", symbol=symbol, interval=tf, limit=100, endTime=as_of), symbol, tf, as_of)
                            for tf in ("5m", "15m", "1h", "4h")}
                result["features"] = features
                rate = None
                fr = funding.get(symbol, {})
                try:
                    if abs(now() - number(fr["time"])) > 300_000:
                        raise DataUnavailable("Stale funding data")
                    rate = number(fr["lastFundingRate"]) * 100
                except (KeyError, ValueError, TypeError):
                    result["warnings"].append("Funding unavailable or stale")
                result.update(funding_pct=rate, funding_interval_hours=intervals.get(symbol),
                              funding_timestamp_ms=fr.get("time"))
                oi_change = None
                try:
                    # Align OI observations to the same two hourly closes as price context.
                    end = features["1h"]["close_time_ms"] + 1
                    oi = data.binance("/futures/data/openInterestHist", symbol=symbol, period="1h", limit=3, endTime=end)
                    points = {int(r["timestamp"]): number(r["sumOpenInterest"], positive=True) for r in oi}
                    oi_change = (points[end] / points[end - 3_600_000] - 1) * 100
                except ScanStopped:
                    raise
                except Exception:
                    result["warnings"].append("Aligned hourly open interest unavailable")
                result["oi_change_pct"] = oi_change
                result.update(rank_candidate(features, rate, oi_change, config))
                if rate is None or oi_change is None:
                    result["reasons"].append("Incomplete funding/OI coverage; research candidate withheld")
                if result["reasons"]:
                    result["bucket"] = "avoid"
                if coindcx_catalogue_status == "available":
                    try:
                        bb = data.binance("/fapi/v1/depth", symbol=symbol, limit=20)
                        db = data.dcx_book(pair)
                        bn, dcx = book_features(bb, now()), book_features(db, now())
                        divergence = abs(dcx["mid"] / bn["mid"] - 1) * 10000
                        venue_reasons = []
                        if abs(bn["timestamp_ms"] - dcx["timestamp_ms"]) > 15_000:
                            venue_reasons.append("Cross-venue books are not time-aligned")
                        if dcx["spread_bps"] > config.max_spread_bps:
                            venue_reasons.append("CoinDCX spread exceeds research limit")
                        depth = dcx["ask_depth_usdt"] if result["direction"] == "long" else dcx["bid_depth_usdt"]
                        if depth < config.min_depth_usdt:
                            venue_reasons.append("CoinDCX visible depth within 10 bps is below research limit")
                        if divergence > config.max_divergence_bps:
                            venue_reasons.append("CoinDCX/Binance midpoint divergence exceeds research limit")
                        result["execution_check"] = {
                            "status": "blocked" if venue_reasons else "verified",
                            "reasons": venue_reasons,
                            "binance": bn,
                            "coindcx": dcx,
                            "divergence_bps": divergence,
                        }
                    except ScanStopped:
                        raise
                    except Exception as exc:
                        result["execution_check"] = {
                            "status": "unverified",
                            "reasons": [f"CoinDCX execution check unavailable: {exc}"],
                        }
            except ScanStopped:
                raise
            except Exception as exc:
                result["bucket"] = "avoid"
                result["reasons"].append(f"Market data unavailable: {exc}")
            rows.append(result)
        rows.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1), r["pair"]))
        return {"schema_version": 2, "model": "transparent-heuristic-v1", "mode": "research_only",
                "ai_enabled": False, "as_of_ms": as_of, "completed_ms": now(),
                "config": asdict(config), "matched_count": len(matched), "scanned_count": len(rows),
                "coindcx_catalogue_status": coindcx_catalogue_status,
                "rows": rows, "excluded": sorted(excluded, key=lambda r: r["pair"]), "warnings": warnings,
                "sources": ["Binance USDT perpetual public REST", "CoinDCX INR futures public REST"]}
