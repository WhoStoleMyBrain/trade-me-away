from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from trader.coinbase_client import FINAL_STATUSES, CoinbaseAdapter
from trader.config import AppConfig, Mode, active_mode
from trader.errors import SafetyError
from trader.portfolio import apply_fills, expected_after_fills, reconcile_actual, seed_ledger
from trader.schemas import (
    AccountState,
    ExecutionResult,
    Fill,
    Ledger,
    OrderIntent,
    StrategyPosition,
)
from trader.storage import Storage
from trader.util import ZERO, D, decimal, event, step, utcnow

Guard = Callable[[OrderIntent], OrderIntent]


class Executor(Protocol):
    mode: Mode

    def load_ledger(self, actual: AccountState) -> Ledger: ...
    def execute(self, intent: OrderIntent, guard: Guard) -> ExecutionResult: ...
    def recover(self, intent: OrderIntent) -> ExecutionResult: ...


class BaseExecutor:
    mode: Mode

    def __init__(
        self,
        cfg: AppConfig,
        store: Storage,
        adapters: dict[str, CoinbaseAdapter],
        env_file: Path = Path(".env"),
    ):
        self.cfg, self.store, self.adapters, self.env_file = cfg, store, adapters, env_file

    def verify_mode(self, intent: OrderIntent) -> None:
        if active_mode(self.env_file) != self.mode or intent.mode != self.mode:
            raise SafetyError("EXECUTION_MODE_CHANGED")

    def prepare(self, intent: OrderIntent, guard: Guard) -> OrderIntent:
        self.verify_mode(intent)
        if self.store.result(intent.client_order_id) is not None or any(
            i.client_order_id == intent.client_order_id for i in self.store.unresolved()
        ):
            raise SafetyError("DUPLICATE_ORDER_INTENT")
        if self.store.unresolved():
            raise SafetyError("UNRESOLVED_PREVIOUS_ORDER")
        approved = guard(intent)
        if (
            approved.client_order_id != intent.client_order_id
            or approved.cycle_id != intent.cycle_id
            or approved.mode != intent.mode
            or approved.portfolio != intent.portfolio
            or approved.product_id != intent.product_id
            or approved.side != intent.side
            or approved.base_size > intent.base_size
            or approved.quote_size > intent.quote_size
        ):
            raise SafetyError("PREFLIGHT_EXPANDED_OR_CHANGED_INTENT")
        self.verify_mode(approved)
        self.store.save_intent(approved)
        event("order_intent_persisted", intent.cycle_id, self.mode, intent=approved)
        return approved

    def _extend_ledger_products(self, ledger: Ledger, actual: AccountState) -> Ledger:
        products = {
            a.product_id for a in self.cfg.enabled_assets if a.portfolio == actual.portfolio
        }
        # Removing tracked assets could discard cost basis/exposure; require a separate migration.
        if set(ledger.positions) - products:
            raise SafetyError("LEDGER_CONFIGURATION_CHANGED")
        missing = products - set(ledger.positions)
        if any(actual.balances[p.split("-")[0]].total != 0 for p in missing):
            raise SafetyError("NEW_ASSET_HAS_UNTRACKED_BALANCE")
        if missing:
            ledger = ledger.model_copy(
                update={"positions": ledger.positions | {p: StrategyPosition() for p in missing}}
            )
            self.store.save_ledger(self.mode, actual.portfolio, ledger)
        return ledger


class PaperExecutor(BaseExecutor):
    mode: Mode = "paper"

    def load_ledger(self, actual: AccountState) -> Ledger:
        ledger = self.store.ledger(self.mode, actual.portfolio)
        if ledger is None:
            ledger = seed_ledger(actual, self.cfg, paper=True)
            self.store.save_ledger(self.mode, actual.portfolio, ledger)
        return self._extend_ledger_products(ledger, actual)

    def execute(self, intent: OrderIntent, guard: Guard) -> ExecutionResult:
        intent = self.prepare(intent, guard)
        ledger = self.store.ledger(self.mode, intent.portfolio)
        if ledger is None:
            raise SafetyError("PAPER_LEDGER_MISSING")
        adapter = self.adapters[intent.portfolio]
        # Guard supplied a fresh executable price; no simulated order is sent to the adapter.
        # Reference book side and spread are encoded by the bounded IOC price. For simulation,
        # read the current book again and enforce the same age/movement/spread checks.
        from trader.market_data import validate_quote

        product = adapter.product(intent.product_id)
        quote = adapter.quote(intent.product_id)
        validate_quote(quote, utcnow(), self.cfg.risk.max_data_age_seconds)
        if (
            abs(quote.mid / intent.reference_price - 1) > self.cfg.risk.max_price_move_fraction
            or quote.spread_bps > self.cfg.risk.max_spread_bps
        ):
            result = ExecutionResult(
                order_id=None,
                status="ABORTED",
                fills=[],
                terminal=True,
                reason="PAPER_QUOTE_CHANGED",
            )
            self.store.record_result(intent, result)
            return result
        sign = D("1") if intent.side == "BUY" else D("-1")
        price = (quote.ask if intent.side == "BUY" else quote.bid) * (
            1 + sign * self.cfg.execution.slippage_bps / 10000
        )
        quantity = step(
            intent.base_size * self.cfg.execution.paper_fill_fraction, product.base_increment
        )
        if intent.order_type == "market_ioc" and intent.side == "BUY":
            quantity = min(quantity, step(intent.quote_size / price, product.base_increment))
        crosses = (
            price <= intent.limit_price if intent.side == "BUY" else price >= intent.limit_price
        )
        if intent.order_type == "limit_ioc" and not crosses:
            quantity = ZERO
        order_id = "paper-" + intent.client_order_id
        fills = []
        if quantity:
            fills = [
                Fill(
                    fill_id=order_id + "-1",
                    order_id=order_id,
                    product_id=intent.product_id,
                    side=intent.side,
                    base_size=quantity,
                    price=price,
                    fee=quantity * price * self.cfg.execution.taker_fee_rate,
                    trade_time=utcnow(),
                )
            ]
        status = (
            "CANCELLED" if not quantity else "PARTIAL" if quantity < intent.base_size else "FILLED"
        )
        result = ExecutionResult(order_id=order_id, status=status, fills=fills, terminal=True)
        updated = apply_fills(ledger, fills)
        self.store.record_result(intent, result, updated)
        event("paper_execution_verified", intent.cycle_id, self.mode, result=result)
        return result

    def recover(self, intent: OrderIntent) -> ExecutionResult:
        # Paper effects are committed atomically; no effects exist for a PREPARED-only intent.
        result = ExecutionResult(
            order_id=None,
            status="ABORTED",
            fills=[],
            terminal=True,
            reason="INTERRUPTED_PAPER_ORDER",
        )
        self.store.record_result(intent, result)
        return result


class CoinbaseLiveExecutor(BaseExecutor):
    mode: Mode = "live"

    def load_ledger(self, actual: AccountState) -> Ledger:
        ledger = self.store.ledger(self.mode, actual.portfolio)
        if ledger is None:
            ledger = seed_ledger(actual, self.cfg, paper=False)
            self.store.save_ledger(self.mode, actual.portfolio, ledger)
        ledger = self._extend_ledger_products(ledger, actual)
        if abs(ledger.cash - actual.balances["USDC"].total) > self.cfg.risk.balance_tolerance_quote:
            raise SafetyError("STRATEGY_CASH_MISMATCH")
        positions = dict(ledger.positions)
        for pid, p in positions.items():
            quantity = actual.balances[pid.split("-")[0]].total
            if abs(p.quantity - quantity) > self.cfg.risk.balance_tolerance_base:
                raise SafetyError("STRATEGY_POSITION_MISMATCH")
            # Coinbase is authoritative for available balances; preserve historical cost basis.
            positions[pid] = p.model_copy(update={"quantity": quantity})
        return Ledger(cash=actual.balances["USDC"].available, positions=positions)

    def execute(self, intent: OrderIntent, guard: Guard) -> ExecutionResult:
        intent = self.prepare(intent, guard)
        # Mark uncertainty BEFORE the network call. A crash here cannot cause a resubmission.
        self.store.intent_status(intent.client_order_id, "SUBMITTING")
        self.verify_mode(intent)
        adapter = self.adapters[intent.portfolio]
        event(
            "LIVE_ORDER_SUBMISSION",
            intent.cycle_id,
            self.mode,
            client_order_id=intent.client_order_id,
        )
        try:
            response = adapter.submit(intent)
        except Exception:
            # Never repeat POST, even if discovery returns no matching order (eventual consistency).
            return self._discover_and_verify(intent)
        if response.get("success") is not True:
            # An explicit, well-formed business rejection proves no order was accepted.
            if (
                response.get("success") is False
                and response.get("error_response")
                and not response.get("success_response")
            ):
                result = ExecutionResult(
                    order_id=None,
                    status="REJECTED",
                    fills=[],
                    terminal=True,
                    reason="COINBASE_ORDER_REJECTED",
                )
                self.store.record_result(intent, result)
                return result
            return self._discover_and_verify(intent)
        success = response.get("success_response", {})
        order_id = success.get("order_id")
        if not order_id or success.get("client_order_id") != intent.client_order_id:
            return self._discover_and_verify(intent)
        result = ExecutionResult(
            order_id=order_id,
            status="UNRESOLVED",
            fills=[],
            terminal=False,
            reason="ACKNOWLEDGED_AWAITING_VERIFICATION",
        )
        self.store.record_result(intent, result)
        return self._verify(intent, order_id)

    def _unknown(self, intent: OrderIntent, order_id: str | None, reason: str) -> ExecutionResult:
        result = ExecutionResult(
            order_id=order_id, status="UNRESOLVED", fills=[], terminal=False, reason=reason
        )
        self.store.record_result(intent, result)
        event(
            "execution_unresolved",
            intent.cycle_id,
            self.mode,
            client_order_id=intent.client_order_id,
            order_id=order_id,
            reason=reason,
        )
        return result

    def _discover_and_verify(self, intent: OrderIntent) -> ExecutionResult:
        try:
            order = self.adapters[intent.portfolio].find_order(intent)
        except Exception:
            return self._unknown(intent, None, "SUBMISSION_OUTCOME_UNKNOWN")
        if order is None:
            return self._unknown(intent, None, "SUBMISSION_NOT_FOUND_YET")
        return self._verify(intent, order["order_id"])

    def _verify(self, intent: OrderIntent, order_id: str) -> ExecutionResult:
        adapter = self.adapters[intent.portfolio]
        for attempt in range(self.cfg.execution.verification_attempts):
            try:
                order = adapter.order(order_id)
                if (
                    order.get("order_id") != order_id
                    or order.get("client_order_id") != intent.client_order_id
                    or order.get("product_id") != intent.product_id
                    or order.get("side") != intent.side
                    or order.get("retail_portfolio_id") != adapter.portfolio_id
                    or order.get("product_type") != "SPOT"
                ):
                    return self._unknown(intent, order_id, "ORDER_IDENTITY_MISMATCH")
                fills = adapter.fills(order_id, intent)
                filled = sum((f.base_size for f in fills), ZERO)
                fees = sum((f.fee for f in fills), ZERO)
                value = sum((f.base_size * f.price for f in fills), ZERO)
                # Persist all observations, even when they cannot yet be applied to the ledger.
                self.store.audit(
                    "reconciliation_events",
                    intent.cycle_id,
                    self.mode,
                    order_id,
                    {"exchange_status": order.get("status"), "observed_fills": fills},
                )
                if (
                    order.get("status") in FINAL_STATUSES
                    and abs(filled - decimal(order["filled_size"]))
                    <= self.cfg.risk.balance_tolerance_base
                    and abs(fees - decimal(order["total_fees"]))
                    <= self.cfg.risk.balance_tolerance_quote
                    and abs(value - decimal(order["filled_value"]))
                    <= self.cfg.risk.balance_tolerance_quote
                    and order.get("settled") is True
                    and (filled > 0 or order["status"] != "FILLED")
                ):
                    if filled > intent.base_size + self.cfg.risk.balance_tolerance_base and not (
                        intent.order_type == "market_ioc" and intent.side == "BUY"
                    ):
                        return self._unknown(intent, order_id, "OVERFILL")
                    if intent.order_type == "limit_ioc" and any(
                        f.price > intent.limit_price
                        if intent.side == "BUY"
                        else f.price < intent.limit_price
                        for f in fills
                    ):
                        return self._unknown(intent, order_id, "LIMIT_PRICE_VIOLATION")
                    status = (
                        (
                            "FILLED"
                            if order["status"] == "FILLED"
                            and (
                                filled + self.cfg.risk.balance_tolerance_base >= intent.base_size
                                or intent.order_type == "market_ioc"
                                and intent.side == "BUY"
                            )
                            else "PARTIAL"
                        )
                        if filled
                        else (
                            "REJECTED" if order["status"] in {"FAILED", "REJECTED"} else "CANCELLED"
                        )
                    )
                    result = ExecutionResult(
                        order_id=order_id, status=status, fills=fills, terminal=True
                    )
                    self._settle(intent, result)
                    return result
            except Exception:
                # Reads may lag fills/settlement. Bound the wait, retaining the original client ID.
                pass
            if attempt + 1 < self.cfg.execution.verification_attempts:
                time.sleep(self.cfg.execution.verification_delay_seconds * 2 ** min(attempt, 3))
        return self._unknown(intent, order_id, "ORDER_OR_FILLS_NOT_VERIFIED")

    def _settle(self, intent: OrderIntent, result: ExecutionResult) -> None:
        ledger = self.store.ledger(self.mode, intent.portfolio)
        expected = self.store.exchange(intent.portfolio)
        if ledger is None or expected is None:
            raise SafetyError("EXECUTION_BASELINE_MISSING")
        applied = self.store.applied_fill_ids(self.mode)
        new_fills = [f for f in result.fills if f.fill_id not in applied]
        updated = apply_fills(ledger, new_fills)
        exchange = expected_after_fills(expected, new_fills)
        # Record actual fills even when post-trade balances have not yet converged.
        pending = result.model_copy(update={"terminal": False})
        self.store.record_result(intent, pending, updated, exchange)
        currencies = {p.split("-")[0] for p in updated.positions} | {"USDC"}
        actual = self.adapters[intent.portfolio].account(currencies)
        reconcile_actual(self.store, actual, self.cfg, intent.cycle_id, self.mode)
        self.store.record_result(intent, result)
        self.store.audit(
            "portfolio_snapshots",
            intent.cycle_id,
            self.mode,
            intent.portfolio,
            {"phase": "post_execution", "actual": actual, "strategy": updated},
        )
        event("live_fills_and_balances_verified", intent.cycle_id, self.mode, result=result)

    def recover(self, intent: OrderIntent) -> ExecutionResult:
        # Read-only recovery is allowed even if the service has since been switched to paper.
        previous = self.store.result(intent.client_order_id)
        return (
            self._verify(intent, previous.order_id)
            if previous and previous.order_id
            else self._discover_and_verify(intent)
        )


def make_executor(
    mode: Mode,
    cfg: AppConfig,
    store: Storage,
    adapters: dict[str, CoinbaseAdapter],
    env_file: Path = Path(".env"),
) -> Executor:
    if mode == "paper":
        return PaperExecutor(cfg, store, adapters, env_file)
    if mode == "live":
        return CoinbaseLiveExecutor(cfg, store, adapters, env_file)
    raise SafetyError("INVALID_TRADING_MODE")


def recover_orders(cfg: AppConfig, store: Storage, adapters: dict[str, CoinbaseAdapter]) -> None:
    """Read-only recovery for both modes. Unresolved live work also blocks paper trading."""
    for intent in store.unresolved():
        executor = make_executor(intent.mode, cfg, store, adapters)
        result = executor.recover(intent)
        if not result.terminal:
            raise SafetyError("UNRESOLVED_PREVIOUS_ORDER")
