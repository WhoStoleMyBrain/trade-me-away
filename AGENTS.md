# Rules for future work

- Preserve fail-closed behavior. Never weaken a risk check merely to make a test pass.
- Never commit credentials, .env files, databases, account exports, or logs. Never log SDK
  exceptions, authentication headers, key material, or entire unfiltered API responses.
- Live trading occurs only when the exact environment value is `TRADING_MODE=live`.
  Missing, ambiguous, or invalid mode means no trade. Never silently change paper/live behavior.
- Keep paper as the example and recommended initial configuration. Changing mode needs no code edit.
- Use Decimal for balances, fees, account values and order quantities; round sizes down to exchange
  increments. Float64 is permitted only for numerical indicators and bounded model fractions,
  converted to Decimal through strings before monetary calculations.
- All tests must mock external APIs and must never create real live orders or need real credentials.
  Keep the test network block in place.
- Coinbase is authoritative for actual balances/orders; SQLite is authoritative for strategy history
  and audit. Never erase a discrepancy, overwrite a baseline to make it pass, or infer missing cost basis.
- OpenAI never executes trades, selects raw quantities, fetches information, or bypasses local risk.
  Model output is untrusted until schema, product coverage, direction, and risk checks pass.
- Maintain one joint multi-asset request per cycle; retry only transport failures with bounded,
  individually recorded budget reservations. Do not feed old rationales into new requests.
- Paper and live share one decision/risk/refresh pipeline. Executor boundaries own simulated versus
  actual account effects. No mode-specific strategy behavior.
- Every real intent needs a unique client order ID and durable storage BEFORE submission.
  Uncertain outcomes require reconciliation before any further trading. Never blindly retry a POST
  or submit an alternative order after a timeout. A missing discovery result is not proof of absence.
- Verify order state, fills, fees and post-trade balances. An acknowledgment is never a fill.
- Keep IOC for buys and sells. HTTP timeout is not order expiry. Overdue cancellation requires exact
  live mode, a durable owned intent, fresh order identity/portfolio checks, and durable bounded attempts.
  A cancel acknowledgment is not finality; verify racing fills and released holds. Never cancel external
  orders or automatically replace them. Keep doctor/reconcile and paper maintenance read-only at Coinbase.
- Order maintenance must run without OpenAI, cost-budget approval, or a fresh strategy decision, and
  share the trading process lock. Never treat elapsed time or an empty lookup as proof of no submission.
- Keep maintenance infrequent (15 minutes by default) and return early when no orders are pending.
  Do not add minute polling, a daemon, or indicator/model work to that path without explicit approval.
- Four-hour and daily features use completed UTC candles, evaluated in the same three-hour cycle.
  Preserve separate freshness checks for current quotes/accounts and one joint model request.
- TP/SL support is a plan in docs/tp-sl-plan.md, not an implemented trading mode. Do not silently
  enable protective orders or change IOC behavior while implementing unrelated work.
- Validate configured fee allowances against the current exchange tier in both modes before sizing.
- Preserve atomic fill/ledger accounting, mode separation, UTC timestamps, locking, and cycle uniqueness.
- Never add withdrawals, transfers, leverage, borrowing, futures or derivatives unless explicitly
  requested in a future task.
- Verify current official API docs and installed SDK signatures before changing an integration.
- Update README and examples when commands/configuration change. Keep the pricing table explicit.
- Before finishing, run `.venv/bin/pytest`, `.venv/bin/ruff check .`, and `.venv/bin/ruff format --check .`.
