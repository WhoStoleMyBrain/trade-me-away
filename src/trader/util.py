from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from trader.errors import SafetyError

D = Decimal
ZERO = D("0")
ONE = D("1")


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str | datetime) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None:
        raise SafetyError("NAIVE_TIMESTAMP")
    return parsed.astimezone(UTC)


def decimal(value: Any) -> Decimal:
    try:
        if isinstance(value, (bool, float)):
            raise ValueError
        result = D(value)
        if not result.is_finite():
            raise ValueError
        return result
    except (ValueError, TypeError, ArithmeticError):
        raise SafetyError("INVALID_DECIMAL") from None


def step(value: Decimal, increment: Decimal, *, up: bool = False) -> Decimal:
    if increment <= 0:
        raise SafetyError("INVALID_INCREMENT")
    return (value / increment).to_integral_value(
        rounding=ROUND_UP if up else ROUND_DOWN
    ) * increment


def json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return timestamp(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def dumps(value: Any) -> str:
    return json.dumps(
        value, default=json_default, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def safe_read[T](call: Callable[[], T], attempts: int = 3, delay: float = 0.5) -> T:
    """Bounded retries ONLY for transport errors, throttling, and server failures."""
    import requests

    for attempt in range(attempts):
        try:
            return call()
        except (requests.Timeout, requests.ConnectionError):
            pass
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status != 429 and status < 500:
                raise SafetyError("COINBASE_READ_REJECTED") from None
        except SafetyError:
            raise
        except Exception:
            raise SafetyError("COINBASE_RESPONSE_INVALID") from None
        if attempt + 1 < attempts:
            time.sleep(delay * 2**attempt)
    raise SafetyError("COINBASE_READ_UNAVAILABLE")


def event(name: str, cycle_id: str, mode: str, **fields: Any) -> None:
    """Only pass explicitly selected domain data, never SDK responses/exceptions."""
    logging.getLogger("trader").info(
        dumps({"time": utcnow(), "event": name, "cycle_id": cycle_id, "mode": mode, **fields})
    )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Third-party HTTP error/debug logging may include request bodies or authentication.
    for name in ("coinbase", "coinbase.RESTClient", "httpx", "httpcore", "openai", "dotenv.main"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
