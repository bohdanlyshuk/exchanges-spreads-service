"""
MEXC Futures WebSocket feed (stub).
Subscribe to tickers via wss://contract.mexc.com/edge, method sub.tickers.
Real message format may differ; verify with debug log on first run.
"""
import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from .base import ExchangePrices, to_canonical_symbol

logger = logging.getLogger(__name__)

WS_URL = "wss://contract.mexc.com/edge"
RECONNECT_DELAY_SEC = 5
PING_INTERVAL_SEC = 15


class MexcWSFeed:
    """
    MEXC Futures WebSocket ticker feed.
    Subscribes to sub.tickers; parses push.tickers (symbol format BTC_USDT).
    """

    def __init__(
        self,
        reconnect_delay: float = RECONNECT_DELAY_SEC,
        ping_interval: float = PING_INTERVAL_SEC,
    ):
        self._prices: dict[str, dict[str, Any]] = {}
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._reconnect_delay = reconnect_delay
        self._ping_interval = ping_interval
        self._last_msg_ts: float = 0
        self._connected = False

    async def start(self) -> None:
        self._running = True
        self._ws_task = asyncio.create_task(self._run_ws_loop())
        logger.info("MexcWSFeed: started")

    async def stop(self) -> None:
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        self._connected = False
        logger.info("MexcWSFeed: stopped")

    async def _run_ws_loop(self) -> None:
        while self._running:
            try:
                await self._connect_ws()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("MexcWSFeed: WS disconnected (%s), reconnecting in %.0fs", e, self._reconnect_delay)
                self._connected = False
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_ws(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                WS_URL,
                heartbeat=self._ping_interval,
                receive_timeout=60,
            ) as ws:
                self._connected = True
                await ws.send_json({"method": "sub.tickers", "param": {}, "gzip": False})
                logger.info("MexcWSFeed: connected to %s, subscribed to tickers", WS_URL)
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._last_msg_ts = time.time()
                        self._handle_message(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        logger.warning("MexcWSFeed: WS %s", msg.type)
                        break

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            channel = data.get("channel") or data.get("c") or ""
            if "ticker" not in channel.lower():
                return
            # push.tickers may send list or single object
            items = data.get("data") or data.get("dataList") or []
            if isinstance(items, dict):
                items = [items]
            for ticker in items:
                if not isinstance(ticker, dict):
                    continue
                symbol_raw = ticker.get("symbol") or ticker.get("s") or ""
                symbol = to_canonical_symbol(symbol_raw)
                if not symbol.endswith("USDT"):
                    continue
                last = str(ticker.get("lastPrice") or ticker.get("last") or ticker.get("p") or "0")
                bid = str(ticker.get("bid1") or ticker.get("maxBidPrice") or ticker.get("b") or last)
                ask = str(ticker.get("ask1") or ticker.get("minAskPrice") or ticker.get("a") or last)
                mark = str(ticker.get("fairPrice") or ticker.get("markPrice") or ticker.get("mark") or last)
                funding = str(ticker.get("fundingRate") or ticker.get("funding_rate") or "0")
                self._prices[symbol] = {
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "mark": mark,
                    "funding_rate": funding,
                }
        except Exception as e:
            logger.debug("MexcWSFeed: parse error %s", e)

    def get_prices(self) -> dict[str, ExchangePrices]:
        """Return current prices as symbol -> ExchangePrices."""
        return {
            sym: ExchangePrices(
                exchange="mexc",
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
