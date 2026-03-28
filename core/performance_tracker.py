"""
SignalEdge — Performance Tracker
==================================
File-backed rolling trade log for dynamic risk management.

Feed trade outcomes via the API:  POST /trades/record
Read system state via:            GET  /status

System modes:
  active      — normal operation
  observation — signals paused when rolling profit factor < 1.2 (last 20 trades)
                or rolling win rate < 40% (last 30 trades)

Risk state:
  risk_mult 1.0 — normal
  risk_mult 0.5 — drawdown > 10% from R-multiple equity peak (active mode only)

Persistence:
  State is stored in $DATA_DIR/performance_tracker.json (default /tmp).
  Set DATA_DIR env var to a mounted volume to survive Railway redeploys.
"""

import json
import os
import threading
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)

_STORE_PATH = Path(os.environ.get("DATA_DIR", "/tmp")) / "performance_tracker.json"
_MAX_TRADES = 100  # rolling cap — enough for all stat windows
_MIN_SAMPLE = 10   # minimum trades before computing win rate / profit factor


class PerformanceTracker:
    """Thread-safe file-backed rolling trade log."""

    def __init__(self, path: Path = _STORE_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ── Private helpers ────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"trades": [], "peak_cumulative_r": 0.0}

    def _flush(self, state: dict) -> None:
        self._path.write_text(json.dumps(state, indent=2))

    # ── Public API ─────────────────────────────────────────────────────────

    def log_trade(self, pair: str, direction: str, r_multiple: float) -> None:
        """Append a closed trade result. Keeps the last _MAX_TRADES entries."""
        with self._lock:
            state = self._load()
            state["trades"].append({
                "pair":       pair,
                "direction":  direction,
                "r_multiple": round(r_multiple, 4),
            })
            state["trades"] = state["trades"][-_MAX_TRADES:]

            # Update the all-time cumulative R peak
            cum_r = sum(t["r_multiple"] for t in state["trades"])
            if cum_r > state.get("peak_cumulative_r", 0.0):
                state["peak_cumulative_r"] = round(cum_r, 4)

            self._flush(state)
            log.info("trade_logged", extra={"pair": pair, "r_multiple": r_multiple})

    def get_system_state(self) -> dict:
        """
        Compute current system health from the rolling trade log.

        Returns:
            {
                "mode":             "active" | "observation",
                "reason":           str | None,
                "win_rate_30":      float | None,   # fraction, e.g. 0.52
                "profit_factor_20": float | None,
                "drawdown_pct":     float,           # negative when in drawdown
                "risk_mult":        1.0 | 0.5,
                "trade_count":      int,
            }
        """
        with self._lock:
            state = self._load()

        trades = state.get("trades", [])
        peak   = state.get("peak_cumulative_r", 0.0)
        n      = len(trades)
        rs     = [t["r_multiple"] for t in trades]

        # ── Rolling 30-trade win rate ──────────────────────────────────────
        last30      = rs[-30:]
        win_rate_30 = None
        if len(last30) >= _MIN_SAMPLE:
            win_rate_30 = sum(1 for r in last30 if r > 0) / len(last30)

        # ── Rolling 20-trade profit factor ────────────────────────────────
        last20 = rs[-20:]
        pf20   = None
        if len(last20) >= _MIN_SAMPLE:
            gross_profit = sum(r for r in last20 if r > 0)
            gross_loss   = abs(sum(r for r in last20 if r < 0))
            pf20 = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None

        # ── Drawdown from peak cumulative R ───────────────────────────────
        cum_r        = sum(rs)
        drawdown_pct = round(((cum_r - peak) / abs(peak) * 100) if peak != 0 else 0.0, 2)

        # ── Evaluation ────────────────────────────────────────────────────
        obs_reasons: list[str] = []

        if win_rate_30 is not None and win_rate_30 < 0.40:
            obs_reasons.append(f"win_rate_30={win_rate_30:.1%}")

        if pf20 is not None and pf20 < 1.2:
            obs_reasons.append(f"profit_factor_20={pf20:.2f}")

        mode      = "observation" if obs_reasons else "active"
        # Drawdown-based risk reduction requires minimum sample to avoid false triggers
        risk_mult = 0.5 if (mode == "active" and n >= _MIN_SAMPLE and drawdown_pct < -10.0) else 1.0

        return {
            "mode":             mode,
            "reason":           ", ".join(obs_reasons) if obs_reasons else None,
            "win_rate_30":      round(win_rate_30, 4) if win_rate_30 is not None else None,
            "profit_factor_20": pf20,
            "drawdown_pct":     drawdown_pct,
            "risk_mult":        risk_mult,
            "trade_count":      n,
        }

    def recent_trades(self, n: int = 20) -> list[dict]:
        with self._lock:
            state = self._load()
        return state.get("trades", [])[-n:]


# Module-level singleton — shared across all requests in the same process.
performance_tracker = PerformanceTracker()
