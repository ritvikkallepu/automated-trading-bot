# Wallet Evidence Pilot

## Implemented scope

This is the first implementation step of [the wallet intelligence framework](WALLET_FLOW_FRAMEWORK.md). It is a read-only evidence collector, not a whale prediction model or a change to trading rules.

- Ethereum and BNB Smart Chain: finalized top-level native transfers and explicitly mapped ERC-20 transfers, with receipt status and token-decimal checks.
- Solana: finalized parsed System and standard SPL Token transfers, including inner instructions and token-account owner resolution.
- Durable, compressed raw evidence and precise integer/decimal transfer quantities in a separate SQLite database.
- Separate chain/coverage checkpoints, duplicate suppression, source failure reporting, bounded requests, approximate storage budget, and per-stream collector leases.
- Transactional writes: failed block validation never advances the checkpoint or partially commits events.
- Immutable observation timestamps: a backfilled event is not visible to a query made before it was observed.
- Dashboard: Research > Wallet Evidence shows readiness, counts, lag and limitations for each network.

Finalized-chain conflicts stop collection for investigation. This pilot does not automatically reverse finalized history or ingest provisional transactions. It does not yet decode purchases, bridges, LP changes, staking, Token-2022 transfers, or internal EVM native transfers. USD values, smart-money performance, institutional identity discovery, and multi-scale predictive models remain unavailable. Explicitly supplied entity labels are assertions with evidence references, not automatically verified identity findings.

## Current activation state

All three collectors are disabled by default. No wallet addresses, provider credentials, or presumed institution identities have been seeded. The existing Binance research scanner and live/paper trading behavior are unchanged. Viewing the wallet dashboard does not enable a collector, create a wallet database, or contact an RPC provider.

Configure `config/wallet_research.json` only after selecting a bounded watchlist and confirming data-provider access. The JSON file contains environment variable names, never API keys or credential-bearing RPC URLs.

| Network | Local environment variable | Network identity check |
|---|---|---|
| Ethereum | `WALLET_ETH_RPC_URL` | `eth_chainId` equals 1 |
| BNB Smart Chain | `WALLET_BSC_RPC_URL` | `eth_chainId` equals 56 |
| Solana | `WALLET_SOL_RPC_URL` | RPC genesis equals the independently verified `expected_genesis_hash` |

Use read-only HTTPS RPC access. No wallet private key, exchange order permission, or signing capability is required. Do not paste API keys into chat, commit URLs containing keys, or add them to command-line arguments. Configure credentials in the collector process's local environment or existing secret-management setup. The module does not automatically load a `.env` file.

Obtain the Solana mainnet genesis hash from a trusted independent source, such as the official mainnet RPC, before enabling that chain. Do not blindly trust the same unverified provider for both the observed and expected identity.

## Watchlists and coverage

Each watched wallet has `address`, `entity_id`, `attribution`, and `evidence`. Use `entity_id: null`, `attribution: "unknown"`, and `evidence: null` until there is supported attribution. `probable` and `verified` labels require an entity ID and a source reference. Only verified same-entity transfers are tagged internal; a probable association does not merge independent flows.

Each asset has exact contract/mint `address`, display `symbol`, and `decimals`; `native` identifies the chain-native asset. Symbols are never used for identity matching. EVM token decimals are checked against the contract at the captured block. Solana decimals are cross-checked against transaction token balances. Unavailable validation stops progress rather than guessing quantities.

An empty watchlist cannot be enabled. The pilot caps configuration at 100 wallets and 100 assets per chain, but these are validation ceilings, not promised free-tier capacity. Start substantially smaller and measure usage, block lag, and storage growth.

`start_height: null` starts at the current finalized block/slot on first activation. It does not backfill earlier wallet history. An explicit earlier start requires archive coverage and provider budget. Restarts use persisted checkpoints. Changing wallet, asset, attribution or start-height coverage creates a new stream so the old history is not retrospectively relabeled; prior streams remain stored. To retain continuity, do not change coverage casually.

## Running the pilot

The standard Research scanner controls do not start the wallet collector. After configuration, run the separate read-only worker using the same Python environment as the bot:

```bash
python -m app.research.wallets check
python -m app.research.wallets collect --once --database /path/to/research-data/wallets.sqlite3
python -m app.research.wallets collect --database /path/to/research-data/wallets.sqlite3
python -m app.research.wallets status --database /path/to/research-data/wallets.sqlite3
```

The database must be named `wallets.sqlite3` beside the dashboard's continuous research database for the dashboard to read it. For the existing local research deployment that directory is `C:\TradingBots\coindcx-research-data\research`. Use the appropriate research data directory on the VPS; do not point a second machine at a live SQLite file over a network share.

`--config` selects a configuration file for the command-line collector. The dashboard currently reads the bundled default `config/wallet_research.json`; keep both on the same configuration when using the dashboard. Collector changes take effect after restarting this separate worker. No trading-loop restart is required by the wallet module itself.

The first one-shot run should be checked against known transactions before leaving the worker running. The supplied default configuration's `check` command makes zero network requests, and `collect --once` makes zero network requests while all chains are disabled.

## Operational limits

The default request rate and per-cycle request budget apply per enabled chain. The provider account's combined limits may be stricter. Quota responses enter a cooldown; failed requests retain the checkpoint. The default approximate 512 MiB storage budget stops new evidence writes rather than deleting recorded history. Monitor actual disk headroom too, since SQLite page and journal overhead vary.

The adapters are sequential bounded block-polling collectors for evidence validation. On a busy network, especially Solana, a small free-tier budget can fall behind. `backlog_heights` and `caught_up_to_observed_head` expose this. Being caught up means caught up to the head sampled at the beginning of that cycle, not zero live latency or complete decoding coverage. Status remains `limited` even when caught up.

Do not increase request limits blindly to chase the head. A measured production upgrade would use filtered provider streams plus checkpointed backfill and independent reconciliation. Finalized-only evidence also has confirmation delay; it is not the provisional micro-frequency alert layer described in the broader framework.

## Verification and next milestone

Offline tests cover configuration/identity validation, exact amounts, failed transactions, duplicates, multiple events per transaction, chain isolation, checkpoints, finalized-history conflicts, rate limits, secret-safe errors, token owners, storage budgets and point-in-time visibility. Browser tests cover the new dashboard view on desktop and mobile.

Live provider end-to-end accuracy is unverified until actual RPC access, watched wallets, and known reference transactions are supplied. No historical fixture or test result should be represented as collected mainnet evidence.

Next milestone: reconcile a small live sample against raw receipts/instructions, measure costs and lag, then add economic-action decoding and continuously decaying multi-scale features. Only after prospective validation should those features influence Weighted Hybrid entries.

Protocol references: [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/), [BSC finality RPC](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/), [Solana getBlock](https://solana.com/docs/rpc/http/getblock), [Solana transaction structures](https://solana.com/docs/rpc/json-structures).
