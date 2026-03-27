# SignalEdge — n8n Workflow Documentation

## Overview

The n8n workflow is the **automation layer** of SignalEdge. It sits between the signal API (which does the heavy lifting — data fetching, indicator computation, AI scoring) and the outside world (Telegram, Google Sheets, and any future destinations).

Think of it as the delivery system. The signal engine is the brain. n8n is the hands.

---

## What the workflow does right now

The workflow runs **every hour**, timed to align with the close of a 1H candle. Here is exactly what happens step by step.

---

### Node 1 — Schedule Trigger (every hour)

Fires automatically every 60 minutes. This is the heartbeat of the system.

There is no intelligence here yet — it fires regardless of market session, day of week, or whether the market is open. The signal engine handles filtering internally (e.g. skipping Thursday, London/NY session gates).

---

### Node 2 — Fetch Active Signals

Makes a `POST` request to `/signals/ai` on the signal API.

```
POST http://signal-api.railway.internal:8000/signals/ai
```

This single call does everything on the API side:
- Fetches live 1H bars from Yahoo Finance for all active pairs
- Computes RSI, ADX, EMA, DI+/DI−, ATR, SMA50
- Evaluates optimized entry conditions per pair
- Passes any active signals to the configured AI model for confidence scoring
- Returns a JSON payload with all signals (active and inactive) plus AI analysis

Timeout is set to 90 seconds — the AI scoring step adds latency.

---

### Node 3 — Filter to 3 test pairs

During the current **test phase**, only three pairs are allowed through:

- EUR/USD
- USD/JPY
- AUD/JPY

USD/CAD is excluded for now — its backtest profit factor (1.11) is marginal, and we want to observe it further before going live.

This node also strips out any signals with `signal = "none"` or `signal = "error"`, so only actionable entries proceed.

**This is the node to edit when you go full production** — either remove the filter entirely or update the pair list.

---

### Node 4 — Any signals found? (IF branch)

Simple gate. If `active_count > 0`, the workflow takes the **YES** branch and proceeds to format and send. If zero active signals, it takes the **NO** branch and does nothing (no-op log).

This prevents empty Telegram messages from being sent on quiet hours.

---

### Node 5 — Format Each Signal

Iterates over each active signal and builds a Telegram-ready Markdown message per signal. The message includes:

- Pair and direction (LONG / SHORT) with colour emoji
- Strategy name
- Entry price, Stop Loss, Take Profit
- SL multiplier (ATR-based) and R:R ratio
- Live indicator values: ADX, DI-spread, RSI
- The optimized filter thresholds that were passed (ADX min, DI-spread min)
- AI confidence score, quality rating, and 2-sentence plain-English summary
- AI-identified key risks
- Bar timestamp in SAST

Each signal becomes its own output item so Telegram receives one message per signal, not one combined message.

---

### Node 6 — Send Telegram Alert

Sends each formatted message to the configured Telegram chat or channel.

Uses Markdown parse mode so bold, code blocks, and bullet points render correctly. The `TELEGRAM_CHAT_ID` environment variable controls the destination — this can be a personal chat, a group, or a channel.

---

### Node 7 — No signals (no-op)

The NO branch of the IF gate. Currently does nothing — it is a placeholder. In future this could write a "no signal this hour" entry to a log, or update a dashboard status indicator.

---

### Node 8 — Log to Google Sheets

Appends a row to a Google Sheet for every active signal that was sent. Columns logged:

| Column | Value |
|--------|-------|
| Timestamp | Time the workflow ran |
| Pair | e.g. EUR/USD |
| Signal | long or short |
| Message | The full formatted Telegram message |

This is the **performance tracking foundation**. Over time, you can add Exit Price, Live PnL, and Win/Loss columns manually to measure how the live signals perform against the backtest numbers.

---

## Current workflow — visual flow

```
[Every Hour]
     │
     ▼
[POST /signals/ai]  ──────────────────── Signal API (Railway)
     │
     ▼
[Filter: EUR/USD, USD/JPY, AUD/JPY only]
     │
     ▼
[IF active_count > 0]
     │                    │
    YES                   NO
     │                    │
     ▼                    ▼
[Format Messages]    [No-op — do nothing]
     │
     ├──▶ [Telegram Alert]    (one message per signal)
     │
     └──▶ [Google Sheets Log] (one row per signal)
```

---

## What is missing and what to build next

These are all achievable in n8n without code changes to the signal engine or API.

---

### Phase 2 — Reliability and observability

**Add a daily summary message**
- Create a second workflow on a daily schedule (e.g. 08:00 SAST)
- Pull yesterday's rows from Google Sheets
- Count signals sent, pairs triggered, AI confidence distribution
- Send a morning digest to Telegram: "Yesterday: 2 signals fired. EUR/USD LONG (conf 78%), AUD/JPY SHORT (conf 64%)"

**Alert on API errors**
- Wrap Node 2 in an error handler
- If the API returns an error or times out, send a Telegram alert: "⚠️ SignalEdge API unreachable — check Railway logs"
- Currently a failed API call silently drops the execution

**Track "no signal" hours**
- Replace Node 7 (no-op) with a Google Sheets append
- Log every hour even when no signal fires — this lets you calculate signal frequency and detect if the engine has gone quiet unexpectedly

---

### Phase 3 — Additional notification channels

**WhatsApp**
n8n has a WhatsApp Business node. You need a WhatsApp Business API account (Meta Cloud API or Twilio).

- Add a WhatsApp node parallel to the Telegram node in Node 5's output
- Use the same formatted message or a shorter SMS-style version
- Useful for reaching people who don't use Telegram

**Email**
- Add a Gmail or SMTP node alongside Telegram
- Good for a daily digest rather than per-signal alerts (email fatigue is real)
- Attach a small HTML table of the day's signals

**Discord**
- n8n has a Discord node — one webhook URL, no extra account needed
- Useful if you run a trading community or Discord server
- Discord supports rich embeds: colour-coded by direction, fields for entry/SL/TP

**Slack**
- Add a Slack node for team or internal use
- Block Kit formatting lets you build structured signal cards

---

### Phase 4 — Content and publishing

**Auto-update a blog or website**

If you run a blog (WordPress, Ghost, Webflow, or a static site on Vercel/Netlify):

- Add a **WordPress node** or **HTTP Request node** after Format Each Signal
- Post a new blog entry when a signal fires: "EUR/USD LONG signal — here's why the system fired"
- Include the AI summary as the post body
- Tag posts by pair, direction, and strategy for filtering

For a Ghost blog:
```
POST https://your-blog.ghost.io/ghost/api/admin/posts/
Authorization: Ghost <admin_api_key>
Body: { title, html, status: "published" }
```

For Webflow CMS:
```
POST https://api.webflow.com/v2/collections/{id}/items
Authorization: Bearer <token>
```

**Tweet / post to X**
- n8n has an X (Twitter) node
- Auto-post each signal as a tweet: "🟢 EUR/USD LONG — AI confidence 81% · Entry 1.1234 · TP 1.1456 #forex #trading"
- Schedule a delay so you tweet after the bar closes, not mid-candle

---

### Phase 5 — Intelligence and feedback loop

**Outcome tracking workflow**

Create a second workflow that runs daily and:
1. Fetches open signals from Google Sheets (rows without an Exit Price)
2. For each open signal, calls `GET /signals/EUR-USD` to get the current price
3. Checks if SL or TP has been hit based on the day's high/low
4. Updates the Exit Price, PnL, and Win/Loss columns in Google Sheets automatically

This closes the feedback loop without manual data entry.

**AI signal review**

Once you have 30+ closed trades in Google Sheets:
- Build a weekly workflow that reads the trade log
- Sends the win rate, average RR, and worst trades to the AI
- Asks: "Based on these results, which conditions are underperforming?"
- Posts the AI's analysis to Telegram as a weekly review

**Confidence threshold gate**

Add a filter node between Format Each Signal and Send Telegram Alert:
- Only send if `ai.confidence >= 65`
- Signals below the threshold are still logged to Google Sheets but not alerted
- Over time, check whether high-confidence signals outperform low-confidence ones — if they do, raise the threshold

---

### Phase 6 — User-facing product

If SignalEdge grows into a product that other people use:

**Webhook-triggered delivery**
- Replace the hourly schedule with a webhook trigger
- External subscribers POST to your n8n webhook to receive signals on demand
- Supports building a simple signal subscription API

**Per-user preferences**
- Store user preferences in Airtable or a Google Sheet (preferred pairs, minimum confidence, direction preference)
- n8n reads preferences before sending — EUR/USD only for some users, all pairs for others

**Stripe payment gate**
- Add a Stripe node to check if a user's subscription is active before delivering signals
- n8n handles the entire subscription check without code

---

## Environment variables the workflow depends on

| Variable | Where set | Description |
|----------|-----------|-------------|
| `SIGNAL_API_URL` | n8n service on Railway | Internal API URL — `http://signal-api.railway.internal:8000` |
| `TELEGRAM_CHAT_ID` | n8n service on Railway | Destination chat, group, or channel ID |
| `GOOGLE_SHEET_ID` | n8n service on Railway | Google Sheets document ID for signal log |

Credentials stored inside n8n (not env vars):
- **Telegram Bot** — bot token from BotFather
- **Google Sheets OAuth** — authorized via n8n's Google credential flow

---

## How to extend the workflow

1. Open n8n at your Railway URL
2. Click the workflow → **Edit**
3. Drag a new node from the node panel onto the canvas
4. Connect it to an existing node's output
5. Configure credentials and field mappings
6. **Save** and the change is live immediately — no redeployment needed

n8n changes do not require touching the signal engine or API. The API is the data source. n8n is the delivery layer. They are intentionally decoupled so either side can evolve independently.
