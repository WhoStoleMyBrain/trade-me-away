from __future__ import annotations

from datetime import datetime

from trader.config import AppConfig
from trader.errors import SafetyError
from trader.schemas import (
    AccountState,
    Fill,
    Ledger,
    PortfolioState,
    Quote,
    StrategyPosition,
)
from trader.storage import Storage
from trader.util import ZERO, D, timestamp


def account_totals(actual: AccountState) -> dict[str, str]:
    return {currency: str(balance.total) for currency, balance in actual.balances.items()}


def reconcile_actual(
    store: Storage, actual: AccountState, cfg: AppConfig, cycle: str, mode: str
) -> None:
    expected = store.exchange(actual.portfolio)
    allowed = {"USDC"} | {
        a.product_id.split("-")[0] for a in cfg.enabled_assets if a.portfolio == actual.portfolio
    }
    reasons = []
    totals = account_totals(actual)
    if any(c not in allowed and D(v) != 0 for c, v in totals.items()):
        reasons.append("UNVALUED_ASSET")
    if actual.open_orders or any(b.hold > 0 for b in actual.balances.values()):
        reasons.append("OUTSTANDING_ORDER_OR_HOLD")
    if expected:
        if (
            actual.observed_at - timestamp(expected["last_reconciled_at"])
        ).total_seconds() > 6 * 86400:
            reasons.append("RECONCILIATION_HISTORY_GAP")
        if expected["portfolio_id"] != actual.portfolio_id:
            reasons.append("PORTFOLIO_MAPPING_CHANGED")
        for currency in set(totals) | set(expected["balances"]):
            tolerance = (
                cfg.risk.balance_tolerance_quote
                if currency == "USDC"
                else cfg.risk.balance_tolerance_base
            )
            if (
                abs(D(totals.get(currency, "0")) - D(expected["balances"].get(currency, "0")))
                > tolerance
            ):
                reasons.append("BALANCE_MISMATCH:" + currency)
        known = store.known_order_ids()
        baseline_fills = set(expected["baseline_fill_ids"])
        for fill in actual.recent_fills:
            if (
                fill["entry_id"] not in baseline_fills
                and fill["order_id"] not in known
                and timestamp(fill["trade_time"]) >= timestamp(expected["initialized_at"])
            ):
                reasons.append("UNTRACKED_FILL")
    store.audit(
        "reconciliation_events",
        cycle,
        mode,
        actual.portfolio,
        {
            "status": "FAILED" if reasons else "OK",
            "reasons": sorted(set(reasons)),
            "actual_balances": totals,
            "expected_balances": expected["balances"] if expected else None,
        },
    )
    if reasons:
        raise SafetyError("RECONCILIATION_FAILED")
    if expected is None:
        store.initialize_exchange(
            actual.portfolio,
            {
                "portfolio_id": actual.portfolio_id,
                "balances": totals,
                "baseline_fill_ids": [f["entry_id"] for f in actual.recent_fills],
                "last_reconciled_at": actual.observed_at.isoformat(),
            },
            actual.observed_at,
        )
    else:
        store.exchange_seen(actual.portfolio, actual.observed_at)


def seed_ledger(actual: AccountState, cfg: AppConfig, *, paper: bool) -> Ledger:
    positions = {}
    for asset in cfg.enabled_assets:
        if asset.portfolio != actual.portfolio:
            continue
        base = asset.product_id.split("-")[0]
        amount = ZERO if paper else actual.balances[base].total
        positions[asset.product_id] = StrategyPosition(
            quantity=amount,
            cost_basis=None if amount else ZERO,
            # Existing position age/cost cannot be reconstructed from a short fill window.
            opened_at=None,
        )
    cash = (
        cfg.portfolios[actual.portfolio].paper_initial_usdc
        if paper
        else actual.balances["USDC"].total
    )
    return Ledger(cash=cash, positions=positions)


def value_portfolio(
    store: Storage,
    mode: str,
    actual: AccountState,
    ledger: Ledger,
    quotes: dict[str, Quote],
    now: datetime,
) -> PortfolioState:
    exposure = sum((p.quantity * quotes[pid].mid for pid, p in ledger.positions.items()), ZERO)
    equity = ledger.cash + exposure
    return PortfolioState(
        portfolio=actual.portfolio,
        observed_at=actual.observed_at,
        cash=ledger.cash,
        positions=ledger.positions,
        equity=equity,
        exposure_value=exposure,
        daily_loss_fraction=store.mark_equity(mode, actual.portfolio, equity, now),
        trades_today=store.trades_today(mode, actual.portfolio, now),
        open_orders=len(actual.open_orders),
        reconciled=True,
    )


def apply_fills(ledger: Ledger, fills: list[Fill]) -> Ledger:
    cash = ledger.cash
    positions = dict(ledger.positions)
    for fill in sorted(fills, key=lambda f: (f.trade_time, f.fill_id)):
        old = positions[fill.product_id]
        notional = fill.price * fill.base_size
        quantity, cost, realized = old.quantity, old.cost_basis, old.realized_pnl
        opened_at = old.opened_at
        if fill.side == "BUY":
            cash -= notional + fill.fee
            cost = cost + notional + fill.fee if cost is not None else None
            quantity += fill.base_size
            if old.quantity == 0:
                opened_at = fill.trade_time
        else:
            if fill.base_size > quantity:
                raise SafetyError("FILL_EXCEEDS_POSITION")
            cash += notional - fill.fee
            if cost is None:
                realized = None
            else:
                removed_cost = cost * fill.base_size / quantity
                if realized is not None:
                    realized += notional - fill.fee - removed_cost
                cost -= removed_cost
            quantity -= fill.base_size
            if quantity == 0:
                cost, opened_at = ZERO, None
        if cash < 0:
            raise SafetyError("FILL_EXCEEDS_CASH")
        positions[fill.product_id] = StrategyPosition(
            quantity=quantity,
            cost_basis=cost,
            realized_pnl=realized,
            opened_at=opened_at,
            last_trade_at=fill.trade_time,
            last_action=fill.side,
        )
    return Ledger(cash=cash, positions=positions)


def expected_after_fills(expected: dict, fills: list[Fill]) -> dict:
    balances = {c: D(v) for c, v in expected["balances"].items()}
    for fill in fills:
        base = fill.product_id.split("-")[0]
        sign = D("1") if fill.side == "BUY" else D("-1")
        balances[base] = balances.get(base, ZERO) + sign * fill.base_size
        balances["USDC"] -= sign * fill.price * fill.base_size + fill.fee
    return {
        "portfolio_id": expected["portfolio_id"],
        "baseline_fill_ids": expected["baseline_fill_ids"],
        "last_reconciled_at": expected["last_reconciled_at"],
        "balances": {c: str(v) for c, v in balances.items()},
    }


def strategy_summary(position: StrategyPosition, price: D, now: datetime) -> dict:
    cost = position.cost_basis
    value = position.quantity * price
    return {
        "quantity": position.quantity,
        "average_entry": cost / position.quantity
        if cost is not None and position.quantity
        else None,
        "unrealized_pnl": value - cost if cost is not None else None,
        "unrealized_return": value / cost - 1 if cost else None,
        "realized_pnl_since_inception": position.realized_pnl,
        "position_age_hours": (now - position.opened_at).total_seconds() / 3600
        if position.opened_at
        else None,
        "hours_since_last_trade": (now - position.last_trade_at).total_seconds() / 3600
        if position.last_trade_at
        else None,
        "last_actual_action": position.last_action,
    }
