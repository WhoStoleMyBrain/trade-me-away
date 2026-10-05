import json
import sqlite3
from datetime import timedelta
from unittest.mock import Mock

import pytest
from conftest import NOW
from test_config_cli import write_config

from trader.__main__ import main
from trader.errors import SafetyError
from trader.execution import CoinbaseLiveExecutor
from trader.funding import record_deposit
from trader.portfolio import reconcile_actual, seed_ledger
from trader.schemas import Balance
from trader.storage import Storage, process_lock
from trader.util import D


@pytest.fixture
def funded(cfg, store, actual, sdk, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    reconcile_actual(store, actual, cfg, "initial", "live")
    store.save_ledger("live", "main", seed_ledger(actual, cfg, paper=False))
    store.save_ledger("paper", "main", seed_ledger(actual, cfg, paper=True))
    store.mark_equity("live", "main", D("1000"), NOW)
    changed = actual.model_copy(
        update={"balances": actual.balances | {"USDC": Balance(available=D("1100"), hold=D("0"))}}
    )
    sdk.account_state["value"] = changed
    return changed


def test_deposit_updates_only_live_cash_and_preserves_history(cfg, store, adapter, sdk, funded):
    checkpoint = store.exchange("main")
    live = store.ledger("live", "main")
    paper = store.ledger("paper", "main")
    with pytest.raises(SafetyError):
        reconcile_actual(store, funded, cfg, "before", "live")
    result = record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    assert result["status"] == "RECORDED"
    assert store.ledger("live", "main").cash == D("1100")
    assert store.ledger("live", "main").positions == live.positions
    assert store.ledger("paper", "main") == paper
    assert store.exchange("main")["initialized_at"] == checkpoint["initialized_at"]
    assert store.exchange("main")["balances"]["USDC"] == "1100"
    audit = json.loads(store.deposit("deposit-1")["payload_json"])
    assert audit["expected_before"]["USDC"] == "1000"
    assert audit["actual_balances"]["USDC"] == "1100"
    reconcile_actual(store, funded, cfg, "after", "live")
    executor = CoinbaseLiveExecutor(cfg, store, {"main": adapter})
    assert executor.load_ledger(funded).cash == D("1100")
    sdk.limit_order_ioc.assert_not_called()
    sdk.market_order_buy.assert_not_called()
    sdk.cancel_orders.assert_not_called()


def test_deposit_retry_is_idempotent_and_conflicts_fail(cfg, store, adapter, funded):
    args = (cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    record_deposit(*args)
    assert record_deposit(*args)["status"] == "ALREADY_RECORDED"
    with pytest.raises(SafetyError, match="DEPOSIT_REFERENCE_CONFLICT"):
        record_deposit(cfg, store, {"main": adapter}, "main", "200", "deposit-1")
    with pytest.raises(SafetyError, match="DEPOSIT_AMOUNT_MISMATCH"):
        record_deposit(cfg, store, {"main": adapter}, "main", "100", "different-reference")
    assert len(store.rows("cash_deposits")) == 1
    assert store.ledger("live", "main").cash == D("1100")


@pytest.mark.parametrize(
    "amount", [None, "", "bad", "NaN", "Infinity", "0", "-100", "99", "100.001"]
)
def test_invalid_or_inexact_amount_never_changes_ledgers(cfg, store, adapter, funded, amount):
    before = store.exchange("main")
    with pytest.raises(SafetyError, match="DEPOSIT_AMOUNT_"):
        record_deposit(cfg, store, {"main": adapter}, "main", amount, "deposit-1")
    assert store.exchange("main") == before
    assert store.ledger("live", "main").cash == D("1000")
    assert not store.rows("cash_deposits")


@pytest.mark.parametrize(
    "problem",
    [
        "pending",
        "holding",
        "open_order",
        "crypto",
        "unknown_asset",
        "fill",
        "mapping",
        "stale",
        "gap",
        "ledger_cash",
        "ledger_position",
        "missing_ledger",
        "missing_equity_baseline",
        "permission",
        "mode_change",
    ],
)
def test_deposit_does_not_accept_other_discrepancies(
    cfg, store, adapter, sdk, funded, actual, intent, monkeypatch, problem
):
    if problem == "pending":
        store.begin_cycle(intent.cycle_id, "pending", "live", cfg, NOW)
        store.save_intent(intent.model_copy(update={"mode": "live"}))
    elif problem == "holding":
        funded = funded.model_copy(
            update={
                "balances": funded.balances
                | {
                    "USDC": Balance(available=D("1099"), hold=D("1")),
                }
            }
        )
    elif problem == "open_order":
        funded = funded.model_copy(update={"open_orders": [{"order_id": "external"}]})
    elif problem in ("crypto", "unknown_asset"):
        currency = "BTC" if problem == "crypto" else "OTHER"
        funded = funded.model_copy(
            update={
                "balances": funded.balances
                | {
                    currency: Balance(available=D("1"), hold=D("0")),
                }
            }
        )
    elif problem == "fill":
        funded = funded.model_copy(
            update={
                "recent_fills": [
                    {
                        "entry_id": "external-fill",
                        "order_id": "external",
                        "trade_time": NOW.isoformat(),
                    }
                ]
            }
        )
    elif problem == "mapping":
        funded = funded.model_copy(update={"portfolio_id": "wrong"})
    elif problem == "stale":
        funded = funded.model_copy(update={"observed_at": NOW - timedelta(minutes=20)})
    elif problem == "gap":
        store.exchange_seen("main", NOW - timedelta(days=7))
    elif problem == "ledger_cash":
        ledger = store.ledger("live", "main").model_copy(update={"cash": D("999")})
        store.save_ledger("live", "main", ledger)
    elif problem == "ledger_position":
        ledger = store.ledger("live", "main").model_copy(update={"positions": {}})
        store.save_ledger("live", "main", ledger)
    elif problem == "missing_ledger":
        with store.db:
            store.db.execute("DELETE FROM strategy_state WHERE mode='live'")
    elif problem == "missing_equity_baseline":
        with store.db:
            store.db.execute("DELETE FROM daily_marks")
    elif problem == "permission":
        sdk.get_api_key_permissions.return_value["can_trade"] = False
    elif problem == "mode_change":
        monkeypatch.setattr("trader.funding.active_mode", Mock(side_effect=["live", "paper"]))
    monkeypatch.setattr(adapter, "account", lambda _: funded)
    before, ledger = store.exchange("main"), store.ledger("live", "main")
    with pytest.raises(SafetyError):
        record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    assert store.exchange("main") == before
    assert store.ledger("live", "main") == ledger
    assert not store.rows("cash_deposits")


def test_deposit_failure_rolls_back_cash_checkpoint_and_audit(cfg, store, adapter, funded):
    before = store.exchange("main")
    count = len(store.rows("reconciliation_events"))
    store.db.execute("""CREATE TRIGGER fail_deposit_audit BEFORE INSERT ON reconciliation_events
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    assert store.exchange("main") == before
    assert store.ledger("live", "main").cash == D("1000")
    assert not store.rows("cash_deposits")
    assert len(store.rows("reconciliation_events")) == count


@pytest.mark.parametrize("observe_after_deposit", [False, True])
def test_deposit_preserves_daily_loss_and_next_day_baseline(
    cfg, store, adapter, funded, observe_after_deposit
):
    assert store.mark_equity("live", "main", D("960"), NOW) == D("0.04")
    record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    if observe_after_deposit:
        assert store.mark_equity("live", "main", D("1060"), NOW) == D("0.04")
    assert store.mark_equity("live", "main", D("1060"), NOW + timedelta(days=1)) == 0
    assert store.mark_equity("live", "main", D("1007"), NOW + timedelta(days=1)) == D("0.05")
    assert store.mark_equity("paper", "main", D("1000"), NOW) == 0
    assert store.mark_equity("paper", "main", D("960"), NOW) == D("0.04")


def test_deposit_before_first_daily_observation_does_not_hide_overnight_loss(
    cfg, store, adapter, funded
):
    with store.db:
        store.db.execute("DELETE FROM daily_marks")
    store.mark_equity("live", "main", D("1000"), NOW - timedelta(days=1))
    record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    assert store.mark_equity("live", "main", D("1060"), NOW) == D("0.04")


def test_multiple_deposits_and_restart_preserve_daily_loss(
    cfg, store, adapter, sdk, funded, tmp_path
):
    store.mark_equity("live", "main", D("960"), NOW)
    record_deposit(cfg, store, {"main": adapter}, "main", "100", "deposit-1")
    sdk.account_state["value"] = funded.model_copy(
        update={
            "balances": funded.balances
            | {
                "USDC": Balance(available=D("1300"), hold=D("0")),
            }
        }
    )
    record_deposit(cfg, store, {"main": adapter}, "main", "200", "deposit-2")
    reopened = Storage(tmp_path / "trader.sqlite3")
    try:
        assert reopened.mark_equity("live", "main", D("1260"), NOW) == D("0.04")
        assert reopened.mark_equity("live", "main", D("1197"), NOW + timedelta(days=1)) == D("0.05")
    finally:
        reopened.close()


def test_first_funding_from_zero_has_a_daily_loss_limit(
    cfg, store, adapter, sdk, actual, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    empty = actual.model_copy(
        update={
            "balances": actual.balances
            | {
                "USDC": Balance(available=D("0"), hold=D("0")),
            }
        }
    )
    reconcile_actual(store, empty, cfg, "initial", "live")
    store.save_ledger("live", "main", seed_ledger(empty, cfg, paper=False))
    assert store.mark_equity("live", "main", D("0"), NOW) == 0
    record_deposit(cfg, store, {"main": adapter}, "main", "1000", "initial-deposit")
    assert store.mark_equity("live", "main", D("1000"), NOW) == 0
    assert store.mark_equity("live", "main", D("960"), NOW) == D("0.04")
    sdk.account_state["value"] = actual.model_copy(
        update={
            "balances": actual.balances
            | {
                "USDC": Balance(available=D("2000"), hold=D("0")),
            }
        }
    )
    record_deposit(cfg, store, {"main": adapter}, "main", "1000", "second-deposit")
    assert store.mark_equity("live", "main", D("1960"), NOW) == D("0.04")


def test_tiny_deposit_cannot_be_credited_twice_with_different_references(
    cfg, store, adapter, sdk, funded
):
    sdk.account_state["value"] = funded.model_copy(
        update={
            "balances": funded.balances
            | {
                "USDC": Balance(available=D("1000.001"), hold=D("0")),
            }
        }
    )
    record_deposit(cfg, store, {"main": adapter}, "main", "0.001", "deposit-1")
    with pytest.raises(SafetyError, match="DEPOSIT_AMOUNT_MISMATCH"):
        record_deposit(cfg, store, {"main": adapter}, "main", "0.001", "deposit-2")
    assert store.ledger("live", "main").cash == D("1000.001")


def test_deposit_cli_uses_lock_and_no_model(
    cfg, adapter, sdk, actual, monkeypatch, tmp_path, capsys
):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING_MODE", "live")
    path = tmp_path / "state.sqlite3"
    storage = Storage(path)
    reconcile_actual(storage, actual, cfg, "initialize", "live")
    storage.save_ledger("live", "main", seed_ledger(actual, cfg, paper=False))
    storage.mark_equity("live", "main", D("1000"), NOW)
    storage.close()
    sdk.account_state["value"] = actual.model_copy(
        update={
            "balances": actual.balances
            | {
                "USDC": Balance(available=D("1100"), hold=D("0")),
            }
        }
    )
    monkeypatch.setattr("trader.coinbase_client.build_adapters", lambda *_: {"main": adapter})
    model = Mock(side_effect=AssertionError("No OpenAI for deposits"))
    monkeypatch.setattr("openai.OpenAI", model)
    args = ["record-deposit", "--portfolio", "main", "--amount", "100", "--reference", "deposit-1"]
    with process_lock(path.with_suffix(".lock")):
        assert main(args) == 1
    assert "CYCLE_ALREADY_RUNNING" in capsys.readouterr().out
    assert main(args) == 0
    assert main(args) == 0
    assert "ALREADY_RECORDED" in capsys.readouterr().out
    assert main(["show-deposits"]) == 0
    assert '"amount":"100"' in capsys.readouterr().out
    model.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()
    sdk.cancel_orders.assert_not_called()


def test_deposit_cli_rejects_paper_before_network(cfg, monkeypatch, tmp_path, capsys):
    write_config(cfg, monkeypatch, tmp_path)
    build = Mock(side_effect=AssertionError("No Coinbase for paper funding"))
    monkeypatch.setattr("trader.coinbase_client.build_adapters", build)
    assert (
        main(["record-deposit", "--portfolio", "main", "--amount", "100", "--reference", "a"]) == 1
    )
    assert "DEPOSIT_REQUIRES_LIVE_MODE" in capsys.readouterr().out
    build.assert_not_called()


def test_version_three_upgrade_preserves_balances_and_loss_marks(store, actual, cfg, tmp_path):
    reconcile_actual(store, actual, cfg, "initial", "paper")
    store.mark_equity("live", "main", D("1000"), NOW)
    store.mark_equity("live", "main", D("960"), NOW)
    with store.db:
        store.db.execute("ALTER TABLE daily_marks DROP COLUMN funding_total")
        store.db.execute("DROP TABLE cash_deposits")
        store.db.execute("PRAGMA user_version=3")
    upgraded = Storage(tmp_path / "trader.sqlite3")
    try:
        assert upgraded.exchange("main") == store.exchange("main")
        assert upgraded.mark_equity("live", "main", D("960"), NOW) == D("0.04")
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        upgraded.close()
