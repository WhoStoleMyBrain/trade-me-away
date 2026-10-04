import json
import logging
import warnings
from unittest.mock import Mock

import pytest

from trader import portfolio_id

PORTFOLIO_UUID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def lookup(monkeypatch):
    client = Mock(spec=["get_api_key_permissions"])
    client.get_api_key_permissions.return_value.to_dict.return_value = {
        "can_view": True,
        "portfolio_uuid": PORTFOLIO_UUID,
        "private_response_field": "DO_NOT_PRINT_RESPONSE",
    }
    factory = Mock(return_value=client)
    monkeypatch.setattr(portfolio_id, "RESTClient", factory)
    answers = iter(["test-key-name", "test-private\\nkey"])
    monkeypatch.setattr(portfolio_id.getpass, "getpass", lambda _: next(answers))
    return factory, client


def test_hidden_prompts_and_only_uuid_output(lookup, capsys):
    factory, client = lookup
    assert portfolio_id.main([]) == 0
    factory.assert_called_once_with(
        api_key="test-key-name",
        api_secret="test-private\nkey",
        timeout=15,
        verbose=False,
    )
    client.get_api_key_permissions.assert_called_once_with()
    captured = capsys.readouterr()
    assert captured.out == PORTFOLIO_UUID + "\n"
    assert captured.err == ""


def test_downloaded_key_file(lookup, tmp_path, capsys):
    factory, _ = lookup
    key_file = tmp_path / "key.json"
    key_file.write_text(
        json.dumps({"name": "file-key", "privateKey": "file-private\nkey"}),
        encoding="utf-8",
    )
    assert portfolio_id.main(["--key-file", str(key_file)]) == 0
    factory.assert_called_once_with(
        api_key="file-key",
        api_secret="file-private\nkey",
        timeout=15,
        verbose=False,
    )
    assert capsys.readouterr().out == PORTFOLIO_UUID + "\n"


@pytest.mark.parametrize(
    "permissions",
    [
        {"can_view": False, "portfolio_uuid": PORTFOLIO_UUID},
        {"can_view": "true", "portfolio_uuid": PORTFOLIO_UUID},
        {"can_view": True},
        {"can_view": True, "portfolio_uuid": "NOT_A_UUID"},
    ],
)
def test_invalid_permissions_or_uuid_are_not_printed(lookup, capsys, permissions):
    _, client = lookup
    client.get_api_key_permissions.return_value.to_dict.return_value = permissions
    assert portfolio_id.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Portfolio lookup failed." in captured.err
    assert PORTFOLIO_UUID not in captured.err
    assert "NOT_A_UUID" not in captured.err


def test_sdk_error_and_log_are_suppressed(lookup, capsys, caplog):
    _, client = lookup
    previous_logging_threshold = logging.root.manager.disable

    def fail():
        logging.getLogger("coinbase.RESTClient").error("DO_NOT_PRINT_RESPONSE")
        raise RuntimeError("DO_NOT_PRINT_SECRET")

    client.get_api_key_permissions.side_effect = fail
    assert portfolio_id.main([]) == 1
    assert logging.root.manager.disable == previous_logging_threshold
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DO_NOT_PRINT" not in captured.err + caplog.text


def test_no_visible_input_fallback(lookup, monkeypatch, capsys):
    factory, _ = lookup

    def insecure_prompt(_):
        warnings.warn("No terminal", portfolio_id.getpass.GetPassWarning, stacklevel=2)
        return "DO_NOT_READ_VISIBLE_INPUT"

    monkeypatch.setattr(portfolio_id.getpass, "getpass", insecure_prompt)
    assert portfolio_id.main([]) == 1
    factory.assert_not_called()
    assert capsys.readouterr().out == ""


def test_malformed_key_file_never_calls_api(lookup, tmp_path, capsys):
    factory, _ = lookup
    key_file = tmp_path / "key.json"
    key_file.write_text("DO_NOT_PRINT_SECRET", encoding="utf-8")
    assert portfolio_id.main(["--key-file", str(key_file)]) == 1
    factory.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DO_NOT_PRINT_SECRET" not in captured.err
