"""Public market GETs and optional authenticated data GETs. No trading APIs."""
import os
import re
import time
from typing import Protocol
from urllib.parse import urlencode, quote
from email.utils import parsedate_to_datetime

from app.exchange.coindcx_rest import UrllibTransport
from app.research.market_scanner import PublicMarketData, DataUnavailable, ScanStopped, number


class FlowFeed(Protocol):
    def whale(self, asset: dict, since_ms: int, until_ms: int) -> dict: ...


class HolderFeed(Protocol):
    def holders(self, asset: dict) -> dict: ...


class ResearchData(PublicMarketData):
    def __init__(self, cfg, stop, transport=None):
        net = cfg["network"]
        super().__init__(cancelled=stop.is_set,
                         transport=transport or UrllibTransport(timeout_seconds=net["timeout_seconds"]),
                         budget_seconds=net["cycle_budget_seconds"])
        self.cfg, self.stop = cfg, stop
        self.disabled_hosts = {}
        self.cooldowns = {}
        self.pace_seconds = 60 / net["requests_per_minute"]

    def _get(self, base, path, _headers=None, **params):
        if base not in {"https://fapi.binance.com", "https://api.coindcx.com",
                        "https://public.coindcx.com", "https://open-api-v4.coinglass.com",
                        "https://api.bubblemaps.io"}:
            raise ValueError("Not an approved research data host")
        if base in self.disabled_hosts:
            raise DataUnavailable(self.disabled_hosts[base])
        if self.cooldowns.get(base, 0) > time.time():
            raise DataUnavailable(f"{base}: provider backoff still active")
        net = self.cfg["network"]
        for attempt in range(int(net["retries"]) + 1):
            self.check()
            self.stop.wait(max(0, self.pace_seconds - (time.monotonic() - self.last_request)))
            self.check()
            self.last_request = time.monotonic()
            try:
                response = self.transport.request("GET", base + path + ("?" + urlencode(params) if params else ""),
                                                  {"Accept": "application/json", "User-Agent": "CoinDCXResearch/1.0 (public-market-data)", **(_headers or {})})
            except Exception:
                response = None
            if response is not None:
                code = response.status_code
                if code == 200:
                    try:
                        return response.json()
                    except (ValueError, TypeError):
                        raise DataUnavailable("Provider returned invalid JSON") from None
                if code in (401, 403, 418, 429):
                    # A denied key/plan or quota must not be retried once per symbol.
                    message = f"{base}: access denied (HTTP {code})" if code in (401, 403) else f"{base}: rate limited (HTTP {code})"
                    self.disabled_hosts[base] = message
                    if code in (418, 429):
                        value = next((str(v) for k,v in response.headers.items() if k.lower() == "retry-after"), "")
                        wait = net["rate_limit_wait_seconds"]
                        try:
                            wait = max(wait, float(value))
                        except ValueError:
                            try:
                                wait = max(wait, parsedate_to_datetime(value).timestamp()-time.time())
                            except (ValueError, TypeError):
                                pass
                        self.cooldowns[base] = time.time()+wait
                    raise DataUnavailable(message)
                if code < 500:
                    raise DataUnavailable(f"{base}: HTTP {code}")
            if attempt < net["retries"]:
                self.stop.wait(net["backoff_seconds"] * 2**attempt)
        raise DataUnavailable(f"{base}: connection or server error")

    def whale(self, asset, since_ms, until_ms):
        if not self.cfg["providers"]["coinglass_enabled"]:
            raise DataUnavailable("CoinGlass disabled; no whale-data subscription configured")
        key = os.environ.get("COINGLASS_API_KEY", "")
        if not key:
            raise DataUnavailable("COINGLASS_API_KEY is not configured")
        if not asset.get("coinglass_symbol") or not asset.get("coinglass_chain"):
            raise DataUnavailable("Exact CoinGlass asset and chain mapping required")
        payload = self._get("https://open-api-v4.coinglass.com", "/api/chain/v2/whale-transfer",
                            _headers={"CG-API-KEY": key}, symbol=asset["coinglass_symbol"],
                            start_time=since_ms, end_time=until_ms)
        if str(payload.get("code")) != "0":
            self.disabled_hosts["https://open-api-v4.coinglass.com"] = "CoinGlass plan, key or request denied"
            raise DataUnavailable("CoinGlass plan, key or request denied")
        if not isinstance(payload.get("data"), list):
            raise DataUnavailable("Invalid CoinGlass transfer response")
        floor = self.cfg["providers"]["coinglass_min_transfer_usd"]
        threshold = max(self.cfg["thresholds"]["whale_usd"], floor)
        labels = {x.casefold() for x in self.cfg["providers"]["coinglass_exchange_labels"]}
        events = []
        for row in payload["data"]:
            if (row.get("asset_symbol") != asset["coinglass_symbol"] or
                    str(row.get("blockchain_name", "")).casefold() != asset["coinglass_chain"].casefold()):
                continue
            amount = number(row["amount_usd"], positive=True)
            ts = int(number(row["block_timestamp"], positive=True) * 1000)
            if amount < threshold or not since_ms <= ts <= until_ms:
                continue
            sender, receiver = str(row.get("from", "")), str(row.get("to", ""))
            from_exchange, to_exchange = sender.casefold() in labels, receiver.casefold() in labels
            # Only exact provider exchange labels are trusted; ambiguous labels stay unknown.
            direction = ("internal" if from_exchange and to_exchange else "inflow" if to_exchange
                         else "outflow" if from_exchange else "unclassified")
            events.append({"id": f"{asset['coinglass_chain']}:{row['transaction_hash']}",
                           "timestamp_ms": ts, "usd": amount, "direction": direction,
                           "from_label": sender, "to_label": receiver})
        return {"events": events, "source": "CoinGlass", "from_ms": since_ms, "to_ms": until_ms,
                "minimum_usd": threshold, "coverage": "provider_reported",
                "limitation": "Only provider-returned transfers; no proof of complete chain coverage. Unknown labels are not directional.",
                "raw": payload}

    def holders(self, asset):
        if not self.cfg["providers"]["bubblemaps_enabled"]:
            raise DataUnavailable("Bubblemaps disabled; no holder-data subscription configured")
        key = os.environ.get("BUBBLEMAPS_API_KEY", "")
        if not key:
            raise DataUnavailable("BUBBLEMAPS_API_KEY is not configured")
        if not all(asset.get(k) for k in ("holder_chain", "holder_address", "holder_share_unit")):
            raise DataUnavailable("Exact chain, contract and holder-share units required")
        chain, address = str(asset["holder_chain"]), str(asset["holder_address"])
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", chain) or not re.fullmatch(r"[a-zA-Z0-9]+", address):
            raise DataUnavailable("Invalid holder asset identity")
        payload = self._get("https://api.bubblemaps.io", f"/v0/tokens/map/{quote(chain)}/{quote(address)}",
                            _headers={"X-ApiKey": key}, return_nodes="true", return_clusters="true")
        if not isinstance(payload.get("clusters"), list) or not payload.get("nodes", {}).get("top_holders"):
            raise DataUnavailable("Holder map lacks clusters or holder coverage")
        scale = 100 if asset["holder_share_unit"] == "fraction" else 1
        nodes = payload["nodes"]["top_holders"]
        by_address = {n["address"]: n for n in nodes}
        clusters, covered = [], set()
        for cluster in payload["clusters"]:
            addresses = cluster["holders"]
            if covered.intersection(addresses) or any(a not in by_address for a in addresses):
                raise DataUnavailable("Overlapping clusters or incomplete holder identities")
            covered.update(addresses)
            clusters.append(self._cluster(addresses, by_address, scale))
        for addr in by_address.keys() - covered:
            clusters.append(self._cluster([addr], by_address, scale))
        return {"source": "Bubblemaps", "timestamp_ms": int(payload["metadata"]["ts_update"]) * 1000,
                "clusters": clusters, "coverage": "top holders only", "raw": payload}

    @staticmethod
    def _cluster(addresses, nodes, scale):
        selected = [nodes[a] for a in addresses]
        shares = [number(n["holder_data"]["share"]) * scale for n in selected]
        if any(not 0 <= x <= 100 for x in shares) or sum(shares) > 100.0001:
            raise DataUnavailable("Invalid holder shares; verify provider units")
        return {"members": sorted(addresses), "share_pct": sum(shares),
                "entity_ids": sorted({str(n["address_details"]["entity_id"]) for n in selected
                                      if n["address_details"].get("entity_id")}),
                "custodial_share_pct": sum(s for s, n in zip(shares, selected)
                                           if n["address_details"].get("is_cex") or n["address_details"].get("is_dex"))}
