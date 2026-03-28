"""
SignalEdge — Signal API
=======================
Thin FastAPI wrapper around the signal engine and AI client.

Run:  uvicorn signal_api:app --host 0.0.0.0 --port 8000

Endpoints:
  GET  /health              — liveness check (provider, model, pair count)
  GET  /signals             — all pairs (active + inactive + errors)
  GET  /signals/active      — only pairs with a live signal
  GET  /signals/{pair}      — single pair  (e.g. /signals/EUR-USD)
  POST /signals/ai          — new signals only (deduped) + AI confidence scoring

Signal deduplication:
  POST /signals/ai returns only signals that have NOT been sent in the current
  process lifetime. Duplicate signals (same pair + direction + bar) are silently
  filtered so n8n never sends repeated alerts for the same trade setup.

Error visibility:
  Data feed failures (yfinance errors) are surfaced in the `errors` field of
  POST /signals/ai so n8n can route them to a Telegram alert node.
"""

import datetime as dt
from zoneinfo import ZoneInfo

SAST = ZoneInfo("Africa/Johannesburg")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from signal_engine import get_signals, LIVE_PAIRS
from ai import score_signal, AI_PROVIDER, AI_MODEL
from core.logging import get_logger
from core.performance_tracker import performance_tracker

log = get_logger(__name__)

app = FastAPI(title="SignalEdge API", version="2.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status":      "ok",
        "ai_provider": AI_PROVIDER,
        "ai_model":    AI_MODEL,
        "pairs":       len(LIVE_PAIRS),
        "time":        dt.datetime.now(SAST).isoformat(),
    }


@app.get("/signals")
def signals_all():
    results = get_signals()
    return {
        "generated": dt.datetime.now(SAST).isoformat(),
        "count":     len(results),
        "signals":   results,
    }


@app.get("/signals/active")
def signals_active():
    results = get_signals()
    active  = [s for s in results if s["signal"] not in ("none", "error", "duplicate")]
    return {
        "generated":    dt.datetime.now(SAST).isoformat(),
        "active_count": len(active),
        "signals":      active,
    }


@app.get("/signals/{pair_slug}")
def signal_single(pair_slug: str):
    """pair_slug: EUR-USD  →  EUR/USD"""
    pair_name = pair_slug.replace("-", "/").upper()
    target    = [p for p in LIVE_PAIRS if p[1] == pair_name]
    if not target:
        raise HTTPException(404, f"Pair '{pair_name}' not in LIVE_PAIRS")
    results = get_signals(pairs=target)
    return results[0] if results else {}


class TradeRecord(BaseModel):
    pair:       str
    direction:  str   # 'long' | 'short'
    r_multiple: float
    risk_pct:   float = 0.0  # informational; stored for context


@app.post("/signals/ai")
def signals_with_ai():
    """
    Return only NEW (non-duplicate) active signals, each enriched with AI scoring.
    Also surfaces any data feed errors in the `errors` field for n8n routing.

    Signals with AI confidence < 70 are moved to `signals_gated` (not sent to n8n).
    If the performance tracker is in observation mode, `signals` is empty and
    `system_mode` will be "observation" so n8n can alert accordingly.

    Set AI_PROVIDER = anthropic | openai | gemini
    """
    # Forex market is closed on weekends — skip signal evaluation entirely
    now_sast = dt.datetime.now(SAST)
    if now_sast.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        log.info("weekend_skip", extra={"day": now_sast.strftime("%A")})
        return {
            "generated":       now_sast.isoformat(),
            "ai_provider":     AI_PROVIDER,
            "ai_model":        AI_MODEL,
            "system_mode":     "weekend",
            "active_count":    0,
            "signals":         [],
            "signals_gated":   [],
            "market_snapshot": [],
            "errors":          [],
        }

    results = get_signals()

    # Surface data feed errors so n8n can alert on them
    errors = [s for s in results if s["signal"] == "error"]
    if errors:
        log.warning("data_feed_errors", extra={"pairs": [e["pair"] for e in errors]})

    # All pairs — used by n8n for the no-signal market summary
    market_snapshot = results

    # Check system health — pause all signals in observation mode
    system_state = performance_tracker.get_system_state()

    if system_state["mode"] == "observation":
        log.warning("system_observation_mode", extra={"reason": system_state["reason"]})
        return {
            "generated":       dt.datetime.now(SAST).isoformat(),
            "ai_provider":     AI_PROVIDER,
            "ai_model":        AI_MODEL,
            "system_mode":     "observation",
            "system_state":    system_state,
            "active_count":    0,
            "signals":         [],
            "signals_gated":   [],
            "market_snapshot": market_snapshot,
            "errors":          errors,
        }

    # Only new, non-duplicate active signals get AI-scored
    new_signals = [s for s in results if s["signal"] not in ("none", "error", "duplicate")]
    scored      = [{**s, "ai": score_signal(s)} for s in new_signals]

    # Confidence gate: only signals with AI confidence >= 70 go to n8n
    signals_passed = [s for s in scored if (s["ai"].get("confidence") or 0) >= 70]
    signals_gated  = [s for s in scored if (s["ai"].get("confidence") or 0) <  70]

    log.info("signals_with_ai_complete", extra={
        "total":   len(results),
        "new":     len(new_signals),
        "passed":  len(signals_passed),
        "gated":   len(signals_gated),
        "errors":  len(errors),
    })

    return {
        "generated":       dt.datetime.now(SAST).isoformat(),
        "ai_provider":     AI_PROVIDER,
        "ai_model":        AI_MODEL,
        "system_mode":     system_state["mode"],
        "active_count":    len(signals_passed),
        "signals":         signals_passed,
        "signals_gated":   signals_gated,
        "market_snapshot": market_snapshot,
        "errors":          errors,
    }


@app.get("/status")
def system_status():
    """System health: performance tracker state and rolling trade stats."""
    state = performance_tracker.get_system_state()
    return {
        "generated":    dt.datetime.now(SAST).isoformat(),
        "system_state": state,
        "recent_trades": performance_tracker.recent_trades(20),
    }


@app.post("/trades/record")
def record_trade(body: TradeRecord):
    """
    Record a closed trade result for performance tracking.

    n8n or your broker webhook should POST here after each trade closes.
    The performance tracker uses r_multiple to compute rolling win rate,
    profit factor, and drawdown state.
    """
    performance_tracker.log_trade(body.pair, body.direction, body.r_multiple)
    log.info("trade_recorded", extra={
        "pair":       body.pair,
        "direction":  body.direction,
        "r_multiple": body.r_multiple,
    })
    state = performance_tracker.get_system_state()
    return {
        "recorded":     True,
        "system_state": state,
    }
