# SignalEdge

A production-grade forex signal engine that runs optimized technical strategies on live 1H bar data, scores each signal with AI confidence analysis, and delivers alerts via Telegram — all self-hosted on Railway.

---

## What it does

1. **Every hour**, the n8n workflow triggers the signal API
2. The API fetches the latest 1H bars for each active pair via Yahoo Finance
3. Indicators are computed (EMA, RSI, ADX, DI+/DI−, SMA50, ATR)
4. Optimized entry conditions are evaluated — parameters were derived from a 2-year grid-search backtest
5. Active signals are scored by an AI model (confidence 0–100, key risks, plain-English summary)
6. Telegram alert is sent and the signal is logged to Google Sheets

---

## Active pairs and strategies

Parameters come from `backtest_optimized.py` — a grid search over ADX threshold, DI-spread minimum, SL multiplier, and R:R ratio across 2 years of 1H data.

| Pair    | Strategy         | ADX min | DI-spread min | SL      | R:R  | Backtest PF | Max DD |
|---------|-----------------|---------|--------------|---------|------|-------------|--------|
| EUR/USD | EMA+ADX Enhanced | ≥35    | ≥0           | 2.5×ATR | 3.5R | 2.72        | 7.7%   |
| USD/JPY | RSI Momentum     | ≥35    | ≥5           | 2.5×ATR | 2.0R | 1.83        | 11.3%  |
| AUD/JPY | EMA Crossover    | ≥20    | ≥5           | 2.5×ATR | 2.5R | 1.31        | 20.4%  |
| USD/CAD | EMA Crossover    | ≥35    | ≥0           | 2.5×ATR | 2.5R | 1.11        | 9.9%   |

**Break-even win rate** with 2.5R → **28.6%**. All strategies run well above this in backtest.

**Risk per trade: 2% of account balance.**

---

## Architecture

```
[n8n — Schedule Trigger every 1H]
        │
        ▼
[POST /signals/ai]  ←── signal_api.py (FastAPI on Railway)
        │                    │
        │                    └── signal_engine.py
        │                            └── yfinance (Yahoo Finance)
        │
        ├── [Filter to test pairs]
        ├── [IF active_count > 0]
        │        │
        │        ├── [Format message]
        │        │        ├── [Telegram alert]
        │        │        └── [Google Sheets log]
        │        │
        │        └── [No signals — no-op]
        │
        └── AI scoring: Anthropic | OpenAI | Gemini
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check — returns provider and model |
| `GET` | `/signals` | All pairs, active and inactive |
| `GET` | `/signals/active` | Only pairs with a live signal |
| `GET` | `/signals/EUR-USD` | Single pair (replace `/` with `-`) |
| `POST` | `/signals/ai` | All active signals + AI scoring |

---

## Deployment on Railway

### 1. Create the project

```bash
# Push this repo to GitHub first
git init && git add . && git commit -m "Initial commit"
git remote add origin https://github.com/your-username/signaledge.git
git push -u origin main
```

### 2. Deploy the Signal API

- Railway → New Project → **Deploy from GitHub repo** → select `signaledge`
- Railway detects the `Dockerfile` automatically
- Set environment variables on the **signal-api** service:

```
ANTHROPIC_API_KEY = sk-ant-...     # if AI_PROVIDER=anthropic
OPENAI_API_KEY    = sk-...         # if AI_PROVIDER=openai
GEMINI_API_KEY    = AIza...        # if AI_PROVIDER=gemini
AI_PROVIDER       = anthropic      # anthropic | openai | gemini
AI_MODEL          =                # leave blank to use default per provider
PORT              = 8000
```

### 3. Deploy n8n

- Same Railway project → **Add service → Docker image** → `n8nio/n8n:latest`
- Add a **Volume** mounted at `/home/node/.n8n` (persists workflows and credentials)
- Set environment variables on the **n8n** service:

```
SIGNAL_API_URL   = http://signal-api.railway.internal:8000
TELEGRAM_CHAT_ID = your_telegram_chat_id
ANTHROPIC_API_KEY= sk-ant-...   (n8n may use AI nodes directly too)
GOOGLE_SHEET_ID  = your_sheet_id   (optional)
N8N_HOST         = 0.0.0.0
N8N_PORT         = 5678
```

- Enable **Public networking** on port 5678 to access the n8n UI

### 4. Import and activate the workflow

- Open `https://your-n8n.railway.app`
- Settings → **Import from file** → select `n8n_workflow.json`
- Add your **Telegram Bot** credentials under Credentials
- **Activate** the workflow

---

## AI provider configuration

Switch providers by setting `AI_PROVIDER` on the Railway signal-api service:

| Provider | Env var | Default model | Flagship (override) |
|----------|---------|--------------|---------------------|
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | `claude-opus-4-6` |
| `openai` | `OPENAI_API_KEY` | `gpt-5.4-mini` | `gpt-5.4` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` | `gemini-2.5-pro` |

> **March 2026 notes:** GPT-4o was retired February 2026. `gemini-2.0-flash` is deprecated (retires June 2026). Defaults have been updated accordingly.

Override the model with `AI_MODEL=model-name`. Example:

```
AI_PROVIDER = openai
AI_MODEL    = gpt-5.4-mini    # affordable — ~$0.75/1M input tokens
```

---

## How to improve the strategy over time

The strategy parameters were optimized on historical data. Markets change — regimes shift, volatility expands and contracts. The system is designed to be re-calibrated as live performance data accumulates.

### Step 1 — Track every live signal

The Google Sheets log captures every signal automatically. Add these columns manually when you close each trade:

| Column | What to record |
|--------|---------------|
| `Exit Price` | Price where you closed |
| `Exit Date` | When you closed |
| `Live PnL (R)` | Actual R gained/lost |
| `Live Win` | TRUE / FALSE |
| `Notes` | Why you exited early, news events, etc. |

### Step 2 — Calculate your live win rate

After every **20 completed trades** per pair, compute:

```
Live Win Rate = Wins / Total Trades × 100
Break-even   = 1 / (1 + RR Ratio) × 100
```

Compare live win rate to the backtest win rate:

| Live vs Backtest | Action |
|-----------------|--------|
| Within 5% | System is working — no changes |
| 5–10% below | Investigate: check if market regime changed (ADX distribution shifted) |
| >10% below for 30+ trades | Re-run `backtest_optimized.py` on the most recent 6 months, update parameters |
| Consistently above | Consider loosening ADX/DI filters slightly to increase trade frequency |

### Step 3 — Re-run the optimizer (quarterly)

The optimizer (`backtest_optimized.py` in the research repo) runs a full grid search:

```bash
cd ../trading
uv run python backtest_optimized.py
```

It will output new optimal parameters per pair. Update `LIVE_PAIRS` in `signal_engine.py` accordingly.

**Key metrics to watch when updating:**
- **Profit Factor** must be ≥ 1.15 after optimization. Below this, the edge is too thin to survive real spreads.
- **Max Drawdown** should not increase significantly from current levels.
- **Trade count** must be ≥ 20 per pair over the test window. Too few trades = unreliable optimization.

### Step 4 — Regime awareness

Some conditions make all strategies underperform regardless of parameters:

| Condition | Signal | What to do |
|-----------|--------|-----------|
| ADX < 20 across all pairs | Ranging market | Reduce position size or pause |
| Major news week (NFP, FOMC, CPI) | High volatility risk | Skip signals within 4h of announcement |
| Correlated losses across pairs | Market dislocation | Review whether pairs are moving together — reduce exposure |

### Step 5 — Adding a new pair

To add a new pair to the live engine:

1. Run the backtest optimizer on the candidate pair across all 4 strategies
2. Only add if: Profit Factor ≥ 1.15 and Max DD ≤ 25% over 2 years
3. Add it to `LIVE_PAIRS` in `signal_engine.py` with the optimal parameters
4. Paper trade it for 1 month before going live

---

## Local development

```bash
# Install dependencies
uv sync

# Run the API locally
uv run uvicorn signal_api:app --reload --port 8000

# Test signal engine directly
uv run python signal_engine.py

# Health check
curl http://localhost:8000/health

# Get all signals
curl http://localhost:8000/signals

# Get AI-scored signals
curl -X POST http://localhost:8000/signals/ai
```

---

## Environment variables reference

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_PROVIDER` | No | `anthropic` (default) \| `openai` \| `gemini` |
| `AI_MODEL` | No | Override default model for chosen provider |
| `ANTHROPIC_API_KEY` | If using Anthropic | Claude API key |
| `OPENAI_API_KEY` | If using OpenAI | OpenAI API key |
| `GEMINI_API_KEY` | If using Gemini | Google AI API key |
| `PORT` | No | API port (default: 8000, Railway sets automatically) |

n8n service additionally needs:

| Variable | Description |
|----------|-------------|
| `SIGNAL_API_URL` | Internal Railway URL: `http://signal-api.railway.internal:8000` |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel to send alerts to |
| `GOOGLE_SHEET_ID` | Google Sheets document ID for signal log (optional) |
