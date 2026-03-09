try:
    import uvloop
    uvloop.install()
except (ImportError, OSError):
    pass

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import Settings
from .exchanges import (
    fetch_all_prices_binance,
    fetch_all_prices_bingx,
    fetch_all_prices_bitget,
    fetch_all_prices_bybit,
    fetch_all_prices_gate,
    fetch_all_prices_kucoin,
    fetch_all_prices_mexc,
    fetch_all_symbols_binance,
    fetch_all_symbols_bingx,
    fetch_all_symbols_bitget,
    fetch_all_symbols_bybit,
    fetch_all_symbols_gate,
    fetch_all_symbols_kucoin,
    fetch_all_symbols_mexc,
)
from .exchanges.binance_ws import BinanceWSFeed
from .exchanges.mexc_ws import MexcWSFeed
from .middleware import setup_middleware
from .models import ExchangePrice, PricesResponse
from .services import compute_spreads
from .utils import to_decimal_str

settings = Settings()

# All supported exchanges: (name, discovery_fn), (name, price_fn). Filter by ENABLED_EXCHANGES.
_ALL_DISCOVERY = [
    ("bybit", fetch_all_symbols_bybit),
    ("binance", fetch_all_symbols_binance),
    ("mexc", fetch_all_symbols_mexc),
    ("gate", fetch_all_symbols_gate),
    ("kucoin", fetch_all_symbols_kucoin),
    ("bingx", fetch_all_symbols_bingx),
    ("bitget", fetch_all_symbols_bitget),
]
_ALL_BULK_PRICES = [
    ("bybit", fetch_all_prices_bybit),
    ("binance", fetch_all_prices_binance),
    ("mexc", fetch_all_prices_mexc),
    ("gate", fetch_all_prices_gate),
    ("kucoin", fetch_all_prices_kucoin),
    ("bingx", fetch_all_prices_bingx),
    ("bitget", fetch_all_prices_bitget),
]
_enabled = {s.strip().lower() for s in (settings.enabled_exchanges or "").split(",") if s.strip()}
if _enabled:
    DISCOVERY_FETCHERS = [(n, f) for n, f in _ALL_DISCOVERY if n in _enabled]
    BULK_PRICE_FETCHERS = [(n, f) for n, f in _ALL_BULK_PRICES if n in _enabled]
else:
    DISCOVERY_FETCHERS = _ALL_DISCOVERY
    BULK_PRICE_FETCHERS = _ALL_BULK_PRICES

# REST-only fetchers (exclude binance/mexc when WS feed is used)
def _rest_bulk_fetchers():
    rest = []
    for n, f in BULK_PRICE_FETCHERS:
        if n == "binance" and getattr(settings, "ws_binance_enabled", False):
            continue
        if n == "mexc" and getattr(settings, "ws_mexc_enabled", False):
            continue
        rest.append((n, f))
    return rest

if not logging.root.handlers:
    lvl = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
for _name in ("httpcore", "httpx"):
    logging.getLogger(_name).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

def _parse_ts(s: Optional[str], default: datetime) -> datetime:
    if not s or not str(s).strip():
        return default
    s = str(s).strip()
    # ISO 8601 or Unix ms
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    if s.upper().endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_response(symbol: str, prices: list[ExchangePrice], errors: list[dict]) -> PricesResponse:
    arb, pairwise = compute_spreads(prices)
    return PricesResponse(
        symbol=symbol,
        prices=prices,
        arbitrage=arb,
        pairwise_spreads=pairwise,
        errors=errors,
    )


async def _broadcast_prices(app: FastAPI) -> None:
    cache = getattr(app.state, "prices_cache", None) or {}
    if not cache:
        return
    clients = getattr(app.state, "ws_clients", None) or set()
    if not clients:
        return
    interval = getattr(settings, "ws_broadcast_interval_sec", 0.0) or 0.0
    if interval > 0:
        last = getattr(app.state, "_ws_last_broadcast_ts", 0.0)
        if time.time() - last < interval:
            return
    sorted_list = sorted(cache.values(), key=lambda r: abs(r.arbitrage.spread_pct), reverse=True)
    payload = [r.model_dump(mode="json") for r in sorted_list]
    payload_json = json.dumps(payload)
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_text(payload_json)
        except Exception:
            dead.add(ws)
    for ws in dead:
        clients.discard(ws)
    app.state._ws_last_broadcast_ts = time.time()


async def _price_update_loop(app: FastAPI) -> None:
    timeout = float(settings.http_timeout)
    interval = max(0.1, settings.price_update_interval)
    rest_fetchers = _rest_bulk_fetchers()
    while True:
        try:
            symbols = [s for s in (getattr(app.state, "symbols", None) or []) if s and str(s).strip()]
            if not symbols:
                await asyncio.sleep(interval)
                continue
            logger.debug("Price update: bulk fetch for %d symbols...", len(symbols))
            t0 = time.perf_counter()
            results = await asyncio.gather(
                *[f(timeout) for _, f in rest_fetchers],
                return_exceptions=True,
            )
            by_name: dict[str, tuple[str | None, dict]] = {}
            for (name, _), r in zip(rest_fetchers, results):
                if isinstance(r, BaseException):
                    by_name[name] = (str(r), {})
                else:
                    by_name[name] = (None, r)
            # Merge WebSocket feeds (Binance, MEXC) when enabled
            binance_feed = getattr(app.state, "binance_ws_feed", None)
            if binance_feed is not None and "binance" in {n for n, _ in BULK_PRICE_FETCHERS}:
                by_name["binance"] = (None, binance_feed.get_prices())
            mexc_feed = getattr(app.state, "mexc_ws_feed", None)
            if mexc_feed is not None and "mexc" in {n for n, _ in BULK_PRICE_FETCHERS}:
                by_name["mexc"] = (None, mexc_feed.get_prices())
            new_cache: dict[str, PricesResponse] = {}
            for sym in symbols:
                prices: list[ExchangePrice] = []
                errors: list[dict] = []
                for name, (err, d) in by_name.items():
                    if err is not None:
                        errors.append({"exchange": name, "error": err})
                    elif sym in d:
                        ep = d[sym]
                        prices.append(
                            ExchangePrice(
                                exchange=ep.exchange,
                                bid=to_decimal_str(ep.bid),
                                ask=to_decimal_str(ep.ask),
                                last=to_decimal_str(ep.last),
                                mark=to_decimal_str(ep.mark),
                                funding_rate=to_decimal_str(getattr(ep, "funding", "0")),
                            )
                        )
                    else:
                        errors.append({"exchange": name, "error": "Symbol not in tickers"})
                if len(prices) < 2:
                    continue
                new_cache[sym] = _build_response(sym, prices, errors)

            if new_cache:
                app.state.prices_cache = new_cache
                await _broadcast_prices(app)
            else:
                exchange_errors = {name: err for name, (err, _) in by_name.items() if err is not None}
                prev_count = len(getattr(app.state, "prices_cache", None) or {})
                err_key = "|".join(f"{k}:{v[:80]}" for k, v in sorted((exchange_errors or {"": "no data"}).items()))
                now_ts = time.time()
                last_ts = getattr(app.state, "_last_zero_coins_warning_ts", 0)
                last_key = getattr(app.state, "_last_zero_coins_warning_key", "")
                if now_ts - last_ts >= 60 or last_key != err_key:
                    app.state._last_zero_coins_warning_ts = now_ts
                    app.state._last_zero_coins_warning_key = err_key
                    hint = ""
                    if any("418" in str(e) for e in (exchange_errors or {}).values()):
                        hint = " (418 = Binance often blocks datacenter IPs; try ENABLED_EXCHANGES=mexc only or other network)"
                    logger.warning(
                        "Price update: 0 coins from exchanges (keeping previous %d). Errors: %s%s",
                        prev_count,
                        exchange_errors or "no data from both",
                        hint,
                    )

            # Throttle spread_history writes by SPREAD_HISTORY_INTERVAL_SECONDS
            if new_cache and settings.database_url and getattr(app.state, "db_pool", None):
                sec = settings.spread_history_interval_seconds
                if sec is not None and sec >= 1:
                    last_write = getattr(app.state, "last_spread_history_ts", None)
                    now_ts = time.time()
                    if last_write is None or (now_ts - last_write) >= sec:
                        try:
                            await db.write_spread_history(app.state.db_pool, new_cache)
                            app.state.last_spread_history_ts = now_ts
                        except Exception:
                            logger.exception("write_spread_history failed")

            elapsed = time.perf_counter() - t0
            logger.info("Price update: %d coins in %.2fs, next in %.1fs", len(new_cache) or len(getattr(app.state, "prices_cache", None) or {}), elapsed, interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Price update error: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    names = [n for n, _ in DISCOVERY_FETCHERS]
    if not names:
        logger.warning("No exchanges enabled. Set ENABLED_EXCHANGES (e.g. bybit,binance,mexc) or leave empty for all.")
    logger.info("Symbol discovery: loading USDT perpetuals from %s...", ", ".join(names) or "none")
    app.state.symbols = []
    app.state.prices_cache = {}
    app.state.ws_clients = set()
    app.state._ws_last_broadcast_ts = 0.0
    app.state.binance_ws_feed = None
    app.state.mexc_ws_feed = None
    pool = None
    if settings.database_url:
        pool = await db.create_pool(settings.database_url)
        await db.ensure_schema(pool)
        app.state.db_pool = pool
    timeout = float(settings.http_timeout)
    results = await asyncio.gather(
        *[f(timeout) for _, f in DISCOVERY_FETCHERS],
        return_exceptions=True,
    )
    all_syms: set[str] = set()
    for (name, _), r in zip(DISCOVERY_FETCHERS, results):
        if isinstance(r, set):
            all_syms |= r
            logger.info("  %s: %d symbols", name, len(r))
        else:
            logger.warning("  %s: failed - %s", name, r)
    app.state.symbols = sorted(s for s in all_syms if s and str(s).upper().endswith("USDT"))
    logger.info("Symbol discovery done: %d symbols. Starting price update loop.", len(app.state.symbols))

    if settings.ws_binance_enabled and "binance" in names:
        binance_feed = BinanceWSFeed(
            reconnect_delay=settings.ws_reconnect_delay_sec,
            ping_interval=settings.ws_ping_interval_sec,
            funding_interval=60 * max(1, settings.funding_rate_refresh_min),
        )
        await binance_feed.start()
        app.state.binance_ws_feed = binance_feed
        logger.info("Binance WebSocket feed enabled (REST polling for binance disabled)")
    if settings.ws_mexc_enabled and "mexc" in names:
        mexc_feed = MexcWSFeed(
            reconnect_delay=settings.ws_reconnect_delay_sec,
            ping_interval=settings.ws_ping_interval_sec,
        )
        await mexc_feed.start()
        app.state.mexc_ws_feed = mexc_feed
        logger.info("MEXC WebSocket feed enabled (REST polling for mexc disabled)")

    update_task = asyncio.create_task(_price_update_loop(app))
    try:
        yield
    finally:
        update_task.cancel()
        try:
            await update_task
        except asyncio.CancelledError:
            pass
        if app.state.binance_ws_feed is not None:
            await app.state.binance_ws_feed.stop()
            app.state.binance_ws_feed = None
        if app.state.mexc_ws_feed is not None:
            await app.state.mexc_ws_feed.stop()
            app.state.mexc_ws_feed = None
        if pool is not None:
            await pool.close()
        logger.info("Shutting down.")


app = FastAPI(title="Futures Spreads API", version="0.1.0", lifespan=lifespan)

# CORS: allow frontend origin(s)
_cors_raw = (settings.cors_origins or "").strip()
if _cors_raw:
    _origins = ["*"] if _cors_raw == "*" else [o.strip() for o in _cors_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

setup_middleware(app)


@app.get("/health")
async def health(request: Request) -> dict:
    out: dict = {"status": "ok"}
    feeds: dict = {}
    bf = getattr(request.app.state, "binance_ws_feed", None)
    if bf is not None:
        feeds["binance_ws"] = {
            "connected": bf.is_connected,
            "symbols": bf.symbols_count,
            "last_msg_age_sec": round(bf.last_msg_age_sec, 1) if bf.last_msg_age_sec >= 0 else None,
        }
    mf = getattr(request.app.state, "mexc_ws_feed", None)
    if mf is not None:
        feeds["mexc_ws"] = {
            "connected": mf.is_connected,
            "symbols": mf.symbols_count,
            "last_msg_age_sec": round(mf.last_msg_age_sec, 1) if mf.last_msg_age_sec >= 0 else None,
        }
    if feeds:
        out["feeds"] = feeds
    return out


@app.get("/v1/prices", response_model=None)
async def get_prices(
    request: Request,
    symbol: Optional[str] = Query(None),
) -> PricesResponse | list[PricesResponse]:
    cache = getattr(request.app.state, "prices_cache", None) or {}
    if symbol is not None and str(symbol).strip():
        sym = str(symbol).strip().upper()
        if sym not in cache:
            raise HTTPException(404, f"Symbol {sym!r} not in cache (may not be discovered or first update not done yet)")
        return cache[sym]
    return sorted(cache.values(), key=lambda r: abs(r.arbitrage.spread_pct), reverse=True)


@app.websocket("/v1/ws/prices")
async def ws_prices(websocket: WebSocket) -> None:
    await websocket.accept()
    clients = getattr(websocket.app.state, "ws_clients", None)
    if clients is not None:
        clients.add(websocket)
    try:
        cache = getattr(websocket.app.state, "prices_cache", None) or {}
        if cache:
            sorted_list = sorted(cache.values(), key=lambda r: abs(r.arbitrage.spread_pct), reverse=True)
            payload = [r.model_dump(mode="json") for r in sorted_list]
            await websocket.send_text(json.dumps(payload))
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        if clients is not None:
            clients.discard(websocket)


@app.get("/v1/spread-history")
async def get_spread_history(
    request: Request,
    symbol: str = Query(...),
    from_param: Optional[str] = Query(None, alias="from"),
    to_param: Optional[str] = Query(None, alias="to"),
    interval: Optional[int] = Query(None),
):
    pool = getattr(request.app.state, "db_pool", None)
    if not settings.database_url or pool is None:
        return JSONResponse(status_code=503, content={"error": "Spread history is disabled: DATABASE_URL not set"})
    now = datetime.now(timezone.utc)
    from_ts = _parse_ts(from_param, now - timedelta(hours=24))
    to_ts = _parse_ts(to_param, now)
    interval_minutes = interval if interval is not None and interval >= 1 else None
    rows = await db.get_spread_history(pool, symbol.strip().upper(), from_ts, to_ts, interval_minutes)

    def _ts_iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    series = [
        {"ts": _ts_iso(r["ts"]), "spread_pct": r["spread_pct"], "net_spread_pct": r["net_spread_pct"]}
        for r in rows
    ]
    return {
        "symbol": symbol.strip().upper(),
        "from": _ts_iso(from_ts),
        "to": _ts_iso(to_ts),
        "interval_minutes": interval_minutes,
        "series": series,
    }
