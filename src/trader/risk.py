from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from trader.config import AppConfig, Mode
from trader.market_data import validate_quote
from trader.schemas import (
    Action,
    Decision,
    OrderIntent,
    PortfolioState,
    Product,
    Quality,
    Quote,
    RiskResult,
)
from trader.util import ONE, ZERO, D, step


def reject(*reasons: str) -> RiskResult:
    return RiskResult(status="REJECTED", reasons=list(reasons), intent=None)


def assess(
    decision: Decision,
    portfolio: PortfolioState,
    product: Product,
    quote: Quote,
    cfg: AppConfig,
    cycle: str,
    mode: Mode,
    now: datetime,
    *,
    data_valid: bool = True,
    budget_ok: bool = True,
    original_price: D | None = None,
    decision_at: datetime | None = None,
) -> RiskResult:
    """Pure deterministic sizing. No network, storage, or model calls."""
    risk, execution = cfg.risk, cfg.execution
    reasons = []
    if decision.product_id != product.product_id or quote.product_id != product.product_id:
        reasons.append("PRODUCT_MISMATCH")
    if not portfolio.reconciled:
        reasons.append("RECONCILIATION_FAILED")
    if portfolio.open_orders:
        reasons.append("OUTSTANDING_ORDER")
    if not data_valid:
        reasons.append("MISSING_DATA")
    if not budget_ok:
        reasons.append("API_BUDGET_EXCEEDED")
    try:
        validate_quote(quote, now, risk.max_data_age_seconds)
    except Exception:
        reasons.append("STALE_QUOTE")
    if not -5 <= (now - portfolio.observed_at).total_seconds() <= risk.max_data_age_seconds:
        reasons.append("STALE_ACCOUNT")
    if (
        decision_at
        and not 0 <= (now - decision_at).total_seconds() <= risk.max_decision_age_seconds
    ):
        reasons.append("STALE_DECISION")
    if not product.tradable or (
        execution.order_type == "market_ioc" and not product.market_allowed
    ):
        reasons.append("PRODUCT_NOT_TRADABLE")
    if quote.spread_bps > risk.max_spread_bps:
        reasons.append("SPREAD_TOO_WIDE")
    if original_price and abs(quote.mid / original_price - 1) > risk.max_price_move_fraction:
        reasons.append("PRICE_MOVED")
    if portfolio.daily_loss_fraction >= risk.max_daily_loss_fraction:
        reasons.append("DAILY_LOSS_LIMIT")
    if portfolio.equity <= 0:
        reasons.append("EMPTY_PORTFOLIO")
    if product.product_id not in portfolio.positions:
        reasons.append("POSITION_MISSING")
    if reasons:
        return reject(*reasons)

    position = portfolio.positions[product.product_id]
    current = portfolio.exposure(product.product_id, quote.mid)
    proposed = D(str(decision.target_exposure))
    if decision.action == Action.HOLD:
        return RiskResult(status="HOLD", reasons=["MODEL_HOLD"], intent=None)
    if decision.data_quality != Quality.GOOD:
        reasons.append("MODEL_DATA_QUALITY_INSUFFICIENT")
    if D(str(decision.confidence)) < risk.min_confidence:
        reasons.append("LOW_CONFIDENCE")
    if portfolio.trades_today >= risk.max_trades_per_day:
        reasons.append("DAILY_TRADE_LIMIT")
    if (
        position.last_trade_at
        and (now - position.last_trade_at).total_seconds() < risk.min_trade_interval_seconds
    ):
        reasons.append("TRADE_COOLDOWN")
    buy = decision.action == Action.INCREASE
    if (buy and proposed <= current) or (not buy and proposed >= current):
        reasons.append("ACTION_TARGET_CONFLICT")
    if reasons:
        return reject(*reasons)

    side = "BUY" if buy else "SELL"
    offset = (
        execution.limit_offset_bps
        if execution.order_type == "limit_ioc"
        else execution.slippage_bps
    )
    price = quote.ask * (ONE + offset / 10000) if buy else quote.bid * (ONE - offset / 10000)
    price = step(price, product.price_increment, up=not buy)
    if price <= 0:
        return reject("INVALID_EXECUTION_PRICE")
    target = min(proposed, risk.max_asset_exposure) if buy else proposed
    fee = execution.taker_fee_rate
    if buy:
        # Worst-case price plus fees reduces post-trade equity. Bound both asset and total exposure.
        asset_room = (target * portfolio.equity - position.quantity * quote.mid) / (
            price * (ONE + target * fee)
        )
        total_room = (risk.max_portfolio_exposure * portfolio.equity - portfolio.exposure_value) / (
            price * (ONE + risk.max_portfolio_exposure * fee)
        )
        wanted = max(ZERO, min(asset_room, total_room))
        capacity = portfolio.cash / (price * (ONE + fee))
    else:
        wanted = max(ZERO, position.quantity - target * portfolio.equity / quote.mid)
        capacity = position.quantity
    quantity = step(
        min(
            wanted,
            capacity,
            risk.max_order_notional / price,
            product.quote_max_size / price,
            product.base_max_size,
        ),
        product.base_increment,
    )
    notional = step(quantity * price, product.quote_increment)
    if quantity < product.base_min_size or notional < max(
        risk.min_order_notional, product.quote_min_size
    ):
        return reject("BELOW_MINIMUM_OR_NO_RISK_CAPACITY")
    if quantity <= 0 or notional <= 0:
        return reject("ZERO_ORDER")
    # A capped sell must stop above the model's target, never liquidate extra to meet a minimum.
    clamped = target != proposed or quantity < wanted or (buy and total_room < asset_room)
    intent = OrderIntent(
        client_order_id=str(uuid4()),
        cycle_id=cycle,
        mode=mode,
        portfolio=portfolio.portfolio,
        product_id=product.product_id,
        side=side,
        order_type=execution.order_type,
        base_size=quantity,
        quote_size=notional,
        limit_price=price,
        reference_price=quote.mid,
        approved_target_exposure=target,
        created_at=now,
    )
    return RiskResult(
        status="REDUCED" if clamped else "APPROVED",
        reasons=["SIZE_OR_EXPOSURE_CLAMPED"] if clamped else [],
        intent=intent,
    )


def cap_refreshed_intent(
    original: OrderIntent, refreshed: OrderIntent, product: Product
) -> OrderIntent:
    """Refresh can shrink an already-approved order; it can never enlarge it or reverse it."""
    if (original.side, original.product_id, original.portfolio, original.mode) != (
        refreshed.side,
        refreshed.product_id,
        refreshed.portfolio,
        refreshed.mode,
    ):
        raise ValueError("refreshed intent identity changed")
    quantity = min(
        original.base_size,
        refreshed.base_size,
        step(original.quote_size / refreshed.limit_price, product.base_increment),
    )
    quote_size = min(
        original.quote_size,
        refreshed.quote_size,
        step(quantity * refreshed.limit_price, product.quote_increment),
    )
    return refreshed.model_copy(
        update={
            "client_order_id": original.client_order_id,
            "base_size": quantity,
            "quote_size": quote_size,
            "reference_price": original.reference_price,
        }
    )
