"""
price_fetcher.py
─────────────────
Free price sources:
  • Stocks / ETFs / Equities → Alpaca Markets API (free, includes after-hours)
  • Crypto                   → CoinGecko free public API (no API key)
"""

import asyncio
import aiohttp
import os
import time

# Simple in-memory price cache — avoids hammering APIs during testing
# and prevents rate limiting
_price_cache: dict = {}  # { "BTC:crypto": (price, timestamp) }
CACHE_TTL = 60  # seconds — reuse a cached price if it's less than 60s old

# Alpaca credentials — set these in Railway environment variables
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = "https://data.alpaca.markets/v2"

# CoinGecko: map common ticker symbols to their CoinGecko IDs
COINGECKO_ID_MAP = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "XRP":   "ripple",
    "ADA":   "cardano",
    "AVAX":  "avalanche-2",
    "DOGE":  "dogecoin",
    "DOT":   "polkadot",
    "MATIC": "matic-network",
    "LINK":  "chainlink",
    "LTC":   "litecoin",
    "UNI":   "uniswap",
    "ATOM":  "cosmos",
    "XLM":   "stellar",
    "BCH":   "bitcoin-cash",
    "ALGO":  "algorand",
    "FIL":   "filecoin",
    "VET":   "vechain",
    "ICP":   "internet-computer",
    "NEAR":  "near",
    "APT":   "aptos",
    "ARB":   "arbitrum",
    "OP":    "optimism",
    "SUI":   "sui",
    "TRX":   "tron",
    "TON":   "the-open-network",
    "SHIB":  "shiba-inu",
    "PEPE":  "pepe",
    "HYPE":  "hyperliquid",
    "WIF":   "dogwifcoin",
    "BONK":  "bonk",
    "JUP":   "jupiter-exchange-solana",
    "RENDER":"render-token",
    "SEI":   "sei-network",
    "TAO":   "bittensor",
    "INJ":   "injective-protocol",
    "FET":   "fetch-ai",
    "ONDO":  "ondo-finance",
}

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
FETCH_TIMEOUT  = 8   # seconds — hard cap per price fetch
HTTP_TIMEOUT   = aiohttp.ClientTimeout(total=6)


async def get_crypto_price(ticker: str) -> float | None:
    """Fetch crypto spot price in USD via CoinGecko free API."""
    coin_id = COINGECKO_ID_MAP.get(ticker.upper())

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            # If not in our map, try a search (slower — add to map above to avoid this)
            if coin_id is None:
                try:
                    url = f"{COINGECKO_BASE}/search?query={ticker}"
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            coins = data.get("coins", [])
                            if coins:
                                coin_id = coins[0]["id"]
                except Exception:
                    return None

            if coin_id is None:
                return None

            url = f"{COINGECKO_BASE}/simple/price?ids={coin_id}&vs_currencies=usd"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get(coin_id, {}).get("usd")
                elif resp.status == 429:
                    return None
    except Exception:
        return None

    return None


async def get_stock_price(ticker: str) -> float | None:
    """
    Fetch stock/ETF/equity price via Alpaca Markets API.
    Includes pre-market and after-hours prices (feed=iex covers extended hours).
    Falls back to latest trade if quote is unavailable.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print(f"[Alpaca] ERROR: API keys not set in environment variables")
        return None

    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            # Try latest quote first (includes ask/bid for after-hours)
            url = f"{ALPACA_BASE_URL}/stocks/{ticker}/quotes/latest?feed=iex"
            async with session.get(url, headers=headers) as resp:
                print(f"[Alpaca quote] {ticker} status={resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    quote = data.get("quote", {})
                    ask = quote.get("ap")
                    bid = quote.get("bp")
                    print(f"[Alpaca quote] {ticker} ask={ask} bid={bid}")
                    if ask and ask > 0:
                        return float(ask)
                    if bid and bid > 0:
                        return float(bid)
                elif resp.status == 403:
                    print(f"[Alpaca] 403 Forbidden — check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway variables")
                    return None
                elif resp.status == 422:
                    print(f"[Alpaca] 422 Unprocessable — ticker '{ticker}' may not be supported on IEX feed")
                    return None
                else:
                    body = await resp.text()
                    print(f"[Alpaca quote error] {ticker} status={resp.status}: {body}")

            # Fallback: latest trade price
            url = f"{ALPACA_BASE_URL}/stocks/{ticker}/trades/latest?feed=iex"
            async with session.get(url, headers=headers) as resp:
                print(f"[Alpaca trade] {ticker} status={resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get("trade", {}).get("p")
                    print(f"[Alpaca trade] {ticker} price={price}")
                    if price:
                        return float(price)
                else:
                    body = await resp.text()
                    print(f"[Alpaca trade error] {ticker} status={resp.status}: {body}")

    except Exception as e:
        print(f"[Alpaca exception] {ticker}: {e}")
        return None

    return None


async def get_price(ticker: str, asset_type: str) -> float | None:
    """
    Unified price fetch with in-memory cache and hard timeout.
    asset_type: 'crypto' or 'stock'
    Returns float (USD) or None if unavailable or timed out.
    """
    cache_key = f"{ticker.upper()}:{asset_type}"
    cached = _price_cache.get(cache_key)
    if cached:
        price, ts = cached
        if time.time() - ts < CACHE_TTL:
            return price  # return cached price, skip API call

    try:
        if asset_type == "crypto":
            price = await asyncio.wait_for(get_crypto_price(ticker), timeout=FETCH_TIMEOUT)
        else:
            price = await asyncio.wait_for(get_stock_price(ticker), timeout=FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        return None

    if price is not None:
        _price_cache[cache_key] = (price, time.time())

    return price
