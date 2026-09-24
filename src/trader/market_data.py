from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from trader.coinbase_client import CoinbaseAdapter
from trader.config import GRANULARITIES, AppConfig
from trader.errors import SafetyError
from trader.features import compute_features
from trader.schemas import AccountState, MarketState, Product, Quote
from trader.storage import Storage
from trader.util import event, utcnow


def validate_quote(quote: Quote, now: datetime, max_age: int) -> None:
    for time in (quote.observed_at, quote.received_at):
        age = (now - time).total_seconds()
        if age < -5 or age > max_age:
            raise SafetyError("STALE_QUOTE")


class MarketData:
    def __init__(self, cfg: AppConfig, adapters: dict[str, CoinbaseAdapter], store: Storage):
        self.cfg, self.adapters, self.store = cfg, adapters, store

    def accounts(self) -> dict[str, AccountState]:
        def fetch(name: str) -> AccountState:
            currencies = {
                a.product_id.split("-")[0] for a in self.cfg.enabled_assets if a.portfolio == name
            }
            adapter = self.adapters[name]
            adapter.check_fee_rate(self.cfg.execution.taker_fee_rate)
            return adapter.account(currencies | {"USDC"})

        with ThreadPoolExecutor(max_workers=min(8, len(self.adapters))) as pool:
            return dict(zip(self.adapters, pool.map(fetch, self.adapters), strict=True))

    def refresh(self) -> tuple[dict[str, tuple[Product, Quote]], dict[str, AccountState]]:
        def fetch(asset):
            adapter = self.adapters[asset.portfolio]
            return asset.product_id, (
                adapter.product(asset.product_id),
                adapter.quote(asset.product_id),
            )

        # Refresh every mapped asset to value shared portfolios consistently.
        with ThreadPoolExecutor(max_workers=min(8, len(self.cfg.enabled_assets) + 1)) as pool:
            accounts = pool.submit(self.accounts)
            markets = dict(pool.map(fetch, self.cfg.enabled_assets))
            actual = accounts.result()
        self.validate_snapshot([q for _, q in markets.values()], actual)
        return markets, actual

    def snapshot(
        self, cycle: str, mode: str
    ) -> tuple[dict[str, MarketState], dict[str, AccountState]]:
        as_of = utcnow()
        candle_as_of = as_of - timedelta(seconds=self.cfg.market.candle_close_grace_seconds)

        def fetch(asset):
            adapter = self.adapters[asset.portfolio]
            product = adapter.product(asset.product_id)
            raw = {}
            for candle in self.cfg.market.candles:
                seconds = GRANULARITIES[candle.granularity]
                end = int(candle_as_of.timestamp()) // seconds * seconds
                raw[candle.granularity] = adapter.candles(
                    asset.product_id,
                    candle.granularity,
                    end - seconds * candle.count,
                    end,
                    candle.count,
                )
            features = compute_features(raw, self.cfg.market, candle_as_of)
            quote = adapter.quote(asset.product_id)
            features["spread_bps"] = float(quote.spread_bps)
            return (
                asset.product_id,
                MarketState(product=product, quote=quote, features=features, as_of=as_of),
                raw,
            )

        with ThreadPoolExecutor(max_workers=min(8, len(self.cfg.enabled_assets) + 1)) as pool:
            account_future = pool.submit(self.accounts)
            collected = list(pool.map(fetch, self.cfg.enabled_assets))
            actual = account_future.result()
        markets = {}
        for pid, market, raw in collected:
            markets[pid] = market
            self.store.audit(
                "market_snapshots",
                cycle,
                mode,
                pid,
                {"as_of": as_of, "product": market.product, "quote": market.quote, "candles": raw},
            )
            self.store.audit("computed_features", cycle, mode, pid, market.features)
            event("features_created", cycle, mode, product_id=pid, count=len(market.features))
        self.validate_snapshot([m.quote for m in markets.values()], actual)
        return markets, actual

    def validate_snapshot(self, quotes: list[Quote], accounts: dict[str, AccountState]) -> None:
        now = utcnow()
        times = [a.observed_at for a in accounts.values()]
        for quote in quotes:
            validate_quote(quote, now, self.cfg.risk.max_data_age_seconds)
            times.append(quote.received_at)
        if (
            not times
            or (max(times) - min(times)).total_seconds() > self.cfg.market.max_snapshot_skew_seconds
        ):
            raise SafetyError("SNAPSHOT_TIME_SKEW")
        if any(
            (now - a.observed_at).total_seconds() > self.cfg.risk.max_data_age_seconds
            or (now - a.observed_at).total_seconds() < -5
            for a in accounts.values()
        ):
            raise SafetyError("STALE_ACCOUNT")
