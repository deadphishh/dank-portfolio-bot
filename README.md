# 📈 Portfolio Tracker Discord Bot

A free Discord bot that monitors long/short leveraged positions across crypto and stocks, posting P&L and liquidation alerts to a dedicated channel.

---

## 🚀 Setup (5 minutes)

### 1. Create Your Discord Bot
1. Go to https://discord.com/developers/applications
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - ✅ Server Members Intent
5. Copy your **Bot Token** (keep this secret!)
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Use Slash Commands`, `Mention Everyone`
7. Visit the generated URL to invite the bot to your server

### 2. Create a Dedicated Alerts Channel
1. In your Discord server, create a channel called `#portfolio-alerts` (or any name)
2. Right-click the channel → **Copy Channel ID**
   - No option? Go to **User Settings → Advanced → Enable Developer Mode** first
3. Save this ID — you need it as `DISCORD_ALERT_CHANNEL_ID`

---

## ☁️ Deploy to Railway (Recommended — Free)

Railway gives 500 free hours/month — enough for a 24/7 bot.

1. Push your code to a GitHub repo:
   ```bash
   git init && git add . && git commit -m "initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/portfolio-bot
   git push -u origin main
   ```
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**
3. Select your repo. Railway auto-detects the Dockerfile.
4. Go to **Variables** tab → add:
   ```
   DISCORD_BOT_TOKEN        = your_bot_token_here
   DISCORD_ALERT_CHANNEL_ID = your_channel_id_here
   ```
5. Click **Deploy** — your bot is live 24/7.

> **Data persistence tip:** Railway's free tier has an ephemeral filesystem — `portfolio.json` resets on redeploy. To fix this, add a Railway Volume (free, 1GB), mount it at `/data`, and change `PORTFOLIO_FILE = "/data/portfolio.json"` in `bot.py`.

---

## ☁️ Deploy to Render (Alternative)

> ⚠️ Render's free tier spins down after inactivity — use Railway instead for a always-on bot.

1. Push to GitHub (same steps above)
2. Go to https://render.com → **New → Blueprint** → connect your repo
3. Render reads `render.yaml` automatically. Set env vars in dashboard:
   ```
   DISCORD_BOT_TOKEN        = your_bot_token_here
   DISCORD_ALERT_CHANNEL_ID = your_channel_id_here
   ```

---

## 📋 Commands

| Command | Description |
|---|---|
| `/add-position` | Opens a popup modal to add a position |
| `/positions` | Lists all open positions with live P&L |
| `/portfolio` | Detailed embedded portfolio summary |
| `/close [index]` | Closes position #N (from /positions) |

### Modal Fields (`/add-position`)
| Field | Example |
|---|---|
| Asset Type | `crypto` or `stock` |
| Ticker | `BTC`, `ETH`, `AAPL`, `TSLA` |
| Direction | `long` or `short` |
| Entry Price | `42000.00` |
| Leverage | `10` (use `1` for spot) |

---

## 🔔 Alerts (posted to your dedicated channel, tagging the user)

**P&L Milestones** — fires once per threshold per position:
`±10%`, `±25%`, `±50%`, `±75%`, `±100%`, `±125%`, `±150%`, `±200%`

**Liquidation Warnings:**
- ⚠️ 80% toward liquidation
- 🚨 95% toward liquidation

Formulas: Long liq = `entry × (1 - 1/leverage)` | Short liq = `entry × (1 + 1/leverage)`

---

## 💰 Free APIs
- **Stocks/ETFs:** yfinance (Yahoo Finance — no key needed)
- **Crypto:** CoinGecko free public API — no key needed

Prices checked every **5 minutes**.

---

## 📁 File Structure
```
portfolio_bot/
├── bot.py              # Commands, modal, monitor loop
├── price_fetcher.py    # Price lookups (Yahoo + CoinGecko)
├── portfolio.py        # JSON helpers, P&L + liquidation math
├── requirements.txt
├── Dockerfile
├── railway.toml
├── render.yaml
├── .gitignore
└── README.md
```
