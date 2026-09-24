# Integration verification notes

Reviewed 2026-09-24. Installed and inspected `openai==2.54.0` and
`coinbase-advanced-py==1.8.4`; the Coinbase dependency is pinned because recovery and execution depend
on its exact request/response behavior. This is source/documentation verification and mocked contract
testing, not authenticated exchange certification.

## OpenAI

- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): Responses
  supports strict JSON schema and Python Pydantic models. We use `responses.create(text={format: ...})`
  with Pydantic's generated schema, then validate the output locally. This permits usage recording
  before handling refusals, incomplete responses, or validation failures.
- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna): requested model retained,
  medium reasoning configurable; prices are copied into explicit configuration and logged per attempt.
- SDK `responses.create` parameters inspected locally: `instructions`, `input`, `reasoning`,
  `text`, `max_output_tokens`, `store`, `metadata`, `service_tier`, `truncation`.
- SDK retries disabled. Network/429/5xx retries occur only through the audited budget-reserving layer.
  Unknown-cost requests retain their reservation. No schema-format retries or follow-up conversation.
- `doctor` retrieves model metadata to check connectivity/access; it cannot certify successful
  generation, Structured Outputs support, quota, or effort support for an arbitrary configured model.

## Coinbase Advanced Trade

| Official source | Adapter contract |
|---|---|
| [SDK reference](https://coinbase.github.io/coinbase-advanced-py/coinbase.rest.html) | `RESTClient`, bounded timeout, no automatic write retry in inspected SDK |
| [Authentication](https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication) | ECDSA/ES256 key; SDK signs JWTs; private key retained only in memory/environment |
| [Key permissions](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions) | Verify `can_view`, `can_trade` for live, no `can_transfer`, exact `portfolio_uuid` |
| [Portfolios](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/portfolios) | Dedicated CDP key scoped to each actual portfolio |
| [Accounts](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts) | Complete cursor pagination, `available_balance` + `hold`, account portfolio identity |
| [Products](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product) | Exact spot/base/USDC identity; trade flags; base, quote and price increments and min/max sizes |
| [Best bid/ask](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-best-bid-ask) | Exact product, two-sided book and exchange timestamp required |
| [Candles](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-candles) | Epoch start/end, official granularity enum including FOUR_HOUR and ONE_DAY, at most 350 bars/request |
| [Create order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order) | Unique persisted client ID; duplicate ID returns existing order; CDP key determines portfolio |
| [Get order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order) | Match identity, status, settled flag, number of fills, filled size/value and fees; inspect pending cancellation |
| [Cancel orders](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/cancel-order) | `cancel_orders(order_ids=[owned_id])`, POST `/orders/batch_cancel`; per-order success acknowledges initiation only |
| [Fee summary](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/fees/get-transaction-summary) | `get_transaction_summary(product_type="SPOT")`; configured allowance must cover `fee_tier.taker_fee_rate`; special/unknown commission schedules fail closed |
| [List orders](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-orders) | Paginate active orders; discover ambiguous submissions by client ID in history |
| [Fills](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills) | Order-filtered pagination, entry IDs, price, size, `size_in_quote`, commission and trade timestamp |

The official SDK's `limit_order_ioc` constructs `sor_limit_ioc` with `base_size` and `limit_price`.
Market buy uses quote size; market sell uses base size. Neither call includes portfolio transfers,
leverage, margin, or derivative fields. Product aliases are not silently converted into a different
quote currency.

IOC applies to buys and sells; the exchange cancels the unfilled remainder immediately.
[Official order types](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/order-types).
The default 15-second SDK HTTP timeout is independent of time-in-force. The 120-second local order
age backstop permits cancellation of a verified owned order still open. The separate recovery timer
checks every 15 minutes after completion, so cancellation may wait until that check; it cannot
promise exchange release by a fixed wall-clock deadline. SDK cancellation and fee
summary signatures were inspected locally; cancellation's HTTP payload is tested using the official
SDK with a mocked transport.

The five configured candle intervals use the existing `get_candles(product_id, start, end,
granularity, limit)` SDK method. Four-hour/daily signatures and accepted API granularities were
rechecked; their serialized GET requests are tested using the official SDK with a mocked transport.
Only completed UTC buckets enter features; quotes and accounts have independent freshness checks.
No native TP/SL order is submitted by this version; see the [implementation plan](tp-sl-plan.md).

Fills can use quote-denominated size, which is converted to base with Decimal price. Cursor-only fill
pagination is followed until exhaustion, with repeated-cursor/page caps failing closed. Coinbase
documents unstable fill pagination; inconsistent totals remain unresolved until a later consistent
read. `proof_token_required` blocks trading instead of treating inaccessible history as empty.

No automatic resubmission policy is enabled: even an empty history query cannot establish that an
ambiguous POST was never received. Recovery preserves the original client ID and never creates an
order. Only live trading/maintenance can request cancellation, with identity/portfolio verification,
durable attempt recording, cooldown, bounded attempts, and fresh state verification before any retry.
`doctor`, `reconcile` and paper maintenance only read Coinbase. Cancellation ACKs are not finality:
fills may race cancellation, and holds must clear before reconciliation succeeds. An entirely unfilled
terminal order may have `settled=false`; exact zero totals/count, no fills, matching balances and no
holds/open orders are all required. Any executed amount still requires `settled=true`.

## Deliberate initial limits

- USDC-quoted spot portfolios only. No fiat FX, implicit USD/USDC parity conversion, margin, transfers,
  or valuation of unconfigured holdings.
- Coinbase account fields, portfolio mapping, book timestamps and order/fill totals are required.
  Regional aliases, key access, SCA, fee currency/tier and settlement latency need real account checks.
- Fees are assumed to be in quote currency, as represented by order/fill commission totals. A balance
  mismatch, unexpected fee accounting, negative available cash or changed fill data blocks trading.
- The spot tier is fetched on each account refresh, before model use and before execution in both
  modes. A missing cost-plus flag, cost-plus commissions, or nonzero GST blocks new trading rather
  than assuming an unverified all-in fee formula. This gate does not prevent owned-order maintenance.
- Paper fees/slippage and fixed partial-fill fractions are transparent scenarios, not depth/queue models.
- Six-day maximum gap between reconciliation observations bounds the seven-day history window.
  There is no automatic rebasing tool for external balance changes or an unknown order outcome.
- No paid OpenAI request, real Coinbase account call, or live order was made during development.
  The Linux service definition is supplied for host validation; the development host is macOS.
