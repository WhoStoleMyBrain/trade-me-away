from __future__ import annotations

import json
from datetime import datetime, timedelta

from trader.config import AppConfig
from trader.portfolio import strategy_summary
from trader.schemas import Fill, MarketState, PortfolioState
from trader.storage import Storage
from trader.util import ZERO, dumps

# Keep this fixed prefix independent of asset selection, time, mode, and previous model output.
INSTRUCTIONS = """You produce spot trading intent from a supplied numerical state.
Base decisions exclusively on the supplied numerical market, portfolio, execution-cost,
and strategy data. Do not use outside news, social-media sentiment, cryptocurrency narratives,
remembered prices, or information not explicitly supplied. Treat supplied current data as
authoritative. Do not infer missing values; null means unknown. Do not fetch data or use tools.
Evaluate momentum, trend, volatility, liquidity, execution costs, and existing positions jointly.
Recent price appreciation alone is not a reason to buy.
Recent decline alone is not a reason to sell.
Prefer HOLD when the expected advantage is weak, uncertain, or unlikely to exceed round-trip fees,
spread and likely slippage. Insufficient data is a reason to HOLD. Consider all assets jointly.
Return exactly one decision for each supplied product_id, with no duplicates or other products.
target_exposure is the asset value divided by the equity of its mapped portfolio, from 0 to 1.
Assets sharing a portfolio share its cash and total exposure limit; do not double count its equity.
INCREASE requires a target above current exposure. DECREASE requires a target below it.
EXIT requires a zero target. For HOLD copy current exposure. Local code may shrink or reject intent.
Never specify an order quantity, coin amount or cash amount. Spot only; no borrowing or leverage.
Use concise rationale text referencing supplied evidence. Do not provide chain-of-thought.
Feature returns, distances, volatility and normalized ATR are fractions; RSI is on a 0-100 scale;
spread and slippage are basis points. Timeframe prefixes denote candle duration in seconds.
Each timeframe's closed_at_epoch is its last closed candle endpoint. There is no raw candle history.
Realized PnL and entry costs include fees where known. Unknown pre-strategy cost basis stays null.
"""


def build_payload(
    cfg: AppConfig,
    markets: dict[str, MarketState],
    portfolios: dict[str, PortfolioState],
    store: Storage,
    mode: str,
    now: datetime,
) -> str:
    # Only actual fills are history. Previous model rationale text never enters this payload.
    rows = store.db.execute(
        """SELECT fill_json FROM fills WHERE mode=? AND trade_time>=?""",
        (mode, (now - timedelta(hours=24)).isoformat()),
    ).fetchall()
    recent = [Fill.model_validate(json.loads(r[0])) for r in rows]
    assets = []
    for asset in sorted(cfg.enabled_assets, key=lambda a: a.product_id):
        market, portfolio = markets[asset.product_id], portfolios[asset.portfolio]
        position = portfolio.positions[asset.product_id]
        fills = [f for f in recent if f.product_id == asset.product_id]
        assets.append(
            {
                "product_id": asset.product_id,
                "portfolio": asset.portfolio,
                "price": market.quote.mid,
                "quote_time": market.quote.observed_at,
                "spread_bps": market.quote.spread_bps,
                "features": market.features,
                "current_exposure": portfolio.exposure(asset.product_id, market.quote.mid),
                "strategy": strategy_summary(position, market.quote.mid, now),
                "actual_trades_24h": len({f.order_id for f in fills}),
                "turnover_24h": sum((f.price * f.base_size for f in fills), ZERO),
            }
        )
    return dumps(
        {
            "as_of": now,
            "assets": assets,
            "portfolios": {
                name: {
                    "equity": p.equity,
                    "cash_usdc": p.cash,
                    "crypto_exposure": p.exposure_value / p.equity if p.equity else ZERO,
                    "daily_loss_fraction": p.daily_loss_fraction,
                    "order_attempts_today": p.trades_today,
                }
                for name, p in sorted(portfolios.items())
            },
            "cost_assumptions": {
                "taker_fee_rate": cfg.execution.taker_fee_rate,
                "slippage_bps_per_side": cfg.execution.slippage_bps,
                "order_type": cfg.execution.order_type,
            },
            "limits": {
                "max_asset_exposure": cfg.risk.max_asset_exposure,
                "max_portfolio_exposure": cfg.risk.max_portfolio_exposure,
                "min_confidence": cfg.risk.min_confidence,
            },
        }
    )
