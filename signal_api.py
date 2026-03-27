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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from signal_engine import get_signals, LIVE_PAIRS
from ai import score_signal, AI_PROVIDER, AI_MODEL
from core.logging import get_logger

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
        "time":        dt.datetime.now().isoformat(),
    }


@app.get("/signals")
def signals_all():
    results = get_signals()
    return {
        "generated": dt.datetime.now().isoformat(),
        "count":     len(results),
        "signals":   results,
    }


@app.get("/signals/active")
def signals_active():
    results = get_signals()
    active  = [s for s in results if s["signal"] not in ("none", "error", "duplicate")]
    return {
        "generated":    dt.datetime.now().isoformat(),
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


@app.post("/signals/ai")
def signals_with_ai():
    """
    Return only NEW (non-duplicate) active signals, each enriched with AI scoring.
    Also surfaces any data feed errors in the `errors` field for n8n routing.

    Set AI_PROVIDER = anthropic | openai | gemini
    """
    results = get_signals()

    # Surface data feed errors so n8n can alert on them
    errors = [s for s in results if s["signal"] == "error"]
    if errors:
        log.warning("data_feed_errors", extra={"pairs": [e["pair"] for e in errors]})

    # Only new, non-duplicate active signals get AI-scored and returned
    new_signals = [s for s in results if s["signal"] not in ("none", "error", "duplicate")]

    enriched = [{**s, "ai": score_signal(s)} for s in new_signals]

    log.info("signals_with_ai_complete", extra={
        "total":   len(results),
        "new":     len(new_signals),
        "errors":  len(errors),
    })

    return {
        "generated":    dt.datetime.now().isoformat(),
        "ai_provider":  AI_PROVIDER,
        "ai_model":     AI_MODEL,
        "active_count": len(enriched),
        "signals":      enriched,
        "errors":       errors,
    }
