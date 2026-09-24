from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trader.errors import SafetyError
from trader.util import D

Mode = Literal["paper", "live"]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]
PositiveMoney = Annotated[D, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[D, Field(ge=0, le=1, allow_inf_nan=False)]
GRANULARITIES = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "FOUR_HOUR": 14400,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Environment(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=True, hide_input_in_errors=True
    )
    # Deliberately required. The shipped example selects paper; absence is an error.
    TRADING_MODE: Mode
    OPENAI_API_KEY: SecretStr
    TRADER_CONFIG: Path = Path("config.yaml")
    TRADER_DB: Path = Path("var/trader.sqlite3")
    OPENAI_MODEL: str | None = None
    OPENAI_REASONING_EFFORT: str | None = None


class PortfolioConfig(ConfigModel):
    portfolio_id_env: EnvName
    api_key_env: EnvName
    api_secret_env: EnvName
    paper_initial_usdc: PositiveMoney = D("1000")

    @model_validator(mode="after")
    def distinct_references(self) -> PortfolioConfig:
        if len({self.portfolio_id_env, self.api_key_env, self.api_secret_env}) != 3:
            raise ValueError("portfolio and credential environment references must be distinct")
        return self


class AssetConfig(ConfigModel):
    product_id: str = Field(pattern=r"^[A-Z0-9]+-USDC$")
    portfolio: str
    enabled: bool = True


class CandleConfig(ConfigModel):
    granularity: str
    count: int = Field(default=300, ge=80, le=350)

    @model_validator(mode="after")
    def supported(self) -> CandleConfig:
        if self.granularity not in GRANULARITIES:
            raise ValueError("unsupported candle granularity")
        return self


class MarketConfig(ConfigModel):
    candles: list[CandleConfig] = Field(
        default_factory=lambda: [
            CandleConfig(granularity="FIVE_MINUTE"),
            CandleConfig(granularity="FIFTEEN_MINUTE"),
            CandleConfig(granularity="ONE_HOUR"),
        ]
    )
    max_snapshot_skew_seconds: int = Field(default=90, ge=1, le=300)
    candle_close_grace_seconds: int = Field(default=180, ge=0, le=600)

    @model_validator(mode="after")
    def coverage(self) -> MarketConfig:
        names = [c.granularity for c in self.candles]
        if len(set(names)) != len(names) or not names:
            raise ValueError("candle granularities must be nonempty and unique")
        for horizon in (900, 3600, 10800, 21600, 43200, 86400, 259200):
            if not any(
                horizon % GRANULARITIES[c.granularity] == 0
                and (c.count - 1) * GRANULARITIES[c.granularity] >= horizon
                for c in self.candles
            ):
                raise ValueError("candle configuration does not cover required return horizons")
        return self


class RiskConfig(ConfigModel):
    max_portfolio_exposure: Fraction = D("0.60")
    max_asset_exposure: Fraction = D("0.30")
    max_order_notional: PositiveMoney = D("100")
    min_order_notional: PositiveMoney = D("10")
    max_trades_per_day: int = Field(default=6, ge=1)
    min_trade_interval_seconds: int = Field(default=10800, ge=0)
    max_daily_loss_fraction: Fraction = D("0.03")
    max_spread_bps: PositiveMoney = D("30")
    max_price_move_fraction: Fraction = D("0.005")
    min_confidence: Fraction = D("0.70")
    max_data_age_seconds: int = Field(default=120, ge=1, le=600)
    max_decision_age_seconds: int = Field(default=300, ge=1, le=900)
    balance_tolerance_base: PositiveMoney = D("0.00000001")
    balance_tolerance_quote: PositiveMoney = D("0.01")

    @model_validator(mode="after")
    def consistent(self) -> RiskConfig:
        if self.min_order_notional > self.max_order_notional:
            raise ValueError("minimum order exceeds maximum")
        if self.max_asset_exposure > self.max_portfolio_exposure:
            raise ValueError("asset exposure exceeds portfolio limit")
        return self


class ExecutionConfig(ConfigModel):
    order_type: Literal["limit_ioc", "market_ioc"] = "limit_ioc"
    taker_fee_rate: Fraction = D("0.006")
    slippage_bps: Annotated[D, Field(ge=0, le=1000)] = D("5")
    limit_offset_bps: Annotated[D, Field(ge=0, le=1000)] = D("10")
    paper_fill_fraction: Fraction = D("1")
    verification_attempts: int = Field(default=5, ge=1, le=10)
    verification_delay_seconds: float = Field(default=1, ge=0, le=5)


class Pricing(ConfigModel):
    model: str = "gpt-6-luna"
    input_per_million: PositiveMoney = D("0.10")
    cached_input_per_million: PositiveMoney = D("0.01")
    cache_write_per_million: PositiveMoney = D("0.125")
    output_per_million: PositiveMoney = D("0.50")
    verified_on: str = "2026-09-24"


class LLMConfig(ConfigModel):
    model: str = "gpt-6-luna"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    max_output_tokens: int = Field(default=4096, ge=512, le=32768)
    max_prompt_bytes: int = Field(default=48000, ge=4000, le=100000)
    attempts: int = Field(default=3, ge=1, le=5)
    timeout_seconds: int = Field(default=90, ge=1, le=180)
    daily_budget_usd: PositiveMoney = D("1")
    monthly_budget_usd: PositiveMoney = D("10")
    pricing: Pricing = Field(default_factory=Pricing)

    @model_validator(mode="after")
    def known_price(self) -> LLMConfig:
        if self.pricing.model != self.model:
            raise ValueError("explicit matching pricing is required for selected model")
        return self


class AppConfig(ConfigModel):
    portfolios: dict[str, PortfolioConfig] = Field(min_length=1)
    assets: list[AssetConfig] = Field(min_length=1)
    market: MarketConfig = Field(default_factory=MarketConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @property
    def enabled_assets(self) -> list[AssetConfig]:
        return [a for a in self.assets if a.enabled]

    @model_validator(mode="after")
    def mappings(self) -> AppConfig:
        assets = self.enabled_assets
        if not assets or len({a.product_id for a in assets}) != len(assets):
            raise ValueError("enabled products must be nonempty and unique")
        if {a.portfolio for a in assets} != set(self.portfolios):
            raise ValueError("every portfolio must map to an enabled asset and vice versa")
        return self


def environment_values(env_file: Path) -> dict[str, str | None]:
    if env_file.exists():
        with env_file.open() as stream:
            keys = set()
            for binding in parse_stream(stream):
                if binding.error or binding.key and binding.key in keys:
                    raise SafetyError("AMBIGUOUS_ENV_FILE")
                if binding.key:
                    keys.add(binding.key)
    return {**dotenv_values(env_file, interpolate=False), **os.environ}


def active_mode(env_file: Path = Path(".env")) -> Mode:
    value = environment_values(env_file).get("TRADING_MODE")
    if value not in ("paper", "live"):
        raise SafetyError("INVALID_TRADING_MODE")
    return value


def required_secret(values: dict[str, str | None], name: str) -> str:
    value = values.get(name)
    if not value or "REPLACE_" in value:
        raise SafetyError("MISSING_CREDENTIAL_OR_PORTFOLIO_REFERENCE")
    return value


def load_config(env_file: Path = Path(".env")) -> tuple[Environment, AppConfig]:
    active_mode(env_file)
    try:
        env = Environment(_env_file=env_file)

        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def mapping(loader, node, deep=False):
            pairs = loader.construct_pairs(node, deep=deep)
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError("duplicate YAML keys")
            return result

        UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        # Preserve unquoted YAML monetary literals without a binary-float round trip.
        UniqueKeyLoader.add_constructor(
            "tag:yaml.org,2002:float", lambda loader, node: D(loader.construct_scalar(node))
        )
        data = yaml.load(env.TRADER_CONFIG.read_text(), Loader=UniqueKeyLoader)
        if env.OPENAI_MODEL:
            data.setdefault("llm", {})["model"] = env.OPENAI_MODEL
        if env.OPENAI_REASONING_EFFORT:
            data.setdefault("llm", {})["reasoning_effort"] = env.OPENAI_REASONING_EFFORT
        config = AppConfig.model_validate(data)
        required_secret({"key": env.OPENAI_API_KEY.get_secret_value()}, "key")
    except Exception:
        raise SafetyError("CONFIGURATION_INVALID") from None
    return env, config
