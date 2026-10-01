from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from app.exchange.coindcx_rest import HTTPResponse
from app.research.wallets.config import address, load_config, scope_id, validate
from app.research.wallets.evm import EVMCollector, TRANSFER_TOPIC
from app.research.wallets.model import transfer
from app.research.wallets.rpc import EvidenceUnavailable, ReadOnlyRPC
from app.research.wallets.service import WalletEvidenceService
from app.research.wallets.solana import SolanaCollector, SYSTEM, TOKEN
from app.research.wallets.store import WalletStore
from app.research.wallets.status import public_status

ALICE, BOB, COIN = ["0x" + c * 40 for c in ("a", "b", "c")]
TX, HASH, PARENT = ["0x" + c * 64 for c in ("1", "2", "3")]
SOL_A = "So11111111111111111111111111111111111111112"
SOL_B = TOKEN
SOL_C = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = 1_800_000_000_000


def config(name="ethereum", tokens=False):
    cfg = load_config()
    chain = cfg["chains"][name]
    chain["enabled"] = True
    chain["wallets"] = [{"address": SOL_A if name == "solana" else ALICE,
                         "entity_id": None, "attribution": "unknown", "evidence": None}]
    if name == "solana":
        chain["expected_genesis_hash"] = SYSTEM
    if tokens:
        chain["assets"].append({"address": SOL_C if name == "solana" else COIN, "symbol": "TEST", "decimals": 6})
    return cfg


def evm_block(height=10, amount=10**18):
    return {"number": hex(height), "hash": HASH, "parentHash": PARENT, "timestamp": hex(NOW // 1000 - 30),
            "transactions": [{"hash": TX, "from": ALICE, "to": BOB, "value": hex(amount)}]}


def receipt(height=10, status=1):
    return {"blockHash": HASH, "blockNumber": hex(height), "transactionHash": TX, "status": hex(status)}


def token_log(index=0, amount=1000000):
    return {"address": COIN, "blockHash": HASH, "blockNumber": "0xa", "transactionHash": TX,
            "logIndex": hex(index), "removed": False, "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + ALICE[2:],
                                                                    "0x" + "0" * 24 + BOB[2:]],
            "data": "0x" + format(amount, "064x")}


class EVMRPC:
    def __init__(self):
        self.block = evm_block()
        self.receipt = receipt()
        self.logs = []
        self.chain = "0x1"
        self.decimals = "0x6"
        self.calls = []

    def reset_budget(self):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return self.chain
        if method == "eth_getBlockByNumber":
            return deepcopy(self.block)
        if method == "eth_getTransactionReceipt":
            return deepcopy(self.receipt)
        if method == "eth_getLogs":
            return deepcopy(self.logs)
        if method == "eth_call":
            return self.decimals
        raise AssertionError(method)


def sol_block(failed=False, inner=True):
    instruction = {"programId": SYSTEM, "parsed": {"type": "transfer", "info": {
        "source": SOL_A, "destination": SOL_B, "lamports": 1000000001}}}
    return {"blockhash": SOL_A, "previousBlockhash": SOL_B, "parentSlot": 8, "blockTime": NOW // 1000 - 20,
            "transactions": [{"transaction": {"signatures": ["signature-1"], "message": {
                "accountKeys": [{"pubkey": SYSTEM}, {"pubkey": SOL_C}],
                "instructions": [instruction]}}, "meta": {"err": {"failed": 1} if failed else None,
                    "innerInstructions": [{"index": 0, "instructions": [deepcopy(instruction)]}] if inner else [],
                    "preTokenBalances": [], "postTokenBalances": []}}]}


def sol_token_block():
    raw = sol_block(inner=False)
    tx = raw["transactions"][0]
    tx["transaction"]["message"]["instructions"] = [{"programId": TOKEN, "parsed": {
        "type": "transferChecked", "info": {"source": SYSTEM, "destination": SOL_C, "mint": SOL_C,
                                            "tokenAmount": {"amount": "1234567", "decimals": 6}}}}]
    tx["meta"]["preTokenBalances"] = [
        {"accountIndex": 0, "mint": SOL_C, "owner": SOL_A, "uiTokenAmount": {"decimals": 6}},
        {"accountIndex": 1, "mint": SOL_C, "owner": SOL_B, "uiTokenAmount": {"decimals": 6}}]
    return raw


class SolRPC:
    def __init__(self, block=None):
        self.raw = sol_block() if block is None else block
        self.genesis, self.head, self.earliest, self.slots = SYSTEM, 10, 0, [10]

    def reset_budget(self):
        pass

    def call(self, method, params):
        if method == "getGenesisHash":
            return self.genesis
        if method == "getSlot":
            return self.head
        if method == "getFirstAvailableBlock":
            return self.earliest
        if method == "getBlocks":
            return self.slots
        if method == "getBlock":
            return deepcopy(self.raw)
        raise AssertionError(method)


class WalletConfigTests(unittest.TestCase):
    def test_default_disabled_and_no_secret_values(self):
        cfg = load_config()
        self.assertEqual(set(cfg["chains"]), {"ethereum", "bsc", "solana"})
        self.assertFalse(any(c["enabled"] for c in cfg["chains"].values()))

    def test_reject_unproven_identity_and_missing_solana_network(self):
        cfg = config()
        cfg["chains"]["ethereum"]["wallets"][0]["entity_id"] = "big-fund"
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg = config("solana")
        cfg["chains"]["solana"]["expected_genesis_hash"] = None
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_reject_credentials_fields_and_duplicates(self):
        for key, value in (("api_key", "secret"), ("wallets", [])):
            cfg = config()
            cfg["chains"]["ethereum"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate(cfg)
        cfg = config()
        cfg["chains"]["ethereum"]["wallets"] *= 2
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_address_normalization_and_solana_byte_length(self):
        self.assertEqual(address("ethereum", "0x" + "A" * 40), ALICE)
        self.assertEqual(address("solana", SYSTEM), SYSTEM)
        for bad in ("111", "1" * 33, "0" * 44):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                address("solana", bad)

    def test_scope_changes_with_coverage_or_attribution(self):
        cfg = config()["chains"]["ethereum"]
        before = scope_id("ethereum", cfg)
        cfg["wallets"][0]["address"] = BOB
        self.assertNotEqual(before, scope_id("ethereum", cfg))
        self.assertNotEqual(before, scope_id("bsc", cfg))


class TransferTests(unittest.TestCase):
    def test_large_integer_is_lossless_and_no_invented_direction(self):
        cfg = config()["chains"]["ethereum"]
        e = transfer("ethereum", cfg, TX, "0", "native", 10**30 + 1, ALICE, BOB)
        self.assertEqual(e["quantity"], "1000000000000.000000000000000001")
        self.assertEqual(e["raw_amount"], str(10**30 + 1))
        self.assertIsNone(e["usd_value"])
        self.assertEqual(e["direction"], "unknown")

    def test_unwatched_unsupported_zero_are_not_events(self):
        cfg = config()["chains"]["ethereum"]
        self.assertIsNone(transfer("ethereum", cfg, TX, "0", "native", 10, BOB, COIN))
        self.assertIsNone(transfer("ethereum", cfg, TX, "0", COIN, 10, ALICE, BOB))
        self.assertIsNone(transfer("ethereum", cfg, TX, "0", "native", 0, ALICE, BOB))

    def test_only_verified_shared_entity_is_internal(self):
        cfg = config()["chains"]["ethereum"]
        cfg["wallets"] = [{"address": a, "entity_id": "entity", "attribution": "probable", "evidence": "reference"}
                          for a in (ALICE, BOB)]
        self.assertEqual(transfer("ethereum", cfg, TX, "0", "native", 1, ALICE, BOB)["action"], "transfer")
        for w in cfg["wallets"]:
            w["attribution"] = "verified"
        self.assertEqual(transfer("ethereum", cfg, TX, "0", "native", 1, ALICE, BOB)["action"], "internal_transfer")
        cfg["wallets"][1]["entity_id"] = "different-exchange"
        self.assertEqual(transfer("ethereum", cfg, TX, "0", "native", 1, ALICE, BOB)["action"], "transfer")


class EVMTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(tokens=True)["chains"]["ethereum"]
        self.rpc = EVMRPC()
        self.collector = EVMCollector("ethereum", self.cfg, self.rpc)

    def test_network_and_finalized_selection(self):
        self.assertEqual(self.collector.head(), 10)
        self.assertIn(("eth_getBlockByNumber", ["finalized", False]), self.rpc.calls)
        self.rpc.chain = "0x38"
        with self.assertRaises(EvidenceUnavailable):
            self.collector.head()
        self.assertEqual(EVMCollector("bsc", self.cfg, self.rpc).head(), 10)

    def test_native_and_multiple_token_events_in_one_transaction(self):
        self.rpc.logs = [token_log(0), token_log(1, 2)]
        block = self.collector.block(10)
        self.assertEqual(len(block["events"]), 3)
        self.assertEqual(len({e["id"] for e in block["events"]}), 3)
        self.assertEqual([e["quantity"] for e in block["events"]], ["1.000000000000000000", "1.000000", "0.000002"])
        self.assertEqual(sum(m == "eth_getTransactionReceipt" for m, _ in self.rpc.calls), 1)

    def test_failed_receipt_has_no_transfer_flow(self):
        self.rpc.receipt = receipt(status=0)
        self.rpc.logs = [token_log()]
        block = self.collector.block(10)
        self.assertEqual(block["events"], [])
        self.assertEqual(block["diagnostics"]["failed_transactions"], 1)

    def test_decimals_mismatch_rejected(self):
        self.rpc.decimals = "0x12"
        with self.assertRaises(EvidenceUnavailable):
            self.collector.block(10)

    def test_removed_logs_and_receipt_block_mismatch_rejected(self):
        self.rpc.logs = [{**token_log(), "removed": True}]
        with self.assertRaises(EvidenceUnavailable):
            self.collector.block(10)
        self.rpc.logs = []
        self.rpc.receipt["blockHash"] = PARENT
        with self.assertRaises(EvidenceUnavailable):
            self.collector.block(10)

    def test_finalized_history_conflict_blocks_progress(self):
        with self.assertRaises(EvidenceUnavailable):
            self.collector.verify_last({"height": 10, "hash": PARENT})


class SolanaTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config("solana", tokens=True)["chains"]["solana"]

    def test_outer_and_inner_instruction_identities(self):
        events = SolanaCollector("solana", self.cfg, SolRPC()).block(10)["events"]
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["id"], events[1]["id"])
        self.assertEqual(events[0]["quantity"], "1.000000001")

    def test_failed_transaction_ignored(self):
        block = SolanaCollector("solana", self.cfg, SolRPC(sol_block(failed=True))).block(10)
        self.assertEqual(block["events"], [])
        self.assertEqual(block["diagnostics"]["failed_transactions"], 1)

    def test_owner_resolution_for_token_transfer(self):
        event = SolanaCollector("solana", self.cfg, SolRPC(sol_token_block())).block(10)["events"][0]
        self.assertEqual(event["from_address"], SOL_A)
        self.assertEqual(event["details"]["from_token_account"], SYSTEM)
        self.assertEqual(event["quantity"], "1.234567")

    def test_owner_ambiguity_is_not_invented(self):
        raw = sol_token_block()
        raw["transactions"][0]["meta"]["postTokenBalances"] = [{"accountIndex": 0, "mint": SOL_C,
            "owner": SOL_B, "uiTokenAmount": {"decimals": 6}}]
        block = SolanaCollector("solana", self.cfg, SolRPC(raw)).block(10)
        self.assertEqual(block["events"], [])
        self.assertEqual(block["diagnostics"]["unresolved_token_owners"], 1)

    def test_spl_decimals_mismatch_rejected(self):
        self.cfg["assets"][1]["decimals"] = 9
        with self.assertRaises(EvidenceUnavailable):
            SolanaCollector("solana", self.cfg, SolRPC(sol_token_block())).block(10)

    def test_wrong_genesis_pruned_history_and_invalid_slots(self):
        rpc = SolRPC()
        collector = SolanaCollector("solana", self.cfg, rpc)
        rpc.genesis = SOL_A
        with self.assertRaises(EvidenceUnavailable):
            collector.head()
        rpc.earliest = 20
        with self.assertRaises(EvidenceUnavailable):
            collector.heights(10, 12, 5)
        rpc.earliest, rpc.slots = 0, [11, 10]
        with self.assertRaises(EvidenceUnavailable):
            collector.heights(10, 12, 5)

    def test_missing_time_is_preserved_unknown(self):
        raw = sol_block()
        raw["blockTime"] = None
        self.assertIsNone(SolanaCollector("solana", self.cfg, SolRPC(raw)).block(10)["event_ms"])

    def test_oversized_solana_quantity_rejected(self):
        raw = sol_token_block()
        raw["transactions"][0]["transaction"]["message"]["instructions"][0]["parsed"]["info"]["tokenAmount"]["amount"] = str(2**64)
        with self.assertRaises(ValueError):
            SolanaCollector("solana", self.cfg, SolRPC(raw)).block(10)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WalletStore(Path(self.tmp.name) / "wallets.sqlite3")
        self.cfg = config()["chains"]["ethereum"]
        self.scope = scope_id("ethereum", self.cfg)
        self.store.initialize(self.scope, "ethereum", self.cfg, 10, NOW)
        self.block = EVMCollector("ethereum", self.cfg, EVMRPC()).block(10)

    def test_duplicate_delivery_and_restart_are_idempotent(self):
        self.block["events"] *= 2
        self.assertTrue(self.store.commit_block(self.scope, 10, self.block, NOW))
        self.assertFalse(self.store.commit_block(self.scope, 10, self.block, NOW + 100))
        store = WalletStore(self.store.path)
        self.assertEqual(store.cursor(self.scope), 11)
        self.assertEqual(len(store.events(self.scope, available_ms=NOW + 200)), 1)

    def test_late_backfill_does_not_leak_into_earlier_prediction(self):
        self.store.commit_block(self.scope, 10, self.block, NOW)
        self.assertEqual(self.store.events(self.scope, available_ms=NOW - 1), [])
        event = self.store.events(self.scope, available_ms=NOW)[0]
        self.assertLess(event["event_ms"], event["available_ms"])

    def test_checkpoint_conflict_does_not_partially_write(self):
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 9, self.block, NOW)
        self.assertIsNone(self.store.latest_block(self.scope))
        self.assertEqual(self.store.cursor(self.scope), 10)

    def test_finalized_replacement_requires_manual_reconciliation(self):
        self.store.commit_block(self.scope, 10, self.block, NOW)
        altered = {**self.block, "hash": PARENT}
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 10, altered, NOW + 100)
        self.assertEqual(self.store.latest_block(self.scope)["hash"], HASH)

    def test_same_hash_changed_evidence_is_rejected(self):
        self.store.commit_block(self.scope, 10, self.block, NOW)
        self.block["raw"]["block"]["timestamp"] = "0x0"
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 10, self.block, NOW)

    def test_storage_budget_preserves_history_and_checkpoint(self):
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 10, self.block, NOW, max_storage_bytes=1)
        self.assertEqual(self.store.cursor(self.scope), 10)
        self.assertEqual(self.store.events(self.scope, available_ms=NOW), [])

    def test_readonly_store_does_not_allow_mutations(self):
        readonly = WalletStore(self.store.path, readonly=True)
        self.assertEqual(readonly.cursor(self.scope), 10)
        with self.assertRaises(Exception):
            readonly.set_status(self.scope, {"checked_ms": NOW})

    def test_conflicting_event_rolls_back_entire_block(self):
        event = deepcopy(self.block["events"][0])
        event["raw_amount"] = "999"
        self.block["events"].append(event)
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 10, self.block, NOW)
        self.assertEqual(self.store.events(self.scope, available_ms=NOW), [])
        self.assertEqual(self.store.cursor(self.scope), 10)

    def test_parent_gap_and_future_timestamp_rejected(self):
        self.store.commit_block(self.scope, 10, self.block, NOW)
        block = {**self.block, "height": 12, "parent_height": 11, "parent_hash": HASH}
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 11, block, NOW)
        block = {**self.block, "height": 11, "event_ms": NOW + 120000}
        with self.assertRaises(EvidenceUnavailable):
            self.store.commit_block(self.scope, 11, block, NOW)

    def test_chain_scopes_are_isolated(self):
        scope = scope_id("bsc", self.cfg)
        self.store.initialize(scope, "bsc", self.cfg, 10, NOW)
        self.store.commit_block(self.scope, 10, self.block, NOW)
        self.assertEqual(self.store.events(scope, available_ms=NOW), [])
        self.assertEqual(self.store.cursor(scope), 10)

    def test_competing_collectors_lease_expiry_and_owner_release(self):
        self.assertTrue(self.store.claim(self.scope, "first", NOW, NOW + 1000))
        self.assertFalse(self.store.claim(self.scope, "second", NOW + 1, NOW + 2000))
        self.store.release(self.scope, "second")
        self.assertFalse(self.store.claim(self.scope, "second", NOW + 2, NOW + 2000))
        self.assertTrue(self.store.claim(self.scope, "second", NOW + 1001, NOW + 2000))

    def test_dashboard_reports_configured_schedule_before_start(self):
        from app.research.continuous.service import ContinuousResearchService
        service = ContinuousResearchService(Path(self.tmp.name) / "research.sqlite3", autostart=False)
        status = service.status()
        self.assertEqual(status["market_interval"], "5m")
        self.assertEqual(status["interval_seconds"], 300)
        self.assertIn("wallet_evidence", status)


class RPCTests(unittest.TestCase):
    def client(self, result=None, code=200):
        transport = Mock()
        transport.request.return_value = HTTPResponse(code, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}), {})
        stop = Mock()
        stop.wait.return_value = False
        return ReadOnlyRPC("https://rpc.example.test/secret-key", load_config()["network"], stop, transport), transport

    def test_write_methods_denied_before_network(self):
        rpc, transport = self.client()
        for method in ("eth_sendRawTransaction", "sendTransaction", "requestAirdrop"):
            with self.subTest(method=method), self.assertRaises(EvidenceUnavailable):
                rpc.call(method, [])
        transport.request.assert_not_called()

    def test_successful_read_and_bounded_requests(self):
        rpc, _ = self.client("0x1")
        self.assertEqual(rpc.call("eth_chainId", []), "0x1")
        rpc.remaining = 0
        with self.assertRaises(EvidenceUnavailable):
            rpc.call("eth_chainId", [])

    def test_secret_errors_redacted_and_rate_limit_backoff(self):
        rpc, transport = self.client()
        transport.request.side_effect = RuntimeError("https://rpc.example.test/secret-key")
        with self.assertRaises(EvidenceUnavailable) as err:
            rpc.call("eth_chainId", [])
        self.assertNotIn("secret-key", str(err.exception))
        rpc, transport = self.client(code=429)
        for _ in range(2):
            with self.assertRaises(EvidenceUnavailable):
                rpc.call("eth_chainId", [])
        self.assertEqual(transport.request.call_count, 1)

    def test_mismatched_response_id_rejected(self):
        rpc, transport = self.client()
        transport.request.return_value = HTTPResponse(200, '{"jsonrpc":"2.0","id":9,"result":"0x1"}', {})
        with self.assertRaises(EvidenceUnavailable):
            rpc.call("eth_chainId", [])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WalletStore(Path(self.tmp.name) / "wallets.sqlite3")

    def test_disabled_and_missing_credentials_never_call_provider(self):
        factory = Mock(side_effect=AssertionError("Must not call"))
        service = WalletEvidenceService(load_config(), self.store, rpc_factory=factory, environ={})
        self.assertTrue(all(c["status"] == "disabled" for c in service.cycle()["chains"].values()))
        factory.assert_not_called()
        service = WalletEvidenceService(config(), self.store, rpc_factory=factory, environ={})
        self.assertEqual(service.cycle()["chains"]["ethereum"]["status"], "unavailable")
        factory.assert_not_called()

    def test_restart_reuses_checkpoint_and_never_calls_it_a_prediction(self):
        cfg, rpc = config(), EVMRPC()
        factory = lambda *args: rpc
        for _ in range(2):
            service = WalletEvidenceService(cfg, self.store, rpc_factory=factory,
                                             environ={"WALLET_ETH_RPC_URL": "fake"}, clock=lambda: NOW)
            status = service.cycle()
            self.assertFalse(status["predictions_enabled"])
            chain = status["chains"]["ethereum"]
            self.assertEqual(chain["status"], "limited")
            self.assertEqual(chain["event_count"], 1)
            self.assertTrue(chain["caught_up_to_observed_head"])

    def test_provider_failure_does_not_advance_checkpoint(self):
        cfg, rpc = config(), EVMRPC()
        rpc.receipt["blockHash"] = PARENT
        service = WalletEvidenceService(cfg, self.store, rpc_factory=lambda *args: rpc,
                                       environ={"WALLET_ETH_RPC_URL": "fake"}, clock=lambda: NOW)
        chain = service.cycle()["chains"]["ethereum"]
        self.assertEqual(chain["status"], "unavailable")
        self.assertEqual(self.store.cursor(chain["scope"]), 10)
        self.assertEqual(chain["event_count"], 0)

    def test_skipped_solana_slots_can_advance_without_events(self):
        cfg, rpc = config("solana"), SolRPC()
        rpc.slots = []
        service = WalletEvidenceService(cfg, self.store, rpc_factory=lambda *args: rpc,
                                       environ={"WALLET_SOL_RPC_URL": "fake"}, clock=lambda: NOW)
        status = service.cycle()["chains"]["solana"]
        self.assertEqual(status["status"], "limited")
        self.assertEqual(self.store.cursor(status["scope"]), 11)
        self.assertEqual(status["event_count"], 0)

    def test_dashboard_readiness_creates_no_database_and_hides_secrets(self):
        path = Path(self.tmp.name) / "absent.sqlite3"
        status = public_status(path, environ={"WALLET_ETH_RPC_URL": "https://example/secret-key"})
        self.assertFalse(path.exists())
        self.assertEqual(status["chains"]["ethereum"]["status"], "not_configured")
        self.assertTrue(status["chains"]["ethereum"]["rpc_configured"])
        self.assertNotIn("secret-key", json.dumps(status))

    def test_dashboard_uses_collectors_environment_readiness(self):
        cfg = config()
        path = Path(self.tmp.name) / "wallet_config.json"
        path.write_text(json.dumps(cfg))
        service = WalletEvidenceService(cfg, self.store, rpc_factory=lambda *args: EVMRPC(),
                                       environ={"WALLET_ETH_RPC_URL": "fake"}, clock=lambda: NOW)
        service.cycle()
        status = public_status(self.store.path, path, environ={})
        self.assertTrue(status["chains"]["ethereum"]["rpc_configured"])
        self.assertEqual(status["chains"]["ethereum"]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
