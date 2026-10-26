"""Backtest the strategy over many settings, stocks and timeframes.

Usage (from this folder):
    python backtest.py                 # run the whole grid from config.toml
    python backtest.py --single        # just the settings in [strategy]
    python backtest.py --symbols TSLA AMD --timeframes 15Min 1Hour

Results are saved to the results/ folder as CSV files you can open in Excel
or Google Sheets.
"""
import argparse
import itertools
import time
from pathlib import Path

import numpy as np
import pandas as pd

from common import (HERE, compute_bands, compute_signals, fetch_alpaca_bars,
                    fetch_yahoo_bars, load_config)

CACHE = HERE / "data_cache"
RESULTS = HERE / "results"


# ------------------------------------------------------------------ data
def get_data(symbols, timeframe, bt):
    """Load bars, using a local cache so repeat runs are fast."""
    CACHE.mkdir(exist_ok=True)
    start, end = bt["start"], bt.get("end") or None
    data, missing = {}, []
    for sym in symbols:
        f = CACHE / f"{bt['data_source']}_{sym}_{timeframe}_{start}_{end or 'now'}.csv"
        if f.exists() and time.time() - f.stat().st_mtime < 12 * 3600:
            data[sym] = pd.read_csv(f, index_col=0, parse_dates=True)
        else:
            missing.append((sym, f))
    if missing:
        syms = [s for s, _ in missing]
        print(f"  downloading {timeframe} bars for {', '.join(syms)} ...")
        if bt["data_source"] == "yahoo":
            fresh = {s: fetch_yahoo_bars(s, timeframe, start, end) for s in syms}
        else:
            fresh = {}
            for i in range(0, len(syms), 20):  # a few symbols per request
                fresh.update(fetch_alpaca_bars(syms[i:i + 20], timeframe, start, end,
                                               feed=bt.get("data_feed", "sip")))
        for sym, f in missing:
            df = fresh.get(sym)
            if df is None or df.empty:
                print(f"  ! no data for {sym} on {timeframe}, skipping")
                continue
            df.to_csv(f)
            data[sym] = df
    return data


# -------------------------------------------------------------- simulator
def simulate(sig, slippage_pct=0.05, stop_loss_pct=0, max_bars_held=0):
    """Walk through the bars one at a time, like a trader would.

    - Signal on a bar's close -> buy/sell at the NEXT bar's open.
    - Stop loss (if on) is checked during each bar after the entry bar.
    Returns (list of trades, equity curve as a numpy array).
    """
    o, h, l, c = (sig[k].to_numpy() for k in ("open", "high", "low", "close"))
    entry_sig, exit_sig = sig["entry"].to_numpy(), sig["exit"].to_numpy()
    n = len(sig)
    slip = slippage_pct / 100
    trades, equity = [], np.ones(n)
    cash, shares = 1.0, 0.0
    in_pos = pending_buy = pending_sell = False
    entry_i = entry_px = stop_px = 0

    def close_trade(i, px, reason):
        nonlocal cash, shares, in_pos
        px *= 1 - slip
        cash = shares * px
        trades.append(dict(entry_time=sig.index[entry_i], exit_time=sig.index[i],
                           entry_price=entry_px, exit_price=px, bars_held=i - entry_i,
                           ret=px / entry_px - 1, reason=reason, entry_idx=entry_i))
        shares, in_pos = 0.0, False

    for i in range(n):
        # 1) fill orders placed on the previous bar, at this bar's open
        if pending_sell:
            close_trade(i, o[i], pending_sell)
            pending_sell = False
        if pending_buy:
            entry_px = o[i] * (1 + slip)
            shares, cash, in_pos, entry_i = cash / entry_px, 0.0, True, i
            stop_px = entry_px * (1 - stop_loss_pct / 100) if stop_loss_pct else 0
            pending_buy = False

        if in_pos:
            # 2) stop loss hit during this bar? (gap down = filled at the open)
            if stop_px and i > entry_i and l[i] <= stop_px:
                close_trade(i, min(o[i], stop_px), "stop")
            # 3) exit rules, checked at the close -> sell next open
            elif exit_sig[i]:
                pending_sell = "exit band"
            elif max_bars_held and i - entry_i >= max_bars_held:
                pending_sell = "max bars"
        elif entry_sig[i] and i < n - 1:
            pending_buy = True

        equity[i] = cash + shares * c[i]

    if in_pos:  # still holding on the last bar: mark it closed at the last price
        close_trade(n - 1, c[-1] / (1 - slip), "end of data")
        equity[-1] = cash
    return trades, equity


def metrics(trades, equity, sig, split_idx):
    rets = np.array([t["ret"] for t in trades])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    peak = np.maximum.accumulate(equity)
    in_mkt = sum(t["bars_held"] + 1 for t in trades)
    ins = [t["ret"] for t in trades if t["entry_idx"] < split_idx]
    oos = [t["ret"] for t in trades if t["entry_idx"] >= split_idx]
    c = sig["close"].to_numpy()
    return dict(
        trades=len(trades),
        win_rate=round(100 * len(wins) / len(rets), 1) if len(rets) else np.nan,
        avg_trade_pct=round(100 * rets.mean(), 2) if len(rets) else np.nan,
        total_return_pct=round(100 * (equity[-1] - 1), 1),
        buy_hold_pct=round(100 * (c[-1] / c[0] - 1), 1),
        max_drawdown_pct=round(100 * ((equity - peak) / peak).min(), 1),
        profit_factor=round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else np.nan,
        time_in_market_pct=round(100 * in_mkt / len(sig), 1),
        in_sample_return_pct=round(100 * (np.prod(1 + np.array(ins)) - 1), 1),
        out_of_sample_return_pct=round(100 * (np.prod(1 + np.array(oos)) - 1), 1),
        oos_trades=len(oos),
    )


# ------------------------------------------------------------------ runner
def run(cfg, symbols, grid, single=False):
    st, bt = cfg["strategy"], cfg["backtest"]
    oos_frac = bt.get("out_of_sample_pct", 30) / 100
    rows, all_trades = [], []
    for tf in grid["timeframe"]:
        print(f"\nTimeframe {tf}")
        data = get_data(symbols, tf, bt)
        for sym, df in data.items():
            split_idx = int(len(df) * (1 - oos_frac))
            for length in grid["ma_length"]:
                basis, dev = compute_bands(df["close"], length, st.get("ma_type", "SMA"))
                for e_std, x_std in itertools.product(grid["entry_std"], grid["exit_std"]):
                    sig = compute_signals(df, length, e_std, x_std, st.get("ma_type", "SMA"),
                                          st.get("entry_mode", "touch"),
                                          st.get("trend_filter_length", 0), basis, dev)
                    trades, eq = simulate(sig, bt.get("slippage_pct", 0.05),
                                          st.get("stop_loss_pct", 0), st.get("max_bars_held", 0))
                    params = dict(symbol=sym, timeframe=tf, ma_length=length,
                                  entry_std=e_std, exit_std=x_std)
                    rows.append({**params, **metrics(trades, eq, sig, split_idx),
                                 "bars": len(df), "first_bar": df.index[0], "last_bar": df.index[-1]})
                    if single:
                        all_trades += [{**params, **t} for t in trades]
            print(f"  {sym}: {len(df)} bars tested")
    return pd.DataFrame(rows), pd.DataFrame(all_trades)


def summarize(detail):
    """Average each setting across all the stocks, so one lucky stock doesn't fool you."""
    keys = ["timeframe", "ma_length", "entry_std", "exit_std"]
    g = detail.groupby(keys)
    s = pd.DataFrame({
        "stocks": g.size(),
        "total_trades": g["trades"].sum(),
        "avg_win_rate": g["win_rate"].mean().round(1),
        "avg_trade_pct": g["avg_trade_pct"].mean().round(2),
        "avg_return_pct": g["total_return_pct"].mean().round(1),
        "median_return_pct": g["total_return_pct"].median().round(1),
        "avg_buy_hold_pct": g["buy_hold_pct"].mean().round(1),
        "avg_max_dd_pct": g["max_drawdown_pct"].mean().round(1),
        "avg_time_in_mkt_pct": g["time_in_market_pct"].mean().round(1),
        "avg_in_sample_pct": g["in_sample_return_pct"].mean().round(1),
        "avg_out_of_sample_pct": g["out_of_sample_return_pct"].mean().round(1),
        "pct_stocks_profitable": g["total_return_pct"].apply(lambda x: round(100 * (x > 0).mean())),
    }).reset_index()
    return s.sort_values("avg_return_pct", ascending=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to a different config file")
    ap.add_argument("--single", action="store_true", help="only test the [strategy] settings, and save every trade")
    ap.add_argument("--symbols", nargs="+", help="override the stock list")
    ap.add_argument("--timeframes", nargs="+", help="override the timeframes, e.g. 15Min 1Hour 1Day")
    args = ap.parse_args()

    cfg = load_config(args.config)
    st = cfg["strategy"]
    symbols = args.symbols or cfg["symbols"]
    if args.single:
        grid = dict(timeframe=[st["timeframe"]], ma_length=[st["ma_length"]],
                    entry_std=[st["entry_std"]], exit_std=[st["exit_std"]])
    else:
        grid = dict(cfg["backtest"]["grid"])
    if args.timeframes:
        grid["timeframe"] = args.timeframes
    n_combo = np.prod([len(v) for v in grid.values()]) * len(symbols)
    print(f"Testing {n_combo} combinations ({len(symbols)} stocks)...")

    detail, trades = run(cfg, symbols, grid, args.single)
    if detail.empty:
        raise SystemExit("No data was downloaded - check your symbols, dates and keys.")
    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    summary = summarize(detail)
    detail.to_csv(RESULTS / f"detail_{stamp}.csv", index=False)
    summary.to_csv(RESULTS / f"summary_{stamp}.csv", index=False)
    if args.single and not trades.empty:
        trades.drop(columns="entry_idx").to_csv(RESULTS / f"trades_{stamp}.csv", index=False)

    pd.set_option("display.width", 220, "display.max_columns", 30)
    cols = ["timeframe", "ma_length", "entry_std", "exit_std", "total_trades", "avg_win_rate",
            "avg_trade_pct", "avg_return_pct", "avg_buy_hold_pct", "avg_max_dd_pct",
            "avg_out_of_sample_pct", "pct_stocks_profitable"]
    print("\nTop settings (averaged across all stocks):")
    print(summary[cols].head(15).to_string(index=False))
    print(f"\nFull results saved in {RESULTS}")
    print("Tip: trust settings that do well on MOST stocks AND in the out-of-sample column,"
          " not just the single best number.")


if __name__ == "__main__":
    main()
