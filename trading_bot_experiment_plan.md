# Trading Bot Experiment Plan

## Purpose

This document collects the main ideas for evolving the current Coinbase + OpenAI trading bot after the initial two-week baseline test.

The current system is intentionally simple:

- Coinbase Advanced Trade for market/account data and execution
- local Python feature calculation
- one joint OpenAI request across enabled assets
- deterministic local risk controls
- SQLite audit/history
- systemd scheduling
- `TRADING_MODE=paper|live`
- current regular decision cadence: every 3 hours

The current implementation should remain unchanged during the initial baseline period unless there is an operational bug. The point of the baseline is to create a clean reference against which later changes can be compared.

---

# 1. Current baseline

The initial live-shaped paper trader currently evaluates multiple assets jointly.

Current assets include:

- BTC-USDC
- ETH-USDC
- SOL-USDC
- DOGE-USDC
- SUI-USDC

The model receives locally computed market features and current portfolio state, then returns a structured decision such as:

- `INCREASE`
- `DECREASE`
- `HOLD`
- `EXIT`

along with fields such as:

- target exposure
- confidence
- expected horizon
- data quality
- rationale

The model does not directly choose executable order quantities.

The local system performs:

1. market-data retrieval
2. feature calculation
3. portfolio reconciliation
4. OpenAI decision request
5. schema validation
6. deterministic risk checks
7. conversion from target exposure to executable order
8. paper/live execution
9. fill verification
10. state reconciliation
11. persistent logging

This separation should be preserved.

---

# 2. Important logging improvement

For every model decision, explicitly preserve three distinct values:

1. **model requested exposure**
2. **risk-approved exposure**
3. **actual executed exposure**

For example:

```json
{
  "product_id": "BTC-USDC",
  "model_target_exposure": 0.08,
  "risk_approved_target_exposure": 0.08,
  "executed_exposure": 0.0799,
  "requested_notional": 80.00,
  "approved_notional": 80.00,
  "executed_notional": 79.92
}
```

Small execution rounding should ideally be distinguished from genuine risk intervention.

Prefer explicit reasons such as:

- `MAX_ASSET_EXPOSURE`
- `MAX_PORTFOLIO_EXPOSURE`
- `MAX_ORDER_NOTIONAL`
- `MIN_CONFIDENCE`
- `DAILY_LOSS_LIMIT`
- `EXECUTION_ROUNDING`

rather than one broad `SIZE_OR_EXPOSURE_CLAMPED` reason where possible.

This becomes important when evaluating the model independently from the risk engine.

---

# 3. Per-asset risk settings

The current risk parameters are global. That is convenient, but BTC, ETH, SOL, DOGE, and SUI have materially different:

- volatility
- liquidity
- spread
- typical drawdown
- market depth
- price-jump behavior

A future improvement is therefore to support per-asset risk profiles.

Example:

```yaml
risk_profiles:
  BTC-USDC:
    max_asset_exposure: 0.25
    max_order_notional: 150

  ETH-USDC:
    max_asset_exposure: 0.20
    max_order_notional: 125

  SOL-USDC:
    max_asset_exposure: 0.15
    max_order_notional: 100

  DOGE-USDC:
    max_asset_exposure: 0.10
    max_order_notional: 75

  SUI-USDC:
    max_asset_exposure: 0.10
    max_order_notional: 75
```

These values are only illustrative. They should be chosen experimentally rather than by intuition alone.

---

# 4. Risk-policy A/B testing

A particularly useful future experiment is to run the same asset under multiple deterministic risk policies.

Example:

```text
BTC-A:
max exposure 10%

BTC-B:
max exposure 20%

BTC-C:
max exposure 30%
```

All variants should receive the **same model decision**.

Do not call the model independently for each risk variant if the purpose is to test risk policy.

Instead:

```text
market snapshot
      ↓
one LLM decision
      ↓
same decision replayed into:
      ├── risk profile A
      ├── risk profile B
      └── risk profile C
```

This isolates the effect of the risk layer.

Useful metrics include:

- net return after fees
- maximum drawdown
- Sharpe ratio
- Sortino ratio
- turnover
- fees paid
- number of trades
- average holding time
- time in market
- worst trade
- realized volatility
- return / drawdown ratio

The current two-week run can serve as `Risk Policy v1`.

Do not change the baseline parameters halfway through the baseline unless needed to fix a bug.

---

# 5. Decision frequency experiments

The current 3-hour cadence may react too slowly to meaningful market moves.

Because OpenAI API cost has been very low, testing higher decision frequencies is economically feasible.

Candidate variants:

```text
A: every 3 hours
B: every 1 hour
C: every 30 minutes
```

The working hypothesis is:

### 3-hour cadence

Advantages:

- stable
- less sensitive to noise
- low turnover
- low chance of reacting to small reversals

Disadvantages:

- may enter trends late
- may react slowly to reversals
- may miss part of intraday moves

### 1-hour cadence

Likely the strongest next candidate.

Advantages:

- materially faster reaction
- still aligned with 12-24 hour trading horizons
- should remain reasonably resistant to very short-term noise

Disadvantages:

- greater chance of unnecessary position adjustments
- more model calls
- potentially more turnover

### 30-minute cadence

Worth testing, but not assumed to be better.

Advantages:

- fast reaction to regime changes
- more opportunities to respond near intraday turning points

Disadvantages:

- substantially more short-term noise
- greater risk of churn
- potential fee drag
- model may repeatedly reconsider essentially the same medium-term trade thesis

The objective is **not** to catch the exact daily low and sell the exact daily high. Those extrema are only obvious with hindsight.

The objective is to determine whether more frequent evaluation captures a larger useful portion of meaningful market moves after fees and slippage.

---

# 6. Frequency A/B testing

Do not replace the existing 3-hour baseline immediately.

Instead, run independent paper strategies:

```text
Strategy A: 3h
Strategy B: 1h
Strategy C: 30m
```

Use:

- same assets
- same model
- same prompt
- same feature definitions
- same risk parameters
- same fee assumptions
- same starting capital

Each strategy must have its **own independent paper portfolio and database/state namespace**.

The 1h strategy must not change the state observed by the 3h strategy.

For example:

```text
BTC / 3h portfolio
BTC / 1h portfolio
BTC / 30m portfolio
```

Shared market data and feature calculation are acceptable, but simulated portfolio state must remain independent.

Compare over at least several weeks, preferably longer.

Important metrics:

- net return
- drawdown
- turnover
- total fees
- trades per day
- average holding period
- percentage of profitable trades
- risk-adjusted return
- percentage of model decisions resulting in execution
- opportunity captured after entry
- opportunity missed by HOLD decisions

---

# 7. Post-decision outcome tracking

For every model decision, automatically record future price performance.

Suggested horizons:

- +1h
- +3h
- +6h
- +12h
- +24h

For each decision store values such as:

```text
price_at_decision
return_1h
return_3h
return_6h
return_12h
return_24h
max_favorable_excursion_24h
max_adverse_excursion_24h
```

This allows questions such as:

- When the model said HOLD, how much opportunity was actually missed?
- When it entered, did price generally move favorably afterward?
- Was the model early or late?
- Would a 1h cadence have entered materially earlier than the 3h system?
- Are high-confidence decisions actually better?
- Is the model excessively conservative?

This is much more informative than simply counting trades.

---

# 8. Event-triggered decision calls

A useful alternative to blindly increasing frequency is:

```text
regular model call every 1 hour
+
extra call when something meaningful changes
```

Possible deterministic triggers:

- price move exceeds X% since last model decision
- volatility spike
- unusual volume spike
- spread widens sharply
- major trend indicator changes sign
- significant drawdown from recent high
- strong cross-asset divergence

Example:

```text
13:05 normal call
14:05 normal call

14:37 SOL moves -2.5% rapidly
      ↓
event-triggered model call
```

This provides faster reaction when the market actually changes while avoiding dozens of redundant model calls during quiet conditions.

The trigger itself should be deterministic Python.

The model should only be called after the trigger fires.

---

# 9. Avoiding short-term overreaction

If moving from a 3h cadence to 1h or 30m, the permanent model instructions should explicitly discourage unnecessary churn.

Conceptually include:

> The system is evaluated frequently. Do not change an existing medium-term position solely because of minor short-term noise. Change exposure only when new information materially alters the expected risk/reward.

This is especially important because the model may return expected horizons such as 12-24 hours.

A 30-minute observation interval should not automatically imply a 30-minute trading horizon.

---

# 10. API cost considerations

API cost is currently small enough that experimentation is practical.

The main optimization remains:

- one joint multi-asset request per strategy cycle
- locally calculated features
- no raw candle dumps unless required
- stable system prompt
- compact objective portfolio state
- skip calls when deterministic preflight checks already force HOLD

If three frequency variants are run simultaneously:

```text
3h  = 8 calls/day
1h  = 24 calls/day
30m = 48 calls/day

total = 80 joint calls/day
```

At approximately $0.0012-$0.0014 per current joint call, this would still only be roughly:

```text
$0.10/day
~$3/month
```

assuming similar token usage.

Trading fees and strategy quality are therefore much more important than OpenAI inference cost at the current scale.

---

# 11. Machine / VM architecture

## Can all paper tests run on the same VPS?

Yes.

The current VPS has far more compute than this workload requires.

Observed trading cycles have used approximately:

- ~130 MB RAM peak
- only a few seconds of CPU time
- roughly 15-25 seconds wall-clock time per decision cycle

Even several independent paper strategies running at 3h, 1h, and 30m cadences should be trivial for a small 2-vCPU / 4-GB VPS.

The reason to separate machines is therefore **not performance**.

It is operational isolation.

---

# 12. Recommended environment layout

## During the current paper-only phase

Use **one VM**.

Run all experimental strategies on the same machine, but isolate them clearly.

For example:

```text
/opt/crypto-trader-prod-baseline
/opt/crypto-trader-test-1h
/opt/crypto-trader-test-30m
```

or one code installation with separate configuration/state directories:

```text
/opt/crypto-trader/
    config/
        baseline-3h.yaml
        test-1h.yaml
        test-30m.yaml

    var/
        baseline-3h/
        test-1h/
        test-30m/
```

Each strategy must have:

- separate SQLite database
- separate paper portfolio state
- separate logs
- separate lock file
- separate systemd service/timer names
- separate strategy identifier
- independent cost accounting

Example systemd units:

```text
crypto-trader-3h.service
crypto-trader-3h.timer

crypto-trader-1h.service
crypto-trader-1h.timer

crypto-trader-30m.service
crypto-trader-30m.timer
```

Timers can be staggered slightly to avoid simultaneous OpenAI/Coinbase requests.

Example:

```text
3h strategy:  xx:05
1h strategy:  xx:10
30m strategy: xx:15 / xx:45
```

Exact timing is less important than keeping each experiment internally consistent.

---

# 13. Once real-money trading begins

At that point, the preferred setup changes.

Use:

```text
Production VM
    ↓
LIVE strategy only

Test VM
    ↓
paper trading
A/B experiments
new cadence experiments
new risk policies
new prompts/models
```

This is recommended even though one machine could easily handle everything.

Reasons:

### Fault isolation

A broken experimental deployment should not crash, lock, corrupt, or restart the live trader.

### Credential isolation

The test VM does not need live-trading credentials.

The production VM can contain only the minimum Coinbase credentials required for the live strategy.

### Configuration safety

It becomes much harder to accidentally point a paper experiment at the live database or live executor.

### Deployment safety

Codex/development changes can happen freely on the test VM without touching the production installation.

### Resource isolation

Not currently necessary, but useful protection against unexpected runaway processes.

### Easier mental model

```text
production VM = boring, stable, rarely changed
test VM       = experiments allowed
```

For a system controlling real money, this simplicity is valuable.

---

# 14. Recommended final deployment strategy

## Now

Use the existing single VPS.

Continue the baseline two-week paper run without strategy changes.

After the baseline:

1. preserve the current run as `baseline-3h`
2. add a 1h paper variant
3. optionally add a 30m paper variant
4. add future-return/outcome tracking
5. improve requested/approved/executed exposure logging
6. later test per-asset risk profiles
7. later test risk-policy A/B variants

All of these can run on the current VM.

## When switching to live trading

Buy or provision a second small VPS.

Recommended separation:

```text
VM 1 — production
- live trading only
- stable release
- live Coinbase credentials
- production SQLite
- production timers
- minimal changes

VM 2 — research/test
- paper trading only
- multiple cadence variants
- multiple risk policies
- alternative prompts/models
- development deployments
- no live execution credentials
```

The second VM does not need to be powerful.

A very small VPS is sufficient.

---

# 15. General experiment rule

Change one major dimension at a time.

Bad experiment:

```text
new cadence
+ new model
+ new prompt
+ new risk settings
+ new features
```

If performance changes, it is impossible to know why.

Better:

```text
Experiment 1:
3h vs 1h
everything else identical

Experiment 2:
risk profile A vs B
same model decisions

Experiment 3:
Luna vs Sol
same saved market snapshots

Experiment 4:
old feature set vs expanded feature set
same cadence and risk policy
```

Saved market snapshots and raw model decisions should be retained whenever practical so strategies can later be replayed offline.

---

# 16. Suggested order of future work

After the initial two-week baseline:

1. **Improve analytics/logging**
   - requested exposure
   - approved exposure
   - executed exposure
   - explicit risk-clamp reasons
   - future returns after every decision

2. **Add 1-hour paper strategy**
   - same model
   - same features
   - same risk settings
   - independent portfolio state

3. **Add 30-minute variant**
   - only after the 1h implementation is stable

4. **Compare cadence performance**
   - especially after fees and turnover

5. **Add per-asset risk profiles**

6. **A/B test risk policies**
   - replay the same LLM decisions through different risk engines

7. **Experiment with event-triggered calls**

8. **Only after sufficient evidence, promote one strategy to live trading**

9. **At live promotion, split production and test workloads onto separate VMs**

---

# Summary

The current machine is easily powerful enough to run many simultaneous paper-trading experiments.

During research:

> **Use one VM and isolate strategies logically.**

Once real money is involved:

> **Use one production VM for live trading and one separate test VM for paper experiments.**

The separation is justified by reliability, credentials, deployment safety, and fault isolation—not CPU or RAM requirements.

The overall objective is to make improvements empirically:

- preserve the baseline
- isolate variables
- log enough information to replay decisions
- compare net performance after fees
- promote changes only when they demonstrably outperform the baseline
