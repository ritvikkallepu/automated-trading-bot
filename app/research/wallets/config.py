"""Separate, secret-free configuration for the bounded wallet evidence pilot."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "wallet_research.json"
CHAINS = {"ethereum": 1, "bsc": 56, "solana": None}
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def address(chain, value):
    if not isinstance(value, str):
        raise ValueError("Address must be a string")
    if chain != "solana":
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            raise ValueError("Invalid EVM address")
        return value.lower()
    if not 32 <= len(value) <= 44 or any(c not in BASE58 for c in value):
        raise ValueError("Invalid Solana address")
    n = 0
    for c in value:
        n = n * 58 + BASE58.index(c)
    size = (n.bit_length() + 7) // 8 + len(value) - len(value.lstrip("1"))
    if size != 32:
        raise ValueError("Solana address must encode 32 bytes")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Wallet configuration integer outside supported range")
    return value


def validate(cfg):
    cfg = deepcopy(cfg)
    if set(cfg) != {"schema_version", "poll_seconds", "network", "chains"} or cfg["schema_version"] != 1:
        raise ValueError("Invalid wallet configuration schema")
    integer(cfg["poll_seconds"], 5, 3600)
    net = cfg["network"]
    if set(net) != {"timeout_seconds", "requests_per_minute", "requests_per_cycle", "max_blocks_per_cycle", "max_storage_mb"}:
        raise ValueError("Invalid wallet network fields; credentials belong in the environment")
    for key, low, high in (("timeout_seconds", 1, 30), ("requests_per_minute", 1, 120),
                           ("requests_per_cycle", 5, 1000), ("max_blocks_per_cycle", 1, 100), ("max_storage_mb", 16, 102400)):
        integer(net[key], low, high)
    if set(cfg["chains"]) != set(CHAINS):
        raise ValueError("Configure ethereum, bsc, and solana separately")
    for name, chain in cfg["chains"].items():
        if set(chain) != {"enabled", "rpc_url_env", "expected_genesis_hash", "start_height", "wallets", "assets"}:
            raise ValueError("Invalid wallet chain fields")
        if type(chain["enabled"]) is not bool or not re.fullmatch(r"[A-Z][A-Z0-9_]+", chain["rpc_url_env"]):
            raise ValueError("Invalid wallet provider configuration")
        if chain["start_height"] is not None:
            integer(chain["start_height"], 0, 2**53 - 1)
        genesis = chain["expected_genesis_hash"]
        if name == "solana":
            if genesis is not None:
                address(name, genesis)
            if chain["enabled"] and genesis is None:
                raise ValueError("Solana requires an independently verified expected genesis hash")
        elif genesis is not None:
            raise ValueError("EVM network identity is verified using eth_chainId")
        if not isinstance(chain["wallets"], list) or len(chain["wallets"]) > 100:
            raise ValueError("Pilot supports at most 100 explicitly selected wallets per chain")
        seen = set()
        for wallet in chain["wallets"]:
            if set(wallet) != {"address", "entity_id", "attribution", "evidence"}:
                raise ValueError("Invalid wallet attribution fields")
            wallet["address"] = address(name, wallet["address"])
            if wallet["address"] in seen:
                raise ValueError("Duplicate watched wallet")
            seen.add(wallet["address"])
            if wallet["attribution"] not in {"unknown", "probable", "verified"}:
                raise ValueError("Unknown attribution level")
            if wallet["attribution"] == "unknown":
                if wallet["entity_id"] is not None or wallet["evidence"] is not None:
                    raise ValueError("Unknown ownership cannot assert an entity")
            elif not all(isinstance(wallet[k], str) and 0 < len(wallet[k]) <= 500
                         for k in ("entity_id", "evidence")):
                raise ValueError("Attributed wallets require an entity and evidence reference")
        if chain["enabled"] and not seen:
            raise ValueError("Enable a chain only after selecting wallets")
        if not isinstance(chain["assets"], list) or not 1 <= len(chain["assets"]) <= 100:
            raise ValueError("Pilot requires 1-100 explicit assets per chain")
        assets = set()
        for asset in chain["assets"]:
            if set(asset) != {"address", "symbol", "decimals"}:
                raise ValueError("Invalid wallet asset fields")
            key = asset["address"]
            if key != "native":
                key = asset["address"] = address(name, key)
            if key in assets:
                raise ValueError("Duplicate asset mapping")
            assets.add(key)
            integer(asset["decimals"], 0, 36)
            if key == "native" and asset["decimals"] != (9 if name == "solana" else 18):
                raise ValueError("Incorrect native asset decimals")
            if not isinstance(asset["symbol"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,30}", asset["symbol"]):
                raise ValueError("Invalid asset display symbol")
    return cfg


def load_config(path=None):
    return validate(json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")))


def scope_id(chain_name, chain):
    # Changing coverage starts a new stream rather than relabeling old observations.
    scope = {k: chain[k] for k in ("wallets", "assets", "expected_genesis_hash", "start_height")}
    raw = json.dumps([chain_name, scope], sort_keys=True, allow_nan=False)
    return chain_name + ":" + hashlib.sha256(raw.encode()).hexdigest()[:20]
