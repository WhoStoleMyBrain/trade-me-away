import json

from conftest import NOW, PRODUCTS

from trader.prompt import INSTRUCTIONS, build_payload
from trader.schemas import MarketState


def test_joint_prompt_compact_and_no_previous_opinions(cfg, store, product, quote, portfolio):
    markets = {
        p: MarketState(
            product=product.model_copy(update={"product_id": p}),
            quote=quote.model_copy(update={"product_id": p}),
            features={"return_15m": 0.01},
            as_of=NOW,
        )
        for p in PRODUCTS
    }
    store.audit(
        "model_decisions", "previous", "paper", "joint", {"rationale": "OLD_OPINION_SENTINEL"}
    )
    payload = build_payload(cfg, markets, {"main": portfolio}, store, "paper", NOW)
    data = json.loads(payload)
    assert len(data["assets"]) == 3 and len(data["portfolios"]) == 1
    assert "OLD_OPINION_SENTINEL" not in payload
    assert "candles" not in payload
    assert "TRADING_MODE" not in payload
    assert "Do not infer missing values" in INSTRUCTIONS
    assert "Prefer HOLD" in INSTRUCTIONS
    assert "chain-of-thought" in INSTRUCTIONS
