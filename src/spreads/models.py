from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ExchangePrice(BaseModel):
    exchange: str
    bid: str
    ask: str
    last: str
    mark: str
    funding_rate: str = "0"


class BestPrice(BaseModel):
    exchange: str
    price: str


class Arbitrage(BaseModel):
    best_bid: BestPrice
    best_ask: BestPrice
    spread_pct: float
    net_spread_pct: float
    direction: str


class PricesResponse(BaseModel):
    symbol: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    prices: list[ExchangePrice]
    arbitrage: Arbitrage
    pairwise_spreads: dict[str, str] = Field(default_factory=dict)
    errors: list[dict[str, str]] = Field(default_factory=list)
