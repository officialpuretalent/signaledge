"""
SignalEdge — Signal API
=======================
FastAPI wrapper around the signal engine.

Run:  uvicorn signal_api:app --host 0.0.0.0 --port 8000

Endpoints:
  GET  /health              — liveness check
  GET  /signals             — all pairs (active + inactive)
  GET  /signals/active      — only pairs with a live signal
  GET  /signals/{pair}      — single pair  (e.g. /signals/EUR-USD)
  POST /signals/ai          — signals + AI confidence scoring

AI provider is selected via the AI_PROVIDER environment variable:
  AI_PROVIDER=anthropic  →  Claude        (requires ANTHROPIC_API_KEY)
  AI_PROVIDER=openai     →  GPT-5.4-mini  (requires OPENAI_API_KEY)
  AI_PROVIDER=gemini     →  Gemini 2.5    (requires GEMINI_API_KEY)

Default models (as of March 2026):
  anthropic → claude-sonnet-4-6      (Sonnet 4.6 — best speed/quality balance)
  openai    → gpt-5.4-mini           (GPT-4o retired Feb 2026; 5.4-mini is affordable tier)
  gemini    → gemini-2.5-flash       (2.0-flash deprecated June 2026; 2.5 Flash is stable)

Override with AI_MODEL env var:
  AI_MODEL=claude-opus-4-6           (Anthropic flagship)
  AI_MODEL=gpt-5.4                   (OpenAI flagship)
  AI_MODEL=gemini-2.5-pro            (Gemini flagship)
"""

import os
import json
import datetime as dt

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from signal_engine import get_signals, LIVE_PAIRS

app = FastAPI(title="SignalEdge API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── AI provider config ────────────────────────────────────────────────────────
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").lower()

# Default models — updated March 2026
# Anthropic: Sonnet 4.6 — best speed/intelligence balance, 1M context
# OpenAI:    GPT-5.4-mini — GPT-4o retired Feb 2026; 5.4-mini is affordable production tier
# Gemini:    2.5 Flash — gemini-2.0-flash deprecated June 2026; 2.5 Flash is current stable
_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-5.4-mini",
    "gemini":    "gemini-2.5-flash",
}

# Flagship models if you want maximum quality (higher cost):
# anthropic → claude-opus-4-6
# openai    → gpt-5.4
# gemini    → gemini-2.5-pro

AI_MODEL = os.environ.get("AI_MODEL") or _DEFAULT_MODEL.get(AI_PROVIDER, "")


def _call_ai(prompt: str) -> str:
    """
    Send prompt to the configured AI provider and return the raw text response.
    Raises RuntimeError if the provider is misconfigured.
    """
    if AI_PROVIDER == "openai":
        import openai
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        client = openai.OpenAI(api_key=api_key)
        resp   = client.chat.completions.create(
            model=AI_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()

    elif AI_PROVIDER == "gemini":
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        client = genai.Client(api_key=api_key)
        resp   = client.models.generate_content(model=AI_MODEL, contents=prompt)
        return resp.text.strip()

    else:  # anthropic (default)
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        client = anthropic.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model=AI_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()


def _parse_ai_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from AI response."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text  = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _build_prompt(sig: dict) -> str:
    ind = sig["indicators"]
    p   = sig["params"]
    direction = sig["signal"].upper()
    risk  = round(abs(sig["entry"] - sig["sl"]), 5)
    reward= round(abs(sig["entry"] - sig["tp"]), 5)

    return f"""You are a professional forex analyst. Evaluate this trading signal and respond with JSON only.

Signal:
  Pair:       {sig['pair']}
  Strategy:   {sig['strategy']}
  Direction:  {direction}
  Entry:      {sig['entry']}
  Stop Loss:  {sig['sl']}  (risk: {risk})
  Take Profit:{sig['tp']}  (reward: {reward})
  R:R Ratio:  {p['rr_ratio']}:1
  SL:         {p['sl_mult']}x ATR(14)

Indicators at signal bar:
  RSI:        {ind['rsi']}
  ADX:        {ind['adx']} (DI+: {ind['dip']:.1f}  DI-: {ind['dim']:.1f}  Spread: {ind['di_spread']:.1f})
  EMA9:       {ind['ema9']}
  EMA21:      {ind['ema21']}
  SMA50:      {ind['sma50']}  (slope: {ind['sma50_slope']})
  Close:      {ind['close']}
  Bar time:   {sig['bar_time']} SAST

Entry filter thresholds (all passed):
  ADX ≥ {p['adx_min']}  ·  DI-spread ≥ {p['di_spread_min']}

Respond ONLY with this JSON:
{{
  "confidence": <integer 0-100>,
  "quality": "strong" | "moderate" | "weak",
  "trend_alignment": true | false,
  "key_risks": ["<risk1>", "<risk2>"],
  "summary": "<2-sentence plain-English alert suitable for Telegram>",
  "emoji": "🟢" | "🟡" | "🔴"
}}"""


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
    active  = [s for s in results if s["signal"] not in ("none", "error")]
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
    Fetch all active signals then score each one with the configured AI provider.
    Set AI_PROVIDER = anthropic | openai | gemini
    """
    results = get_signals()
    active  = [s for s in results if s["signal"] not in ("none", "error")]

    if not active:
        return {
            "generated":    dt.datetime.now().isoformat(),
            "ai_provider":  AI_PROVIDER,
            "ai_model":     AI_MODEL,
            "active_count": 0,
            "signals":      [],
        }

    enriched = []
    for sig in active:
        try:
            raw     = _call_ai(_build_prompt(sig))
            ai_data = _parse_ai_response(raw)
        except Exception as e:
            ai_data = {"error": str(e), "confidence": None}

        enriched.append({**sig, "ai": ai_data})

    return {
        "generated":    dt.datetime.now().isoformat(),
        "ai_provider":  AI_PROVIDER,
        "ai_model":     AI_MODEL,
        "active_count": len(enriched),
        "signals":      enriched,
    }
