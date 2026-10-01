# Research Bot: Complete Guide

Implementation reviewed: **30 September 2026**. This guide describes the code and local configuration, not a promise of trading performance. The separate VPS has not been inspected for this document.

## Read This First

**The continuous Research watchlist is a long-only, conditional research planner. It is not currently an enter-long/enter-short trading signal engine.**

- Its public score is a formula, not an AI probability or the trading bot's approval confidence.
- The default plan waits for a future pullback to EMA21. Its watchlist label remains **Waiting for pullback**, even when a separately tracked historical plan has filled in simulation.
- **Enter Long** and **Enter Short** are not assessment labels implemented in this view.
- Short research exists in the separate **Manual Scan**, not in the continuous planner.
- The wallet collector is an evidence pilot. It does not yet supply whale predictions, smart-money scores, or institutional-flow signals to this ranking.
- Nothing in this research module places an order, changes an actual position, or approves a Weighted Hybrid trade.

The absence of entry labels is therefore partly a missing capability/UI distinction, not evidence that the scanner is carefully waiting for a perfect trade. The saved data also shows a genuine shortage of qualifying long ideas. See [the dated assessment audit](RESEARCH_ASSESSMENT_AUDIT_2026-09-30.md).

## Contents

1. [System Components](#system-components)
2. [Current Local Setup](#current-local-setup)
3. [Startup and Operation](#startup-and-operation)
4. [Reading the Dashboard](#reading-the-dashboard)
5. [Universe and Scan Selection](#universe-and-scan-selection)
6. [Market Data and Timeframes](#market-data-and-timeframes)
7. [Scores and Assessments](#scores-and-assessments)
8. [Conditional Entry and Exit Rules](#conditional-entry-and-exit-rules)
9. [Setup History and Validation](#setup-history-and-validation)
10. [Manual Long and Short Research](#manual-long-and-short-research)
11. [Whale, Holder, and Wallet Evidence](#whale-holder-and-wallet-evidence)
12. [Configuration Reference](#configuration-reference)
13. [Alerts](#alerts)
14. [Storage, Backups, and Logs](#storage-backups-and-logs)
15. [Laptop and VPS Deployment](#laptop-and-vps-deployment)
16. [Troubleshooting](#troubleshooting)
17. [Implementation Map and API](#implementation-map-and-api)
18. [Verification and Limitations](#verification-and-limitations)
19. [What Must Be Built Next](#what-must-be-built-next)
20. [Glossary](#glossary)

## System Components

| Component | What it actually does | What it does not do |
|---|---|---|
| Continuous Research / Current Watchlist | Periodically ranks public Binance data and constructs conditional long plans | No short plan generator; no immediate entry approvals |
| Setup History | Preserves original plans and checks their subsequent price path | Not actual orders, positions, or exchange fills |
| Research Validation | Compares frozen ranking ideas with subsequent four-hour prices and controls | Not the Weighted Hybrid strategy's win rate |
| Manual Scan | On-demand long/short ranking across 5m, 15m, 1h, and 4h, with separate CoinDCX venue checks | Not the automatic continuous scanner |
| Manual shadow outcomes | Checks subsequent 15-minute, one-hour, and four-hour directional returns | No conditional stop/target strategy simulation |
| Wallet Evidence | Records selected finalized transfers on Ethereum, BNB Smart Chain, and Solana | No automatic institution discovery, smart-money PnL, or predictive fusion |
| Trading bot | Separate live/paper strategies, execution, risk management, and portfolio controls | Does not automatically trade research rankings |

The current implementation uses deterministic Python analysis and browser JavaScript. No ChatGPT/OpenAI key is required because no LLM is called. No Hugging Face model, news sentiment model, or learned coin-selection model is active in these research paths.

```text
Public Binance market data + public CoinDCX catalogue
                       |
           liquidity and rotation selection
                       |
        closed-candle features + aligned OI
                       |
   public ranking + optional whale/holder confluence
                       |
          conditional LONG plan and assessment
                       |
          immutable publication / frozen setup
                       |
       dashboard + forward evaluation + opt-in alerts

Separate path:
ETH / BSC / SOL RPC -> finalized wallet evidence -> wallet database
                                      |
                             Wallet Evidence status

Neither path automatically sends orders to CoinDCX.
```

## Current Local Setup

These are machine-specific paths observed during this review, not portable defaults:

| Item | Local value |
|---|---|
| Running source checkout | `C:\TradingBots\coindcx-trading-bot` |
| Research dashboard | `http://127.0.0.1:63332/#research` |
| Runtime data root | `C:\TradingBots\coindcx-research-data` |
| Continuous history | `C:\TradingBots\coindcx-research-data\research\research.sqlite3` |
| Wallet database expected by dashboard | `C:\TradingBots\coindcx-research-data\research\wallets.sqlite3` |
| Supervisor log | `C:\TradingBots\coindcx-research-data\logs\research.log` |
| Windows task name | `CoinDCX Research Collector` |
| Research configuration | `config/research.json` in the running checkout, unless overridden |
| Wallet configuration | `config/wallet_research.json` |

At the audit cutoff, continuous research used **5m candles every 300 seconds**, `pullback` entry plans, and a public candidate threshold of **65/100**. CoinGlass, Bubblemaps, Telegram, Discord, and all three direct wallet collectors were disabled. No wallet addresses were configured.

This is not a statement that all external services are broken. They have not been activated. Editing another checkout's file will not change the running process's configuration.

## Startup and Operation

### Fresh Installation

Python **3.11 or newer** is required. Use an editable installation from a full repository checkout because the research configuration lives in the repository's `config` directory. The core public research path has no third-party Python runtime dependencies declared in `pyproject.toml`. The trading bot's optional WebSocket packages are not required for this REST research worker.

Windows PowerShell, for a fresh environment:

```powershell
cd C:\TradingBots\coindcx-trading-bot
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m app.research.continuous.runtime --data-dir C:\TradingBots\coindcx-research-data --port 63332
```

Ubuntu, from an existing checkout:

```bash
cd /opt/coindcx-trading-bot
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m app.research.continuous.runtime --data-dir /opt/coindcx-research-data --port 63332
```

Do not launch another instance if a task/service already owns that port or data directory. Do not recreate an existing environment unnecessarily. These commands start research, not live trading.

Open [the local Research dashboard](http://127.0.0.1:63332/#research). The dedicated runtime binds to localhost and rejects non-research trading actions. Other trading navigation labels may still be visible because the HTML is shared; their presence does not enable trading in this process.

### Controls

| Control | Effect |
|---|---|
| Start Research | Enables/resumes the continuous worker and queues a cycle |
| Stop Research | Intentionally pauses continuous scans and persists that pause |
| Scan Now | Requests a continuous cycle, subject to busy/queued state and a 60-second start cooldown |
| Export Evidence | Exports the last completed publication and its evidence references |
| Manual Scan controls | Run/cancel the separate on-demand scanner |
| Closing the browser | Does not stop the supervised collector |

Restarting the runtime preserves an intentional research pause. Starting research does not start wallet collection. Restarting the research service does not restart a separate live-trading process.

### Headless Alternative

```bash
python -m app.research.continuous --database data/research/research.sqlite3
```

This runs continuous research without the dashboard supervisor. Use one owner per database. A database lease prevents overlapping collection, but independent databases will produce independent histories.

## Reading the Dashboard

### Freshness and Health

- **Last completed scan:** publication time, not the current market price timestamp.
- **Next research scan:** schedule/queue state. A scan can take time to finish after its start.
- **Research scan schedule:** selected candle interval and scan cadence. Current default is 5 minutes / 5m.
- **Dashboard connected:** last successful status response received by the browser.
- **Collector health:** supervisor observations, distinct from browser connectivity.
- **Latest completed results:** most recent complete publication, still not live quotes.
- **Update pending:** old completed results remain visible during a new scan.
- **Saved results / not current:** stopped, disconnected, failed, or overdue information; not a fresh opportunity list.

The browser polls status every **5 seconds**, which does not rescan all markets every five seconds. Browser loss-of-contact detection is approximately 20 seconds; individual status requests have a longer timeout. Continuous results become overdue after two configured scan intervals, currently about 10 minutes. Startup also treats restored history as historical until a fresh cycle completes.

### Current Watchlist

| Column | Meaning |
|---|---|
| Rank / pair | Ordered research row; not predicted profit or execution priority |
| Current assessment | Current scan's plan geometry/timing, not a historical fill state |
| Public score / 100 | Public-data heuristic; not win probability or live approval confidence |
| Data coverage | Coverage of public ranking components only; 100% does not mean whale data is available |
| Open interest | Interpreted four-hour price/OI state |
| Conditional entry / stop | Hypothetical Binance levels shown only for eligible timing |
| TP1 / TP2 / TP3 | Hypothetical research scale-out levels |
| TP1 R:R | Gross price-based reward/risk, not leverage-adjusted account return |
| Candle close / time | Reference price and last closed research candle, displayed in IST |
| Details | Calculated levels, rejection reasons, contributions, timestamps, and raw input references |

The assessment filters are **All checked pairs**, **Waiting / confirmed**, **Blocked / extended**, and **Not checked / excluded**. Being visible in Waiting / confirmed does not require a 65-point score. It means the timing is eligible. The stricter score qualification is used separately for candidate validation and alerts.

The short reason in a row is `plan.reason`, otherwise the first aggregate reason. It is not a complete checklist. For example, "No bullish price/OI buildup confluence" can appear alongside Waiting for pullback because missing bullish OI prevents High Conviction but neutral OI alone does not forbid a Watch plan. Inspect Details for the full distinction.

### Exact Assessment Labels

| UI label | Internal state | Actual condition |
|---|---|---|
| Waiting for pullback | `forming`, pullback trigger | Eligible conditional plan; not an immediate buy instruction |
| Breakout forming | `forming`, breakout trigger | Valid plan, but closing price has not confirmed the breakout |
| Breakout confirmed | `trigger_confirmed` | Valid breakout plan and reference close above prior resistance |
| Too extended | `overextended` | Price more than configured ATR distance above EMA21; takes precedence over other invalidity |
| Blocked | `invalidated` | Invalid plan geometry or Avoid classification |
| Not checked this cycle | `unscanned` | Not deeply analyzed, unavailable data, or no mapping |

**There is no `Enter Long` or `Enter Short` label.** For pullback plans, `trigger_confirmed` is always false in the plan generator. Fills are evaluated separately and appear in Setup History, not by replacing the watchlist assessment with Enter Long.

## Universe and Scan Selection

The continuous scanner enumerates Binance instruments with all of these properties:

- Trading status is `TRADING`.
- Contract is `PERPETUAL`.
- Quote and margin assets are both USDT.
- Symbol matches the exact base asset plus `USDT`.

It also fetches the CoinDCX INR-margin futures catalogue. Binance pairs remain researchable when CoinDCX listing is absent or unverified. CoinDCX-only instruments without an exact Binance mapping remain visible as unscanned. No alias or multiplier equivalence is invented. Catalogue membership is not a validation of order size, margin availability, spread, or execution price.

All catalogue rows receive cheap screening. Detailed analysis is bounded:

1. Require a valid, fresh 24-hour ticker and at least **1,000,000 USDT** quote volume.
2. Sort liquidity-eligible instruments by quote volume.
3. Select the top **50** as priority instruments.
4. Select up to **50** additional instruments using oldest prior market observation first, then liquidity and symbol.
5. Mark the remainder queued, rather than showing an old result as a new recommendation.
6. Respect the network/cycle budget; unfinished rows remain explicit.

At the audit cutoff, the combined universe contained 528 rows, with 475 liquidity-eligible instruments and 100 analyzed per full scan. These are observed counts, not permanent limits. With a stable 475-pair eligible universe, 50 priority slots and 50 rotating slots imply roughly nine cycles to revisit the entire non-priority remainder: about 45 minutes before failures or changes. **Full catalogue visibility is not full-universe five-minute deep coverage.**

Increasing `deep_pairs_per_cycle` without enough request/time budget will not guarantee more coverage. Existing history and active setups are not guaranteed fresh market observations on every cycle if their pairs rotate out. Ongoing scans can backfill recent candles; long gaps beyond fetched history can remain unverifiable.

## Market Data and Timeframes

### Public Data Used

| Source | Data | Authentication in this implementation |
|---|---|---|
| Binance futures REST | Server time, contract catalogue, 24h tickers, OHLCV, funding, OI history | No account API key |
| CoinDCX public REST | Active INR futures catalogue; manual scanner also checks order books | No account API key |
| Optional CoinGlass | Provider-reported large transfers | Configured API key and suitable access |
| Optional Bubblemaps | Holder/cluster map | Configured API key and suitable access |
| Direct chain RPC | Selected finalized transfer evidence | Provider endpoint, which may contain a credential |

Public endpoints can still fail, rate-limit, or be unavailable from a particular network. An accessible website does not establish API entitlement or full data coverage. Check provider terms/access before enabling optional feeds. This guide does not quote current subscription prices.

### Candle Features

Each selected continuous pair requests 100 candles, discards the still-forming candle, and requires at least 60 closed, contiguous, valid observations. Identical duplicates are collapsed; conflicting duplicates, invalid timestamps/OHLCV, missing bars, zero volume baseline, or invalid ATR are rejected.

Features include EMA21, EMA55, ATR14, the latest close, candle return, and relative volume:

```text
volume_ratio = latest closed candle volume / mean(previous 20 closed candle volumes)

bullish trend = close > EMA21 > EMA55
                AND latest EMA55 > EMA55 three candles earlier

bearish trend = close < EMA21 < EMA55
                AND latest EMA55 < EMA55 three candles earlier

extension_ATR = (close - EMA21) / ATR14
```

Volumes are compared within the same timeframe. A 5m forming candle is not compared with completed 5m candles to manufacture a low volume ratio.

### What 5m Means Here

The continuous planner uses closed **5m** price candles. It starts scheduled cycles near the five-minute boundary, with approximately two seconds of buffer. Requests are sequential, so not every pair is inspected at exactly the same instant; per-pair candle times and publication time are preserved.

OI interpretation still uses an aligned **four-hour** change. For 5m operation it requests up to 60 OI points; one-hour acceleration needs intermediate points. This is not a complete adaptive micro-to-macro engine. The original 15m history remains separately identified and must not be relabeled as 5m results.

Your trading preference, **Weighted Hybrid decisions on 5m with 1m execution**, is a separate workflow. Changing the research scanner to 5m did not make its ranking validation a replay of that strategy or add 1m execution to its conditional replay.

## Scores and Assessments

### Public Ranking: `public-v1`

This score is displayed in Current Watchlist. Each component is bounded to a fraction from zero to one, then multiplied by its weight and 100.

| Component | Weight | Formula/interpretation |
|---|---:|---|
| Relative volume | 25% | `clamp(volume_ratio / 3)` |
| OI buildup/acceleration | 20% | `0.7 * clamp(4h OI change / 8%) + 0.3 * clamp(1h acceleration / 2 percentage points)`; zero if OI direction is bearish |
| Bullish EMA trend | 20% | `clamp(((EMA21 - EMA55) / ATR) / 2)` only in bullish trend |
| Range compression | 15% | Compare average last-five candle ranges with prior 20; full compression contribution at a ratio of 0.5 |
| Resistance proximity | 20% | `clamp(1 - abs((prior 20-bar high - close) / ATR) / 3)` |

Missing OI earns no OI contribution and is not redistributed to other features. Excessive upward extension removes the proximity contribution. Positive funding above **0.05%** subtracts **10 score points**. The final score is bounded below at zero; weights sum to one.

A score of 65 is **not** a 65% likelihood of profit. `ranking.candidate_score = 65` qualifies a candidate only when timing is also eligible. It does not turn a blocked plan into an eligible plan. Even a 90-point score cannot override invalid risk/reward or a late-chase block.

### OI Interpretation

Four-hour price and OI endpoints must align in time. The OI threshold is 1%; the price threshold is 0.5%. If either change does not clear its threshold, the combined state is `flat`.

| State | Default OI score | Direction used by continuous confluence |
|---|---:|---|
| Price up / OI up | 85 | Bullish |
| Price down / OI up | 15 | Bearish |
| Price up / OI down | 60 | Neutral |
| Price down / OI down | 30 | Bearish |
| Flat | 50 | Bearish if price direction is below its negative threshold; otherwise neutral |

An expensive positive funding rate reduces this separate OI score by 15 points. OI counts outstanding contracts; it does not identify whether an institution opened a long, short, hedge, or arbitrage position.

### Optional Multi-Provider Confluence

This is a different score shown in Details:

```text
composite = 0.40 * whale_score
          + 0.40 * OI_score
          + 0.20 * holder_safety_score
```

Missing families contribute nothing; available weights are reported as coverage. With only OI, coverage is 40% and the theoretical weighted-family ceiling is 40/100. With the current OI score table, its maximum actual contribution is **34/100** (`85 * 0.4`) before funding penalties.

High Conviction requires all of: bullish OI; available, adequately covered accumulation-proxy whale evidence; available non-excluded holder evidence; valid geometry; and composite score at least 70. With whale/holder feeds disabled, **High Conviction is unreachable**.

This does **not** prevent public-data Watch plans or public candidate validation. It does mean an absent whale/holder feed must not be presented as fully verified confluence. The wallet evidence pilot is separate and does not currently replace either optional scoring feed.

### Classification and Ordering

`avoid` is assigned for excluded holder concentration, bearish OI direction, or whale distribution. Otherwise the row is `watch` when any High Conviction requirement is unmet, and `high_conviction` only when all pass.

Rows sort by classification, then timing, then public score descending, then pair name. Therefore a low-scoring eligible Watch plan can appear above a higher-scoring blocked plan. Rank changes compare pairs common to adjacent scans under the same configuration, not unrelated rotating universes.

## Conditional Entry and Exit Rules

These rules belong to the research simulation. They do not override your trading bot's notional stop, leverage, risk percentage, or trailing policy.

### Pullback Entry: Current Default

The planner requires a bullish closed-candle trend and constructs a future EMA21 limit-entry idea. It does not enter immediately at the latest price. Price below the proposed entry invalidates the current pullback geometry. A price over 2.5 ATR above EMA21 is too extended.

Every eligible current scan can describe a newly calculated EMA level, while an existing Setup History episode retains its original level. A historical fill therefore must be checked against the frozen setup, not the latest watchlist price.

### Breakout Entry: Implemented Alternative, Not Enabled Globally

With `levels.trigger = "breakout"`, or an exact-symbol trigger override, the planner uses the prior 20-candle high and requires a closed research candle above it. A valid confirmed setup can display **Breakout confirmed**. Replay then checks the subsequent full candle's opening price with slippage, entry geometry, and chase limits.

Changing to breakout is a strategy change, not a UI fix. It should be evaluated as a new configuration. It does not add short trades.

### Stop Construction

For a long plan:

```text
structural candidate = lowest low of prior 20 candles
volatility candidate = entry - 1.5 * ATR14
eligible stop = positive AND at least 1 ATR below entry
chosen stop = higher/tighter of the eligible candidates
risk distance = entry - chosen stop
```

The selected stop must have enough ATR room. These are price distances, not fixed percentages of portfolio capital. No liquidation model, leverage selection, account sizing, or CoinDCX margin check is applied by this planner.

### Targets and the Main Rejection Bottleneck

- TP1 is the smaller of **entry + 1.5R** and the nearest overhead confirmed pivot/window resistance.
- TP2 is **entry + 2R**.
- TP3 is **entry + 3R**, or a larger sector-relative stretch target when eligible.
- Required ordering: `0 < stop < entry < TP1 < TP2 < TP3`.
- TP1 must still offer **at least 1.5R**.

The TP1 cap and minimum are both 1.5R. Consequently, any overhead resistance closer than 1.5R rejects the plan. The code does not accept a closer first partial target and evaluate the remaining targets' combined reward. This is the main measured blocker in the current sample, not an unavailable API key.

Illustration, not a market recommendation: entry 100, stop 98 gives R = 2. Required TP1 is at least 103. Resistance at 102 caps TP1 to 102, leaving only 1R, so the plan is blocked. Raising the public score cannot repair this geometry.

### Hypothetical Scale-Out and Trailing

The research replay allocates 50% to TP1, 30% to TP2, and 20% to TP3. After TP2, the remaining position trails at `closed price - 2 * ATR14`, never lowering the stop. A trailing update takes effect on the next candle, not retroactively inside the candle that calculated it.

Plans expire after 12 hours from original publication. Open replays close the remaining fraction at the last eligible fully closed candle on timeout. An unfilled plan expires as `not_triggered`. These mechanics do not imply executable exchange orders or guaranteed limit fills.

The plan's text also describes whale/OI signal invalidation. Replay does **not** simulate a discretionary post-entry signal-flip exit. An already entered frozen plan continues its price-based replay exits.

## Setup History and Validation

Three different evaluation systems coexist. Do not combine their counts or call any of them real portfolio PnL.

### 1. Conditional Setup Replay

An eligible forming/confirmed row creates a frozen setup even when its score is below 65. This is deliberate observational coverage of geometrically eligible plans, not candidate approval.

- One unresolved episode per pair and research interval.
- Entry, stop, targets, original score, config ID, publication time, and evidence are frozen.
- Later scans append observations; they do not move the original entry or restart expiry.
- A resolved setup must see a later invalid assessment and then a fresh valid candle to re-arm.
- Configuration edits do not fork an unresolved episode.
- A missing-data pair is not silently canceled or reset.
- A fresh invalid assessment can cancel an unfilled setup when no unresolved replay gap prevents that conclusion.

Replay uses only complete candles starting after publication. For a pullback, a later candle touching the frozen EMA limit can produce a simulated fill. The fill candle cannot claim a favorable high that may have occurred before entry. Stops are processed first when intrabar ordering is unknown. Gaps, invalid bars, and missing history remain explicit.

Replay result names include `pending`, `trigger_confirmed`, `open`, `stop_loss`, `trailing_stop`, `all_targets`, `timeout`, `not_triggered`, `invalidated`, `gap_invalidated`, `fill_geometry_rejected`, `gap_chase_rejected`, and `data_gap`.

Costs are explicit: fees default to 6 bps per side. The 4 bps slippage assumption applies to modeled breakout entries and stop/timeout exits; resting pullback entries and target exits use their limit prices with fees. It is not accurate to describe every replay fill as having identical slippage treatment.

An open replay's displayed net return is the **realized portion minus charged fees**, not total mark-to-market profit. For example, a new open position with no scale-outs can show -0.06% from the entry fee alone.

### 2. Continuous Four-Hour Ranking Validation

The scanner records non-overlapping samples for all analyzed pairs, including blocked/below-threshold controls. A qualified idea requires eligible timing and a score of at least 65 at the sampling time.

- Start: next full research candle open after publication.
- Horizon: four hours, not the 12-hour setup expiry.
- Current resolution: 5m; older 15m samples remain separate.
- Direction: long for this continuous model.
- Assumed entry/exit: opening and final closing reference prices adjusted for slippage and fees.
- A sample needs every expected closed candle before it can be checked.
- Missing data is not a loss, a win, or a zero-return result.
- Completed samples are not rewritten using later settings.

The Validation ledger defaults to qualified ideas under the selected/current configuration. Comparison observations have a separate filter. Outcome detail shows original publication, score, reference evidence, test times, and stored candles. It is a price-direction test, not an execution audit of Weighted Hybrid.

The 30-qualified-observation indicator is a review milestone, not proof of profitability. Multiple samples can share coins and market conditions. Candidate-versus-control comparisons are observational, not randomized causal evidence. Funding and a leveraged portfolio model are absent.

Sampling does not record every five-minute row as a new four-hour prediction. This is why repeated qualifying rows can outnumber unique candidate samples. A score crossing inside an already reserved observation window need not create another sample.

### 3. Manual Scan Shadow Outcomes

These use manual long/short scores, a next-complete-1m-open reference entry, and fixed 15m/1h/4h horizons. They apply the manual scan's frozen round-trip cost assumption. They do not use the continuous EMA pullback/scale-out rules.

### How to Judge Results

First separate configuration, timeframe, selected candidates, controls, pending samples, and missing-data cases. Then compare counts, mean/median net return, adverse excursions, and regime/pair concentration. Do not add independent percentage returns and call the sum portfolio profit. Do not tune and claim validation on the same time period.

The audit found zero score-qualified 5m candidate samples at its cutoff. That is **insufficient candidate evidence**, not a 0% win rate and not proof that future performance will be good or bad.

## Manual Long and Short Research

Manual Scan is the existing bilateral research component. It fetches 5m, 15m, 1h, and 4h candles, constructs separate directional scores, selects a side, and applies its own gates. It does not consume `ranking.weights` from continuous research.

Trend alignment contributes up to 70 points with timeframe weights of 15%, 20%, 30%, and 35%; volume and the latest 5m direction contribute up to 15 each. Extension and expensive directional funding reduce the score. Required checks include aligned 1h/4h trends, no opposing lower-timeframe trend, volume, score, extension, and usable OI/funding evidence.

CoinDCX spread, book depth, and cross-venue price divergence are reported separately as verified, blocked, or unverified. Verified public book checks still do not authorize an order.

| Manual setting | Default | Meaning |
|---|---:|---|
| `max_pairs` | 20 | Top-liquidity candidates to inspect; supported range 1-50 |
| `min_quote_volume` | 10,000,000 | USDT 24h liquidity floor |
| `min_volume_ratio` | 0.5 | Latest closed 5m relative volume floor |
| `min_score` | 65 | Directional heuristic minimum |
| `max_extension_atr` | 2.5 | Anti-chase limit |
| `max_spread_bps` | 20 | CoinDCX spread limit |
| `max_divergence_bps` | 50 | Binance/CoinDCX price divergence limit |
| `min_depth_usdt` | 5,000 | Directional visible depth within 10 bps |
| `funding_warning_pct` | 0.05 | Expensive directional funding threshold |
| `round_trip_cost_pct` | 0.20 | Frozen manual shadow-outcome cost assumption |

Run it from the Manual Scan view. It has a separate one-minute scan-start cooldown and approximately four-minute network budget. A status refresh does not launch it. Long/short bucket labels are research classifications, not Enter Now instructions.

## Whale, Holder, and Wallet Evidence

### Optional CoinGlass Transfer Feed

Enablement requires a supported API entitlement, `COINGLASS_API_KEY`, an exact asset/chain mapping, and `providers.coinglass_enabled = true`. No access is assumed merely because the website is free to view.

The current adapter calls `/api/chain/v2/whale-transfer`, filters exact exchange labels, and summarizes 1h/4h/24h inflow/outflow. Its direction score is based on 24h exchange-flow imbalance. Outflow is only a custody/accumulation proxy; inflow is not proof of a sale.

The configured provider floor is $10 million, while the desired whale threshold is $500,000. With those settings the analysis explicitly declares limited coverage. Increasing the threshold changes which whale cohort is studied; it does not recover smaller transfers. This adapter deduplicates at chain/transaction level, not at every transfer-log index, and has no proof of complete on-chain coverage.

### Optional Bubblemaps Holder Feed

Requires `BUBBLEMAPS_API_KEY`, enablement, and exact `holder_chain`, `holder_address`, and share-unit mapping. It reads holder clusters, excludes reported custodial shares from the relevant risk calculation, and tests concentration against the configured limit.

Shared transfer links do not prove common ownership. Changes in holder share do not prove purchases. Top-holder coverage is not a complete beneficial-ownership graph. A native coin without a supported map remains unsupported, not risk-free.

### Direct ETH / BNB Chain / Solana Pilot

The separate [Wallet Evidence Pilot guide](WALLET_EVIDENCE_PILOT.md) contains the activation procedure. Its implemented scope is:

| Network | Recorded evidence | Important omissions |
|---|---|---|
| Ethereum | Finalized top-level native transfers and explicit ERC-20 Transfer logs | Internal native calls, economic swap/bridge/LP decoding |
| BNB Smart Chain | Same EVM collection model, separate chain identity | Same omissions; not interchangeable with Ethereum |
| Solana | Finalized parsed System and standard SPL transfers, including inner instructions and owner resolution | Token-2022, opaque instructions, economic swap/staking/wrapping decoding |

Events have precise raw amounts, explicit asset identity, raw evidence, block/slot identity, and observation/availability timestamps. Transactions that fail do not contribute executed transfer events. Duplicate delivery does not add the same event again. A finalized-history conflict stops progress instead of silently changing history. A delayed backfill is not visible to an earlier point-in-time query.

All chains are disabled and watchlists are empty by default. RPC values are supplied privately to the wallet worker through `WALLET_ETH_RPC_URL`, `WALLET_BSC_RPC_URL`, and `WALLET_SOL_RPC_URL`. The collector needs no wallet private key, seed phrase, exchange trading permission, or signing key.

Ethereum chain ID must be 1; BSC chain ID must be 56. Solana requires an independently verified expected genesis hash. Never invent token addresses or institution labels from a ticker/name.

Commands, after configuration:

```bash
python -m app.research.wallets check
python -m app.research.wallets collect --once --database /path/to/research-data/research/wallets.sqlite3
python -m app.research.wallets collect --database /path/to/research-data/research/wallets.sqlite3
python -m app.research.wallets status --database /path/to/research-data/research/wallets.sqlite3
```

`check` validates configuration without network requests. `status` opens the wallet store and can create an empty store if absent; it does not perform collection. The dashboard status reader itself is read-only and does not create the database. Keep `wallets.sqlite3` beside the continuous `research.sqlite3`.

The default wallet poll delay is 15 seconds, with per-chain request limits. This is bounded finalized-block polling, not a subminute signal guarantee. Free-tier quotas can produce backlog, especially on Solana. "Caught up" refers to the head sampled at the beginning of a cycle and does not mean all event types are decoded.

The [Wallet Flow Framework](WALLET_FLOW_FRAMEWORK.md) is a target architecture. Continuous multi-scale memory, provisional-event revision handling, economic-action decoding, USD valuation, independent entity discovery, and smart-money performance attribution are **not implemented by merely adding this collector**.

## Configuration Reference

### Which File Controls What?

| Source | Scope | When changes apply |
|---|---|---|
| `config/research.json` | Continuous ranking, geometry, providers, schedule, alerts | Loaded at each new cycle |
| `RESEARCH_CONFIG_PATH` | Alternate continuous JSON path | Set in the actual worker/supervisor environment |
| Runtime `--config` | Explicit continuous JSON path; takes precedence over environment/default | Runtime launch |
| Manual Scan form / `ScanConfig` | Separate on-demand long/short scanner | Next manual scan; snapshot freezes values |
| `config/wallet_research.json` | Separate wallet collector | Collector restart; dashboard reads the bundled default path |
| Wallet CLI `--config` | Alternate wallet config for that CLI process | CLI launch; not automatically the dashboard's config |
| Trading `.env`, runtime env, live settings | Separate trading strategies/risk/account execution | Governed by the trading bot, not this research guide |

The isolated research runtime does not load the trading `.env` files. Changing `LIVE_MIN_CONFIDENCE`, short-strictness bonuses, leverage, or trading trailing percentages will not change this continuous research score.

Configuration has strict expected fields and bounds. Unknown keys, malformed JSON, invalid ranges, mismatched candle/schedule intervals, or weights that do not sum to one are rejected. Credentials do not belong in JSON because configurations are saved in evidence snapshots.

Inspect the effective configuration without starting a scan:

```bash
python -c "from app.research.continuous.config import load_config, config_id; c=load_config(); print(config_id(c)); print(c['market_interval'], c['schedules'], c['levels'], c['ranking'])"
```

For a custom file, call `load_config('path/to/research.json')`. To identify settings actually used by a completed scan, inspect that publication's `config`, not just the current file on disk.

### Continuous Settings: Schedule and Requests

| Key | Current default | Purpose |
|---|---|---|
| `schema_version` | 1 | Validated config schema |
| `autostart` | true | Start when runtime allows autostart, unless intentionally paused |
| `market_interval` | `5m` | Supported alternatives: 5m or 15m |
| `schedules.market_seconds` | 300 | Must match candle interval: 300 or 900 |
| `schedules.whale_seconds` | 3600 | Optional whale cache interval |
| `schedules.holder_seconds` | 21600 | Optional holder cache interval |
| `universe.min_quote_volume` | 1000000 | 24h USDT liquidity floor |
| `universe.deep_pairs_per_cycle` | 100 | Maximum selected deep scans, not guaranteed completions |
| `universe.priority_pairs` | 50 | Highest-liquidity priority slots |
| `network.requests_per_minute` | 120 | Local request pacing, not a guarantee about exchange weight quotas |
| `network.timeout_seconds` | 8 | Request timeout |
| `network.retries` | 2 | Additional transient connection/server-error attempts |
| `network.backoff_seconds` | 2 | Exponential retry base |
| `network.rate_limit_wait_seconds` | 120 | Minimum quota cooldown, extended by Retry-After |
| `network.cycle_budget_seconds` | 240 | Bounded request cycle; must be below scan interval |

401/403 denies are not hammered once per pair. 418/429 stop requests to the affected provider and preserve cooldown state across restarts. Invalid/missing responses are unavailable evidence, not fabricated neutral values. An unsuccessful cycle retains its previous publication as history.

### Continuous Settings: Ranking and Confluence

| Key | Current default | Purpose |
|---|---|---|
| `ranking.weights` | volume .25, oi .20, trend .20, compression .15, proximity .20 | Public ranking weights |
| `ranking.volume_full_ratio` | 3 | Relative volume for full volume points |
| `ranking.oi_full_pct` | 8 | Four-hour OI rise for full buildup component |
| `ranking.oi_acceleration_full_pp` | 2 | One-hour acceleration for full acceleration component |
| `ranking.trend_full_atr` | 2 | EMA spread in ATR for full trend points |
| `ranking.compression_full_ratio` | .5 | Range ratio for full compression points |
| `ranking.breakout_distance_atr` | 3 | Proximity contribution distance scale |
| `ranking.funding_penalty` | 10 | Public score penalty in points |
| `ranking.candidate_score` | 65 | Candidate/alert threshold, not live confidence |
| `weights` | whale .40, oi .40, holder_safety .20 | Separate confluence weights |
| `thresholds.high_conviction_score` | 70 | Composite minimum in addition to evidence gates |
| `thresholds.whale_usd` | 500000 | Desired transfer cohort threshold |
| `thresholds.flow_imbalance` | .2 | Exchange-flow imbalance needed for accumulation/distribution proxy |
| `thresholds.oi_change_pct` | 1 | OI direction classification threshold |
| `thresholds.price_change_pct` | .5 | Price direction classification threshold |
| `thresholds.oi_outlier_pct` | 20 | OI outlier flag threshold |
| `thresholds.flat_price_pct` | 1 | Maximum price change for that OI-outlier flag |
| `thresholds.funding_warning_pct` | .05 | Funding rate in percent, not fraction |
| `thresholds.top_cluster_risk_pct` | 35 | Holder concentration exclusion threshold |
| `thresholds.top_n_clusters` | 5 | Number of holder clusters summarized |
| `oi_scores` | 85 / 15 / 60 / 30 / 50; penalty 15 | State scores listed in the OI table above |

### Continuous Settings: Plans, Sectors, Providers, Alerts

| Key | Current default | Purpose |
|---|---|---|
| `levels.trigger` | `pullback` | Conditional EMA pullback; alternative `breakout` |
| `levels.atr_multiplier` | 1.5 | ATR stop candidate distance |
| `levels.min_stop_atr` | 1 | Minimum stop breathing room |
| `levels.swing_lookback` | 20 | Prior-window swing/resistance search |
| `levels.min_rr` | 1.5 | Required TP1 gross reward/risk |
| `levels.tp1_r`, `tp2_r`, `tp3_r` | 1.5, 2, 3 | Nominal R-multiple target levels |
| `levels.scale_out` | [.5, .3, .2] | Target fractions; must sum to one |
| `levels.max_extension_atr` | 2.5 | Anti-chase distance |
| `levels.trail_atr` | 2 | Post-TP2 replay trail |
| `levels.plan_hours` | 12 | Frozen setup expiry |
| `levels.fee_bps_per_side` | 6 | Simulation fee, not a fetched account fee |
| `levels.slippage_bps` | 4 | Simulation slippage assumption |
| `laggards.min_correlation` | .65 | Required synchronized peer return correlation |
| `laggards.min_samples` | 40 | Minimum return observations |
| `laggards.leader_return_pct` | 5 | Minimum peer leader return |
| `laggards.min_gap_pct` | 3 | Required relative-performance gap |
| `sectors` | Explicit symbol-to-sector dictionary | Untagged assets stay Unclassified |
| `pair_overrides` | {} | Exact Binance symbol; only `trigger` override is supported |
| `assets` | {} | Exact provider chain/symbol/contract mappings |
| `providers.coinglass_enabled` | false | Optional transfer adapter |
| `providers.bubblemaps_enabled` | false | Optional holder adapter |
| `providers.coinglass_min_transfer_usd` | 10000000 | Configured minimum provider coverage floor |
| `providers.coinglass_exchange_labels` | Binance, Coinbase, Kraken, OKX, Bybit, Bitfinex, KuCoin | Exact labels recognized for flow classification |
| `alerts.telegram_enabled`, `discord_enabled` | false | Explicit opt-in channels |
| `alerts.cooldown_seconds` | 21600 | Pair/channel opportunity cooldown; lifecycle handling is separate |

Sector comparisons use up to 96 synchronized returns from the available research candles. Thus their elapsed lookback changes with candle interval; they are not a fixed macro accumulation measure. Sector stretch prices use relative returns applied to the laggard's own price, not the leader's nominal price.

Example trigger override, as a fragment to merge into the existing JSON, not a complete config:

```json
{"pair_overrides": {"BTCUSDT": {"trigger": "breakout"}}}
```

This example is documentation, not an applied strategy recommendation. JSON does not permit comments or trailing commas. Continuous configs are fingerprinted; changing even a non-scoring config field can create a new config ID and separate validation group.

### Wallet Settings

| Key | Default / requirement |
|---|---|
| `schema_version` | 1 |
| `poll_seconds` | 15; wait between collector cycles |
| `network.timeout_seconds` | 8 |
| `network.requests_per_minute` | 60 per enabled chain |
| `network.requests_per_cycle` | 60 per enabled chain |
| `network.max_blocks_per_cycle` | 5 per chain |
| `network.max_storage_mb` | 512 approximate safety budget |
| `chains` | Separate `ethereum`, `bsc`, `solana` entries |
| `chains.<chain>.enabled` | false by default; nonempty watchlist required to enable |
| `rpc_url_env` | Name of private environment variable; not the URL/key itself |
| `expected_genesis_hash` | Independently verified Solana value; null for EVM |
| `start_height` | null starts at first observed finalized head; explicit history requires coverage |
| `wallets` | Address, entity ID, attribution level, evidence reference |
| `assets` | Exact native/contract/mint identity, display symbol, decimals |

Unknown attribution must not assert an entity. Probable/verified attribution requires an evidence reference but remains a supplied assertion, not automatic identity verification. Only verified same-entity labels classify internal movements. Schema ceilings of 100 wallets/assets per chain are not promised free-tier capacity.

Changing watchlist/assets/attribution/history scope starts a new stream and retains the prior one. Do not casually change scope and assume a continuous historical series. Unlike continuous config, wallet config is loaded at worker startup, not hot-reloaded per cycle.

## Alerts

Research notifications are opt-in and separate from the trading bot's portfolio/stop-loss notifications.

| Channel | Environment variables |
|---|---|
| Telegram | `RESEARCH_TELEGRAM_BOT_TOKEN`, `RESEARCH_TELEGRAM_CHAT_ID` |
| Discord | `RESEARCH_DISCORD_WEBHOOK_URL` |

Enable the matching flag in the continuous config, configure secrets privately in the supervisor environment, and restart that research supervisor when changing its environment. A later terminal's environment is not automatically inherited by an already-running task/service.

Telegram requires a bot conversation the user has started and phone notifications enabled for that chat. Discord uses an allowed HTTPS Discord webhook. Never publish these secrets in Git, screenshots, this README, or chat.

Notifications concern meaningful setup transitions, such as creation, score qualification, trigger, simulated open, completion, and invalidation, for qualified or previously qualified episodes. Unchanged scans are silent. Deduplication keys include setup, event type, and channel; repeated event types for the same setup are not repeatedly sent after success. New opportunities respect pair/channel cooldown; lifecycle events are not hidden by that opportunity cooldown.

Messages are sent after research cycle processing, not instantly at an exchange tick. Failed deliveries are recorded and not marked delivered, but there is no guaranteed durable retry queue for every missed setup event. Health-transition alerts have their own retry handling. Neither a dead supervisor nor an offline computer can notify you; external host monitoring is a separate requirement.

## Storage, Backups, and Logs

### Files Under a Dedicated Runtime Root

```text
research-data/
  research/
    research.sqlite3       continuous publications, observations, validation
    wallets.sqlite3        separate wallet evidence, when created
  scans/                   manual scan JSON snapshots
  outcomes/                manual shadow-outcome JSON sidecars
  logs/
    research.log           supervisor and child output
    research.log.1 ...     rotated logs
  backups/                 offline compaction backups, when created
  supervisor.lock          one supervisor per runtime directory
```

The continuous store includes observations, publications, closed candles, setups, setup events, replay results, validation samples/results, and operational state. Raw observations and publications are append-only; completed evaluation results are preserved while pending results and health state can update.

Payloads may be plain JSON or losslessly compressed JSON. Use the store's `decode` helper or dashboard exports; SQLite `json_extract` on a compressed payload is not valid. Export Evidence contains observation references, not a full database backup. Export History includes setups, events, and validation results but is also not a replacement for raw-evidence backups.

### Backup Rules

Use SQLite's online backup API for an active database, or stop all writers before a coherent offline copy. Do not copy only a live `.sqlite3` file while ignoring its WAL. Do not sync an active SQLite database through OneDrive or share it between laptop and VPS writers. Back up manual JSON archives separately.

The database is not capped by log rotation. Continuous history is retained and grows. Wallet writes stop at their approximate storage budget rather than deleting old evidence. Allow headroom for journals, backups, and filesystem overhead.

Offline compaction, with the research task/service stopped:

```bash
python -m app.research.continuous.runtime --data-dir /path/to/research-data --compact
```

Compaction takes a new SQLite backup, compresses supported payloads, verifies per-table content hashes and integrity, and vacuums free pages. It does not purge old observations or decide which trades to keep. Keep the backup until verified. Do not use compaction as a way to erase unfavorable outcomes.

### Logs and Supervision

Research logs rotate at 2,000,000 bytes with five backups. The supervisor checks its child approximately every 15 seconds, restarts failures after a delay, and detects an overdue worker using the configured cycle budget plus 120 seconds of grace. With the current 240-second cycle budget, that threshold is about 360 seconds, not the older 960-second configuration.

The full supervision path also respects intentional pauses, avoids duplicate owners, and checks for orphaned workers. A surviving process does not prove a successful scan; confirm a fresh publication.

Windows log tail:

```powershell
Get-Content 'C:\TradingBots\coindcx-research-data\logs\research.log' -Tail 80
```

Ubuntu service logs:

```bash
journalctl -u coindcx-research -n 80 --no-pager
```

Keep trading logs separate. No wallet credential URLs should appear in public status/errors; inspect logs privately before sharing diagnostic archives.

## Laptop and VPS Deployment

### Windows Background Task

For initial installation, after preparing the chosen Python environment:

```powershell
.\scripts\install_research_task.ps1 -Python 'C:\TradingBots\coindcx-trading-bot\.venv\Scripts\python.exe' -DataDirectory 'C:\TradingBots\coindcx-research-data'
Get-ScheduledTask -TaskName 'CoinDCX Research Collector'
```

The script prefers `pythonw.exe` when present and registers a limited, interactive-user login task. It can run with the browser closed, but not with the laptop powered off, asleep, disconnected, or signed out. Do not install duplicate supervisors for the same directory.

### Ubuntu Service

After installing the checked-out code/environment, use your real deployment account in place of `botuser`:

```bash
sudo RESEARCH_USER=botuser RESEARCH_DATA_DIR=/opt/coindcx-research-data bash scripts/install_research_service.sh
systemctl status coindcx-research --no-pager
```

The installer creates/enables the research service, uses `.venv/bin/python` by default, and loads an optional private `/etc/coindcx-research/alerts.env`. It does not modify the trading service. A custom Python path/port can be supplied through `RESEARCH_PYTHON`/`RESEARCH_PORT`.

For subsequent code updates, preserve configuration and data, inspect Git state before pulling, reinstall dependencies if necessary, and restart **only** `coindcx-research` to load changed Python code. JSON research settings are reloaded per cycle; changed process environment requires a restart. Never use a destructive Git reset to resolve runtime-data confusion.

The dashboard is unauthenticated and must remain private. Use an SSH tunnel rather than opening port 63332 to the internet:

```bash
ssh -L 63333:127.0.0.1:63332 botuser@YOUR_VPS_IP
```

Open `http://127.0.0.1:63333/#research` for the VPS, leaving local `63332` distinct. Separate data directories and histories are expected; there is no automatic laptop/VPS history sync. Free provider quotas may be shared across both instances using the same account.

## Troubleshooting

| Symptom | What to check / explanation |
|---|---|
| Never says Enter Long | That label is not implemented. Pullback plans remain conditional; check frozen Setup History for simulated fills. |
| Never says Enter Short | Continuous ranking/plans/replay are long-only. Manual Scan is a separate bilateral model. |
| All rows Blocked | Inspect Details and the actual gate counts; bearish/neutral structure and nearby resistance can invalidate long geometry. |
| High score but Blocked | Score does not override risk/reward, trend, extension, or Avoid classification. |
| Low score at rank 1 | Sorting prioritizes classification/timing before score. Rank 1 is not approval. |
| Always Waiting for pullback | Default trigger is conditional EMA entry, and watchlist state is not historical fill state. Recalculated current EMA and frozen entry may differ. |
| No High Conviction | Currently whale/holder evidence is missing; OI-only composite cannot reach 70. This is separate from public candidate qualification. |
| 100% coverage but whale unavailable | Coverage beside the public score measures public components, not all optional data. |
| Validation has zero qualified ideas | Eligible timing plus score threshold plus non-overlapping sample schedule may produce none. Check Comparison observations separately. |
| Setup History has fills but no qualified ideas | All eligible geometries can be replayed, even below 65. A simulated fill is not qualification. |
| 15m results remain after switching to 5m | Historical records keep their original interval/config; they must not be rewritten. Check the current schedule and selected validation group. |
| Some pairs do not refresh every 5m | Only the configured deep-scan budget is processed; non-priority pairs rotate. |
| Research stopped after laptop sleep | No data collection during sleep/offline time; check supervisor recovery and retained gaps. |
| DISCONNECTED but process exists | Browser/server connectivity differs from process health. Inspect current publication and logs before restarting. |
| Stale results after failure | Prior evidence is intentionally retained as history, not relabeled fresh. |
| 401/403 | Provider access/key/entitlement or network restriction; do not disable validation to hide it. |
| 418/429 / backoff | Respect quota cooldown. Reduce demand rather than starting duplicate scanners. |
| `data_gap` | Required forward candles are missing or invalid. Do not count the outcome as a win/loss. |
| Wallet Evidence says not configured | Correct default until read-only endpoints, exact assets, and wallets are configured and the separate collector starts. |
| Wallet backlog grows | Provider budget or full-block polling cannot keep up. Increase coverage only after measuring access/cost; zero backlog is not promised. |
| Config changed but nothing changed | Check the running checkout, explicit `--config`, process environment, active publication config ID, and next successful cycle. |
| Port/lease already in use | Identify the existing research owner; do not kill unrelated Python/trading processes. |
| No phone alert | Channels default off; check credentials, qualification, cooldown, destination permissions, and delivery state. |

## Implementation Map and API

### Source Map

| Path | Responsibility |
|---|---|
| [continuous/config.py](../app/research/continuous/config.py) | Config validation, defaults, fingerprint, per-pair trigger override |
| [continuous/service.py](../app/research/continuous/service.py) | Universe rotation, scans, publications, worker lifecycle |
| [continuous/feeds.py](../app/research/continuous/feeds.py) | Request pacing, retries, optional provider adapters |
| [continuous/analysis.py](../app/research/continuous/analysis.py) | OI, whale flow, concentration, laggards, confluence tiers |
| [continuous/ranking.py](../app/research/continuous/ranking.py) | Public score, timing, ordering, rank comparison |
| [continuous/levels.py](../app/research/continuous/levels.py) | Conditional long entry, stop, targets |
| [continuous/setups.py](../app/research/continuous/setups.py) | Frozen episodes, lifecycle/reset, candidate samples |
| [continuous/replay.py](../app/research/continuous/replay.py) | Conservative conditional OHLC simulation |
| [continuous/validation.py](../app/research/continuous/validation.py) | Four-hour samples, controls, score groups |
| [continuous/validation_audit.py](../app/research/continuous/validation_audit.py) | Individual verdict ledger and candle evidence |
| [continuous/store.py](../app/research/continuous/store.py) | SQLite, compressed payloads, leases, compaction |
| [continuous/runtime.py](../app/research/continuous/runtime.py) / [health.py](../app/research/continuous/health.py) | Research-only server, supervisor, recovery, health |
| [continuous/notify.py](../app/research/continuous/notify.py) | Research setup notifications |
| [market_scanner.py](../app/research/market_scanner.py) / [scan_service.py](../app/research/scan_service.py) | Manual bilateral scanner and worker |
| [outcome_tracker.py](../app/research/outcome_tracker.py) | Manual shadow outcomes |
| [wallets](../app/research/wallets/) | Separate chain adapters, ledger, limits, readiness |
| [dashboard/server.py](../app/dashboard/server.py) | HTTP routes |
| [continuous.js](../app/dashboard/static/continuous.js) / [validation.js](../app/dashboard/static/validation.js) | Continuous dashboard and validation views |
| [research.js](../app/dashboard/static/research.js) | Manual scanner UI |
| [config/research.json](../config/research.json) / [wallet_research.json](../config/wallet_research.json) | Current bundled research settings |

### Local HTTP Endpoints

All paths below are relative to the private dashboard origin. The research API is an internal application interface, not an authenticated public service.

| Method / path | Purpose |
|---|---|
| `GET /api/health` | Server responds; not proof of fresh market data |
| `GET /api/research/continuous` | Status, latest snapshot, summaries, wallet readiness |
| `POST /api/research/continuous/start` | Resume continuous scanning |
| `POST /api/research/continuous/stop` | Persist intentional pause |
| `POST /api/research/continuous/refresh` | Queue scan subject to worker/cooldown rules |
| `GET /api/research/continuous/evidence?id=...` | Raw observation by ID |
| `GET /api/research/continuous/history` | Setup/event history and validation results |
| `GET /api/research/continuous/history?setup_id=...` | One frozen setup and its events |
| `GET /api/research/continuous/validation` | Filtered validation ledger |
| `GET /api/research/continuous/validation?sample_id=...` | One sample's original evidence and outcome |
| `GET /api/research` | Manual scanner status/defaults |
| `POST /api/research` | Start manual scan using validated manual settings |
| `POST /api/research/cancel` | Cancel manual scan |
| `GET /api/research/outcomes` | Manual shadow-outcome status/summary |
| `POST /api/research/outcomes/refresh` | Wake manual outcome checking |

Validation filters include `config_id`, `cohort`, `outcome`, `q`, and zero-based `page`. Use the dashboard's supported controls rather than guessing filter values. API health alone must not be used as a trading readiness signal.

## Verification and Limitations

Automated tests cover closed-candle validation, score/geometry behavior, failed providers, configuration, pair isolation, frozen setups, missing-data handling, conservative replay, storage, supervision, and wallet identity/deduplication/checkpoint behavior. Browser tests cover research and validation interactions with synthetic fixtures, desktop/mobile layout, stale/error states, and evidence charts.

Typical local verification commands:

```bash
python -m unittest discover -s tests -p "test_*research*.py"
python -m unittest discover -s tests -p "test_validation_audit.py"
python -m app.research.wallets check
```

Browser tests are [browser_research.cjs](../tests/browser_research.cjs) and [browser_validation.cjs](../tests/browser_validation.cjs); they require the test harness, Node/Playwright, and a browser runtime. Their fixtures are not production fallback data.

For this documentation review, 146 research tests and six validation-audit tests passed. The documented configuration inspection and offline wallet check also passed; the latter made zero network requests. All 67 local documentation links/anchors checked successfully. Browser suites were not rerun because this review changed documentation only. Passing tests demonstrate tested behavior, not predictive edge, uninterrupted data coverage, provider entitlement, or zero bugs. Direct wallet mainnet end-to-end accuracy remains unverified until configured known transactions are reconciled.

Known limitations that matter before trading:

- Continuous long-only model cannot exploit a falling market by producing short setups.
- No immediate executable entry state; current timing and historical episode state are separate.
- Large observed rejection/low-qualified-sample bottleneck; no demonstrated current 5m candidate edge.
- Small/rotating deep coverage can miss fast moves and leave history gaps.
- No complete, continuously correlated micro/structural/macro wallet model yet.
- No learned smart-money classification, news/sentiment fusion, or calibrated price-move probabilities.
- Reference Binance prices are not guaranteed CoinDCX execution prices.
- No account sizing, liquidation, funding-PnL, portfolio correlation exposure, or execution impact model in continuous replay.
- Current validation is not exact Weighted Hybrid 5m/1m replay.
- Phone delivery and data collection cannot be guaranteed during outages.
- No public-product authentication, tenant isolation, deployment redundancy, or proven commercial data redistribution rights are established here.

## What Must Be Built Next

These are proposed follow-ups, not changes performed by writing this guide:

1. **Make state meanings explicit.** Show direction, public qualification, geometry, pending entry, and simulated/real execution state as distinct fields. An entry-trigger event must not mean an exchange order was filled.
2. **Add a symmetric continuous short model.** Short-side scoring, swing/EMA geometry, downside targets, funding interpretation, replay fills, and outcome tests all need implementation. Relabeling Avoid as Short would be incorrect.
3. **Add the actual 5m/1m validation contract.** Freeze a 5m decision, monitor only subsequent 1m execution evidence, and apply the same chosen strategy/risk rules as the bot. Handle missing data and fees conservatively.
4. **Measure the eligibility funnel.** Report trend, extension, OI/holder/flow exclusions, stop geometry, resistance R:R, score threshold, and final fill separately. Compare parameter variants without overwriting history.
5. **Review target construction on held-out data.** Test whether resistance-capped TP1 with a 1.5R floor is appropriate for the intended scalping workflow. Do not lower stops, scores, or safety checks solely to create more green labels.
6. **Connect and reconcile wallet evidence.** Start with bounded verified identities/assets and provider quotas, then add economic-action decoding and simultaneous decaying timescale features. A transfer alone is not a purchase.
7. **Prove incremental value before integration.** Run prospective shadow/paper evaluation across market regimes, compare against controls and the unchanged strategy, then consider explicitly approved research-to-watchlist integration.

## Glossary

| Term | Meaning in this system |
|---|---|
| ATR | Average true range; a volatility price-distance measure |
| R | Entry-to-stop price distance; 1.5R is reward 1.5 times that risk distance |
| bps | Basis points; 1 bp = 0.01%, so 6 bps = 0.06% |
| OI | Open interest; outstanding contracts, not identified investor direction |
| Public score | Rule-based ranking from available public inputs |
| Confluence | Separate optional-family agreement/weighted evidence score |
| Candidate | Eligible timing plus frozen public score threshold at sampling |
| Setup | One frozen conditional plan with its own lifecycle |
| Replay | Hypothetical price-path simulation, not broker execution |
| MFE / MAE | Maximum favorable/adverse excursion under the evaluator's assumptions |
| Data gap | Missing/invalid evidence prevents checking; not a trade result |
| Finalized | Chain-specific confirmed history accepted by the collector |
| Whale | Size-related hypothesis; not automatically an institution or skilled trader |
| Config ID | Hash identifying the complete frozen research configuration |

For measured explanations of the missing entry assessments, start with [the 30 September assessment audit](RESEARCH_ASSESSMENT_AUDIT_2026-09-30.md). For operational detail see [Research Operations](RESEARCH_OPERATIONS.md); for the future wallet architecture see [Wallet Flow Framework](WALLET_FLOW_FRAMEWORK.md).
