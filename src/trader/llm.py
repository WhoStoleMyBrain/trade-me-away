from __future__ import annotations

import time
from uuid import uuid4

from openai import APIConnectionError, APIStatusError, OpenAI

from trader.config import LLMConfig
from trader.costs import estimate_cost, reservation
from trader.errors import SafetyError
from trader.prompt import INSTRUCTIONS
from trader.schemas import DecisionBatch
from trader.storage import Storage
from trader.util import dumps, event, utcnow


class DecisionClient:
    def __init__(self, client: OpenAI, cfg: LLMConfig, store: Storage):
        # Caller constructs official SDK with max_retries=0: attempts are audited here.
        self.client, self.cfg, self.store = client, cfg, store

    def doctor(self) -> None:
        # Read-only model access check; no chargeable generation and no trade intent.
        for attempt in range(self.cfg.attempts):
            try:
                self.client.models.retrieve(self.cfg.model)
                return
            except Exception as exc:
                transient = isinstance(exc, APIConnectionError) or (
                    isinstance(exc, APIStatusError)
                    and (exc.status_code == 429 or exc.status_code >= 500)
                )
                if not transient or attempt + 1 == self.cfg.attempts:
                    raise SafetyError("OPENAI_CONNECTIVITY_OR_MODEL_ACCESS_FAILED") from None
                time.sleep(0.5 * 2**attempt)

    def decide(self, payload: str, products: set[str], cycle: str, mode: str) -> DecisionBatch:
        schema = DecisionBatch.model_json_schema()
        amount = reservation(len((INSTRUCTIONS + payload + dumps(schema)).encode()), self.cfg)
        self.store.audit(
            "model_requests",
            cycle,
            mode,
            "joint",
            {
                "instructions": INSTRUCTIONS,
                "payload": payload,
                "schema": schema,
                "model": self.cfg.model,
                "reasoning_effort": self.cfg.reasoning_effort,
            },
        )
        for attempt in range(self.cfg.attempts):
            request_id = str(uuid4())
            self.store.reserve_request(request_id, cycle, self.cfg, amount, utcnow())
            start = time.monotonic()
            try:
                # Record usage before parsing, including refusals and incomplete responses.
                response = self.client.responses.create(
                    model=self.cfg.model,
                    reasoning={"effort": self.cfg.reasoning_effort},
                    instructions=INSTRUCTIONS,
                    input=payload,
                    store=False,
                    max_output_tokens=self.cfg.max_output_tokens,
                    service_tier="default",
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "trading_decisions",
                            "strict": True,
                            "schema": schema,
                        }
                    },
                    metadata={"cycle_id": cycle, "request_id": request_id},
                    truncation="disabled",
                )
            except Exception as exc:
                retryable = isinstance(exc, APIConnectionError) or (
                    isinstance(exc, APIStatusError)
                    and (exc.status_code == 429 or exc.status_code >= 500)
                )
                self.store.finish_request(
                    request_id,
                    status="ERROR_UNKNOWN_COST",
                    latency_ms=int((time.monotonic() - start) * 1000),
                )
                event("openai_error", cycle, mode, request_id=request_id, retryable=retryable)
                # Preserve the full reservation if the server may have processed the call.
                if retryable and attempt + 1 < self.cfg.attempts:
                    time.sleep(0.5 * 2**attempt)
                    continue
                raise SafetyError("OPENAI_REQUEST_FAILED") from None
            latency = int((time.monotonic() - start) * 1000)
            usage = None
            cost = None
            if response.usage is not None:
                u = response.usage
                usage = {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cached_input_tokens": getattr(u.input_tokens_details, "cached_tokens", 0),
                    "reasoning_tokens": getattr(u.output_tokens_details, "reasoning_tokens", 0),
                }
                cost = estimate_cost(usage, self.cfg.pricing)
            self.store.finish_request(
                request_id,
                status=response.status,
                latency_ms=latency,
                response_id=response.id,
                usage=usage,
                cost=cost,
            )
            event(
                "openai_usage",
                cycle,
                mode,
                request_id=request_id,
                usage=usage,
                estimated_usd=cost,
                status=response.status,
            )
            if usage is None:
                raise SafetyError("OPENAI_USAGE_MISSING")
            refused = any(
                getattr(content, "type", None) == "refusal"
                for item in response.output
                if getattr(item, "type", None) == "message"
                for content in item.content
            )
            if refused or response.status != "completed":
                raise SafetyError("OPENAI_REFUSAL" if refused else "OPENAI_RESPONSE_INCOMPLETE")
            try:
                batch = DecisionBatch.model_validate_json(response.output_text, strict=True)
                batch.validate_products(products)
            except Exception:
                self.store.finish_request(
                    request_id,
                    status="INVALID_DECISION",
                    latency_ms=latency,
                    response_id=response.id,
                )
                raise SafetyError("OPENAI_DECISION_INVALID") from None
            self.store.audit("model_decisions", cycle, mode, "joint", batch)
            return batch
        raise SafetyError("OPENAI_REQUEST_FAILED")
