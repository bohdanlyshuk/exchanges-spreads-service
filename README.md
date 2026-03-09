# Futures Spreads API

REST API that aggregates USDT-margined perpetual futures prices from **Bybit**, **Binance**, **MEXC**, **Gate.io**, **KuCoin**, **BingX**, and **Bitget**, computes cross-exchange spreads and arbitrage metrics, and serves them with minimal latency for bots and dashboards.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## Features

- **Live prices** — In-memory cache updated every `PRICE_UPDATE_INTERVAL` seconds; `GET /v1/prices` does not call exchanges.
- **WebSocket for Binance (and optional MEXC)** — Binance uses WebSocket by default to avoid REST rate limits (418); MEXC can be switched to WS via `WS_MEXC_ENABLED=true`.
- **Seven exchanges** — Bybit, Binance, MEXC, Gate.io, KuCoin, BingX, Bitget; symbols discovered at startup, merged by `BTCUSDT`-style ticker.
- **Arbitrage metrics** — Best bid/ask across venues, `spread_pct` (signed), `net_spread_pct` (with funding), `pairwise_spreads` between exchanges.
- **Spread history** — Optional PostgreSQL backend for `GET /v1/spread-history` (time series for charts). Disabled when `DATABASE_URL` is unset.

---

## Quick Start

```bash
git clone <repo>
cd exchanges-spreads-service
cp .env.example .env
# edit .env: HTTP_TIMEOUT, PORT, LOG_LEVEL, PRICE_UPDATE_INTERVAL

./run.sh
```

Requires **uv** for `./run.sh`. Then: `http://localhost:8000/health`, `http://localhost:8000/v1/prices?symbol=BTCUSDT`, and **http://localhost:8000/docs** for interactive API docs.

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness |
| `GET` | `/v1/prices` | All symbols from cache, sorted by `abs(spread_pct)` descending |
| `GET` | `/v1/prices?symbol=BTCUSDT` | One symbol |
| `WebSocket` | `/v1/ws/prices` | Real-time prices: connect to receive the same list as `GET /v1/prices` pushed on every cache update |
| `GET` | `/v1/spread-history?symbol=BTCUSDT&from=...&to=...&interval=5` | Time series (requires `DATABASE_URL`) |

**Interactive docs:** `/docs` (Swagger), `/redoc`.

### WebSocket `GET /v1/ws/prices`

Connect to receive the same JSON array as `GET /v1/prices` (list of price objects with `symbol`, `prices`, `arbitrage`, `pairwise_spreads`, `errors`, `updated_at`) pushed in real time whenever the server updates its cache. On connect you get an initial snapshot; then each cache update triggers a new message. Use this for lowest latency instead of polling. Optional: set `WS_BROADCAST_INTERVAL_SEC` (e.g. `0.2`) to throttle broadcast frequency.

### Example: `GET /v1/prices?symbol=BTCUSDT`

```json
{
  "symbol": "BTCUSDT",
  "prices": [
    { "exchange": "bybit",  "bid": "96501.9", "ask": "96502.0", "last": "96501.9", "mark": "96501.8", "funding_rate": "0.0001" },
    { "exchange": "binance", "bid": "96501.0", "ask": "96501.2", "last": "96501.1", "mark": "96501.0", "funding_rate": "0.000099" }
  ],
  "arbitrage": {
    "best_bid": { "exchange": "bybit",  "price": "96501.9" },
    "best_ask": { "exchange": "binance", "price": "96501.2" },
    "spread_pct": 0.00073,
    "net_spread_pct": 0.00083,
    "direction": "LONG on binance @ 96501.2, SHORT on bybit @ 96501.9"
  },
  "pairwise_spreads": { "binance_bybit": "-0.8", "binance_gate": "0.0012", ... },
  "errors": []
}
```

- **`spread_pct`** — `(best_bid − best_ask) / best_ask × 100` (signed; positive = arbitrage opportunity)
- **`net_spread_pct`** — `spread_pct` plus funding adjustment (LONG pays, SHORT receives)
- **`pairwise_spreads`** — `last_A − last_B` for each exchange pair, key `"A_B"` (alphabetical)

### `GET /v1/spread-history`

Requires `DATABASE_URL`. Otherwise returns `503` with `{"error": "Spread history is disabled: DATABASE_URL not set"}`.

| Query | Required | Description |
|-------|----------|-------------|
| `symbol` | yes | e.g. `BTCUSDT` |
| `from` | no | ISO 8601 or Unix ms; default 24h ago |
| `to` | no | ISO 8601 or Unix ms; default now |
| `interval` | no | Bucket size in minutes (5, 15, 60); omit for raw points |

**Response:**

```json
{
  "symbol": "BTCUSDT",
  "from": "2026-01-24T12:00:00Z",
  "to": "2026-01-25T12:00:00Z",
  "interval_minutes": 5,
  "series": [
    { "ts": "2026-01-24T12:00:00Z", "spread_pct": 0.05, "net_spread_pct": 0.048 },
    { "ts": "2026-01-24T12:05:00Z", "spread_pct": 0.06, "net_spread_pct": 0.055 }
  ]
}
```

---

## Configuration

Copy `.env.example` → `.env`. No built-in defaults; all values from env.

| Variable | Required | Description |
|----------|----------|-------------|
| `HTTP_TIMEOUT` | yes | Timeout for exchange HTTP (seconds) |
| `PORT` | yes | Server port |
| `LOG_LEVEL` | yes | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PRICE_UPDATE_INTERVAL` | yes | Seconds between full price refresh cycles |
| `DATABASE_URL` | no | PostgreSQL URL; enables `/v1/spread-history` |
| `SPREAD_HISTORY_INTERVAL_SECONDS` | no | How often to append a snapshot to DB; only when `DATABASE_URL` is set |
| `WS_BINANCE_ENABLED` | no | Use WebSocket for Binance (default `true`); avoids 418 rate limit |
| `WS_MEXC_ENABLED` | no | Use WebSocket for MEXC (default `false`) |
| `FUNDING_RATE_REFRESH_MIN` | no | Minutes between Binance funding/mark REST refresh (default `10`) |
| `WS_BROADCAST_INTERVAL_SEC` | no | Min seconds between client WS broadcasts (default `0` = every cache update) |

Symbols are discovered at startup from all seven exchanges. When WebSocket is enabled for Binance/MEXC, their prices come from WS streams; other exchanges are polled via REST every `PRICE_UPDATE_INTERVAL` seconds. `/health` includes `feeds.binance_ws` and `feeds.mexc_ws` when applicable.

---

## Database (for spread history)

The app creates the `spread_history` table on first run. You only need a running Postgres and a database.

### Option A: Docker Compose (Postgres only)

```bash
docker compose up -d postgres
```

Then in `.env`:

```
DATABASE_URL=postgresql://spreads:spreads@localhost:5432/spreads
SPREAD_HISTORY_INTERVAL_SECONDS=3600
```

### Option B: Docker Compose (API + Postgres)

```bash
docker compose --profile full up -d
```

Runs Postgres and the API; API at `http://localhost:8000`.

### Option C: Local Postgres

**macOS (Homebrew):**

```bash
brew install postgresql@16
brew services start postgresql@16
export PATH="$(brew --prefix)/opt/postgresql@16/bin:$PATH"
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt install postgresql postgresql-client
sudo systemctl start postgresql
```

Create DB and user:

```bash
psql -d postgres -f scripts/init-local-db.sql
```

If that fails (auth), try: `psql -U postgres -d postgres -f scripts/init-local-db.sql` or on Linux `sudo -u postgres psql -d postgres -f scripts/init-local-db.sql`.

This creates user `spreads` / password `spreads` and database `spreads`. Set `DATABASE_URL` and `SPREAD_HISTORY_INTERVAL_SECONDS` in `.env`.

---

## Run

### Local

```bash
cp .env.example .env
# set HTTP_TIMEOUT, PORT, LOG_LEVEL, PRICE_UPDATE_INTERVAL

./run.sh
```

`./run.sh` uses **uv**; ensure `uv` is installed. Alternatively:

```bash
pip install -r requirements.txt
set -a && . ./.env && set +a
PYTHONPATH=src uvicorn spreads.main:app --host 0.0.0.0 --port "$PORT"
```

### Docker (image only)

```bash
docker build -t spreads .
docker run -p 8000:8000 --env-file .env spreads
```

---

## Project structure

```
exchanges-spreads-service/
├── src/spreads/
│   ├── main.py          # FastAPI app, routes, lifespan, price loop
│   ├── config.py        # Settings from .env
│   ├── db.py            # Postgres pool, spread_history table, read/write
│   ├── models.py        # Pydantic: ExchangePrice, Arbitrage, PricesResponse
│   ├── utils.py         # to_decimal_str
│   ├── exchanges/       # Bybit, Binance, MEXC, Gate, KuCoin, BingX, Bitget (symbols + prices)
│   ├── services/        # compute_spreads (arbitrage, pairwise)
│   └── middleware/      # request logging, unhandled exception → 500
├── scripts/
│   └── init-local-db.sql
├── tests/
├── .env.example
├── pyproject.toml
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── run.sh
├── Makefile
└── LICENSE
```

---

## Development

```bash
uv sync --all-extras
# or: pip install -e ".[dev]"

pytest
```

`Makefile` targets: `make run`, `make test`, `make install`, `make docker-build`, `make docker-up`, `make docker-up-full`, `make postgres`.

---

## Stack

- **Python 3.11+**
- **FastAPI**, **httpx**, **pydantic**, **pydantic-settings**, **uvicorn**
- **asyncpg** (Postgres, for spread history)
- **uvloop** (optional, when available)
