import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
from datetime import datetime, timezone, timedelta
from price_fetcher import get_price
from portfolio import (
    load_portfolio, save_portfolio,
    add_position, close_position,
    get_user_positions, get_all_positions,
    get_leaderboard, calculate_pnl,
    calculate_liquidation_price
)

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALERT_CHANNEL_ID   = int(os.getenv("DISCORD_ALERT_CHANNEL_ID", "0"))  # ID of your #alerts channel
PORTFOLIO_FILE    = "portfolio.json"
PRICE_ALERTS_FILE = "price_alerts.json"

# P&L milestones to alert on (in percent, both positive and negative)
PNL_MILESTONES = [10, 25, 50, 75, 100, 125, 150, 200]
LIQ_WARNING_LEVELS = [80, 95]  # % of the way to liquidation

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── Modal: Add Position ──────────────────────────────────────────────────────
class AddPositionModal(discord.ui.Modal, title="Add New Position"):
    asset_type = discord.ui.TextInput(
        label="Asset Type",
        placeholder="crypto  OR  stock",
        required=True,
        max_length=10
    )
    ticker = discord.ui.TextInput(
        label="Ticker Symbol",
        placeholder="e.g. BTC, ETH, AAPL, TSLA",
        required=True,
        max_length=20
    )
    direction = discord.ui.TextInput(
        label="Direction",
        placeholder="long  OR  short",
        required=True,
        max_length=5
    )
    entry_price = discord.ui.TextInput(
        label="Entry Price (USD)",
        placeholder="e.g. 42000.00",
        required=True,
        max_length=20
    )
    leverage = discord.ui.TextInput(
        label="Leverage (1 = spot, 100 = 100x)",
        placeholder="e.g. 1, 5, 10, 50, 100",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately with thinking=True — tells Discord to show a
        # loading state and wait indefinitely for our followup response.
        await interaction.response.defer(thinking=True)

        # Validate inputs
        asset_type_val = self.asset_type.value.strip().lower()
        if asset_type_val not in ("crypto", "stock"):
            await interaction.followup.send(
                "❌ Asset type must be **crypto** or **stock**.", ephemeral=True
            )
            return

        direction_val = self.direction.value.strip().lower()
        if direction_val not in ("long", "short"):
            await interaction.followup.send(
                "❌ Direction must be **long** or **short**.", ephemeral=True
            )
            return

        try:
            entry = float(self.entry_price.value.strip())
            lev = float(self.leverage.value.strip())
            if entry <= 0 or lev < 1 or lev > 100:
                raise ValueError
        except ValueError:
            await interaction.followup.send(
                "❌ Entry price must be a positive number and leverage must be between 1 and 100.", ephemeral=True
            )
            return

        ticker_val = self.ticker.value.strip().upper()

        # Verify the ticker resolves to a real price
        price = await get_price(ticker_val, asset_type_val)
        if price is None:
            await interaction.followup.send(
                f"❌ Could not fetch a price for **{ticker_val}** ({asset_type_val}). "
                "Double-check the ticker symbol.", ephemeral=True
            )
            return

        liq_price = calculate_liquidation_price(entry, lev, direction_val)

        position = {
            "ticker": ticker_val,
            "asset_type": asset_type_val,
            "direction": direction_val,
            "entry_price": entry,
            "leverage": lev,
            "liquidation_price": liq_price,
            "opened_at": datetime.utcnow().isoformat(),
            "alerted_milestones": [],      # e.g. ["10", "-10", "25"]
            "alerted_liq_levels": []       # e.g. [80, 95]
        }

        portfolio = load_portfolio(PORTFOLIO_FILE)
        add_position(portfolio, str(interaction.user.id), position)
        save_portfolio(PORTFOLIO_FILE, portfolio)

        direction_emoji = "📈" if direction_val == "long" else "📉"
        await interaction.followup.send(
            f"{direction_emoji} **Position opened!**\n"
            f"```\n"
            f"Ticker:      {ticker_val}\n"
            f"Direction:   {direction_val.upper()} {lev}x\n"
            f"Entry Price: ${entry:,.4f}\n"
            f"Liq. Price:  ${liq_price:,.4f}\n"
            f"Current:     ${price:,.4f}\n"
            f"```\n"
            f"Good luck out there, degen 🎰"
        )


# ── Slash Commands ───────────────────────────────────────────────────────────
@tree.command(name="add-position", description="Open a new long or short position")
async def add_position_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(AddPositionModal())


@tree.command(name="positions", description="View all open positions across all users")
async def positions_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    if not portfolio:
        await interaction.response.send_message("📭 No open positions in this server.")
        return

    await interaction.response.defer(thinking=True)
    lines = ["📊 **All Open Positions**\n"]
    counter = 1

    for user_id, positions in get_all_positions(portfolio).items():
        if not positions:
            continue
        # Resolve username
        user = client.get_user(int(user_id))
        if user is None:
            try:
                user = await client.fetch_user(int(user_id))
                username = user.display_name
            except Exception:
                username = f"User {user_id}"
        else:
            username = user.display_name

        for pos in positions:
            price = await get_price(pos["ticker"], pos["asset_type"])
            if price is None:
                price_display = "N/A"
                pnl_str = "N/A"
            else:
                pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
                pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"
                price_display = f"${price:,.4f}"

            direction_emoji = "📈" if pos["direction"] == "long" else "📉"
            lines.append(
                f"**#{counter} {direction_emoji} {pos['ticker']}** — "
                f"{pos['direction'].upper()} {pos['leverage']}x | **{username}**\n"
                f"  Entry: ${pos['entry_price']:,.4f} | Current: {price_display} | "
                f"P&L: **{pnl_str}** | Liq: ${pos['liquidation_price']:,.4f}\n"
            )
            counter += 1

    if counter == 1:
        await interaction.followup.send("📭 No open positions in this server.")
        return

    await interaction.followup.send("\n".join(lines))


@tree.command(name="portfolio", description="View a detailed portfolio summary with P&L")
async def portfolio_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(interaction.user.id))
    if not positions:
        await interaction.response.send_message("📭 You have no open positions.")
        return

    await interaction.response.defer(thinking=True)

    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name}'s Portfolio",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )

    total_weighted_pnl = 0
    valid_count = 0

    for i, pos in enumerate(positions):
        price = await get_price(pos["ticker"], pos["asset_type"])
        if price is None:
            field_val = (
                f"Direction: {pos['direction'].upper()} {pos['leverage']}x\n"
                f"Entry: ${pos['entry_price']:,.4f}\n"
                f"Price: ⚠️ unavailable\n"
                f"Liq: ${pos['liquidation_price']:,.4f}"
            )
        else:
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            total_weighted_pnl += pnl
            valid_count += 1
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            field_val = (
                f"Direction: {pos['direction'].upper()} {pos['leverage']}x\n"
                f"Entry: ${pos['entry_price']:,.4f} → ${price:,.4f}\n"
                f"P&L: {pnl_emoji} **{'+' if pnl >= 0 else ''}{pnl:.2f}%**\n"
                f"Liq: ${pos['liquidation_price']:,.4f}"
            )
        direction_emoji = "📈" if pos["direction"] == "long" else "📉"
        embed.add_field(
            name=f"#{i+1} {direction_emoji} {pos['ticker']}",
            value=field_val,
            inline=True
        )

    if valid_count:
        avg_pnl = total_weighted_pnl / valid_count
        embed.set_footer(text=f"Avg P&L across {valid_count} position(s): {'+' if avg_pnl >= 0 else ''}{avg_pnl:.2f}%")

    await interaction.followup.send(embed=embed)


@tree.command(name="close", description="Close one of your open positions by index")
@app_commands.describe(index="Position number from /positions (e.g. 1, 2, 3)")
async def close_cmd(interaction: discord.Interaction, index: int):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(interaction.user.id))
    if not positions:
        await interaction.response.send_message("📭 You have no open positions to close.")
        return

    idx = index - 1
    if idx < 0 or idx >= len(positions):
        await interaction.response.send_message(
            f"❌ Invalid index. You have {len(positions)} position(s). Use /positions to see them.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    pos = positions[idx]
    price = await get_price(pos["ticker"], pos["asset_type"])
    exit_price = price if price else pos["entry_price"]  # fallback to entry if price unavailable

    close_position(portfolio, str(interaction.user.id), idx,
                   exit_price=exit_price,
                   username=interaction.user.display_name)
    save_portfolio(PORTFOLIO_FILE, portfolio)

    if price:
        pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        await interaction.followup.send(
            f"✅ **Position closed.**\n"
            f"```\n"
            f"Ticker:    {pos['ticker']} ({pos['direction'].upper()} {pos['leverage']}x)\n"
            f"Entry:     ${pos['entry_price']:,.4f}\n"
            f"Exit:      ${price:,.4f}\n"
            f"Final P&L: {'+' if pnl >= 0 else ''}{pnl:.2f}%\n"
            f"```"
        )
    else:
        await interaction.followup.send(
            f"✅ Closed **{pos['ticker']}** position (could not fetch exit price)."
        )


# ── Alert Helper ─────────────────────────────────────────────────────────────
async def send_alert(message: str):
    """Post an alert message to the configured alerts channel."""
    if ALERT_CHANNEL_ID == 0:
        print(f"[ALERT — no channel set] {message}")
        return
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(ALERT_CHANNEL_ID)
        except Exception:
            print(f"[ALERT — channel not found] {message}")
            return
    try:
        await channel.send(message)
    except discord.Forbidden:
        print(f"[ALERT — missing permissions] {message}")


# ── Background Price Monitor ─────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def monitor_positions():
    portfolio = load_portfolio(PORTFOLIO_FILE)
    changed = False

    for user_id, positions in get_all_positions(portfolio).items():
        for pos in positions:
            price = await get_price(pos["ticker"], pos["asset_type"])
            if price is None:
                continue

            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            liq = pos["liquidation_price"]
            entry = pos["entry_price"]
            direction = pos["direction"]

            # ── P&L milestone alerts ──────────────────────────────────────
            for milestone in PNL_MILESTONES:
                for sign, label in [(1, f"+{milestone}"), (-1, f"-{milestone}")]:
                    threshold = sign * milestone
                    key = label
                    if key not in pos["alerted_milestones"]:
                        hit = (threshold > 0 and pnl >= threshold) or (threshold < 0 and pnl <= threshold)
                        if hit:
                            emoji = "🚀" if threshold > 0 else "💥"
                            await send_alert(
                                f"{emoji} **P&L Alert — {pos['ticker']}** | "
                                f"<@{user_id}>\n"
                                f"Your **{pos['direction'].upper()} {pos['leverage']}x** position hit "
                                f"**{label}%** P&L!\n"
                                f"Entry: ${entry:,.4f} | Current: ${price:,.4f} | P&L: {pnl:+.2f}%"
                            )
                            pos["alerted_milestones"].append(key)
                            changed = True

            # ── Liquidation proximity alerts ──────────────────────────────
            if liq and entry:
                if direction == "long":
                    total_move = entry - liq
                    current_move = entry - price
                else:
                    total_move = liq - entry
                    current_move = price - entry

                if total_move > 0:
                    liq_pct = (current_move / total_move) * 100
                    for level in LIQ_WARNING_LEVELS:
                        if liq_pct >= level and level not in pos["alerted_liq_levels"]:
                            await send_alert(
                                f"⚠️ **LIQUIDATION WARNING — {pos['ticker']}** | "
                                f"<@{user_id}>\n"
                                f"You are **{level}% of the way to liquidation!**\n"
                                f"Entry: ${entry:,.4f} | Current: ${price:,.4f} | "
                                f"Liq: ${liq:,.4f}\n"
                                f"Consider closing or adding margin to your "
                                f"**{direction.upper()} {pos['leverage']}x** position."
                            )
                            pos["alerted_liq_levels"].append(level)
                            changed = True

    if changed:
        save_portfolio(PORTFOLIO_FILE, portfolio)


@monitor_positions.before_loop
async def before_monitor():
    await client.wait_until_ready()


# ── Leaderboard Command ──────────────────────────────────────────────────────
@tree.command(name="leaderboard", description="Top 10 most profitable trades of all time")
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    portfolio = load_portfolio(PORTFOLIO_FILE)

    # Build entries from closed history
    entries = get_leaderboard(portfolio, limit=100)  # get more than 10 so we can inject open trades

    # Inject open positions with live P&L
    for user_id, positions in get_all_positions(portfolio).items():
        user = client.get_user(int(user_id))
        if user is None:
            try:
                user = await client.fetch_user(int(user_id))
                username = user.display_name
            except Exception:
                username = f"User {user_id}"
        else:
            username = user.display_name

        for pos in positions:
            price = await get_price(pos["ticker"], pos["asset_type"])
            if price is None:
                continue
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            entries.append({
                "username":    username,
                "ticker":      pos["ticker"],
                "asset_type":  pos["asset_type"],
                "direction":   pos["direction"],
                "leverage":    pos["leverage"],
                "entry_price": pos["entry_price"],
                "exit_price":  None,
                "pnl":         round(pnl, 4),
                "opened_at":   pos.get("opened_at", ""),
                "closed_at":   None,
                "status":      "open",
            })

    # Sort all entries by P&L and take top 10
    entries.sort(key=lambda x: x["pnl"], reverse=True)
    top = entries[:10]

    if not top:
        await interaction.followup.send("📭 No trades recorded yet.")
        return

    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    embed = discord.Embed(
        title="🏆 All-Time Top 10 Trades",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )

    for i, entry in enumerate(top):
        pnl = entry["pnl"]
        pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"
        direction_emoji = "📈" if entry["direction"] == "long" else "📉"
        status = "🟢 Open" if entry["status"] == "open" else "🔒 Closed"

        # Format opened_at date
        try:
            opened = datetime.fromisoformat(entry["opened_at"]).strftime("%b %d, %Y")
        except Exception:
            opened = "Unknown"

        embed.add_field(
            name=f"{medals[i]} #{i+1} — {entry['ticker']} {direction_emoji} {entry['leverage']}x",
            value=(
                f"**{pnl_str}** P&L\n"
                f"👤 {entry['username']}\n"
                f"Entry: ${entry['entry_price']:,.4f}\n"
                f"Opened: {opened}\n"
                f"{status}"
            ),
            inline=True
        )

    embed.set_footer(text="Ranked by leveraged P&L %")
    await interaction.followup.send(embed=embed)



# ── Price Alerts Helpers ──────────────────────────────────────────────────────
def load_price_alerts() -> list:
    if not os.path.exists(PRICE_ALERTS_FILE):
        return []
    with open(PRICE_ALERTS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_price_alerts(alerts: list) -> None:
    with open(PRICE_ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


# ── /mypositions ──────────────────────────────────────────────────────────────
@tree.command(name="mypositions", description="View only your own open positions (private)")
async def mypositions_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(interaction.user.id))
    if not positions:
        await interaction.response.send_message("📭 You have no open positions.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    lines = ["📊 **Your Open Positions**\n"]
    for i, pos in enumerate(positions):
        price = await get_price(pos["ticker"], pos["asset_type"])
        if price is None:
            price_display = "N/A"
            pnl_str = "N/A"
        else:
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"
            price_display = f"${price:,.4f}"
        direction_emoji = "📈" if pos["direction"] == "long" else "📉"
        lines.append(
            f"**#{i+1} {direction_emoji} {pos['ticker']}** — "
            f"{pos['direction'].upper()} {pos['leverage']}x\n"
            f"  Entry: ${pos['entry_price']:,.4f} | Current: {price_display} | "
            f"P&L: **{pnl_str}** | Liq: ${pos['liquidation_price']:,.4f}\n"
        )
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ── /history ──────────────────────────────────────────────────────────────────
@tree.command(name="history", description="View your closed trade history and realized P&L")
async def history_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    history = [h for h in portfolio.get("history", []) if h.get("user_id") == str(interaction.user.id)]
    if not history:
        await interaction.response.send_message("📭 You have no closed trades yet.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)
    total_pnl = sum(h["pnl"] for h in history)
    wins = sum(1 for h in history if h["pnl"] > 0)
    losses = len(history) - wins
    win_rate = (wins / len(history)) * 100

    embed = discord.Embed(
        title=f"📒 {interaction.user.display_name}'s Trade History",
        color=discord.Color.blurple(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(
        name="📊 Summary",
        value=(
            f"Total Trades: **{len(history)}**\n"
            f"Wins: **{wins}** | Losses: **{losses}**\n"
            f"Win Rate: **{win_rate:.1f}%**\n"
            f"Total Realized P&L: **{'+' if total_pnl >= 0 else ''}{total_pnl:.2f}%**"
        ),
        inline=False
    )
    for h in history[-10:]:  # show last 10 trades
        try:
            closed = datetime.fromisoformat(h["closed_at"]).strftime("%b %d, %Y")
        except Exception:
            closed = "Unknown"
        pnl_emoji = "🟢" if h["pnl"] >= 0 else "🔴"
        direction_emoji = "📈" if h["direction"] == "long" else "📉"
        embed.add_field(
            name=f"{direction_emoji} {h['ticker']} {h['direction'].upper()} {h['leverage']}x",
            value=(
                f"{pnl_emoji} **{'+' if h['pnl'] >= 0 else ''}{h['pnl']:.2f}%**\n"
                f"Entry: ${h['entry_price']:,.4f} → ${h['exit_price']:,.4f}\n"
                f"Closed: {closed}"
            ),
            inline=True
        )
    embed.set_footer(text="Showing last 10 closed trades")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /stats ────────────────────────────────────────────────────────────────────
@tree.command(name="stats", description="View trading stats for a user")
@app_commands.describe(user="The user to view stats for")
async def stats_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    portfolio = load_portfolio(PORTFOLIO_FILE)
    history = [h for h in portfolio.get("history", []) if h.get("user_id") == str(user.id)]
    open_positions = get_user_positions(portfolio, str(user.id))

    if not history and not open_positions:
        await interaction.followup.send(f"📭 **{user.display_name}** has no trading activity yet.")
        return

    # Closed trade stats
    total_closed = len(history)
    wins = sum(1 for h in history if h["pnl"] > 0)
    losses = total_closed - wins
    win_rate = (wins / total_closed * 100) if total_closed else 0
    total_realized = sum(h["pnl"] for h in history)
    best_trade = max(history, key=lambda x: x["pnl"]) if history else None
    worst_trade = min(history, key=lambda x: x["pnl"]) if history else None
    avg_leverage = sum(h["leverage"] for h in history) / total_closed if total_closed else 0

    # Open position stats
    total_unrealized = 0
    valid_open = 0
    for pos in open_positions:
        price = await get_price(pos["ticker"], pos["asset_type"])
        if price:
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            total_unrealized += pnl
            valid_open += 1

    embed = discord.Embed(
        title=f"📈 {user.display_name}'s Trading Stats",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.add_field(
        name="📊 All-Time Record",
        value=(
            f"Closed Trades: **{total_closed}**\n"
            f"Wins: **{wins}** | Losses: **{losses}**\n"
            f"Win Rate: **{win_rate:.1f}%**\n"
            f"Avg Leverage: **{avg_leverage:.1f}x**"
        ),
        inline=True
    )
    embed.add_field(
        name="💰 P&L",
        value=(
            f"Realized: **{'+' if total_realized >= 0 else ''}{total_realized:.2f}%**\n"
            f"Unrealized: **{'+' if total_unrealized >= 0 else ''}{total_unrealized:.2f}%** ({valid_open} open)\n"
        ),
        inline=True
    )
    if best_trade:
        embed.add_field(
            name="🚀 Best Trade",
            value=f"{best_trade['ticker']} {best_trade['direction'].upper()} {best_trade['leverage']}x\n**+{best_trade['pnl']:.2f}%**",
            inline=True
        )
    if worst_trade:
        embed.add_field(
            name="💀 Worst Trade",
            value=f"{worst_trade['ticker']} {worst_trade['direction'].upper()} {worst_trade['leverage']}x\n**{worst_trade['pnl']:.2f}%**",
            inline=True
        )
    await interaction.followup.send(embed=embed)


# ── /alert ────────────────────────────────────────────────────────────────────
alert_group = app_commands.Group(name="alert", description="Manage price alerts")

@alert_group.command(name="set", description="Set a price alert for a ticker")
@app_commands.describe(ticker="Ticker symbol e.g. BTC, AAPL", asset_type="crypto or stock", target_price="Price to alert at")
async def alert_set(interaction: discord.Interaction, ticker: str, asset_type: str, target_price: float):
    asset_type = asset_type.strip().lower()
    if asset_type not in ("crypto", "stock"):
        await interaction.response.send_message("❌ Asset type must be **crypto** or **stock**.", ephemeral=True)
        return

    alerts = load_price_alerts()
    alerts.append({
        "user_id":      str(interaction.user.id),
        "username":     interaction.user.display_name,
        "ticker":       ticker.upper(),
        "asset_type":   asset_type,
        "target_price": target_price,
        "created_at":   datetime.utcnow().isoformat(),
        "triggered":    False
    })
    save_price_alerts(alerts)
    await interaction.response.send_message(
        f"🔔 Alert set! I'll ping you in the channel when **{ticker.upper()}** hits **${target_price:,.4f}**.",
        ephemeral=True
    )

@alert_group.command(name="list", description="View your active price alerts")
async def alert_list(interaction: discord.Interaction):
    alerts = [a for a in load_price_alerts() if a["user_id"] == str(interaction.user.id) and not a["triggered"]]
    if not alerts:
        await interaction.response.send_message("📭 You have no active price alerts.", ephemeral=True)
        return
    lines = ["🔔 **Your Active Price Alerts**\n"]
    for i, a in enumerate(alerts):
        lines.append(f"**#{i+1}** {a['ticker']} — target: **${a['target_price']:,.4f}**")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@alert_group.command(name="remove", description="Remove one of your price alerts")
@app_commands.describe(index="Alert number from /alert list")
async def alert_remove(interaction: discord.Interaction, index: int):
    all_alerts = load_price_alerts()
    user_alerts = [a for a in all_alerts if a["user_id"] == str(interaction.user.id) and not a["triggered"]]
    if not user_alerts or index < 1 or index > len(user_alerts):
        await interaction.response.send_message("❌ Invalid alert number. Use /alert list to see yours.", ephemeral=True)
        return
    target = user_alerts[index - 1]
    all_alerts.remove(target)
    save_price_alerts(all_alerts)
    await interaction.response.send_message(
        f"✅ Removed alert for **{target['ticker']}** at **${target['target_price']:,.4f}**.", ephemeral=True
    )

tree.add_command(alert_group)


# ── Price Alert Monitor ───────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def monitor_price_alerts():
    alerts = load_price_alerts()
    if not alerts:
        return

    changed = False
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        return

    for alert in alerts:
        if alert.get("triggered"):
            continue
        price = await get_price(alert["ticker"], alert["asset_type"])
        if price is None:
            continue
        target = alert["target_price"]
        # Trigger if price crosses target in either direction
        if price >= target:
            try:
                await channel.send(
                    f"🔔 **Price Alert!** <@{alert['user_id']}> — "
                    f"**{alert['ticker']}** has hit your target of **${target:,.4f}**!\n"
                    f"Current price: **${price:,.4f}**"
                )
            except Exception:
                pass
            alert["triggered"] = True
            changed = True

    if changed:
        save_price_alerts(alerts)


# ── Weekly Recap ──────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def weekly_recap():
    now = datetime.now(timezone.utc)
    # Fire on Sunday at 8pm ET (UTC-4 in summer / UTC-5 in winter, use UTC 00:00 Monday ≈ Sunday 8pm ET)
    # We target Sunday = weekday 6, at 23:55-00:00 UTC (~ 7-8pm ET)
    if now.weekday() != 6 or now.hour != 23 or now.minute != 55:
        return

    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        return

    portfolio = load_portfolio(PORTFOLIO_FILE)
    history = portfolio.get("history", [])

    # Filter to trades closed this past week
    week_ago = now - timedelta(days=7)
    week_trades = []
    for h in history:
        try:
            closed_at = datetime.fromisoformat(h["closed_at"]).replace(tzinfo=timezone.utc)
            if closed_at >= week_ago:
                week_trades.append(h)
        except Exception:
            continue

    # Also include open positions with live P&L
    open_entries = []
    for user_id, positions in get_all_positions(portfolio).items():
        user = client.get_user(int(user_id))
        if user is None:
            try:
                user = await client.fetch_user(int(user_id))
                uname = user.display_name
            except Exception:
                uname = f"User {user_id}"
        else:
            uname = user.display_name
        for pos in positions:
            price = await get_price(pos["ticker"], pos["asset_type"])
            if price:
                pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
                open_entries.append({
                    "username": uname,
                    "user_id": user_id,
                    "ticker": pos["ticker"],
                    "direction": pos["direction"],
                    "leverage": pos["leverage"],
                    "pnl": pnl,
                    "status": "open"
                })

    all_week = week_trades + open_entries
    if not all_week:
        await channel.send("📊 **Weekly Recap** — No trades this week. Everyone's a coward. 🐔")
        return

    best = max(all_week, key=lambda x: x["pnl"])
    worst = min(all_week, key=lambda x: x["pnl"])

    # Most active trader (most closed trades this week)
    from collections import Counter
    trader_counts = Counter(h["username"] for h in week_trades)
    most_active = trader_counts.most_common(1)[0] if trader_counts else None

    # Highest leverage used
    all_leverages = week_trades + open_entries
    max_lev_trade = max(all_leverages, key=lambda x: x["leverage"]) if all_leverages else None

    embed = discord.Embed(
        title="📊 Weekly Trading Recap",
        description=f"Week ending {now.strftime('%B %d, %Y')}",
        color=discord.Color.gold(),
        timestamp=now
    )
    embed.add_field(
        name="🚀 Biggest Winner",
        value=f"**{best['username']}** — {best['ticker']} {best['direction'].upper()} {best['leverage']}x\n**{'+' if best['pnl'] >= 0 else ''}{best['pnl']:.2f}%**",
        inline=True
    )
    embed.add_field(
        name="💀 Biggest Loser",
        value=f"**{worst['username']}** — {worst['ticker']} {worst['direction'].upper()} {worst['leverage']}x\n**{'+' if worst['pnl'] >= 0 else ''}{worst['pnl']:.2f}%**",
        inline=True
    )
    if most_active:
        embed.add_field(
            name="🏃 Most Active",
            value=f"**{most_active[0]}** — {most_active[1]} trade(s) closed",
            inline=True
        )
    if max_lev_trade:
        embed.add_field(
            name="🎰 Most Degenerate",
            value=f"**{max_lev_trade['username']}** — {max_lev_trade['ticker']} at **{max_lev_trade['leverage']}x** leverage",
            inline=True
        )
    embed.add_field(
        name="📈 Total Activity",
        value=f"Closed trades this week: **{len(week_trades)}**\nOpen positions: **{len(open_entries)}**",
        inline=False
    )
    await channel.send(embed=embed)


# ── Bot Events ───────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    monitor_positions.start()
    monitor_price_alerts.start()
    weekly_recap.start()
    print(f"✅ Logged in as {client.user} | Monitoring every 5 minutes")


client.run(TOKEN)
