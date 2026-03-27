"""
SignalEdge — AI Client
======================
Unified interface for scoring signals across Anthropic, OpenAI, and Gemini.

Provider is selected via the AI_PROVIDER environment variable:
  AI_PROVIDER=anthropic  →  Claude        (requires ANTHROPIC_API_KEY)
  AI_PROVIDER=openai     →  GPT-5.4-mini  (requires OPENAI_API_KEY)
  AI_PROVIDER=gemini     →  Gemini 2.5    (requires GEMINI_API_KEY)

Override the default model with AI_MODEL env var:
  AI_MODEL=claude-opus-4-6   (Anthropic flagship)
  AI_MODEL=gpt-5.4           (OpenAI flagship)
  AI_MODEL=gemini-2.5-pro    (Gemini flagship)

Public API:
  from ai import score_signal
  ai_data = score_signal(signal_dict)
"""

import json
import os
from pathlib import Path

from core.logging import get_logger

log = get_logger(__name__)

# ── Provider / model config ────────────────────────────────────────────────────
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").lower()

# Default models — updated March 2026
# Anthropic: Sonnet 4.6 — best speed/intelligence balance, 1M context
# OpenAI:    GPT-5.4-mini — GPT-4o retired Feb 2026; 5.4-mini is affordable production tier
# Gemini:    2.5 Flash — gemini-2.0-flash deprecated June 2026; 2.5 Flash is current stable
_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai":    "gpt-5.4-mini",
    "gemini":    "gemini-2.5-flash",
}

# Flagship overrides (higher quality, higher cost):
#   anthropic → claude-opus-4-6
#   openai    → gpt-5.4
#   gemini    → gemini-2.5-pro

AI_MODEL = os.environ.get("AI_MODEL") or _DEFAULT_MODEL.get(AI_PROVIDER, "")

# ── Prompt template ────────────────────────────────────────────────────────────
# Loaded once at module import — FileNotFoundError at startup is intentional
# (fail fast if the prompts directory was not copied into the Docker image).
_PROMPT_TEMPLATE = (
    Path(__file__).parent.parent / "prompts" / "signal_score.txt"
).read_text(encoding="utf-8")


def _build_prompt(sig: dict) -> str:
    ind = sig["indicators"]
    p   = sig["params"]
    return _PROMPT_TEMPLATE.format(
        pair=sig["pair"],
        strategy=sig["strategy"],
        direction=sig["signal"].upper(),
        entry=sig["entry"],
        sl=sig["sl"],
        risk=round(abs(sig["entry"] - sig["sl"]), 5),
        tp=sig["tp"],
        reward=round(abs(sig["entry"] - sig["tp"]), 5),
        rr_ratio=p["rr_ratio"],
        sl_mult=p["sl_mult"],
        rsi=ind["rsi"],
        adx=ind["adx"],
        dip=f"{ind['dip']:.1f}",
        dim=f"{ind['dim']:.1f}",
        di_spread=f"{ind['di_spread']:.1f}",
        ema9=ind["ema9"],
        ema21=ind["ema21"],
        sma50=ind["sma50"],
        sma50_slope=ind["sma50_slope"],
        close=ind["close"],
        bar_time=sig["bar_time"],
        adx_min=p["adx_min"],
        di_spread_min=p["di_spread_min"],
    )


def _call_provider(prompt: str) -> str:
    """Route prompt to the configured AI provider; return raw text response."""
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


def _parse_response(raw: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text  = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def score_signal(sig: dict) -> dict:
    """
    Score a single signal dict with AI analysis.

    Returns an ai_data dict on success:
      { confidence, quality, trend_alignment, key_risks, summary, emoji }

    Returns { error, confidence: None } on any failure so callers never crash.
    """
    try:
        prompt  = _build_prompt(sig)
        raw     = _call_provider(prompt)
        ai_data = _parse_response(raw)
        log.info("ai_scored", extra={
            "pair":       sig["pair"],
            "provider":   AI_PROVIDER,
            "model":      AI_MODEL,
            "confidence": ai_data.get("confidence"),
        })
        return ai_data
    except Exception as exc:
        log.warning("ai_score_failed", extra={"pair": sig["pair"], "error": str(exc)})
        return {"error": str(exc), "confidence": None}
