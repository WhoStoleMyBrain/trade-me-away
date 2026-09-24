from datetime import timedelta

import pytest
from conftest import NOW

from trader.errors import SafetyError
from trader.portfolio import apply_fills, reconcile_actual, seed_ledger
from trader.schemas import Balance, Fill
from trader.storage import process_lock
from trader.util import D


def test_reconciliation_mismatch_is_not_rebaselined(cfg, store, actual):
    reconcile_actual(store, actual, cfg, "c", "paper")
    balances = actual.balances | {"BTC": Balance(available=D("0.1"), hold=D("0"))}
    changed = actual.model_copy(update={"balances": balances})
    with pytest.raises(SafetyError, match="RECONCILIATION_FAILED"):
        reconcile_actual(store, changed, cfg, "c", "paper")
    assert store.exchange("main")["balances"]["BTC"] == "0"
    assert "BALANCE_MISMATCH:BTC" in store.rows("reconciliation_events")[0]["payload_json"]


def test_untracked_round_trip_and_unknown_assets_fail(cfg, store, actual):
    reconcile_actual(store, actual, cfg, "c", "paper")
    changed = actual.model_copy(
        update={
            "recent_fills": [
                {"entry_id": "outside-fill", "order_id": "outside", "trade_time": NOW.isoformat()}
            ]
        }
    )
    with pytest.raises(SafetyError):
        reconcile_actual(store, changed, cfg, "c", "paper")
    changed = actual.model_copy(
        update={"balances": actual.balances | {"UNKNOWN": Balance(available=D("1"), hold=D("0"))}}
    )
    with pytest.raises(SafetyError):
        reconcile_actual(store, changed, cfg, "c", "paper")


def test_preexisting_cost_basis_is_unknown(cfg, actual):
    actual = actual.model_copy(
        update={"balances": actual.balances | {"BTC": Balance(available=D("1"), hold=D("0"))}}
    )
    ledger = seed_ledger(actual, cfg, paper=False)
    assert ledger.positions["BTC-USDC"].cost_basis is None
    sell = Fill(
        fill_id="sell",
        order_id="sell",
        product_id="BTC-USDC",
        side="SELL",
        base_size=D("0.5"),
        price=D("100"),
        fee=D("0.3"),
        trade_time=NOW,
    )
    updated = apply_fills(ledger, [sell])
    assert updated.positions["BTC-USDC"].realized_pnl is None


def test_daily_loss_includes_overnight_change_and_restart(store):
    assert store.mark_equity("paper", "main", D("1000"), NOW - timedelta(days=1)) == 0
    assert store.mark_equity("paper", "main", D("900"), NOW) == D("0.1")
    assert store.mark_equity("paper", "main", D("800"), NOW) == D("0.2")


def test_duplicate_cycles_blocked_across_modes(store, cfg):
    store.begin_cycle("c1", "same-slot", "paper", cfg, NOW)
    with pytest.raises(SafetyError, match="DUPLICATE_CYCLE"):
        store.begin_cycle("c2", "same-slot", "live", cfg, NOW)


def test_nonoverlapping_process_lock(tmp_path):
    with (
        process_lock(tmp_path / "cycle.lock"),
        pytest.raises(SafetyError, match="CYCLE_ALREADY_RUNNING"),
        process_lock(tmp_path / "cycle.lock"),
    ):
        pass
    with process_lock(tmp_path / "cycle.lock"):
        pass


def test_no_negative_cash_or_short_sale(ledger):
    for side in ("BUY", "SELL"):
        fill = Fill(
            fill_id="invalid",
            order_id="invalid",
            product_id="BTC-USDC",
            side=side,
            base_size=D("100"),
            price=D("100"),
            fee=D("0"),
            trade_time=NOW,
        )
        with pytest.raises(SafetyError):
            apply_fills(ledger, [fill])


def test_history_gap_cannot_silently_skip_external_fills(cfg, store, actual):
    reconcile_actual(store, actual, cfg, "c", "paper")
    old = actual.model_copy(update={"observed_at": NOW + timedelta(days=7)})
    with pytest.raises(SafetyError, match="RECONCILIATION_FAILED"):
        reconcile_actual(store, old, cfg, "c", "paper")
    assert "RECONCILIATION_HISTORY_GAP" in store.rows("reconciliation_events")[0]["payload_json"]
