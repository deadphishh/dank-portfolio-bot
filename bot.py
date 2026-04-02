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
    get_user_positions, get_all_positions,
    get_leaderboard, calculate_pnl,
    calculate_liquidation_price
)

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALERT_CHANNEL_ID   = int(os.getenv("DISCORD_ALERT_CHANNEL_ID", "0"))  # ID of your #alerts channel
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
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
            f"Ticker:      {ticker_val} ({asset_type_val})\n"
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

    await interaction.response.defer()
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
                f"**#{counter} {direction_emoji} {pos['ticker']}** ({pos['asset_type']}) — "
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


# ── Roast Command ────────────────────────────────────────────────────────────
@tree.command(name="roast", description="Roast another user's positions with AI")
@app_commands.describe(user="The user you want to roast")
async def roast_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()

    if not ANTHROPIC_API_KEY:
        await interaction.followup.send("❌ ANTHROPIC_API_KEY not set in environment variables.")
        return

    portfolio = load_portfolio(PORTFOLIO_FILE)
    positions = get_user_positions(portfolio, str(user.id))

    if not positions:
        await interaction.followup.send(
            f"📭 **{user.display_name}** has no open positions to roast. "
            f"They're too scared to even be in the market. 🐔"
        )
        return

    # Fetch live prices and build position summary for Claude
    position_lines = []
    for pos in positions:
        price = await get_price(pos["ticker"], pos["asset_type"])
        if price:
            pnl = calculate_pnl(pos["entry_price"], price, pos["leverage"], pos["direction"])
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl:.2f}%"
            liq = pos["liquidation_price"]
            liq_distance = abs((price - liq) / price * 100) if liq else None
            liq_str = f"{liq_distance:.1f}% from liquidation" if liq_distance else "unknown liq distance"
            position_lines.append(
                f"- {pos['ticker']} {pos['direction'].upper()} {pos['leverage']}x | "
                f"Entry: ${pos['entry_price']:,.4f} | Current: ${price:,.4f} | "
                f"P&L: {pnl_str} | Liq: ${liq:,.4f} ({liq_str})"
            )
        else:
            position_lines.append(
                f"- {pos['ticker']} {pos['direction'].upper()} {pos['leverage']}x | "
                f"Entry: ${pos['entry_price']:,.4f} | Price unavailable"
            )

    positions_text = "\n".join(position_lines)
    username = user.display_name

    prompt = f"""You are the most unhinged, degenerate trading roast bot on the internet. You talk like a terminally online crypto degen who has blown up 6 accounts, been liquidated on Christmas morning, and still thinks they're a genius. You roast with the energy of a group chat where everyone talks shit to each other because they're close friends — go hard, be ruthless, be filthy. Use heavy profanity freely — fuck, shit, ass, bastard, dickhead, dipshit, etc. Use them naturally and often. Use trading slang, memes, and pure savagery. Reference their specific positions, leverage, liquidation prices, and P&L to make it personal and cutting. If they're close to liquidation, absolutely destroy them for it. If they're up, accuse them of being a lucky idiot about to blow it. If they're down, pile on mercilessly. Be creative, be vile, be funny as hell. 5-6 sentences max. Go absolutely unhinged.

{username}'s positions:
{positions_text}

Destroy them:"""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    roast_text = data["content"][0]["text"].strip()
                    await interaction.followup.send(
                        f"🤡 **Roasting {user.mention}...**\n\n{roast_text}"
                    )
                else:
                    body = await resp.text()
                    await interaction.followup.send(f"❌ Claude API error: {resp.status}")
                    print(f"[Roast API error] {resp.status}: {body}")
    except Exception as e:
        await interaction.followup.send("❌ Failed to generate roast. Claude is probably also losing money.")
        print(f"[Roast exception] {e}")


# ── Leaderboard Command ──────────────────────────────────────────────────────
@tree.command(name="leaderboard", description="Top 10 most profitable trades of all time")
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
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
                f"**{pnl_str}** P&L
"
                f"👤 {entry['username']}
"
                f"Entry: ${entry['entry_price']:,.4f}
"
                f"Opened: {opened}
"
                f"{status}"
            ),
            inline=True
        )

    embed.set_footer(text="Ranked by leveraged P&L %")
    await interaction.followup.send(embed=embed)


# ── Bot Events ───────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    monitor_positions.start()
    print(f"✅ Logged in as {client.user} | Monitoring every 5 minutes")


client.run(TOKEN)
