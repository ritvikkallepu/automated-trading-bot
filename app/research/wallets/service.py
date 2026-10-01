"""Independent read-only pilot cycles; no changes to rankings or trading."""
import os
import threading
import time
import uuid

from .config import scope_id, validate
from .evm import EVMCollector, LIMITATIONS as EVM_LIMITATIONS
from .solana import SolanaCollector, LIMITATIONS as SOL_LIMITATIONS
from .rpc import EvidenceUnavailable, ReadOnlyRPC


class WalletEvidenceService:
    def __init__(self, cfg, store, *, rpc_factory=ReadOnlyRPC, environ=None, stop=None, clock=None):
        self.cfg, self.store = validate(cfg), store
        self.rpc_factory = rpc_factory
        self.environ = os.environ if environ is None else environ
        self.stop = stop or threading.Event()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.clients = {}
        self.owner = uuid.uuid4().hex

    def cycle(self):
        statuses = {}
        for name, chain in self.cfg["chains"].items():
            scope = scope_id(name, chain)
            status = {"chain": name, "scope": scope, "checked_ms": self.clock(),
                      "status": "disabled", "trading_enabled": False,
                      "limitations": SOL_LIMITATIONS if name == "solana" else EVM_LIMITATIONS}
            try:
                if chain["enabled"]:
                    net = self.cfg["network"]
                    lease_ms = int((net["requests_per_cycle"] * (net["timeout_seconds"] + 60 / net["requests_per_minute"]) + 60) * 1000)
                    if not self.store.claim(scope, self.owner, self.clock(), self.clock() + lease_ms):
                        statuses[name] = {**self.store.status(scope), "observer_reason": "Another collector owns this stream"}
                        continue
                    try:
                        self._collect(name, chain, scope, status)
                    finally:
                        self.store.release(scope, self.owner)
            except Exception as exc:
                status.update(status="unavailable", reason=str(exc) if isinstance(exc, EvidenceUnavailable)
                              else "Provider evidence failed validation; checkpoint retained")
            status["checked_ms"] = self.clock()
            self.store.set_status(scope, status)
            statuses[name] = self.store.status(scope, self.cfg["poll_seconds"] * 3000)
        return {"mode": "read_only_evidence_pilot", "chains": statuses, "predictions_enabled": False}

    def _collect(self, name, chain, scope, status):
        url = self.environ.get(chain["rpc_url_env"], "")
        status["rpc_configured"] = bool(url)
        if not url:
            raise EvidenceUnavailable(f"Configure {chain['rpc_url_env']} locally; do not paste credentials in chat")
        if self.stop.is_set():
            raise EvidenceUnavailable("Collector stopped")
        if name not in self.clients:
            self.clients[name] = self.rpc_factory(url, self.cfg["network"], self.stop)
        rpc = self.clients[name]
        rpc.reset_budget()
        adapter = (SolanaCollector if name == "solana" else EVMCollector)(name, chain, rpc)
        head = adapter.head()
        status["finalized_head"] = head
        cursor = self.store.cursor(scope)
        if cursor is None:
            start = chain["start_height"] if chain["start_height"] is not None else head
            if start > head:
                raise EvidenceUnavailable("Configured start is after finalized head")
            cursor = self.store.initialize(scope, name, chain, start, self.clock())
        last = self.store.latest_block(scope)
        adapter.verify_last(last)
        if last and last["height"] > head:
            raise EvidenceUnavailable("Provider finalized head regressed; checkpoint retained")
        count = self.cfg["network"]["max_blocks_per_cycle"]
        if cursor <= head:
            heights = adapter.heights(cursor, head, count)
            if not heights and name == "solana":
                self.store.skip_empty_slots(scope, cursor, min(head, cursor + count - 1), self.clock())
            for height in heights:
                if self.stop.is_set():
                    raise EvidenceUnavailable("Collector stopped")
                block = adapter.block(height)
                block["diagnostics"]["skipped_slots_before"] = height - cursor if name == "solana" else 0
                self.store.commit_block(scope, cursor, block, self.clock(),
                                        max_storage_bytes=self.cfg["network"]["max_storage_mb"] * 1048576)
                cursor = self.store.cursor(scope)
                status["last_block_diagnostics"] = block["diagnostics"]
                status["last_event_time_ms"] = block["event_ms"]
        cursor = self.store.cursor(scope)
        status.update(status="limited", reason="Partial decoder and watched-wallet coverage; no trade signals",
                      next_height=cursor, backlog_heights=max(0, head - cursor + 1),
                      caught_up_to_observed_head=cursor > head)

    def status(self):
        return {"mode": "read_only_evidence_pilot", "predictions_enabled": False,
                "chains": {name: {"enabled": chain["enabled"], "scope": scope_id(name, chain),
                                  **self.store.status(scope_id(name, chain), self.cfg["poll_seconds"] * 3000)}
                           for name, chain in self.cfg["chains"].items()}}
