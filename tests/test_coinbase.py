from unittest.mock import Mock

import pytest
import requests

from trader.errors import SafetyError
from trader.util import safe_read


def test_portfolio_permissions_and_product_type(adapter, sdk):
    adapter.check_permissions("live")
    sdk.get_api_key_permissions.return_value["can_transfer"] = True
    with pytest.raises(SafetyError, match="TRANSFER_PERMISSION"):
        adapter.check_permissions("live")
    response = sdk.get_product.side_effect("BTC-USDC")
    sdk.get_product.side_effect = None
    sdk.get_product.return_value = response | {"product_type": "FUTURE"}
    with pytest.raises(SafetyError, match="PRODUCT_IDENTITY_OR_TYPE"):
        adapter.product("BTC-USDC")


def test_complete_pagination_and_repeated_cursor_fails(adapter, sdk):
    sdk.list_orders.side_effect = [
        {"orders": [{"order_id": "1"}], "has_next": True, "cursor": "a"},
        {"orders": [{"order_id": "2"}], "has_next": False, "cursor": ""},
    ]
    assert len(adapter.pages("list_orders", "orders")) == 2
    assert sdk.list_orders.call_args.kwargs["cursor"] == "a"
    sdk.list_orders.side_effect = None
    sdk.list_orders.return_value = {"orders": [], "has_next": True, "cursor": "a"}
    with pytest.raises(SafetyError, match="PAGINATION_INCOMPLETE"):
        adapter.pages("list_orders", "orders")


def test_sca_requirement_does_not_look_like_empty_fills(adapter, sdk):
    sdk.get_fills.return_value = {"fills": [], "cursor": "", "proof_token_required": True}
    with pytest.raises(SafetyError, match="HISTORY_AUTHENTICATION_REQUIRED"):
        adapter.pages("get_fills", "fills")


def test_safe_reads_retry_only_transient_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr("trader.util.time.sleep", sleeps.append)
    call = Mock(side_effect=[requests.Timeout(), requests.ConnectionError(), {"ok": True}])
    assert safe_read(call) == {"ok": True}
    assert sleeps == [0.5, 1.0]
    response = requests.Response()
    response.status_code = 401
    call = Mock(side_effect=requests.HTTPError("DO_NOT_LOG", response=response))
    with pytest.raises(SafetyError, match="READ_REJECTED"):
        safe_read(call)
    assert call.call_count == 1


def test_read_exhaustion_fails_closed(monkeypatch):
    monkeypatch.setattr("trader.util.time.sleep", lambda _: None)
    call = Mock(side_effect=requests.Timeout())
    with pytest.raises(SafetyError, match="READ_UNAVAILABLE"):
        safe_read(call)
    assert call.call_count == 3


def test_quote_product_identity_and_missing_spread(adapter, sdk):
    sdk.get_best_bid_ask.side_effect = None
    sdk.get_best_bid_ask.return_value = {"pricebooks": []}
    with pytest.raises(SafetyError):
        adapter.quote("BTC-USDC")


def test_coinbase_cdp_portfolio_is_not_selected_by_deprecated_order_parameter(adapter, sdk, intent):
    sdk.limit_order_ioc.return_value = {"success": False}
    adapter.submit(intent)
    params = sdk.limit_order_ioc.call_args.kwargs
    assert "retail_portfolio_id" not in params
    assert "leverage" not in params and "margin_type" not in params
    assert params["client_order_id"] == intent.client_order_id


@pytest.mark.parametrize(
    "order_type,side,configuration",
    [
        ("limit_ioc", "BUY", {"sor_limit_ioc": {"base_size": "1", "limit_price": "100.10"}}),
        ("market_ioc", "BUY", {"market_market_ioc": {"quote_size": "100.10"}}),
        ("market_ioc", "SELL", {"market_market_ioc": {"base_size": "1"}}),
    ],
)
def test_official_coinbase_sdk_http_contract(intent, monkeypatch, order_type, side, configuration):
    from coinbase.rest import RESTClient

    from trader.coinbase_client import CoinbaseAdapter

    client = RESTClient(api_key="unit-test-unused", api_secret="unit-test-unused", timeout=15)
    monkeypatch.setattr(client, "set_headers", lambda *_: {})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"success":false,"error_response":{"error":"MOCK_REJECTION"}}'
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)
    adapter = CoinbaseAdapter(client, "main", "test-portfolio")
    adapter.submit(intent.model_copy(update={"order_type": order_type, "side": side}))
    assert request.call_count == 1
    assert request.call_args.args == ("POST", "https://api.coinbase.com/api/v3/brokerage/orders")
    assert request.call_args.kwargs["json"] == {
        "client_order_id": intent.client_order_id,
        "product_id": "BTC-USDC",
        "side": side,
        "order_configuration": configuration,
    }
    client.session.close()


def test_quote_denominated_fills_are_converted_once(adapter, sdk, intent):
    from conftest import NOW

    sdk.get_fills.return_value = {
        "fills": [
            {
                "entry_id": "f",
                "order_id": "o",
                "product_id": "BTC-USDC",
                "side": "BUY",
                "retail_portfolio_id": "test-portfolio",
                "price": "100",
                "size": "50",
                "commission": "0.3",
                "size_in_quote": True,
                "trade_time": NOW.isoformat(),
            }
        ],
        "cursor": "",
    }
    fills = adapter.fills("o", intent)
    from trader.util import D

    assert fills[0].base_size == D("0.5")


def test_fee_assumption_must_cover_exchange_tier(adapter, sdk, cfg):
    adapter.check_fee_rate(cfg.execution.taker_fee_rate)
    sdk.get_transaction_summary.assert_called_once_with(product_type="SPOT")
    sdk.get_transaction_summary.return_value["fee_tier"]["taker_fee_rate"] = "0.012"
    with pytest.raises(SafetyError, match="FEE_RATE_UNDERESTIMATED"):
        adapter.check_fee_rate(cfg.execution.taker_fee_rate)


@pytest.mark.parametrize(
    "fee_state",
    [
        {"has_cost_plus_commission": True},
        {"has_cost_plus_commission": None},
        {"goods_and_services_tax": {"rate": "0.1", "type": "EXCLUSIVE"}},
    ],
)
def test_special_or_unknown_fees_fail_closed(adapter, sdk, cfg, fee_state):
    sdk.get_transaction_summary.return_value.update(fee_state)
    with pytest.raises(SafetyError, match="UNSUPPORTED_FEE_SCHEDULE"):
        adapter.check_fee_rate(cfg.execution.taker_fee_rate)


def test_official_cancel_sdk_http_contract(monkeypatch):
    from coinbase.rest import RESTClient

    from trader.coinbase_client import CoinbaseAdapter

    client = RESTClient(api_key="unit-test-unused", api_secret="unit-test-unused", timeout=15)
    monkeypatch.setattr(client, "set_headers", lambda *_: {})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"results":[{"order_id":"owned-id","success":true}]}'
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)
    assert CoinbaseAdapter(client, "main", "test-portfolio").cancel("owned-id") is True
    assert request.call_args.args == (
        "POST",
        "https://api.coinbase.com/api/v3/brokerage/orders/batch_cancel",
    )
    assert request.call_args.kwargs["json"] == {"order_ids": ["owned-id"]}
    request.assert_called_once()
    client.session.close()


def test_official_fee_sdk_http_contract(monkeypatch, cfg):
    from coinbase.rest import RESTClient

    from trader.coinbase_client import CoinbaseAdapter

    client = RESTClient(api_key="unit-test-unused", api_secret="unit-test-unused", timeout=15)
    monkeypatch.setattr(client, "set_headers", lambda *_: {})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"fee_tier":{"taker_fee_rate":"0.006"},"has_cost_plus_commission":false}'
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)
    CoinbaseAdapter(client, "main", "test-portfolio").check_fee_rate(cfg.execution.taker_fee_rate)
    request.assert_called_once()
    assert request.call_args.args == (
        "GET",
        "https://api.coinbase.com/api/v3/brokerage/transaction_summary",
    )
    assert request.call_args.kwargs["params"] == {"product_type": "SPOT"}
    client.session.close()


@pytest.mark.parametrize("granularity,count", [("FOUR_HOUR", 180), ("ONE_DAY", 120)])
def test_official_long_candle_sdk_http_contract(monkeypatch, granularity, count):
    from coinbase.rest import RESTClient

    from trader.coinbase_client import CoinbaseAdapter

    client = RESTClient(api_key="unit-test-unused", api_secret="unit-test-unused", timeout=15)
    monkeypatch.setattr(client, "set_headers", lambda *_: {})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"candles":[]}'
    request = Mock(return_value=response)
    monkeypatch.setattr(client.session, "request", request)
    adapter = CoinbaseAdapter(client, "main", "test-portfolio")
    assert adapter.candles("BTC-USDC", granularity, 100, 200, count) == []
    assert request.call_args.args == (
        "GET",
        "https://api.coinbase.com/api/v3/brokerage/products/BTC-USDC/candles",
    )
    assert request.call_args.kwargs["params"] == {
        "start": "100",
        "end": "200",
        "granularity": granularity,
        "limit": count,
    }
    request.assert_called_once()
    client.session.close()
