# Std-Dev Mean Reversion Bot (Alpaca paper trading + TradingView)

**The idea:** when a stock drops unusually far below its average price (e.g. 2 standard
deviations below), buy it and bet that it bounces back. Sell when it gets back above a
higher band (e.g. average + 1 standard deviation).

| File | What it is |
|---|---|
| `config.toml` | **All the settings.** Open it in Notepad/TextEdit and change numbers. |
| `backtest.py` | Tests many settings, stocks and timeframes on past data. |
| `live_trader.py` | Watches your stock list and trades your Alpaca **paper** account. |
| `std_dev_mean_reversion.pine` | The same strategy for TradingView (Pine Script v6). |
| `common.py` | Shared math. You don't need to touch it. |

## One-time setup (about 10 minutes)

1. Install **Python 3.11 or newer** from https://www.python.org/downloads/
   (on Windows, tick **"Add Python to PATH"** during install).
2. Put this folder somewhere on your computer, then open a terminal in it:
   - Windows: open the folder, click the address bar, type `cmd`, press Enter.
   - Mac: right-click the folder → *New Terminal at Folder*.
3. Install the libraries:
   ```
   pip install -r requirements.txt
   ```
4. Get your Alpaca **paper** API keys: https://app.alpaca.markets → make sure you're on
   *Paper Trading* → *API Keys* → *Generate*.
5. Copy `.env.example` to a new file named `.env` and paste the keys in.

## Backtesting

```
python backtest.py
```
This tries every combination listed under `[backtest.grid]` in `config.toml`, on every stock in
`symbols`, and prints the best settings **averaged across all your stocks**. Full results go
into the `results/` folder (open the CSVs in Excel or Google Sheets).

Other handy runs:
```
python backtest.py --single                             # just your [strategy] settings, saves every trade
python backtest.py --timeframes 15Min 1Hour 1Day        # compare timeframes
python backtest.py --symbols TSLA AMD COIN              # try other stocks
```

**How to read the results**
- `avg_return_pct` vs `avg_buy_hold_pct` — did it beat simply buying and holding?
- `avg_max_dd_pct` — the worst drop from a peak. Ask yourself if you could stomach it.
- `avg_out_of_sample_pct` — results on the last 30% of the data, which acts as a
  "fresh" test. If a setting looks great overall but bad here, it's probably luck.
- `pct_stocks_profitable` — good settings work on most stocks, not just one.

Intraday timeframes (5Min, 15Min, 1Hour) download much more data; start with a few stocks
or a later `start` date.

## Paper trading

```
python live_trader.py --once     # one scan, prints what it would do
python live_trader.py            # keeps running; checks after every new bar
```
It starts with `dry_run = true` (prints only). When it looks right, set `dry_run = false`
in `config.toml` to place real **paper** orders. The code always connects to the paper
account, so it can't touch real money. Leave the terminal window open while it runs
(and stop your computer from going to sleep). Press `Ctrl+C` to stop. Everything it does
is written to `trader.log`.

## TradingView

1. Open a chart → **Pine Editor** (bottom of screen) → paste the whole contents of
   `std_dev_mean_reversion.pine` → **Add to chart**.
2. Open **Strategy Tester** (bottom tab) to see trades, win rate and profit.
3. Click the ⚙ next to the strategy name to change length, std devs, stop loss, etc.
4. Change the chart's timeframe (e.g. 15m, 1h, 1D) to test other timeframes.

The Python backtester and the Pine script use the same rules (signal on the bar's close,
fill on the next bar's open), so results should be close. Small differences come from data
providers and how TradingView handles slippage.
