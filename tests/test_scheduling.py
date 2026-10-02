import json
from datetime import timedelta

import pytest
from conftest import NOW
from test_experiments import experiment_config, experiment_service

from trader.config import AppConfig, StrategyConfig
from trader.errors import SafetyError
from trader.scheduling import check_schedule, cycle_slot, render_timers
from trader.schemas import Ledger, StrategyPosition
from trader.util import D


def cadence_config(cfg):
    data = experiment_config(cfg).model_dump()
    data["portfolios"]["variant"]["strategy"] = "hourly"
    data["strategies"]["hourly"] = {"cadence_minutes": 60, "offset_minutes": 10}
    return AppConfig.model_validate(data)


def test_cadence_cycles_share_lock_database_but_not_slots(
    cfg, store, adapter, actual, openai_mock, decision
):
    cfg = cadence_config(cfg)
    baseline = experiment_service(cfg, store, adapter, actual, openai_mock, decision)
    first = baseline.run()
    hourly = experiment_service(cfg, store, adapter, actual, openai_mock, decision)
    hourly.strategy = "hourly"
    second = hourly.run()
    assert first != second
    assert openai_mock.responses.create.call_count == 2
    assert {r["strategy"] for r in store.rows("trading_cycles")} == {"default", "hourly"}
    assert {r["strategy"] for r in store.rows("api_usage")} == {"default", "hourly"}
    assert {r["portfolio"] for r in store.rows("decision_records")} == {"reference", "variant"}
    prompt = json.loads(openai_mock.responses.create.call_args.kwargs["input"])
    assert list(prompt["portfolios"]) == ["variant"]
    assert prompt["decision_context"]["cadence_minutes"] == 60
    with pytest.raises(SafetyError, match="DUPLICATE_CYCLE"):
        hourly.run()


def test_strategy_binding_prevents_adopting_another_ledger(cfg, store):
    cfg = cadence_config(cfg)
    store.save_ledger(
        "paper", "variant", Ledger(cash=D("1000"), positions={"BTC-USDC": StrategyPosition()})
    )
    with pytest.raises(SafetyError, match="PORTFOLIO_STRATEGY_CHANGED"):
        store.bind_strategies(cfg, "paper")
    assert not store.db.execute("SELECT * FROM portfolio_strategies").fetchall()


@pytest.mark.parametrize("minutes,offset", [(30, 15), (60, 10), (180, 5)])
def test_schedule_boundaries_and_timer_agree(cfg, tmp_path, minutes, offset):
    schedule = StrategyConfig(cadence_minutes=minutes, offset_minutes=offset)
    cfg.strategies["default"] = schedule
    paths = render_timers(cfg, tmp_path)
    text = (tmp_path / "crypto-trader@default.timer").read_text()
    assert len(paths) == 1
    times = [line for line in text.splitlines() if line.startswith("OnCalendar=")]
    assert len(times) == 1440 // minutes
    start = NOW.replace(hour=0, minute=offset, second=0, microsecond=0)
    check_schedule(start, schedule)
    with pytest.raises(SafetyError, match="OUTSIDE_SCHEDULE_WINDOW"):
        check_schedule(start - timedelta(seconds=1), schedule)
    assert cycle_slot(start, schedule, "a") != cycle_slot(start, schedule, "b")
    assert cycle_slot(start, schedule) != cycle_slot(start + timedelta(minutes=minutes), schedule)
    assert "Persistent=false" in text


def test_default_slot_retains_existing_duplicate_protection():
    assert cycle_slot(NOW) == "2026-09-24T09:00:00+00:00"


def test_pending_other_strategy_is_recovered_before_model(
    cfg, store, adapter, actual, openai_mock, decision, monkeypatch
):
    cfg = cadence_config(cfg)
    service = experiment_service(cfg, store, adapter, actual, openai_mock, decision)
    service.strategy = "hourly"

    def recovery(full_cfg, storage, adapters, **kwargs):
        assert set(adapters) == {"reference", "variant"}
        assert set(full_cfg.portfolios) == {"reference", "variant"}
        raise SafetyError("UNRESOLVED_PREVIOUS_ORDER")

    monkeypatch.setattr("trader.orchestrator.recover_orders", recovery)
    with pytest.raises(SafetyError, match="UNRESOLVED_PREVIOUS_ORDER"):
        service.run()
    openai_mock.responses.create.assert_not_called()
