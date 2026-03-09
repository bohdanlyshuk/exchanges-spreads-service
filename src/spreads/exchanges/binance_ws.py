"""
Binance USDS-M Futures WebSocket feed.
Uses !bookTicker for real-time bid/ask; funding + mark via REST every N minutes.
Avoids REST rate limits (418) when used instead of REST polling.
"""
import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from .base import ExchangePrices

logger = logging.getLogger(__name__)

# All symbols bookTicker stream (best bid/ask)
WS_BOOK_TICKER = "wss://fstream.binance.com/ws/!bookTicker"
FUNDING_REST_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
RECONNECT_DELAY_SEC = 5
PING_INTERVAL_SEC = 20
FUNDING_REFRESH_INTERVAL_SEC = 600  # 10 min


class BinanceWSFeed:
    """
    Subscribes to Binance Futures !bookTicker for real-time bid/ask.
    Refreshes funding rate and mark price via REST periodically.
    """

    def __init__(
        self,
        reconnect_delay: float = RECONNECT_DELAY_SEC,
        ping_interval: float = PING_INTERVAL_SEC,
        funding_interval: float = FUNDING_REFRESH_INTERVAL_SEC,
    ):
        self._prices: dict[str, dict[str, Any]] = {}
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._funding_task: asyncio.Task | None = None
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._funding_interval = funding_interval
        self._last_msg_ts: float = 0
        self._connected = False

    async def start(self) -> None:
        self._running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        self._funding_task = asyncio.create_task(self._run_funding_loop())
        logger.info("BinanceWSFeed: started")

    async def stop(self) -> None:
        self._running = False
        for t in (self._ws_task, self._funding_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._ws_task = None
        self._funding_task = None
        self._connected = False
        logger.info("BinanceWSFeed: stopped")

    async def _run_ws_loop(self) -> None:
        while self._running:
            try:
                await self._connect_ws()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BinanceWSFeed: WS disconnected (%s), reconnecting in %.0fs", e, self._reconnect_delay)
                self._connected = False
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_ws(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                WS_BOOK_TICKER,
                heartbeat=self._ping_interval,
                receive_timeout=60,
            ) as ws:
                self._connected = True
                logger.info("BinanceWSFeed: connected to %s", WS_BOOK_TICKER)
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._last_msg_ts = time.time()
                        self._handle_book_ticker(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        logger.warning("BinanceWSFeed: WS %s", msg.type)
                        break

    def _handle_book_ticker(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            # Combined stream wrap: {"stream":"!bookTicker","data":{...}}
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            if not isinstance(data, dict):
                return
            symbol = (data.get("s") or "").strip()
            if not symbol or not symbol.endswith("USDT"):
                return
            bid = data.get("b") or data.get("lastPrice") or "0"
            ask = data.get("a") or data.get("lastPrice") or "0"
            entry = self._prices.get(symbol, {})
            entry["bid"] = bid
            entry["ask"] = ask
            entry["last"] = ask
            entry.setdefault("mark", ask)
            entry.setdefault("funding_rate", "0")
            self._prices[symbol] = entry
        except Exception as e:
            logger.debug("BinanceWSFeed: parse error %s", e)

    async def _run_funding_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_funding()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("BinanceWSFeed: funding refresh failed: %s", e)
            await asyncio.sleep(self._funding_interval)

    async def _refresh_funding(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    FUNDING_REST_URL,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; SpreadsBot/1.0)"},
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
        except Exception as e:
            logger.debug("BinanceWSFeed: funding request error %s", e)
            return
        if not isinstance(data, list):
            return
        for item in data:
            sym = (item.get("symbol") or "").strip()
            if not sym or sym not in self._prices:
                continue
            self._prices[sym]["mark"] = str(item.get("markPrice") or self._prices[sym].get("mark", "0"))
            self._prices[sym]["funding_rate"] = str(item.get("lastFundingRate") or "0")

    def get_prices(self) -> dict[str, ExchangePrices]:
        """Return current prices as symbol -> ExchangePrices for merge with REST."""
        return {
            sym: ExchangePrices(
                exchange="binance",
                bid=entry.get("bid", "0"),
                ask=entry.get("ask", "0"),
                last=entry.get("last", "0"),
                mark=entry.get("mark", "0"),
                funding=entry.get("funding_rate", "0"),
            )
            for sym, entry in list(self._prices.items())
        }

    @property
    def symbols_count(self) -> int:
        return len(self._prices)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_msg_age_sec(self) -> float:
        if self._last_msg_ts <= 0:
            return -1
        return time.time() - self._last_msg_ts
