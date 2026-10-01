"""Deterministic evidence analysis. Scores are heuristics, not probabilities."""
from collections import defaultdict
import math
import statistics

from app.research.market_scanner import DataUnavailable, candle_features, number

STEP = 900_000
STEPS = {"5m": 300_000, "15m": STEP}


def closed_market(raw, symbol, now_ms, interval="15m"):
    features = candle_features(raw["candles"], symbol, interval, now_ms)
    candles = sorted([r for r in raw["candles"] if int(r[6]) < now_ms], key=lambda r: int(r[0]))
    # candle_features checks duplicates; retain each time exactly once for downstream geometry.
    candles = list({int(r[0]): r for r in candles}.values())
    return {"features": features, "candles": candles, "interval": interval, "step_ms": STEPS[interval]}


def oi_analysis(raw, market, cfg, now_ms):
    step = market.get("step_ms", STEP)
    points = {}
    for row in raw["oi"]:
        ts = int(number(row["timestamp"], positive=True))
        value = number(row["sumOpenInterest"], positive=True)
        if ts > now_ms or ts % step:
            continue
        if ts in points and points[ts] != value:
            raise DataUnavailable("Conflicting OI observations")
        points[ts] = value
    if not points:
        raise DataUnavailable("OI history unavailable")
    latest = max(points)
    previous = latest - 4 * 3600_000
    if now_ms - latest > step + 60_000 or previous not in points:
        raise DataUnavailable("Aligned, fresh four-hour OI history unavailable")
    prices = {int(r[6])+1: number(r[4], positive=True) for r in market["candles"]}
    if latest not in prices or previous not in prices:
        raise DataUnavailable("Price and OI observation times do not align")
    oi_pct = (points[latest] / points[previous] - 1) * 100
    price_pct = (prices[latest] / prices[previous] - 1) * 100
    t = cfg["thresholds"]
    oi_dir = 1 if oi_pct >= t["oi_change_pct"] else -1 if oi_pct <= -t["oi_change_pct"] else 0
    price_dir = 1 if price_pct >= t["price_change_pct"] else -1 if price_pct <= -t["price_change_pct"] else 0
    state = f"price_{'up' if price_dir > 0 else 'down'}_oi_{'up' if oi_dir > 0 else 'down'}" if oi_dir and price_dir else "flat"
    score = cfg["oi_scores"][state]
    funding = raw.get("funding") or {}
    funding_pct = None
    if funding and 0 <= now_ms - int(funding.get("time", 0)) <= step:
        funding_pct = number(funding["lastFundingRate"]) * 100
    if funding_pct is not None and funding_pct > t["funding_warning_pct"]:
        score = max(0, score - cfg["oi_scores"]["funding_penalty"])
    acceleration = None
    hour = 3600_000
    if latest-hour in points and latest-2*hour in points:
        acceleration = ((points[latest]/points[latest-hour]-1) -
                        (points[latest-hour]/points[latest-2*hour]-1))*100
    return {"status": "available", "state": state, "score": score,
            "direction": "bullish" if state == "price_up_oi_up" else "bearish" if price_dir < 0 else "neutral",
            "oi_change_pct": oi_pct, "price_change_pct": price_pct, "funding_pct": funding_pct,
            "oi_acceleration_pp": acceleration,
            "outlier": oi_pct >= t["oi_outlier_pct"] and abs(price_pct) <= t["flat_price_pct"],
            "start_ms": previous, "end_ms": latest,
            "basis": "Four-hour change in contract OI (not USD notional) and same-time closes. Positioning interpretation, not proof of new longs or shorts."}


def whale_analysis(feed, cfg, now_ms):
    t = cfg["thresholds"]
    seen, events = set(), []
    for e in feed["events"]:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        if number(e["usd"]) >= t["whale_usd"] and now_ms-86400_000 <= e["timestamp_ms"] <= now_ms:
            events.append(e)
    windows = {}
    for hours in (1, 4, 24):
        window = [e for e in events if e["timestamp_ms"] >= now_ms - hours * 3600_000]
        inflow = sum(e["usd"] for e in window if e["direction"] == "inflow")
        outflow = sum(e["usd"] for e in window if e["direction"] == "outflow")
        windows[str(hours)] = {"inflow_usd": inflow, "outflow_usd": outflow,
                               "net_outflow_usd": outflow - inflow, "count": len(window)}
    w = windows["24"]
    gross = w["inflow_usd"] + w["outflow_usd"]
    imbalance = w["net_outflow_usd"] / gross if gross else 0
    limited = feed["minimum_usd"] > t["whale_usd"] or feed["from_ms"] > now_ms-86400_000 or feed["to_ms"] < now_ms-cfg["schedules"]["whale_seconds"]*1000
    direction = "accumulation" if imbalance >= t["flow_imbalance"] else "distribution" if imbalance <= -t["flow_imbalance"] else "neutral"
    return {"status": "limited" if limited else "available", "windows": windows,
            "net_whale_flow_24h": w["net_outflow_usd"], "large_tx_count": len(events),
            "flow_direction": direction, "score": 50 + 50 * imbalance,
            "minimum_usd": feed["minimum_usd"], "observed_through_ms": feed["to_ms"],
            "basis": "Positive net exchange outflow is an accumulation proxy, not proof of buying. Internal/unlabelled transfers have no directional vote.",
            "limitation": feed.get("limitation", "Provider coverage only")}


def concentration_analysis(feed, cfg, now_ms, previous=None):
    if not -60_000 <= now_ms - feed["timestamp_ms"] <= cfg["schedules"]["holder_seconds"] * 1000:
        raise DataUnavailable("Holder map is stale or future-dated")
    clusters = feed["clusters"]
    if not clusters or sum(c["share_pct"] for c in clusters) > 100.001:
        raise DataUnavailable("Invalid cluster coverage or share units")
    # Entity labels can join separate transfer clusters; transfer links alone are not ownership proof.
    entities = defaultdict(float)
    for c in clusters:
        for entity in c["entity_ids"]:
            entities[entity] += max(0, c["share_pct"] - c["custodial_share_pct"])
    noncustodial = sorted([max(0, c["share_pct"]-c["custodial_share_pct"]) for c in clusters], reverse=True)
    top = max(noncustodial[0], max(entities.values(), default=0))
    limit = cfg["thresholds"]["top_cluster_risk_pct"]
    risk = min(100, top / limit * 100)
    prior = {tuple(c["members"]): c["share_pct"] for c in (previous or {}).get("clusters", [])}
    changes = [{"members": c["members"], "share_change_pp": c["share_pct"]-prior[tuple(c["members"]) ]}
               for c in clusters if tuple(c["members"]) in prior]
    return {"status": "available", "risk_score": risk, "excluded": top >= limit,
            "largest_non_custodial_cluster_pct": top,
            "top_n_cluster_pct": sum(sorted([c["share_pct"] for c in clusters], reverse=True)[:cfg["thresholds"]["top_n_clusters"]]),
            "entities": dict(entities), "cluster_share_changes": changes,
            "timestamp_ms": feed["timestamp_ms"], "coverage": feed["coverage"],
            "basis": "Top-holder transfer clusters, not full supply ownership. Share changes are not proof of buying/selling; custodial balances excluded from ownership-risk numerator."}


def lagging_pairs(markets, cfg):
    sectors = defaultdict(list)
    for symbol, market in markets.items():
        sector = cfg["sectors"].get(symbol)
        if sector:
            sectors[sector].append(symbol)
    result = {}
    rules = cfg["laggards"]
    for symbols in sectors.values():
        for symbol in symbols:
            own = {int(c[6]): float(c[4]) for c in markets[symbol]["candles"]}
            best = None
            for peer in symbols:
                if peer == symbol:
                    continue
                other = {int(c[6]): float(c[4]) for c in markets[peer]["candles"]}
                times = sorted(own.keys() & other.keys())[-97:]
                step = markets[symbol].get("step_ms", STEP)
                if len(times) < rules["min_samples"]+1 or any(b-a != step for a,b in zip(times, times[1:])):
                    continue
                a, b = [own[t] for t in times], [other[t] for t in times]
                ar, br = [math.log(y/x) for x,y in zip(a,a[1:])], [math.log(y/x) for x,y in zip(b,b[1:])]
                try:
                    correlation = statistics.correlation(ar, br)
                except statistics.StatisticsError:
                    continue
                own_return, leader_return = (a[-1]/a[0]-1)*100, (b[-1]/b[0]-1)*100
                gap = leader_return - own_return
                if correlation >= rules["min_correlation"] and leader_return >= rules["leader_return_pct"] and gap >= rules["min_gap_pct"]:
                    candidate = {"leader": peer, "correlation": correlation, "gap_pct": gap,
                                 "catchup_price": a[-1]*(1+leader_return/100)/(1+own_return/100),
                                 "start_ms": times[0], "end_ms": times[-1], "samples": len(ar)}
                    if best is None or gap > best["gap_pct"]:
                        best = candidate
            if best:
                result[symbol] = best
    return result


def aggregate(oi, whale, holder, plan, cfg):
    scores = {"oi": oi.get("score"), "whale": whale.get("score"),
              "holder_safety": 100-holder["risk_score"] if holder.get("risk_score") is not None else None}
    available = sum(cfg["weights"][k] for k,v in scores.items() if v is not None)
    contribution = {k: cfg["weights"][k]*v if v is not None else None for k,v in scores.items()}
    # No renormalization: missing data cannot inflate a one-family signal to 100.
    composite = sum(v for v in contribution.values() if v is not None)
    reasons = []
    if oi.get("direction") != "bullish":
        reasons.append("No bullish price/OI buildup confluence")
    if whale.get("flow_direction") != "accumulation" or whale.get("status") != "available":
        reasons.append("Whale accumulation evidence missing, limited or not supportive")
    if holder.get("status") != "available":
        reasons.append("Holder concentration risk unverified")
    if holder.get("excluded"):
        reasons.append("Holder concentration exceeds risk limit")
    if not plan.get("valid") or plan.get("risk_reward", 0)+1e-9 < cfg["levels"]["min_rr"]:
        reasons.append(plan.get("reason", "No valid trade geometry"))
    if composite < cfg["thresholds"]["high_conviction_score"]:
        reasons.append("Composite below high-conviction threshold")
    avoid = holder.get("excluded") or oi.get("direction") == "bearish" or whale.get("flow_direction") == "distribution"
    return {"composite_score": round(composite, 2) if available else None,
            "score_coverage_pct": available*100, "sub_scores": scores, "contributions": contribution,
            "tier": "avoid" if avoid else "watch" if reasons else "high_conviction", "reasons": reasons}
