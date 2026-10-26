"""Shared pieces: reading settings, downloading price bars, and the band math.

Both backtest.py and live_trader.py use this file, so the backtest and the
live bot always calculate signals exactly the same way.
"""
import os
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- settings
def load_config(path=None):
    path = Path(path) if path else HERE / "config.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_keys():
    """Read the Alpaca keys from .env (or from environment variables)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret or "your_paper" in key:
        raise SystemExit(
            "Alpaca keys not found. Copy .env.example to .env and paste your PAPER keys into it."
        )
    return key, secret


# --------------------------------------------------------------- timeframes
_TF_RE = re.compile(r"^(\d+)\s*(Min|Hour|Day|Week)$", re.IGNORECASE)


def parse_timeframe(tf):
    """'15Min' -> (15, 'Min'). Raises a friendly error for typos."""
    m = _TF_RE.match(tf.strip())
    if not m:
        raise SystemExit(f"Don't understand timeframe '{tf}'. Use e.g. 5Min, 1Hour, 1Day.")
    return int(m.group(1)), m.group(2).capitalize()


def timeframe_delta(tf):
    n, unit = parse_timeframe(tf)
    return {"Min": timedelta(minutes=n), "Hour": timedelta(hours=n),
            "Day": timedelta(days=n), "Week": timedelta(weeks=n)}[unit]


def is_intraday(tf):
    return parse_timeframe(tf)[1] in ("Min", "Hour")


def alpaca_timeframe(tf):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    n, unit = parse_timeframe(tf)
    units = {"Min": TimeFrameUnit.Minute, "Hour": TimeFrameUnit.Hour,
             "Day": TimeFrameUnit.Day, "Week": TimeFrameUnit.Week}
    return TimeFrame(n, units[unit])


# --------------------------------------------------------------- price data
def _clean(df):
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df[~df.index.duplicated()].sort_index()
    return df.dropna()


def regular_hours_only(df):
    """Drop pre-market and after-hours bars (intraday timeframes only)."""
    ny = df.index.tz_convert("America/New_York")
    mask = (ny.time >= pd.Timestamp("09:30").time()) & (ny.time < pd.Timestamp("16:00").time())
    return df[mask]


def fetch_alpaca_bars(symbols, timeframe, start, end=None, feed="sip", key=None, secret=None):
    """Download bars for several symbols. Returns {symbol: DataFrame}."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest

    if key is None:
        key, secret = load_keys()
    client = StockHistoricalDataClient(key, secret)
    if end is None:
        # Free accounts can't request the most recent 15 minutes of SIP data.
        end = datetime.now(timezone.utc) - timedelta(minutes=16)
    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=alpaca_timeframe(timeframe),
        start=pd.Timestamp(start, tz="UTC").to_pydatetime() if isinstance(start, str) else start,
        end=pd.Timestamp(end, tz="UTC").to_pydatetime() if isinstance(end, str) else end,
        feed=DataFeed.SIP if feed.lower() == "sip" else DataFeed.IEX,
        adjustment=Adjustment.ALL,  # adjust for splits and dividends
    )
    raw = client.get_stock_bars(req).df
    out = {}
    if raw.empty:
        return out
    for sym in symbols:
        if sym in raw.index.get_level_values(0):
            df = _clean(raw.xs(sym, level=0))
            if is_intraday(timeframe):
                df = regular_hours_only(df)
            out[sym] = df
    return out


def fetch_yahoo_bars(symbol, timeframe, start, end=None):
    import yfinance as yf
    n, unit = parse_timeframe(timeframe)
    interval = {"Min": f"{n}m", "Hour": f"{n}h", "Day": f"{n}d", "Week": f"{n}wk"}[unit]
    df = yf.download(symbol, start=start, end=end or None, interval=interval,
                     auto_adjust=True, progress=False, multi_level_index=False)
    if df.empty:
        return df
    df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return _clean(df)


# ------------------------------------------------------------- the strategy
def compute_bands(close, length, ma_type="SMA"):
    """Moving average and standard deviation (same formula as TradingView)."""
    if ma_type.upper() == "EMA":
        basis = close.ewm(span=length, adjust=False).mean()
    else:
        basis = close.rolling(length).mean()
    # ddof=0 matches TradingView's ta.stdev
    dev = close.rolling(length).std(ddof=0)
    return basis, dev


def compute_signals(df, ma_length, entry_std, exit_std, ma_type="SMA",
                    entry_mode="touch", trend_filter_length=0, basis=None, dev=None):
    """Adds the bands plus 'entry' and 'exit' True/False columns to a copy of df.

    entry: this bar touched/closed below  MA - entry_std * stdev  (and passed the trend filter)
    exit:  this bar closed at or above   MA + exit_std  * stdev
    Orders are filled at the NEXT bar's open, like TradingView does by default.
    """
    out = df.copy()
    if basis is None or dev is None:
        basis, dev = compute_bands(df["close"], ma_length, ma_type)
    out["basis"] = basis
    out["lower"] = basis - entry_std * dev
    out["exit_band"] = basis + exit_std * dev
    trigger = out["low"] if entry_mode == "touch" else out["close"]
    entry = trigger <= out["lower"]
    if trend_filter_length and trend_filter_length > 0:
        trend = df["close"].rolling(trend_filter_length).mean()
        out["trend_ma"] = trend
        entry &= df["close"] > trend
    out["entry"] = entry.fillna(False).astype(bool)
    out["exit"] = (out["close"] >= out["exit_band"]).fillna(False).astype(bool)
    return out
