# Continuous Research Watchlist

Start with the consolidated [Research Bot guide](RESEARCH_BOT_README.md). For measured rejection counts and the missing entry-label explanation, see the [assessment audit](RESEARCH_ASSESSMENT_AUDIT_2026-09-30.md). This continuous planner is long-only; Manual Scan is a separate bilateral model.

This read-only module is separate from live/paper execution. It never places an
order, starts a trading loop, changes stops, or supplies live entry approvals.
Existing on-demand research and shadow outcomes remain available below it.

## What works without paid keys

- The dashboard starts a background research worker when `autostart` is true.
  Start/Stop Research only controls that worker.
- All active Binance USDT-margined perpetuals and the CoinDCX INR futures list
  are enumerated. Unmapped CoinDCX instruments remain visible as unscanned.
- Cheap liquidity screening covers the entire catalogue. By default the top 50
  liquid pairs plus 50 rotating eligible pairs get deeper analysis per cycle.
  Rotation prefers the least recently checked pairs; excluded/queued rows have
  reasons and are never presented as current recommendations. Set
  `deep_pairs_per_cycle` to the eligible universe size for exhaustive deep scans,
  only if the API budget permits. Time-budget misses remain explicit.
- Public Binance closed 5m candles, four-hour contract-OI changes and funding
  are collected. Price/OI endpoints use aligned timestamps. Increasing OI does
  **not** prove new longs: the states are positioning interpretations.
- Configured sector peers are compared using synchronized return correlation.
  Catch-up targets use relative returns applied to the laggard's own price,
  never another asset's nominal price. Untagged assets stay unclassified.
- Missing whale/holder evidence is unavailable, not an invented neutral reading.
  Scores are not renormalized. With only the default OI family available, the
  **confluence** score ceiling is 40/100 and coverage is 40%; this is intentional.
  Such pairs remain Watch, not High Conviction. Scores are not probabilities.
- A separate `public-v1` long ranking now differentiates public-data opportunities.
  Its contributions are relative volume (25%), OI buildup/acceleration (20%),
  EMA trend strength (20%), recent range compression (15%) and proximity to
  breakout resistance (20%). Expensive positive funding is penalized; excessive
  EMA extension earns no proximity contribution. Missing OI is not reweighted.
  One-hour OI acceleration compares two consecutive equal-length hours; absent
  intermediate points earn no acceleration points. All inputs and contributions
  are available in Details. These are configurable heuristics, not trained AI.
- Ordering is classification, timing eligibility, public score, then pair name
  for deterministic ties. Forming/trigger-confirmed opportunities precede
  overextended/invalidated ones within the same classification. Rank changes
  compare only pairs common to adjacent scans under the same configuration;
  rotating a coin into the scan cannot by itself manufacture a rank jump.

## Configuration and startup

Use this repository as an editable installation (`pip install -e .`). All
weights, schedules, thresholds, ATR/risk settings, target fractions, sector tags
and per-pair trigger overrides are in `config/research.json`. Invalid settings
stop the research cycle and display an error; trading settings are untouched.
The file is reloaded at each cycle. Export `RESEARCH_CONFIG_PATH` for a separate
machine-local config, or supply `--config` to the standalone worker.

Windows or Ubuntu, from the repository with its virtual environment active:

```text
python -m app.main dashboard --host 127.0.0.1 --port 8000
```

The Research tab has continuous status, full coverage, filters, plans, evidence
buttons, export and replay summaries. On the VPS keep the dashboard on localhost
and use your existing SSH tunnel. Do not expose the trading dashboard publicly.

A separate headless worker is also available:

```text
python -m app.research.continuous --database data/research/research.sqlite3
```

Run one research owner per database. A SQLite lease prevents overlapping scans
from a headless worker and dashboard using the same file. Separate laptop/VPS
databases are independent; neither sync nor shared research history is implied.
No VPS deployment, Git commit or push is performed by installing this module.

Defaults: market cycles approximately every 5 minutes; whale cache 1 hour;
holder cache 6 hours. Workers pace requests, retry transient failures with
exponential backoff, and persist provider Retry-After cooldowns across restarts.
A stopped/disconnected dashboard or failed cycle marks displayed history stale.

## Optional provider access

Only exact asset mappings are accepted. No symbol guessing or public embed
scraping. Credentials belong in environment variables, **not JSON, source code,
screenshots or chat**. The standalone worker reads exported process environment,
not the trading bot's private env files. Configure systemd `EnvironmentFile` or
your shell privately if required. Research snapshots do not include keys.

### CoinGlass

Set `providers.coinglass_enabled` to true and privately configure
`COINGLASS_API_KEY`. Map each supported symbol in `assets`, for example:

```json
{"BTCUSDT": {"coinglass_symbol": "BTC", "coinglass_chain": "bitcoin"}}
```

The connector uses v4 `/api/chain/v2/whale-transfer` with `CG-API-KEY`.
The API is **not free merely because the website is accessible**. Official docs
say this endpoint requires Startup or above and returns transfers of at least
$10 million. The default requested threshold is $500,000, so its evidence is
explicitly marked limited at that setting. To research only the much larger
transfer cohort, deliberately set `thresholds.whale_usd` to at least $10 million;
that changes the research question and must not be mistaken for $500k coverage.
Transfers are deduplicated by chain/transaction hash; only exact configured
exchange labels vote directionally. Exchange-to-exchange and unlabelled transfers
do not count as bullish/bearish flow. Positive **outflow** is an accumulation
proxy; exchange inflow is not labelled accumulation. This does not prove buys.
Source coverage is limited to returned transactions, not verified whole-chain
completeness. The connector's `FlowFeed` protocol permits a different provider.

- https://docs.coinglass.com/reference/whale-transfer
- https://docs.coinglass.com/reference/authentication
- https://www.coinglass.com/pricing

### Bubblemaps

Set `providers.bubblemaps_enabled` and privately configure `BUBBLEMAPS_API_KEY`.
Each asset mapping needs `holder_chain`, `holder_address` and explicit
`holder_share_unit` (`fraction` or `percent`, confirmed against your returned
schema). Never guess a token contract from a ticker. The connector calls
`/v0/tokens/map/{chain}/{token_address}` with `X-ApiKey`, requesting nodes/clusters.

Top-holder clusters, custodial shares and entity labels are stored. The risk
filter compares the largest non-custodial cluster/linked entity with the
configured threshold. Cluster links are not proof of shared ownership. Changes
in stable cluster membership show changes in supply share, **not proven net
buying/selling**. No concentration score is fabricated for native assets lacking
a supported map. Keys/subscriptions were unavailable during development:
these authenticated adapters are fixture-tested, not live-account verified.

- https://docs.bubblemaps.io/data/api/authentication
- https://github.com/bubblemaps/api-docs/blob/main/openapi.json

V1 selects Binance-only reference OI from the brief's open options. Bybit/OKX
aggregation and Whale Alert/Arkham/Nansen adapters are not implemented. A separate
[direct-chain evidence pilot](WALLET_EVIDENCE_PILOT.md) now exists, but is not yet
connected to these scores. CoinGlass is an optional whale feed, not an OI replacement.

## Scoring and plans

Default weighted contributions: whale 40%, OI 40%, holder safety 20%. Holder safety
never votes bullish. High Conviction requires bullish OI, sufficient whale
accumulation coverage, available non-excluded holder data, the minimum composite
score and a valid TP1 reward/risk ratio. A score alone cannot override these gates.

Plans are hypothetical **Binance reference-price long plans**, not executable
CoinDCX orders. CoinDCX listing alone does not verify price, depth or fills.
The existing manual scanner retains its separate public execution checks.

- Pullback default: future EMA21 limit touch in a confirmed closed-candle trend.
- Breakout option: closed 5m confirmation above recent resistance; next opening
  price must still satisfy the anti-chase and actual-fill R:R checks in replay.
- Stop: tighter eligible swing-low/1.5 ATR stop, with at least 1 ATR breathing room.
- TP1: 1.5R or closer overhead resistance. If closer resistance means R:R < 1.5,
  the plan is blocked. This resolves the brief's contradictory 1R/1.5R requirement.
- TP2: 2R. TP3: 3R or a further relative catch-up stretch target.
- Fractions: 50/30/20. After TP2, the remainder trails at 2 ATR, updated at closed
  candles and applied only to the following candle. Every level has a basis/time.
- Whale distribution or bearish OI is separately stated as signal invalidation.
  Price replay does not claim to simulate discretionary signal-flip exits.

## Evidence and replay

The dashboard separates Current Watchlist, Setup History, Validation and Manual
Scan. The current watchlist is the last atomically completed publication, not a
live price feed. It shows the scan cadence (5 minutes by default), completion
time, next scan, browser connection time and each pair's closed 5m candle time
in IST. The browser polls status every five seconds; this does not run a new scan.
Busy/queued scans retain the previous completed publication with an update-pending
label. Stopped, failed, disconnected or overdue results are explicitly historical.
An unchanged publication does not replace table rows or reset selected evidence.

Current assessment and public ranking are separate from historical setup state.
Blocked/extended/unscanned rows have no eligible entry or targets in the main
table; calculated rejected levels remain in evidence. Original historical levels
stay frozen in Setup History. Optional whale/holder coverage is explicit, not
represented as fully available. Manual scans are on demand, never scheduled by
the status poll. Their cooldown indicates when the button becomes available,
not the time of an automatic scan. No trade execution settings are changed.

`data/research/research.sqlite3` persists immutable raw observations, config
fingerprints, universe snapshots and published plans. Replay results and worker
state are separate mutable records. Runtime data is Git-ignored. Export Evidence
exports the publication with observation IDs; raw input buttons retrieve each
underlying record. Back up the SQLite database using SQLite's backup facility
while running, or copy the database together with WAL files only when stopped.
History is retained, so monitor disk use on a long-running VPS.

Each eligible pair gets a persistent setup ID with original publication time,
configuration, evidence and frozen entry/stop/targets. Later scans append events
and update the latest assessment; they never move the original replay levels or
restart its expiry. Pair histories are independent. A completed/invalidated setup
needs a later invalid assessment followed by a new valid closed candle to re-arm.
Changing configuration does not fork an unresolved setup. Queued/unscanned coins
are not cancelled, and missing candles remain unresolved data gaps, not resets.
Invalidation cancels an unfilled plan only when replay has no unresolved data
gap. Already-entered plans continue their frozen price exits, not hindsight exits.

Setup History has pair search, pagination, original levels, outcomes and full
event evidence. Export History includes all setups, events and ranking validation
results. Previous scan-only replays are preserved but excluded from distinct-setup
metrics; old snapshots are not retroactively relabelled as prospective setups.

The dashboard checks frozen setups against stored forward candles each cycle.
Offline replay checks all tracked episodes, including older unresolved ones:

```text
python -m app.research.continuous --replay --database data/research/research.sqlite3
```

Replay ignores candles straddling publication, uses only closed post-publication
bars, rejects data gaps, handles gap stops and fees/slippage, and assumes stops
first when candle order is ambiguous. Pullback-fill bars cannot also claim their
possibly pre-entry high as a target. Timeout uses the last fully closed bar before
expiry. No historical data from before collection is invented. A missing sample
is pending/data-gap, not a successful trade. Empty metrics remain empty.

Results group by frozen config, public-score threshold and tier. They show
TP1/2/3 hits, stops, unfilled/pending/data-gap setups, net returns and conservative
MFE/MAE estimates. The fill bar's potentially pre-entry high is excluded; stop
bars do not earn favourable excursions, and adverse movement stops at the modeled
exit. These are lower-resolution OHLC estimates, not tick-precise measurements.
Do not sum percentages as portfolio PnL. Simultaneous pairs remain correlated.

Ranking Validation independently records ALL analysed pairs, including invalid
plans, at non-overlapping four-hour windows per pair. A candidate requires valid
timing and public score >= `ranking.candidate_score` (default 65). Starting at the
next full 5m open, each sample measures a four-hour hold with identical configured
fees/slippage. Candidate, below-threshold/invalid controls and all-sample results
show counts, mean returns, excursions and candidate-minus-control differences.
Pending/missing data stay explicit; completed results are not rewritten. This is
a prospective observational comparison, not random assignment or causal proof.
Rotating deep-scan coverage and market regimes affect the sample. Funding is not
modeled. Collect results across regimes before tuning thresholds; a small positive
difference is not evidence of reliable profitability. No subscription is required.

## Optional phone notifications

Qualified public-data setups can now alert without whale/holder keys. Only
meaningful setup changes (creation, threshold crossing, trigger, open, completed
or invalidation) are eligible; unchanged scans are silent. Each setup/event/channel
is delivered at most once after confirmed success, and new opportunities respect
the configured pair/channel cooldown. Lifecycle changes are not hidden by that
cooldown. Delivery occurs after a completed research cycle, not tick-by-tick.
Recent Setup Changes is available without external notifications. Nothing is sent
to a phone by default. Telegram: export `RESEARCH_TELEGRAM_BOT_TOKEN` and
`RESEARCH_TELEGRAM_CHAT_ID`, start your Telegram bot conversation, then set
`alerts.telegram_enabled`. Discord: privately export
`RESEARCH_DISCORD_WEBHOOK_URL`, then set `alerts.discord_enabled`. These are
research-plan alerts, not replacements for existing portfolio/SL notifications.
Provider failures are recorded without tokens/webhook URLs. No paid provider or
real notification was contacted in the tests.

For the isolated persistent dashboard, Windows login task, Ubuntu service,
outage monitoring and lossless database migration, see
[Research Operations](RESEARCH_OPERATIONS.md). The Validation view now includes
score bands, tied-score deciles and the current configuration's 30-observation
review milestone. None of these features enable trading.
