from __future__ import annotations

import re
from decimal import InvalidOperation
from pathlib import Path

from trader.coinbase_client import CoinbaseAdapter
from trader.config import AppConfig, active_mode
from trader.errors import SafetyError
from trader.portfolio import account_totals, reconciliation_reasons
from trader.storage import Storage
from trader.util import D, event, utcnow


def record_deposit(
    cfg: AppConfig,
    store: Storage,
    adapters: dict[str, CoinbaseAdapter],
    portfolio: str | None,
    amount_text: str | None,
    reference: str | None,
    env_file: Path = Path(".env"),
) -> dict:
    """Operator-confirmed USDC funding; caller holds the shared process lock. No API writes."""
    if active_mode(env_file) != "live":
        raise SafetyError("DEPOSIT_REQUIRES_LIVE_MODE")
    try:
        amount = D(amount_text or "")
    except InvalidOperation:
        raise SafetyError("DEPOSIT_AMOUNT_INVALID") from None
    if not amount.is_finite() or amount <= 0:
        raise SafetyError("DEPOSIT_AMOUNT_INVALID")
    if portfolio not in cfg.portfolios:
        raise SafetyError("DEPOSIT_PORTFOLIO_INVALID")
    if not reference or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", reference):
        raise SafetyError("DEPOSIT_REFERENCE_INVALID")
    result = {"portfolio": portfolio, "currency": "USDC", "amount": amount, "reference": reference}
    previous = store.deposit(reference)
    if previous:
        if previous["portfolio"] != portfolio or D(previous["amount"]) != amount:
            raise SafetyError("DEPOSIT_REFERENCE_CONFLICT")
        return result | {"status": "ALREADY_RECORDED"}
    if store.unresolved():
        raise SafetyError("UNRESOLVED_PREVIOUS_ORDER")
    expected = store.exchange(portfolio)
    ledger = store.ledger("live", portfolio)
    if (
        expected is None
        or ledger is None
        or store.db.execute(
            "SELECT 1 FROM daily_marks WHERE mode='live' AND portfolio=?", (portfolio,)
        ).fetchone()
        is None
    ):
        raise SafetyError("DEPOSIT_BASELINE_MISSING")
    risk = cfg.portfolio_risk(portfolio)
    if abs(ledger.cash - D(expected["balances"].get("USDC", "0"))) > risk.balance_tolerance_quote:
        raise SafetyError("STRATEGY_CASH_MISMATCH")
    products = {a.product_id for a in cfg.enabled_assets if a.portfolio == portfolio}
    if set(ledger.positions) != products or any(
        abs(position.quantity - D(expected["balances"].get(pid.split("-")[0], "0")))
        > risk.balance_tolerance_base
        for pid, position in ledger.positions.items()
    ):
        raise SafetyError("STRATEGY_POSITION_MISMATCH")

    adapter = adapters[portfolio]
    adapter.check_permissions("live")
    actual = adapter.account({p.split("-")[0] for p in products} | {"USDC"})
    now = utcnow()
    if actual.portfolio != portfolio or actual.portfolio_id != expected["portfolio_id"]:
        raise SafetyError("PORTFOLIO_MAPPING_CHANGED")
    age = (now - actual.observed_at).total_seconds()
    if age < -5 or age > risk.max_data_age_seconds:
        raise SafetyError("STALE_ACCOUNT")
    balances = dict(expected["balances"])
    # Require the exact declared increase, not the usual dust tolerance. A second reference
    # cannot credit the same small deposit again, and an absent transfer cannot be accepted.
    increase = actual.balances["USDC"].total - D(balances.get("USDC", "0"))
    if increase != amount:
        raise SafetyError("DEPOSIT_AMOUNT_MISMATCH", details={"portfolio": portfolio})
    balances["USDC"] = str(D(balances.get("USDC", "0")) + amount)
    updated = expected | {"balances": balances}
    reasons = reconciliation_reasons(store, actual, cfg, updated)
    if reasons:
        raise SafetyError(
            "RECONCILIATION_FAILED", details={"portfolio": portfolio, "reasons": reasons}
        )
    if active_mode(env_file) != "live":
        raise SafetyError("DEPOSIT_REQUIRES_LIVE_MODE")
    updated["last_reconciled_at"] = actual.observed_at.isoformat()
    payload = result | {
        "status": "DEPOSIT_RECORDED",
        "source": "operator_confirmed_external_funding",
        "expected_before": expected["balances"],
        "expected_after": balances,
        "actual_balances": account_totals(actual),
        "observed_at": actual.observed_at,
        "cash_before": ledger.cash,
        "cash_after": ledger.cash + amount,
    }
    store.record_deposit(
        reference,
        portfolio,
        amount,
        ledger.model_copy(update={"cash": ledger.cash + amount}),
        updated,
        payload,
        now,
    )
    event("external_deposit_recorded", reference, "live", **result)
    return result | {"status": "RECORDED"}
