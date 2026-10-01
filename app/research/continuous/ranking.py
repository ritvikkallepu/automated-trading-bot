"""Transparent public-data long ranking, separate from multi-provider confluence."""
import statistics

VERSION = "public-v1"
DEFAULTS = {
    "weights": {"volume": .25, "oi": .20, "trend": .20, "compression": .15, "proximity": .20},
    "volume_full_ratio": 3.0, "oi_full_pct": 8.0, "oi_acceleration_full_pp": 2.0,
    "trend_full_atr": 2.0, "compression_full_ratio": .5, "breakout_distance_atr": 3.0,
    "funding_penalty": 10.0, "candidate_score": 65.0,
}


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def public_rank(market, oi, cfg):
    rules = cfg.get("ranking", DEFAULTS)
    f, bars = market["features"], market["candles"]
    atr = f["atr"]
    ranges = [float(r[2])-float(r[3]) for r in bars]
    prior = statistics.mean(ranges[-25:-5])
    compression = statistics.mean(ranges[-5:])/prior if prior else 1.0
    resistance = max(float(r[2]) for r in bars[-21:-1])
    distance = (resistance-f["close"])/atr
    spread = (f["ema21"]-f["ema55"])/atr
    extension = (f["close"]-f["ema21"])/atr
    hour_bars = 3_600_000 // market["step_ms"]
    hour_change_pct = (f["close"] / f["closes"][-hour_bars-1] - 1) * 100
    scores = {
        "volume": clamp(f["volume_ratio"]/rules["volume_full_ratio"]),
        "trend": clamp(spread/rules["trend_full_atr"]) if f["trend"] == 1 else 0.0,
        "compression": clamp((1-compression)/(1-rules["compression_full_ratio"])),
        "proximity": clamp(1-abs(distance)/rules["breakout_distance_atr"]),
        "oi": None,
    }
    if oi.get("status") == "available":
        buildup = clamp(oi["oi_change_pct"]/rules["oi_full_pct"])
        acceleration = oi.get("oi_acceleration_pp")
        # Missing intermediate OI points earn no acceleration contribution.
        scores["oi"] = (0.7*buildup + (0.3*clamp(acceleration/rules["oi_acceleration_full_pp"]) if acceleration is not None else 0)) if oi.get("direction") != "bearish" else 0.0
    if extension > cfg["levels"]["max_extension_atr"]:
        scores["proximity"] = 0.0
    components = {k: round(v*rules["weights"][k]*100, 4) if v is not None else None for k,v in scores.items()}
    penalty = rules["funding_penalty"] if (oi.get("funding_pct") or 0) > cfg["thresholds"]["funding_warning_pct"] else 0
    return {"version": VERSION, "score": round(max(0, sum(v for v in components.values() if v is not None)-penalty), 2),
            "coverage_pct": sum(rules["weights"][k]*100 for k,v in scores.items() if v is not None),
            "contributions": components, "funding_penalty": penalty,
            "features": {"volume_ratio": f["volume_ratio"], "return_pct": f["return_pct"],
                         "hour_change_pct": hour_change_pct, "oi_change_pct": oi.get("oi_change_pct"),
                         "oi_acceleration_pp": oi.get("oi_acceleration_pp"), "ema_spread_atr": spread,
                         "compression_ratio": compression, "breakout_distance_atr": distance,
                         "extension_atr": extension},
            "basis": f"Closed {market.get('interval', '15m')} public-data long ranking. Missing OI is not reweighted; whale and holder data are not included. Not a probability."}


def timing(row, cfg):
    if row.get("tier") == "unscanned":
        return "unscanned"
    extension = row.get("public_rank", {}).get("features", {}).get("extension_atr", 0)
    if extension > cfg["levels"]["max_extension_atr"]:
        return "overextended"
    if row.get("tier") == "avoid" or not row.get("plan", {}).get("valid"):
        return "invalidated"
    return "trigger_confirmed" if row["plan"].get("trigger_confirmed") else "forming"


def rank_rows(rows):
    tiers = {"high_conviction": 0, "watch": 1, "avoid": 2, "unscanned": 3}
    stages = {"trigger_confirmed": 0, "forming": 1, "overextended": 2, "invalidated": 3, "unscanned": 4}
    return sorted(rows, key=lambda r: (tiers[r["tier"]], stages.get(r.get("timing"), 4),
                                      -r.get("public_rank", {}).get("score", 0), r["pair"]))


def ranking_changes(rows, previous, cfg):
    """Compare only the common cohort; rotation alone cannot create a rank jump."""
    old = {r["symbol"]: r for r in (previous or {}).get("rows", []) if r.get("public_rank")}
    common = {r["symbol"] for r in rows if r.get("public_rank")} & old.keys()
    before = {r["symbol"]: i+1 for i,r in enumerate(rank_rows([old[s] for s in common]))}
    after = {r["symbol"]: i+1 for i,r in enumerate(r for r in rows if r["symbol"] in common)}
    compatible = (previous or {}).get("config_id") is not None and (previous or {}).get("config") == cfg
    rank = 0
    for row in rows:
        row["rank_change"] = None
        row["score_change"] = None
        row["rank_change_basis"] = f"{len(common)} common pairs in adjacent cycles"
        if not row.get("public_rank"):
            continue
        rank += 1
        row["rank"] = rank
        if compatible and row["symbol"] in common:
            row["rank_change"] = before[row["symbol"]]-after[row["symbol"]]
            row["score_change"] = round(row["public_rank"]["score"]-old[row["symbol"]]["public_rank"]["score"], 2)
