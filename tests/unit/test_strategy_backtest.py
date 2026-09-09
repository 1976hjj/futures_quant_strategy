from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from alpha_research_os.portfolio.strategy_backtest import (
    StrategyBacktestRequest,
    _maximum_drawdown_period,
    _post_trade_exposure,
    _sell_order_quantity,
    _target_share_quantity,
    select_portfolio,
)
from scripts.serve_strategy_backtest_api import StrategyJobManager

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def test_post_trade_exposure_uses_total_account_equity() -> None:
    exposure = _post_trade_exposure(
        {"600000.SH": 1_000, "000001.SZ": 2_000},
        20_000.0,
        {"600000.SH": 10.0, "000001.SZ": 5.0},
        "600000.SH",
    )

    assert exposure == {
        "post_security_value_cny": 10_000.0,
        "post_invested_value_cny": 20_000.0,
        "post_account_value_cny": 40_000.0,
        "post_security_weight": 0.25,
        "post_total_position_weight": 0.5,
    }


def _request(**updates: object) -> StrategyBacktestRequest:
    payload: dict[str, object] = {
        "name": "test strategy",
        "start": "2025-01-02",
        "end": "2025-12-31",
        "score_rules": [
            {"factor_id": "value", "release_id": DIGEST_A, "direction": "HIGH", "weight": 1}
        ],
        "filter_rules": [
            {
                "factor_id": "turnover",
                "release_id": DIGEST_B,
                "mode": "EXCLUDE_HIGH",
                "fraction": 0.2,
            }
        ],
        "target_count": 3,
        "retention_rank": 5,
    }
    payload.update(updates)
    return StrategyBacktestRequest.model_validate(payload)


def test_strategy_request_requires_valid_retention_and_unique_scores() -> None:
    with pytest.raises(ValidationError, match="retention_rank"):
        _request(retention_rank=2)
    with pytest.raises(ValidationError, match="score factors must be unique"):
        _request(
            score_rules=[
                {"factor_id": "value", "release_id": DIGEST_A, "weight": 1},
                {"factor_id": "value", "release_id": DIGEST_B, "weight": 1},
            ]
        )


def test_selection_filters_then_scores_and_keeps_buffer_holding() -> None:
    request = _request()
    rows = [
        {
            "ts_code": f"{index:06d}.SZ",
            "security_name": f"stock {index}",
            "is_st": False,
            "listed_session_number": 300,
            "factor_values": {
                ("value", DIGEST_A): float(index),
                ("turnover", DIGEST_B): float(index),
            },
        }
        for index in range(10)
    ]

    result = select_portfolio(rows, request, existing_holdings={"000005.SZ"})

    assert result["base_count"] == 10
    assert result["after_filters"] == 8
    assert result["filter_counts"][0]["excluded"] == 2
    assert [item["ts_code"] for item in result["holdings"]] == [
        "000005.SZ",
        "000007.SZ",
        "000006.SZ",
    ]
    assert result["holdings"][0]["retained"] is True


def test_maximum_drawdown_period_reports_peak_trough_and_recovery() -> None:
    period = _maximum_drawdown_period(
        [
            {"session": "2025-01-02", "nav": 100.0},
            {"session": "2025-01-03", "nav": 120.0},
            {"session": "2025-01-06", "nav": 90.0},
            {"session": "2025-01-07", "nav": 115.0},
            {"session": "2025-01-08", "nav": 121.0},
        ]
    )

    assert period == {
        "drawdown": -0.25,
        "peak_session": "2025-01-03",
        "trough_session": "2025-01-06",
        "recovery_session": "2025-01-08",
        "peak_to_trough_sessions": 1,
        "recovery_sessions": 2,
        "peak_to_recovery_sessions": 3,
        "peak_to_trough_calendar_days": 3,
        "recovery_calendar_days": 2,
        "peak_to_recovery_calendar_days": 5,
        "recovered": True,
    }


def test_target_quantities_follow_board_lot_rules() -> None:
    assert _target_share_quantity("600000.SH", 10_099, 10) == 1000
    assert _target_share_quantity("000001.SZ", 999, 10) == 0
    assert _target_share_quantity("688001.SH", 2_019, 10) == 201
    assert _target_share_quantity("688001.SH", 1_999, 10) == 0
    assert _sell_order_quantity("600000.SH", 1200, 1050) == 100
    assert _sell_order_quantity("600000.SH", 299, 0) == 299


def test_strategy_job_history_is_persisted_and_sorted(tmp_path) -> None:
    manager = StrategyJobManager(tmp_path)
    older = manager.run_root / "20260908-120000-aaaaaa.request.json"
    newer = manager.run_root / "20260908-130000-bbbbbb.request.json"
    request = {"name": "history test", "start": "2025-01-01", "end": "2025-12-31", "score_rules": []}
    older.write_text(json.dumps(request), encoding="utf-8")
    newer.write_text(json.dumps(request), encoding="utf-8")
    (manager.run_root / "20260908-120000-aaaaaa.result.json").write_text(
        json.dumps({"summary": {"total_return": 0.1}}), encoding="utf-8"
    )

    jobs = manager.list()

    assert [item["job_id"] for item in jobs] == ["20260908-130000-bbbbbb", "20260908-120000-aaaaaa"]
    assert jobs[0]["status"] == "STOPPED"
    assert jobs[1]["status"] == "PASS"
    assert jobs[1]["result_summary"] == {"total_return": 0.1}
    assert "result" not in jobs[1]
    assert "log_tail" not in jobs[1]

    deleted = manager.delete("20260908-130000-bbbbbb")
    assert deleted["deleted"] is True
    assert not newer.exists()


def test_strategy_job_status_exposes_live_progress(tmp_path) -> None:
    manager = StrategyJobManager(tmp_path)
    job_id = "20260908-140000-cccccc"
    (manager.run_root / f"{job_id}.request.json").write_text(
        json.dumps({"name": "live test"}), encoding="utf-8"
    )
    (manager.run_root / f"{job_id}.progress.json").write_text(
        json.dumps(
            {
                "phase": "连续账户回放",
                "progress": 47,
                "heartbeat_at": "2026-09-08T14:05:00+08:00",
                "processed_sessions": 600,
                "total_sessions": 1500,
                "current_session": "2022-05-06",
                "rebalance_count": 60,
                "position_count": 50,
            }
        ),
        encoding="utf-8",
    )

    class RunningProcess:
        @staticmethod
        def poll() -> None:
            return None

    manager.process = RunningProcess()  # type: ignore[assignment]
    manager.active_job_id = job_id

    status = manager.status(job_id)

    assert status["status"] == "RUNNING"
    assert status["process_alive"] is True
    assert status["progress"] == 47
    assert status["processed_sessions"] == 600
    assert status["current_session"] == "2022-05-06"


def test_strategy_trade_history_is_filtered_by_year_and_deleted_with_result(tmp_path) -> None:
    manager = StrategyJobManager(tmp_path)
    job_id = "20260908-150000-dddddd"
    (manager.run_root / f"{job_id}.request.json").write_text(
        json.dumps({"name": "trade history"}), encoding="utf-8"
    )
    result_path = manager.run_root / f"{job_id}.result.json"
    result_path.write_text(
        json.dumps(
            {
                "summary": {"total_return": 0.1},
                "trades": [
                    {"session": "2025-01-03", "side": "BUY", "amount_cny": 100, "total_cost_cny": 1, "rebalance_id": 1},
                    {
                        "session": "2025-02-03", "side": "SELL", "amount_cny": 120,
                        "total_cost_cny": 2, "realized_pnl_cny": -7.5, "rebalance_id": 1,
                    },
                    {"session": "2026-01-05", "side": "BUY", "amount_cny": 200, "total_cost_cny": 3, "rebalance_id": 2},
                ],
            }
        ),
        encoding="utf-8",
    )

    status = manager.status(job_id)
    yearly = manager.trades(job_id, 2025)

    assert status["trade_detail_available"] is True
    assert status["execution_model_valid"] is False
    assert "trades" not in status["result"]
    assert yearly["total"] == 2
    assert yearly["summary"] == {
        "buy_amount_cny": 100.0,
        "sell_amount_cny": 120.0,
        "commission_cny": 0.0,
        "stamp_duty_cny": 0.0,
        "transfer_fee_cny": 0.0,
        "total_cost_cny": 3.0,
        "realized_pnl_cny": -7.5,
        "rebalance_count": 1,
    }
    manager.delete(job_id)
    assert not result_path.exists()
