import pytest
from conftest import NOW
from pydantic import ValidationError

from trader.config import AppConfig
from trader.risk import assess
from trader.util import D


def test_sparse_inheritance_changes_with_defaults(cfg):
    cfg.risk_profiles = {"BTC-USDC": {"max_asset_exposure": "0.15"}}
    cfg = AppConfig.model_validate(cfg.model_dump())
    cfg.risk.min_confidence = D("0.8")
    assert cfg.risk_for("BTC-USDC", "main").min_confidence == D("0.8")
    assert cfg.risk_for("BTC-USDC", "main").max_asset_exposure == D("0.15")
    assert cfg.risk_for("ETH-USDC", "main").max_asset_exposure == D("0.30")


@pytest.mark.parametrize(
    "overrides",
    [
        {"typo": 1},
        {"min_confidence": None},
        {"max_order_notional": "1"},
        {"max_asset_exposure": "0.9"},
        {"max_data_age_seconds": 601},
    ],
)
def test_invalid_merged_risk_fails_configuration(cfg, overrides):
    data = cfg.model_dump()
    data["risk_profiles"] = {"BTC-USDC": overrides}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_shared_portfolio_limits_cannot_be_bypassed(cfg):
    cfg.risk_profiles = {
        "BTC-USDC": {
            "max_portfolio_exposure": "0.4",
            "max_trades_per_day": 2,
            "max_daily_loss_fraction": "0.01",
        }
    }
    policy = cfg.risk_for("ETH-USDC", "main")
    assert policy.max_portfolio_exposure == D("0.4")
    assert policy.max_trades_per_day == 2
    assert policy.max_daily_loss_fraction == D("0.01")


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_effective_profile_controls_assessment(cfg, decision, portfolio, product, quote, mode):
    cfg.risk_profiles = {"BTC-USDC": {"max_order_notional": "20"}}
    result = assess(decision, portfolio, product, quote, cfg, "c", mode, NOW)
    assert result.intent.quote_size <= D("20")
    assert "MAX_ORDER_NOTIONAL" in result.reasons
    cfg.risk_profiles["BTC-USDC"]["min_confidence"] = "0.95"
    assert assess(decision, portfolio, product, quote, cfg, "c", mode, NOW).intent is None
