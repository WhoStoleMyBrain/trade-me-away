import logging
from unittest.mock import Mock

import pytest
import yaml

from trader.__main__ import main
from trader.config import active_mode, load_config
from trader.errors import SafetyError
from trader.util import D, configure_logging


def write_config(cfg, monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    monkeypatch.setenv("TRADER_CONFIG", str(path))
    monkeypatch.setenv("TRADER_DB", str(tmp_path / "state.sqlite3"))
    return path


def test_duplicate_environment_values_fail_closed(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRADING_MODE=paper\nTRADING_MODE=live\n")
    with pytest.raises(SafetyError, match="AMBIGUOUS_ENV_FILE"):
        active_mode(path)


def test_duplicate_yaml_and_unknown_fields(cfg, monkeypatch, tmp_path):
    path = write_config(cfg, monkeypatch, tmp_path)
    original = path.read_text()
    path.write_text(original + "llm: {}\n")
    with pytest.raises(SafetyError, match="CONFIGURATION_INVALID"):
        load_config()
    path.write_text(original + "trading_mode: live\n")
    with pytest.raises(SafetyError, match="CONFIGURATION_INVALID"):
        load_config()


def test_model_changes_require_explicit_matching_pricing(cfg, monkeypatch, tmp_path):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_MODEL", "different-model")
    with pytest.raises(SafetyError):
        load_config()


def test_yaml_money_preserves_decimal_literal(cfg, monkeypatch, tmp_path):
    path = write_config(cfg, monkeypatch, tmp_path)
    path.write_text(
        path.read_text().replace(
            "max_order_notional: '100'", "max_order_notional: 100.00000000000000001"
        )
    )
    _, loaded = load_config()
    assert loaded.risk.max_order_notional == D("100.00000000000000001")


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_doctor_shows_mode_without_trading(
    cfg, adapter, sdk, openai_mock, monkeypatch, tmp_path, capsys, mode
):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setattr("trader.coinbase_client.build_adapters", lambda *_: {"main": adapter})
    monkeypatch.setattr("openai.OpenAI", lambda **_: openai_mock)
    assert main(["doctor"]) == 0
    assert f"TRADING MODE: {mode.upper()}" in capsys.readouterr().out
    openai_mock.models.retrieve.assert_called_once_with(cfg.llm.model)
    openai_mock.responses.create.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()
    sdk.market_order_buy.assert_not_called()


def test_cli_errors_never_print_secrets(cfg, monkeypatch, tmp_path, capsys):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "trader.coinbase_client.build_adapters", Mock(side_effect=RuntimeError("SECRET_SENTINEL"))
    )
    assert main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "SECRET_SENTINEL" not in captured.out + captured.err


def test_sdk_owned_log_handler_is_silenced(capsys):
    import requests
    from coinbase.rest.rest_base import handle_exception

    configure_logging()
    response = requests.Response()
    response.status_code = 401
    response._content = b"SECRET_SENTINEL"
    with pytest.raises(requests.HTTPError):
        handle_exception(response)
    captured = capsys.readouterr()
    assert "SECRET_SENTINEL" not in captured.out + captured.err
    assert logging.getLogger("coinbase.RESTClient").propagate is False


def test_inspection_is_available_without_network(cfg, monkeypatch, tmp_path, capsys):
    write_config(cfg, monkeypatch, tmp_path)
    build = Mock(side_effect=AssertionError("network adapter should not be built"))
    monkeypatch.setattr("trader.coinbase_client.build_adapters", build)
    for command in ("show-orders", "show-costs", "show-state"):
        assert main([command]) == 0
    build.assert_not_called()


def test_missing_mode_fails_before_database_or_network(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TRADING_MODE")
    assert main(["doctor"]) == 1
    assert "INVALID_TRADING_MODE" in capsys.readouterr().out
    assert not (tmp_path / "var").exists()
