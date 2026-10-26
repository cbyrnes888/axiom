"""Live scanner + trader for your Alpaca PAPER account.

Usage (from this folder):
    python live_trader.py          # runs forever, checks after every new bar
    python live_trader.py --once   # one scan, then quit (good for testing)

It starts in dry-run mode (only prints what it would do). When you're happy,
set dry_run = false in config.toml to place paper orders.

How it decides, after each bar closes:
  - SELL a stock you hold if: close >= MA + exit_std * stdev,
    or it has been held max_bars_held bars (if that rule is on).
    A stop-loss order (if stop_loss_pct > 0) sits at Alpaca the whole time.
  - BUY a stock you don't hold if: the bar touched MA - entry_std * stdev
    (and passed the trend filter), as long as you have fewer than max_positions.
"""
import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from common import (HERE, compute_signals, fetch_alpaca_bars, is_intraday, load_config,
                    load_keys, timeframe_delta)

STATE_FILE = HERE / "bot_state.json"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(HERE / "trader.log", "a") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def lookback_start(st):
    """How far back to download so the moving averages have enough bars."""
    bars_needed = max(st["ma_length"], st.get("trend_filter_length", 0) or 0) * 2 + 10
    step = timeframe_delta(st["timeframe"])
    if is_intraday(st["timeframe"]):
        # ~6.5 trading hours a day, plus weekends/holidays
        days = bars_needed * step.total_seconds() / (6.5 * 3600) * 1.6 + 5
    else:
        days = bars_needed * step.days * 1.6 + 10
    return datetime.now(timezone.utc) - timedelta(days=days)


def completed_bars(df, timeframe):
    """Drop the bar that is still forming, so we only act on finished bars."""
    now = pd.Timestamp.now(tz="UTC")
    return df[df.index + timeframe_delta(timeframe) <= now]


def scan_once(cfg, trading, key, secret, state):
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopOrderRequest

    st, lv = cfg["strategy"], cfg["live"]
    dry = lv.get("dry_run", True)
    tag = "[DRY RUN] " if dry else ""
    symbols = cfg["symbols"]

    bars = fetch_alpaca_bars(symbols, st["timeframe"], lookback_start(st),
                             feed=lv.get("data_feed", "iex"),
                             end=datetime.now(timezone.utc), key=key, secret=secret)
    account = trading.get_account()
    equity = float(account.equity)
    positions = {p.symbol: p for p in trading.get_all_positions()}
    open_orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    orders_by_sym = {}
    for o in open_orders:
        orders_by_sym.setdefault(o.symbol, []).append(o)

    log(f"Scan: equity ${equity:,.0f}, holding {len(positions)} "
        f"({', '.join(positions) or 'nothing'})")

    signals = {}
    for sym in symbols:
        df = bars.get(sym)
        if df is None:
            continue
        df = completed_bars(df, st["timeframe"])
        if len(df) < st["ma_length"] + 1:
            log(f"  {sym}: not enough bars yet ({len(df)})")
            continue
        sig = compute_signals(df, st["ma_length"], st["entry_std"], st["exit_std"],
                              st.get("ma_type", "SMA"), st.get("entry_mode", "touch"),
                              st.get("trend_filter_length", 0))
        signals[sym] = sig

    # Only act once per finished bar (the bot re-checks often, especially on daily bars)
    last_seen = state.setdefault("last_bar", {})
    fresh = {sym: sig for sym, sig in signals.items() if last_seen.get(sym) != str(sig.index[-1])}

    # ---- exits first (frees up slots for new buys)
    sold = set()
    for sym, pos in positions.items():
        if sym not in signals:
            continue
        sig = signals[sym]
        last = sig.iloc[-1]
        reason = None
        if sym in fresh and last["exit"]:
            reason = f"close {last['close']:.2f} >= exit band {last['exit_band']:.2f}"
        max_bars = st.get("max_bars_held", 0)
        entry_time = state["entries"].get(sym)
        if not reason and sym in fresh and max_bars and entry_time:
            held = int((sig.index > pd.Timestamp(entry_time)).sum())
            if held >= max_bars:
                reason = f"held {held} bars (max {max_bars})"
        if reason:
            log(f"  {tag}SELL {sym} ({pos.qty} sh): {reason}")
            if not dry:
                for o in orders_by_sym.get(sym, []):
                    trading.cancel_order_by_id(o.id)  # remove the stop order first
                trading.close_position(sym)
                state["entries"].pop(sym, None)
            sold.add(sym)
            continue
        # make sure a protective stop exists for positions we hold
        stop_pct = st.get("stop_loss_pct", 0)
        has_stop = any(o.side == OrderSide.SELL for o in orders_by_sym.get(sym, []))
        if stop_pct and not has_stop:
            stop_px = round(float(pos.avg_entry_price) * (1 - stop_pct / 100), 2)
            log(f"  {tag}placing stop for {sym} at {stop_px}")
            if not dry:
                trading.submit_order(StopOrderRequest(
                    symbol=sym, qty=abs(int(float(pos.qty))), side=OrderSide.SELL,
                    stop_price=stop_px, time_in_force=TimeInForce.GTC))

    # ---- entries
    slots = lv.get("max_positions", 5) - (len(positions) - len(sold))
    candidates = []
    for sym, sig in fresh.items():
        last = sig.iloc[-1]
        if sym in positions or orders_by_sym.get(sym):
            continue
        if last["entry"]:
            # rank by how far below the band it went (deepest first)
            depth = (last["lower"] - min(last["low"], last["close"])) / last["close"]
            candidates.append((depth, sym, last))
    candidates.sort(key=lambda c: c[0], reverse=True)
    for depth, sym, last in candidates:
        if slots <= 0:
            log(f"  skip BUY {sym}: already at max_positions")
            continue
        price = float(last["close"])
        qty = math.floor(equity * lv.get("position_size_pct", 10) / 100 / price)
        if qty < 1:
            log(f"  skip BUY {sym}: position size too small for price {price:.2f}")
            continue
        log(f"  {tag}BUY {sym} x{qty} (~${qty * price:,.0f}): low {last['low']:.2f} "
            f"touched band {last['lower']:.2f}")
        if not dry:
            trading.submit_order(MarketOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
            state["entries"][sym] = str(signals[sym].index[-1])
        slots -= 1

    if not dry:  # in dry run, keep re-printing so you can watch it think
        for sym, sig in fresh.items():
            last_seen[sym] = str(sig.index[-1])
    if not candidates:
        log("  no buy signals" if fresh else "  no new bars since last scan")
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to a different config file")
    ap.add_argument("--once", action="store_true", help="scan one time and quit")
    args = ap.parse_args()

    from alpaca.trading.client import TradingClient

    cfg = load_config(args.config)
    key, secret = load_keys()
    trading = TradingClient(key, secret, paper=True)  # paper=True: never touches real money
    state = load_state()
    st = cfg["strategy"]
    log(f"Started. {len(cfg['symbols'])} stocks, {st['timeframe']} bars, MA {st['ma_length']}, "
        f"buy at -{st['entry_std']} sd, sell at {st['exit_std']:+} sd, "
        f"dry_run={cfg['live'].get('dry_run', True)}")

    while True:
        try:
            clock = trading.get_clock()
            if clock.is_open or args.once:
                scan_once(cfg, trading, key, secret, state)
            else:
                log(f"Market closed. Next open: {clock.next_open}")
        except Exception as e:  # keep running through network hiccups
            log(f"ERROR: {e!r}")
        if args.once:
            break
        # wait until just after the next bar closes
        step = timeframe_delta(st["timeframe"])
        if is_intraday(st["timeframe"]):
            now = datetime.now(timezone.utc)
            secs = step.total_seconds()
            wait = secs - (now.timestamp() % secs) + 15
        else:
            wait = 30 * 60  # daily bars: check every 30 min; it only acts once per new bar
        time.sleep(min(wait, 30 * 60))


if __name__ == "__main__":
    main()
