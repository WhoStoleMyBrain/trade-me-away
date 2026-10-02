import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from conftest import NOW
from test_config_cli import write_config

from trader.__main__ import main
from trader.outcomes import track_outcomes
from trader.schemas import Action
from trader.util import D


def seed(store, cfg, decision, at, portfolio="main", cycle="cycle", mode="paper"):
    if not store.db.execute("SELECT 1 FROM trading_cycles WHERE cycle_id=?", (cycle,)).fetchone():
        store.begin_cycle(cycle, cycle, mode, cfg, at)
    store.decision_record(
        cycle,
        mode,
        portfolio,
        decision.product_id,
        decision=decision,
        decision_at=at,
        quote_time=at,
        reference_price=D("100"),
        strategy="default",
    )
    store.db.execute(
        "UPDATE decision_records SET created_at=? WHERE cycle_id=?", (at.isoformat(), cycle)
    )
    store.db.commit()


def historical_rows(product, granularity, start, end, count):
    assert granularity == "FIVE_MINUTE"
    assert 0 < count <= 288
    assert end % 300 == 299
    assert count == (end + 1 - start) // 300
    return [
        {"start": str(t), "open": "100", "high": "120", "low": "80", "close": "110", "volume": "1"}
        for t in reversed(range(start, end + 1, 300))
    ]


@pytest.mark.parametrize("action", ["INCREASE", "HOLD", "DECREASE", "EXIT"])
def test_all_decisions_get_historical_outcomes_without_trades(cfg, store, decision, action):
    decision = decision.model_copy(update={"action": Action(action)})
    seed(store, cfg, decision, NOW - timedelta(days=2))
    adapter = Mock()
    adapter.candles.side_effect = historical_rows
    result = track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)
    assert result["completed"] == 1
    row = store.rows("decision_outcomes")[0]
    data = json.loads(row["payload_json"])
    assert row["status"] == "COMPLETE"
    assert all(D(data[f"return_{h}h"]) == D("0.1") for h in (1, 3, 6, 12, 24))
    assert D(data["max_favorable_excursion_24h"]) == D("0.2")
    assert D(data["max_adverse_excursion_24h"]) == D("-0.2")
    assert data["candle_count"] == 287  # Excludes both partial boundary candles.
    assert all(-300 < v["timing_offset_seconds"] <= 0 for v in data["observations"].values())
    assert adapter.candles.call_count == 1
    assert track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)["checked"] == 0
    assert adapter.candles.call_count == 1
    assert not store.rows("order_intents") and not store.rows("api_usage")


def test_only_mature_horizons_and_mode_are_collected(cfg, store, decision):
    seed(store, cfg, decision, NOW - timedelta(hours=2))
    seed(store, cfg, decision, NOW - timedelta(days=2), mode="live", cycle="live")
    adapter = Mock()
    adapter.candles.side_effect = historical_rows
    track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)
    rows = store.rows("decision_outcomes")
    assert len(rows) == 1 and rows[0]["mode"] == "paper"
    data = json.loads(rows[0]["payload_json"])
    assert set(data["observations"]) == {"1"}
    assert "max_favorable_excursion_24h" not in data


def test_gaps_are_reported_and_retried_without_changing_recorded_returns(cfg, store, decision):
    seed(store, cfg, decision, NOW - timedelta(days=2))
    adapter = Mock()
    adapter.candles.side_effect = lambda *args: historical_rows(*args)[:-1]
    assert track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)["errors"] == 1
    data = json.loads(store.rows("decision_outcomes")[0]["payload_json"])
    assert data["missing_candles_24h"] == 1
    assert "max_favorable_excursion_24h" not in data
    adapter.candles.side_effect = historical_rows
    assert (
        track_outcomes(cfg, store, {"main": adapter}, "paper", NOW + timedelta(hours=1))[
            "completed"
        ]
        == 1
    )
    assert (
        json.loads(store.rows("decision_outcomes")[0]["payload_json"])["return_1h"]
        == data["return_1h"]
    )


@pytest.mark.parametrize("problem", ["duplicate", "nan", "ohlc", "future", "exception"])
def test_invalid_data_never_becomes_a_measured_outcome(cfg, store, decision, problem, caplog):
    seed(store, cfg, decision, NOW - timedelta(days=2))

    def invalid(*args):
        rows = historical_rows(*args)
        if problem == "duplicate":
            return rows + [rows[0]]
        if problem == "exception":
            raise RuntimeError("SECRET_SENTINEL")
        field, value = {
            "nan": ("close", "NaN"),
            "ohlc": ("low", "200"),
            "future": ("start", str(int(NOW.timestamp()) // 300 * 300)),
        }[problem]
        rows[0][field] = value
        return rows

    adapter = Mock()
    adapter.candles.side_effect = invalid
    assert track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)["errors"] == 1
    payload = store.rows("decision_outcomes")[0]["payload_json"]
    assert "return_1h" not in payload
    assert "SECRET_SENTINEL" not in payload + caplog.text


def test_variants_share_reads_but_keep_separate_outcomes(cfg, store, decision):
    seed(store, cfg, decision, NOW - timedelta(days=2))
    seed(store, cfg, decision, NOW - timedelta(days=2), portfolio="variant")
    adapter = Mock()
    adapter.candles.side_effect = historical_rows
    assert track_outcomes(cfg, store, {"main": adapter}, "paper", NOW)["completed"] == 2
    assert adapter.candles.call_count == 1
    assert {r["portfolio"] for r in store.rows("decision_outcomes")} == {"main", "variant"}


def test_cli_no_work_needs_no_api_clients(cfg, monkeypatch, tmp_path):
    write_config(cfg, monkeypatch, tmp_path)
    coinbase = Mock(side_effect=AssertionError("No API client should be created"))
    openai = Mock(side_effect=AssertionError("No OpenAI client should be created"))
    monkeypatch.setattr("trader.coinbase_client.build_adapters", coinbase)
    monkeypatch.setattr("openai.OpenAI", openai)
    assert main(["track-outcomes"]) == 0
    assert main(["show-outcomes"]) == 0
    coinbase.assert_not_called()
    openai.assert_not_called()


def test_cli_collects_without_openai_or_execution(cfg, store, decision, monkeypatch, tmp_path):
    write_config(cfg, monkeypatch, tmp_path)
    monkeypatch.setenv("TRADER_DB", str(tmp_path / "trader.sqlite3"))
    monkeypatch.setattr("trader.__main__.utcnow", lambda: NOW)
    seed(store, cfg, decision, NOW - timedelta(days=2))
    adapter = Mock()
    adapter.candles.side_effect = historical_rows
    monkeypatch.setattr("trader.coinbase_client.build_adapters", lambda *_: {"main": adapter})
    openai = Mock(side_effect=AssertionError("No OpenAI client should be created"))
    executor = Mock(side_effect=AssertionError("No executor should be created"))
    monkeypatch.setattr("openai.OpenAI", openai)
    monkeypatch.setattr("trader.execution.make_executor", executor)
    assert main(["track-outcomes"]) == 0
    assert store.rows("decision_outcomes")[0]["status"] == "COMPLETE"
    assert {call[0] for call in adapter.mock_calls} == {"candles"}
    openai.assert_not_called()
    executor.assert_not_called()
