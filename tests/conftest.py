from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from coinbase.rest import RESTClient

from trader.coinbase_client import CoinbaseAdapter
from trader.config import AppConfig
from trader.schemas import (
    AccountState,
    Balance,
    Decision,
    DecisionBatch,
    Ledger,
    OrderIntent,
    PortfolioState,
    Product,
    Quote,
    StrategyPosition,
)
from trader.storage import Storage
from trader.util import D, dumps

NOW = datetime(2026, 9, 24, 9, 5, 10, tzinfo=UTC)
PRODUCTS = ("BTC-USDC", "ETH-USDC", "SOL-USDC")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Tests cannot open a network connection, read user credentials, or select live by accident."""

    def denied(*args, **kwargs):
        raise AssertionError("External network is forbidden in tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-unused")
    monkeypatch.chdir(tmp_path)
    import trader.coinbase_client
    import trader.execution
    import trader.llm
    import trader.market_data
    import trader.orchestrator
    import trader.storage
    import trader.util

    for module in (
        trader.coinbase_client,
        trader.execution,
        trader.llm,
        trader.market_data,
        trader.orchestrator,
        trader.storage,
        trader.util,
    ):
        monkeypatch.setattr(module, "utcnow", lambda: NOW)


@pytest.fixture
def cfg():
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = AppConfig.model_validate(yaml.safe_load(path.read_text()))
    config.execution.verification_delay_seconds = 0
    return config


@pytest.fixture
def store(tmp_path):
    storage = Storage(tmp_path / "trader.sqlite3")
    yield storage
    storage.close()


@pytest.fixture
def product():
    return Product(
        product_id="BTC-USDC",
        base_currency="BTC",
        quote_currency="USDC",
        base_increment=D("0.00001"),
        quote_increment=D("0.01"),
        price_increment=D("0.01"),
        base_min_size=D("0.00001"),
        base_max_size=D("1000000"),
        quote_min_size=D("1"),
        quote_max_size=D("1000000"),
        tradable=True,
        market_allowed=True,
    )


@pytest.fixture
def quote():
    return Quote(
        product_id="BTC-USDC", bid=D("99.99"), ask=D("100.01"), observed_at=NOW, received_at=NOW
    )


@pytest.fixture
def actual():
    return AccountState(
        portfolio="main",
        portfolio_id="test-portfolio",
        observed_at=NOW,
        balances={
            c: Balance(available=D("1000") if c == "USDC" else D("0"), hold=D("0"))
            for c in ("USDC", "BTC", "ETH", "SOL")
        },
        open_orders=[],
        recent_fills=[],
    )


@pytest.fixture
def ledger():
    return Ledger(cash=D("1000"), positions={p: StrategyPosition() for p in PRODUCTS})


@pytest.fixture
def portfolio(ledger):
    return PortfolioState(
        portfolio="main",
        observed_at=NOW,
        cash=ledger.cash,
        positions=ledger.positions,
        equity=D("1000"),
        exposure_value=D("0"),
        daily_loss_fraction=D("0"),
        trades_today=0,
        open_orders=0,
        reconciled=True,
    )


@pytest.fixture
def decision():
    return Decision(
        product_id="BTC-USDC",
        action="INCREASE",
        target_exposure=0.2,
        confidence=0.9,
        expected_horizon_hours=12,
        data_quality="GOOD",
        rationale="Numerical trend supports intent.",
    )


@pytest.fixture
def intent():
    return OrderIntent(
        client_order_id="test-client-id",
        cycle_id="test-cycle",
        mode="paper",
        portfolio="main",
        product_id="BTC-USDC",
        side="BUY",
        order_type="limit_ioc",
        base_size=D("1"),
        quote_size=D("100.10"),
        limit_price=D("100.10"),
        reference_price=D("100"),
        approved_target_exposure=D("0.2"),
        created_at=NOW,
    )


def candle_rows(seconds: int, count: int, end: int) -> list[dict]:
    return [
        {
            "start": str(end - (count - i) * seconds),
            "open": str(100 + i / 100),
            "close": str(100 + i / 100),
            "high": str(101 + i / 100),
            "low": str(99 + i / 100),
            "volume": str(100 + i % 20),
        }
        for i in range(count)
    ]


@pytest.fixture
def sdk(actual):
    client = Mock(spec=RESTClient)
    client.get_api_key_permissions.return_value = {
        "can_view": True,
        "can_trade": True,
        "can_transfer": False,
        "portfolio_uuid": "test-portfolio",
    }
    account_state = {"value": actual}

    def accounts(**kwargs):
        value = account_state["value"]
        return {
            "has_next": False,
            "cursor": "",
            "accounts": [
                {
                    "currency": c,
                    "available_balance": {"value": str(b.available), "currency": c},
                    "hold": {"value": str(b.hold), "currency": c},
                    "active": True,
                    "ready": True,
                    "retail_portfolio_id": value.portfolio_id,
                }
                for c, b in value.balances.items()
            ],
        }

    def products(product_id, **kwargs):
        return {
            "product_id": product_id,
            "product_type": "SPOT",
            "base_currency_id": product_id.split("-")[0],
            "quote_currency_id": "USDC",
            "status": "online",
            "base_increment": "0.00001",
            "quote_increment": "0.01",
            "price_increment": "0.01",
            "base_min_size": "0.00001",
            "base_max_size": "1000000",
            "quote_min_size": "1",
            "quote_max_size": "1000000",
            **dict.fromkeys(
                [
                    "is_disabled",
                    "trading_disabled",
                    "cancel_only",
                    "post_only",
                    "auction_mode",
                    "view_only",
                    "limit_only",
                ],
                False,
            ),
        }

    def quotes(product_ids):
        return {
            "pricebooks": [
                {
                    "product_id": p,
                    "bids": [{"price": "99.99"}],
                    "asks": [{"price": "100.01"}],
                    "time": NOW.isoformat(),
                }
                for p in product_ids
            ]
        }

    def candles(product_id, start, end, granularity, limit):
        from trader.config import GRANULARITIES

        return {"candles": candle_rows(GRANULARITIES[granularity], limit, int(end))}

    client.get_accounts.side_effect = accounts
    client.get_product.side_effect = products
    client.get_best_bid_ask.side_effect = quotes
    client.get_candles.side_effect = candles
    client.list_orders.return_value = {"orders": [], "has_next": False, "cursor": ""}
    client.get_fills.return_value = {"fills": [], "cursor": ""}
    client.account_state = account_state
    return client


@pytest.fixture
def adapter(sdk):
    return CoinbaseAdapter(sdk, "main", "test-portfolio")


def response_for(batch: DecisionBatch, **changes):
    defaults = {
        "id": "test-response",
        "status": "completed",
        "output_text": dumps(batch),
        "output": [],
        "usage": SimpleNamespace(
            input_tokens=2000,
            output_tokens=300,
            input_tokens_details=SimpleNamespace(cached_tokens=1000),
            output_tokens_details=SimpleNamespace(reasoning_tokens=100),
        ),
    }
    return SimpleNamespace(**(defaults | changes))


@pytest.fixture
def openai_mock(decision):
    client = Mock()
    batch = DecisionBatch(
        market_regime="mixed",
        decisions=[decision.model_copy(update={"product_id": p}) for p in PRODUCTS],
    )
    client.responses.create.return_value = response_for(batch)
    return client
