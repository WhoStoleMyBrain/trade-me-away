import json

import pytest
from pydantic import ValidationError

from trader.config import Environment, active_mode
from trader.errors import SafetyError
from trader.schemas import Decision, DecisionBatch


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_exposure", -0.1),
        ("target_exposure", 1.1),
        ("target_exposure", "0.2"),
        ("target_exposure", True),
        ("target_exposure", float("nan")),
        ("confidence", 2),
        ("expected_horizon_hours", 1.5),
        ("action", "BUY"),
        ("coin_quantity", 1),
    ],
)
def test_strict_model_fields(decision, field, value):
    data = decision.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        Decision.model_validate_json(json.dumps(data), strict=True)


def test_missing_field_and_exit_target(decision):
    data = decision.model_dump(mode="json")
    del data["confidence"]
    with pytest.raises(ValidationError):
        Decision.model_validate(data)
    with pytest.raises(ValidationError):
        Decision.model_validate(decision.model_dump() | {"action": "EXIT"})


def test_coverage_rejects_duplicates_unknown_missing(decision):
    for decisions in (
        [decision, decision],
        [decision.model_copy(update={"product_id": "OTHER-USDC"})],
        [decision],
    ):
        batch = DecisionBatch(market_regime="mixed", decisions=decisions)
        with pytest.raises(SafetyError, match="PRODUCT_SET"):
            batch.validate_products({"BTC-USDC", "ETH-USDC"})


@pytest.mark.parametrize("value", [None, "", "LIVE", "live ", "paper,live", "true", "simulation"])
def test_missing_or_invalid_mode(value, monkeypatch, tmp_path):
    if value is None:
        monkeypatch.delenv("TRADING_MODE")
    else:
        monkeypatch.setenv("TRADING_MODE", value)
    with pytest.raises(SafetyError, match="INVALID_TRADING_MODE"):
        active_mode(tmp_path / "missing")
    with pytest.raises(ValidationError):
        Environment(_env_file=None)


def test_dotenv_and_env_precedence(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRADING_MODE=paper\n")
    monkeypatch.delenv("TRADING_MODE")
    assert active_mode(path) == "paper"
    monkeypatch.setenv("TRADING_MODE", "live")
    assert active_mode(path) == "live"
