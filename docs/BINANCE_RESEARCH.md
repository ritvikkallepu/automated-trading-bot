# Binance Research And Shadow Validation

Scope: this document primarily describes the **Manual Scan** and its separate
shadow outcomes. For the current continuous watchlist, wallet pilot, and full
setup/configuration reference, use [Research Bot: Complete Guide](RESEARCH_BOT_README.md).
The continuous planner is not the same bilateral model described below.

## Decision

Use Binance USDT perpetual data for cross-market research, and CoinDCX data for
venue eligibility. Keep the research worker completely separate from execution.
This is an implemented observation-mode baseline, not a predictive AI model or
a validated source of excess returns. No strategy, leverage, margin, position,
cooldown, or watchlist setting is changed by a scan.

Research conducted September 12, 2026. The Binance and GitHub connectors were
checked; Hugging Face model metadata was inspected. The VPS runs the public REST
client directly and does not require these Codex plugins.

## Provider Findings

Binance documents perpetual instrument metadata, OHLCV, funding, open-interest
history and depth endpoints. We filter explicitly by trading status, contract
type, quote and margin asset. The latest candle can still be forming; only fully
closed intervals enter this scanner. OI history has limited retention and is not
a complete historical backtest dataset. Funding intervals can vary by symbol;
missing interval metadata is displayed as unknown, not assumed to be eight hours.
[1](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

CoinDCX exposes active futures instruments and public instrument order books.
We query INR-margin availability and keep quote-price/depth units in USDT.
CoinDCX data redistribution terms require review before turning this personal
dashboard into a product for other traders.
[2](https://docs.coindcx.com/)

FinBERT is an English financial-text sentiment classifier, not a coin-return
predictor. Its published training context does not establish effectiveness for
our crypto universe. This milestone neither installs it nor assigns pretend
sentiment scores when it is absent.
[3](https://huggingface.co/ProsusAI/finbert)

TradingView's charting library requires a supplied data feed. Alpaca crypto data
is not a replacement for the derivatives-specific features needed here. These
remain optional charting/macro sources, not the primary scanner provider.
[4](https://www.tradingview.com/charting-library-docs/latest/connecting_data/)
[5](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)

## Implementation

1. Request exchange time, active instrument catalogues and 24-hour liquidity.
   Match exact base assets only. Ambiguous aliases or contract multipliers are
   excluded. A missing CoinDCX catalogue is not treated as proof of venue
   eligibility. If CoinDCX is unreachable, exact Binance USDT perpetuals
   are still ranked but each row is explicitly marked CoinDCX `unverified`.
2. Select up to the configured number of matching contracts by Binance quote
   volume. The dashboard shows every excluded contract and the exclusion reason.
3. Evaluate 100 fetched candles per interval: 5m, 15m, 1h and 4h. Require 60 closed,
   contiguous observations, valid OHLCV, a positive volume baseline and ATR.
   Identical duplicates are deduplicated; conflicting duplicates are rejected.
4. Reuse the bot's EMA and ATR functions. The volume ratio compares the latest
   closed candle with the preceding 20 candles, excluding that candle itself.
5. Calculate separate long and short scores. Trend alignment contributes up to
   70 points, volume up to 15 and the latest 5m price direction up to 15. Trend
   timeframe weights are 15%, 20%, 30%, 35%. Extension above one ATR reduces the
   score by ten points per ATR, capped at 30. Expensive directional funding
   subtracts ten. Scores are clipped to 0-100, not calibrated probabilities.
6. Require aligned 1h/4h direction, no opposing lower-timeframe trend, adequate
   volume, the selected score threshold and acceptable price extension. Falling
   OI is interpreted alongside hourly price change, not blindly as bearish.
   OI receives no directional score bonus. Missing aligned OI/funding withholds
   candidate status and remains visible as incomplete coverage.
7. Compare time-aligned Binance and CoinDCX midpoints. Check CoinDCX spread and
   visible directional book depth within ten basis points. These are snapshots,
   not fill guarantees or execution approvals. Revalidate at order time in any
   future integration. Venue checks are reported independently as `verified`,
   `blocked`, or `unverified`; they do not rewrite the Binance ranking. Only a
   `verified` check has passed the current public listing/book filters, and even
   that is not an order approval. Current execution is untouched.
8. The normal scan limit is 20 liquidity-ranked contracts and can be set from 1
   to 50. Raising the limit increases public API calls and scan duration. It does
   not force weak pairs into the long or short buckets; low-quality rows remain
   visible as `avoid`.

The defaults are transparent engineering starting points, not optimized trading
parameters. Numeric filters are editable in the Research tab and recorded in each
snapshot. Filters are independent of the bot's actual live risk configuration.

## Operation

Start the existing dashboard and select **Research**, then **Scan Markets**.
No scan runs merely from opening the page. Refreshing status does not call the
exchange again. A single background worker handles scans with an eight-second
HTTP timeout, a four-minute network budget, paced requests and a one-minute
minimum interval between scan starts. HTTP 418/429 stops the scan without retries.
Cancellation affects research only. Slow research never runs in the trading loop.

Completed snapshots are written atomically to `data/research_scans/<scan_id>.json`
and excluded from Git. Export Snapshot downloads the same evidence, settings and
timestamps. No old snapshot is automatically restored as current on a restart.
During refresh/failure, the previous completed scan is explicitly identified.
Snapshots older than five minutes are marked stale. Archive the JSON directories
separately and monitor disk use during long observation campaigns.

Each completed scan is also eligible for forward-only shadow evaluation. The
evaluator freezes the original direction, score, bucket, venue status and cost
assumption. Its assumed entry is the open of the next complete Binance one-minute
candle after the scan finished, never a candle available to the scanner. It then
records immutable 15-minute, one-hour and four-hour outcomes after each horizon
has fully elapsed. Sidecars are written atomically to
`data/research_outcomes/<scan_id>.json`; the original scan is never modified.

For every scored direction, including `avoid` rows used as a baseline, the
dashboard reports directional return less the frozen round-trip cost, hit rate,
median and average net return, maximum favourable excursion (MFE), and maximum
adverse excursion (MAE). Candidate, long, short, CoinDCX-verified, all-scored and
avoided groups remain separate. These are Binance mark-to-market observations,
not simulated CoinDCX fills. The default cost assumption is 0.20% and is editable
before a scan; it is stored in that scan and cannot be retroactively changed.

The outcome worker checks once per minute and processes at most two due scans per
cycle to keep research load bounded. **Check Due Outcomes** wakes it early. A scan
first contributes after 15 minutes, then again after one hour and four hours.
The UI says `collecting` until 200 unique candidate signals have outcomes. That is
a review checkpoint, not proof of predictive value and not permission to tune on
the same sample.

The existing dashboard includes trading controls on other tabs. Keep it on
localhost or behind an authenticated SSH tunnel, not an exposed public port.
No API keys or new paid services are needed for this milestone.

## Validation And Limits

Unit/integration tests cover directional symmetry, cross-pair isolation,
closed-candle handling, incomplete data, timestamp checks, rate limits, bounded
settings, persistence, cancellation and HTTP wiring. Browser checks exercise
the UI with explicitly synthetic test fixtures and separately exercise real
provider failure states. A fake fixture is never a runtime fallback.

Implementation verification includes focused tests for next-minute entries,
long/short return symmetry, horizon maturity, missing one-minute candles,
immutable outcomes, separate baselines, persistence, and bounded background
processing. Playwright also covers the outcome panel and its sample status in
addition to scan/filter/search/evidence/export, tab isolation, nonblank chart
pixels, mobile overflow and stale/error states.

The direct Binance public API worked from this machine during implementation.
CoinDCX public catalogue and depth requests returned HTTP 403 here. Binance-only
rankings therefore work locally with CoinDCX marked `unverified`; a verified
end-to-end cross-venue candidate scan cannot be produced on this network. This is
not evidence that the VPS has the same restriction. No VPS deployment or live
trading test was performed.

Visible book depth is only a lower-bound observation at a moment in time, not
total market liquidity. Exact-name matching deliberately misses some valid
alias/multiplier contracts. Cross-venue instrument semantics still need ongoing
verification. A four-minute scan is not simultaneous across every coin. Scores
are not entry instructions or a replacement for strategy-specific setups.

## Next Milestones

1. Confirm both providers on the deployment network, then collect untouched
   shadow snapshots across rising, falling and sideways markets. Do not alter
   thresholds while building the first evaluation sample.
2. At the review checkpoint, lock a chronological training period and judge the
   unchanged scanner on a later held-out period. Add confidence intervals,
   turnover and regime slices before claiming improvement over the baseline.
3. Add licensed news with source URL, publication/fetch timestamps, entity
   matching, deduplication and prompt-injection handling. Evaluate FinBERT against
   hand-labelled crypto news before using sentiment as an optional feature.
4. Add a schema-validated, source-linked AI research brief. Keep it explanatory
   initially, with explicit uncertainty and unsupported-claim rejection. Measure
   value above the deterministic baseline and monitor provider cost/latency.
5. Only after evidence supports it, consider human-approved watchlist proposals.
   Preserve deterministic exposure, stop-loss and kill-switch checks. Any wider
   public product also needs authentication, tenant isolation and data licences.

## Uploaded Project Comparison

The supplied `AI_Investment_Command_Center_Friends` source inspired the separation
of market scoring, news research and an explanatory interface. In the reviewed
`app/agents.py` and `app/recommendations.py`, fallback scores and heuristic return
projections were not evidence of predictive accuracy. This implementation does
not copy that code or its fallback outputs. The CoinDCX bot keeps its own tested
execution and risk components.

## Sources

- [1: Binance Futures REST market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [2: CoinDCX API and market-data terms](https://docs.coindcx.com/)
- [3: ProsusAI FinBERT model card](https://huggingface.co/ProsusAI/finbert)
- [4: TradingView data-feed documentation](https://www.tradingview.com/charting-library-docs/latest/connecting_data/)
- [5: Alpaca crypto data documentation](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)
- Local sources: `app/dashboard/server.py`, `app/data/indicators.py`,
  `app/exchange/coindcx_rest.py`, and the uploaded command-center source files.
