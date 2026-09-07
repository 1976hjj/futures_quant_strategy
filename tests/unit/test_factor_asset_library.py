from __future__ import annotations

import json

import pytest

from alpha_research_os.reporting.factor_asset_library import query_factor_assets


def _asset(asset_id: str, *, horizon: int | None, tested: bool, execution: bool, completed: str) -> dict:
    return {
        "asset_id": asset_id,
        "factor_id": f"factor-{asset_id}",
        "factor_version": "1",
        "chinese_name": f"因子{asset_id}",
        "external_name": None,
        "category": "动量",
        "source_collection": "CURRENT",
        "description": "",
        "holding_sessions": horizon,
        "test_window": {"start": "2020-01-02", "end": "2024-12-31"} if tested else None,
        "m4_completed": tested,
        "has_execution": execution,
        "completed_at": completed,
    }


def test_asset_query_filters_scope_and_keeps_deterministic_order() -> None:
    items = [
        _asset("a", horizon=5, tested=True, execution=False, completed="2026-01-01"),
        _asset("b", horizon=10, tested=True, execution=True, completed="2026-01-03"),
        _asset("c", horizon=None, tested=False, execution=False, completed="2026-01-02"),
    ]

    five_day = query_factor_assets(items, horizon=5)
    executed = query_factor_assets(items, status="WITH_EXECUTION")

    assert [item["factor_id"] for item in five_day["items"]] == ["factor-a"]
    assert [item["factor_id"] for item in executed["items"]] == ["factor-b"]
    assert [item["factor_id"] for item in query_factor_assets(items)["items"]] == ["factor-b", "factor-c", "factor-a"]


def test_asset_query_paginates_and_counts_identical_filtered_set() -> None:
    items = [
        _asset(str(index), horizon=5, tested=True, execution=False, completed=f"2026-01-{index + 1:02d}")
        for index in range(5)
    ]
    response = query_factor_assets(items, page=2, page_size=2, status="TESTED")

    assert response["totalItems"] == 5
    assert response["totalPages"] == 3
    assert len(response["items"]) == 2
    assert response["counts"] == {"total": 5, "tested": 5, "raw_only": 0, "with_execution": 0, "runs": 5}


def test_asset_query_groups_multiple_runs_of_one_factor_into_one_card() -> None:
    five_day = _asset("five-day", horizon=5, tested=True, execution=False, completed="2026-01-01")
    ten_day = _asset("ten-day", horizon=10, tested=True, execution=True, completed="2026-01-02")
    ten_day["factor_id"] = five_day["factor_id"] = "same-factor"
    ten_day["chinese_name"] = five_day["chinese_name"] = "同一个因子"

    response = query_factor_assets([five_day, ten_day])

    assert response["totalItems"] == 1
    assert response["counts"] == {"total": 1, "tested": 1, "raw_only": 0, "with_execution": 1, "runs": 2}
    group = response["items"][0]
    assert group["factor_id"] == "same-factor"
    assert group["run_count"] == 2
    assert group["horizons"] == [5, 10]
    assert [run["asset_id"] for run in group["runs"]] == ["ten-day", "five-day"]


def test_horizon_filter_keeps_only_matching_runs_inside_group() -> None:
    five_day = _asset("five-day", horizon=5, tested=True, execution=False, completed="2026-01-01")
    ten_day = _asset("ten-day", horizon=10, tested=True, execution=False, completed="2026-01-02")
    ten_day["factor_id"] = five_day["factor_id"] = "same-factor"

    response = query_factor_assets([five_day, ten_day], horizon=5)

    assert response["totalItems"] == 1
    assert response["items"][0]["run_count"] == 1
    assert response["items"][0]["runs"][0]["asset_id"] == "five-day"


def test_asset_query_rejects_unbounded_or_unknown_filters() -> None:
    with pytest.raises(ValueError, match="pageSize"):
        query_factor_assets([], page_size=101)
    with pytest.raises(ValueError, match="horizon"):
        query_factor_assets([], horizon=7)


def test_m4_without_optional_execution_is_still_complete(tmp_path) -> None:
    from alpha_research_os.reporting.factor_asset_library import build_factor_asset_library

    release_id = "sha256:raw-release"
    release = {
        "release_id": release_id,
        "created_at": "2026-09-07T22:00:00+08:00",
        "request": {
            "start": "2020-01-02",
            "end": "2024-12-31",
            "factors": [{"factor_id": "alpha158-sump10", "factor_version": "qlib-main-catalog-1"}],
        },
    }
    manifest = tmp_path / "data" / "factor_store" / "releases" / "raw-release" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(release), encoding="utf-8")

    explorer_dir = tmp_path / "reports" / "factor_explorer" / "report"
    explorer_dir.mkdir(parents=True)
    (explorer_dir / "evidence-summary.json").write_text(
        json.dumps(
            {
                "report": {"window": {"start": "2020-01-02", "end": "2024-12-31"}, "label_horizon_sessions": 5},
                "factors": [{"factor_id": "alpha158-sump10", "factor_version": "qlib-main-catalog-1", "variant": "RAW"}],
            }
        ),
        encoding="utf-8",
    )
    run = {
        "status": "PASS",
        "completed_at": "2026-09-07T23:00:00+08:00",
        "config": {"raw_factor_release_id": release_id, "basic_evidence": {"holding_sessions": 5}},
        "stages": {
            "processed": {},
            "basic_evidence": {},
            "robustness": {},
            "walk_forward": {},
            "redundancy": {},
            "factor_explorer": {"result": {"index": str(explorer_dir / "index.html")}},
        },
    }
    run_path = tmp_path / "reports" / "m4_runs" / "job.json"
    run_path.parent.mkdir(parents=True)
    run_path.write_text(json.dumps(run), encoding="utf-8")

    asset = build_factor_asset_library(tmp_path)[0]

    assert asset["m4_completed"] is True
    assert asset["has_execution"] is False
    assert asset["status_label"] == "M4 已完成（未运行 M4.6）"
