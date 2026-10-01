# Cross-Chain Wallet Intelligence Framework

Status: target architecture; the separate [finalized evidence pilot](WALLET_EVIDENCE_PILOT.md) implements the initial collection foundation. Predictive modeling and trading integration are not implemented or enabled.
Prepared: 2026-09-29.

## Objective and scope

Track economically meaningful whale, smart-money, and attributable institutional activity across Ethereum mainnet, BNB Smart Chain, and Solana mainnet. Scope includes native assets and selected tokens on these networks, with explicit contract/mint identity. Ethereum L2s and other BNB ecosystem networks require separate adapters and coverage declarations.

Maintain concurrent short-, medium-, and long-memory states and update them whenever new evidence arrives. The objective is to detect changes in buying/selling pressure and inventory, explain conflicting evidence, and measure whether these observations improve the existing Weighted Hybrid 5m strategy / 1m execution workflow.

Simultaneous multi-timescale analysis can reduce specific false positives; it cannot eliminate false signals, expose every institution's position, or guarantee advance identification of large price moves. Off-chain exchange trades, hedges, OTC transactions, and beneficial ownership can remain unobservable.

## Current bot: what exists and what is missing

The inspected research configuration uses 5m market candles and a 300-second market schedule. Its optional whale schedule is hourly, and holder snapshots are scheduled every six hours. The configured asset mapping is empty and the CoinGlass/Bubblemaps adapters are disabled. These are observations of the local configuration, not a claim about any separately deployed VPS configuration.

Relevant existing files:

- `config/research.json`: schedules, asset mappings, provider switches, thresholds.
- `app/research/continuous/feeds.py`: optional CoinGlass transfers and Bubblemaps holder-map adapters.
- `app/research/continuous/analysis.py`: whale, holder concentration, and derivatives analysis.

The whale analyzer summarizes 1h, 4h, and 24h windows, but derives its directional score from the 24h exchange-flow imbalance. It lacks continuous wallet inventory histories and independently verified entity attribution. Its current transfer identity uses chain plus transaction hash; multiple transfer events in one transaction need finer identities. Transfers between any two exchange-labeled endpoints are categorized as internal, which does not distinguish same-exchange reshuffling from movement between separate exchanges.

These are prerequisites for stronger wallet analysis. Increasing scan frequency alone will not resolve them. Existing ranking-outcome checks also do not substitute for replaying actual Weighted Hybrid entries, stops, and exits.

## Architecture

```text
Ethereum / BSC / Solana collectors       Asset and entity registries
                    |                              |
                    v                              v
        Durable raw events -> normalized ledger -> economic actions
                                                   |
                         inventory + continuous multi-scale features
                                                   |
 Binance market context -> evidence fusion <- CoinDCX execution context
                                                   |
                      immutable research signals and explanations
                                                   |
                     dashboard + alerts + shadow/paper evaluation
                                                   |
                optional strategy integration after validation approval
```

On-chain collection runs independently of the 5m market scanner. Its output is continuously available to the scanner and dashboard. A slow holder-data request must not stall fresh market observations. Research remains read-only with respect to trading accounts during the initial rollout.

## 1. Collect chain evidence correctly

| Network | Required observations | Special handling |
|---|---|---|
| Ethereum | Transactions, receipts, token logs, balances, supported native-transfer traces | Internal native movements need trace/indexer coverage; contract events alone are insufficient |
| BNB Smart Chain | EVM transactions, receipts, token logs, balances, supported traces | Separate chain identity and finality tracking; never merge assets solely by ticker |
| Solana | Transactions, outer/inner instructions, token-account ownership, pre/post balances | Resolve token accounts to owners; reconcile fees, rent, wrapping, and supported token-program behavior |

For every adapter, declare supported assets, protocols, event types, history depth, price coverage, and observed lag. Unsupported decoding produces `unknown`, not an inferred buy or sell.

Use chain-native finality information. Ethereum exposes safe/finalized block tags; BSC documents finality-aware RPC methods; Solana distinguishes processed, confirmed, and finalized commitment. Their semantics must be preserved by each adapter rather than flattened to the same confirmation count. Sources: [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/), [BSC RPC](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/bsc-api-list/), [Solana RPC](https://solana.com/docs/rpc).

Keep provisional and finalized accounting separately. Provisional observations can generate clearly labeled early research alerts; revisions/reorganizations retract their contributions and update dependent signals. Failed transactions contribute no executed trade flow, although fees can remain relevant to wallet accounting. Pending or unconfirmed activity must never be labeled an executed institutional purchase.

### Canonical ledger contract

Store, at minimum:

- Network ID, block/slot and block identity, transaction hash/signature, event path, and finality/status.
- Stable event identity: EVM transaction plus log index or trace path; Solana signature plus instruction/inner-instruction path and normalized event ordinal. Preserve block identity for reorganization bookkeeping.
- Chain event time, first received time, decoded/available time, and subsequent revision times.
- Asset contract/mint, decimals, raw integer amount, normalized quantity, and native/wrapped relationship.
- USD value with price source, price timestamp, freshness, and valuation uncertainty. Missing or unreliable prices remain unknown.
- Source/destination address, attributed entities, attribution evidence/version, economic action, decoder version, and raw payload reference.
- Link to an economic-action group when multiple transfer legs belong to one swap, bridge movement, or other protocol action.

Use integer/decimal quantities rather than floating-point ledger balances. Ingest duplicate deliveries idempotently, including delivery from multiple providers. Finality upgrades change event state; they must not add the amount again. Helius explicitly documents that webhook retries can produce duplicate events. [Helius delivery documentation](https://www.helius.dev/docs/webhooks)

Persist per-chain checkpoints, detect gaps, backfill within quotas, and distinguish delayed evidence from live evidence. Historical replay must use the time evidence actually became available to the system, including decoding and identity labels. A late backfill cannot become a prediction that supposedly existed earlier.

## 2. Interpret the economic action before assigning direction

| Observed action | Permitted interpretation |
|---|---|
| Verified swap with net token acquisition | Observed purchase of that asset, subject to decoder confidence |
| Verified swap with net token disposal | Observed sale; account for route legs and costs |
| Deposit to an attributed exchange | Potential inventory available for sale; not proof of a sale |
| Withdrawal from an exchange | Custody/inventory movement; not proof of a fresh purchase |
| Transfer between wallets of one verified entity | Internal movement; no new independent buyer |
| Transfer between different exchanges | Inter-exchange movement; separate from same-entity reshuffling |
| Bridge send and receive | Cross-chain movement; link both sides when protocol evidence supports it |
| LP changes, staking, borrowing, collateral movements | Their own economic categories; do not automatically score as spot demand |
| Mint/burn, wrap/unwrap, treasury distributions | Supply or representation changes; investigate purpose separately |
| Unknown, ambiguous, unsupported transaction | Preserve as evidence; abstain from directional classification |

Group multi-hop routes into their net economic outcome. For bridges, prefer protocol message identifiers and verified asset mappings. Matching amounts and nearby timestamps alone do not establish a cross-chain link. Unlinked observations remain explicitly unresolved.

Reconcile observed actions with inventory changes. A token balance increase may be a purchase, transfer, reward, airdrop, or rebasing effect. Exclude obvious self-churn and flag suspected wash/MEV patterns without claiming intent or ownership that the data cannot establish.

## 3. Separate whales, smart money, and institutions

### Whales: economic significance

Record absolute transfer size, holdings, share of circulating supply where reliable, size relative to executable liquidity, and deviation from that wallet's normal activity. These are separate features. A fixed USD threshold can be a collection-budget filter, but must not be the sole definition of market impact.

### Smart money: demonstrated prior trading quality

Build a point-in-time wallet track record from reconstructable trades and cost basis. Evaluate net realized performance, marked open inventory, fees/gas/slippage, drawdown, consistency, and excess performance relative to the asset/market. Distinguish scalpers, swing traders, long-term holders, arbitrageurs, and market makers.

Require adequate independent observations and shrink sparse estimates toward an uninformative baseline. Unknown acquisition cost remains unknown; airdropped tokens are not evidence of skilled entries. One large winning trade or a high raw win rate is insufficient. Track wallets that disappear or lose money to avoid survivor-only rankings.

### Institutions: evidence-backed attribution

Maintain verified, probable, and unknown attribution states with provenance, confidence, effective dates, and first-known dates. Separate an institution's own treasury from custody, exchange client funds, and market-making operations whenever evidence permits. A large wallet is not automatically an institution, and an attributed institution's on-chain movement does not reveal its net hedged exposure.

### Holder graph and independence

Use typed edges: transfer, verified common control, shared funder, protocol interaction, and bridge linkage. Shared funding or a direct transfer is evidence of a relationship, not proof of common ownership. A shared exchange endpoint is not a reason to merge all its customers.

Count independently supported entities, not raw wallet addresses. Keep uncertain clusters visible as uncertainty. Bubblemaps can supply holder, transfer, and cluster information through its Data API; embedding its visual map is a separate product. Confirm access, chain/asset coverage, and snapshot age before treating its data as an input. [Bubblemaps product documentation](https://docs.bubblemaps.io/introduction)

## 4. Maintain concurrent, continuously decaying memories

The engine should update all relevant temporal states on each event. Candle boundaries remain useful for market context and trade execution, but do not reset wallet memory.

For each feature family, maintain a bank of exponentially decaying states:

```text
state_j(t) = state_j(previous) * 2 ** (-(t - previous) / half_life_j)
             + new_event_contribution
```

An initial experimental bank could span 30 seconds, 2 minutes, 10 minutes, 1 hour, 6 hours, 1 day, 7 days, and 30 days. These are overlapping memory scales, not isolated analysis windows or promised optimal settings. Each event contributes to every supported scale; quiet periods decay correctly. Revisions require compensating contributions or checkpoint replay.

Normalize directional flow by corresponding gross flow and liquidity; also retain absolute activity and coverage. Normalize count/volume rates for each decay scale before comparing them. Maintain transaction-count and traded-volume clocks alongside wall time to distinguish a genuinely active burst from a thin-market outlier. Fit robust baselines only from past data, including time-of-day patterns where supported.

| Concurrent view | Main features | Question |
|---|---|---|
| Fast pressure | Net executed purchases/sales, independent entity arrival rate, flow acceleration, liquidity absorption | Is meaningful new pressure appearing now? |
| Structural behavior | Persistent inventory changes, repeated accumulation/distribution, participation breadth, price/flow divergence | Is this pressure sustained or being reversed? |
| Macro positioning | Cohort holdings, reserve movements, concentration changes, stablecoin deployment, attributable capital rotation | What longer accumulation/distribution context surrounds it? |

These views are interpretations of the shared state, not three separate voting bots. The same transaction appearing in short and long memories remains one piece of evidence. Model correlations explicitly and cap a single entity's influence so related features cannot manufacture agreement.

Detect changes in flow rates and regime, with persistence/hysteresis to reduce flip-flopping. A slow-memory accumulator can temporarily sell; retain the disagreement and its magnitude. Do not force every horizon into a single bullish/bearish label. Missing weeks of history must display as insufficient macro evidence.

Keep the memory bank stable within a model version. Adapt feature emphasis to measured volatility, liquidity, activity, and trend regime. Learn those relationships using chronological validation; do not let a language model rewrite thresholds during live trading.

## 5. Fuse evidence with market and execution context

Start with auditable features and a simple regularized model or transparent rule baseline. Add complexity only when it improves held-out performance. Estimate outcomes separately for relevant holding horizons, conditioned on the shared multi-scale context.

Inputs include observed purchases versus transfers, prior wallet quality, independent entity breadth, inventory persistence, holder risks, source completeness, latency, Binance price/volume/depth and derivatives context, and the actual CoinDCX instrument's spread/liquidity/basis. Preserve contract/mint-to-market mappings; matching ticker strings is insufficient.

Maintain separate outputs for evidence quality, directional strength, and calibrated outcome probability. A heuristic score of 80 is not an 80% chance of profit. Probabilities need a defined target and demonstrated calibration on unseen observations.

An hourly flow provider cannot support a credible subminute signal. Measure chain-to-receipt and receipt-to-signal latency, decoder delay, queue age, finality delay, and market-quote age. If observed latency consumes the useful horizon, mark that horizon unavailable. Fast research alerts may be provisional, but need revision handling and must not masquerade as finalized evidence.

The optional language-model layer explains evidence IDs, summarizes conflicts, and proposes hypotheses for review. Ledger accounting, feature calculations, identity confidence, and numerical outcome measurements remain reproducible. No invented labels, missing-history reconstruction, or unsupported claims about a wallet's motives.

## 6. Publish an inspectable signal, including uncertainty

Every signal snapshot should include:

- Asset identity, chain, market mapping, model/config version, creation time, and data available-through time.
- Fast, structural, and macro observations with their contributing event IDs and independent entity counts.
- Distinction between verified swaps, custody flows, uncertain movement, and holder-graph risk.
- Coverage, latency, finality, valuation, and attribution quality; an explicit reason when unavailable.
- Directional hypothesis, intended evaluation horizon, counterevidence, invalidation conditions, and expiry/reassessment policy.
- Current status such as observing, evidence aligned, conflicting, stale, invalidated, or insufficient history.
- Frozen original call and subsequent revisions, followed by measured outcomes when mature.

Illustrative interpretation: several independent, previously successful entities acquire an asset while structural inventory grows, but macro holder concentration is high. This supports an accumulation candidate with concentration risk. It does not prove an imminent rally or authorize entry. Conversely, a large exchange reshuffle should not become a sell alert simply because its USD size is high.

The dashboard should show observation time, evaluation horizon, source health, and evidence links next to the conclusion. Sorting uses complete timestamps and stable tie-breaking. Separate newly observed signals from historical/backfilled observations; neither should silently replace the other.

## 7. Fit the existing 5m / 1m trading workflow

Research publishes wallet state continuously. Weighted Hybrid still evaluates its configured closed 5m strategy candles; its configured 1m execution/position-management behavior remains separate. Reading faster wallet events does not silently change entry frequency.

Initially run wallet research in shadow mode. Record whether it would support, oppose, or abstain on each real strategy decision without altering orders. Then compare paper runs with and without the feature on identical market data and execution assumptions.

Each instrument needs its own state and cooldowns. Portfolio-wide constraints remain explicitly separate. A flow observation or winning trade in one pair must not accidentally block another pair's same-direction signal.

Any eventual trading influence is a separate approved change with declared stale-feed behavior and rollback. Source failures should not disable existing position-protection mechanisms. Do not alter live leverage, stops, confidence thresholds, or portfolio limits as part of this framework.

## 8. Prove incremental value and prevent hindsight

Freeze hypotheses before observing outcomes. Replay only transactions, labels, prices, wallet scores, and decoded evidence available at each decision time. Data reconstructed after the fact can support retrospective research, but cannot establish that the system actually predicted a past move.

Use chronological walk-forward evaluation with appropriate purging/embargo for overlapping targets. Preserve failed alerts, delisted assets, sparse wallets, and periods of source failure. Account for repeated observations of the same entity or market episode when calculating uncertainty.

Compare price/volume-only, wallet-only, and combined systems. Report by chain, liquidity group, market regime, and holding horizon:

- Precision, false-alert rate, recall of predefined qualifying moves, and lead time from actual signal availability.
- Probability calibration where probabilities are supplied, with sample size and uncertainty.
- Favorable/adverse excursion, tradable return after costs, drawdown, and turnover.
- Exact Weighted Hybrid 5m/1m trade results with fees, slippage, funding where applicable, entry timing, stops, and exits.
- Coverage, missing-event rate estimates, duplicate/retraction counts, and latency percentiles.

Choose promotion criteria before examining the test period. Require sufficient independent observations and improved net outcomes without unacceptable drawdown or operational degradation. A few correctly identified rallies or an improved four-hour ranking score are insufficient.

Minimum correctness tests: duplicate delivery; multiple transfers in one transaction; reorganization and re-inclusion; finality upgrade; failed transaction; late decoding/backfill; future-leaking label; bridge double-count; multi-hop swap; self-transfer; inter-exchange transfer; invalid token decimals; symbol collision; stale/missing USD price; Solana token-account ownership; source outage; restart replay; cross-pair state isolation; and contradictory multi-scale evidence.

## 9. Free-tier / trial pilot and rollout

Public blockchain transactions do not come with complete, accurate institution labels or validated smart-money histories. Free websites also do not imply unrestricted API access. Full-universe, low-latency indexing and deep history across all three networks cannot be assumed within a free-only budget.

Candidate infrastructure: an EVM RPC provider such as Alchemy for Ethereum/BSC and a Solana provider such as Helius. Both publish free tiers; verify the specific RPC methods, stream access, archive depth, regional availability, and consumption limits before selecting them. Helius documents parsed event delivery options; Solana warns that shared public RPC endpoints are not intended for production applications. Sources: [Alchemy plans](https://www.alchemy.com/pricing), [Helius plans](https://www.helius.dev/pricing), [Helius event delivery](https://www.helius.dev/docs/webhooks), [Solana RPC guidance](https://solana.com/docs/rpc).

Keep CoinGlass as an optional supplementary flow/derivatives feed and Bubblemaps as optional holder-graph evidence. Neither should be treated as a complete authoritative wallet tape without demonstrated coverage. Missing paid/trial features must be visible; do not manufacture substitute readings.

Begin with a bounded, explicitly documented universe, for example 10-20 assets and 20-50 curated entities per chain if measured quotas support it. Validate asset mappings and entity provenance first. These are pilot targets, not provider capacity promises. Keep an exploration allocation when expanding discovery so only already-popular assets do not monopolize coverage. Log admission, exclusion, and coverage decisions.

| Phase | Deliverable | Completion evidence |
|---|---|---|
| 1. Reliable evidence | Asset/entity registries, three chain adapters, durable ledger, replay and source-health reporting | Sample transactions reconcile; duplicates and reorganizations pass tests; cost/lag measured |
| 2. Multi-scale interpretation | Economic-action decoding, inventories, concurrent decays, uncertainty and conflict outputs | Deterministic replay; transfers distinguished from trades; no cross-pair contamination |
| 3. Shadow dashboard | Evidence-linked snapshots, source age, history, alerts with corrections, frozen predictions | Calls are traceable to information available then; stale feeds produce visible abstention |
| 4. Evaluation | Wallet versus market baselines and exact Weighted Hybrid paper comparison | Predeclared validation criteria met on unseen observations |
| 5. Optional execution integration | Versioned, reversible strategy integration | Separate approval, documented risk limits and failure behavior |

Start with a durable local database and bounded raw-event storage consistent with the current app. A SQLite pilot is reasonable with a controlled writer; move to PostgreSQL when concurrent collectors, throughput, or deployment needs justify it. Persist checkpoints, model versions, and replay inputs. Use incremental feature updates rather than rescanning the full ledger every five minutes.

Collectors require read-only data credentials, never wallet private keys. Keep provider secrets out of logs and repositories. Authenticate incoming webhooks where supported, restrict exposed endpoints, apply rate limits, redact sensitive configuration, and set usage caps without automatic paid upgrades.

The immediate engineering milestone is Phase 1: trustworthy events and attribution with transparent coverage. It provides the foundation for meaningful multi-cycle analysis and lets subsequent model improvements be measured rather than assumed.
