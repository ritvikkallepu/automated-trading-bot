"""Finalized EVM native and ERC-20 transfer evidence, with declared trace gaps."""
import re

from .config import CHAINS, address
from .model import evm_number, transfer
from .rpc import EvidenceUnavailable

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
LIMITATIONS = ["Watched wallets and explicitly mapped assets only",
               "Native transfers inside contract calls are not collected in this pilot",
               "Transfers are not decoded swaps; USD valuation and smart-money scores unavailable",
               "Finalized evidence only; not a subminute early-warning feed"]


def tx_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("Invalid EVM hash")
    return value.lower()


class EVMCollector:
    def __init__(self, chain, cfg, rpc):
        self.chain, self.cfg, self.rpc = chain, cfg, rpc

    def head(self):
        if evm_number(self.rpc.call("eth_chainId", [])) != CHAINS[self.chain]:
            raise EvidenceUnavailable("RPC network does not match configured mainnet")
        head = self.rpc.call("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(head, dict):
            raise EvidenceUnavailable("Finalized block unavailable; no latest-block fallback")
        return evm_number(head["number"])

    def verify_last(self, last):
        if last:
            header = self.rpc.call("eth_getBlockByNumber", [hex(last["height"]), False])
            if not header or tx_hash(header["hash"]) != last["hash"]:
                raise EvidenceUnavailable("Finalized history conflict; checkpoint retained")

    def heights(self, start, head, count):
        return list(range(start, min(head + 1, start + count)))

    def block(self, height):
        raw = self.rpc.call("eth_getBlockByNumber", [hex(height), True])
        if not isinstance(raw, dict) or evm_number(raw["number"]) != height:
            raise EvidenceUnavailable("Requested block unavailable; checkpoint retained")
        block_hash = tx_hash(raw["hash"])
        event_ms = evm_number(raw["timestamp"]) * 1000
        assets = {a["address"]: a for a in self.cfg["assets"]}
        watched = {w["address"] for w in self.cfg["wallets"]}
        events, receipts, logs = [], {}, []
        failed = 0

        def successful(transaction_hash):
            nonlocal failed
            if transaction_hash not in receipts:
                receipt = self.rpc.call("eth_getTransactionReceipt", [transaction_hash])
                if (not isinstance(receipt, dict) or tx_hash(receipt["blockHash"]) != block_hash
                        or tx_hash(receipt["transactionHash"]) != transaction_hash
                        or evm_number(receipt["blockNumber"]) != height):
                    raise EvidenceUnavailable("Receipt does not match finalized block")
                status = evm_number(receipt["status"])
                if status not in {0, 1}:
                    raise ValueError("Invalid receipt status")
                failed += status == 0
                receipts[transaction_hash] = receipt
            return evm_number(receipts[transaction_hash]["status"]) == 1

        for tx in raw["transactions"]:
            sender = address(self.chain, tx["from"])
            receiver = address(self.chain, tx["to"]) if tx["to"] else None
            amount = evm_number(tx["value"])
            if "native" not in assets or not amount or not ({sender, receiver} & watched):
                continue
            tid = tx_hash(tx["hash"])
            if not successful(tid):
                continue
            if receiver is None:
                receiver = address(self.chain, receipts[tid]["contractAddress"])
            event = transfer(self.chain, self.cfg, tid, "native:top", "native", amount, sender, receiver)
            if event:
                events.append(event)
        contracts = sorted(set(assets) - {"native"})
        decimals_evidence = {}
        if contracts:
            for contract in contracts:
                decimals = self.rpc.call("eth_call", [{"to": contract, "data": "0x313ce567"}, hex(height)])
                if evm_number(decimals) != assets[contract]["decimals"]:
                    raise EvidenceUnavailable("Token decimals do not match verified asset mapping")
                decimals_evidence[contract] = decimals
            logs = self.rpc.call("eth_getLogs", [{"blockHash": block_hash, "address": contracts, "topics": [TRANSFER_TOPIC]}])
            if not isinstance(logs, list):
                raise ValueError("Invalid logs response")
            for log in logs:
                contract = address(self.chain, log["address"])
                if (contract not in contracts or tx_hash(log["blockHash"]) != block_hash
                        or evm_number(log["blockNumber"]) != height or log.get("removed", False)):
                    raise EvidenceUnavailable("Log is outside finalized block or configured assets")
                topics = log["topics"]
                if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
                    raise EvidenceUnavailable("Mapped token has unsupported transfer log structure")
                if any(not re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", topic) for topic in topics[1:]):
                    raise ValueError("Invalid indexed transfer address")
                sender, receiver = ["0x" + t[-40:] for t in topics[1:]]
                sender, receiver = sender.lower(), receiver.lower()
                if not {sender, receiver} & watched:
                    continue
                if not re.fullmatch(r"0x[0-9a-fA-F]{64}", log["data"]):
                    raise ValueError("Invalid transfer amount encoding")
                tid = tx_hash(log["transactionHash"])
                if not successful(tid):
                    continue
                event = transfer(self.chain, self.cfg, tid, f"log:{evm_number(log['logIndex'])}", contract,
                                 evm_number(log["data"]), sender, receiver)
                if event:
                    events.append(event)
        return {"height": height, "hash": block_hash, "parent_height": height - 1,
                "parent_hash": tx_hash(raw["parentHash"]), "event_ms": event_ms, "events": events,
                "raw": {"block": raw, "receipts": receipts, "logs": logs, "decimals": decimals_evidence},
                "diagnostics": {"failed_transactions": failed, "limitations": LIMITATIONS}}
