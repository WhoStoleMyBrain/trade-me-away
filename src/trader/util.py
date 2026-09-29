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

from trader.errors import SafetyError, error_details

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


def safe_read[T](
    call: Callable[[], T],
    attempts: int = 3,
    delay: float = 0.5,
    *,
    operation: str = "coinbase_read",
) -> T:
    """Bounded retries ONLY for transport errors, throttling, and server failures."""
    import requests

    details = {}

    def record(exc: Exception, attempt: int, status: int | None, retryable: bool) -> dict:
        diagnostic = {
            **error_details(exc),
            "operation": operation,
            "attempt": attempt + 1,
            "max_attempts": attempts,
            "http_status": status,
            "retryable": retryable,
        }
        # The read boundary also runs in worker threads and during standalone maintenance.
        # Terminal diagnostics are propagated to the caller's cycle/command failure record.
        event("coinbase_read_error", None, None, details=diagnostic)
        return diagnostic

    for attempt in range(attempts):
        try:
            return call()
        except (requests.Timeout, requests.ConnectionError) as exc:
            details = record(exc, attempt, None, True)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            details = record(exc, attempt, status or None, status == 429 or status >= 500)
            if status != 429 and status < 500:
                raise SafetyError("COINBASE_READ_REJECTED", details=details) from None
        except SafetyError as exc:
            exc.details = record(exc, attempt, None, False)
            raise
        except Exception as exc:
            details = record(exc, attempt, None, False)
            raise SafetyError("COINBASE_RESPONSE_INVALID", details=details) from None
        if attempt + 1 < attempts:
            time.sleep(delay * 2**attempt)
    raise SafetyError("COINBASE_READ_UNAVAILABLE", details=details)


def event(name: str, cycle_id: str | None, mode: str | None, **fields: Any) -> None:
    """Only pass explicitly selected domain data, never SDK responses/exceptions."""
    logging.getLogger("trader").info(
        dumps({"time": utcnow(), "event": name, "cycle_id": cycle_id, "mode": mode, **fields})
    )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Third-party HTTP error/debug logging may include request bodies or authentication.
    for name in ("coinbase", "coinbase.RESTClient", "httpx", "httpcore", "openai", "dotenv.main"):
        logger = logging.getLogger(name)
        # Lazy SDK imports can reset the level and add handlers after startup configuration.
        logger.disabled = True
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
