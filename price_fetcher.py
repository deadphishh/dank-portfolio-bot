"""
price_fetcher.py
─────────────────
All prices via Alpaca Markets free API:
  • Stocks / ETFs    → /v2/stocks/{ticker}/trades/latest
  • Crypto           → /v1beta3/crypto/us/latest/trades

Includes 60-second in-memory cache and 8-second hard timeout.
"""

import asyncio
import aiohttp
import os
import time

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

STOCK_BASE  = "https://data.alpaca.markets/v2"
CRYPTO_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"

FETCH_TIMEOUT = 8
HTTP_TIMEOUT  = aiohttp.ClientTimeout(total=6)

_price_cache: dict = {}
CACHE_TTL = 60  # seconds


def _headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }


async def get_stock_price(ticker: str) -> float | None:
    """Fetch stock/ETF price via Alpaca — includes after-hours (SIP feed, IEX fallback)."""
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            for feed in ("sip", "iex"):
                url = f"{STOCK_BASE}/stocks/{ticker}/trades/latest?feed={feed}"
                async with session.get(url, headers=_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = data.get("trade", {}).get("p")
                        if price and float(price) > 0:
                            return float(price)
                    elif resp.status == 403 and feed == "sip":
                        continue  # no SIP access, try IEX
                    elif resp.status == 403:
                        print(f"[Alpaca] 403 Forbidden for {ticker} — check API keys")
                        return None
                    elif resp.status == 422:
                        print(f"[Alpaca] 422 — ticker '{ticker}' not supported")
                        return None
    except Exception as e:
        print(f"[Alpaca stock exception] {ticker}: {e}")
    return None


async def get_crypto_price(ticker: str) -> float | None:
    """Fetch crypto price via Alpaca crypto API."""
    # Alpaca crypto uses pair format e.g. BTC/USD
    pair = f"{ticker}/USD"
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            url = f"{CRYPTO_BASE}/latest/trades?symbols={pair}"
            async with session.get(url, headers=_headers()) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    trades = data.get("trades", {})
                    trade = trades.get(pair, {})
                    price = trade.get("p")
                    if price and float(price) > 0:
                        return float(price)
                elif resp.status == 403:
                    print(f"[Alpaca] 403 Forbidden for crypto {ticker} — check API keys")
                elif resp.status == 422:
                    print(f"[Alpaca] 422 — crypto ticker '{ticker}' not supported")
                else:
                    body = await resp.text()
                    print(f"[Alpaca crypto] {ticker} status={resp.status}: {body[:100]}")
    except Exception as e:
        print(f"[Alpaca crypto exception] {ticker}: {e}")
    return None


async def get_price(ticker: str, asset_type: str) -> float | None:
    """
    Unified price fetch with cache and hard timeout.
    asset_type: 'crypto' or 'stock'
    """
    cache_key = f"{ticker.upper()}:{asset_type}"
    cached = _price_cache.get(cache_key)
    if cached:
        price, ts = cached
        if time.time() - ts < CACHE_TTL:
            return price

    try:
        if asset_type == "crypto":
            price = await asyncio.wait_for(get_crypto_price(ticker), timeout=FETCH_TIMEOUT)
        else:
            price = await asyncio.wait_for(get_stock_price(ticker), timeout=FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[Alpaca timeout] {ticker}")
        return None

    if price is not None:
        _price_cache[cache_key] = (price, time.time())

    return price
