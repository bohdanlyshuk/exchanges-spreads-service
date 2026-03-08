try:
    import uvloop
    uvloop.install()
except (ImportError, OSError):
    pass

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
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


async def _price_update_loop(app: FastAPI) -> None:
    timeout = float(settings.http_timeout)
    interval = max(1, settings.price_update_interval)
    while True:
        try:
            symbols = [s for s in (getattr(app.state, "symbols", None) or []) if s and str(s).strip()]
            if not symbols:
                await asyncio.sleep(interval)
                continue
            logger.debug("Price update: bulk fetch for %d symbols...", len(symbols))
            t0 = time.perf_counter()
            results = await asyncio.gather(
                *[f(timeout) for _, f in BULK_PRICE_FETCHERS],
                return_exceptions=True,
            )
            by_name: dict[str, tuple[str | None, dict]] = {}
            for (name, _), r in zip(BULK_PRICE_FETCHERS, results):
                if isinstance(r, BaseException):
                    by_name[name] = (str(r), {})
                else:
                    by_name[name] = (None, r)
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
            app.state.prices_cache = new_cache

            # Throttle spread_history writes by SPREAD_HISTORY_INTERVAL_SECONDS
            if settings.database_url and getattr(app.state, "db_pool", None):
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
            logger.debug("Price update done: %d symbols in %.1fs. Next in %ds.", len(new_cache), elapsed, interval)
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
    update_task = asyncio.create_task(_price_update_loop(app))
    try:
        yield
    finally:
        update_task.cancel()
        try:
            await update_task
        except asyncio.CancelledError:
            pass
        if pool is not None:
            await pool.close()
        logger.info("Shutting down.")


app = FastAPI(title="Futures Spreads API", version="0.1.0", lifespan=lifespan)
setup_middleware(app)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
    return sorted(cache.values(), key=lambda r: r.arbitrage.spread_pct_abs, reverse=True)


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
        {"ts": _ts_iso(r["ts"]), "spread_pct_abs": r["spread_pct_abs"], "net_spread_pct": r["net_spread_pct"]}
        for r in rows
    ]
    return {
        "symbol": symbol.strip().upper(),
        "from": _ts_iso(from_ts),
        "to": _ts_iso(to_ts),
        "interval_minutes": interval_minutes,
        "series": series,
    }
