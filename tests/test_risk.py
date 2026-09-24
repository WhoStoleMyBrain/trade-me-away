from datetime import timedelta

import pytest
from conftest import NOW

from trader.risk import assess, cap_refreshed_intent
from trader.schemas import StrategyPosition
from trader.util import D


def test_risk_clamps_notional_and_exposure(cfg, decision, portfolio, product, quote):
    result = assess(decision, portfolio, product, quote, cfg, "cycle", "paper", NOW)
    assert result.status == "REDUCED"
    assert result.intent.base_size * result.intent.limit_price <= cfg.risk.max_order_notional
    assert result.intent.base_size % product.base_increment == 0
    cfg.risk.max_order_notional = D("1000")
    result = assess(decision, portfolio, product, quote, cfg, "cycle", "paper", NOW)
    size, price = result.intent.base_size, result.intent.limit_price
    resulting_equity = portfolio.equity - size * (
        price * (1 + cfg.execution.taker_fee_rate) - quote.mid
    )
    assert size * quote.mid / resulting_equity <= D(str(decision.target_exposure))


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"reconciled": False}, "RECONCILIATION_FAILED"),
        ({"open_orders": 1}, "OUTSTANDING_ORDER"),
        ({"observed_at": NOW - timedelta(minutes=10)}, "STALE_ACCOUNT"),
        ({"daily_loss_fraction": D("0.1")}, "DAILY_LOSS_LIMIT"),
        ({"trades_today": 999}, "DAILY_TRADE_LIMIT"),
        ({"equity": D("0")}, "EMPTY_PORTFOLIO"),
    ],
)
def test_portfolio_risk_rejections(cfg, decision, portfolio, product, quote, change, reason):
    result = assess(
        decision, portfolio.model_copy(update=change), product, quote, cfg, "c", "paper", NOW
    )
    assert result.intent is None and reason in result.reasons


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"confidence": 0.1}, "LOW_CONFIDENCE"),
        ({"data_quality": "INSUFFICIENT"}, "MODEL_DATA_QUALITY_INSUFFICIENT"),
        ({"target_exposure": 0.0}, "ACTION_TARGET_CONFLICT"),
    ],
)
def test_decision_risk_rejection(cfg, decision, portfolio, product, quote, change, reason):
    result = assess(
        decision.model_copy(update=change), portfolio, product, quote, cfg, "c", "paper", NOW
    )
    assert reason in result.reasons


def test_stale_price_spread_and_movement(cfg, decision, portfolio, product, quote):
    for changed, extra, reason in [
        (quote.model_copy(update={"observed_at": NOW - timedelta(minutes=10)}), {}, "STALE_QUOTE"),
        (quote.model_copy(update={"ask": D("110")}), {}, "SPREAD_TOO_WIDE"),
        (quote, {"original_price": D("90")}, "PRICE_MOVED"),
        (quote, {"decision_at": NOW - timedelta(hours=1)}, "STALE_DECISION"),
        (quote, {"data_valid": False}, "MISSING_DATA"),
        (quote, {"budget_ok": False}, "API_BUDGET_EXCEEDED"),
    ]:
        assert (
            reason
            in assess(
                decision, portfolio, product, changed, cfg, "c", "paper", NOW, **extra
            ).reasons
        )


def test_aggregate_exposure_cash_and_no_shorting(cfg, decision, portfolio, product, quote):
    cfg.risk.max_order_notional = D("10000")
    portfolio = portfolio.model_copy(update={"cash": D("400"), "exposure_value": D("600")})
    assert assess(decision, portfolio, product, quote, cfg, "c", "paper", NOW).intent is None
    portfolio = portfolio.model_copy(update={"cash": D("1"), "exposure_value": D("0")})
    assert assess(decision, portfolio, product, quote, cfg, "c", "paper", NOW).intent is None
    sell = decision.model_copy(update={"action": "EXIT", "target_exposure": 0.0})
    assert assess(sell, portfolio, product, quote, cfg, "c", "paper", NOW).intent is None


def test_selling_cannot_exceed_proposed_reduction(cfg, decision, portfolio, product, quote):
    portfolio = portfolio.model_copy(
        update={
            "cash": D("500"),
            "exposure_value": D("500"),
            "positions": {"BTC-USDC": StrategyPosition(quantity=D("5"), cost_basis=D("400"))},
        }
    )
    sell = decision.model_copy(update={"action": "DECREASE", "target_exposure": 0.45})
    result = assess(sell, portfolio, product, quote, cfg, "c", "paper", NOW)
    assert result.intent.base_size <= D("0.5")
    cooldown = portfolio.model_copy(
        update={
            "positions": {
                "BTC-USDC": StrategyPosition(
                    quantity=D("5"), cost_basis=D("400"), last_trade_at=NOW
                )
            }
        }
    )
    assert (
        "TRADE_COOLDOWN" in assess(sell, cooldown, product, quote, cfg, "c", "paper", NOW).reasons
    )


def test_refresh_only_shrinks(intent, product):
    refreshed = intent.model_copy(
        update={"base_size": D("2"), "quote_size": D("220"), "limit_price": D("110")}
    )
    capped = cap_refreshed_intent(intent, refreshed, product)
    assert capped.base_size <= intent.base_size
    assert capped.quote_size <= intent.quote_size
    assert capped.client_order_id == intent.client_order_id


def test_same_risk_in_paper_and_live(cfg, decision, portfolio, product, quote):
    paper = assess(decision, portfolio, product, quote, cfg, "c", "paper", NOW)
    live = assess(decision, portfolio, product, quote, cfg, "c", "live", NOW)
    assert paper.reasons == live.reasons
    assert paper.intent.base_size == live.intent.base_size
    assert paper.intent.quote_size == live.intent.quote_size
