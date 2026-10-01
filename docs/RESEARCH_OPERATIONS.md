# Research collection and validation

For the full product/configuration reference and assessment meanings, see the
[Research Bot guide](RESEARCH_BOT_README.md).

The dedicated runtime never loads trading `.env` files, uses public market GETs,
binds to localhost, and rejects non-research POST actions. It cannot enable live
or paper trading. Keep the existing trading process separate.

## Start without the preview harness

From the repository, with its Python environment:

```sh
python -m app.research.continuous.runtime --data-dir data/research_runtime --port 63332
```

Open `http://127.0.0.1:63332/#research`. The supervisor owns a separate worker,
checks it every 15 seconds, restarts failures, and restarts a hung scan after its
configured cycle budget plus 120 seconds of grace (currently 240 + 120 seconds).
It uses a per-directory exclusive
lock and the existing per-database worker lease to avoid duplicate collection.
Logs rotate at 2 MB, with five backups, in `<data-dir>/logs/research.log`.

The dashboard Stop Research action saves an intentional pause. Restarting the
runtime preserves that pause. Start Research resumes collection. Closing the
browser has no effect on a supervised collector.

Windows login task:

```powershell
.\scripts\install_research_task.ps1 -Python 'C:\path\to\python.exe' -DataDirectory 'C:\path\to\research-data'
Get-ScheduledTask -TaskName 'CoinDCX Research Collector'
```

The task runs while that Windows user is logged in. It restarts the supervisor
on failure and starts again at login. It cannot collect while the PC is off,
asleep, disconnected, or signed out. Use the Ubuntu service for continuous
collection while your laptop is unavailable.

Ubuntu, after pulling the tested code and installing dependencies:

```sh
sudo RESEARCH_USER=your_deployment_user bash scripts/install_research_service.sh
systemctl status coindcx-research
journalctl -u coindcx-research -n 50 --no-pager
```

Set `RESEARCH_PYTHON`, `RESEARCH_DATA_DIR`, or `RESEARCH_PORT` when overriding the
defaults. The installer only changes `coindcx-research.service`, not the trading
service. Do not expose the unauthenticated dashboard port publicly. Access a VPS
dashboard through an SSH tunnel, e.g. `ssh -L 63332:127.0.0.1:63332 user@host`.

Use separate data directories on laptop and VPS. Never synchronize an active
SQLite database with OneDrive or run two hosts against the same file. Migrate a
closed database or SQLite backup, including all evidence, when moving hosts.

## Outages and phone alerts

Collection Health persists transitions for process failure, overdue scan,
recovery and intentional pause. A scan is overdue after two configured scan
intervals (10 minutes with the current 5m default). A process restart is not a recovery until a
new publication completes. Disabled phone channels still record local events.

For Telegram, set `alerts.telegram_enabled` in your research config to `true`,
and provide these environment variables to the research supervisor:

```text
RESEARCH_TELEGRAM_BOT_TOKEN=...
RESEARCH_TELEGRAM_CHAT_ID=...
```

Create a bot with Telegram's official BotFather, start a private chat with it,
obtain your chat ID, and enable notifications for that chat on your phone. Keep
tokens out of chat messages, Git, and screenshots. On Ubuntu use the private
`/etc/coindcx-research/alerts.env` file, then restart `coindcx-research`. On Windows
configure user environment variables and restart the scheduled task. A task
does not load variables from a terminal that was opened after it started.

Discord uses `alerts.discord_enabled` and `RESEARCH_DISCORD_WEBHOOK_URL`.
Delivery failures are never marked delivered. Health alerts retry at most once
per five minutes; delivered transitions are deduplicated. Neither an offline
computer nor a dead supervisor can send an alert. OS restart supervision helps
recovery; fully independent host-down alerts require an external monitor.

## Lossless storage

New payloads are compressed only when smaller. The store reads both legacy
plain JSON and versioned compressed JSON. Public evidence exports remain JSON.
Direct SQLite JSON functions no longer work on compressed payloads: use
`HistoryStore`/`decode` or dashboard exports instead.

Stop the scheduled task/service before running offline compaction:

```sh
python -m app.research.continuous.runtime --data-dir data/research_runtime --compact
```

Compaction refuses active workers, takes a new SQLite backup, compresses existing
rows in a transaction, verifies SHA-256 content per table, vacuums free pages,
and checks database integrity. It does not delete observations, samples, setups
or frozen plans. Keep the backup until you have checked the new dashboard.
Backups are not silently deleted; budget disk space for them. History still
grows with new evidence, at a much lower rate.

## Evidence before trading

The current Research Validation page evaluates rankings from closed 5-minute
Binance candles over four hours. It does not evaluate Weighted Hybrid strategy
signals or fills, stops and exits from 1-minute candles. The
price-check verdict must not be used as a trade win rate. To validate that bot
configuration, collect its frozen 5m decisions and complete 1m execution
candles, then replay the same strategy, risk rules, fees and exits on the
matching venue. Older 15m research outcomes remain separate and cannot be
relabelled as 5m outcomes or trades.

Research -> Validation opens an individual prediction ledger, filtered to
qualified ideas under the current settings. Each row shows the frozen call and
its fixed four-hour price test: positive, negative, flat, waiting, awaiting a
check, or cannot verify. Scored-only pairs are comparison observations, not
selected calls; they have a separate filter and do not inflate the headline
count. Missing-data outcomes are excluded from the checked denominator.

View outcome displays the original score, publication and test timestamps,
cost-adjusted start/end prices, and the stored Binance candles in that exact
window. The chart does not substitute current prices or bridge missing candle
intervals. Historical reference prices come from the exact frozen publication,
never the latest scan. Opening the evidence cannot change scores or outcomes.
The net result is a price-direction test, not an actual fill or portfolio PnL;
fees and slippage are included but funding is not. Advanced summaries and
conditional trade simulations are below the ledger and labelled separately.

Validation separates fixed 4-hour ranking samples from conditional setup
replays. Candidate qualification, score and costs are frozen at publication.
Completed, pending and missing-candle samples are shown separately per config.
Score bands include all sampled pairs, not just valid entry setups. Deciles are
ordered by score; tied scores stay together, so groups need not be equal size.
Missing-score legacy samples are excluded from deciles and labelled separately.

The 30-qualified-observation counter uses only completed samples from the
current config. It is a review milestone, not a statistical guarantee or an
automatic permission to trade. Multiple samples may share pairs or market
conditions. Compare means, medians, drawdowns, control outcomes and coverage
before deciding on an independent forward paper trial. No threshold or trading
mode is changed automatically at 30.

CoinDCX listing verification uses its documented public INR catalogue. Each
publication records verification time and count. A failed request yields
unverified rows, never cached listings masquerading as fresh checks. Listing
verification alone does not confirm order size, spread, margin or execution.
