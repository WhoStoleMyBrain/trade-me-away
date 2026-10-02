import json
from unittest.mock import Mock

import pytest
from conftest import NOW, response_for
from pydantic import ValidationError

from trader.config import AppConfig
from trader.execution import PaperExecutor
from trader.llm import DecisionClient
from trader.market_data import MarketData
from trader.orchestrator import Orchestrator
from trader.schemas import DecisionBatch, Ledger, StrategyPosition
from trader.storage import Storage
from trader.util import D


def experiment_config(cfg):
    data = cfg.model_dump()
    data["portfolios"] = {name: data["portfolios"]["main"] for name in ("reference", "variant")}
    data["assets"] = [
        {"product_id": "BTC-USDC", "portfolio": "reference"},
        {"product_id": "BTC-USDC", "portfolio": "variant", "risk": {"max_order_notional": "20"}},
    ]
    data["decision_references"] = {"BTC-USDC": "reference"}
    return AppConfig.model_validate(data)


def experiment_service(cfg, store, adapter, actual, openai_mock, decision):
    adapters = {}
    for name in cfg.portfolios:
        wrapped = Mock(wraps=adapter)
        wrapped.account.return_value = actual.model_copy(
            update={"portfolio": name, "portfolio_id": name}
        )
        adapters[name] = wrapped
    openai_mock.responses.create.return_value = response_for(
        DecisionBatch(market_regime="mixed", decisions=[decision])
    )
    return Orchestrator(
        cfg,
        "paper",
        store,
        MarketData(cfg, adapters, store),
        DecisionClient(openai_mock, cfg.llm, store),
        PaperExecutor(cfg, store, adapters),
    )


def test_same_model_decision_independent_fills_and_histories(
    cfg, store, adapter, actual, openai_mock, decision, sdk
):
    cfg = experiment_config(cfg)
    app = experiment_service(cfg, store, adapter, actual, openai_mock, decision)
    app.run()
    assert openai_mock.responses.create.call_count == 1
    payload = json.loads(openai_mock.responses.create.call_args.kwargs["input"])
    assert list(payload["portfolios"]) == ["reference"]
    assert len(payload["assets"]) == 1
    assert len(store.rows("orders")) == 2
    assert len(store.rows("computed_features")) == 1
    assert len(store.rows("market_snapshots")) == 1
    ref, variant = (store.ledger("paper", name) for name in ("reference", "variant"))
    assert ref.positions["BTC-USDC"].quantity > variant.positions["BTC-USDC"].quantity > 0
    assert ref.cash < variant.cash < 1000
    records = [json.loads(r["payload_json"]) for r in store.rows("decision_records")]
    assert records[0]["decision"] == records[1]["decision"]
    assert all(r["reference_portfolio"] == "reference" for r in records)
    # The next prompt must not mix the other variant's fill into reference history.
    from trader.prompt import build_payload

    markets, accounts = app.market.snapshot("read", "paper")
    states = app.portfolios(accounts, {p: m.quote for p, m in markets.items()}, "read", "read")
    payload = json.loads(build_payload(cfg, markets, states, store, "paper", NOW))
    assert payload["assets"][0]["actual_trades_24h"] == 1
    sdk.limit_order_ioc.assert_not_called()


def test_variant_direction_conflict_cannot_reverse_order(
    cfg, store, adapter, actual, openai_mock, decision
):
    cfg = experiment_config(cfg)
    store.save_ledger(
        "paper",
        "variant",
        Ledger(cash=D("500"), positions={"BTC-USDC": StrategyPosition(quantity=D("5"))}),
    )
    experiment_service(cfg, store, adapter, actual, openai_mock, decision).run()
    records = {
        r["portfolio"]: json.loads(r["payload_json"]) for r in store.rows("decision_records")
    }
    assert records["reference"]["execution_status"] == "FILLED"
    assert records["variant"]["initial_risk"]["reasons"] == ["ACTION_TARGET_CONFLICT"]
    assert len(store.rows("orders")) == 1


def test_invalid_reference_direction_cannot_trade_other_variant(
    cfg, store, adapter, actual, openai_mock, decision
):
    cfg = experiment_config(cfg)
    store.save_ledger(
        "paper",
        "reference",
        Ledger(cash=D("500"), positions={"BTC-USDC": StrategyPosition(quantity=D("5"))}),
    )
    experiment_service(cfg, store, adapter, actual, openai_mock, decision).run()
    assert not store.rows("orders")
    assert all(
        "REFERENCE_ACTION_TARGET_CONFLICT" in r["payload_json"]
        for r in store.rows("decision_records")
    )


@pytest.mark.parametrize("problem", ["missing_reference", "unknown_reference", "same_portfolio"])
def test_ambiguous_experiment_fails_configuration(cfg, problem):
    data = experiment_config(cfg).model_dump()
    if problem == "missing_reference":
        data["decision_references"] = {}
    elif problem == "unknown_reference":
        data["decision_references"]["BTC-USDC"] = "missing"
    else:
        data["assets"][1]["portfolio"] = "reference"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_database_migration_preserves_fills_and_order_identity(cfg, store, intent, tmp_path):
    from trader.schemas import ExecutionResult, Fill

    store.begin_cycle(intent.cycle_id, "slot", "paper", cfg, NOW)
    store.save_intent(intent)
    store.record_result(
        intent,
        ExecutionResult(
            order_id="order",
            status="FILLED",
            terminal=True,
            fills=[
                Fill(
                    fill_id="fill",
                    order_id="order",
                    product_id=intent.product_id,
                    side="BUY",
                    base_size=D("1"),
                    price=D("100"),
                    fee=D("0.6"),
                    trade_time=NOW,
                )
            ],
        ),
    )
    # Reproduce the legacy unique constraint, retaining child rows as in an existing installation.
    sql = store.db.execute("SELECT sql FROM sqlite_master WHERE name='order_intents'").fetchone()[0]
    store.db.execute("PRAGMA foreign_keys=OFF")
    store.db.execute(
        sql.replace("order_intents", "legacy_intents").replace(
            "UNIQUE(cycle_id, portfolio, product_id)", "UNIQUE(cycle_id, product_id)"
        )
    )
    store.db.execute("INSERT INTO legacy_intents SELECT * FROM order_intents")
    store.db.execute("DROP TABLE order_intents")
    store.db.execute("ALTER TABLE legacy_intents RENAME TO order_intents")
    store.db.execute("PRAGMA user_version=2")
    store.db.commit()
    upgraded = Storage(tmp_path / "trader.sqlite3")
    try:
        assert upgraded.saved_intent(intent.client_order_id)[0] == intent
        assert len(upgraded.rows("fills")) == len(upgraded.rows("orders")) == 1
        upgraded.save_intent(
            intent.model_copy(update={"portfolio": "variant", "client_order_id": "second"})
        )
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchone() is None
        assert upgraded.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        upgraded.close()
