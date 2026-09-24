from __future__ import annotations

import argparse
import os
from pathlib import Path
from uuid import uuid4

from trader.config import active_mode, environment_values, load_config
from trader.errors import SafetyError
from trader.storage import Storage, process_lock
from trader.util import configure_logging, dumps, utcnow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed Coinbase spot trader")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "command",
        choices=[
            "doctor",
            "run",
            "reconcile",
            "maintain-orders",
            "show-state",
            "show-costs",
            "show-orders",
        ],
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="reject launches outside 00/03/... UTC + 5–20 minutes",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    configure_logging()
    store = None
    client = None
    try:
        mode = active_mode(args.env_file)
        banner = f"TRADING MODE: {mode.upper()}"
        if mode == "live":
            banner = f"!!! {banner} — REAL ORDERS ENABLED !!!"
        print(banner, flush=True)
        env, cfg = load_config(args.env_file)
        database = env.TRADER_DB.resolve()
        with process_lock(database.with_suffix(".lock")):
            store = Storage(database)
            # Inspection remains available during an outage; it cannot start a trading cycle.
            if args.command.startswith("show-"):
                if args.command == "show-costs":
                    day, month = store.spending(utcnow())
                    print(
                        dumps(
                            {
                                "daily_usd": day,
                                "monthly_usd": month,
                                "recent_requests": store.rows("api_usage"),
                            }
                        )
                    )
                else:
                    table = "strategy_state" if args.command == "show-state" else "orders"
                    result = {table: store.rows(table), "unresolved_orders": store.unresolved()}
                    if args.command == "show-orders":
                        result["cancellation_attempts"] = store.rows("cancellation_attempts")
                    print(dumps(result))
                return 0
            if args.command == "maintain-orders" and not store.unresolved():
                # Most maintenance invocations have no work. Avoid initializing SDKs or importing
                # the model/indicator pipeline, and do not require Coinbase credentials for a no-op.
                print(dumps({"status": "OK", "mode": mode, "unresolved_orders": []}))
                return 0
            from trader.coinbase_client import build_adapters
            from trader.execution import make_executor, recover_orders

            adapters = build_adapters(cfg, environment_values(args.env_file))
            if args.command == "maintain-orders":
                # No market data, model request, budget gate or cycle slot needed to reduce
                # outstanding execution risk. An API/model outage must not prevent recovery.
                recover_orders(
                    cfg,
                    store,
                    adapters,
                    allow_cancel=mode == "live",
                    env_file=args.env_file,
                    check_permissions=True,
                )
                print(dumps({"status": "OK", "mode": mode, "unresolved_orders": []}))
                return 0
            from openai import OpenAI

            from trader.llm import DecisionClient
            from trader.market_data import MarketData
            from trader.orchestrator import Orchestrator

            client = OpenAI(
                api_key=env.OPENAI_API_KEY.get_secret_value(),
                max_retries=0,
                timeout=cfg.llm.timeout_seconds,
            )
            llm = DecisionClient(client, cfg.llm, store)
            executor = make_executor(mode, cfg, store, adapters, args.env_file)
            service = Orchestrator(
                cfg, mode, store, MarketData(cfg, adapters, store), llm, executor
            )
            if args.command == "run":
                print(dumps({"completed_cycle": service.run(scheduled=args.scheduled)}))
            else:
                service.startup(str(uuid4()))
                if args.command == "doctor":
                    llm.doctor()
                print(
                    dumps(
                        {
                            "status": "OK",
                            "mode": mode,
                            "model": cfg.llm.model,
                            "products": [a.product_id for a in cfg.enabled_assets],
                            "checks": [
                                "configuration",
                                "database",
                                "Coinbase",
                                "portfolios",
                                "reconciliation",
                                "orders",
                                "budget",
                            ]
                            + (["OpenAI model access"] if args.command == "doctor" else []),
                        }
                    )
                )
        return 0
    except Exception as exc:
        # Never print an SDK exception or Pydantic error containing environment input.
        reason = exc.code if isinstance(exc, SafetyError) else "STARTUP_OR_CONFIGURATION_FAILED"
        if args.command == "maintain-orders" and reason == "CYCLE_ALREADY_RUNNING":
            print(dumps({"status": "BUSY", "reason": reason}), flush=True)
            return 0  # The next maintenance tick will retry without overlapping a trading cycle.
        print(dumps({"status": "FAILED_CLOSED", "reason": reason}), flush=True)
        return 1
    finally:
        if client is not None:
            client.close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
