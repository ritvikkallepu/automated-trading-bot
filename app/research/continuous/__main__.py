"""Standalone research worker and local evidence replay for Windows or Ubuntu."""
import argparse
import json
from pathlib import Path
import time

from .config import load_config
from .replay import replay_stored, summary
from .validation import evaluate_stored, validation_summary
from .service import ContinuousResearchService
from .store import HistoryStore


def main():
    parser = argparse.ArgumentParser(description="Read-only continuous research; no trades")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/research/research.sqlite3"))
    parser.add_argument("--replay", action="store_true", help="Replay stored prices/plans offline, then exit")
    args = parser.parse_args()
    if args.replay:
        store = HistoryStore(args.database)
        now_ms = int(time.time()*1000)
        replay_stored(store, {}, now_ms)
        evaluate_stored(store, now_ms)
        print(json.dumps({"setups": summary(store.replay_results()),
                          "validation": validation_summary(store.validation_results())}, indent=2, allow_nan=False))
        return
    load_config(args.config)
    service = ContinuousResearchService(args.database, config_path=args.config)
    service.start()
    print("Read-only research running. History:", args.database.resolve(), flush=True)
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        service.close()


if __name__ == "__main__":
    main()
