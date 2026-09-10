"""Run one generic multi-portfolio rotation backtest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.kernel.canonical import canonical_json_bytes  # noqa: E402
from alpha_research_os.portfolio.rotation_backtest import (  # noqa: E402
    RotationBacktestRequest,
    run_rotation_backtest,
)
from scripts.run_strategy_backtest import ProgressReporter, _atomic_write  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    reporter = ProgressReporter(args.progress)
    request = RotationBacktestRequest.model_validate_json(args.request.read_bytes())
    print("validating rotation inputs", flush=True)
    reporter.update({"phase": "校验轮动候选组合", "progress": 3})
    try:
        result = run_rotation_backtest(args.project_root.resolve(), request, reporter.update)
    except BaseException:
        reporter.update({"phase": "轮动回测失败"})
        reporter.close()
        raise
    print("publishing backtest report", flush=True)
    reporter.update({"phase": "生成轮动回测报告", "progress": 99})
    _atomic_write(args.result, canonical_json_bytes(result) + b"\n")
    reporter.update({"phase": "轮动回测完成", "progress": 100})
    reporter.close()
    print(json.dumps({"run_id": result["run_id"], "status": "PASS"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
