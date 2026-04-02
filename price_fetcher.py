"""
price_fetcher.py
─────────────────
Free price sources:
  • Stocks / ETFs / Equities → yfinance (Yahoo Finance, no API key)
  • Crypto                   → CoinGecko free public API (no API key)
"""

import asyncio
import aiohttp
import yfinance as yf
from functools import lru_cache

# CoinGecko: map common ticker symbols to their CoinGecko IDs
# This covers the most popular coins; unknown tickers fall back to a search.
COINGECKO_ID_MAP = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "BNB":  "binancecoin",
    "XRP":  "ripple",
    "ADA":  "cardano",
    "AVAX": "avalanche-2",
    "DOGE": "dogecoin",
    "DOT":  "polkadot",
    "MATIC":"matic-network",
    "LINK": "chainlink",
    "LTC":  "litecoin",
    "UNI":  "uniswap",
    "ATOM": "cosmos",
    "XLM":  "stellar",
    "BCH":  "bitcoin-cash",
    "ALGO": "algorand",
    "FIL":  "filecoin",
    "VET":  "vechain",
    "ICP":  "internet-computer",
    "NEAR": "near",
    "APT":  "aptos",
    "ARB":  "arbitrum",
    "OP":   "optimism",
    "SUI":  "sui",
    "TRX":  "tron",
    "TON":  "the-open-network",
    "SHIB": "shiba-inu",
    "PEPE": "pepe",
}

COINGECKO_BASE = "https://api.coingecko.com/api/v3"


async def get_crypto_price(ticker: str) -> float | None:
    """Fetch crypto spot price in USD via CoinGecko free API."""
    coin_id = COINGECKO_ID_MAP.get(ticker.upper())

    async with aiohttp.ClientSession() as session:
        # If not in our map, try to search for it
        if coin_id is None:
            try:
                url = f"{COINGECKO_BASE}/search?query={ticker}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        coins = data.get("coins", [])
                        if coins:
                            coin_id = coins[0]["id"]
            except Exception:
                return None

        if coin_id is None:
            return None

        try:
            url = f"{COINGECKO_BASE}/simple/price?ids={coin_id}&vs_currencies=usd"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get(coin_id, {}).get("usd")
        except Exception:
            return None

    return None


async def get_stock_price(ticker: str) -> float | None:
    """Fetch stock/ETF/equity price via yfinance (Yahoo Finance, free, no key)."""
    def _fetch():
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = getattr(info, "last_price", None)
            if price is None:
                hist = t.history(period="1d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            return price
        except Exception:
            return None

    # Run the blocking yfinance call in a thread pool
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def get_price(ticker: str, asset_type: str) -> float | None:
    """
    Unified price fetch.
    asset_type: 'crypto' or 'stock'
    Returns float (USD) or None if unavailable.
    """
    if asset_type == "crypto":
        return await get_crypto_price(ticker)
    else:
        return await get_stock_price(ticker)
