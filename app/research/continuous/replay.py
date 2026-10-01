"""Conservative OHLC replay of frozen research plans, not broker execution."""
from collections import defaultdict
import math
import statistics
from app.research.continuous.analysis import STEP, STEPS
from app.research.market_scanner import DataUnavailable, candle_features


def replay_plan(plan, published_ms, raw_candles, now_ms, symbol):
    if not plan.get("valid"):
        return {"status": "invalid_plan", "terminal": True}
    rules = plan["rules"]
    interval = plan.get("interval", "15m")
    step = STEPS[interval]
    expires = published_ms + rules["plan_hours"]*3600_000
    first = (published_ms//step+1)*step
    # Only wholly post-publication candles. A forming candle includes unseen pre-signal extremes.
    candles = [r for r in raw_candles if int(r[0]) >= first and int(r[6]) < min(now_ms, expires)]
    candles.sort(key=lambda c: int(c[0]))
    if not candles:
        return {"status": "data_gap" if first+step <= min(now_ms, expires) else "pending", "terminal": False, "tp_hits": [False]*3}
    entry, stop = None, plan["stop_loss"]
    remaining, gross, fees, hits = 1.0, 0.0, 0.0, [False]*3
    confirmed = plan["trigger_confirmed"]
    entered_ms, exited_ms = None, None
    slippage, fee = rules["slippage_bps"]/10000, rules["fee_bps_per_side"]/10000
    result_status = "pending"
    mfe, mae, processed_ms = 0.0, 0.0, None
    def result(status, terminal=False):
        return {"status": status, "terminal": terminal, "entry_price": entry,
                "entered_ms": entered_ms, "exited_ms": exited_ms, "remaining_fraction": remaining,
                "gross_return_pct": gross*100 if entry else None,
                "net_return_pct": (gross-fees)*100 if entry else None,
                "tp_hits": hits, "risk_reward_at_fill": (plan["tp1"]-entry)/(entry-plan["stop_loss"]) if entry else None,
                "mfe_pct": mfe*100 if entry else None, "mae_pct": mae*100 if entry else None,
                "last_bar_ms": processed_ms,
                "method": f"{interval} OHLC; stops first when ambiguous; no same-bar favourable targets on intrabar entry; fees/slippage included; no signal-flip exits"}
    expected = first
    for row in candles:
        ts, op, high, low, close = int(row[0]), *map(float, row[1:5])
        if ts != expected or not all(math.isfinite(v) for v in (op, high, low, close)) or not 0 < low <= min(op, close) <= max(op, close) <= high or int(row[6]) != ts+step-1:
            return result("data_gap")
        expected += step
        processed_ms = int(row[6])
        new_entry = False
        if entry is None:
            price = None
            if plan["trigger"] == "pullback" and low <= plan["entry"]:
                # Conservative resting limit fill, no assumed price improvement or fill below stop.
                if op <= stop:
                    return result("gap_invalidated", True)
                price = plan["entry"]
            elif plan["trigger"] == "breakout":
                if confirmed:
                    price = op*(1+slippage)
                elif close > plan["trigger_level"]:
                    confirmed = True
                    result_status = "trigger_confirmed"
                    continue
            if price is None:
                continue
            if not stop < price < plan["tp1"] or (plan["tp1"]-price)/(price-stop) < rules["min_rr"]-1e-9:
                return result("fill_geometry_rejected", True)
            if price > plan["entry"]+plan["atr"]*rules["max_extension_atr"]:
                return result("gap_chase_rejected", True)
            entry, entered_ms, new_entry = price, ts, True
            fees += fee
            result_status = "open"
        if low <= stop:
            exit_price = min(op, stop)*(1-slippage)
            # No favourable extremes on a stop candle: their sequence is unknown.
            mae = min(mae, exit_price/entry-1)
            gross += remaining*(exit_price/entry-1)
            fees += remaining*exit_price/entry*fee
            remaining, exited_ms = 0, ts
            return result("trailing_stop" if hits[1] else "stop_loss", True)
        mae = min(mae, low/entry-1)
        # OHLC cannot establish whether a pre-entry high occurred before a pullback fill.
        if new_entry and plan["trigger"] == "pullback":
            continue
        mfe = max(mfe, min(high, plan["tp3"])/entry-1)
        for i, target in enumerate((plan["tp1"], plan["tp2"], plan["tp3"])):
            if not hits[i] and high >= target:
                fraction = rules["scale_out"][i]
                gross += fraction*(target/entry-1)
                fees += fraction*target/entry*fee
                remaining = max(0, remaining-fraction)
                hits[i] = True
        if remaining < 1e-9:
            exited_ms = ts
            return result("all_targets", True)
        if hits[1]:
            history = [r for r in raw_candles if int(r[6]) <= int(row[6])]
            if len(history) < 60:
                return result("data_gap")
            try:
                atr = candle_features(history, symbol, interval, int(row[6])+1)["atr"]
            except DataUnavailable:
                return result("data_gap")
            # Updated after candle close, active from the next candle only.
            stop = max(stop, close-rules["trail_atr"]*atr)
    if now_ms >= expires and int(candles[-1][6])+1 >= expires//step*step:
        if entry:
            price = float(candles[-1][4])*(1-slippage)
            gross += remaining*(price/entry-1)
            fees += remaining*price/entry*fee
            remaining, exited_ms = 0, int(candles[-1][6])
            return result("timeout", True)
        return result("not_triggered", True)
    return result(result_status)


def summary(results):
    legacy = sum(not r.get("setup_id") for r in results)
    results = [r for r in results if r.get("setup_id")]
    groups = defaultdict(list)
    for row in results:
        groups[(row["config_id"], row["threshold"], row["tier"])].append(row)
    output = []
    for (config, threshold, tier), rows in sorted(groups.items()):
        terminal = [r for r in rows if r.get("terminal")]
        entered = [r for r in terminal if r.get("entry_price")]
        scored = [r for r in entered if r.get("score", -1) >= threshold]
        output.append({"config_id": config, "score_threshold": threshold, "tier": tier,
                       "plans": len(rows), "closed_entries": len(entered),
                       "target_hits": [sum(bool(r.get("tp_hits", [False]*3)[i]) for r in entered) for i in range(3)],
                       "stop_exits": sum(r["status"] in {"stop_loss", "trailing_stop"} for r in entered),
                       "pending": sum(not r.get("terminal") for r in rows),
                       "data_gaps": sum(r["status"] == "data_gap" for r in rows),
                       "unfilled": len(terminal)-len(entered),
                       "avg_net_return_pct": statistics.mean(r["net_return_pct"] for r in entered) if entered else None,
                       "avg_mfe_pct": statistics.mean(r["mfe_pct"] for r in entered if r.get("mfe_pct") is not None) if any(r.get("mfe_pct") is not None for r in entered) else None,
                       "avg_mae_pct": statistics.mean(r["mae_pct"] for r in entered if r.get("mae_pct") is not None) if any(r.get("mae_pct") is not None for r in entered) else None,
                       "above_threshold_entries": len(scored),
                       "above_threshold_tp1_hit_rate": sum(r["tp_hits"][0] for r in scored)/len(scored) if scored else None,
                       "positive_net_rate": sum(r["net_return_pct"] > 0 for r in entered)/len(entered) if entered else None})
    return {"groups": output, "legacy_excluded": legacy,
            "unit": "Distinct frozen setups, one active per pair. Conservative OHLC MFE/MAE; fees/slippage included, funding excluded. Not portfolio PnL.",
            "results": sorted(results, key=lambda r: (r["published_ms"], r["pair"]), reverse=True)}


def replay_stored(store, markets, now_ms):
    done = {r["id"] for r in store.replay_results() if r.get("terminal")}
    candles = {}
    for setup in store.setups():
        key, symbol = setup["id"], setup["symbol"]
        if key in done:
            continue
        # Recovery is not restricted to the latest two days. Old unresolved episodes
        # remain visible as gaps, never silently omitted from the denominator.
        interval = setup.get("interval", setup.get("plan", {}).get("interval", "15m"))
        step = STEPS[interval]
        cache_key = (symbol, setup["published_ms"], interval)
        if cache_key not in candles:
            candles[cache_key] = store.candles(symbol, setup["published_ms"]-100*step, interval)
        try:
            outcome = replay_plan(setup["plan"], setup["published_ms"], candles[cache_key],
                                  min(now_ms, setup.get("cancelled_ms", now_ms)), symbol)
        except (DataUnavailable, ValueError, TypeError, KeyError, IndexError):
            outcome = {"status": "data_gap", "terminal": False, "tp_hits": [False]*3}
        if setup.get("cancelled_ms") and not outcome.get("entry_price") and outcome["status"] != "data_gap":
            outcome.update(status="invalidated", terminal=True, invalidated_ms=setup["cancelled_ms"])
        outcome.update(id=key, setup_id=key, pair=setup["pair"], config_id=setup["config_id"], score=setup["score"],
                       tier=setup["tier"], published_ms=setup["published_ms"], threshold=setup["threshold"])
        store.save_replay(key, outcome)
