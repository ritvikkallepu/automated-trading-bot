# Why Research Has Not Shown Enter Long or Enter Short

Read-only local audit captured **30 September 2026 at 05:17:44 IST**. No scoring thresholds, strategy code, runtime configuration, positions, or stored results were changed for this audit.

## Conclusion

The complaint is valid, but three different issues are being combined:

1. **Capability mismatch:** continuous research is long-only and has no Enter Long/Enter Short UI labels. The separate manual scanner supports long/short research.
2. **State mismatch:** a current pullback assessment is not the historical setup's simulated entry state. The watchlist can say Waiting for pullback while Setup History contains a prior simulated fill.
3. **Qualification bottleneck:** under the current 5m settings, none of the inspected observations passed both eligible timing and public score >=65. Nearby resistance and the required TP1 reward/risk reject most rows.

It would be inaccurate to explain this only as "the bot is very selective" or "you need whale API keys." Some requested functionality has not been implemented, and the public-data settings are producing almost no qualified candidates.

## Evidence Scope

| Item | Value |
|---|---|
| Database | `C:\TradingBots\coindcx-research-data\research\research.sqlite3` |
| Access | SQLite `mode=ro`; one consistent read transaction |
| Latest included publication ID | `d935a7c3099e42df880c19b14eaca7c0` |
| Latest included publication | 30 September 2026, 05:16:52.209 IST |
| Current configuration ID | `f5105613c4557832` |
| All retained publications | 339, from 15 September 2026, 21:41:41.575 IST |
| Current 5m configuration publications | 67, from 29 September 2026, 22:43:27.729 IST |

This is the local research database, not the VPS trade log or CoinDCX account history. Collection has gaps; the date range does not imply uninterrupted monitoring. Counts below are frozen at this audit cutoff and will not automatically refresh as the scanner continues.

Rows from repeated scans of one pair are **observations**, not unique coins, trades, or independent predictions. Only rows with an actual `public_rank` were counted as scored observations. Unscanned rows were excluded from these denominators.

## Current 5m Results

| Measurement | Count |
|---|---:|
| Scans | 67 |
| Scored pair-observations | 6,631 |
| Waiting / forming | 104 (1.57%) |
| Too extended | 73 (1.10%) |
| Blocked | 6,454 (97.33%) |
| Breakout confirmed | 0 |
| Short-direction research rows | 0 |
| Valid price geometries before classification/timing | 111 |
| Eligible timing after other classification checks | 104 |
| Public score >=65, regardless of eligibility | 2 |
| Eligible timing AND public score >=65 | **0** |
| Highest public score among eligible rows | **54.56** |
| High Conviction rows | 0 |

Both observations scoring at least 65 failed timing eligibility. The 104 eligible rows were below the score threshold. This is not an API fetch failure explanation: the last included scan completed 100 analyses with zero market fetch failures, although all 100 plans in that particular scan were blocked.

### Rejection Reasons

These are non-exclusive: one observation can have several reasons. Percentages use all 6,631 scored observations, so they do not sum to 100%.

| Plan rejection reason | Observations | Share of scored observations |
|---|---:|---:|
| Nearest resistance leaves TP1 below minimum reward/risk | 5,981 | 90.20% |
| Closed 5m trend does not support a long plan | 5,302 | 79.96% |
| Pullback entry would be above the current reference price | 3,633 | 54.79% |
| More than 2.5 ATR above EMA21 | 73 | 1.10% |

The extension reason is stored as "Price is extended from EMA; late-chase filter." It is one gate, not two independent failures.

## Why the Labels Cannot Show an Immediate Entry

In [levels.py](../app/research/continuous/levels.py), `trigger_confirmed` is true only for a **breakout** plan whose closed reference price exceeds resistance. The current configuration uses `pullback`, with no per-pair overrides. All 31,210 scored observations in the retained history used pullback plans.

In [ranking.py](../app/research/continuous/ranking.py), valid non-avoided plans with no confirmed breakout are `forming`. In [continuous.js](../app/dashboard/static/continuous.js), forming pullback plans are displayed as **Waiting for pullback**. This mapping does not inspect whether a previous frozen setup filled.

The current scanner calculates a new present-tense EMA pullback idea each cycle. The historical setup deliberately keeps its original EMA entry. These are different objects and can produce different-looking states without either being a real order.

There is no continuous short geometry/replay path. [service.py](../app/research/continuous/service.py) assigns analyzed rows `long` or `avoid`; [setups.py](../app/research/continuous/setups.py) samples the long direction. Avoid is not an implicit short signal. Manual Scan uses a different bilateral ranking implementation in [market_scanner.py](../app/research/market_scanner.py).

## Why the R:R Gate Dominates

The current settings combine:

```text
TP1 nominal target = entry + 1.5R
TP1 actual target = nearer of nominal target and overhead resistance
minimum acceptable TP1 reward/risk = 1.5R
```

Any resistance nearer than the nominal 1.5R target makes the plan fail. There is no tolerance for taking an earlier TP1 and relying on TP2/TP3 for aggregate reward. That is how the code is written, not a bug in displaying the configured value.

The bullish trend requirement also requires `close > EMA21 > EMA55` with a rising EMA55. Falling or non-aligned markets fail this long-only filter instead of being assessed for shorts. During this 5m sample, the overlap of that requirement, resistance geometry, and score >=65 left no qualified candidates.

This short sample does not prove that loosening either rule improves returns. It does justify a measured eligibility/geometry review rather than waiting indefinitely without explaining the rejected stages.

## Did Any Pullback Plans Fill in Simulation?

**Yes.** For the current 5m configuration:

| Setup replay measure | Count |
|---|---:|
| Distinct frozen setups | 65 |
| Simulated entries observed | 26 |
| Entered and closed | 17 |
| Open simulated entries | 9 |
| Closed by stop loss | 15 |
| Closed by trailing stop | 2 |
| Invalidated before entry | 31 |
| Unresolved data-gap setups | 8 |
| Entered setups with original frozen score >= their threshold | **0** |

These 26 entries were below-threshold geometries being observed, not 26 approved trading calls. Across all retained configurations, 795 distinct setups had replay records and 266 had a simulated entry. Two additional legacy scan-only replay records were excluded from the distinct-setup counts.

Example: the B-BR_USDT setup published on 30 September at 04:21:54.331 IST had frozen score **11.96**, entry approximately **0.82162342**, and a simulated entry candle at **04:25 IST**. At the audit cutoff it was open in replay. That is evidence of the simulation processing a pullback, not an approved BR trade or a CoinDCX fill.

An open replay can show only entry fees/realized partial PnL. Its displayed net result is not necessarily current unrealized profit. Do not interpret the example's open status as a recommendation.

## Qualification and Validation Are Sparse

Across all retained configurations, eight repeated scan rows had eligible timing and public score >=65. They were associated with BR, 1INCH, CETUS, BABY, and 0G. All preceded the current 5m configuration. They are not eight independent validated trades.

Only three qualified four-hour validation samples existed across the retained history: two complete and one with missing data. Non-overlapping per-pair sampling and repeated scans explain why row counts and sample counts differ. This is far too little qualified evidence to claim reliable accuracy.

For the current 5m configuration, there were 959 comparison observations: 475 complete, 476 pending, and eight with data gaps. **There were zero qualified candidate samples.** The large comparison count does not substitute for evidence on selected ideas.

## Missing Optional Feeds: What They Do and Do Not Explain

CoinGlass and Bubblemaps were disabled and mappings empty. High Conviction requires available whale accumulation-proxy evidence, verified holder coverage within risk limits, bullish OI, valid geometry, and composite >=70. Those conditions cannot all be satisfied in the current configuration.

However, the public Watch ranking is independent of optional feeds. Eligible public-data plans can still be recorded and evaluated. API activation alone will not add short support, create an Enter Long label, repair reward/risk geometry, or validate the current 5m model.

Wallet Evidence is a separate finalized-transfer collector and is not yet connected to the public ranking or optional confluence score. Its tab being present does not mean whale intelligence is active.

## Recommended Work, Not Applied Changes

1. Separate direction, setup validity, score qualification, entry-trigger state, and execution state in the UI.
2. Add properly tested continuous short scoring/plans/replay rather than translating Avoid into Short.
3. Add a frozen 5m decision and subsequent 1m entry/exit monitor matching the intended trading workflow.
4. Expose the rejection funnel so the largest blockers are visible without a database audit.
5. Compare risk/reward/target variants on held-out and prospective data, preserving the current baseline.
6. Evaluate real wallet evidence before allowing it to change rankings or trading decisions.

No threshold was lowered and no new trades were enabled by this audit. The complete operating and configuration guide is [Research Bot: Complete Guide](RESEARCH_BOT_README.md).
