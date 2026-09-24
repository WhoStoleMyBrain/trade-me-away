from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from trader.config import AppConfig, Mode
from trader.costs import budget_available
from trader.errors import SafetyError
from trader.execution import Executor, recover_orders
from trader.llm import DecisionClient
from trader.market_data import MarketData
from trader.portfolio import reconcile_actual, value_portfolio
from trader.prompt import build_payload
from trader.risk import assess, cap_refreshed_intent
from trader.schemas import AccountState, OrderIntent, PortfolioState, Quote
from trader.storage import Storage
from trader.util import event, utcnow


def cycle_slot(now: datetime) -> str:
    return now.replace(hour=now.hour // 3 * 3, minute=0, second=0, microsecond=0).isoformat()


def check_schedule(now: datetime) -> None:
    boundary = datetime.fromisoformat(cycle_slot(now))
    age = (now - boundary).total_seconds()
    if not 300 <= age <= 1200:
        raise SafetyError("OUTSIDE_SCHEDULE_WINDOW")


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        mode: Mode,
        store: Storage,
        market: MarketData,
        llm: DecisionClient,
        executor: Executor,
    ):
        self.cfg, self.mode, self.store = cfg, mode, store
        self.market, self.llm, self.executor = market, llm, executor

    def portfolios(
        self, actual: dict[str, AccountState], quotes: dict[str, Quote], cycle: str, phase: str
    ) -> dict[str, PortfolioState]:
        states = {}
        for name, account in actual.items():
            reconcile_actual(self.store, account, self.cfg, cycle, self.mode)
            ledger = self.executor.load_ledger(account)
            states[name] = value_portfolio(self.store, self.mode, account, ledger, quotes, utcnow())
            self.store.audit(
                "portfolio_snapshots",
                cycle,
                self.mode,
                name,
                {"phase": phase, "actual": account, "active": states[name]},
            )
            event(
                "portfolio_reconciled", cycle, self.mode, portfolio=name, equity=states[name].equity
            )
        return states

    def startup(self, cycle: str, *, allow_cancel: bool = False) -> None:
        for adapter in self.market.adapters.values():
            adapter.check_permissions(self.mode)
        recover_orders(
            self.cfg,
            self.store,
            self.market.adapters,
            allow_cancel=allow_cancel and self.mode == "live",
            env_file=self.executor.env_file,
        )
        refreshed, actual = self.market.refresh()
        for product, _ in refreshed.values():
            if not product.tradable or (
                self.cfg.execution.order_type == "market_ioc" and not product.market_allowed
            ):
                raise SafetyError("PRODUCT_NOT_TRADABLE")
        self.portfolios(actual, {pid: q for pid, (_, q) in refreshed.items()}, cycle, "startup")
        if self.store.unresolved():
            raise SafetyError("UNRESOLVED_PREVIOUS_ORDER")
        if not budget_available(self.store, self.cfg.llm, utcnow()):
            raise SafetyError("API_BUDGET_EXCEEDED")
        event("startup_checks_passed", cycle, self.mode)

    def run(self, *, scheduled: bool = False) -> str:
        start = utcnow()
        if scheduled:
            check_schedule(start)
        cycle = str(uuid4())
        self.store.begin_cycle(cycle, cycle_slot(start), self.mode, self.cfg, start)
        event("cycle_started", cycle, self.mode)
        try:
            self.startup(cycle, allow_cancel=True)
            markets, actual = self.market.snapshot(cycle, self.mode)
            states = self.portfolios(
                actual, {p: m.quote for p, m in markets.items()}, cycle, "decision"
            )
            # Preflight all assets locally before incurring model costs.
            for market in markets.values():
                if not market.features or not market.product.tradable:
                    raise SafetyError("MARKET_STATE_INSUFFICIENT")
            if any(
                p.equity <= 0 or p.daily_loss_fraction >= self.cfg.risk.max_daily_loss_fraction
                for p in states.values()
            ):
                raise SafetyError("PORTFOLIO_PREFLIGHT_FAILED")
            payload = build_payload(self.cfg, markets, states, self.store, self.mode, utcnow())
            # One joint decision, with transport-only retries internal to the client.
            batch = self.llm.decide(payload, set(markets), cycle, self.mode)
            batch.validate_products(set(markets))
            decisions = {d.product_id: d for d in batch.decisions}
            decision_at = min(m.as_of for m in markets.values())
            for asset in self.cfg.enabled_assets:
                decision = decisions[asset.product_id]
                market = markets[asset.product_id]
                risk = assess(
                    decision,
                    states[asset.portfolio],
                    market.product,
                    market.quote,
                    self.cfg,
                    cycle,
                    self.mode,
                    utcnow(),
                    decision_at=decision_at,
                    budget_ok=budget_available(self.store, self.cfg.llm, utcnow()),
                )
                self.store.audit(
                    "risk_results",
                    cycle,
                    self.mode,
                    asset.product_id,
                    {"phase": "initial", "result": risk},
                )
                event(
                    "risk_decision",
                    cycle,
                    self.mode,
                    product_id=asset.product_id,
                    status=risk.status,
                    reasons=risk.reasons,
                )
                if risk.intent is None:
                    continue

                def guard(intent: OrderIntent) -> OrderIntent:
                    refreshed, accounts = self.market.refresh()
                    updated = self.portfolios(
                        accounts, {p: q for p, (_, q) in refreshed.items()}, cycle, "pre_execution"
                    )
                    product, quote = refreshed[intent.product_id]
                    checked = assess(
                        decisions[intent.product_id],
                        updated[intent.portfolio],
                        product,
                        quote,
                        self.cfg,
                        cycle,
                        self.mode,
                        utcnow(),
                        original_price=intent.reference_price,
                        decision_at=decision_at,
                        budget_ok=budget_available(self.store, self.cfg.llm, utcnow()),
                    )
                    self.store.audit(
                        "risk_results",
                        cycle,
                        self.mode,
                        intent.product_id,
                        {"phase": "pre_execution", "result": checked},
                    )
                    if checked.intent is None:
                        raise SafetyError("PRE_EXECUTION_ABORT")
                    final = cap_refreshed_intent(intent, checked.intent, product)
                    if final.base_size < product.base_min_size or final.quote_size < max(
                        product.quote_min_size, self.cfg.risk.min_order_notional
                    ):
                        raise SafetyError("PRE_EXECUTION_BELOW_MINIMUM")
                    return final

                result = self.executor.execute(risk.intent, guard)
                if not result.terminal:
                    raise SafetyError("UNRESOLVED_EXECUTION")
                # Refresh the entire portfolio after each execution, so shared cash/exposure and
                # daily limits are updated before considering another asset from the fixed batch.
                refreshed, accounts = self.market.refresh()
                states = self.portfolios(
                    accounts, {p: q for p, (_, q) in refreshed.items()}, cycle, "after_execution"
                )
            self.store.finish_cycle(cycle, "COMPLETED")
            event("cycle_completed", cycle, self.mode)
            return cycle
        except Exception as exc:
            reason = exc.code if isinstance(exc, SafetyError) else "UNEXPECTED_CYCLE_FAILURE"
            self.store.finish_cycle(cycle, "HOLD", reason)
            self.store.audit(
                "risk_results",
                cycle,
                self.mode,
                "cycle",
                {"status": "REJECTED", "reasons": [reason]},
            )
            event("cycle_failed_closed", cycle, self.mode, reason=reason)
            raise SafetyError(reason) from None
