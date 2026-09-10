from __future__ import annotations

from datetime import date

import duckdb
import pytest
from pydantic import ValidationError

import alpha_research_os.portfolio.rotation_backtest as rotation_module
from alpha_research_os.portfolio.rotation_backtest import (
    RotationAllocationSpec,
    RotationBacktestRequest,
    RotationSignalSpec,
    RotationState,
    advance_rotation,
    pit_industry_snapshot,
    run_rotation_backtest,
)
from scripts.serve_strategy_backtest_api import PROJECT_ROOT, rotation_options

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _candidate(candidate_id: str, factor_id: str, release_id: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "name": candidate_id,
        "score_rules": [
            {"factor_id": factor_id, "release_id": release_id, "direction": "HIGH", "weight": 1}
        ],
        "target_count": 20,
        "retention_rank": 30,
    }


def _request(**updates: object) -> RotationBacktestRequest:
    payload: dict[str, object] = {
        "name": "value-small rotation",
        "start": "2020-01-02",
        "end": "2025-12-31",
        "candidates": [
            _candidate("value", "value-factor", DIGEST_A),
            _candidate("small", "size-factor", DIGEST_B),
        ],
    }
    payload.update(updates)
    return RotationBacktestRequest.model_validate(payload)


def test_rotation_request_requires_two_unique_candidates() -> None:
    with pytest.raises(ValidationError, match="at least 2"):
        _request(candidates=[_candidate("value", "value-factor", DIGEST_A)])
    with pytest.raises(ValidationError, match="candidate_id values must be unique"):
        _request(
            candidates=[
                _candidate("value", "value-factor", DIGEST_A),
                _candidate("value", "size-factor", DIGEST_B),
            ]
        )


def test_rotation_options_expose_calculated_and_not_yet_calculated_factors() -> None:
    options = rotation_options(PROJECT_ROOT)

    assert options["factor_counts"]["total"] == 177
    assert options["factor_counts"]["calculated"] > 0
    assert options["factor_counts"]["needs_calculation"] > 0
    assert len(options["factors"]) == options["factor_counts"]["total"]
    assert any(item["release_id"] is None for item in options["factors"])
    assert any(item["release_id"] is not None for item in options["factors"])


def test_rotation_hysteresis_requires_threshold_confirmation_and_minimum_hold() -> None:
    signal = RotationSignalSpec(
        switch_threshold=0.01,
        confirmation_periods=2,
        minimum_hold_periods=2,
    )
    allocation = RotationAllocationSpec(mode="WINNER_TAKE_ALL", minimum_cash_fraction=0.02)

    first = advance_rotation(RotationState(), {"value": 0.05, "small": 0.02}, signal, allocation)
    assert first["reason"] == "INITIAL_SELECTION"
    assert first["weights"] == {"value": 0.98, "small": 0.0}

    held = advance_rotation(first["state"], {"value": 0.01, "small": 0.04}, signal, allocation)
    assert held["reason"] == "MINIMUM_HOLD"
    assert held["state"].active_candidate_id == "value"

    pending = advance_rotation(held["state"], {"value": 0.01, "small": 0.04}, signal, allocation)
    assert pending["reason"] == "AWAITING_CONFIRMATION"
    assert pending["state"].confirmation_count == 1

    switched = advance_rotation(pending["state"], {"value": 0.01, "small": 0.04}, signal, allocation)
    assert switched["reason"] == "SWITCH_CONFIRMED"
    assert switched["state"].active_candidate_id == "small"
    assert switched["weights"] == {"small": 0.98, "value": 0.0}


def test_rotation_below_threshold_resets_pending_switch() -> None:
    signal = RotationSignalSpec(switch_threshold=0.02, confirmation_periods=2, minimum_hold_periods=0)
    allocation = RotationAllocationSpec()
    state = RotationState(
        active_candidate_id="value",
        pending_candidate_id="small",
        confirmation_count=1,
        periods_since_switch=10,
    )

    result = advance_rotation(state, {"value": 0.04, "small": 0.05}, signal, allocation)

    assert result["reason"] == "BELOW_THRESHOLD"
    assert result["state"].pending_candidate_id is None
    assert result["state"].confirmation_count == 0


def test_pit_industry_snapshot_uses_latest_valid_membership_without_future_fill() -> None:
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA research")
    connection.execute(
        """CREATE TABLE research.sw_industry_membership (
        ts_code VARCHAR, l1_code VARCHAR, l1_name VARCHAR, l2_code VARCHAR, l2_name VARCHAR,
        l3_code VARCHAR, l3_name VARCHAR, in_date DATE, out_date DATE,
        source_snapshot_id VARCHAR
        )"""
    )
    connection.executemany(
        "INSERT INTO research.sw_industry_membership VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("000001.SZ", "L1-OLD", "旧行业", "L2", "二级", "L3", "三级", "2010-01-01", None, "s1"),
            ("000001.SZ", "L1-NEW", "新行业", "L2", "二级", "L3", "三级", "2022-01-01", None, "s2"),
            ("000002.SZ", "L1-FUT", "未来行业", "L2", "二级", "L3", "三级", "2025-01-01", None, "s3"),
        ],
    )

    snapshot = pit_industry_snapshot(
        connection,
        date(2023, 1, 3),
        ["000001.SZ", "000002.SZ", "000003.SZ"],
    )

    assert snapshot["memberships"]["000001.SZ"]["l1_code"] == "L1-NEW"
    assert snapshot["memberships"]["000001.SZ"]["valid_count"] == 2
    assert snapshot["overlaps"] == 1
    assert snapshot["missing"] == ["000002.SZ", "000003.SZ"]


def test_rotation_runner_nets_candidate_targets_into_one_execution_schedule(monkeypatch) -> None:
    request = _request(
        start="2025-01-02",
        end="2025-01-07",
        signal={
            "lookback_sessions": 2,
            "decision_interval_sessions": 1,
            "switch_threshold": 0,
            "confirmation_periods": 1,
            "minimum_hold_periods": 0,
        },
    )
    sessions = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    navs = {"value": [100, 101, 103, 104], "small": [100, 102, 101, 105]}
    captured_schedule: dict[date, dict[str, float]] = {}

    monkeypatch.setattr(
        rotation_module,
        "preflight_rotation",
        lambda *_: {"status": "READY", "session_count": 4},
    )

    def fake_run(_root, strategy_request, _progress=None, **kwargs):
        nonlocal captured_schedule
        schedule = kwargs.get("target_schedule")
        if schedule is not None:
            captured_schedule = schedule
            return {
                "status": "PASS",
                "run_id": "shadow",
                "summary": {"total_return": 0.01},
                "daily": [],
            }
        candidate_id = "value" if strategy_request.name.endswith("/ value") else "small"
        code = "000001.SZ" if candidate_id == "value" else "000002.SZ"
        return {
            "status": "PASS",
            "summary": {"total_return": navs[candidate_id][-1] / 100 - 1},
            "daily": [
                {
                    "session": session,
                    "nav": nav,
                    "daily_return": 0 if index == 0 else nav / navs[candidate_id][index - 1] - 1,
                }
                for index, (session, nav) in enumerate(zip(sessions, navs[candidate_id], strict=True))
            ],
            "benchmark": None,
            "selection_history": [
                {
                    "signal_session": sessions[0],
                    "execution_session": sessions[1],
                    "holdings": [{"ts_code": code}],
                }
            ],
        }

    monkeypatch.setattr(rotation_module, "run_backtest", fake_run)

    result = run_rotation_backtest(PROJECT_ROOT, request)

    assert captured_schedule[date(2025, 1, 2)] == {"000001.SZ": 0.5, "000002.SZ": 0.5}
    assert captured_schedule[date(2025, 1, 6)] == {"000001.SZ": 1.0}
    assert result["strategy_type"] == "ROTATION"
    assert result["rotation"]["decision_count"] == 2
