"""Retrospective price measurements only; never calls the model or an executor."""

import json
from datetime import UTC, datetime, timedelta
from math import ceil

from trader.config import AppConfig
from trader.errors import SafetyError, error_details
from trader.storage import Storage
from trader.util import ZERO, decimal, event, timestamp

HORIZONS = (1, 3, 6, 12, 24)
SECONDS = 300
GRACE = 180


def candle_map(rows: list[dict], start: int, end: int) -> dict[int, dict]:
    result = {}
    for row in rows:
        raw_start = decimal(row["start"])
        when = int(raw_start)
        values = {k: decimal(row[k]) for k in ("open", "high", "low", "close", "volume")}
        if (
            raw_start != when
            or when % SECONDS
            or not start <= when < end
            or when in result
            or not 0
            < values["low"]
            <= min(values["open"], values["close"])
            <= max(values["open"], values["close"])
            <= values["high"]
            or values["volume"] < 0
        ):
            raise SafetyError("OUTCOME_CANDLES_INVALID")
        result[when] = values
    return result


def track_outcomes(
    cfg: AppConfig, store: Storage, adapters: dict, mode: str, now: datetime
) -> dict:
    if not cfg.outcomes.enabled:
        return {"status": "DISABLED", "checked": 0, "errors": 0}
    pending = store.pending_outcomes(mode, now, cfg.outcomes.max_decisions_per_run)
    cache = {}
    errors = completed = 0
    for row in pending:
        decision = json.loads(row["payload_json"])
        result = json.loads(row["outcome_json"]) if row["outcome_json"] else {}
        status = "PENDING"
        next_check = now + timedelta(hours=1)
        try:
            at = timestamp(decision["decision_at"])
            price = decimal(decision["reference_price"])
            if price <= 0 or at > now:
                raise SafetyError("OUTCOME_REFERENCE_INVALID")
            result.update(
                {
                    "strategy": decision.get("strategy", "default"),
                    "decision_at": at,
                    "quote_time": decision["quote_time"],
                    "price_at_decision": price,
                    "action": decision["decision"]["action"],
                    "confidence": decision["decision"]["confidence"],
                    "reference_portfolio": decision.get("reference_portfolio", row["portfolio"]),
                    "granularity_seconds": SECONDS,
                    "excursion_basis": "long_asset_price",
                }
            )
            targets = {h: at + timedelta(hours=h) for h in HORIZONS}
            due = [h for h, target in targets.items() if target + timedelta(seconds=GRACE) <= now]
            observations = result.setdefault("observations", {})
            missing = [h for h in due if str(h) not in observations]
            if missing or (24 in due and "max_favorable_excursion_24h" not in result):
                start = ceil(at.timestamp() / SECONDS) * SECONDS
                end = int(targets[max(due)].timestamp()) // SECONDS * SECONDS
                key = (row["product_id"], start, end)
                if key not in cache:
                    reference = next(
                        (
                            a.portfolio
                            for a in cfg.decision_assets
                            if a.product_id == row["product_id"]
                        ),
                        next(iter(adapters)),
                    )
                    # A 24-hour window fits in a single <=288-bucket Coinbase request.
                    raw = adapters[reference].candles(
                        row["product_id"], "FIVE_MINUTE", start, end - 1, (end - start) // SECONDS
                    )
                    cache[key] = candle_map(raw, start, end)
                candles = cache[key]
                for horizon in missing:
                    endpoint = int(targets[horizon].timestamp()) // SECONDS * SECONDS
                    candle = candles.get(endpoint - SECONDS)
                    if candle is not None:
                        value = candle["close"] / price - 1
                        observations[str(horizon)] = {
                            "target_at": targets[horizon],
                            "observed_at": datetime.fromtimestamp(endpoint, UTC),
                            "price": candle["close"],
                            "return": value,
                            "timing_offset_seconds": endpoint - targets[horizon].timestamp(),
                        }
                        result[f"return_{horizon}h"] = value
                if 24 in due and "max_favorable_excursion_24h" not in result:
                    expected = set(range(start, end, SECONDS))
                    result["missing_candles_24h"] = len(expected - candles.keys())
                    if expected == candles.keys():
                        high_at = max(candles, key=lambda t: candles[t]["high"])
                        low_at = min(candles, key=lambda t: candles[t]["low"])
                        result.update(
                            {
                                "max_favorable_excursion_24h": max(
                                    ZERO, candles[high_at]["high"] / price - 1
                                ),
                                "max_adverse_excursion_24h": min(
                                    ZERO, candles[low_at]["low"] / price - 1
                                ),
                                "excursion_window_start": datetime.fromtimestamp(start, UTC),
                                "excursion_window_end": datetime.fromtimestamp(end, UTC),
                                "candle_count": len(candles),
                                "high": candles[high_at]["high"],
                                "low": candles[low_at]["low"],
                                "high_candle_start": datetime.fromtimestamp(high_at, UTC),
                                "low_candle_start": datetime.fromtimestamp(low_at, UTC),
                            }
                        )
            missing = [h for h in due if str(h) not in observations]
            result["missing_horizons"] = missing
            result.pop("error", None)
            if missing or (24 in due and "max_favorable_excursion_24h" not in result):
                status = "PARTIAL_DATA"
                errors += 1
            elif len(observations) == len(HORIZONS) and "max_favorable_excursion_24h" in result:
                status = "COMPLETE"
                completed += 1
            else:
                next_check = min(
                    target + timedelta(seconds=GRACE)
                    for h, target in targets.items()
                    if h not in due
                )
        except Exception as exc:
            status = "ERROR"
            errors += 1
            result["error"] = {
                "reason": exc.code if isinstance(exc, SafetyError) else "OUTCOME_READ_FAILED",
                "details": error_details(exc),
            }
        store.save_outcome(row, status, next_check, result, now)
        event(
            "decision_outcome",
            row["cycle_id"],
            mode,
            portfolio=row["portfolio"],
            product_id=row["product_id"],
            strategy=decision.get("strategy", "default"),
            status=status,
            error=result.get("error"),
            missing_horizons=result.get("missing_horizons"),
        )
    return {
        "status": "PARTIAL" if errors else "OK",
        "checked": len(pending),
        "batch_limit_reached": len(pending) == cfg.outcomes.max_decisions_per_run,
        "completed": completed,
        "errors": errors,
    }
