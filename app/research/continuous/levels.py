"""Conditional long research plans in reference-venue prices, never orders."""
from app.research.continuous.config import pair_levels


def calculate_levels(market, symbol, cfg, laggard=None):
    rules = pair_levels(cfg, symbol)
    f, candles = market["features"], market["candles"]
    interval = market.get("interval", "15m")
    lookback = rules["swing_lookback"]
    recent = candles[-lookback-1:-1]
    low_row = min(recent, key=lambda c: float(c[3]))
    high_row = max(recent, key=lambda c: float(c[2]))
    swing_low, resistance = float(low_row[3]), float(high_row[2])
    entry = f["ema21"] if rules["trigger"] == "pullback" else f["close"] if f["close"] > resistance else resistance
    atr, reference = f["atr"], f["close"]
    reasons = []
    if f["trend"] != 1:
        reasons.append(f"Closed {interval} trend does not support a long plan")
    if (reference-f["ema21"])/atr > rules["max_extension_atr"]:
        reasons.append("Price is extended from EMA; late-chase filter")
    atr_stop = entry - atr*rules["atr_multiplier"]
    eligible_stops = [s for s in (swing_low, atr_stop) if s > 0 and entry-s >= atr*rules["min_stop_atr"]]
    if not eligible_stops or entry <= 0:
        return {"valid": False, "reason": "No stop with sufficient ATR breathing room", "reference_price": reference}
    stop = max(eligible_stops)
    risk = entry-stop
    # A resistance pivot needs two subsequent closed candles; ordinary candle wicks
    # along a trend are not independent resistance levels.
    history = candles[-lookback-5:]
    pivots = {float(history[i][2]) for i in range(2, len(history)-2)
              if all(float(history[i][2]) > float(history[j][2])
                     for j in (i-2, i-1, i+1, i+2))}
    overhead = sorted(p for p in pivots | {resistance} if p > entry)
    tp1 = min(entry+rules["tp1_r"]*risk, overhead[0]) if overhead else entry+rules["tp1_r"]*risk
    rr = (tp1-entry)/risk
    if rr+1e-9 < rules["min_rr"]:
        reasons.append("Nearest resistance leaves TP1 below minimum reward/risk")
    tp2 = entry+rules["tp2_r"]*risk
    tp3 = max(entry+rules["tp3_r"]*risk, (laggard or {}).get("catchup_price", 0))
    if not 0 < stop < entry < tp1 < tp2 < tp3:
        reasons.append("Invalid target geometry")
    if rules["trigger"] == "pullback" and reference < entry:
        reasons.append("Pullback limit is above current reference price")
    basis = {
        "entry": "EMA21 pullback limit, conditional on future price touch" if rules["trigger"] == "pullback" else f"{interval} close above prior {lookback}-candle high {resistance:g}; subsequent opening price must not chase",
        "stop_loss": f"{'Swing low' if stop == swing_low else 'ATR14 stop'}; max of eligible structural and {rules['atr_multiplier']:g} ATR stops with at least {rules['min_stop_atr']:g} ATR breathing room",
        "tp1": "Nearest confirmed swing/window resistance" if overhead and tp1 == overhead[0] else f"{rules['tp1_r']:g}R target",
        "tp2": f"{rules['tp2_r']:g}R target",
        "tp3": "Sector return catch-up mapped onto this asset's price (stretch)" if laggard and tp3 == laggard["catchup_price"] else f"{rules['tp3_r']:g}R stretch target",
    }
    return {"valid": not reasons, "reason": "; ".join(reasons), "entry": entry, "stop_loss": stop,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "risk_reward": rr,
            "trigger": rules["trigger"], "trigger_level": resistance if rules["trigger"] == "breakout" else entry,
            "trigger_confirmed": rules["trigger"] == "breakout" and reference > resistance,
            "reference_price": reference, "atr": atr, "rules": rules, "basis": basis, "interval": interval,
            "as_of_ms": f["close_time_ms"], "swing_low_time_ms": int(low_row[6]),
            "resistance_time_ms": int(high_row[6]),
            "signal_invalidation": "Whale flow turns to distribution or aligned price/OI becomes bearish. Not executable until re-observed; price replay excludes this discretionary exit."}
