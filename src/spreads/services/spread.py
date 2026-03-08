from ..models import Arbitrage, BestPrice, ExchangePrice
from ..utils import to_decimal_str


def compute_spreads(prices: list[ExchangePrice]) -> tuple[Arbitrage, dict[str, str]]:
    if not prices:
        return (
            Arbitrage(
                best_bid=BestPrice(exchange="", price="0"),
                best_ask=BestPrice(exchange="", price="0"),
                spread_pct=0.0,
                net_spread_pct=0.0,
                direction="No arbitrage",
            ),
            {},
        )

    best_bid = max(prices, key=lambda p: float(p.bid or "0"))
    best_ask = min(prices, key=lambda p: float(p.ask or "inf"))

    # Arbitrage only across different exchanges; avoid best_bid and best_ask from the same venue
    if best_bid.exchange == best_ask.exchange:
        others = [p for p in prices if p.exchange != best_bid.exchange]
        if not others:
            # single exchange: no cross-arbitrage
            _by_ex: dict[str, ExchangePrice] = {p.exchange: p for p in prices}
            _ex_names = sorted(_by_ex.keys())
            _pairwise: dict[str, str] = {}
            for i in range(len(_ex_names)):
                for j in range(i + 1, len(_ex_names)):
                    a, b = _ex_names[i], _ex_names[j]
                    va = float(_by_ex[a].last or "0")
                    vb = float(_by_ex[b].last or "0")
                    _pairwise[f"{a}_{b}"] = to_decimal_str(str(round(va - vb, 8)))
            return (
                Arbitrage(
                    best_bid=BestPrice(exchange=best_bid.exchange, price=best_bid.bid),
                    best_ask=BestPrice(exchange=best_ask.exchange, price=best_ask.ask),
                    spread_pct=0.0,
                    net_spread_pct=0.0,
                    direction="No arbitrage",
                ),
                _pairwise,
            )
        # other venues: pick cross-pair with max spread
        best_ask_cross = min(others, key=lambda p: float(p.ask or "inf"))
        best_bid_cross = max(others, key=lambda p: float(p.bid or "0"))
        spread1 = float(best_bid.bid or "0") - float(best_ask_cross.ask or "0")
        spread2 = float(best_bid_cross.bid or "0") - float(best_ask.ask or "0")
        if spread1 >= spread2:
            best_bid, best_ask = best_bid, best_ask_cross
        else:
            best_bid, best_ask = best_bid_cross, best_ask

    spread = float(best_bid.bid or "0") - float(best_ask.ask or "0")
    ask_f = float(best_ask.ask or "0")
    spread_pct = (spread / (ask_f or 1) * 100) if ask_f else 0.0

    # net = spread_pct + (funding_bid - funding_ask)*100 (funding in decimal, 0.0001 = 0.01% per 8h)
    funding_bid = float(best_bid.funding_rate or "0")
    funding_ask = float(best_ask.funding_rate or "0")
    net_spread_pct = round(spread_pct + (funding_bid - funding_ask) * 100, 4)

    direction = (
        f"LONG on {best_ask.exchange} @ {best_ask.ask}, SHORT on {best_bid.exchange} @ {best_bid.bid}"
        if spread > 0
        else "No arbitrage"
    )

    arbitrage = Arbitrage(
        best_bid=BestPrice(exchange=best_bid.exchange, price=best_bid.bid),
        best_ask=BestPrice(exchange=best_ask.exchange, price=best_ask.ask),
        spread_pct=round(spread_pct, 4),
        net_spread_pct=net_spread_pct,
        direction=direction,
    )

    by_ex: dict[str, ExchangePrice] = {p.exchange: p for p in prices}
    ex_names = sorted(by_ex.keys())
    pairwise: dict[str, str] = {}
    for i in range(len(ex_names)):
        for j in range(i + 1, len(ex_names)):
            a, b = ex_names[i], ex_names[j]
            va = float(by_ex[a].last or "0")
            vb = float(by_ex[b].last or "0")
            pairwise[f"{a}_{b}"] = to_decimal_str(str(round(va - vb, 8)))

    return (arbitrage, pairwise)
