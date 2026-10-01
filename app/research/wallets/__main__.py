"""Explicit wallet evidence pilot; disabled chains never contact a provider."""
import argparse
import json
from pathlib import Path

from .config import load_config
from .service import WalletEvidenceService
from .store import WalletStore


def main():
    parser = argparse.ArgumentParser(description="Read-only wallet evidence pilot. Does not place trades.")
    parser.add_argument("command", choices=("check", "collect", "status"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/research/wallets.sqlite3"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "check":
        print(json.dumps({"valid": True, "enabled_chains": [n for n, c in cfg["chains"].items() if c["enabled"]],
                          "mode": "read_only_evidence_pilot", "network_requests": 0}, indent=2))
        return
    service = WalletEvidenceService(cfg, WalletStore(args.database))
    if args.command == "status":
        print(json.dumps(service.status(), indent=2))
        return
    try:
        while True:
            print(json.dumps(service.cycle(), allow_nan=False), flush=True)
            if args.once or service.stop.wait(cfg["poll_seconds"]):
                break
    except KeyboardInterrupt:
        service.stop.set()


if __name__ == "__main__":
    main()
