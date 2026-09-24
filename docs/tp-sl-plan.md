# TP/SL implementation plan

Status: design only, reviewed 2026-09-24. The current service does not create take-profit/stop-loss
orders. Adding this document does not change trading behavior or authorize live testing.

The aim is exchange-held protection for multi-day spot positions, while keeping three-hour joint
model reviews and the existing 15-minute recovery job. No new daemon, minute polling, WebSocket
service, broker, or database is proposed. A slower recovery interval means slower detection/repair
of lost protection; exchange-held exits reduce dependence on the host but do not remove that gap.

## Smallest useful first version

- One managed position and one native SELL bracket per product/portfolio. Spot holdings only.
- Locally calculated, fixed TP and SL levels, with explicit configuration; the model continues to
  return trading intent only. No multiple profit targets, trailing stops or model-selected prices.
- Attach protection to a BUY entry if that exact combination is supported and verified. Do not
  silently turn the existing IOC entry into a resting GTC entry or open an unprotected position.
- Initially reject INCREASE while that position has active protection. HOLD preserves protection.
  DECREASE/EXIT must safely coordinate the bracket and the sale; they cannot sell reserved coins twice.
- Reuse SQLite, the process lock, execution adapters and maintenance command. Paper simulates the
  same lifecycle. Enable the feature only after its paper behavior and exchange contract are verified.

## Exchange contract to verify first

The Advanced Trade guide documents attached `trigger_bracket_gtc` under
`attached_order_configuration`, with size inherited from the parent. Standalone brackets support
GTC/GTD. This confirms the building blocks, not compatibility with every parent type or product.
[Coinbase order management](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/orders).

The installed `coinbase-advanced-py==1.8.4` exposes `create_order(..., order_configuration, **kwargs)`,
`preview_order(..., order_configuration, **kwargs)` and
`trigger_bracket_order_gtc(client_order_id, product_id, side, base_size, limit_price,
stop_trigger_price, ...)`. Inspect the SDK's serialization and add HTTP contract tests before using
attachment fields; a permissive `**kwargs` signature does not prove API support.

Before implementation can safely select an entry method, establish these facts for the configured
spot products and account permissions:

1. Whether `sor_limit_ioc` and/or `market_market_ioc` accept the desired attachment. The guide shows
   a GTC parent, so IOC attachment compatibility remains unverified. Use official API/SDK contracts
   and a non-trading preview where available; previews alone do not verify execution behavior.
2. How the child is identified, sized and activated after zero, partial and full parent fills;
   whether parent cancellation preserves protection for inventory already bought.
3. Trigger/reference-price rules, tick/minimum constraints, fees, holds, child status transitions,
   and how to discover the child if the parent response is lost.
4. How to distinguish a profit-side partial fill from a stop-triggered order in the returned state.
   Do not infer that the stop remains armed just because the order remains OPEN.

Order details expose parent/child relationship fields including `originating_order_id` and
`attached_order_id`. Capture and verify them rather than relying on a product-wide order search.
[Get Order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order).

Coinbase documents two essential limitations: spot downside protection uses a stop-limit order and
can fail to fill in a sharp move; a partial take-profit fill disables the stop trigger on the remaining
order. A resting remainder must therefore not automatically be labelled protected.
[Coinbase TP/SL behavior](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/order-types).

If atomic IOC attachment is unavailable, stop this rollout and document the concrete alternative
and its protection gap. A post-fill standalone bracket or a GTC parent would be a separate design
decision, not an automatic fallback after a failed/uncertain request. No funded test was performed
for this plan; real fills, regional access and settlement remain unverified.

## Required code and storage changes

| Component | Concrete change |
|---|---|
| `config.py`, examples | Add an explicit disabled-by-default protection configuration: fixed volatility multipliers, planned loss budget, repair limits and supported order type. Keep mode selection exclusively in `TRADING_MODE`. |
| `schemas.py` | Distinguish entry, discretionary exit and protective exit. Add a durable protection plan with parent/child IDs, intended quantity, remaining quantity, TP/SL prices, trigger state and verification time. Do not expand the LLM decision schema. |
| `risk.py` | Calculate Decimal levels and size locally from closed four-hour ATR, position/exposure limits and fees. Use the more restrictive size from loss-budget and existing risk limits. Quantize, revalidate and reject invalid or uneconomic protection. |
| `coinbase_client.py` | Implement only the verified bracket/attachment request. Parse whitelisted relationship/trigger fields, discover original children, and retain single-write/idempotent recovery behavior. |
| `storage.py` | Make an additive migration for plans and linked orders. The current unique `(cycle_id, product_id)` intent constraint cannot represent a parent plus children/replacements; preserve entry uniqueness while adding purpose/generation identity. Keep actual order IDs and client IDs unique and audit every transition. |
| `portfolio.py` | Recognize only verified strategy-owned protective orders and their reserved balances. Continue comparing actual totals to the fill ledger, value held inventory, and prevent reserved inventory from being sold again. Unknown orders/holds still fail closed. |
| `execution.py` | Add common protection operations to paper/live execution, separately from IOC recovery. Persist the parent intent and protection plan before submission; record exchange-generated child IDs after discovery without inventing child client IDs. Independent replacement orders require new durable client IDs. |
| `orchestrator.py`, CLI | Reconcile known protective orders before model use; include objective protection state in the payload. Healthy resting protection must not be confused with an uncertain IOC order. Reuse `maintain-orders` to reconcile active plans even after the parent entry is terminal. |

Sizing must account for the intended exit price, costs and the verified stop-limit buffer, not treat
the stop trigger as a guaranteed exit price or maximum possible loss. Revalidate planned levels
against actual entry fills. A missing/invalid ATR prevents a new protected entry; it must not delete
protection already held at Coinbase. Do not widen a stop merely to avoid recognizing a loss.

## Order lifecycle and recovery rules

Persist explicit states such as PLANNED, AWAITING_CHILD, PROTECTED, EXITING, DEGRADED, UNRESOLVED
and CLOSED. These are strategy states, separate from Coinbase's raw order statuses.

1. **Entry:** store the entry and protection plan atomically before POST. An entry acknowledgment
   proves neither a fill nor protection. Verify the parent fills and matching child for the executed
   quantity. Zero fills must not leave a child capable of selling unrelated inventory. Any uncertain
   outcome blocks new entries; discover the original relationship before considering another write.
2. **Protected holding:** verify the bracket identity, live status, remaining quantity and armed
   trigger. Expected reservations are allowed only when explained by that known order. Protective
   orders intentionally persist for days and must be exempt from `max_order_age_seconds`; keep the
   short IOC age rule for entry/discretionary orders.
3. **Any fill:** commit the new fill, realized PnL, ledger, expected exchange balances and remaining
   protection quantity atomically. Deduplicate by exchange fill ID. Re-read balances and order state;
   an observed trigger or cancellation acknowledgment is never proof of a completed exit.
4. **Partial TP or lost protection:** mark DEGRADED and block new entries. For the first version,
   cancel the old remainder, verify terminal state and all racing fills, then size at most one new
   bracket for the confirmed remaining inventory. Persist bounded repair attempts. If cancellation
   is uncertain, do not replace or submit an overlapping sell. If inventory is below exchange
   minimums, record unprotected dust explicitly and fail closed; never pretend it is covered.
5. **Model DECREASE/EXIT:** obtain confirmed exclusive use of the intended inventory before a sale.
   Cancel the bracket, verify its terminal state/fills and released hold, then re-read the position
   and recompute a sale no larger than the approved reduction. Re-protect a retained balance after
   confirmed fills. Failure leaves an explicit DEGRADED/UNRESOLVED state; there is no blind retry.
6. **Restart, mode switch or outage:** recover persisted parents/children and repair states, without
   OpenAI. A full close cancels/verifies any surviving protective remainder. Paper mode must never
   cancel live protection; it can read/reconcile it. Stopping the host must not cancel exchange-held
   protection merely because the service is shutting down.

Risk-reducing protective fills and verified repairs need a separate deterministic policy from new
entries: API-cost exhaustion, entry cooldowns and entry-count limits must not suppress existing
protection. They still require valid live mode, verified ownership, no short sale, bounded sizes and
reconciliation of uncertain writes. Never achieve this by disabling the common risk engine globally.

Keep the 15-minute maintenance interval for the first design. Lost protection may remain unnoticed
until the next check, longer during outages. Blocking new entries does not protect existing exposed
inventory. Record DEGRADED/UNRESOLVED status prominently in SQLite, console output and service exit
status; accept that detection window explicitly before enabling this feature. Changing to faster
polling or a stream would require a separate, evidence-based decision.

## Paper model and acceptance tests

Paper needs persisted reservations and outstanding protective orders, not just immediate simulated
fills. On recovery, replay only newly completed execution candles since its last checkpoint. Use
fine-grained candles for barrier detection; four-hour/daily ATR supplies context, not intrabar order.
If both barriers occur inside one candle and their order is unknown, use a documented conservative
stop-first assumption, include fees/slippage, and mark that ambiguity. Missing replay history fails
closed. Model stop-limit non-fills/gaps and partial TP deactivation; do not guarantee a simulated
exit merely because a candle crossed the trigger.

All API tests remain mocked and network-blocked. Required scenarios include:

- Parent rejection, zero fill, partial fill, missing child, wrong child/portfolio and held balances.
- Parent submission timeout, late child discovery and duplicate prevention across process restarts.
- Stop-limit trigger without a fill, gap through the limit, partial TP with disabled stop, and dust.
- Cancel/fill races, cancellation timeout, no replacement while uncertain, and bounded repair limits.
- Repeated recovery applying each fill once, partial PnL accounting and atomic crash recovery.
- Manual/unknown orders still rejected; no overselling when the model also requests DECREASE/EXIT.
- Protective orders surviving their entry's two-minute age threshold, host restart and API-budget cap.
- Mode changes, read-only doctor/paper maintenance, and equivalent paper/live planning rules.
- No future candles in paper replay, conservative ambiguous barriers and missing history rejection.

Roll out in order: verify the contract; implement durable lifecycle and reconciliation; implement
the paper simulator; pass failure/HTTP contract tests; observe paper operation including restart
recovery; update README/doctor/show-orders; only then consider explicit live activation. Do not
simultaneously change the three-hour model frequency or strategy indicators, so behavior changes
remain attributable. Actual account compatibility and the acceptable unprotected-inventory window
are the remaining rollout decisions, not details that mocks can establish.
