# Proposed maker execution for future experiments

Status: design only. Nothing in this document enables maker orders. The current
execution modes remain limit IOC and market IOC, with taker-fee allowances. The
VM upgrade templates keep limit IOC. Implementing this proposal requires an
explicit future change to the project's IOC-only rule and execution lifecycle.

## Objective and scope

Evaluate whether lower actual commissions improve net results after allowing for
missed fills, delayed execution, partial fills and adverse selection. A lower fee
alone does not establish an improvement. Keep spot trading, existing risk checks,
portfolio isolation, durable order intents and fail-closed reconciliation.

The model should still choose direction and a bounded allocation, not order
prices, raw quantities, fee rates or API operations. A deterministic execution
policy decides whether an approved intent uses the existing IOC path or a future
maker path. Do not ask the model to declare a trade a maker trade.

Coinbase charges according to liquidity provision and the applicable fee tier.
An immediately matched limit order can be a taker order; merely changing the
configured fee rate or order label cannot obtain maker pricing. The maker/taker
difference is tier- and product-dependent, not a guaranteed 50% discount.
[Coinbase Advanced fees](https://help.coinbase.com/en-gb/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees)

## Initial policy: post-only limit with exchange expiry

Prefer an opt-in post-only, good-until-date limit order. Coinbase's order schema
provides `limit_limit_gtd`, `post_only`, `limit_price`, sizes and `end_time`;
`limit_limit_gtc` is also available. Confirm supported combinations for the
selected spot products and installed SDK before implementing. Send exactly one
order configuration in each request.
[Create Order](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/create-order)

Proposed first version:

- IOC remains the default. Maker execution requires an explicit per-portfolio
  policy and a separate experiment portfolio.
- Use base quantity rounded down to exchange increments. Buy at the current best
  bid, rounding price down; sell at the best ask, rounding price up. Refuse stale,
  crossed or invalid books. Re-run existing spread, price-movement, balance,
  exposure and decision-age checks immediately before preparing the intent.
- Set `post_only=true`. If a quote race would make the order take liquidity, accept
  rejection as an unfilled attempt. Never silently retry without post-only or
  fall back to IOC. Verify actual exchange behavior and error codes in testing.
- Start with a short, configurable exchange lifetime, for example five minutes.
  This is a proposed experiment parameter, not a promise of optimal execution.
  Bound it below the decision cadence and size it separately from HTTP timeout.
- No automatic repricing, edit, cancel-and-replace or taker fallback in version 1.
  A later scheduled decision can form a new intent only after the previous one
  is conclusively resolved.
- Prefer GTD over GTC so order expiry does not depend on a healthy VM. Expiry is
  still not proof of finality: verify status, racing fills and released holds.

Preview support, if added, is validation only. A preview is neither a reservation
nor proof that a later submission will be accepted or filled.

## Fees and sizing

Keep separate maker and taker allowances, rather than renaming
`execution.taker_fee_rate`. The transaction summary exposes both rates. Validate
the selected allowance against a fresh exchange tier before sizing in both paper
and live modes, retaining the existing rejection of unsupported tax/cost-plus
conditions. Record the observed tier and allowance on the intent. Actual live
commissions come from fills, not this estimate.
[Get Transaction Summary](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/fees/get-transaction-summary)

For a buy with base quantity `q`, limit `p` and nonnegative fee allowance `f`,
reserve at least `q * p * (1 + f)`, subject to the exchange's actual reservation
semantics. Round quantities down and retain the existing minimum notional and
cash-buffer rules. Reserve sell quantity until fills or verified cancellation
release it. The same funds must never back a second order.

Do not assume that the fee at submission stays valid until the last fill. A
fee-tier change must not cause accounting to discard a real fill: book actual
fees atomically, then freeze further trading if the allowance or balances were
violated. Design any automatic fee refresh as a separate explicit policy with a
maximum accepted rate and an audit trail. Keep a fixed conservative allowance as
the initial behavior.

Initial maker support should reject fee structures the current ledger cannot
represent, including negative fees/rebates if its money schema disallows them.
Supporting rebates requires an explicit signed-fee/accounting design and tests;
never clamp a rebate or unexpected commission to zero.

## Durable order lifecycle: the substantial change

Current IOC execution expects prompt finality. Resting orders introduce ordinary
pending states and exchange-held balances. Existing checks must not be bypassed
by accepting every open order or ignoring held funds.

Persist before submission:

- mode, strategy, portfolio, product, cycle, decision and unique client order ID;
- execution-policy version, order type, post-only requirement and expiry;
- side, base quantity, limit price, expected maximum debit and reservations;
- quote timestamp, decision timestamp, fee assumption and applicable risk limits.

Conceptually distinguish `prepared`, `submission_uncertain`, `verified_open`,
`partially_filled`, `cancel_pending` and a verified terminal outcome. Map actual
Coinbase statuses into these states without guessing unsupported enumerations.
Do not conflate an HTTP timeout, an elapsed expiry, a cancel acknowledgement and
a confirmed terminal order.

There must be one durable intent and at most one initial POST. Timeouts or lost
responses enter reconciliation; they never trigger a second order. Discovery
must match client ID, exchange order ID, portfolio, product, side, configuration,
size and price. An empty lookup is not proof that submission did not happen.

Each new fill is deduplicated and applied atomically with fees, inventory, cash,
cost basis, trade counters and the corresponding audit record. Partial fills
update cooldowns according to the existing actual-fill semantics. Unfilled
quantity releases its reservation only after exchange confirmation. Exposure
checks must include remaining reserved buy notional, and sell availability must
exclude reserved inventory. A submitted intent itself never counts as a fill.

Do not lose already-accounted fills if a later response is malformed. Preserve
the evidence, flag the unresolved discrepancy and block additional trading.

## Locking, isolation and maintenance

Keep the shared SQLite database and process lock. Do not hold the process lock
while sleeping for the entire lifetime of a resting order. Persist its state,
exit, and resume verification from maintenance or the next strategy cycle.

For an initial implementation, retain the existing global unresolved-order gate.
A verified resting order may therefore block new trades in other strategies
until finality. That is an explicit limitation for parallel experiments, not a
reason to weaken reconciliation. Log which portfolio/order caused the block and
measure skipped decisions.

A later relaxation could allow unrelated portfolios to continue only when the
pending order is fully identified, reservations are accounted for, and no
submission/accounting uncertainty exists. That requires a separate proof of
portfolio isolation and additional tests. Unknown orders, unidentified holds and
ambiguous submissions must continue to block trading globally.

Keep the existing 15-minute maintenance cadence initially. Exchange-side GTD
limits the resting lifetime even when the VM is down; maintenance can verify
finality later. This trades prompt accounting for simple operation and can skip
strategy runs. Minute polling, a WebSocket consumer or a daemon would be a
separate operational change requiring approval, not an incidental addition.

Maker orders need a deadline based on their recorded expiry plus a bounded
verification grace. The current IOC `max_order_age_seconds: 120` cannot blindly
be reused for a five-minute resting order. Cancellation remains restricted to
durable, owned, identity-checked live orders, with durable bounded attempts and
subsequent fill/hold verification. Doctor/reconcile stay read-only at Coinbase;
paper maintenance must never cancel an exchange order.

## Paper simulation and useful comparison

The current paper IOC model applies a deterministic adverse price adjustment
and a configured fill fraction. Reusing `paper_fill_fraction: 1` for a resting
maker order would overstate execution quality.

A future simulator needs order creation/expiry timestamps and market events
after placement. A candle touching the limit is not proof of execution: queue
position, available volume and event ordering are unknown. Candle lows/highs
alone cannot establish whether a fill occurred before an expiry or cancellation.
Never use future candles to fill an order at its creation time.

Start with explicitly labelled conservative and optimistic fill assumptions;
report their range rather than claiming an accurate maker backtest. A stronger
simulation requires timestamped trades/order-book observations, bounded queue
assumptions, partial fills and expiry. Do not introduce that data collection
silently as part of changing an order type. Paper/live should retain the same
decision, risk and refresh pipeline; simulated account effects belong in the
paper executor.

For the eventual experiment, use distinct Coinbase portfolios, the same shared
reference decision, matched starting capital and the same risk settings/cadence.
Vary only the execution policy. Account for the global pending-order gate: maker
orders that suppress IOC comparison cycles invalidate a naive independent A/B
claim. Resolve that limitation explicitly before treating results as causal.

Compare net marked-to-market equity and drawdown, actual commissions, fill ratio,
time to first/final fill, missed opportunities, partial fills, cancel/reject rates,
price movement after fills, and skipped cycles. Include all eligible intents in
the denominator; comparing only completed maker fills creates selection bias.
The existing post-decision outcomes are useful context, but are not a substitute
for realized execution costs or portfolio P&L.

## Observability and model context

Every event must retain mode, strategy, portfolio, product, cycle and decision
identity. Add policy/version, intended liquidity role, reservation changes,
verified order state, expiry, fill/cancel timestamps, actual fees and a stable
reason code for rejects, skips and uncertainty. Preserve the distinction between
risk reduction, exchange rounding, partial fill and no fill.

Coinbase fill records expose commission and `liquidity_indicator`. Normalize
documented values, explicitly retain unknown values, and verify portfolio/order
identity across paginated fill retrieval. Do not infer maker status just from
the requested order type. Unexpected taker evidence on a post-only intent must
be accounted for and flagged for investigation.
[List Fills](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills)

The prompt may receive an execution-policy summary and the applicable estimated
costs, including the possibility of no fill. It must not treat a pending order
as inventory, or assume that a recommendation was executed. Keep the mid-term
horizon guidance and local authority over risk and execution.

## Implementation sequence and acceptance tests

1. Add opt-in policy validation, versioned storage, reservations and audit fields;
   keep IOC behavior unchanged and migrate without losing history.
2. Add a post-only GTD adapter after checking current official API docs and
   installed SDK signatures. Add no fallback route.
3. Extend recovery, atomic partial-fill accounting, owned cancellation and expiry
   verification. Keep normal pending state distinct from uncertainty.
4. Add explicitly bounded paper fill models, then wire the existing decision/risk
   pipeline through the selected executor policy.
5. Document experiment limitations, perform mocked regression tests, and require
   a deliberate later live rollout. No real orders in automated tests.

Required cases include quote races; rejected post-only orders; SDK/HTTP timeouts;
restart before/after submission; empty or paginated discovery; external orders;
wrong portfolio/product/side; stale data; price/size rounding; fee changes;
insufficient funds with existing reservations; partial fills across restarts;
duplicate fills; expiry/cancellation racing a fill; failed cancellations;
unreleased holds; unsupported commissions; and actual liquidity inconsistent
with intent. Verify strategy locking, mode separation, budget-independent
maintenance, read-only paper/doctor behavior and unchanged IOC regressions.

Success means demonstrably correct accounting and bounded behavior under these
failures, followed by useful paper results. It does not mean guaranteeing fills,
profitability or a particular fee saving.
