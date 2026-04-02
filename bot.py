import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import asyncio
from datetime import datetime
from price_fetcher import get_price
from portfolio import (
    load_portfolio, save_portfolio,
    add_position, close_position,
    get_user_positions, calculate_pnl,
    calculate_liquidation_price
)

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALERT_CHANNEL_ID = int(os.getenv("DISCORD_ALERT_CHANNEL_ID", "0"))  # ID of your #alerts channel
PORTFOLIO_FILE = "portfolio.json"

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
        label="Leverage (1 = spot, 10 = 10x)",
        placeholder="e.g. 1, 5, 10, 20",
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Defer immediately — Discord requires a response within 3 seconds,
        # and price fetching can take longer than that.
        await interaction.response.defer()

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
            if entry <= 0 or lev < 1:
                raise ValueError
        except ValueError:
            await interaction.followup.send(
                "❌ Entry price must be a positive number and leverage must be ≥ 1.", ephemeral=True
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
            f"Ticker:      {ticker_val} ({asset_type_val})\n"
            f"Direction:   {direction_val.upper()} {lev}x\n"
            f"Entry Price: ${entry:,.4f}\n"
            f"Liq. Price:  ${liq_price:,.4f}\n"
            f"Current:     ${price:,.4f}\n"
            f"```\n"
            f"Good luck, faggot 🎰"
        )


# ── Slash Commands ───────────────────────────────────────────────────────────
@tree.command(name="add-position", description="Open a new long or short position")
async def add_position_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(AddPositionModal())


@tree.command(name="positions", description="View all your open positions")
async def positions_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(interaction.user.id))
    if not positions:
        await interaction.response.send_message("📭 You have no open positions.")
        return

    await interaction.response.defer()
    lines = [f"📊 **{interaction.user.display_name}'s Open Positions**\n"]

    for i, pos in enumerate(positions):
        price = await get_price(pos["ticker"], pos["asset_type"])
        if price is None:
            price_str = "N/A"
            pnl_str = "N/A"
        else:
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"

        direction_emoji = "📈" if pos["direction"] == "long" else "📉"
        lines.append(
            f"**#{i+1} {direction_emoji} {pos['ticker']}** ({pos['asset_type']}) — "
            f"{pos['direction'].upper()} {pos['leverage']}x\n"
            f"  Entry: ${pos['entry_price']:,.4f} | Current: ${price:,.4f} | "
            f"P&L: **{pnl_str}** | Liq: ${pos['liquidation_price']:,.4f}\n"
        )

    await interaction.followup.send("\n".join(lines))


@tree.command(name="portfolio", description="View a detailed portfolio summary with P&L")
async def portfolio_cmd(interaction: discord.Interaction):
    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(interaction.user.id))
    if not positions:
        await interaction.response.send_message("📭 You have no open positions.")
        return

    await interaction.response.defer()

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
            name=f"#{i+1} {direction_emoji} {pos['ticker']} ({pos['asset_type']})",
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

    await interaction.response.defer()
    pos = positions[idx]
    price = await get_price(pos["ticker"], pos["asset_type"])

    close_position(portfolio, str(interaction.user.id), idx)
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

    for user_id, positions in portfolio.items():
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


# ── Bot Events ───────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    monitor_positions.start()
    print(f"✅ Logged in as {client.user} | Monitoring every 5 minutes")


client.run(TOKEN)
