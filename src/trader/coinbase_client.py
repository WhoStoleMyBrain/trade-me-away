"""The only Coinbase-specific boundary. No transfers, margin, or withdrawal API exists here."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

from coinbase.rest import RESTClient

from trader.config import AppConfig, Mode, required_secret
from trader.errors import SafetyError
from trader.schemas import AccountState, Balance, Fill, OrderIntent, Product, Quote
from trader.util import ZERO, decimal, safe_read, timestamp, utcnow

ACTIVE_STATUSES = ["OPEN", "PENDING", "QUEUED", "CANCEL_QUEUED"]
FINAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "FAILED", "REJECTED"}


def plain(value: Any) -> dict:
    result = value if isinstance(value, dict) else value.to_dict()
    if not isinstance(result, dict):
        raise SafetyError("COINBASE_RESPONSE_INVALID")
    return result


class CoinbaseAdapter:
    def __init__(self, client: RESTClient, portfolio: str, portfolio_id: str):
        self.client = client
        self.portfolio = portfolio
        self.portfolio_id = portfolio_id
        self._lock = threading.RLock()

    def read(self, method: str, **kwargs: Any) -> dict:
        with self._lock:
            return safe_read(lambda: plain(getattr(self.client, method)(**kwargs)))

    def check_permissions(self, mode: Mode) -> None:
        p = self.read("get_api_key_permissions")
        if p.get("can_view") is not True or p.get("portfolio_uuid") != self.portfolio_id:
            raise SafetyError("COINBASE_PORTFOLIO_PERMISSION_MISMATCH")
        if p.get("can_transfer") is not False:
            raise SafetyError("COINBASE_TRANSFER_PERMISSION_ENABLED_OR_UNKNOWN")
        if mode == "live" and p.get("can_trade") is not True:
            raise SafetyError("COINBASE_TRADE_PERMISSION_MISSING")

    def pages(self, method: str, key: str, **kwargs: Any) -> list[dict]:
        result: list[dict] = []
        cursor = None
        seen = set()
        for _ in range(100):
            page = self.read(method, limit=100, **({"cursor": cursor} if cursor else {}), **kwargs)
            if page.get("proof_token_required"):
                raise SafetyError("COINBASE_HISTORY_AUTHENTICATION_REQUIRED")
            if not isinstance(page.get(key), list):
                raise SafetyError("COINBASE_PAGE_INVALID")
            result.extend(page[key])
            # Fills uses cursor-only pagination; accounts/orders expose has_next.
            next_cursor = page.get("cursor")
            if "has_next" in page:
                if not isinstance(page["has_next"], bool):
                    raise SafetyError("COINBASE_PAGINATION_INVALID")
                more = page["has_next"]
            elif method == "get_fills":
                more = bool(next_cursor) and bool(page[key])
            else:
                raise SafetyError("COINBASE_PAGINATION_MISSING")
            if not more:
                return result
            if not next_cursor or next_cursor in seen:
                raise SafetyError("COINBASE_PAGINATION_INCOMPLETE")
            seen.add(next_cursor)
            cursor = next_cursor
        raise SafetyError("COINBASE_PAGINATION_LIMIT")

    def product(self, product_id: str) -> Product:
        data = self.read("get_product", product_id=product_id, get_tradability_status=True)
        base, quote = product_id.split("-")
        if (
            data.get("product_id") != product_id
            or data.get("product_type") != "SPOT"
            or data.get("quote_currency_id") != quote
            or data.get("base_currency_id") != base
        ):
            raise SafetyError("PRODUCT_IDENTITY_OR_TYPE_MISMATCH")
        flags = (
            "is_disabled",
            "trading_disabled",
            "cancel_only",
            "post_only",
            "auction_mode",
            "view_only",
        )
        if any(not isinstance(data.get(f), bool) for f in (*flags, "limit_only")):
            raise SafetyError("PRODUCT_TRADABILITY_MISSING")
        return Product(
            product_id=product_id,
            base_currency=base,
            quote_currency=quote,
            base_increment=decimal(data["base_increment"]),
            quote_increment=decimal(data["quote_increment"]),
            price_increment=decimal(data["price_increment"]),
            base_min_size=decimal(data["base_min_size"]),
            base_max_size=decimal(data["base_max_size"]),
            quote_min_size=decimal(data["quote_min_size"]),
            quote_max_size=decimal(data["quote_max_size"]),
            tradable=not any(data[f] for f in flags) and data.get("status") == "online",
            market_allowed=not data["limit_only"],
        )

    def quote(self, product_id: str) -> Quote:
        data = self.read("get_best_bid_ask", product_ids=[product_id])
        books = data.get("pricebooks", [])
        if len(books) != 1 or books[0].get("product_id") != product_id:
            raise SafetyError("QUOTE_PRODUCT_MISMATCH")
        book = books[0]
        if not book.get("bids") or not book.get("asks") or not book.get("time"):
            raise SafetyError("QUOTE_MISSING")
        return Quote(
            product_id=product_id,
            bid=decimal(book["bids"][0]["price"]),
            ask=decimal(book["asks"][0]["price"]),
            observed_at=timestamp(book["time"]),
            received_at=utcnow(),
        )

    def candles(
        self, product_id: str, granularity: str, start: int, end: int, count: int
    ) -> list[dict]:
        data = self.read(
            "get_candles",
            product_id=product_id,
            start=str(start),
            end=str(end),
            granularity=granularity,
            limit=count,
        )
        if not isinstance(data.get("candles"), list):
            raise SafetyError("CANDLES_MISSING")
        return data["candles"]

    def account(self, required_currencies: set[str]) -> AccountState:
        # Timestamp the start, conservatively accounting for pagination/read latency.
        observed_at = utcnow()
        accounts = self.pages("get_accounts", "accounts")
        balances = {}
        for account in accounts:
            if account.get("retail_portfolio_id") != self.portfolio_id:
                raise SafetyError("ACCOUNT_PORTFOLIO_MISMATCH")
            currency = account["currency"]
            if (
                currency in balances
                or account.get("active") is not True
                or account.get("ready") is not True
            ):
                raise SafetyError("ACCOUNT_UNAVAILABLE_OR_DUPLICATE")
            if (
                account["available_balance"]["currency"] != currency
                or account["hold"]["currency"] != currency
            ):
                raise SafetyError("BALANCE_CURRENCY_MISMATCH")
            balances[currency] = Balance(
                available=decimal(account["available_balance"]["value"]),
                hold=decimal(account["hold"]["value"]),
            )
        if "USDC" not in balances:
            raise SafetyError("USDC_ACCOUNT_MISSING")
        # A complete authenticated account listing establishes zero for absent crypto accounts.
        for currency in required_currencies:
            balances.setdefault(currency, Balance(available=ZERO, hold=ZERO))
        orders = self.pages("list_orders", "orders", order_status=ACTIVE_STATUSES)
        fills = self.pages(
            "get_fills",
            "fills",
            start_sequence_timestamp=(observed_at - timedelta(days=7)).isoformat(),
        )
        for item in [*orders, *fills]:
            if item.get("retail_portfolio_id") != self.portfolio_id:
                raise SafetyError("ORDER_PORTFOLIO_MISMATCH")
        return AccountState(
            portfolio=self.portfolio,
            portfolio_id=self.portfolio_id,
            observed_at=observed_at,
            balances=balances,
            open_orders=[self._order_summary(o) for o in orders],
            recent_fills=[self._fill_summary(f) for f in fills],
        )

    @staticmethod
    def _order_summary(order: dict) -> dict:
        return {
            k: order[k] for k in ("order_id", "client_order_id", "product_id", "side", "status")
        }

    @staticmethod
    def _fill_summary(fill: dict) -> dict:
        return {
            k: fill[k]
            for k in (
                "entry_id",
                "order_id",
                "product_id",
                "side",
                "trade_time",
                "price",
                "size",
                "commission",
                "size_in_quote",
            )
        }

    def submit(self, intent: OrderIntent) -> dict:
        """One write attempt. Caller must persist intent first. Never retry here."""
        params = {"client_order_id": intent.client_order_id, "product_id": intent.product_id}
        with self._lock:
            if intent.order_type == "limit_ioc":
                response = self.client.limit_order_ioc(
                    **params,
                    side=intent.side,
                    base_size=str(intent.base_size),
                    limit_price=str(intent.limit_price),
                )
            elif intent.side == "BUY":
                response = self.client.market_order_buy(**params, quote_size=str(intent.quote_size))
            else:
                response = self.client.market_order_sell(**params, base_size=str(intent.base_size))
        return plain(response)

    def find_order(self, intent: OrderIntent) -> dict | None:
        orders = self.pages(
            "list_orders",
            "orders",
            start_date=(intent.created_at - timedelta(minutes=5)).isoformat(),
        )
        matches = [o for o in orders if o.get("client_order_id") == intent.client_order_id]
        if len(matches) > 1:
            raise SafetyError("DUPLICATE_EXCHANGE_CLIENT_ID")
        return matches[0] if matches else None

    def order(self, order_id: str) -> dict:
        data = self.read("get_order", order_id=order_id)
        if not isinstance(data.get("order"), dict):
            raise SafetyError("ORDER_STATE_MISSING")
        return data["order"]

    def fills(self, order_id: str, intent: OrderIntent) -> list[Fill]:
        data = self.pages("get_fills", "fills", order_ids=[order_id])
        fills = {}
        for item in data:
            if (
                item["order_id"] != order_id
                or item["product_id"] != intent.product_id
                or item["side"] != intent.side
                or item.get("retail_portfolio_id") != self.portfolio_id
            ):
                raise SafetyError("FILL_IDENTITY_MISMATCH")
            if not isinstance(item.get("size_in_quote"), bool):
                raise SafetyError("FILL_UNIT_UNKNOWN")
            price, size = decimal(item["price"]), decimal(item["size"])
            if price <= 0:
                raise SafetyError("FILL_PRICE_INVALID")
            fill = Fill(
                fill_id=item["entry_id"],
                order_id=order_id,
                product_id=intent.product_id,
                side=intent.side,
                base_size=size / price if item["size_in_quote"] else size,
                price=price,
                fee=decimal(item["commission"]),
                trade_time=timestamp(item["trade_time"]),
            )
            if fill.trade_time > utcnow() + timedelta(
                seconds=5
            ) or fill.trade_time < intent.created_at - timedelta(minutes=5):
                raise SafetyError("FILL_TIMESTAMP_INVALID")
            if fill.fill_id in fills and fills[fill.fill_id] != fill:
                raise SafetyError("FILL_DUPLICATE_CONFLICT")
            fills[fill.fill_id] = fill
        return list(fills.values())


def build_adapters(cfg: AppConfig, values: dict[str, str | None]) -> dict[str, CoinbaseAdapter]:
    adapters = {}
    ids = set()
    for name, portfolio in cfg.portfolios.items():
        portfolio_id = required_secret(values, portfolio.portfolio_id_env)
        if portfolio_id in ids:
            raise SafetyError("DUPLICATE_PORTFOLIO_MAPPING")
        ids.add(portfolio_id)
        client = RESTClient(
            api_key=required_secret(values, portfolio.api_key_env),
            api_secret=required_secret(values, portfolio.api_secret_env).replace("\\n", "\n"),
            timeout=15,
            verbose=False,
        )
        adapters[name] = CoinbaseAdapter(client, name, portfolio_id)
    return adapters
