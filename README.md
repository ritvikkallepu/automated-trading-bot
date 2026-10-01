# CoinDCX Futures Trading Bot

Paper-first automated CoinDCX futures bot.

This version is intentionally conservative:

- Futures only.
- Paper trading remains the default.
- INR-M futures are the default margin mode.
- Live order, cancel, edit, leverage, and margin-type calls are locked unless both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true`.
- REST signing, public futures market data, private read endpoints, websocket market data, candles, indicators, signal-only strategies, paper risk approval, paper execution, historical backtesting, a local monitoring dashboard, config, logging, rate limiting, and tests are included.

## Research Bot Documentation

The separate research subsystem is documented in [Research Bot: Complete Guide](docs/RESEARCH_BOT_README.md), including setup, all configuration groups, scoring, conditional entries/exits, validation, wallet evidence, alerts, and deployment. Its continuous watchlist is currently **long-only research, not live entry approvals**; the separate manual scanner supports long/short research.

For the measured explanation of Waiting for pullback / Too extended / Blocked assessments, see the [30 September 2026 assessment audit](docs/RESEARCH_ASSESSMENT_AUDIT_2026-09-30.md).

## Current Phase

Phase 9 builds the local monitoring dashboard.

Implemented:

- Compact JSON HMAC-SHA256 signing compatible with CoinDCX examples.
- Public futures REST endpoints:
  - Active instruments
  - Instrument details
  - Recent trades
  - Order book snapshot
  - Historical candlesticks
- Private read REST endpoints:
  - Futures wallets
  - Futures positions
  - Futures orders
- Live mutation methods are present but guarded:
  - Place order
  - Cancel order
  - Edit order
  - Update leverage
  - Change margin type
- Standard-library HTTP transport with retries and typed errors.
- CoinDCX futures Socket.IO channel helpers.
- Normalized market events for trades, LTP, candles, orderbook snapshots, and current-prices data.
- In-memory market store for latest prices, latest candles, latest orderbooks, and recent trades.
- Offline websocket replay smoke test.
- Live websocket stream command guarded by an optional Socket.IO dependency.
- Trade-to-candle builder for fixed intervals.
- Candle series helpers for REST and websocket candle events.
- Indicator engine:
  - EMA
  - RSI
  - MACD
  - Bollinger Bands
  - ATR
  - Volume profile
- Modular strategy engine that emits structured signals only.
- Starter futures strategies:
  - EMA/RSI trend-following crossover
  - Bollinger Band squeeze plus volume mean reversion
- Experimental paper strategies:
  - Adaptive hybrid strategy that switches by timeframe
  - Bollinger dynamic futures grid with basket scale-ins and close-confirmed dynamic trail exit
- Paper-first risk manager:
  - Fixed-fraction position sizing
  - Max risk per trade
  - Max daily loss
  - Max open positions
  - Max leverage
  - Stop-loss requirement for entries
  - Optional candle-close trailing stop configuration
  - Take-profit sanity checks
  - Instrument minimum and quantity-step checks
- Paper broker and execution engine:
  - In-memory paper orders
  - Paper fills
  - Open paper positions
  - Realized and unrealized PnL accounting
  - Maker/taker fee and slippage simulation hooks
  - Market-style simulated fills
  - Stop-loss and take-profit candle triggers
  - Optional trailing stop movement after configurable favorable price movement
  - Same-strategy paper scale-ins for approved grid entries
  - Conservative same-candle trigger handling where stop-loss wins if stop and target are both touched
- Backtesting engine:
  - Historical candle loading from CoinDCX public REST
  - Chronological candle replay without lookahead bias
  - Reuse of the same strategy, risk, paper broker, and execution modules
  - Strategy selection for all, EMA/RSI trend, Bollinger/volume reversion, hybrid, adaptive, or dynamic grid
  - Equity curve tracking
  - Closed trade reconstruction
  - Total return, net PnL, win rate, profit factor, max drawdown, and Sharpe ratio metrics
- Local dashboard:
  - Browser UI served by the standard library HTTP server
  - Paper/live safety status
  - Strategy, pair, interval, risk-per-trade, fee, slippage, leverage, and equity controls
  - On-demand historical backtest runs from the browser
  - Final equity, total return, win rate, profit factor, drawdown, and fees
  - Equity curve visualization
  - Recent trades, orders, and fills summary

Strategies do not place orders. Risk approval must happen before paper execution. The paper broker simulates orders and fills locally only; it does not place, edit, or cancel CoinDCX orders.

## Setup

Use Python 3.11 or newer.

```powershell
cd "C:\Users\ritvi\OneDrive\Documents\New project\coindcx-trading-bot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

Install the websocket extra before using live streaming:

```powershell
python -m pip install -e ".[ws]"
```

CoinDCX's docs still mention Socket.IO 2.4 compatibility in some examples. If the stable websocket extra connects but receives no data, reinstall the legacy extra:

```powershell
python -m pip install -e ".[ws-legacy]"
```

Create your local `.env` from the example:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` locally and add your CoinDCX key and secret. Do not paste secrets into chat.

If your `.env` was created before Phase 3, add these non-secret defaults:

```env
COINDCX_WS_URL=wss://stream.coindcx.com
WS_PING_INTERVAL_SECONDS=25
```

## Test

Run the offline unit tests:

```powershell
python -m unittest discover -s tests
```

Run a public API smoke test:

```powershell
python -m app.main public-smoke --pair B-BTC_USDT
```

Run a private read-only smoke test after `.env` is configured:

```powershell
python -m app.main auth-smoke
python -m app.main private-read-smoke
```

The auth smoke test reads basic user info. The private read smoke test reads wallet and position data only. Neither command places, edits, or cancels orders.

Run the offline websocket pipeline smoke test:

```powershell
python -m app.main ws-replay-smoke --pair B-BTC_USDT
```

Run a short live futures websocket stream after installing the websocket extra:

```powershell
python -m app.main market-stream --pair B-BTC_USDT --interval 1m --depth 50 --max-events 10
```

CoinDCX futures websocket channels used in this phase:

- Trades: `B-BTC_USDT@trades-futures`
- LTP: `B-BTC_USDT@prices-futures`
- Candles: `B-BTC_USDT_1m-futures`
- Orderbook: `B-BTC_USDT@orderbook@50-futures`

The bot listens for both CoinDCX orderbook event names, `depth-snapshot` and `depth-update`, because the docs show both forms across market-data examples. Snapshots replace the local book; updates merge into the latest local book and remove levels whose quantity is zero.

Run the indicator smoke test with public CoinDCX futures candles:

```powershell
python -m app.main indicator-smoke --pair B-BTC_USDT --interval 1h --lookback 80
```

This fetches recent candles and prints the latest EMA, RSI, MACD, Bollinger Bands, ATR, and volume profile snapshot.

Run the strategy smoke test with public CoinDCX futures candles:

```powershell
python -m app.main strategy-smoke --pair B-BTC_USDT --interval 1h --lookback 120
```

This prints structured strategy signals. A `hold` signal is normal when no setup is present.

Run the risk smoke test with public CoinDCX futures candles:

```powershell
python -m app.main risk-smoke --pair B-BTC_USDT --interval 1h --lookback 120 --equity 1000 --leverage 1
```

## Dashboard

Start the local monitoring dashboard:

```powershell
python -m app.main dashboard --port 8000
```

Open `http://localhost:8000` in your browser.

The dashboard includes:
- **Paper Trading Mode**: Set configuration, pick pairs, and run simulated trading in real-time.
- **Live Monitor**: A read-only safety panel for observing the bot when it's running in live or live-dry-run mode. 
  - Displays actual mode (Paper / Live Dry-Run / Real Live).
  - Kill Switch panel (enables emergency block and flatten).
  - Pair health and WebSocket silence metrics.
  - Active positions, open orders, and strategy evaluation audits.

### Kill Switch
The kill switch can be enabled via the dashboard's Live Monitor tab. Enabling the kill switch prevents any new entries. Depending on `.env` settings (`LIVE_CLOSE_ON_KILL_SWITCH`), it can also flatten existing positions. Disabling the kill switch requires a manual confirmation typing `DISABLE`.

> **Note:** The dashboard itself does not make the bot trade live. Live trading requires `LIVE_TRADING_ENABLED=true` in `.env` and starting the bot with `python -m app.main live-run`. The dashboard only monitors the state generated by the live loop.

This evaluates the current strategy signals through the paper risk manager and prints structured `APPROVED` or `REJECTED` decisions. A `hold` signal is rejected by risk because it does not request a risked entry.

Run the paper execution smoke test with public CoinDCX futures candles:

```powershell
python -m app.main paper-execution-smoke --pair B-BTC_USDT --interval 1h --lookback 120 --equity 1000 --leverage 1
```

This evaluates current strategy signals, risk-checks them, and sends approved decisions to the in-memory paper broker. A `hold` signal is normal and will not create a fill.

Use the deterministic demo flag to verify the paper fill path even when real strategies are holding:

```powershell
python -m app.main paper-execution-smoke --pair B-BTC_USDT --interval 1h --lookback 120 --equity 1000 --leverage 1 --demo-entry
```

Run a historical paper-only backtest:

```powershell
python -m app.main backtest --pair B-BTC_USDT --interval 1h --lookback 500 --equity 1000 --leverage 1
```

Backtest a single strategy and include fee/slippage assumptions:

```powershell
python -m app.main backtest --pair B-BTC_USDT --interval 1h --lookback 500 --equity 1000 --leverage 1 --strategy ema_rsi_trend --fee-rate 0.0005 --slippage-pct 0.02
```

For CoinDCX INR-M futures, the default rates are maker `0.02%` and taker `0.05%`. A fill pays one of those rates depending on whether it adds liquidity or removes liquidity. The dashboard exposes both rates plus entry/exit fee type selectors.

Use these CLI flags for mixed maker/taker assumptions:

```powershell
--maker-fee-pct 0.02 --taker-fee-pct 0.05 --entry-fee-type maker --exit-fee-type taker
```

Backtest with candle-close trailing stop enabled:

```powershell
python -m app.main backtest --pair B-SOL_USDT --interval 1h --lookback 1000 --equity 1000 --leverage 3 --risk-per-trade-pct 5 --strategy adaptive_hybrid --maker-fee-pct 0.02 --taker-fee-pct 0.05 --entry-fee-type taker --exit-fee-type taker --slippage-pct 0.02 --trailing-stop --trailing-stop-activation-pct 1 --trailing-stop-distance-pct 2
```

Backtest with manual stop-loss and take-profit percentages:

```powershell
python -m app.main backtest --pair B-SOL_USDT --interval 1h --lookback 1000 --equity 1000 --leverage 3 --risk-per-trade-pct 5 --strategy adaptive_hybrid --stop-loss-pct 2 --take-profit-pct 4 --maker-fee-pct 0.02 --taker-fee-pct 0.05 --slippage-pct 0.02
```

Backtest with dynamic ATR exits. The entry gets an ATR-based initial risk boundary, then the stop/target levels are recalculated after each closed candle using the latest ATR:

```powershell
python -m app.main backtest --pair B-SOL_USDT --interval 1h --lookback 1000 --equity 1000 --leverage 3 --risk-per-trade-pct 2 --strategy adaptive_hybrid --atr-dynamic-exits --maker-fee-pct 0.02 --taker-fee-pct 0.05 --slippage-pct 0.02
```

Backtest risk sizing uses the initial equity by default. For example, if equity starts at `1000` and risk is `2%`, every trade sizes from a `20` risk budget even after earlier profits. Use `--compound-risk-equity` only when you intentionally want position size to grow or shrink with account equity.

Backtest the paper-only Bollinger futures grid with maker entries and taker exits:

```powershell
python -m app.main backtest --pair B-SOL_USDT --interval 4h --lookback 1000 --equity 1000 --leverage 3 --risk-per-trade-pct 5 --strategy bb_dynamic_grid --maker-fee-pct 0.02 --taker-fee-pct 0.05 --entry-fee-type maker --exit-fee-type taker --slippage-pct 0.02 --trailing-stop-activation-pct 1 --trailing-stop-distance-pct 2
```

Backtest interval options include `1m`, `5m`, `15m`, `30m`/`30min`, `1h`, `2h`/`2hr`, `4h`, `1d`, and `24h`.

Research sweeps default to the active intraday set: `5m`, `15m`, and `1h`. Use `--intervals` when you want to override that list.

```powershell
python -m app.main research-sweep --intervals 5m,15m,1h --risk-per-trade-pct 1 --atr-dynamic-exits --leverage 3 --output-dir research/backtests/intraday_focus
```

Run the local dashboard:

```powershell
python -m app.main dashboard --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

## Safety Defaults

Risk defaults are stored in config now so every later module reads from one place:

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `FUTURES_MARGIN_CURRENCY=INR`
- `MAX_RISK_PER_TRADE_PCT=5`
- `MAX_DAILY_LOSS_PCT=10`
- `MAX_OPEN_POSITIONS=1`
- `MAX_LEVERAGE=30`
- `TRAILING_STOP_ENABLED=false`
- `TRAILING_STOP_ACTIVATION_PCT=1`
- `TRAILING_STOP_DISTANCE_PCT=2`

Phase 9 execution is still paper-only. The dynamic futures grid is also paper/backtest-only; it is not CoinDCX native grid execution yet. If both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true` are set, the risk manager rejects approvals. The paper broker, backtester, and dashboard never call live CoinDCX mutation endpoints.

Do not enable live trading until the paper broker, strategy engine, risk manager, execution engine, dashboard, alerts, and reconciliation are tested. Live mutations require both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true`.

## Live Deployment Guide

The bot implements a strict, staged rollout process to ensure live trading is not enabled recklessly. Follow these steps carefully:

1. **Configure Environment**
   - Copy `.env.example` to `.env`.
   - Provide your `COINDCX_API_KEY` and `COINDCX_API_SECRET`.

2. **Verify Integrity**
   - Run the test suite: `python -m unittest discover -s tests`
   - Check redacted config: `python -m app.main config-check`
   - Run API connection smoke test: `python -m app.main auth-smoke`

3. **Stage 1: Paper Trading (Simulation)**
   - Start the dashboard (`python -m app.main dashboard`) or run from CLI (`python -m app.main paper`).
   - Monitor the bot's decisions over a 24-hour period.

4. **Stage 2: Live Dry-Run**
   - In `.env`, set `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, and `LIVE_PILOT_DRY_RUN=true`.
   - Run: `python -m app.main live-run --pair B-BTC_USDT --equity 100000 --leverage 3`
   - The bot will fetch real live state but *only log* the orders it would place.

5. **Stage 3: Tiny Capital Pilot**
   - Set `LIVE_PILOT_DRY_RUN=false` and confirm risk awareness: `LIVE_CONFIRM_I_UNDERSTAND_RISK=YES`.
   - Set strict safety caps in `.env`: `LIVE_MAX_ORDER_NOTIONAL=500` (e.g., ₹500 max order).
   - Start the live loop and monitor the first 2-3 live entries/exits to verify fees and slippage on the exchange.

6. **Live Controls & Safety**
   - **Check Status**: `python -m app.main live-status`
   - **Kill Switch (Block Entries)**: `python -m app.main live-kill-switch --enable`
   - **Panic Flatten**: `python -m app.main live-flatten --pair B-BTC_USDT --confirm-flatten YES`

*Review `LIVE_TRADING_CHECKLIST.md` before deploying real capital.*

