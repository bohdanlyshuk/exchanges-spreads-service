import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .models import PricesResponse

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS spread_history (
    symbol varchar(32) NOT NULL,
    ts timestamptz NOT NULL,
    spread_pct numeric(12,6) NOT NULL,
    net_spread_pct numeric(12,6) NOT NULL,
    best_bid_ex varchar(16) NOT NULL,
    best_ask_ex varchar(16) NOT NULL
);
"""
CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_spread_history_symbol_ts ON spread_history (symbol, ts);
"""


async def create_pool(database_url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=4,
        command_timeout=10,
    )


async def ensure_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE)
        await conn.execute(CREATE_INDEX)
        # Migrate existing table: spread_pct_abs -> spread_pct
        has_abs = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'spread_history' AND column_name = 'spread_pct_abs'"
        )
        has_pct = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'spread_history' AND column_name = 'spread_pct'"
        )
        if has_abs and not has_pct:
            await conn.execute("ALTER TABLE spread_history RENAME COLUMN spread_pct_abs TO spread_pct")


async def write_spread_history(
    pool: asyncpg.Pool,
    cache: dict[str, PricesResponse],
) -> None:
    if not cache:
        return
    now = datetime.now(timezone.utc)
    symbols: list[str] = []
    ts_list: list[datetime] = []
    spread_list: list[float] = []
    net_list: list[float] = []
    bid_ex_list: list[str] = []
    ask_ex_list: list[str] = []
    for r in cache.values():
        if not isinstance(r, PricesResponse):
            continue
        symbols.append(r.symbol)
        ts_list.append(now)
        spread_list.append(float(r.arbitrage.spread_pct))
        net_list.append(float(r.arbitrage.net_spread_pct))
        bid_ex_list.append((r.arbitrage.best_bid.exchange or "")[:16])
        ask_ex_list.append((r.arbitrage.best_ask.exchange or "")[:16])
    if not symbols:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO spread_history (symbol, ts, spread_pct, net_spread_pct, best_bid_ex, best_ask_ex)
            SELECT * FROM unnest($1::varchar[], $2::timestamptz[], $3::numeric[], $4::numeric[], $5::varchar[], $6::varchar[])
            """,
            symbols,
            ts_list,
            spread_list,
            net_list,
            bid_ex_list,
            ask_ex_list,
        )
    logger.info("spread_history saved: %d rows at %s", len(symbols), now.isoformat())


async def get_spread_history(
    pool: asyncpg.Pool,
    symbol: str,
    from_ts: datetime,
    to_ts: datetime,
    interval_minutes: int | None = None,
) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        if interval_minutes is None or interval_minutes < 1:
            rows = await conn.fetch(
                """
                SELECT ts, spread_pct, net_spread_pct
                FROM spread_history
                WHERE symbol = $1 AND ts >= $2 AND ts <= $3
                ORDER BY ts
                """,
                symbol,
                from_ts,
                to_ts,
            )
            return [{"ts": r["ts"], "spread_pct": float(r["spread_pct"]), "net_spread_pct": float(r["net_spread_pct"])} for r in rows]
        # Bucket: floor(minute/interval)*interval
        rows = await conn.fetch(
            """
            SELECT
                date_trunc('hour', ts) + (floor(extract(minute from ts)::numeric / $4) * $4) * interval '1 minute' AS bucket,
                avg(spread_pct)::numeric AS spread_pct,
                avg(net_spread_pct)::numeric AS net_spread_pct
            FROM spread_history
            WHERE symbol = $1 AND ts >= $2 AND ts <= $3
            GROUP BY 1
            ORDER BY 1
            """,
            symbol,
            from_ts,
            to_ts,
            interval_minutes,
        )
        return [{"ts": r["bucket"], "spread_pct": float(r["spread_pct"]), "net_spread_pct": float(r["net_spread_pct"])} for r in rows]
