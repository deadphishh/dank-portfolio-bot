"""
portfolio.py
────────────
JSON-based portfolio storage and calculation helpers.

File structure (portfolio.json):
{
  "positions": {
    "USER_ID": [ { ...position... } ]
  },
  "history": [
    {
      "user_id": "123456789",
      "username": "deadphish",
      "ticker": "BTC",
      "asset_type": "crypto",
      "direction": "long",
      "leverage": 10.0,
      "entry_price": 42000.0,
      "exit_price": 50000.0,
      "pnl": 190.48,
      "opened_at": "2024-01-01T00:00:00",
      "closed_at": "2024-01-10T00:00:00"
    }
  ]
}

NOTE: Old portfolio.json files used a flat { "USER_ID": [...] } structure.
load_portfolio() handles both formats transparently.
"""

import json
import os
from datetime import datetime


def load_portfolio(filepath: str) -> dict:
    """Load portfolio from JSON. Migrates old flat format automatically."""
    if not os.path.exists(filepath):
        return {"positions": {}, "history": []}
    with open(filepath, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"positions": {}, "history": []}

    # Migrate old flat format: { "USER_ID": [...] }
    if "positions" not in data:
        data = {"positions": data, "history": []}

    if "history" not in data:
        data["history"] = []

    # Sanitize any tickers that got saved with markdown chars
    for user_id, positions in data["positions"].items():
        for pos in positions:
            pos["ticker"] = pos["ticker"].replace("*", "").replace("`", "").replace("_", "")
    for entry in data["history"]:
        if "ticker" in entry:
            entry["ticker"] = entry["ticker"].replace("*", "").replace("`", "").replace("_", "")

    return data


def save_portfolio(filepath: str, portfolio: dict) -> None:
    """Save portfolio dict to JSON file."""
    with open(filepath, "w") as f:
        json.dump(portfolio, f, indent=2)


def add_position(portfolio: dict, user_id: str, position: dict) -> None:
    """Append a position to a user's open positions."""
    if user_id not in portfolio["positions"]:
        portfolio["positions"][user_id] = []
    portfolio["positions"][user_id].append(position)


def close_position(portfolio: dict, user_id: str, index: int,
                   exit_price: float, username: str) -> dict | None:
    """
    Remove position at index from open positions, record it in history.
    Returns the closed position dict or None if index is invalid.
    """
    positions = portfolio["positions"].get(user_id, [])
    if index < 0 or index >= len(positions):
        return None

    pos = positions.pop(index)
    pnl = calculate_pnl(pos["entry_price"], exit_price, pos["leverage"], pos["direction"])

    history_entry = {
        "user_id":    user_id,
        "username":   username,
        "ticker":     pos["ticker"],
        "asset_type": pos["asset_type"],
        "direction":  pos["direction"],
        "leverage":   pos["leverage"],
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "pnl":        round(pnl, 4),
        "opened_at":  pos.get("opened_at", ""),
        "closed_at":  datetime.utcnow().isoformat(),
        "partial":    False,
    }
    portfolio["history"].append(history_entry)
    return pos


def partial_close_position(portfolio: dict, user_id: str, index: int,
                           exit_price: float, username: str,
                           units_sold: float, total_units: float) -> dict | None:
    """
    Partially close a position by reducing its size.
    units_sold / total_units determines the fraction closed.
    Records the partial close in history and updates the remaining position.
    Returns a dict with partial close details or None if invalid.
    """
    positions = portfolio["positions"].get(user_id, [])
    if index < 0 or index >= len(positions):
        return None

    pos = positions[index]
    fraction = units_sold / total_units
    pnl = calculate_pnl(pos["entry_price"], exit_price, pos["leverage"], pos["direction"])

    # Scale margin and size by fraction sold
    orig_margin = pos.get("margin", pos.get("size", 0) / pos["leverage"])
    orig_size   = pos.get("size", orig_margin * pos["leverage"])
    sold_margin = round(orig_margin * fraction, 4)
    sold_size   = round(orig_size * fraction, 4)

    history_entry = {
        "user_id":     user_id,
        "username":    username,
        "ticker":      pos["ticker"],
        "asset_type":  pos["asset_type"],
        "direction":   pos["direction"],
        "leverage":    pos["leverage"],
        "entry_price": pos["entry_price"],
        "exit_price":  exit_price,
        "pnl":         round(pnl, 4),
        "opened_at":   pos.get("opened_at", ""),
        "closed_at":   datetime.utcnow().isoformat(),
        "partial":     True,
        "fraction":    round(fraction, 4),
        "sold_size":   sold_size,
    }
    portfolio["history"].append(history_entry)

    # Update remaining position size/margin
    remaining_fraction = 1 - fraction
    pos["margin"] = round(orig_margin * remaining_fraction, 4)
    pos["size"]   = round(orig_size * remaining_fraction, 4)

    return history_entry


def get_user_positions(portfolio: dict, user_id: str) -> list:
    """Return the list of open positions for a user."""
    return portfolio["positions"].get(user_id, [])


def get_all_positions(portfolio: dict) -> dict:
    """Return all open positions keyed by user_id."""
    return portfolio["positions"]


def get_leaderboard(portfolio: dict, limit: int = 10) -> list:
    """
    Return top N trades by P&L % combining:
    - Closed trades from history (final P&L recorded at close)
    - Open trades (current P&L must be injected by caller since it needs live prices)

    Returns list of dicts sorted by pnl descending.
    Each entry has: username, ticker, asset_type, direction, leverage,
                    entry_price, exit_price (or None), pnl, opened_at, closed_at (or None)
    """
    entries = []

    # Closed trades
    for h in portfolio.get("history", []):
        entries.append({
            "username":   h.get("username", "Unknown"),
            "ticker":     h["ticker"],
            "asset_type": h["asset_type"],
            "direction":  h["direction"],
            "leverage":   h["leverage"],
            "entry_price": h["entry_price"],
            "exit_price": h.get("exit_price"),
            "pnl":        h["pnl"],
            "opened_at":  h.get("opened_at", ""),
            "closed_at":  h.get("closed_at"),
            "status":     "closed",
        })

    # Open trades are added by the caller (bot.py) with live P&L injected
    entries.sort(key=lambda x: x["pnl"], reverse=True)
    return entries[:limit]


def calculate_pnl(entry_price: float, current_price: float,
                  leverage: float, direction: str) -> float:
    """
    Calculate leveraged P&L as a percentage.
    Long:  ((current - entry) / entry) * leverage * 100
    Short: ((entry - current) / entry) * leverage * 100
    """
    if entry_price == 0:
        return 0.0
    if direction.strip().lower() == "long":
        return ((current_price - entry_price) / entry_price) * leverage * 100
    else:
        return ((entry_price - current_price) / entry_price) * leverage * 100


def calculate_liquidation_price(entry_price: float, leverage: float,
                                direction: str) -> float:
    """
    Estimate liquidation price (simplified, no funding/fees).
    Long:  entry * (1 - 1/leverage)
    Short: entry * (1 + 1/leverage)
    """
    if leverage <= 0:
        return 0.0
    if direction.strip().lower() == "long":
        return round(entry_price * (1 - 1 / leverage), 6)
    else:
        return round(entry_price * (1 + 1 / leverage), 6)
