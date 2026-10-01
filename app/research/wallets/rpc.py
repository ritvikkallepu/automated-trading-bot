"""Bounded, read-only JSON-RPC. Provider URLs and errors never enter public status."""
import json
import time
from urllib.parse import urlsplit

from app.exchange.coindcx_rest import UrllibTransport

METHODS = frozenset({"eth_chainId", "eth_getBlockByNumber", "eth_getTransactionReceipt", "eth_call",
                     "eth_getLogs", "getGenesisHash", "getSlot", "getBlocks", "getBlock", "getFirstAvailableBlock"})


class EvidenceUnavailable(Exception):
    """Safe-to-display collector failure."""


class ReadOnlyRPC:
    def __init__(self, url, network, stop, transport=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise EvidenceUnavailable("RPC endpoint must be HTTPS without user-info or fragment")
        self.url, self.network, self.stop = url, network, stop
        self.transport = transport or UrllibTransport(timeout_seconds=network["timeout_seconds"])
        self.request_id = 0
        self.remaining = network["requests_per_cycle"]
        self.last_request = 0.0
        self.cooldown_until = 0.0

    def reset_budget(self):
        self.remaining = self.network["requests_per_cycle"]

    def call(self, method, params):
        if method not in METHODS:
            raise EvidenceUnavailable("RPC method is outside the read-only allowlist")
        if self.remaining <= 0:
            raise EvidenceUnavailable("Request budget reached; checkpoint retained")
        if time.monotonic() < self.cooldown_until:
            raise EvidenceUnavailable("Provider cooldown active")
        delay = max(0, 60 / self.network["requests_per_minute"] - (time.monotonic() - self.last_request))
        if self.stop.wait(delay):
            raise EvidenceUnavailable("Collector stopped")
        self.remaining -= 1
        self.request_id += 1
        self.last_request = time.monotonic()
        body = json.dumps({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}).encode()
        try:
            response = self.transport.request("POST", self.url,
                                              {"Content-Type": "application/json", "Accept": "application/json"}, body)
        except Exception:
            raise EvidenceUnavailable("RPC connection failed; checkpoint retained") from None
        if response.status_code != 200:
            if response.status_code == 429:
                wait = 60
                for key, value in response.headers.items():
                    if key.lower() == "retry-after" and str(value).isdigit():
                        wait = max(wait, min(int(value), 86400))
                self.cooldown_until = time.monotonic() + wait
            raise EvidenceUnavailable(f"RPC HTTP {response.status_code}; checkpoint retained")
        try:
            value = response.json()
            if not isinstance(value, dict) or value.get("id") != self.request_id or value.get("jsonrpc") != "2.0":
                raise ValueError
            if "error" in value or "result" not in value:
                raise ValueError
            return value["result"]
        except (ValueError, KeyError, TypeError):
            raise EvidenceUnavailable("RPC payload or method unavailable; checkpoint retained") from None
