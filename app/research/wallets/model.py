"""Exact, non-directional transfer evidence shared by the chain adapters."""
from decimal import Decimal, localcontext
import re

from .config import address

DECODER_VERSION = "transfers-v1"


def evm_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ValueError("Invalid EVM quantity")
    return int(value, 16)


def transfer(chain, cfg, tx, path, asset_key, raw_amount, sender, receiver, **details):
    assets = {a["address"]: a for a in cfg["assets"]}
    if asset_key not in assets:
        return None
    wallets = {w["address"]: w for w in cfg["wallets"]}
    sender, receiver = address(chain, sender), address(chain, receiver)
    if sender not in wallets and receiver not in wallets:
        return None
    if type(raw_amount) is not int or raw_amount < 0:
        raise ValueError("Invalid transfer amount")
    if raw_amount == 0:
        return None
    asset = assets[asset_key]
    with localcontext() as ctx:
        ctx.prec = max(100, len(str(raw_amount)) + asset["decimals"] + 1)
        quantity = format(Decimal(raw_amount).scaleb(-asset["decimals"]), "f")
    left, right = wallets.get(sender), wallets.get(receiver)
    same_entity = (sender == receiver or (left and right and left["attribution"] == right["attribution"] == "verified"
                                         and left["entity_id"] == right["entity_id"]))
    kind = "internal_transfer" if same_entity else "transfer"
    if chain != "solana" and (sender == "0x" + "0" * 40 or receiver == "0x" + "0" * 40):
        kind = "mint" if sender == "0x" + "0" * 40 else "burn"
    return {"id": f"{chain}:{tx}:{path}", "chain": chain, "transaction": tx, "event_path": path,
            "asset": asset_key, "symbol": asset["symbol"], "decimals": asset["decimals"],
            "raw_amount": str(raw_amount), "quantity": quantity, "from_address": sender, "to_address": receiver,
            "from_attribution": left, "to_attribution": right, "action": kind, "direction": "unknown",
            "usd_value": None, "valuation_status": "not_configured", "decoder_version": DECODER_VERSION,
            "details": details}
