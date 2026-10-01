"""Validated, reloadable research configuration; credentials never enter snapshots."""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from .ranking import DEFAULTS as RANKING_DEFAULTS

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "research.json"


def load_config(path=None):
    cfg = json.loads(Path(path or os.environ.get("RESEARCH_CONFIG_PATH") or DEFAULT_PATH).read_text(encoding="utf-8"))
    cfg.setdefault("ranking", deepcopy(RANKING_DEFAULTS))
    cfg.setdefault("market_interval", "15m")
    required = {"schema_version", "autostart", "market_interval", "schedules", "universe", "network", "weights",
                "thresholds", "oi_scores", "levels", "laggards", "sectors", "pair_overrides",
                "assets", "providers", "alerts", "ranking"}
    if set(cfg) != required or cfg["schema_version"] != 1:
        raise ValueError("Invalid research config sections or schema version")
    if cfg["market_interval"] not in {"5m", "15m"}:
        raise ValueError("Research market interval must be 5m or 15m")
    section_keys = {
        "schedules": "market_seconds whale_seconds holder_seconds",
        "universe": "min_quote_volume deep_pairs_per_cycle priority_pairs",
        "network": "requests_per_minute timeout_seconds retries backoff_seconds rate_limit_wait_seconds cycle_budget_seconds",
        "thresholds": "high_conviction_score whale_usd flow_imbalance oi_change_pct price_change_pct oi_outlier_pct flat_price_pct funding_warning_pct top_cluster_risk_pct top_n_clusters",
        "oi_scores": "price_up_oi_up price_down_oi_up price_up_oi_down price_down_oi_down flat funding_penalty",
        "levels": "trigger atr_multiplier min_stop_atr swing_lookback min_rr tp1_r tp2_r tp3_r scale_out max_extension_atr trail_atr plan_hours fee_bps_per_side slippage_bps",
        "laggards": "min_correlation min_samples leader_return_pct min_gap_pct",
        "providers": "coinglass_enabled bubblemaps_enabled coinglass_min_transfer_usd coinglass_exchange_labels",
        "alerts": "telegram_enabled discord_enabled cooldown_seconds",
    }
    for section, keys in section_keys.items():
        if not isinstance(cfg[section], dict) or set(cfg[section]) != set(keys.split()):
            raise ValueError(f"Invalid {section} fields; credentials belong only in environment variables")
    def numeric(section, name, low, high, integer=False):
        v = cfg[section][name]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"Invalid {section}.{name}")
        if not low <= v <= high or (integer and int(v) != v):
            raise ValueError(f"{section}.{name} outside supported range")
    if set(cfg["ranking"]) != set(RANKING_DEFAULTS):
        raise ValueError("Invalid public ranking fields")
    weights = cfg["ranking"]["weights"]
    if set(weights) != set(RANKING_DEFAULTS["weights"]) or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in weights.values()) or not math.isclose(sum(weights.values()), 1):
        raise ValueError("Public ranking weights must sum to one")
    for k in RANKING_DEFAULTS:
        if k != "weights":
            numeric("ranking", k, 0.01, .99 if k == "compression_full_ratio" else 100)
    for key, lo in (("market_seconds", 60), ("whale_seconds", 3600), ("holder_seconds", 14400)):
        numeric("schedules", key, lo, 86400, True)
    numeric("universe", "min_quote_volume", 0, 1e12)
    numeric("universe", "deep_pairs_per_cycle", 1, 2000, True)
    numeric("universe", "priority_pairs", 0, cfg["universe"]["deep_pairs_per_cycle"], True)
    for k, lo, hi in (("requests_per_minute", 1, 240), ("timeout_seconds", 1, 30),
                      ("retries", 0, 4), ("backoff_seconds", 1, 60),
                      ("rate_limit_wait_seconds", 60, 3600), ("cycle_budget_seconds", 10, 3600)):
        numeric("network", k, lo, hi)
    if cfg["network"]["cycle_budget_seconds"] >= cfg["schedules"]["market_seconds"]:
        raise ValueError("Cycle budget must be shorter than market interval")
    if cfg["schedules"]["market_seconds"] != {"5m": 300, "15m": 900}[cfg["market_interval"]]:
        raise ValueError("Research schedule must match its market candle interval")
    if set(cfg["weights"]) != {"whale", "oi", "holder_safety"}:
        raise ValueError("Unknown score family")
    for k in cfg["weights"]:
        numeric("weights", k, 0, 1)
    if not math.isclose(sum(cfg["weights"].values()), 1):
        raise ValueError("Research weights must sum to one")
    for k in ("high_conviction_score", "oi_change_pct", "price_change_pct", "oi_outlier_pct",
              "flat_price_pct", "funding_warning_pct", "top_cluster_risk_pct"):
        numeric("thresholds", k, 0.0001, 100)
    numeric("thresholds", "whale_usd", 1, 1e12)
    numeric("thresholds", "flow_imbalance", 0.001, 1)
    numeric("thresholds", "top_n_clusters", 1, 100, True)
    for k in cfg["oi_scores"]:
        numeric("oi_scores", k, 0, 100)
    levels = cfg["levels"]
    for k in ("atr_multiplier", "min_stop_atr", "min_rr", "tp1_r", "tp2_r", "tp3_r",
              "max_extension_atr", "trail_atr"):
        numeric("levels", k, 0.1, 20)
    numeric("levels", "plan_hours", 1, 20)
    numeric("levels", "swing_lookback", 5, 50, True)
    numeric("levels", "fee_bps_per_side", 0, 100)
    numeric("levels", "slippage_bps", 0, 100)
    scale = levels["scale_out"]
    if len(scale) != 3 or any(not isinstance(v, (float, int)) or not 0 < v < 1 for v in scale) or not math.isclose(sum(scale), 1):
        raise ValueError("Three scale-out fractions must sum to one")
    if not levels["min_rr"] <= levels["tp1_r"] < levels["tp2_r"] < levels["tp3_r"]:
        raise ValueError("Targets must increase and TP1 must meet minimum RR")
    if levels["min_stop_atr"] > levels["atr_multiplier"]:
        raise ValueError("Minimum breathing room exceeds ATR stop")
    if levels["trigger"] not in {"pullback", "breakout"}:
        raise ValueError("Unknown entry trigger")
    numeric("laggards", "min_correlation", 0, 1)
    numeric("laggards", "min_samples", 10, 90, True)
    for k in ("leader_return_pct", "min_gap_pct"):
        numeric("laggards", k, 0, 1000)
    numeric("providers", "coinglass_min_transfer_usd", 10000000, 1e12)
    numeric("alerts", "cooldown_seconds", 60, 604800)
    for section, k in ((None, "autostart"), ("providers", "coinglass_enabled"),
                       ("providers", "bubblemaps_enabled"), ("alerts", "telegram_enabled"),
                       ("alerts", "discord_enabled")):
        if type((cfg[section] if section else cfg)[k]) is not bool:
            raise ValueError(f"{k} must be a boolean")
    for symbol, override in cfg["pair_overrides"].items():
        if set(override) != {"trigger"} or override["trigger"] not in {"pullback", "breakout"}:
            raise ValueError(f"Invalid override for {symbol}")
    if not isinstance(cfg["providers"]["coinglass_exchange_labels"], list) or any(not isinstance(v, str) or not v.strip() for v in cfg["providers"]["coinglass_exchange_labels"]):
        raise ValueError("CoinGlass exchange labels must be exact nonempty labels")
    for mapping in (cfg["sectors"], cfg["assets"], cfg["pair_overrides"]):
        if any(not re.fullmatch(r"[A-Z0-9]+USDT", s) for s in mapping):
            raise ValueError("Mappings require exact Binance perpetual symbols")
    for asset in cfg["assets"].values():
        if set(asset) - {"coinglass_symbol", "coinglass_chain", "holder_chain", "holder_address", "holder_share_unit"}:
            raise ValueError("Unknown asset mapping field; do not put API keys in config")
        if asset.get("holder_share_unit") not in (None, "fraction", "percent"):
            raise ValueError("Explicit holder share units required")
    return cfg


def config_id(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]


def pair_levels(cfg, symbol):
    return {**deepcopy(cfg["levels"]), **cfg["pair_overrides"].get(symbol, {})}
