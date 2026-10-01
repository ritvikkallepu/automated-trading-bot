"""Finalized Solana standard System/SPL transfers, retaining owner ambiguity."""
from .config import address
from .model import transfer
from .rpc import EvidenceUnavailable

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
LIMITATIONS = ["Watched owners and explicitly mapped assets only",
               "Only parsed standard System/SPL transfers; Token-2022 and opaque instructions remain unsupported",
               "Fees, rent, staking, wrapping and swaps are not economic-action decoded",
               "Finalized bounded block polling; not a subminute early-warning feed",
               "USD valuation and smart-money scores unavailable"]


def uint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("Invalid Solana integer")
    return value


class SolanaCollector:
    def __init__(self, chain, cfg, rpc):
        self.chain, self.cfg, self.rpc = chain, cfg, rpc

    def head(self):
        if self.rpc.call("getGenesisHash", []) != self.cfg["expected_genesis_hash"]:
            raise EvidenceUnavailable("RPC genesis does not match independently configured network")
        return uint(self.rpc.call("getSlot", [{"commitment": "finalized"}]))

    def fetch(self, height, full=True):
        raw = self.rpc.call("getBlock", [height, {"commitment": "finalized", "encoding": "jsonParsed",
                                                 "transactionDetails": "full" if full else "none",
                                                 "rewards": False, "maxSupportedTransactionVersion": 0}])
        if not isinstance(raw, dict):
            raise EvidenceUnavailable("Requested Solana block unavailable; checkpoint retained")
        return raw

    def verify_last(self, last):
        if last and self.fetch(last["height"], full=False)["blockhash"] != last["hash"]:
            raise EvidenceUnavailable("Finalized history conflict; checkpoint retained")

    def heights(self, start, head, count):
        earliest = uint(self.rpc.call("getFirstAvailableBlock", []))
        if start < earliest:
            raise EvidenceUnavailable("Provider history is pruned before checkpoint; archive backfill required")
        end = min(head, start + count - 1)
        values = self.rpc.call("getBlocks", [start, end, {"commitment": "finalized"}])
        if (not isinstance(values, list) or any(type(v) is not int or not start <= v <= end for v in values)
                or sorted(set(values)) != values):
            raise EvidenceUnavailable("Invalid finalized slot range; checkpoint retained")
        return values

    def block(self, height):
        raw = self.fetch(height)
        block_hash = address("solana", raw["blockhash"])
        event_ms = None if raw["blockTime"] is None else uint(raw["blockTime"]) * 1000
        events = []
        diagnostics = {"failed_transactions": 0, "unsupported_instructions": 0,
                       "unresolved_token_owners": 0, "limitations": LIMITATIONS}
        for item in raw["transactions"]:
            meta = item["meta"]
            if not isinstance(meta, dict) or "err" not in meta:
                raise EvidenceUnavailable("Transaction metadata missing; checkpoint retained")
            if meta["err"] is not None:
                diagnostics["failed_transactions"] += 1
                continue
            tx = item["transaction"]
            signature = tx["signatures"][0]
            if not isinstance(signature, str) or not signature:
                raise ValueError("Missing transaction signature")
            keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["message"]["accountKeys"]]
            token_accounts = {}
            for row in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
                index = uint(row["accountIndex"])
                if index >= len(keys):
                    raise ValueError("Token account index out of bounds")
                info = (row["mint"], row.get("owner"), row["uiTokenAmount"]["decimals"])
                token_accounts.setdefault(keys[index], set()).add(info)
            instructions = [(f"ix:{i}", ix) for i, ix in enumerate(tx["message"]["instructions"])]
            if meta.get("innerInstructions") is None:
                diagnostics["inner_instructions_unavailable"] = True
            for group in meta.get("innerInstructions") or []:
                index = uint(group["index"])
                if index >= len(tx["message"]["instructions"]):
                    raise ValueError("Inner instruction index out of bounds")
                instructions.extend((f"ix:{index}/inner:{i}", ix) for i, ix in enumerate(group["instructions"]))
            for path, instruction in instructions:
                parsed = instruction.get("parsed")
                program = instruction.get("programId")
                if not isinstance(parsed, dict):
                    diagnostics["unsupported_instructions"] += 1
                    continue
                kind, info = parsed.get("type"), parsed.get("info", {})
                event = None
                if program == SYSTEM and kind in {"transfer", "transferWithSeed"}:
                    event = transfer("solana", self.cfg, signature, path, "native", uint(info["lamports"]),
                                     info["source"], info["destination"])
                elif program == TOKEN and kind in {"transfer", "transferChecked"}:
                    source, target = info["source"], info["destination"]
                    left, right = token_accounts.get(source, set()), token_accounts.get(target, set())
                    if len(left) != 1 or len(right) != 1:
                        diagnostics["unresolved_token_owners"] += 1
                        continue
                    lm, lo, ld = next(iter(left))
                    rm, ro, rd = next(iter(right))
                    if lm != rm or ld != rd or not lo or not ro:
                        diagnostics["unresolved_token_owners"] += 1
                        continue
                    asset = next((a for a in self.cfg["assets"] if a["address"] == lm), None)
                    if not asset:
                        continue
                    if type(ld) is not int or ld != asset["decimals"]:
                        raise EvidenceUnavailable("SPL token decimals do not match asset mapping")
                    if kind == "transferChecked":
                        if info["mint"] != lm or info["tokenAmount"]["decimals"] != ld:
                            raise ValueError("SPL transfer metadata mismatch")
                        amount = info["tokenAmount"]["amount"]
                    else:
                        amount = info["amount"]
                    if not isinstance(amount, str) or not amount.isascii() or not amount.isdigit():
                        raise ValueError("SPL amount must be an unsigned integer string")
                    event = transfer("solana", self.cfg, signature, path, lm, uint(int(amount)), lo, ro,
                                     from_token_account=source, to_token_account=target)
                else:
                    diagnostics["unsupported_instructions"] += 1
                if event:
                    events.append(event)
        return {"height": height, "hash": block_hash, "parent_height": uint(raw["parentSlot"]),
                "parent_hash": address("solana", raw["previousBlockhash"]), "event_ms": event_ms,
                "events": events, "raw": raw, "diagnostics": diagnostics}
