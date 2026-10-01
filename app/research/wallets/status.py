"""Dashboard reads only: inspecting readiness must not start collectors or create history."""
import os

from .config import load_config, scope_id
from .evm import LIMITATIONS as EVM_LIMITATIONS
from .solana import LIMITATIONS as SOL_LIMITATIONS
from .store import WalletStore


def public_status(database, config_path=None, environ=None):
    environ = os.environ if environ is None else environ
    result = {"mode": "read_only_evidence_pilot", "predictions_enabled": False, "chains": {}}
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "error": "Wallet configuration unavailable or invalid"}
    try:
        store = WalletStore(database, readonly=True)
    except FileNotFoundError:
        store = None
    for name, chain in cfg["chains"].items():
        status = {"status": "not_started" if chain["enabled"] else "not_configured", "checked_ms": None,
                  "event_count": 0, "stale": True}
        if store:
            try:
                status.update(store.status(scope_id(name, chain), cfg["poll_seconds"] * 3000))
            except Exception:
                status.update(status="unavailable", reason="Wallet history could not be read")
        if not chain["enabled"]:
            status.update(status="disabled" if chain["wallets"] else "not_configured")
        result["chains"][name] = {**status, "enabled": chain["enabled"],
                                  "wallet_count": len(chain["wallets"]), "asset_count": len(chain["assets"]),
                                  "rpc_configured": status.get("rpc_configured", bool(environ.get(chain["rpc_url_env"]))),
                                  "limitations": SOL_LIMITATIONS if name == "solana" else EVM_LIMITATIONS}
    return result
