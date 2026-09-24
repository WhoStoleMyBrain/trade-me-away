from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests
from conftest import NOW
from test_config_cli import write_config
from test_execution import exchange_fill, initialize

from trader.__main__ import main
from trader.errors import SafetyError
from trader.execution import CoinbaseLiveExecutor, recover_orders
from trader.schemas import Balance, ExecutionResult, StrategyPosition
from trader.storage import Storage, process_lock
from trader.util import D


def pending(cfg, store, adapter, sdk, actual, ledger, intent, *, side="BUY", size="0"):
    intent = intent.model_copy(
        update={
            "mode": "live",
            "side": side,
            "created_at": NOW - timedelta(seconds=cfg.execution.max_order_age_seconds + 1),
            "limit_price": D("100.10") if side == "BUY" else D("99.90"),
        }
    )
    if side == "SELL":
        actual = actual.model_copy(
            update={
                "balances": actual.balances | {"BTC": Balance(available=D("2"), hold=D("0"))},
            }
        )
        ledger = ledger.model_copy(
            update={
                "positions": ledger.positions
                | {
                    "BTC-USDC": StrategyPosition(quantity=D("2"), cost_basis=D("160")),
                },
            }
        )
    sdk.account_state["value"] = actual
    initialize(store, cfg, actual, ledger, intent)
    order, fill = exchange_fill(
        sdk,
        intent,
        size=size,
        price="100",
        fee=str(D(size) * D("0.6")),
        status="OPEN",
        settled=False,
    )
    sdk.get_order.side_effect = lambda **_: {"order": dict(order)}
    sdk.list_orders.side_effect = lambda **kw: {
        "orders": [order] if "start_date" in kw or order["status"] == "OPEN" else [],
        "has_next": False,
        "cursor": "",
    }
    store.save_intent(intent)
    store.intent_status(intent.client_order_id, "SUBMITTING")
    store.record_result(
        intent,
        ExecutionResult(
            order_id=order["order_id"],
            status="UNRESOLVED",
            terminal=False,
            fills=[],
        ),
    )
    return intent, order, fill, CoinbaseLiveExecutor(cfg, store, {"main": adapter})


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("size", ["0", "0.4"])
def test_overdue_orders_cancel_and_verify_remaining_fills(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    side,
    size,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, executor = pending(
        cfg,
        store,
        adapter,
        sdk,
        actual,
        ledger,
        intent,
        side=side,
        size=size,
    )

    def cancel(**kwargs):
        assert store.cancellations(intent.client_order_id)[0]["status"] == "REQUESTED"
        assert kwargs == {"order_ids": [order["order_id"]]}
        order.update(status="CANCELLED", settled=D(size) > 0)
        return {"results": [{"order_id": order["order_id"], "success": True}]}

    sdk.cancel_orders.side_effect = cancel
    result = executor.recover(intent, allow_cancel=True)
    assert result.terminal and result.status == ("PARTIAL" if D(size) else "CANCELLED")
    assert not store.unresolved()
    position = store.ledger("live", "main").positions["BTC-USDC"]
    assert position.quantity == (D(size) if side == "BUY" else 2 - D(size))
    assert len(store.rows("fills")) == (1 if D(size) else 0)
    sdk.cancel_orders.assert_called_once()
    sdk.limit_order_ioc.assert_not_called()
    sdk.market_order_buy.assert_not_called()
    sdk.market_order_sell.assert_not_called()


def test_full_fill_racing_cancellation_is_accounted(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent, size="1")

    def cancel(**kwargs):
        order.update(status="FILLED", settled=True)
        return {"results": [{"order_id": order["order_id"], "success": False}]}

    sdk.cancel_orders.side_effect = cancel
    assert executor.recover(intent, allow_cancel=True).status == "FILLED"
    assert store.ledger("live", "main").positions["BTC-USDC"].quantity == 1
    assert store.cancellations(intent.client_order_id)[0]["status"] == "REJECTED"


@pytest.mark.parametrize("reply", ["ack", "timeout", "malformed"])
def test_cancel_ack_timeout_or_bad_response_never_means_released(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    reply,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    if reply == "timeout":
        sdk.cancel_orders.side_effect = requests.Timeout("SECRET_SENTINEL")
    else:
        sdk.cancel_orders.return_value = {
            "results": [
                {
                    "order_id": order["order_id"] if reply == "ack" else "someone-else",
                    "success": True,
                }
            ]
        }
    assert not executor.recover(intent, allow_cancel=True).terminal
    assert not executor.recover(intent, allow_cancel=True).terminal
    sdk.cancel_orders.assert_called_once()  # persisted cooldown, not a blind POST retry
    assert store.unresolved()
    assert store.ledger("live", "main") == ledger
    assert "SECRET_SENTINEL" not in str(store.rows("cancellation_attempts"))


def test_cancel_retry_requires_new_state_and_is_bounded(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    sdk.cancel_orders.side_effect = requests.Timeout()
    for n in range(cfg.execution.max_cancel_attempts + 1):
        monkeypatch.setattr("trader.execution.utcnow", lambda n=n: NOW + timedelta(minutes=n * 2))
        monkeypatch.setattr("trader.storage.utcnow", lambda n=n: NOW + timedelta(minutes=n * 2))
        assert not executor.recover(intent, allow_cancel=True).terminal
    assert sdk.cancel_orders.call_count == cfg.execution.max_cancel_attempts
    assert store.result(intent.client_order_id).reason == "CANCEL_ATTEMPT_LIMIT"
    assert sdk.get_order.call_count >= sdk.cancel_orders.call_count
    order.update(status="CANCELLED", settled=False)
    assert executor.recover(intent, allow_cancel=True).terminal  # still verify after cap


@pytest.mark.parametrize(
    "problem",
    [
        "young",
        "read_only",
        "paper",
        "missing_mode",
        "mode_change",
        "identity",
        "portfolio",
        "pending_cancel",
    ],
)
def test_cancellation_safety_gates(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    problem,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    if problem == "young":
        cfg.execution.max_order_age_seconds = 86400
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    if problem == "young":
        monkeypatch.setattr(
            "trader.execution.utcnow", lambda: intent.created_at + timedelta(seconds=1)
        )
    elif problem == "paper":
        monkeypatch.setenv("TRADING_MODE", "paper")
    elif problem == "missing_mode":
        monkeypatch.delenv("TRADING_MODE")
    elif problem == "mode_change":
        original = store.begin_cancellation

        def begin(*args):
            result = original(*args)
            monkeypatch.setenv("TRADING_MODE", "paper")
            return result

        monkeypatch.setattr(store, "begin_cancellation", begin)
    elif problem == "identity":
        order["client_order_id"] = "unowned-order"
    elif problem == "portfolio":
        adapter.portfolio_id = "different-portfolio"
        order["retail_portfolio_id"] = adapter.portfolio_id
    elif problem == "pending_cancel":
        order["pending_cancel"] = True
    result = executor.recover(intent, allow_cancel=problem != "read_only")
    assert not result.terminal
    sdk.cancel_orders.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()


def test_cancelled_order_retains_block_until_holds_release(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
):
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    order.update(status="CANCELLED", settled=False)
    sdk.account_state["value"] = actual.model_copy(
        update={
            "balances": actual.balances
            | {
                "USDC": Balance(available=D("900"), hold=D("100")),
            }
        }
    )
    assert not executor.recover(intent).terminal
    assert store.result(intent.client_order_id).reason == "RECONCILIATION_FAILED"
    sdk.account_state["value"] = actual
    assert executor.recover(intent).terminal
    sdk.cancel_orders.assert_not_called()


def test_missing_dust_fill_cannot_hide_in_balance_tolerance(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
):
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    order.update(
        status="CANCELLED",
        settled=True,
        filled_size="0.000000001",
        filled_value="0.0000001",
        number_of_fills="1",
    )
    assert not executor.recover(intent).terminal
    assert not store.rows("fills")


def test_missing_previously_accounted_fill_stays_unresolved(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
):
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent, size="1")
    order.update(status="FILLED", settled=True)
    correct = sdk.account_state["value"]
    sdk.account_state["value"] = actual  # accounting committed, balances lag
    assert not executor.recover(intent).terminal
    before = store.ledger("live", "main")
    sdk.account_state["value"] = correct
    order.update(
        status="CANCELLED", filled_size="0", filled_value="0", total_fees="0", number_of_fills="0"
    )
    sdk.get_fills.return_value = {"fills": [], "cursor": ""}
    assert executor.recover(intent).reason == "FILL_HISTORY_MISSING"
    assert store.ledger("live", "main") == before


def test_maintenance_ignores_model_connectivity_and_budget(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, _ = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    order.update(status="CANCELLED", settled=False)
    store.reserve_request("spent", "cycle", cfg.llm, cfg.llm.daily_budget_usd, NOW)
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("TRADER_DB", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setattr("trader.__main__.build_adapters", lambda *_: {"main": adapter})
    model = Mock(side_effect=AssertionError("must not instantiate OpenAI"))
    monkeypatch.setattr("trader.__main__.OpenAI", model)
    assert main(["maintain-orders"]) == 0
    model.assert_not_called()
    sdk.get_product.assert_not_called()
    assert not store.unresolved()


def test_maintenance_skips_busy_database(cfg, monkeypatch, tmp_path):
    write_config(cfg, monkeypatch, tmp_path)
    with process_lock(tmp_path / "state.lock"):
        assert main(["maintain-orders"]) == 0


def test_recovery_checks_all_intents_even_when_one_is_unresolved(
    cfg,
    store,
    adapter,
    intent,
    monkeypatch,
):
    intents = [intent, intent.model_copy(update={"client_order_id": "second"})]
    monkeypatch.setattr(store, "unresolved", lambda: intents)
    executor = Mock()
    executor.recover.side_effect = [
        SafetyError("UNAVAILABLE"),
        ExecutionResult(
            order_id=None,
            status="ABORTED",
            terminal=True,
            fills=[],
        ),
    ]
    monkeypatch.setattr("trader.execution.make_executor", lambda *_: executor)
    with pytest.raises(SafetyError, match="UNRESOLVED_PREVIOUS_ORDER"):
        recover_orders(cfg, store, {"main": adapter})
    assert executor.recover.call_count == 2


def test_schema_upgrade_preserves_order_history(cfg, store, intent, tmp_path):
    store.begin_cycle(intent.cycle_id, "slot", "paper", cfg, NOW)
    store.save_intent(intent)
    store.db.execute("PRAGMA user_version=1")
    store.db.commit()
    with_db = Storage(tmp_path / "trader.sqlite3")
    try:
        assert with_db.saved_intent(intent.client_order_id)[0] == intent
        assert with_db.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert with_db.cancellations(intent.client_order_id) == []
    finally:
        with_db.close()


def test_unknown_submission_is_discovered_then_cancelled_without_replacement(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    store.record_result(
        intent,
        ExecutionResult(
            order_id=None,
            status="UNRESOLVED",
            terminal=False,
            fills=[],
        ),
    )

    def cancel(**kwargs):
        order.update(status="CANCELLED", settled=False)
        raise requests.Timeout()  # Cancellation reached Coinbase but its response was lost.

    sdk.cancel_orders.side_effect = cancel
    assert executor.recover(intent, allow_cancel=True).terminal
    sdk.cancel_orders.assert_called_once()
    sdk.limit_order_ioc.assert_not_called()
    assert sdk.get_order.call_count >= 2


def test_ambiguous_submission_absence_never_creates_cancel_or_replacement(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, _, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    store.record_result(
        intent,
        ExecutionResult(
            order_id=None,
            status="UNRESOLVED",
            terminal=False,
            fills=[],
        ),
    )
    sdk.list_orders.side_effect = None
    sdk.list_orders.return_value = {"orders": [], "has_next": False, "cursor": ""}
    assert executor.recover(intent, allow_cancel=True).reason == "SUBMISSION_NOT_FOUND_YET"
    sdk.cancel_orders.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()


def test_doctor_remains_read_only_for_overdue_live_orders(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    pending(cfg, store, adapter, sdk, actual, ledger, intent)
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("TRADER_DB", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setattr("trader.__main__.build_adapters", lambda *_: {"main": adapter})
    monkeypatch.setattr("trader.__main__.OpenAI", lambda **_: Mock())
    assert main(["doctor"]) == 1
    sdk.cancel_orders.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()


def test_crash_before_submission_can_abort_without_exchange_requests(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
):
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    store.save_intent(intent)
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).recover(intent)
    assert result.terminal and result.status == "ABORTED"
    assert not store.unresolved()
    sdk.get_order.assert_not_called()
    sdk.list_orders.assert_not_called()
    sdk.cancel_orders.assert_not_called()
    sdk.limit_order_ioc.assert_not_called()


def test_prepared_flag_cannot_override_an_exchange_acknowledgment(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
):
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    store.intent_status(intent.client_order_id, "PREPARED")
    result = executor.recover(intent)
    assert not result.terminal and result.reason == "ORDER_STATE_CONFLICT"
    assert result.order_id == order["order_id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("filled_size", "-0.000000001"),
        ("filled_value", "-0.001"),
        ("total_fees", "-0.001"),
        ("total_fees", "0.001"),
        ("number_of_fills", "0.5"),
    ],
)
def test_zero_fill_order_requires_exact_consistent_totals(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    field,
    value,
):
    intent, order, _, executor = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    order.update(status="CANCELLED", settled=True)
    order[field] = value
    assert not executor.recover(intent).terminal


def test_cancel_inflight_at_process_crash_preserves_retry_cooldown(
    cfg,
    store,
    adapter,
    sdk,
    actual,
    ledger,
    intent,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent, order, _, _ = pending(cfg, store, adapter, sdk, actual, ledger, intent)
    store.begin_cancellation(intent, order["order_id"])
    restarted = Storage(tmp_path / "trader.sqlite3")
    try:
        result = CoinbaseLiveExecutor(cfg, restarted, {"main": adapter}).recover(
            intent,
            allow_cancel=True,
        )
        assert not result.terminal and result.reason == "CANCEL_RETRY_COOLDOWN"
        sdk.cancel_orders.assert_not_called()
        assert restarted.cancellations(intent.client_order_id)[0]["status"] == "REQUESTED"
    finally:
        restarted.close()
