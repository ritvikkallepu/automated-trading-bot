"""Persistent pair-specific episodes. Scan revisions never reset an active plan."""
from copy import deepcopy
import uuid

from .analysis import STEP, STEPS
from .ranking import VERSION

HORIZON = 4*3600_000


def track_publication(store, snapshot):
    latest = {(s["symbol"], s.get("interval", s.get("plan", {}).get("interval", "15m"))): s for s in store.setups()}
    outcomes = {r["id"]: r for r in store.replay_results() if r.get("setup_id")}
    prior_samples = {(s["symbol"], s.get("interval", "15m")): s for s in store.validation_samples()}
    updated, events, samples = [], [], []
    at = snapshot["published_ms"]
    cfg = snapshot["config"]
    interval = cfg.get("market_interval", "15m")
    step = STEPS[interval]
    for row in snapshot["rows"]:
        if not row.get("public_rank"):
            continue
        symbol = row["symbol"]
        bar_ms = row["plan"].get("as_of_ms", row.get("market_close_ms", 0))
        stage = row["timing"]
        valid = stage in {"forming", "trigger_confirmed"}
        current = latest.get((symbol, interval))
        outcome = outcomes.get(current["id"], {}) if current else {}
        terminal = bool(outcome.get("terminal")) or bool(current and current.get("cancelled_ms"))
        # An episode ends only once. A later invalid setup then a fresh valid candle
        # is required to re-arm; repeated bullish scans/config edits cannot re-enter.
        if current and terminal and not valid and bar_ms > current["last_bar_ms"]:
            current["reset_bar_ms"] = bar_ms
        rearmed = current and terminal and current.get("reset_bar_ms", 0) < bar_ms and bool(current.get("reset_bar_ms"))
        if valid and (current is None or rearmed):
            current = {"id": uuid.uuid4().hex, "symbol": symbol, "pair": row["pair"], "interval": interval,
                       "published_ms": at, "snapshot_id": snapshot["id"], "config_id": snapshot["config_id"],
                       "ranking_version": VERSION, "plan": deepcopy(row["plan"]),
                       "score": row["public_rank"]["score"], "threshold": cfg["ranking"]["candidate_score"],
                       "tier": row["tier"], "evidence": deepcopy(row["evidence"]),
                       "stage": stage, "last_bar_ms": bar_ms, "revision_count": 0}
            outcome, terminal = {}, False
            event_type = "created"
        else:
            event_type = "updated"
        if current:
            previous_stage = current["stage"]
            if not terminal and not valid and not outcome.get("entry_price") and outcome.get("status") != "data_gap":
                current["cancelled_ms"] = at
                current["cancel_reason"] = stage
                terminal = True
            display_stage = ("invalidated" if current.get("cancelled_ms") else
                             "completed" if terminal else "open" if outcome.get("entry_price") else
                             "trigger_confirmed" if outcome.get("status") == "trigger_confirmed" else stage)
            current.update(stage=display_stage, last_seen_ms=at, last_bar_ms=bar_ms,
                           revision_count=current["revision_count"]+1)
            if previous_stage != display_stage:
                event_type = display_stage
            threshold = cfg["ranking"]["candidate_score"]
            score = row["public_rank"]["score"]
            if event_type == "updated" and not terminal and valid and current.get("last_score", current["score"]) < threshold <= score:
                event_type = "rank_qualified"
            current["last_score"] = score
            event = {"id": uuid.uuid4().hex, "setup_id": current["id"], "at_ms": at,
                     "snapshot_id": snapshot["id"], "type": event_type, "stage": display_stage,
                     "score": row["public_rank"]["score"], "rank": row.get("rank"),
                     "timing": stage, "evidence": row["evidence"]}
            events.append(event)
            updated.append(current)
            latest[(symbol, interval)] = current
            row["setup"] = {"id": current["id"], "stage": display_stage,
                            "first_seen_ms": current["published_ms"], "revisions": current["revision_count"],
                            "event": event_type, "frozen_plan": current["plan"],
                            "was_candidate": current.get("ever_qualified", current["score"] >= current["threshold"]),
                            "outcome": outcome.get("status"), "awaiting_reset": terminal,
                            "config_changed": current["config_id"] != snapshot["config_id"]}
            current["ever_qualified"] = current.get("ever_qualified", False) or (valid and score >= threshold)
        # Non-overlapping, prospective samples for ALL analysed pairs, not just
        # successful geometries or the winners that survive a later scan.
        previous = prior_samples.get((symbol, interval))
        first = (at//step+1)*step
        if previous is None or first >= previous["first_ms"]+HORIZON:
            sample = {"id": uuid.uuid4().hex, "symbol": symbol, "pair": row["pair"], "interval": interval,
                      "published_ms": at, "first_ms": first, "horizon_ms": HORIZON,
                      "config_id": snapshot["config_id"], "ranking_version": VERSION,
                      "snapshot_id": snapshot["id"], "direction": "long",
                      "reference_price": row.get("reference_price", row["plan"].get("reference_price")),
                      "evidence": deepcopy(row.get("evidence", {})),
                      "score": row["public_rank"]["score"], "timing": stage,
                      "candidate": valid and row["public_rank"]["score"] >= cfg["ranking"]["candidate_score"],
                      "threshold": cfg["ranking"]["candidate_score"],
                      "rules": {k: cfg["levels"][k] for k in ("fee_bps_per_side", "slippage_bps")}}
            samples.append(sample)
    store.publish(snapshot, updated, events, samples)


def setup_status(store):
    results = {r["id"]: r for r in store.replay_results() if r.get("setup_id")}
    history = []
    for s in reversed(store.setups()):
        r = results.get(s["id"], {})
        history.append({**s, "outcome": r, "stage": "invalidated" if s.get("cancelled_ms") else
                        "completed" if r.get("terminal") else "open" if r.get("entry_price") else s["stage"]})
    return {"total": len(history), "items": history,
            "events": list(reversed(store.setup_events()))[:200],
            "history_policy": "One frozen plan per pair until resolved and reset; older scan-only replays excluded"}
