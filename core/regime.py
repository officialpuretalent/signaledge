"""
SignalEdge — Market Regime Classifier
======================================
Classifies the current market regime using ADX level and ATR volatility ratio.
Used by signal_engine.get_signals() to attach regime context to each signal and
compute a regime-adjusted risk multiplier.

Requires 'ATR14' and 'ATR20avg' columns in the indicator DataFrame (both added
by signal_engine.add_indicators()).
"""

import pandas as pd

from core.logging import get_logger

log = get_logger(__name__)


def classify_regime(df: pd.DataFrame) -> dict:
    """
    Classify the market regime for the most recent completed bar (iloc[-2]).

    ADX thresholds (fixed, independent of per-pair adx_min):
      >= 25  → 'trending'
      20–25  → 'ranging'
      < 20   → 'high-uncertainty'

    ATR ratio = current ATR14 / 20-bar mean ATR14.
      > 1.5  → extreme volatility; risk halved regardless of ADX state.

    Returns:
        {
            "state":     str,    # 'trending' | 'ranging' | 'high-uncertainty'
            "atr_ratio": float,  # current ATR14 / 20-bar mean
            "risk_mult": float,  # 1.0 (normal) or 0.5 (caution)
        }
    """
    row = df.iloc[-2]

    adx      = float(row["ADX"])
    atr_now  = float(row["ATR14"])
    atr_avg  = float(row["ATR20avg"])

    atr_ratio = round(atr_now / atr_avg, 4) if atr_avg > 0 else 1.0

    if adx >= 25:
        state = "trending"
    elif adx >= 20:
        state = "ranging"
    else:
        state = "high-uncertainty"

    risk_mult = 0.5 if (state in ("ranging", "high-uncertainty") or atr_ratio > 1.5) else 1.0

    log.debug("regime_classified", extra={
        "state": state, "adx": round(adx, 2), "atr_ratio": atr_ratio, "risk_mult": risk_mult,
    })

    return {
        "state":     state,
        "atr_ratio": atr_ratio,
        "risk_mult": risk_mult,
    }
