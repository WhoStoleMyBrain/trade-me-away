import json
import logging
from unittest.mock import Mock

import httpx
import pytest
import requests
from conftest import NOW, PRODUCTS, candle_rows
from openai import APIConnectionError, APIStatusError
from test_config_cli import write_config
from test_execution import initialize
from test_orchestrator import service

from trader.__main__ import main
from trader.errors import SafetyError
from trader.execution import CoinbaseLiveExecutor
from trader.features import closed_candles
from trader.llm import DecisionClient
from trader.util import D


def events(caplog, name):
    return [
        row
        for record in caplog.records
        if record.name == "trader" and (row := json.loads(record.message)).get("event") == name
    ]


@pytest.mark.parametrize(
    "damage,validation",
    [
        ("count", "COUNT_MISMATCH"),
        ("duplicate", "DUPLICATE_OR_MISALIGNED_START"),
        ("stale", "GAP_OR_STALE_CANDLE"),
        ("nan", "NONFINITE_VALUE"),
        ("negative", "NONPOSITIVE_PRICE_OR_NEGATIVE_VOLUME"),
        ("ohlc", "OHLC_BOUNDS_INVALID"),
        ("text", "FIELDS_OR_NUMBERS_INVALID"),
    ],
)
def test_candle_validation_details_are_safe(damage, validation):
    end = int(NOW.timestamp()) // 300 * 300
    rows = candle_rows(300, 100, end)
    if damage == "count":
        rows.pop()
    elif damage == "duplicate":
        rows[-1] = rows[-2]
    elif damage == "stale":
        rows = candle_rows(300, 100, end - 300)
    elif damage == "nan":
        rows[-1]["close"] = float("nan")
    elif damage == "negative":
        rows[-1]["volume"] = "-1"
    elif damage == "ohlc":
        rows[-1]["high"] = "1"
    else:
        rows[-1]["close"] = "SECRET_SENTINEL"
    with pytest.raises(SafetyError, match="CANDLE_DATA_INSUFFICIENT_OR_INVALID") as caught:
        closed_candles(rows, 300, 100, NOW)
    assert caught.value.details == {
        "validation": validation,
        "interval_seconds": 300,
        "expected_count": 100,
        "received_count": len(rows),
        "closed_count": len(rows),
    }
    assert "SECRET_SENTINEL" not in json.dumps(caught.value.details)


def test_failed_candle_context_reaches_cycle_log_and_audit(
    cfg, store, adapter, sdk, openai_mock, caplog
):
    caplog.set_level(logging.INFO, logger="trader")
    original = sdk.get_candles.side_effect

    def damaged(**kwargs):
        response = original(**kwargs)
        if kwargs["product_id"] == "ETH-USDC" and kwargs["granularity"] == "ONE_HOUR":
            response["candles"].pop(40)
        return response

    sdk.get_candles.side_effect = damaged
    with pytest.raises(SafetyError, match="CANDLE_DATA_INSUFFICIENT_OR_INVALID") as caught:
        service(cfg, store, adapter, openai_mock).run()
    details = caught.value.details
    assert details["product_id"] == "ETH-USDC"
    assert details["timeframe"] == "ONE_HOUR"
    assert details["stage"] == "market_snapshot"
    assert details["validation"] == "COUNT_MISMATCH"
    assert details["expected_count"] == 300
    assert details["received_count"] == details["closed_count"] == 299
    failure = events(caplog, "cycle_failed_closed")[0]
    assert failure["details"] == details
    audit = store.rows("risk_results")[0]
    assert audit["cycle_id"] == failure["cycle_id"]
    assert json.loads(audit["payload_json"])["details"] == details
    assert store.rows("trading_cycles")[0]["status"] == "HOLD"
    assert not store.rows("orders") and not store.rows("fills")
    openai_mock.responses.create.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 429, 503, None])
def test_coinbase_error_metadata_preserves_retry_policy(adapter, sdk, monkeypatch, caplog, status):
    caplog.set_level(logging.INFO, logger="trader")
    monkeypatch.setattr("trader.util.time.sleep", lambda _: None)
    if status is None:
        error = requests.Timeout("SECRET_SENTINEL")
    else:
        response = requests.Response()
        response.status_code = status
        response._content = b"SECRET_SENTINEL"
        response.url = "https://example.invalid/SECRET_SENTINEL"
        response.headers["Authorization"] = "SECRET_SENTINEL"
        error = requests.HTTPError("SECRET_SENTINEL", response=response)
    sdk.get_product.side_effect = error
    retryable = status is None or status == 429 or status >= 500
    reason = "COINBASE_READ_UNAVAILABLE" if retryable else "COINBASE_READ_REJECTED"
    attempts = 3 if retryable else 1
    with pytest.raises(SafetyError, match=reason) as caught:
        adapter.product("BTC-USDC")
    assert sdk.get_product.call_count == attempts
    details = caught.value.details
    assert details["operation"] == "get_product"
    assert details["product_id"] == "BTC-USDC"
    assert details["portfolio"] == "main"
    assert details["http_status"] == status
    assert details["attempt"] == attempts
    assert details["retryable"] == retryable
    records = events(caplog, "coinbase_read_error")
    assert [r["details"]["attempt"] for r in records] == list(range(1, attempts + 1))
    assert "SECRET_SENTINEL" not in caplog.text + json.dumps(details)


@pytest.mark.parametrize("doctor", [False, True])
@pytest.mark.parametrize("status", [400, 401, 429, 503, None])
def test_openai_error_metadata_preserves_retries_and_budget(
    cfg, store, openai_mock, monkeypatch, caplog, status, doctor
):
    caplog.set_level(logging.INFO, logger="trader")
    monkeypatch.setattr("trader.llm.time.sleep", lambda _: None)
    request = httpx.Request(
        "POST",
        "https://example.invalid/SECRET_SENTINEL",
        headers={"Authorization": "SECRET_SENTINEL"},
    )
    error = (
        APIConnectionError(message="SECRET_SENTINEL", request=request)
        if status is None
        else APIStatusError(
            "SECRET_SENTINEL",
            response=httpx.Response(status, request=request, text="SECRET_SENTINEL"),
            body={"error": "SECRET_SENTINEL"},
        )
    )
    call = openai_mock.models.retrieve if doctor else openai_mock.responses.create
    call.side_effect = error
    llm = DecisionClient(openai_mock, cfg.llm, store)
    with pytest.raises(SafetyError) as caught:
        if doctor:
            llm.doctor()
        else:
            llm.decide("{}", set(PRODUCTS), "cycle", "paper")
    expected_code = (
        "OPENAI_CONNECTIVITY_OR_MODEL_ACCESS_FAILED" if doctor else "OPENAI_REQUEST_FAILED"
    )
    assert caught.value.code == expected_code
    retryable = status is None or status == 429 or status >= 500
    attempts = cfg.llm.attempts if retryable else 1
    assert call.call_count == attempts
    details = caught.value.details
    assert details["operation"] == ("models.retrieve" if doctor else "responses.create")
    assert details["http_status"] == status
    assert details["attempt"] == attempts
    records = events(caplog, "openai_error")
    assert [r["details"]["attempt"] for r in records] == list(range(1, attempts + 1))
    usage = store.rows("api_usage")
    assert len(usage) == (0 if doctor else attempts)
    assert all(r["status"] == "ERROR_UNKNOWN_COST" and D(r["estimated_usd"]) > 0 for r in usage)
    assert "SECRET_SENTINEL" not in caplog.text + json.dumps(details)


@pytest.mark.parametrize("stage", ["startup", "market_snapshot", "model_request", "execution"])
def test_unexpected_cycle_failure_has_safe_source_and_stage(
    cfg, store, adapter, sdk, openai_mock, monkeypatch, caplog, stage
):
    caplog.set_level(logging.INFO, logger="trader")
    app = service(cfg, store, adapter, openai_mock)
    obj, method = {
        "startup": (app, "startup"),
        "market_snapshot": (app.market, "snapshot"),
        "model_request": (app.llm, "decide"),
        "execution": (app.executor, "execute"),
    }[stage]
    monkeypatch.setattr(obj, method, Mock(side_effect=RuntimeError("SECRET_SENTINEL")))
    with pytest.raises(SafetyError, match="UNEXPECTED_CYCLE_FAILURE") as caught:
        app.run()
    details = caught.value.details
    assert details["stage"] == stage
    assert details["exception_type"] == "RuntimeError"
    assert details["source"]["file"] == "orchestrator.py"
    assert details["source"]["function"] == "run"
    assert details["source"]["line"] > 0
    assert events(caplog, "cycle_failed_closed")[0]["details"] == details
    assert json.loads(store.rows("risk_results")[0]["payload_json"])["details"] == details
    assert not store.rows("orders")
    sdk.limit_order_ioc.assert_not_called()
    assert "SECRET_SENTINEL" not in caplog.text + json.dumps(details)


def test_cli_startup_failure_reports_safe_location(cfg, monkeypatch, tmp_path, capsys):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "trader.coinbase_client.build_adapters", Mock(side_effect=RuntimeError("SECRET_SENTINEL"))
    )
    assert main(["doctor"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out.splitlines()[-1])
    assert result["reason"] == "STARTUP_OR_CONFIGURATION_FAILED"
    assert result["details"]["stage"] == "coinbase_setup"
    assert result["details"]["exception_type"] == "RuntimeError"
    assert result["details"]["source"]["file"] == "__main__.py"
    assert "SECRET_SENTINEL" not in output.out + output.err


def test_submission_error_is_logged_without_repeating_post(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="trader")
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    response = requests.Response()
    response.status_code = 503
    response._content = b"SECRET_SENTINEL"
    sdk.limit_order_ioc.side_effect = requests.HTTPError("SECRET_SENTINEL", response=response)
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "UNRESOLVED"
    assert store.unresolved() and not store.rows("fills")
    sdk.limit_order_ioc.assert_called_once()
    record = events(caplog, "order_submission_error")[0]
    assert record["client_order_id"] == intent.client_order_id
    assert record["details"]["http_status"] == 503
    assert record["details"]["attempt"] == 1
    assert "SECRET_SENTINEL" not in caplog.text
