"""
portfolio.py
────────────
JSON-based portfolio storage and calculation helpers.

File structure (portfolio.json):
{
  "USER_ID": [
    {
      "ticker": "BTC",
      "asset_type": "crypto",
      "direction": "long",
      "entry_price": 42000.0,
      "leverage": 10.0,
      "liquidation_price": 37800.0,
      "opened_at": "2024-01-01T00:00:00",
      "alerted_milestones": ["+10", "-10"],
      "alerted_liq_levels": [80]
    },
    ...
  ],
  ...
}
"""

import json
import os
from typing import Any


def load_portfolio(filepath: str) -> dict:
    """Load portfolio from JSON. Returns empty dict if file doesn't exist."""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_portfolio(filepath: str, portfolio: dict) -> None:
    """Save portfolio dict to JSON file."""
    with open(filepath, "w") as f:
        json.dump(portfolio, f, indent=2)


def add_position(portfolio: dict, user_id: str, position: dict) -> None:
    """Append a position to a user's list."""
    if user_id not in portfolio:
        portfolio[user_id] = []
    portfolio[user_id].append(position)


def close_position(portfolio: dict, user_id: str, index: int) -> dict | None:
    """Remove and return the position at the given index. Returns None if invalid."""
    positions = portfolio.get(user_id, [])
    if 0 <= index < len(positions):
        return positions.pop(index)
    return None


def get_user_positions(portfolio: dict, user_id: str) -> list:
    """Return the list of open positions for a user."""
    return portfolio.get(user_id, [])


def calculate_pnl(entry_price: float, current_price: float, leverage: float, direction: str) -> float:
    """
    Calculate leveraged P&L as a percentage.

    Long:  ((current - entry) / entry) * leverage * 100
    Short: ((entry - current) / entry) * leverage * 100
    """
    if entry_price == 0:
        return 0.0
    if direction == "long":
        return ((current_price - entry_price) / entry_price) * leverage * 100
    else:  # short
        return ((entry_price - current_price) / entry_price) * leverage * 100


def calculate_liquidation_price(entry_price: float, leverage: float, direction: str) -> float:
    """
    Estimate liquidation price (simplified, no funding/fees).

    Long:  entry * (1 - 1/leverage)
    Short: entry * (1 + 1/leverage)

    At 1x leverage, longs liquidate at 0 (no liquidation in practice).
    """
    if leverage <= 0:
        return 0.0
    if direction == "long":
        return round(entry_price * (1 - 1 / leverage), 6)
    else:
        return round(entry_price * (1 + 1 / leverage), 6)
