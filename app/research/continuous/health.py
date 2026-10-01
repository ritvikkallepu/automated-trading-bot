"""Durable outage/recovery tracking, called by the independent supervisor."""
import time
import uuid

from .notify import send_message


def check_health(store, cfg, *, process_alive, started_ms, now_ms=None):
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    old = store.state("research_health", {})
    snapshot = store.latest_publication()
    last = snapshot.get("published_ms") if snapshot else None
    overdue_ms = cfg["schedules"]["market_seconds"]*2000
    paused = not store.state("research_enabled", True)
    state = ("paused" if paused else "process_down" if not process_alive else
             "scan_overdue" if now_ms-(last or started_ms) > overdue_ms else
             "collecting" if not last else "healthy")
    # A restart is not a recovery until a new publication actually completes.
    outage = old.get("status") in {"process_down", "scan_overdue"}
    if outage and state in {"healthy", "collecting"} and (last or 0) <= old.get("outage_publication_ms", 0):
        state = "scan_overdue"
    events = old.get("events", [])
    if state != old.get("status"):
        event = {"id": uuid.uuid4().hex, "at_ms": now_ms, "status": state}
        events = (events+[event])[-100:]
    else:
        event = events[-1] if events else {"id": uuid.uuid4().hex, "at_ms": now_ms, "status": state}
    result = {"status": state, "checked_ms": now_ms, "last_publication_ms": last,
              "overdue_after_seconds": overdue_ms//1000, "events": events,
              "outage_publication_ms": (old.get("outage_publication_ms", last or 0) if outage else last or 0)}
    store.set_state("research_health", result)
    if state in {"process_down", "scan_overdue"} or (state == "healthy" and any(e["status"] in {"process_down", "scan_overdue"} for e in events[:-1])):
        message = f"Research scanner: {state.replace('_', ' ')}. Last completed scan: {last or 'none'} (UTC epoch ms). Research only; trading is unchanged."
        for channel in ("telegram", "discord"):
            if not cfg["alerts"][f"{channel}_enabled"]:
                continue
            key = f"health_alert:{channel}"
            sent = store.state(key, {})
            if sent.get("event_id") == event["id"] or now_ms-sent.get("attempt_ms", 0) < 300_000:
                continue
            delivered = send_message(channel, message, store)
            store.set_state(key, {"attempt_ms": now_ms, "event_id": event["id"] if delivered else None})
    return result
