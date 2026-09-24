from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from trader.config import Mode
from trader.errors import SafetyError
from trader.util import ZERO, D

Money = Annotated[D, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[D, Field(gt=0, allow_inf_nan=False)]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Action(StrEnum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    HOLD = "HOLD"
    EXIT = "EXIT"


class Quality(StrEnum):
    GOOD = "GOOD"
    INSUFFICIENT = "INSUFFICIENT"


class Regime(StrEnum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class Decision(DomainModel):
    product_id: str
    action: Action
    target_exposure: Annotated[float, Field(strict=True, ge=0, le=1)]
    confidence: Annotated[float, Field(strict=True, ge=0, le=1)]
    expected_horizon_hours: Annotated[int, Field(strict=True, ge=1, le=168)]
    data_quality: Quality
    rationale: str = Field(min_length=1, max_length=280)

    @model_validator(mode="after")
    def consistent_exit(self) -> Decision:
        if self.action == Action.EXIT and self.target_exposure != 0:
            raise ValueError("EXIT requires zero target")
        return self


class DecisionBatch(DomainModel):
    market_regime: Regime
    decisions: list[Decision] = Field(min_length=1)

    def validate_products(self, products: set[str]) -> None:
        seen = [d.product_id for d in self.decisions]
        if set(seen) != products or len(seen) != len(products):
            raise SafetyError("DECISION_PRODUCT_SET_MISMATCH")


class Product(DomainModel):
    product_id: str
    base_currency: str
    quote_currency: Literal["USDC"]
    base_increment: Positive
    quote_increment: Positive
    price_increment: Positive
    base_min_size: Positive
    base_max_size: Positive
    quote_min_size: Positive
    quote_max_size: Positive
    tradable: bool
    market_allowed: bool


class Quote(DomainModel):
    product_id: str
    bid: Positive
    ask: Positive
    observed_at: AwareDatetime
    received_at: AwareDatetime

    @model_validator(mode="after")
    def spread_valid(self) -> Quote:
        if self.bid > self.ask:
            raise ValueError("crossed market")
        return self

    @property
    def mid(self) -> D:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> D:
        return (self.ask - self.bid) / self.mid * 10000


class Balance(DomainModel):
    available: Money
    hold: Money

    @property
    def total(self) -> D:
        return self.available + self.hold


class AccountState(DomainModel):
    portfolio: str
    portfolio_id: str
    observed_at: AwareDatetime
    balances: dict[str, Balance]
    open_orders: list[dict]
    recent_fills: list[dict]


class StrategyPosition(DomainModel):
    quantity: Money = ZERO
    cost_basis: Money | None = ZERO
    realized_pnl: D | None = ZERO
    opened_at: AwareDatetime | None = None
    last_trade_at: AwareDatetime | None = None
    last_action: str | None = None


class Ledger(DomainModel):
    cash: Money
    positions: dict[str, StrategyPosition]


class PortfolioState(DomainModel):
    portfolio: str
    observed_at: AwareDatetime
    cash: Money
    positions: dict[str, StrategyPosition]
    equity: Money
    exposure_value: Money
    daily_loss_fraction: D
    trades_today: int
    open_orders: int
    reconciled: bool

    def exposure(self, product_id: str, price: D) -> D:
        return self.positions[product_id].quantity * price / self.equity if self.equity else ZERO


class OrderIntent(DomainModel):
    client_order_id: str
    cycle_id: str
    mode: Mode
    portfolio: str
    product_id: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["limit_ioc", "market_ioc"]
    base_size: Positive
    quote_size: Positive
    limit_price: Positive
    reference_price: Positive
    approved_target_exposure: Annotated[D, Field(ge=0, le=1)]
    created_at: AwareDatetime


class RiskResult(DomainModel):
    status: Literal["APPROVED", "REDUCED", "REJECTED", "HOLD"]
    reasons: list[str]
    intent: OrderIntent | None


class Fill(DomainModel):
    fill_id: str
    order_id: str
    product_id: str
    side: Literal["BUY", "SELL"]
    base_size: Positive
    price: Positive
    fee: Money
    trade_time: AwareDatetime


class ExecutionResult(DomainModel):
    order_id: str | None
    status: Literal["FILLED", "PARTIAL", "CANCELLED", "REJECTED", "UNRESOLVED", "ABORTED"]
    fills: list[Fill]
    terminal: bool
    reason: str | None = None


class MarketState(DomainModel):
    product: Product
    quote: Quote
    features: dict[str, float]
    as_of: datetime
