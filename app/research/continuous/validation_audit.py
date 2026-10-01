"""Human-readable checks of frozen forward samples. No live prices or new scoring."""
import math
import time

from .analysis import STEPS


def verdict(sample, now_ms):
    if sample.get("status") == "complete":
        value = sample.get("net_return_pct")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return "positive" if value > 0 else "negative" if value < 0 else "flat"
        return "data_gap"
    if sample.get("status") == "data_gap":
        return "data_gap"
    return "pending" if now_ms < sample["first_ms"]+sample["horizon_ms"] else "awaiting_check"


def samples_with_results(store, now_ms):
    results = {r["id"]: r for r in store.validation_results()}
    rows = []
    for s in store.validation_samples():
        r = results.get(s["id"], {})
        # Always take identity, qualification and the test window from the frozen sample.
        row = {**r, **s, "status": r.get("status", "pending"),
               "end_ms": s["first_ms"]+s["horizon_ms"], "direction": "long"}
        row["verdict"] = verdict(row, now_ms)
        if row["verdict"] not in {"positive", "negative", "flat"}:
            for key in ("net_return_pct", "entry_price", "exit_price", "mfe_pct", "mae_pct"):
                row[key] = None
        rows.append(row)
    return sorted(rows, key=lambda r: (-r["published_ms"], r["symbol"], r["id"]))


def audit_page(store, *, config_id="", cohort="candidates", outcome="all", query="", page=0, now_ms=None):
    if cohort not in {"candidates", "controls", "all"} or outcome not in {"all", "positive", "negative", "flat", "pending", "data_gap"}:
        raise ValueError("Invalid validation filter")
    if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= 1000000 or len(query) > 100 or len(config_id) > 64:
        raise ValueError("Invalid validation page")
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    rows = samples_with_results(store, now_ms)
    rows = [r for r in rows if not config_id or r["config_id"] == config_id]
    counts = {"candidates": sum(bool(r["candidate"]) for r in rows), "controls": sum(not r["candidate"] for r in rows)}
    rows = [r for r in rows if (cohort == "all" or bool(r["candidate"]) == (cohort == "candidates"))
            and query.strip().upper() in r["pair"].upper()]
    summary = {k: sum(r["verdict"] == k for r in rows) for k in ("positive", "negative", "flat", "pending", "awaiting_check", "data_gap")}
    summary.update(total=len(rows), checked=sum(summary[k] for k in ("positive", "negative", "flat")))
    rows = [r for r in rows if outcome == "all" or r["verdict"] == outcome or (outcome == "pending" and r["verdict"] == "awaiting_check")]
    total, size = len(rows), 25
    page = min(page, max(0, (total-1)//size))
    return {"items": rows[page*size:(page+1)*size], "total": total, "page": page,
            "pages": max(1, (total+size-1)//size), "summary": summary,
            "cohort_counts": counts, "config_id": config_id, "cohort": cohort, "as_of_ms": now_ms}


def audit_detail(store, sample_id, *, now_ms=None):
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    row = next((r for r in samples_with_results(store, now_ms) if r["id"] == sample_id), None)
    if row is None:
        return None
    interval = row.get("interval", "15m")
    step = STEPS[interval]
    snapshot = store.publication_at(row["published_ms"], row["config_id"])
    original = next((r for r in (snapshot or {}).get("rows", []) if r["symbol"] == row["symbol"]), None)
    reference = row.get("reference_price")
    if reference is None and original:
        reference = original.get("reference_price", original.get("plan", {}).get("reference_price"))
    bars = store.candles(row["symbol"], row["first_ms"], interval)
    bars = [b for b in bars if row["first_ms"] <= int(b[0]) < row["end_ms"] and int(b[6]) < now_ms]
    # Return only well-formed closed bars inside this fixed window; never a today's-price substitute.
    clean = []
    for b in bars:
        try:
            op, high, low, close = map(float, b[1:5])
            if all(math.isfinite(v) for v in (op,high,low,close)) and 0 < low <= min(op,close) <= max(op,close) <= high and int(b[6]) == int(b[0])+step-1:
                clean.append({"open_ms": int(b[0]), "close_ms": int(b[6]), "open": op, "high": high, "low": low, "close": close})
        except (ValueError, TypeError, IndexError):
            continue
    expected = list(range(row["first_ms"], min(row["end_ms"], now_ms//step*step), step))
    present = {b["open_ms"] for b in clean}
    missing = [ms for ms in expected if ms not in present]
    return {"sample": row, "reference_price": reference,
            "snapshot_id": row.get("snapshot_id") or (snapshot or {}).get("id"),
            "frozen_assessment": original, "candles": clean, "missing_open_times": missing,
            "expected_candles": row["horizon_ms"]//step, "source": f"Stored Binance {interval} candles",
            "method": f"Long-direction test from the next full {interval} candle open to the close four hours later. Fees and slippage included; funding excluded. Not a conditional trade fill or portfolio PnL."}
