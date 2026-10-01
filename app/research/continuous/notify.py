"""Opt-in phone alerts for research only; no keys or webhook URLs in saved evidence."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.parse import urlparse


def send_message(channel, text, store):
    try:
        if channel == "telegram":
            token, chat = os.environ.get("RESEARCH_TELEGRAM_BOT_TOKEN"), os.environ.get("RESEARCH_TELEGRAM_CHAT_ID")
            if not token or not chat:
                raise ValueError("Alert credentials missing")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            body = {"chat_id": chat, "text": text}
        elif channel == "discord":
            url = os.environ.get("RESEARCH_DISCORD_WEBHOOK_URL", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "discord.com" or not parsed.path.startswith("/api/webhooks/"):
                raise ValueError("Invalid Discord webhook")
            body = {"content": text, "allowed_mentions": {"parse": []}}
        else:
            raise ValueError("Unknown channel")
        request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=8) as response:
            if response.status not in (200, 204):
                raise ValueError("Alert provider rejected request")
            if channel == "telegram" and not json.loads(response.read()).get("ok"):
                raise ValueError("Telegram rejected request")
        store.set_state(f"alert_status:{channel}", {"status": "delivered", "at_ms": int(time.time()*1000)})
        return True
    except Exception:
        store.set_state(f"alert_status:{channel}", {"status": "failed", "reason": "Check alert credentials, destination and provider access", "at_ms": int(time.time()*1000)})
        return False


def publish_alerts(snapshot, store, cfg, *, cancelled=lambda: False):
    enabled = cfg["alerts"]
    if not (enabled["telegram_enabled"] or enabled["discord_enabled"]):
        return
    for row in snapshot["rows"]:
        setup = row.get("setup", {})
        event = setup.get("event", "updated")
        qualified = row.get("public_rank", {}).get("score", 0) >= cfg["ranking"]["candidate_score"]
        if event == "updated" or not setup or not (qualified or setup.get("was_candidate")):
            continue
        p = setup["frozen_plan"]
        text = (f"Research update: {event} | {row['pair']} | public score {row['public_rank']['score']:.1f}/100\n"
                f"{p['trigger']} entry {p['entry']:.8g} | stop {p['stop_loss']:.8g}\n"
                f"Targets {p['tp1']:.8g} / {p['tp2']:.8g} / {p['tp3']:.8g}\n"
                f"TP1 RR {p['risk_reward']:.2f} | snapshot {snapshot['id']}\n"
                "Frozen Binance research plan. Whale/holder evidence may be missing. Not an order or CoinDCX execution approval.")
        for channel in ("telegram", "discord"):
            if cancelled():
                return
            if not enabled[f"{channel}_enabled"]:
                continue
            state_key = f"setup_alert:{channel}:{setup['id']}:{event}"
            last = store.state(state_key, {})
            if last.get("sent_at"):
                continue
            pair_key = f"setup_alert_last:{channel}:{row['symbol']}"
            pair_last = store.state(pair_key, {})
            # Repeat research opportunities respect cooldown; lifecycle changes of
            # an already-announced setup (especially invalidation) remain timely.
            if event in {"created", "rank_qualified"} and time.time()-pair_last.get("sent_at", 0) < enabled["cooldown_seconds"]:
                continue
            if send_message(channel, text, store):
                store.set_state(state_key, {"sent_at": time.time(), "snapshot": snapshot["id"]})
                store.set_state(pair_key, {"sent_at": time.time(), "setup_id": setup["id"]})
