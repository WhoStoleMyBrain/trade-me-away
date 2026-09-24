import pytest
import requests
from conftest import NOW

from trader.errors import SafetyError
from trader.execution import CoinbaseLiveExecutor, PaperExecutor, make_executor, recover_orders
from trader.portfolio import reconcile_actual
from trader.schemas import Balance
from trader.util import D


def initialize(store, cfg, actual, ledger, intent):
    store.begin_cycle(intent.cycle_id, "test-slot", intent.mode, cfg, NOW)
    reconcile_actual(store, actual, cfg, intent.cycle_id, intent.mode)
    store.save_ledger(intent.mode, "main", ledger)


def exchange_fill(
    sdk, intent, *, size="1", price="100.05", fee="0.6003", status="FILLED", settled=True
):
    fill = {
        "entry_id": "fill-1",
        "order_id": "exchange-order",
        "product_id": intent.product_id,
        "side": intent.side,
        "price": price,
        "size": size,
        "commission": fee,
        "trade_time": NOW.isoformat(),
        "size_in_quote": False,
        "retail_portfolio_id": "test-portfolio",
    }
    order = {
        "order_id": "exchange-order",
        "client_order_id": intent.client_order_id,
        "product_id": intent.product_id,
        "side": intent.side,
        "retail_portfolio_id": "test-portfolio",
        "product_type": "SPOT",
        "status": status,
        "settled": settled,
        "filled_size": size,
        "filled_value": str(D(size) * D(price)),
        "total_fees": fee,
        "number_of_fills": "1" if D(size) else "0",
        "pending_cancel": False,
    }
    sdk.limit_order_ioc.return_value = {
        "success": True,
        "success_response": {
            "order_id": "exchange-order",
            "client_order_id": intent.client_order_id,
            "product_id": intent.product_id,
            "side": intent.side,
        },
    }
    sdk.get_order.return_value = {"order": order}
    sdk.get_fills.return_value = {"fills": [fill] if D(size) else [], "cursor": ""}
    old = sdk.account_state["value"]
    balances = dict(old.balances)
    base = intent.product_id.split("-")[0]
    sign = D("1") if intent.side == "BUY" else D("-1")
    balances[base] = Balance(available=old.balances[base].total + sign * D(size), hold=D("0"))
    balances["USDC"] = Balance(
        available=old.balances["USDC"].total - sign * D(size) * D(price) - D(fee), hold=D("0")
    )
    sdk.account_state["value"] = old.model_copy(update={"balances": balances})
    return order, fill


def test_paper_fills_commission_and_balances(cfg, store, adapter, sdk, actual, ledger, intent):
    initialize(store, cfg, actual, ledger, intent)
    executor = PaperExecutor(cfg, store, {"main": adapter})
    result = executor.execute(intent, lambda x: x)
    assert result.status == "FILLED"
    assert result.fills[0].fee > 0
    updated = store.ledger("paper", "main")
    assert updated.positions["BTC-USDC"].quantity == 1
    assert updated.cash == D("1000") - result.fills[0].price - result.fills[0].fee
    assert store.exchange("main")["balances"]["USDC"] == "1000"
    sdk.limit_order_ioc.assert_not_called()
    sdk.market_order_buy.assert_not_called()
    assert not store.unresolved()


def test_paper_partial_and_limit_no_fill(cfg, store, adapter, actual, ledger, intent):
    initialize(store, cfg, actual, ledger, intent)
    cfg.execution.paper_fill_fraction = D("0.4")
    result = PaperExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "PARTIAL" and result.terminal
    assert result.fills[0].base_size == D("0.4")


def test_paper_nonmarketable_ioc_cancels(cfg, store, adapter, actual, ledger, intent):
    intent = intent.model_copy(update={"limit_price": D("99")})
    initialize(store, cfg, actual, ledger, intent)
    result = PaperExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "CANCELLED" and not result.fills
    assert store.ledger("paper", "main") == ledger


def test_paper_sell_realizes_pnl(cfg, store, adapter, actual, ledger, intent):
    from trader.portfolio import apply_fills
    from trader.schemas import Fill

    buy = Fill(
        fill_id="buy",
        order_id="buy",
        product_id="BTC-USDC",
        side="BUY",
        base_size=D("2"),
        price=D("90"),
        fee=D("1"),
        trade_time=NOW,
    )
    ledger = apply_fills(ledger, [buy])
    intent = intent.model_copy(
        update={"side": "SELL", "limit_price": D("99"), "quote_size": D("99")}
    )
    initialize(store, cfg, actual, ledger, intent)
    result = PaperExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    state = store.ledger("paper", "main").positions["BTC-USDC"]
    assert state.quantity == 1
    assert state.cost_basis == D("90.5")
    assert state.realized_pnl == result.fills[0].price - result.fills[0].fee - D("90.5")


def test_live_persists_before_submit_and_verifies(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    exchange_fill(sdk, intent)
    response = sdk.limit_order_ioc.return_value

    def submit(**kwargs):
        assert store.rows("order_intents")[0]["status"] == "SUBMITTING"
        assert kwargs["client_order_id"] == intent.client_order_id
        return response

    sdk.limit_order_ioc.side_effect = submit
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "FILLED" and result.terminal
    assert store.ledger("live", "main").cash == D("899.3497")
    assert len(store.rows("fills")) == 1
    assert not store.unresolved()
    sdk.limit_order_ioc.assert_called_once()
    sdk.get_order.assert_called_once_with(order_id="exchange-order")


@pytest.mark.parametrize("mode", ["paper", "", "LIVE"])
def test_live_guard_fails_closed(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch, mode
):
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    monkeypatch.setenv("TRADING_MODE", mode)
    with pytest.raises(SafetyError):
        CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    sdk.limit_order_ioc.assert_not_called()


def test_mode_change_during_guard_aborts(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)

    def guard(i):
        monkeypatch.setenv("TRADING_MODE", "paper")
        return i

    with pytest.raises(SafetyError):
        CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, guard)
    sdk.limit_order_ioc.assert_not_called()


def test_duplicate_intent_never_resubmits(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    exchange_fill(sdk, intent)
    executor = CoinbaseLiveExecutor(cfg, store, {"main": adapter})
    executor.execute(intent, lambda x: x)
    with pytest.raises(SafetyError, match="DUPLICATE_ORDER"):
        executor.execute(intent, lambda x: x)
    sdk.limit_order_ioc.assert_called_once()


def test_timeout_discovery_finds_original_without_resubmit(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    order, _ = exchange_fill(sdk, intent)
    # Discovery returns the original; account open-order listing must remain empty.
    sdk.list_orders.side_effect = lambda **kw: {
        "orders": [order] if "start_date" in kw else [],
        "has_next": False,
        "cursor": "",
    }
    sdk.limit_order_ioc.side_effect = requests.Timeout("DO_NOT_LOG_SECRET")
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "FILLED"
    sdk.limit_order_ioc.assert_called_once()


def test_uncertain_timeout_blocks_future_trades(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    sdk.limit_order_ioc.side_effect = requests.Timeout("DO_NOT_LOG_SECRET")
    executor = CoinbaseLiveExecutor(cfg, store, {"main": adapter})
    result = executor.execute(intent, lambda x: x)
    assert result.status == "UNRESOLVED"
    assert store.unresolved()
    with pytest.raises(SafetyError, match="UNRESOLVED"):
        executor.execute(intent.model_copy(update={"client_order_id": "another"}), lambda x: x)
    with pytest.raises(SafetyError, match="UNRESOLVED"):
        recover_orders(cfg, store, {"main": adapter})
    sdk.limit_order_ioc.assert_called_once()


def test_partial_cancelled_live_order_is_recorded(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    exchange_fill(sdk, intent, size="0.4", fee="0.24012", status="CANCELLED")
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "PARTIAL" and result.terminal
    assert store.ledger("live", "main").positions["BTC-USDC"].quantity == D("0.4")


def test_rejected_order_never_assumes_fill(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    sdk.limit_order_ioc.return_value = {
        "success": False,
        "error_response": {"error": "INSUFFICIENT_FUND"},
    }
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "REJECTED" and result.terminal and not result.fills
    assert store.ledger("live", "main") == ledger


@pytest.mark.parametrize("problem", ["missing_fills", "fees", "identity", "unsettled", "balance"])
def test_acknowledgment_is_not_execution(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch, problem
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    order, _ = exchange_fill(sdk, intent)
    if problem == "missing_fills":
        sdk.get_fills.return_value = {"fills": [], "cursor": ""}
    elif problem == "fees":
        order["total_fees"] = "5"
    elif problem == "identity":
        order["client_order_id"] = "another-id"
    elif problem == "unsettled":
        order["settled"] = False
    else:
        sdk.account_state["value"] = actual
    result = CoinbaseLiveExecutor(cfg, store, {"main": adapter}).execute(intent, lambda x: x)
    assert result.status == "UNRESOLVED" and not result.terminal
    assert store.unresolved()
    sdk.limit_order_ioc.assert_called_once()


def test_recovery_applies_confirmed_fills_exactly_once(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    exchange_fill(sdk, intent)
    correct_state = sdk.account_state["value"]
    sdk.account_state["value"] = actual  # balances lag the confirmed fill
    executor = CoinbaseLiveExecutor(cfg, store, {"main": adapter})
    assert not executor.execute(intent, lambda x: x).terminal
    assert len(store.rows("fills")) == 1
    before = store.ledger("live", "main")
    sdk.account_state["value"] = correct_state
    monkeypatch.setenv(
        "TRADING_MODE", "paper"
    )  # recovery performs GETs only, including across modes
    recover_orders(cfg, store, {"main": adapter})
    assert store.ledger("live", "main") == before
    assert len(store.rows("fills")) == 1
    assert not store.unresolved()
    sdk.limit_order_ioc.assert_called_once()


def test_executor_selection_is_explicit(cfg, store, adapter):
    assert isinstance(make_executor("paper", cfg, store, {"main": adapter}), PaperExecutor)
    assert isinstance(make_executor("live", cfg, store, {"main": adapter}), CoinbaseLiveExecutor)
    with pytest.raises(SafetyError):
        make_executor("unknown", cfg, store, {"main": adapter})


def test_new_zero_balance_asset_extends_paper_ledger(cfg, store, adapter, actual, ledger):
    from trader.config import AssetConfig

    store.save_ledger("paper", "main", ledger)
    cfg.assets.append(AssetConfig(product_id="NEW-USDC", portfolio="main"))
    actual = actual.model_copy(
        update={"balances": actual.balances | {"NEW": Balance(available=D("0"), hold=D("0"))}}
    )
    updated = PaperExecutor(cfg, store, {"main": adapter}).load_ledger(actual)
    assert updated.cash == ledger.cash
    assert updated.positions["NEW-USDC"].quantity == 0


def test_fill_history_cannot_change_during_recovery(
    cfg, store, adapter, sdk, actual, ledger, intent, monkeypatch
):
    monkeypatch.setenv("TRADING_MODE", "live")
    intent = intent.model_copy(update={"mode": "live"})
    initialize(store, cfg, actual, ledger, intent)
    order, fill = exchange_fill(sdk, intent)
    sdk.account_state["value"] = actual
    executor = CoinbaseLiveExecutor(cfg, store, {"main": adapter})
    assert not executor.execute(intent, lambda x: x).terminal
    before = store.ledger("live", "main")
    fill["commission"] = "0.5"
    order["total_fees"] = "0.5"
    assert not executor.recover(intent).terminal
    assert store.ledger("live", "main") == before
