from __future__ import annotations

from datetime import datetime

from trader.config import LLMConfig, Pricing
from trader.errors import SafetyError
from trader.storage import Storage
from trader.util import ZERO, D


def estimate_cost(usage: dict, pricing: Pricing) -> D:
    count, cached = usage["input_tokens"], usage["cached_input_tokens"]
    output = usage["output_tokens"]
    if any(type(v) is not int or v < 0 for v in usage.values()) or cached > count:
        raise SafetyError("API_USAGE_INVALID")
    # Use the cache-write rate for uncached tokens conservatively when no separate breakdown exists.
    # Reasoning tokens are a subset of output_tokens and are NOT charged twice.
    uncached_rate = max(pricing.input_per_million, pricing.cache_write_per_million)
    return (
        D(count - cached) * uncached_rate
        + D(cached) * pricing.cached_input_per_million
        + D(output) * pricing.output_per_million
    ) / 1_000_000


def reservation(prompt_bytes: int, cfg: LLMConfig) -> D:
    if prompt_bytes > cfg.max_prompt_bytes:
        raise SafetyError("PROMPT_SIZE_LIMIT")
    # UTF-8 byte count + framing is a conservative upper bound for text tokenization.
    return estimate_cost(
        {
            "input_tokens": prompt_bytes + 2048,
            "cached_input_tokens": 0,
            "output_tokens": cfg.max_output_tokens,
            "reasoning_tokens": 0,
        },
        cfg.pricing,
    )


def budget_available(store: Storage, cfg: LLMConfig, now: datetime, amount: D = ZERO) -> bool:
    day, month = store.spending(now)
    return day + amount < cfg.daily_budget_usd and month + amount < cfg.monthly_budget_usd
