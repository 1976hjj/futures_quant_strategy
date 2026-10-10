"""Calculate one sector factor via the ordinary single/batch factor job contract."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    sys.path.insert(0, str(directory))

from alpha_research_os.factors.bank_timing import bank_timing_catalog  # noqa: E402
from scripts.bank_factor_workflow import dependency_task, prepare_sector_inputs, workflow_progress  # noqa: E402
from scripts.bank_processing import ProcessingRequest, execute, resolve_asset  # noqa: E402
from scripts.bank_processing_api import atomic_json  # noqa: E402


def publish(root, start, end, factor_id, source_mode="warehouse"):
    definitions = {item.factor_id: item for item in bank_timing_catalog()}
    if factor_id not in definitions:
        raise ValueError("未知银行板块因子")
    with dependency_task(root):
        source_id = prepare_sector_inputs(root, start, end, source_mode)
        workflow_progress("计算板块因子并检查逐日覆盖", 80)
        request = ProcessingRequest(stage="INDICATORS", start=start, end=end, source_id=source_id,
                                    factor_ids=[factor_id])
        output = execute(root, request, lambda phase, percent: workflow_progress(phase, 80 + percent // 6))
        _, release = resolve_asset(root, "INDICATORS", output["asset_id"])
        summary = next(item for item in release["summaries"] if item["factor_id"] == factor_id)
        message = (f"板块因子处理完成：{summary['valid_sessions']}/{summary['session_count']}个交易日有有效值；"
                   f"最新日{'有效' if summary['latest_value'] is not None else '输入不足'}。缺失日期保留原因。")
        workflow_progress("板块因子处理完成", 100)
        return {"release_id": output["asset_id"], "cache_hit": output["cache_hit"], "factor_id": factor_id,
                "factor_version": definitions[factor_id].factor_version, "observation_level": "SECTOR",
                "session_count": summary["session_count"], "row_count": summary["session_count"],
                "accuracy_status": "NOT_REQUIRED", "summary": summary, "source_id": source_id,
                "calculation": {"mode": "BANK_SECTOR", "message": message},
                "data_warnings": [] if summary["latest_value"] is not None
                else ["最新日输入不足：" + (summary.get("latest_reason_detail") or summary["latest_reason"])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-id", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--source-mode", choices=("warehouse", "frozen"), default="warehouse")
    args = parser.parse_args()
    result = publish(ROOT, args.start, args.end, args.factor_id, args.source_mode)
    if args.result:
        atomic_json(args.result, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
