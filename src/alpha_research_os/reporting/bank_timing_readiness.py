"""Read-only catalog attachment for completed allocation input packs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def attach_bank_timing_readiness(project_root: Path, definitions: list[dict[str, Any]]):
    candidates = []
    for path in (project_root / "data/bank_timing_store/packs").glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_bytes())
            candidates.append((manifest["created_at"], path, manifest))
        except (ValueError, KeyError):
            continue  # An incomplete build cannot publish its readiness.
    summary: dict[str, Any] = {"status": "MISSING", "pack_id": None}
    inputs = {}
    releases = []
    for path in (project_root / "data/bank_processing_store/indicators").glob("*/manifest.json"):
        try:
            payload = json.loads(path.read_bytes())
            parquet = path.parent / "sector_values.parquet"
            if (payload.get("observation_level") == "SECTOR" and not payload.get("future_labels", True)
                    and hashlib.sha256(parquet.read_bytes()).hexdigest() == payload["files"][parquet.name]):
                releases.append(payload)
        except (OSError, ValueError, KeyError):
            continue
    if candidates:
        _, path, manifest = max(candidates, key=lambda item: (item[0], str(item[1])))
        try:
            if path.parent.name != manifest["pack_id"].removeprefix("sha256:"):
                raise ValueError("Pack identity mismatch")
            audit_bytes = (path.parent / "audit.json").read_bytes()
            if hashlib.sha256(audit_bytes).hexdigest() != manifest["files"]["audit.json"]:
                raise ValueError("Audit hash mismatch")
            audit = json.loads(audit_bytes)
            if audit["status"] != "PASS_WITH_EXPLICIT_LIMITATIONS" or manifest["future_labels"]:
                raise ValueError("Input pack is not an approved feature-only build")
            inputs = {item["factor_id"]: item for item in audit["indicator_inputs"]}
            summary = {"status": "AVAILABLE_RESEARCH_ONLY", "pack_id": manifest["pack_id"],
                       "start": manifest["start"], "end": manifest["end"],
                       "historical_grade": manifest["historical_grade"],
                       "benchmark_status": audit["benchmark_status"],
                       "benchmark_missing_sessions": audit.get("benchmark_missing_sessions", [])}
        except (OSError, ValueError, KeyError):
            summary = {"status": "INVALID_PACK", "pack_id": manifest.get("pack_id")}
    for definition in definitions:
        calculation = next((release for release in sorted(releases, key=lambda item: item["created_at"], reverse=True)
                            if any(item["factor_id"] == definition["factor_id"]
                                   and item.get("factor_version", "1.0.0") == definition["factor_version"]
                                    for item in release["summaries"])), None)
        ready = inputs.get(definition["factor_id"])
        definition["sector_compute_supported"] = True
        definition["sector_calculation"] = None
        definition["data_readiness"] = dict(summary, indicator=ready)
        if ready:
            label = "就绪" if ready["latest_ready"] else "不足"
            missing = summary["benchmark_missing_sessions"]
            gap = f" 宽基缺失日期：{'、'.join(missing)}。" if missing else ""
            definition["dependency_note"] += (
                f" 基础包覆盖{summary['start']}至{summary['end']}，"
                f"该指标输入就绪{ready['ready_sessions']}/{ready['session_count']}个交易日；"
                f"最新日输入{label}，覆盖率{ready['latest_coverage']:.1%}。"
                f"{gap} {'最终指标已有独立计算结果' if calculation else '最终指标值尚未计算'}；历史认证限制仍保留。"
            )
        elif summary["status"] == "INVALID_PACK":
            definition["dependency_note"] += " 基础包审计或完整性检查未通过，输入就绪状态不可用。"
        if calculation:
            item = next(row for row in calculation["summaries"] if row["factor_id"] == definition["factor_id"])
            definition["sector_calculation"] = {"release_id": calculation["release_id"], **item}
            valid = item["valid_sessions"] > 0
            definition.update(calculated=valid, status="CALCULATED" if valid else "NOT_CALCULATED",
                              status_label="研究值已计算" if valid else "已处理，覆盖不足",
                              release_count=sum(any(row["factor_id"] == definition["factor_id"]
                                                    for row in release["summaries"]) for release in releases),
                              coverage={"start": calculation["start"], "end": calculation["end"],
                                        "coverage": item["valid_sessions"] / item["session_count"]
                                        if item["session_count"] else 0},
                              latest_release_id=calculation["release_id"])
    return definitions
