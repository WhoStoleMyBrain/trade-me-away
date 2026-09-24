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
| [Candles](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product-candles) | Epoch start/end, official granularity enum, at most 350 bars/request |
| [Create order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order) | Unique persisted client ID; duplicate ID returns existing order; CDP key determines portfolio |
| [Get order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order) | Match identity, status, settled flag, filled size/value and fees |
| [List orders](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-orders) | Paginate active orders; discover ambiguous submissions by client ID in history |
| [Fills](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills) | Order-filtered pagination, entry IDs, price, size, `size_in_quote`, commission and trade timestamp |

The official SDK's `limit_order_ioc` constructs `sor_limit_ioc` with `base_size` and `limit_price`.
Market buy uses quote size; market sell uses base size. Neither call includes portfolio transfers,
leverage, margin, or derivative fields. Product aliases are not silently converted into a different
quote currency.

Fills can use quote-denominated size, which is converted to base with Decimal price. Cursor-only fill
pagination is followed until exhaustion, with repeated-cursor/page caps failing closed. Coinbase
documents unstable fill pagination; inconsistent totals remain unresolved until a later consistent
read. `proof_token_required` blocks trading instead of treating inaccessible history as empty.

No automatic resubmission policy is enabled: even an empty history query cannot establish that an
ambiguous POST was never received. Recovery is read-only and preserves the original client ID.
Unexpected state requires investigation, never an alternative order.

## Deliberate initial limits

- USDC-quoted spot portfolios only. No fiat FX, implicit USD/USDC parity conversion, margin, transfers,
  or valuation of unconfigured holdings.
- Coinbase account fields, portfolio mapping, book timestamps and order/fill totals are required.
  Regional aliases, key access, SCA, fee currency/tier and settlement latency need real account checks.
- Fees are assumed to be in quote currency, as represented by order/fill commission totals. A balance
  mismatch, unexpected fee accounting, negative available cash or changed fill data blocks trading.
- Paper fees/slippage and fixed partial-fill fractions are transparent scenarios, not depth/queue models.
- Six-day maximum gap between reconciliation observations bounds the seven-day history window.
  There is no automatic rebasing tool for external balance changes or an unknown order outcome.
- No paid OpenAI request, real Coinbase account call, or live order was made during development.
  The Linux service definition is supplied for host validation; the development host is macOS.
