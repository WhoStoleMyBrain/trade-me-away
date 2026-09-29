from datetime import timedelta

import pytest
from conftest import NOW

from trader.config import GRANULARITIES
from trader.errors import SafetyError
from trader.market_data import MarketData


@pytest.mark.parametrize("max_count", [False, True])
def test_snapshot_requests_only_closed_candles_with_inclusive_api_end(
    cfg, store, adapter, sdk, max_count
):
    cfg.assets = cfg.assets[:1]
    if max_count:
        cfg.market.candles = [c.model_copy(update={"count": 350}) for c in cfg.market.candles]
    markets, _ = MarketData(cfg, {"main": adapter}, store).snapshot("cycle", "paper")
    market = markets["BTC-USDC"]
    cutoff_time = NOW - timedelta(seconds=cfg.market.candle_close_grace_seconds)
    assert sdk.get_candles.call_count == len(cfg.market.candles)
    for call, candle in zip(sdk.get_candles.call_args_list, cfg.market.candles, strict=True):
        seconds = GRANULARITIES[candle.granularity]
        cutoff = int(cutoff_time.timestamp()) // seconds * seconds
        assert call.kwargs == {
            "product_id": "BTC-USDC",
            "granularity": candle.granularity,
            "start": str(cutoff - seconds * candle.count),
            "end": str(cutoff - 1),
            "limit": candle.count,
        }
        assert market.features[f"{seconds}s_closed_at_epoch"] == cutoff
    assert len(store.rows("computed_features")) == 1


def test_snapshot_still_rejects_missing_closed_candle(cfg, store, adapter, sdk):
    cfg.assets = cfg.assets[:1]
    original = sdk.get_candles.side_effect

    def missing_candle(**kwargs):
        response = original(**kwargs)
        if kwargs["granularity"] == "FIVE_MINUTE":
            del response["candles"][40]
        return response

    sdk.get_candles.side_effect = missing_candle
    with pytest.raises(SafetyError, match="CANDLE_DATA_INSUFFICIENT_OR_INVALID"):
        MarketData(cfg, {"main": adapter}, store).snapshot("cycle", "paper")
    assert not store.rows("computed_features")
