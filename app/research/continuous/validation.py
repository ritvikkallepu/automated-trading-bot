"""Prospective non-overlapping 4h samples, including non-candidates as controls."""
import math
import statistics

from .analysis import STEPS
from .store import decode, encode


def evaluate_sample(sample, candles, now_ms):
    step = STEPS[sample.get("interval", "15m")]
    start = sample["first_ms"]
    end = start+sample["horizon_ms"]
    result = {**sample, "status": "pending", "terminal": False}
    if now_ms < end:
        return result
    bars = [r for r in candles if start <= int(r[0]) < end and int(r[6]) < now_ms]
    expected = list(range(start, end, step))
    if [int(r[0]) for r in bars] != expected:
        return {**result, "status": "data_gap"}
    for r in bars:
        op, hi, lo, cl = map(float, r[1:5])
        if not all(math.isfinite(x) for x in (op, hi, lo, cl)) or not 0 < lo <= min(op, cl) <= max(op, cl) <= hi or int(r[6]) != int(r[0])+step-1:
            return {**result, "status": "data_gap"}
    rules = sample["rules"]
    slip, fee = rules["slippage_bps"]/10000, rules["fee_bps_per_side"]/10000
    entry, exit_price = float(bars[0][1])*(1+slip), float(bars[-1][4])*(1-slip)
    ratio = exit_price/entry
    return {**result, "status": "complete", "terminal": True,
            "net_return_pct": (ratio-1-fee*(1+ratio))*100,
            "mfe_pct": max(0, (max(float(r[2]) for r in bars)/entry-1)*100),
            "mae_pct": min(0, (min(float(r[3]) for r in bars)/entry-1)*100),
            "entry_price": entry, "exit_price": exit_price}


def evaluate_stored(store, now_ms):
    previous = {r["id"]: r for r in store.validation_results()}
    samples = store.validation_samples()
    updates = []
    with store.connect() as db:
        for sample in samples:
            old = previous.get(sample["id"], {})
            if old.get("terminal") or (old.get("status") == "pending" and now_ms < sample["first_ms"]+sample["horizon_ms"]):
                continue
            interval = sample.get("interval", "15m")
            asset = sample["symbol"] if interval == "15m" else f"{sample['symbol']}|{interval}"
            start, end = sample["first_ms"], sample["first_ms"]+sample["horizon_ms"]
            count, latest = db.execute(
                "SELECT COUNT(*), MAX(opened_ms) FROM closed_candles WHERE asset=? AND opened_ms>=? AND opened_ms<?",
                (asset, start, end)).fetchone()
            if old.get("status") == "data_gap" and old.get("checked_candles") == count and old.get("checked_latest_ms") == latest:
                continue
            rows = db.execute("SELECT payload FROM closed_candles WHERE asset=? AND opened_ms>=? AND opened_ms<? ORDER BY opened_ms",
                              (asset, start, end)).fetchall()
            result = evaluate_sample(sample, [decode(r[0]) for r in rows], now_ms)
            result.update(checked_candles=count, checked_latest_ms=latest)
            updates.append((sample["id"], encode(result)))
        if updates:
            db.executemany("INSERT OR REPLACE INTO validation_results VALUES (?,?)", updates)


def validation_summary(results):
    groups = {}
    for r in results:
        key = (r["config_id"], r["ranking_version"], r["threshold"])
        groups.setdefault(key, []).append(r)
    def stats(rows):
        return {"count": len(rows), "mean_net_pct": statistics.mean(r["net_return_pct"] for r in rows) if rows else None,
                "median_net_pct": statistics.median(r["net_return_pct"] for r in rows) if rows else None,
                "positive_rate": sum(r["net_return_pct"] > 0 for r in rows)/len(rows) if rows else None,
                "mean_mfe_pct": statistics.mean(r["mfe_pct"] for r in rows) if rows else None,
                "mean_mae_pct": statistics.mean(r["mae_pct"] for r in rows) if rows else None}
    def cohort(label, rows):
        mature = [r for r in rows if r["status"] == "complete"]
        scores = [r["score"] for r in rows if r.get("score") is not None]
        return {"label": label, **stats(mature), "total": len(rows),
                "pending": sum(r["status"] == "pending" for r in rows),
                "data_gaps": sum(r["status"] == "data_gap" for r in rows),
                "min_score": min(scores) if scores else None, "max_score": max(scores) if scores else None}
    output = []
    for (cid, version, threshold), rows in sorted(groups.items()):
        mature = [r for r in rows if r["status"] == "complete"]
        candidates = stats([r for r in mature if r["candidate"]])
        controls = stats([r for r in mature if not r["candidate"]])
        bands = [cohort(label, [r for r in rows if r.get("score") is not None and low <= r["score"] < high])
                 for label, low, high in [("Below 40", 0, 40), ("40 to <55", 40, 55),
                                          ("55 to <65", 55, 65), ("65 to <80", 65, 80), ("80 to 100", 80, 101)]]
        missing = [r for r in rows if r.get("score") is None]
        if missing:
            bands.append(cohort("Score unavailable", missing))
        scored = sorted([r for r in rows if r.get("score") is not None], key=lambda r: r["score"])
        buckets, previous, bucket = [[] for _ in range(10)], None, 0
        for index, row in enumerate(scored):
            if row["score"] != previous:
                bucket = min(9, index*10//len(scored))
                previous = row["score"]
            buckets[bucket].append(row)
        qualified = [r for r in rows if r["candidate"]]
        output.append({"config_id": cid, "version": version, "threshold": threshold,
                       "interval": rows[0].get("interval", "15m"),
                       "score_bands": bands,
                       "deciles": [cohort(f"D{i+1}", b) for i, b in enumerate(buckets)],
                       "progress": {"target": 30, "complete": candidates["count"],
                                    "remaining": max(0, 30-candidates["count"]),
                                    "pending": sum(r["status"] == "pending" for r in qualified),
                                    "data_gaps": sum(r["status"] == "data_gap" for r in qualified),
                                    "unique_pairs": len({r["symbol"] for r in mature if r["candidate"]}),
                                    "status": "review_ready" if candidates["count"] >= 30 else "collecting"},
                       "all": stats(mature), "candidates": candidates, "controls": controls,
                       "pending": sum(r["status"] == "pending" for r in rows),
                       "data_gaps": sum(r["status"] == "data_gap" for r in rows),
                       "candidate_minus_control_pct": candidates["mean_net_pct"]-controls["mean_net_pct"] if candidates["count"] and controls["count"] else None})
    return {"groups": output, "unit": "4h from next full research candle open; same fees/slippage for all cohorts. Samples do not overlap per pair and interval. No funding; pairs/time periods may be correlated. Not portfolio PnL.",
            "results": sorted(results, key=lambda r: (r["published_ms"], r["symbol"]), reverse=True)}
