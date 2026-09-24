from datetime import timedelta

import pandas as pd
import pytest
from conftest import NOW, candle_rows

from trader.config import GRANULARITIES
from trader.errors import SafetyError
from trader.features import atr, closed_candles, compute_features, rsi, timeframe_features


def raw_data(cfg):
    return {
        c.granularity: candle_rows(
            GRANULARITIES[c.granularity],
            c.count,
            int(NOW.timestamp()) // GRANULARITIES[c.granularity] * GRANULARITIES[c.granularity],
        )
        for c in cfg.market.candles
    }


def test_features_are_deterministic_and_return_math(cfg):
    raw = raw_data(cfg)
    a = compute_features(raw, cfg.market, NOW)
    assert a == compute_features(raw, cfg.market, NOW)
    assert a["return_15m"] == pytest.approx(102.99 / 102.96 - 1, abs=1e-10)
    assert a["return_3d"] > 0
    assert a["300s_rsi14"] == 100
    assert a["300s_atr14"] == pytest.approx(2)
    assert a["300s_max_drawdown72"] == 0


def test_no_lookahead_unfinished_and_future_bars_excluded(cfg):
    raw = raw_data(cfg)
    expected = compute_features(raw, cfg.market, NOW)
    for candle in cfg.market.candles:
        seconds = GRANULARITIES[candle.granularity]
        end = int(NOW.timestamp()) // seconds * seconds
        raw[candle.granularity].extend(
            [
                {
                    "start": str(end + i * seconds),
                    "open": "999999",
                    "close": "999999",
                    "high": "999999",
                    "low": "999999",
                    "volume": "999999",
                }
                for i in range(3)
            ]
        )
    assert compute_features(raw, cfg.market, NOW) == expected


@pytest.mark.parametrize(
    "damage", ["missing", "gap", "duplicate", "nan", "ohlc", "negative", "stale"]
)
def test_malformed_candles_rejected(damage):
    end = int(NOW.timestamp()) // 300 * 300
    rows = candle_rows(300, 100, end)
    if damage == "missing":
        del rows[-1]["close"]
    elif damage == "gap":
        del rows[40]
    elif damage == "duplicate":
        rows[-1] = rows[-2]
    elif damage == "nan":
        rows[-1]["close"] = "NaN"
    elif damage == "ohlc":
        rows[-1]["high"] = "1"
    elif damage == "negative":
        rows[-1]["volume"] = "-1"
    else:
        rows = candle_rows(300, 100, end - 300)
    with pytest.raises(SafetyError, match="CANDLE_DATA"):
        closed_candles(rows, 300, 100, NOW)


def test_indicator_edge_cases():
    assert rsi(pd.Series([100.0] * 100)) == 50
    assert rsi(pd.Series(list(range(100, 0, -1)))) == 0
    end = int(NOW.timestamp()) // 300 * 300
    rows = candle_rows(300, 100, end)
    for row in rows:
        row.update(open="100", close="100", high="100", low="100", volume="10")
    frame = closed_candles(rows, 300, 100, NOW)
    assert atr(frame) == 0
    assert timeframe_features(frame, 300)["volume_zscore24"] == 0
    with pytest.raises(SafetyError):
        closed_candles(rows, 300, 100, NOW + timedelta(hours=1))


def test_missing_timeframe_skips_all_features(cfg):
    with pytest.raises(SafetyError):
        compute_features({}, cfg.market, NOW)


def test_unfinished_candle_values_do_not_affect_closed_indicators(cfg):
    raw = raw_data(cfg)
    expected = compute_features(raw, cfg.market, NOW)
    end = int(NOW.timestamp()) // 300 * 300
    raw["FIVE_MINUTE"].append(
        {
            "start": str(end),
            "open": None,
            "high": None,
            "low": None,
            "close": "invalid-unfinished",
            "volume": None,
        }
    )
    assert compute_features(raw, cfg.market, NOW) == expected
