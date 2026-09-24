from types import SimpleNamespace

import httpx
import pytest
from conftest import NOW, PRODUCTS
from openai import APIConnectionError, OpenAI

from trader.costs import estimate_cost
from trader.errors import SafetyError
from trader.llm import DecisionClient
from trader.schemas import DecisionBatch
from trader.util import D, dumps


def test_one_structured_request_and_usage_costs(cfg, store, openai_mock):
    llm = DecisionClient(openai_mock, cfg.llm, store)
    batch = llm.decide('{"assets":[]}', set(PRODUCTS), "cycle", "paper")
    assert len(batch.decisions) == 3
    call = openai_mock.responses.create.call_args.kwargs
    assert call["model"] == "gpt-6-luna"
    assert call["reasoning"] == {"effort": "medium"}
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in call and "previous_response_id" not in call
    assert call["store"] is False
    assert openai_mock.responses.create.call_count == 1
    usage = store.rows("api_usage")[0]
    assert usage["input_tokens"] == 2000
    assert usage["cached_input_tokens"] == 1000
    assert usage["reasoning_tokens"] == 100
    assert usage["output_tokens"] == 300
    assert D(usage["estimated_usd"]) == D("0.000285")


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("refusal", "OPENAI_REFUSAL"),
        ("incomplete", "OPENAI_RESPONSE_INCOMPLETE"),
        ("json", "OPENAI_DECISION_INVALID"),
        ("missing_asset", "OPENAI_DECISION_INVALID"),
        ("usage", "OPENAI_USAGE_MISSING"),
    ],
)
def test_refusals_incomplete_and_schema_errors_hold(cfg, store, openai_mock, damage, reason):
    response = openai_mock.responses.create.return_value
    if damage == "refusal":
        response.output = [
            SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])
        ]
    elif damage == "incomplete":
        response.status = "incomplete"
    elif damage == "json":
        response.output_text = "garbage"
    elif damage == "usage":
        response.usage = None
    else:
        batch = DecisionBatch.model_validate_json(response.output_text)
        response.output_text = dumps(batch.model_copy(update={"decisions": batch.decisions[:1]}))
    with pytest.raises(SafetyError, match=reason):
        DecisionClient(openai_mock, cfg.llm, store).decide("{}", set(PRODUCTS), "cycle", "paper")
    assert openai_mock.responses.create.call_count == 1
    assert D(store.rows("api_usage")[0]["estimated_usd"]) > 0


def test_transient_retries_are_bounded_and_accounted(cfg, store, openai_mock, monkeypatch):
    monkeypatch.setattr("trader.llm.time.sleep", lambda _: None)
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    openai_mock.responses.create.side_effect = APIConnectionError(request=request)
    with pytest.raises(SafetyError, match="OPENAI_REQUEST_FAILED"):
        DecisionClient(openai_mock, cfg.llm, store).decide("{}", set(PRODUCTS), "cycle", "paper")
    assert openai_mock.responses.create.call_count == cfg.llm.attempts
    usage = store.rows("api_usage")
    assert len(usage) == cfg.llm.attempts
    assert all(r["status"] == "ERROR_UNKNOWN_COST" for r in usage)
    assert all(D(r["estimated_usd"]) > 0 for r in usage)


def test_budget_rechecked_before_retry(cfg, store, openai_mock, monkeypatch):
    from trader.costs import reservation
    from trader.prompt import INSTRUCTIONS

    monkeypatch.setattr("trader.llm.time.sleep", lambda _: None)
    amount = reservation(
        len((INSTRUCTIONS + "{}" + dumps(DecisionBatch.model_json_schema())).encode()), cfg.llm
    )
    cfg.llm.daily_budget_usd = amount * D("1.5")
    openai_mock.responses.create.side_effect = APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )
    with pytest.raises(SafetyError, match="API_BUDGET_EXCEEDED"):
        DecisionClient(openai_mock, cfg.llm, store).decide("{}", set(PRODUCTS), "cycle", "paper")
    assert openai_mock.responses.create.call_count == 1


@pytest.mark.parametrize("limit", ["daily_budget_usd", "monthly_budget_usd"])
def test_preflight_budget_skips_api(cfg, store, openai_mock, limit):
    setattr(cfg.llm, limit, D("0.000001"))
    with pytest.raises(SafetyError, match="API_BUDGET_EXCEEDED"):
        DecisionClient(openai_mock, cfg.llm, store).decide("{}", set(PRODUCTS), "cycle", "paper")
    openai_mock.responses.create.assert_not_called()


def test_sdk_http_contract_uses_responses_strict_schema(cfg, store, decision):
    batch = DecisionBatch(market_regime="mixed", decisions=[decision])
    seen = []

    def handle(request):
        import json

        seen.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": int(NOW.timestamp()),
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "model": "gpt-6-luna",
                "parallel_tool_calls": False,
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": dumps(batch), "annotations": []}
                        ],
                    }
                ],
                "tools": [],
                "tool_choice": "none",
                "metadata": {},
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 80,
                    "total_tokens": 180,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 40},
                },
            },
        )

    client = OpenAI(
        api_key="unit-test-unused",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    result = DecisionClient(client, cfg.llm, store).decide("{}", {"BTC-USDC"}, "cycle", "paper")
    assert result == batch
    assert len(seen) == 1 and seen[0]["text"]["format"]["strict"] is True
    client.close()


def test_reasoning_not_double_charged(cfg):
    base = {
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "reasoning_tokens": 0,
    }
    assert estimate_cost(base, cfg.llm.pricing) == estimate_cost(
        base | {"reasoning_tokens": 90}, cfg.llm.pricing
    )


def test_crashed_request_reservation_survives_restart(store, cfg):
    store.reserve_request("pending", "cycle", cfg.llm, D("0.1"), NOW)
    assert store.spending(NOW) == (D("0.1"), D("0.1"))
    assert store.rows("api_usage")[0]["status"] == "RESERVED"
