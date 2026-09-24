from datetime import timedelta

import pytest
from conftest import NOW, PRODUCTS, response_for

from trader.errors import SafetyError
from trader.execution import PaperExecutor
from trader.llm import DecisionClient
from trader.market_data import MarketData
from trader.orchestrator import Orchestrator, check_schedule
from trader.schemas import Action, Balance, DecisionBatch
from trader.util import D


def service(cfg, store, adapter, openai_mock):
    adapters = {"main": adapter}
    return Orchestrator(
        cfg,
        "paper",
        store,
        MarketData(cfg, adapters, store),
        DecisionClient(openai_mock, cfg.llm, store),
        PaperExecutor(cfg, store, adapters),
    )


def test_complete_joint_paper_cycle(cfg, store, adapter, sdk, openai_mock):
    app = service(cfg, store, adapter, openai_mock)
    cycle = app.run()
    assert openai_mock.responses.create.call_count == 1
    for product in PRODUCTS:
        assert product in openai_mock.responses.create.call_args.kwargs["input"]
    assert len(store.rows("computed_features")) == 3
    assert len(store.rows("market_snapshots")) == 3
    assert len(store.rows("risk_results")) == 6
    assert len(store.rows("orders")) == 3
    assert len(store.rows("fills")) == 3
    assert len(store.rows("api_usage")) == 1
    assert store.rows("trading_cycles")[0]["status"] == "COMPLETED"
    assert store.rows("trading_cycles")[0]["cycle_id"] == cycle
    assert store.ledger("paper", "main").cash < 1000
    assert store.exchange("main")["balances"]["USDC"] == "1000"
    sdk.limit_order_ioc.assert_not_called()
    with pytest.raises(SafetyError, match="DUPLICATE_CYCLE"):
        app.run()
    assert openai_mock.responses.create.call_count == 1


def test_hold_cycle_has_no_orders(cfg, store, adapter, sdk, openai_mock):
    batch = DecisionBatch.model_validate_json(openai_mock.responses.create.return_value.output_text)
    batch = batch.model_copy(
        update={
            "decisions": [
                d.model_copy(update={"action": Action.HOLD, "target_exposure": 0.0})
                for d in batch.decisions
            ]
        }
    )
    openai_mock.responses.create.return_value = response_for(batch)
    service(cfg, store, adapter, openai_mock).run()
    assert not store.rows("orders")
    assert openai_mock.responses.create.call_count == 1


@pytest.mark.parametrize(
    "problem", ["candles", "balance", "open_order", "stale", "permission", "budget", "fees"]
)
def test_preflight_failures_skip_openai(cfg, store, adapter, sdk, actual, openai_mock, problem):
    app = service(cfg, store, adapter, openai_mock)
    if problem == "candles":
        sdk.get_candles.side_effect = lambda **kwargs: {"candles": []}
    elif problem == "balance":
        from trader.portfolio import reconcile_actual

        reconcile_actual(store, actual, cfg, "c", "paper")
        sdk.account_state["value"] = actual.model_copy(
            update={
                "balances": actual.balances | {"USDC": Balance(available=D("900"), hold=D("0"))}
            }
        )
    elif problem == "open_order":
        sdk.list_orders.return_value = {
            "has_next": False,
            "orders": [
                {
                    "order_id": "outside",
                    "client_order_id": "outside",
                    "product_id": "BTC-USDC",
                    "side": "BUY",
                    "status": "OPEN",
                    "retail_portfolio_id": "test-portfolio",
                }
            ],
        }
    elif problem == "stale":
        sdk.get_best_bid_ask.side_effect = lambda product_ids: {
            "pricebooks": [
                {
                    "product_id": product_ids[0],
                    "bids": [{"price": "99.99"}],
                    "asks": [{"price": "100.01"}],
                    "time": (NOW - timedelta(hours=1)).isoformat(),
                }
            ]
        }
    elif problem == "permission":
        sdk.get_api_key_permissions.return_value["portfolio_uuid"] = "wrong"
    elif problem == "fees":
        sdk.get_transaction_summary.return_value["fee_tier"]["taker_fee_rate"] = "0.012"
    else:
        store.reserve_request("spent", "past", cfg.llm, cfg.llm.daily_budget_usd, NOW)
    with pytest.raises(SafetyError):
        app.run()
    openai_mock.responses.create.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()
    assert store.rows("trading_cycles")[0]["status"] == "HOLD"


def test_preexecution_price_move_aborts_without_second_model_call(
    cfg, store, adapter, sdk, openai_mock
):
    original_response = openai_mock.responses.create.return_value

    def changed_market(**kwargs):
        sdk.get_best_bid_ask.side_effect = lambda product_ids: {
            "pricebooks": [
                {
                    "product_id": product_ids[0],
                    "bids": [{"price": "119.99"}],
                    "asks": [{"price": "120.01"}],
                    "time": NOW.isoformat(),
                }
            ]
        }
        return original_response

    openai_mock.responses.create.side_effect = changed_market
    with pytest.raises(SafetyError, match="PRE_EXECUTION_ABORT"):
        service(cfg, store, adapter, openai_mock).run()
    assert not store.rows("orders")
    assert openai_mock.responses.create.call_count == 1
    assert any("PRICE_MOVED" in r["payload_json"] for r in store.rows("risk_results"))


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_fee_increase_after_decision_prevents_submission(
    cfg,
    store,
    adapter,
    sdk,
    openai_mock,
    monkeypatch,
    mode,
):
    from trader.execution import make_executor

    monkeypatch.setenv("TRADING_MODE", mode)
    response = openai_mock.responses.create.return_value

    def changed_fee(**kwargs):
        sdk.get_transaction_summary.return_value["fee_tier"]["taker_fee_rate"] = "0.012"
        return response

    openai_mock.responses.create.side_effect = changed_fee
    adapters = {"main": adapter}
    app = Orchestrator(
        cfg,
        mode,
        store,
        MarketData(cfg, adapters, store),
        DecisionClient(openai_mock, cfg.llm, store),
        make_executor(mode, cfg, store, adapters),
    )
    with pytest.raises(SafetyError, match="FEE_RATE_UNDERESTIMATED"):
        app.run()
    assert openai_mock.responses.create.call_count == 1
    assert not store.rows("order_intents")
    sdk.limit_order_ioc.assert_not_called()


def test_scheduled_late_restart_is_rejected():
    check_schedule(NOW)
    with pytest.raises(SafetyError, match="SCHEDULE"):
        check_schedule(NOW + timedelta(hours=1))
    with pytest.raises(SafetyError, match="SCHEDULE"):
        check_schedule(NOW.replace(minute=0))


def test_shared_portfolio_daily_trade_limit_applies_midcycle(cfg, store, adapter, openai_mock):
    cfg.risk.max_trades_per_day = 1
    service(cfg, store, adapter, openai_mock).run()
    assert len(store.rows("orders")) == 1
    assert any("DAILY_TRADE_LIMIT" in r["payload_json"] for r in store.rows("risk_results"))


def test_multiple_portfolios_are_joint_but_do_not_share_cash(
    cfg, store, adapter, actual, openai_mock
):
    import json
    from unittest.mock import Mock

    from trader.coinbase_client import CoinbaseAdapter
    from trader.config import AssetConfig, PortfolioConfig

    cfg.portfolios["eth"] = PortfolioConfig(
        portfolio_id_env="ETH_PORTFOLIO",
        api_key_env="ETH_KEY",
        api_secret_env="ETH_SECRET",
        paper_initial_usdc=D("2000"),
    )
    cfg.assets = [
        AssetConfig(
            product_id=a.product_id, portfolio="eth" if a.product_id == "ETH-USDC" else "main"
        )
        for a in cfg.assets
    ]
    second = Mock(spec=CoinbaseAdapter)
    second.account.return_value = actual.model_copy(
        update={"portfolio": "eth", "portfolio_id": "eth-portfolio"}
    )
    second.product.side_effect = adapter.product
    second.quote.side_effect = adapter.quote
    second.candles.side_effect = adapter.candles
    adapters = {"main": adapter, "eth": second}
    app = Orchestrator(
        cfg,
        "paper",
        store,
        MarketData(cfg, adapters, store),
        DecisionClient(openai_mock, cfg.llm, store),
        PaperExecutor(cfg, store, adapters),
    )
    app.run()
    payload = json.loads(openai_mock.responses.create.call_args.kwargs["input"])
    assert D(payload["portfolios"]["main"]["equity"]) == D("1000")
    assert D(payload["portfolios"]["eth"]["equity"]) == D("2000")
    assert set(store.ledger("paper", "eth").positions) == {"ETH-USDC"}
    assert len(store.rows("orders")) == 3
    assert openai_mock.responses.create.call_count == 1


def test_complete_live_cycle_uses_same_pipeline_with_mocked_orders(
    cfg, store, adapter, sdk, openai_mock, monkeypatch
):
    from trader.execution import CoinbaseLiveExecutor

    monkeypatch.setenv("TRADING_MODE", "live")
    orders, fills = {}, []

    def submit(client_order_id, product_id, side, base_size, limit_price):
        order_id = f"exchange-{client_order_id}"
        size, price = D(base_size), D("100.05")
        fee = size * price * cfg.execution.taker_fee_rate
        order = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side,
            "retail_portfolio_id": "test-portfolio",
            "product_type": "SPOT",
            "status": "FILLED",
            "number_of_fills": "1",
            "pending_cancel": False,
            "settled": True,
            "filled_size": base_size,
            "filled_value": str(size * price),
            "total_fees": str(fee),
        }
        orders[order_id] = order
        fills.append(
            {
                "entry_id": order_id + "-fill",
                "order_id": order_id,
                "product_id": product_id,
                "side": side,
                "retail_portfolio_id": "test-portfolio",
                "price": str(price),
                "size": base_size,
                "commission": str(fee),
                "size_in_quote": False,
                "trade_time": NOW.isoformat(),
            }
        )
        actual = sdk.account_state["value"]
        balances = dict(actual.balances)
        currency = product_id.split("-")[0]
        balances[currency] = Balance(available=balances[currency].available + size, hold=D("0"))
        balances["USDC"] = Balance(
            available=balances["USDC"].available - size * price - fee, hold=D("0")
        )
        sdk.account_state["value"] = actual.model_copy(update={"balances": balances})
        return {
            "success": True,
            "success_response": {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "product_id": product_id,
                "side": side,
            },
        }

    sdk.limit_order_ioc.side_effect = submit
    sdk.get_order.side_effect = lambda order_id: {"order": orders[order_id]}
    sdk.get_fills.side_effect = lambda **kw: {
        "fills": [f for f in fills if not kw.get("order_ids") or f["order_id"] in kw["order_ids"]],
        "cursor": "",
    }
    adapters = {"main": adapter}
    app = Orchestrator(
        cfg,
        "live",
        store,
        MarketData(cfg, adapters, store),
        DecisionClient(openai_mock, cfg.llm, store),
        CoinbaseLiveExecutor(cfg, store, adapters),
    )
    app.run()
    assert openai_mock.responses.create.call_count == 1
    assert sdk.limit_order_ioc.call_count == 3
    assert len({c.kwargs["client_order_id"] for c in sdk.limit_order_ioc.call_args_list}) == 3
    assert len(store.rows("fills")) == 3
    assert all(o["mode"] == "live" and o["status"] == "FILLED" for o in store.rows("orders"))
    assert (
        store.ledger("live", "main").cash == sdk.account_state["value"].balances["USDC"].available
    )
    assert not store.unresolved()
