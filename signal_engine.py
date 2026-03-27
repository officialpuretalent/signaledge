"""
SignalEdge — Signal Engine
==========================
Fetches live 1H bars, computes indicators, and evaluates optimized entry
conditions for each active pair.

Pairs and parameters are derived from a 2-year grid-search backtest.
Each pair carries its own ADX floor, DI-spread minimum, SL multiplier,
and R:R ratio — do not change these without re-running the optimizer.

Run standalone:   python signal_engine.py
Called by API:    from signal_engine import get_signals
"""

import json
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

SAST = ZoneInfo("Africa/Johannesburg")

import numpy as np
import pandas as pd
import yfinance as yf

from core.logging import get_logger
from core.deduplication import deduplicator

log = get_logger(__name__)


# ── Active pairs + optimized parameters ───────────────────────────────────────
# (ticker, pair_name, strategy, adx_min, di_spread_min, sl_mult, rr_ratio)
#
# Parameters come from backtest_optimized.py grid search over 2 years of 1H data.
# Re-run the optimizer quarterly or after 50+ live trades to stay calibrated.
#
#                                                adx  di   sl    rr    OptPF  OptDD
LIVE_PAIRS = [
    ("EURUSD=X", "EUR/USD", "ema_adx_enhanced", 35,  0,  2.5, 3.5),  # 2.72   7.7%
    ("USDJPY=X", "USD/JPY", "rsi_momentum",     35,  5,  2.5, 2.0),  # 1.83  11.3%
    ("AUDJPY=X", "AUD/JPY", "ema_cross",        20,  5,  2.5, 2.5),  # 1.31  20.4%
    ("USDCAD=X", "USD/CAD", "ema_cross",        35,  0,  2.5, 2.5),  # 1.11   9.9%
]


# ── Data fetch ────────────────────────────────────────────────────────────────
def _clean(raw: pd.DataFrame) -> pd.DataFrame:
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.loc[:, ~raw.columns.duplicated()]
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC").tz_convert("Africa/Johannesburg").tz_localize(None)
    else:
        raw.index = raw.index.tz_convert("Africa/Johannesburg").tz_localize(None)
    return raw[["Open", "High", "Low", "Close", "Volume"]].dropna()


def fetch(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (df_1h, df_daily) both in SAST, tz-naive."""
    df_1h = _clean(yf.download(ticker, period="60d", interval="1h",
                               auto_adjust=True, progress=False))
    df_1d = _clean(yf.download(ticker, period="2y",  interval="1d",
                               auto_adjust=True, progress=False))
    df_1h = df_1h[df_1h.index.dayofweek < 5]
    df_1d = df_1d[df_1d.index.dayofweek < 5]
    return df_1h, df_1d


def _fetch_pair_data(pair_config: tuple) -> tuple[str, tuple | Exception]:
    """Fetch data for one pair. Returns (ticker, (df_1h, df_1d)) or (ticker, Exception)."""
    ticker = pair_config[0]
    try:
        return ticker, fetch(ticker)
    except Exception as exc:
        return ticker, exc


# ── Indicators ────────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame, df_daily: pd.DataFrame,
                   price_scale: float = 1.0) -> pd.DataFrame:
    d = df.copy()

    d["SMA50"]  = d["Close"].rolling(50).mean()
    d["SMA200"] = d["Close"].rolling(200).mean()

    delta    = d["Close"].diff()
    gain     = delta.clip(lower=0).rolling(14).mean()
    loss     = (-delta.clip(upper=0)).rolling(14).mean()
    d["RSI"] = 100 - (100 / (1 + gain / loss))

    tr = pd.concat([
        d["High"] - d["Low"],
        (d["High"] - d["Close"].shift(1)).abs(),
        (d["Low"]  - d["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    d["ATR14"] = tr.rolling(14).mean()

    d["SMA50_slope"] = d["SMA50"].rolling(10).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True
    )

    daily_sma200 = df_daily["Close"].squeeze().rolling(200).mean()
    d["SMA200D"] = daily_sma200.reindex(d.index, method="ffill")

    d["EMA9"]  = d["Close"].ewm(span=9,  adjust=False).mean()
    d["EMA21"] = d["Close"].ewm(span=21, adjust=False).mean()
    d["DCH"]   = d["High"].rolling(20).max()
    d["DCL"]   = d["Low"].rolling(20).min()

    hi, lo      = d["High"], d["Low"]
    dm_plus     = (hi - hi.shift(1)).clip(lower=0)
    dm_minus    = (lo.shift(1) - lo).clip(lower=0)
    dm_plus     = dm_plus.where(dm_plus > dm_minus, 0)
    dm_minus    = dm_minus.where(dm_minus > dm_plus, 0)
    atr14       = tr.ewm(span=14, adjust=False).mean()
    d["DIP"]    = dm_plus.ewm(span=14,  adjust=False).mean() / atr14 * 100
    d["DIM"]    = dm_minus.ewm(span=14, adjust=False).mean() / atr14 * 100
    dx          = (abs(d["DIP"] - d["DIM"]) / (d["DIP"] + d["DIM"]) * 100).fillna(0)
    d["ADX"]    = dx.ewm(span=14, adjust=False).mean()

    return d.dropna()


# ── Signal logic ──────────────────────────────────────────────────────────────
def check_signal(strategy: str, row: pd.Series, prev: pd.Series,
                 price_scale: float = 1.0,
                 adx_min: float = 25,
                 di_spread_min: float = 0) -> str | None:
    """
    Returns 'long', 'short', or None.

    adx_min       — minimum ADX at the signal bar (filters weak trends)
    di_spread_min — minimum |DI+ − DI-| (filters directionally ambiguous bars)
    """
    di_spread = abs(float(row["DIP"]) - float(row["DIM"]))

    if strategy == "ema_adx_enhanced":
        if row.name.dayofweek == 3: return None          # skip Thursday
        if row["ADX"] < adx_min:   return None
        if di_spread  < di_spread_min: return None
        up = prev["EMA9"] < prev["EMA21"] and row["EMA9"] >= row["EMA21"]
        dn = prev["EMA9"] > prev["EMA21"] and row["EMA9"] <= row["EMA21"]
        if up and row["Close"] > row["SMA50"] and row["DIP"] > row["DIM"]: return "long"
        if dn and row["Close"] < row["SMA50"] and row["DIM"] > row["DIP"]: return "short"

    elif strategy == "rsi_momentum":
        if row.name.dayofweek == 3: return None          # skip Thursday
        if row["ADX"] < adx_min:   return None
        if di_spread  < di_spread_min: return None
        h = row.name.hour
        if not ((9 <= h <= 18) or (15 <= h <= 23)): return None   # London / NY
        up = prev["RSI"] < 50 <= row["RSI"]
        dn = prev["RSI"] > 50 >= row["RSI"]
        if up and row["Close"] > row["SMA50"] and row["DIP"] > row["DIM"]: return "long"
        if dn and row["Close"] < row["SMA50"] and row["DIM"] > row["DIP"]: return "short"

    elif strategy == "ema_cross":
        if row["ADX"] < adx_min:   return None
        if di_spread  < di_spread_min: return None
        up = prev["EMA9"] < prev["EMA21"] and row["EMA9"] >= row["EMA21"]
        dn = prev["EMA9"] > prev["EMA21"] and row["EMA9"] <= row["EMA21"]
        if up and row["Close"] > row["SMA50"]: return "long"
        if dn and row["Close"] < row["SMA50"]: return "short"

    elif strategy == "rsi_sma_refined":
        if row["ADX"] < adx_min:   return None
        if di_spread  < di_spread_min: return None
        up = prev["RSI"] < 50 <= row["RSI"]
        dn = prev["RSI"] > 50 >= row["RSI"]
        if not (up or dn): return None
        h = row.name.hour
        if not ((9 <= h <= 18) or (15 <= h <= 23)): return None
        slope_thresh = 0.000003 * price_scale
        if up and row["Close"] > row["SMA50"]:
            if row["Close"] <= row["SMA200D"]: return None
            if row["SMA50_slope"] <= slope_thresh: return None
            return "long"
        if dn and row["Close"] < row["SMA50"]:
            if row["Close"] >= row["SMA200D"]: return None
            if row["SMA50_slope"] >= -slope_thresh: return None
            return "short"

    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def get_signals(pairs: list | None = None) -> list[dict]:
    """
    Evaluate all active pairs and return a list of signal dicts.

    pairs — optional override of LIVE_PAIRS.
            Each entry: (ticker, pair_name, strategy, adx_min, di_spread_min, sl_mult, rr_ratio)
    """
    if pairs is None:
        pairs = LIVE_PAIRS

    now = dt.datetime.now(SAST).strftime("%Y-%m-%d %H:%M:%S")
    log.info("fetching_pairs", extra={"count": len(pairs)})

    # ── Phase 1: fetch all pairs concurrently ─────────────────────────────────
    fetch_results: dict[str, tuple | Exception] = {}
    with ThreadPoolExecutor(max_workers=min(len(pairs), 8)) as pool:
        for ticker, result in pool.map(_fetch_pair_data, pairs):
            fetch_results[ticker] = result

    # ── Phase 2: compute indicators + evaluate signals (serial) ───────────────
    signals = []
    for ticker, pair_name, strategy, adx_min, di_spread_min, sl_mult, rr_ratio in pairs:
        result = fetch_results.get(ticker)

        if isinstance(result, Exception):
            log.error("fetch_failed", extra={"pair": pair_name, "error": str(result)})
            signals.append({
                "pair": pair_name, "ticker": ticker,
                "strategy": strategy, "signal": "error",
                "error": str(result), "generated": now,
            })
            continue

        try:
            df_1h, df_1d = result

            if len(df_1h) < 60:
                log.warning("insufficient_bars", extra={"pair": pair_name, "bars": len(df_1h)})
                continue

            price_scale = float(df_1h["Close"].mean()) / 1.10
            df          = add_indicators(df_1h, df_1d, price_scale)

            if len(df) < 3:
                continue

            # Use the last two COMPLETED bars (-1 may be in-progress mid-hour)
            row  = df.iloc[-2]
            prev = df.iloc[-3]

            signal   = check_signal(strategy, row, prev, price_scale, adx_min, di_spread_min)
            bar_time = str(row.name)[:16]
            atr      = float(row["ATR14"])
            entry    = float(row["Close"])

            if signal == "long":
                sl = entry - atr * sl_mult
                tp = entry + atr * sl_mult * rr_ratio
            elif signal == "short":
                sl = entry + atr * sl_mult
                tp = entry - atr * sl_mult * rr_ratio
            else:
                sl = tp = None

            # Deduplication — mark as duplicate if this bar+signal was already alerted
            is_duplicate = (
                signal is not None
                and not deduplicator.is_new(pair_name, signal, bar_time)
            )

            result_dict = {
                "pair":     pair_name,
                "ticker":   ticker,
                "strategy": strategy,
                "signal":   "duplicate" if is_duplicate else (signal or "none"),
                "entry":    round(entry, 5) if signal else None,
                "sl":       round(sl, 5)    if signal else None,
                "tp":       round(tp, 5)    if signal else None,
                "atr":      round(atr, 5),
                "params": {
                    "adx_min":       adx_min,
                    "di_spread_min": di_spread_min,
                    "sl_mult":       sl_mult,
                    "rr_ratio":      rr_ratio,
                },
                "indicators": {
                    "rsi":         round(float(row["RSI"]),         2),
                    "adx":         round(float(row["ADX"]),         2),
                    "dip":         round(float(row["DIP"]),         2),
                    "dim":         round(float(row["DIM"]),         2),
                    "di_spread":   round(abs(float(row["DIP"]) - float(row["DIM"])), 2),
                    "ema9":        round(float(row["EMA9"]),        5),
                    "ema21":       round(float(row["EMA21"]),       5),
                    "sma50":       round(float(row["SMA50"]),       5),
                    "sma50_slope": round(float(row["SMA50_slope"]), 8),
                    "close":       round(entry,                     5),
                },
                "bar_time":  bar_time,
                "generated": now,
            }
            signals.append(result_dict)

            log.info("signal_evaluated", extra={
                "pair":     pair_name,
                "strategy": strategy,
                "signal":   result_dict["signal"],
                "adx":      result_dict["indicators"]["adx"],
            })

        except Exception as exc:
            log.error("signal_error", extra={"pair": pair_name, "error": str(exc)}, exc_info=True)
            signals.append({
                "pair": pair_name, "ticker": ticker,
                "strategy": strategy, "signal": "error",
                "error": str(exc), "generated": now,
            })

    return signals


# ── Standalone run ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from core.logging import setup_logging
    setup_logging()

    log.info("starting", extra={"time": dt.datetime.now(SAST).strftime("%Y-%m-%d %H:%M")})
    results = get_signals()

    active = [s for s in results if s["signal"] not in ("none", "error", "duplicate")]
    log.info("complete", extra={"active": len(active), "total": len(results)})

    for s in active:
        p = s["params"]
        print(f"\n  ★ {s['pair']} — {s['signal'].upper()}")
        print(f"    Strategy : {s['strategy']}")
        print(f"    Entry    : {s['entry']}")
        print(f"    SL       : {s['sl']}  ({p['sl_mult']}×ATR)")
        print(f"    TP       : {s['tp']}  ({p['rr_ratio']}R)")
        print(f"    ADX      : {s['indicators']['adx']}  (min {p['adx_min']})")
        print(f"    Bar      : {s['bar_time']} SAST")

    out = "signals_latest.json"
    with open(out, "w") as f:
        json.dump({
            "generated": results[0]["generated"] if results else "",
            "signals":   results,
        }, f, indent=2)
    print(f"\nSaved → {out}")
