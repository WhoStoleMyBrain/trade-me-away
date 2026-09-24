from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.errors import SafetyError
from trader.schemas import ExecutionResult, Fill, Ledger, OrderIntent
from trader.util import ZERO, D, dumps, utcnow

AUDIT_TABLES = {
    "market_snapshots",
    "computed_features",
    "portfolio_snapshots",
    "model_requests",
    "model_decisions",
    "risk_results",
    "reconciliation_events",
}
TERMINAL = ("FILLED", "PARTIAL", "CANCELLED", "REJECTED", "ABORTED")


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError("CYCLE_ALREADY_RUNNING") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise SafetyError("DATABASE_VERSION_UNSUPPORTED")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trading_cycles (
                cycle_id TEXT PRIMARY KEY, slot TEXT NOT NULL UNIQUE, mode TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                reason TEXT, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_intents (
                client_order_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, mode TEXT NOT NULL,
                portfolio TEXT NOT NULL, product_id TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, intent_json TEXT NOT NULL,
                UNIQUE(cycle_id, product_id),
                FOREIGN KEY(cycle_id) REFERENCES trading_cycles(cycle_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY, order_id TEXT UNIQUE, mode TEXT NOT NULL,
                cycle_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT NOT NULL,
                FOREIGN KEY(client_order_id) REFERENCES order_intents(client_order_id)
            );
            CREATE TABLE IF NOT EXISTS fills (
                mode TEXT NOT NULL, fill_id TEXT NOT NULL, order_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL, cycle_id TEXT NOT NULL,
                portfolio TEXT NOT NULL, product_id TEXT NOT NULL,
                trade_time TEXT NOT NULL, fill_json TEXT NOT NULL,
                PRIMARY KEY(mode, fill_id),
                FOREIGN KEY(client_order_id) REFERENCES order_intents(client_order_id)
            );
            CREATE TABLE IF NOT EXISTS strategy_state (
                mode TEXT NOT NULL, portfolio TEXT NOT NULL, updated_at TEXT NOT NULL,
                state_json TEXT NOT NULL, PRIMARY KEY(mode, portfolio)
            );
            CREATE TABLE IF NOT EXISTS exchange_state (
                portfolio TEXT PRIMARY KEY, initialized_at TEXT NOT NULL,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_marks (
                mode TEXT NOT NULL, portfolio TEXT NOT NULL, day TEXT NOT NULL,
                baseline TEXT NOT NULL, latest TEXT NOT NULL,
                PRIMARY KEY(mode, portfolio, day)
            );
            CREATE TABLE IF NOT EXISTS api_usage (
                request_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, created_at TEXT NOT NULL,
                model TEXT NOT NULL, reasoning_effort TEXT NOT NULL,
                input_tokens INTEGER, cached_input_tokens INTEGER, reasoning_tokens INTEGER,
                output_tokens INTEGER, estimated_usd TEXT NOT NULL, latency_ms INTEGER,
                status TEXT NOT NULL, response_id TEXT, pricing_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS api_usage_date ON api_usage(created_at);
            CREATE INDEX IF NOT EXISTS fills_portfolio ON fills(mode, portfolio, trade_time);
            PRAGMA user_version=1;
        """)
        for table in sorted(AUDIT_TABLES):
            self.db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, mode TEXT NOT NULL,
                created_at TEXT NOT NULL, subject TEXT, payload_json TEXT NOT NULL
            )""")
        self.db.commit()
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SafetyError("DATABASE_INTEGRITY_FAILED")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SafetyError("DATABASE_FOREIGN_KEY_FAILED")

    def close(self) -> None:
        self.db.close()

    def audit(self, table: str, cycle: str, mode: str, subject: str, payload: Any) -> None:
        if table not in AUDIT_TABLES:
            raise ValueError("unknown audit table")
        with self.db:
            self.db.execute(
                f"INSERT INTO {table}(cycle_id,mode,created_at,subject,payload_json) "
                "VALUES(?,?,?,?,?)",
                (cycle, mode, utcnow().isoformat(), subject, dumps(payload)),
            )

    def begin_cycle(self, cycle: str, slot: str, mode: str, config: Any, now: datetime) -> None:
        try:
            with self.db:
                self.db.execute(
                    """INSERT INTO trading_cycles
                    (cycle_id,slot,mode,started_at,status,config_json) VALUES(?,?,?,?,?,?)""",
                    (cycle, slot, mode, now.isoformat(), "RUNNING", dumps(config)),
                )
        except sqlite3.IntegrityError:
            raise SafetyError("DUPLICATE_CYCLE") from None

    def finish_cycle(self, cycle: str, status: str, reason: str | None = None) -> None:
        with self.db:
            self.db.execute(
                """UPDATE trading_cycles SET status=?,reason=?,finished_at=?
                WHERE cycle_id=?""",
                (status, reason, utcnow().isoformat(), cycle),
            )

    def ledger(self, mode: str, portfolio: str) -> Ledger | None:
        row = self.db.execute(
            "SELECT state_json FROM strategy_state WHERE mode=? AND portfolio=?", (mode, portfolio)
        ).fetchone()
        return Ledger.model_validate_json(row[0]) if row else None

    def save_ledger(self, mode: str, portfolio: str, ledger: Ledger) -> None:
        with self.db:
            self._save_ledger(mode, portfolio, ledger)

    def _save_ledger(self, mode: str, portfolio: str, ledger: Ledger) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO strategy_state VALUES(?,?,?,?)",
            (mode, portfolio, utcnow().isoformat(), dumps(ledger)),
        )

    def exchange(self, portfolio: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM exchange_state WHERE portfolio=?", (portfolio,)
        ).fetchone()
        if not row:
            return None
        return {"initialized_at": row["initialized_at"], **json.loads(row["state_json"])}

    def initialize_exchange(self, portfolio: str, state: dict, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO exchange_state VALUES(?,?,?)",
                (portfolio, now.isoformat(), dumps(state)),
            )

    def exchange_seen(self, portfolio: str, observed_at: datetime) -> None:
        with self.db:
            self.db.execute(
                """UPDATE exchange_state
                SET state_json=json_set(state_json, '$.last_reconciled_at', ?) WHERE portfolio=?""",
                (observed_at.isoformat(), portfolio),
            )

    def mark_equity(self, mode: str, portfolio: str, equity: D, now: datetime) -> D:
        day = now.date().isoformat()
        row = self.db.execute(
            "SELECT baseline FROM daily_marks WHERE mode=? AND portfolio=? AND day=?",
            (mode, portfolio, day),
        ).fetchone()
        if row:
            baseline = D(row[0])
        else:
            previous = self.db.execute(
                """SELECT latest FROM daily_marks
                WHERE mode=? AND portfolio=? AND day<? ORDER BY day DESC LIMIT 1""",
                (mode, portfolio, day),
            ).fetchone()
            baseline = D(previous[0]) if previous else equity
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO daily_marks VALUES(?,?,?,?,?)",
                (mode, portfolio, day, str(baseline), str(equity)),
            )
        return max(ZERO, (baseline - equity) / baseline) if baseline else ZERO

    def trades_today(self, mode: str, portfolio: str, now: datetime) -> int:
        # Count submissions, even rejections, to bound churn. Fill history supplies actual turnover.
        return self.db.execute(
            """SELECT COUNT(*) FROM order_intents
            WHERE mode=? AND portfolio=? AND substr(created_at,1,10)=?""",
            (mode, portfolio, now.date().isoformat()),
        ).fetchone()[0]

    def save_intent(self, intent: OrderIntent) -> None:
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?)",
                    (
                        intent.client_order_id,
                        intent.cycle_id,
                        intent.mode,
                        intent.portfolio,
                        intent.product_id,
                        intent.created_at.isoformat(),
                        "PREPARED",
                        dumps(intent),
                    ),
                )
        except sqlite3.IntegrityError:
            raise SafetyError("DUPLICATE_ORDER_INTENT") from None

    def intent_status(self, client_id: str, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE order_intents SET status=? WHERE client_order_id=?", (status, client_id)
            )

    def unresolved(self) -> list[OrderIntent]:
        rows = self.db.execute("""SELECT intent_json FROM order_intents
            WHERE status NOT IN ('FILLED','PARTIAL','CANCELLED','REJECTED','ABORTED')""").fetchall()
        return [OrderIntent.model_validate_json(r[0]) for r in rows]

    def result(self, client_id: str) -> ExecutionResult | None:
        row = self.db.execute(
            "SELECT result_json FROM orders WHERE client_order_id=?", (client_id,)
        ).fetchone()
        return ExecutionResult.model_validate_json(row[0]) if row else None

    def record_result(
        self,
        intent: OrderIntent,
        result: ExecutionResult,
        ledger: Ledger | None = None,
        exchange: dict | None = None,
    ) -> None:
        """Atomic fill + ledger + expected exchange balance commit; safe to replay after a crash."""
        with self.db:
            self.db.execute(
                """INSERT INTO orders VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    order_id=excluded.order_id, status=excluded.status,
                    updated_at=excluded.updated_at, result_json=excluded.result_json""",
                (
                    intent.client_order_id,
                    result.order_id,
                    intent.mode,
                    intent.cycle_id,
                    result.status,
                    utcnow().isoformat(),
                    dumps(result),
                ),
            )
            self.db.execute(
                "UPDATE order_intents SET status=? WHERE client_order_id=?",
                (result.status if result.terminal else "UNRESOLVED", intent.client_order_id),
            )
            for fill in result.fills:
                old = self.db.execute(
                    "SELECT fill_json FROM fills WHERE mode=? AND fill_id=?",
                    (intent.mode, fill.fill_id),
                ).fetchone()
                if old and Fill.model_validate_json(old[0]) != fill:
                    raise SafetyError("FILL_HISTORY_CHANGED")
                self.db.execute(
                    "INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        intent.mode,
                        fill.fill_id,
                        fill.order_id,
                        intent.client_order_id,
                        intent.cycle_id,
                        intent.portfolio,
                        fill.product_id,
                        fill.trade_time.isoformat(),
                        dumps(fill),
                    ),
                )
            if ledger is not None:
                self._save_ledger(intent.mode, intent.portfolio, ledger)
            if exchange is not None:
                self.db.execute(
                    "UPDATE exchange_state SET state_json=? WHERE portfolio=?",
                    (dumps(exchange), intent.portfolio),
                )

    def known_order_ids(self) -> set[str]:
        return {
            r[0] for r in self.db.execute("SELECT order_id FROM orders WHERE mode='live'") if r[0]
        }

    def applied_fill_ids(self, mode: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT fill_id FROM fills WHERE mode=?", (mode,))}

    def spending(self, now: datetime) -> tuple[D, D]:
        day, month = now.date().isoformat(), now.strftime("%Y-%m")
        rows = self.db.execute(
            "SELECT created_at,estimated_usd FROM api_usage WHERE created_at>=?", (month,)
        ).fetchall()
        return (
            sum((D(r[1]) for r in rows if r[0].startswith(day)), ZERO),
            sum((D(r[1]) for r in rows), ZERO),
        )

    def reserve_request(
        self, request_id: str, cycle: str, cfg: Any, amount: D, now: datetime
    ) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            day, month = self.spending(now)
            if day + amount > cfg.daily_budget_usd or month + amount > cfg.monthly_budget_usd:
                raise SafetyError("API_BUDGET_EXCEEDED")
            self.db.execute(
                """INSERT INTO api_usage
                (request_id,cycle_id,created_at,model,reasoning_effort,estimated_usd,status,pricing_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    cycle,
                    now.isoformat(),
                    cfg.model,
                    cfg.reasoning_effort,
                    str(amount),
                    "RESERVED",
                    dumps(cfg.pricing),
                ),
            )

    def finish_request(
        self,
        request_id: str,
        *,
        status: str,
        latency_ms: int,
        response_id: str | None = None,
        usage: dict | None = None,
        cost: D | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                """UPDATE api_usage SET status=?,latency_ms=?,response_id=?
                WHERE request_id=?""",
                (status, latency_ms, response_id, request_id),
            )
            if usage is not None and cost is not None:
                self.db.execute(
                    """UPDATE api_usage SET input_tokens=?,cached_input_tokens=?,
                    reasoning_tokens=?,output_tokens=?,estimated_usd=? WHERE request_id=?""",
                    (
                        usage["input_tokens"],
                        usage["cached_input_tokens"],
                        usage["reasoning_tokens"],
                        usage["output_tokens"],
                        str(cost),
                        request_id,
                    ),
                )

    def rows(self, table: str, limit: int = 50) -> list[dict]:
        allowed = AUDIT_TABLES | {
            "trading_cycles",
            "strategy_state",
            "orders",
            "order_intents",
            "fills",
            "api_usage",
            "exchange_state",
            "daily_marks",
        }
        if table not in allowed:
            raise ValueError("unknown table")
        return [
            dict(r)
            for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))
        ]
