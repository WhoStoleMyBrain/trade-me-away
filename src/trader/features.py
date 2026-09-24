"""Numerical indicators use float64; money, balances and executable sizes never do."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from trader.config import GRANULARITIES, MarketConfig
from trader.errors import SafetyError

HORIZONS = {
    "15m": 900,
    "1h": 3600,
    "3h": 10800,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "3d": 259200,
}


def closed_candles(raw: list[dict], seconds: int, count: int, as_of: datetime) -> pd.DataFrame:
    try:
        df = pd.DataFrame(raw)[["start", "open", "high", "low", "close", "volume"]].copy()
        df["start"] = pd.to_numeric(df["start"], errors="raise")
        # Exclude any unfinished/future candles before calculating any statistic.
        cutoff = int(as_of.timestamp()) // seconds * seconds
        df = (
            df[df.start + seconds <= cutoff].sort_values("start").tail(count).reset_index(drop=True)
        )
        df = df.apply(pd.to_numeric, errors="raise")
        if len(df) != count or not np.isfinite(df.to_numpy()).all():
            raise ValueError
        if df.start.duplicated().any() or (df.start % seconds != 0).any():
            raise ValueError
        if not (df.start.diff().iloc[1:] == seconds).all() or df.start.iloc[-1] + seconds != cutoff:
            raise ValueError
        if (df[["open", "high", "low", "close"]] <= 0).any().any() or (df.volume < 0).any():
            raise ValueError
        if (df.high < df[["open", "close", "low"]].max(axis=1)).any():
            raise ValueError
        if (df.low > df[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError
        return df
    except (ValueError, KeyError, TypeError, IndexError):
        raise SafetyError("CANDLE_DATA_INSUFFICIENT_OR_INVALID") from None


def rsi(close: pd.Series, period: int = 14) -> float:
    changes = close.diff()
    gain = (
        changes.clip(lower=0)
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
        .iloc[-1]
    )
    loss = (
        (-changes.clip(upper=0))
        .ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
        .iloc[-1]
    )
    return (
        50.0 if gain == loss == 0 else 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))
    )


def atr(df: pd.DataFrame, period: int = 14) -> float:
    previous = df.close.shift()
    tr = pd.concat(
        [df.high - df.low, (df.high - previous).abs(), (df.low - previous).abs()], axis=1
    ).max(axis=1)
    return float(tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1])


def timeframe_features(df: pd.DataFrame, seconds: int) -> dict[str, float]:
    close, volume = df.close, df.volume
    last = float(close.iloc[-1])
    recent = df.tail(72)
    ema12 = close.ewm(span=12, adjust=False).mean().iloc[-1]
    ema26 = close.ewm(span=26, adjust=False).mean().iloc[-1]
    true_range = atr(df)
    previous_volume = volume.iloc[-25:-1]
    std = previous_volume.std(ddof=0)
    if std == 0 and volume.iloc[-1] != previous_volume.mean():
        raise SafetyError("VOLUME_ZSCORE_UNDEFINED")
    current_sum, old_sum = volume.iloc[-12:].sum(), volume.iloc[-24:-12].sum()
    if old_sum <= 0:
        raise SafetyError("VOLUME_HISTORY_INSUFFICIENT")
    slope = np.polyfit(np.arange(24), np.log(close.iloc[-24:].to_numpy()), 1)[0]
    result = {
        "ema12_over_ema26": float(ema12 / ema26 - 1),
        "price_over_ema26": float(last / ema26 - 1),
        "rsi14": rsi(close),
        "atr14": true_range,
        "normalized_atr14": true_range / last,
        "realized_vol_24_bars": float(np.log(close).diff().tail(24).std(ddof=0) * np.sqrt(24)),
        "volume_change_12_bars": float(current_sum / old_sum - 1),
        "volume_zscore24": float((volume.iloc[-1] - previous_volume.mean()) / std) if std else 0.0,
        "distance_from_high72": float(last / recent.high.max() - 1),
        "distance_from_low72": float(last / recent.low.min() - 1),
        "log_trend_slope_per_hour24": float(slope * 3600 / seconds),
        "drawdown72": float(last / recent.close.max() - 1),
        "max_drawdown72": float((recent.close / recent.close.cummax() - 1).min()),
    }
    if not all(np.isfinite(v) for v in result.values()):
        raise SafetyError("NONFINITE_FEATURE")
    return result


def compute_features(
    raw: dict[str, list[dict]], config: MarketConfig, as_of: datetime
) -> dict[str, float]:
    frames = {}
    result = {}
    for candle in config.candles:
        seconds = GRANULARITIES[candle.granularity]
        frame = closed_candles(raw.get(candle.granularity, []), seconds, candle.count, as_of)
        frames[seconds] = frame
        result.update({f"{seconds}s_{k}": v for k, v in timeframe_features(frame, seconds).items()})
        result[f"{seconds}s_closed_at_epoch"] = float(frame.start.iloc[-1] + seconds)
    for name, horizon in HORIZONS.items():
        candidates = [s for s, f in frames.items() if horizon % s == 0 and len(f) > horizon // s]
        if not candidates:
            raise SafetyError("RETURN_HISTORY_MISSING")
        seconds = min(candidates)
        close = frames[seconds].close
        result[f"return_{name}"] = float(close.iloc[-1] / close.iloc[-1 - horizon // seconds] - 1)
    return {k: round(v, 10) for k, v in result.items()}
