"""Read-only index of published factor values and scoped M4 evaluation runs."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from .factor_catalog_overview import build_factor_catalog_overview

STAGE_KEYS = {
    "m4_1": "basic_evidence",
    "m4_2": "processed",
    "m4_3": "robustness",
    "m4_4": "walk_forward",
    "m4_5": "redundancy",
    "m4_6": "execution",
    "m4_7": "factor_explorer",
}
VARIANT_LABELS = {
    "RAW": "原始值",
    "WINSORIZED_ZSCORE": "缩尾标准化",
    "SIZE_NEUTRALIZED": "规模中性化",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _variant_summary(entity: dict[str, Any]) -> dict[str, Any]:
    basic = entity.get("basic_evidence") or {}
    robust = entity.get("robustness") or {}
    folds = entity.get("folds") or []
    execution = entity.get("execution") or {}
    test_values = [
        float(row["test_mean_rank_ic_directed"])
        for row in folds
        if row.get("test_mean_rank_ic_directed") is not None
    ]
    supported = sum(
        row.get("hac_direction_outcome") == "DIRECTION_SUPPORTED"
        or row.get("bootstrap_direction_outcome") == "DIRECTION_SUPPORTED"
        for row in folds
    )
    contradicted = sum(
        row.get("hac_direction_outcome") == "DIRECTION_CONTRADICTED"
        or row.get("bootstrap_direction_outcome") == "DIRECTION_CONTRADICTED"
        for row in folds
    )
    variant = entity.get("variant", "RAW")
    return {
        "variant": variant,
        "variant_label": VARIANT_LABELS.get(variant, variant),
        "release_id": (entity.get("quality") or {}).get("release_id"),
        "coverage": basic.get("mean_coverage", (entity.get("quality") or {}).get("coverage")),
        "mean_rank_ic": basic.get("mean_rank_ic"),
        "mean_test_rank_ic_directed": _mean(test_values),
        "quantile_spread": basic.get("raw_q_high_minus_low"),
        "top_turnover": basic.get("top_quantile_turnover"),
        "hac_q_value": robust.get("hac_bh_q_value"),
        "bootstrap_q_value": robust.get("bootstrap_bh_q_value"),
        "supported_folds": supported,
        "contradicted_folds": contradicted,
        "fold_count": len(folds),
        "deduplication": (entity.get("deduplication") or {}).get("decision"),
        "cluster_role": (entity.get("cluster") or {}).get("selection_status"),
        "execution_status": execution.get("status", "NOT_AVAILABLE"),
        "routes": entity.get("routes") or [],
    }


def _run_reports(project_root: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted((project_root / "reports" / "m4_runs").glob("*.json")):
        if path.name.endswith(".config.json"):
            continue
        payload = _read_json(path)
        if payload and payload.get("config"):
            payload["_job_id"] = path.stem
            reports.append(payload)
    return reports


def _explorer_for_run(run: dict[str, Any]) -> dict[str, Any] | None:
    index = (
        run.get("stages", {}).get("factor_explorer", {}).get("result", {}).get("index")
    )
    return _read_json(Path(index).parent / "evidence-summary.json") if index else None


def _raw_releases(project_root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in (project_root / "data" / "factor_store" / "releases").glob("*/manifest.json"):
        payload = _read_json(path)
        if payload:
            result[payload["release_id"]] = payload
    return result


def build_factor_asset_library(project_root: Path) -> list[dict[str, Any]]:
    catalog = {
        (item["factor_id"], item["factor_version"]): item
        for item in build_factor_catalog_overview(project_root)
    }
    by_id = {item["factor_id"]: item for item in catalog.values()}
    releases = _raw_releases(project_root)
    evaluated_release_ids: set[str] = set()
    assets: list[dict[str, Any]] = []

    for run in _run_reports(project_root):
        config = run["config"]
        release_id = config.get("raw_factor_release_id")
        release = releases.get(release_id)
        if not release:
            continue
        evaluated_release_ids.add(release_id)
        explorer = _explorer_for_run(run)
        entities: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if explorer:
            for entity in explorer.get("factors", []):
                entities.setdefault((entity["factor_id"], entity["factor_version"]), []).append(entity)
        completed_stage_keys = set(run.get("stages", {}))
        stage_status = {
            stage: "COMPLETED" if key in completed_stage_keys else "NOT_RUN"
            for stage, key in STAGE_KEYS.items()
        }
        scope = (explorer or {}).get("report", {}).get("window") or {
            "start": config.get("basic_evidence", {}).get("window_start"),
            "end": config.get("basic_evidence", {}).get("window_end"),
        }
        holding = (explorer or {}).get("report", {}).get("label_horizon_sessions") or config.get(
            "basic_evidence", {}
        ).get("holding_sessions")
        for factor in release["request"]["factors"]:
            key = (factor["factor_id"], factor["factor_version"])
            meta = catalog.get(key) or by_id.get(factor["factor_id"], {})
            variants = sorted(
                (_variant_summary(item) for item in entities.get(key, [])),
                key=lambda item: ("RAW", "WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED").index(item["variant"])
                if item["variant"] in VARIANT_LABELS else 99,
            )
            tested = bool(variants or completed_stage_keys)
            completed_m4 = [stage for stage in ("m4_1", "m4_2", "m4_3", "m4_4", "m4_5", "m4_6") if stage_status[stage] == "COMPLETED"]
            core_m4_completed = all(
                stage_status[stage] == "COMPLETED"
                for stage in ("m4_1", "m4_2", "m4_3", "m4_4", "m4_5", "m4_7")
            )
            status_label = (
                "M4 已完成（含 M4.6）"
                if core_m4_completed and stage_status["m4_6"] == "COMPLETED"
                else "M4 已完成（未运行 M4.6）"
                if core_m4_completed
                else f"已完成 {len(completed_m4)}/6 个 M4 阶段"
                if completed_m4
                else "仅有 RAW 因子值"
            )
            assets.append(
                {
                    "asset_id": f"{run['_job_id']}|{factor['factor_id']}|{factor['factor_version']}",
                    "job_id": run["_job_id"],
                    "report_id": (explorer or {}).get("report", {}).get("report_id"),
                    "factor_release_id": release_id,
                    "factor_id": factor["factor_id"],
                    "factor_version": factor["factor_version"],
                    "chinese_name": meta.get("chinese_name", factor["factor_id"]),
                    "external_name": meta.get("external_name"),
                    "category": meta.get("category", "未分类"),
                    "source_collection": meta.get("source_collection", "CURRENT"),
                    "description": meta.get("description", ""),
                    "asset_window": {"start": release["request"]["start"], "end": release["request"]["end"]},
                    "test_window": scope if tested else None,
                    "holding_sessions": holding,
                    "quantile_count": config.get("basic_evidence", {}).get("quantile_count"),
                    "minimum_pairs_per_session": config.get("basic_evidence", {}).get("minimum_pairs_per_session"),
                    "stage_status": stage_status,
                    "status": run.get("status", "UNKNOWN"),
                    "status_label": status_label,
                    "m4_completed": core_m4_completed,
                    "variants": variants,
                    "variant_count": len(variants) or 1,
                    "has_execution": stage_status["m4_6"] == "COMPLETED",
                    "completed_at": run.get("completed_at"),
                    "scope_note": (
                        f"结果只代表 {scope.get('start')} 至 {scope.get('end')}、预测未来 {holding} 日的口径；"
                        "不代表其他日期或其他持仓期。"
                        if tested and scope.get("start") and holding
                        else "该资产尚未运行收益检验。"
                    ),
                }
            )

    for release_id, release in releases.items():
        if release_id in evaluated_release_ids:
            continue
        for factor in release["request"]["factors"]:
            key = (factor["factor_id"], factor["factor_version"])
            meta = catalog.get(key) or by_id.get(factor["factor_id"], {})
            assets.append(
                {
                    "asset_id": f"{release_id}|{factor['factor_id']}|{factor['factor_version']}",
                    "job_id": None,
                    "report_id": None,
                    "factor_release_id": release_id,
                    "factor_id": factor["factor_id"],
                    "factor_version": factor["factor_version"],
                    "chinese_name": meta.get("chinese_name", factor["factor_id"]),
                    "external_name": meta.get("external_name"),
                    "category": meta.get("category", "未分类"),
                    "source_collection": meta.get("source_collection", "CURRENT"),
                    "description": meta.get("description", ""),
                    "asset_window": {"start": release["request"]["start"], "end": release["request"]["end"]},
                    "test_window": None,
                    "holding_sessions": None,
                    "quantile_count": None,
                    "minimum_pairs_per_session": None,
                    "stage_status": {stage: "NOT_RUN" for stage in STAGE_KEYS},
                    "status": "RAW_ONLY",
                    "status_label": "仅有 RAW 因子值",
                    "m4_completed": False,
                    "variants": [],
                    "variant_count": 1,
                    "has_execution": False,
                    "completed_at": release.get("created_at"),
                    "scope_note": "该资产尚未运行收益检验。",
                }
            )
    return assets


def query_factor_assets(
    items: list[dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 12,
    query: str = "",
    horizon: int | None = None,
    status: Literal["ALL", "TESTED", "RAW_ONLY", "WITH_EXECUTION"] = "ALL",
    sort_order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValueError("page must be positive and pageSize must be between 1 and 100")
    if horizon is not None and horizon not in {5, 10, 20, 30}:
        raise ValueError("horizon must be 5, 10, 20, or 30")
    if status not in {"ALL", "TESTED", "RAW_ONLY", "WITH_EXECUTION"}:
        raise ValueError("unknown asset status")
    term = query.strip().casefold()
    filtered_runs = [
        item
        for item in items
        if (not term or term in " ".join(str(item.get(key) or "") for key in ("factor_id", "external_name", "chinese_name")).casefold())
        and (horizon is None or item["holding_sessions"] == horizon)
        and (
            status == "ALL"
            or (status == "TESTED" and item["test_window"] is not None)
            or (status == "RAW_ONLY" and item["test_window"] is None)
            or (status == "WITH_EXECUTION" and item["has_execution"])
        )
    ]
    filtered_runs.sort(
        key=lambda item: (item.get("completed_at") or "", item["factor_id"], item["asset_id"]),
        reverse=True,
    )

    groups_by_factor: dict[str, list[dict[str, Any]]] = {}
    for run in filtered_runs:
        groups_by_factor.setdefault(run["factor_id"], []).append(run)

    groups: list[dict[str, Any]] = []
    for factor_id, runs in groups_by_factor.items():
        latest = runs[0]
        tested_runs = [run for run in runs if run["test_window"] is not None]
        completed_runs = [run for run in runs if run.get("m4_completed")]
        execution_runs = [run for run in runs if run["has_execution"]]
        status_label = (
            "M4 已完成（含 M4.6）"
            if execution_runs
            else "M4 已完成（未运行 M4.6）"
            if completed_runs
            else "已有部分 M4 检验"
            if tested_runs
            else "仅有 RAW 因子值"
        )
        groups.append(
            {
                "factor_id": factor_id,
                "factor_version": latest["factor_version"],
                "chinese_name": latest["chinese_name"],
                "external_name": latest["external_name"],
                "category": latest["category"],
                "source_collection": latest["source_collection"],
                "description": latest["description"],
                "status_label": status_label,
                "m4_completed": bool(completed_runs),
                "has_execution": bool(execution_runs),
                "run_count": len(runs),
                "tested_run_count": len(tested_runs),
                "horizons": sorted(
                    {run["holding_sessions"] for run in tested_runs if run["holding_sessions"] is not None}
                ),
                "latest_completed_at": latest.get("completed_at"),
                "runs": runs,
            }
        )

    groups.sort(
        key=lambda item: (item.get("latest_completed_at") or "", item["factor_id"]),
        reverse=sort_order == "desc",
    )
    total = len(groups)
    start = (page - 1) * page_size

    all_groups: dict[str, list[dict[str, Any]]] = {}
    for run in items:
        all_groups.setdefault(run["factor_id"], []).append(run)
    return {
        "items": groups[start : start + page_size],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": math.ceil(total / page_size) if total else 0,
        "counts": {
            "total": len(all_groups),
            "tested": sum(any(run["test_window"] is not None for run in runs) for runs in all_groups.values()),
            "raw_only": sum(all(run["test_window"] is None for run in runs) for runs in all_groups.values()),
            "with_execution": sum(any(run["has_execution"] for run in runs) for runs in all_groups.values()),
            "runs": len(items),
        },
    }
