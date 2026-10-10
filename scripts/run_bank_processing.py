"""Execute one bounded manual processing job; no downloads, positions or backtests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    sys.path.insert(0, str(directory))

from scripts.bank_processing import ProcessingRequest, execute, processing_plan  # noqa: E402
from scripts.bank_processing_api import BankProcessingManager, atomic_json  # noqa: E402


def run(root, job_id):
    paths = BankProcessingManager(root).paths(job_id)
    payload = json.loads(paths["request.json"].read_bytes())
    request = ProcessingRequest.model_validate(payload["request"])

    def progress(phase, percent, **extra):
        atomic_json(paths["progress.json"], {"status": "RUNNING", "phase": phase, "progress": percent,
                                           "worker_pid": os.getpid(), "updated_at": datetime.now(UTC).isoformat(),
                                           **extra})

    try:
        progress("核对研究范围与来源版本", 5)
        if processing_plan(root, request)["processing_key"] != payload["processing_key"]:
            raise RuntimeError("计划检查后数据或处理规则发生变化，请重新检查计划")
        result = execute(root, request, progress)
        atomic_json(paths["progress.json"], {"status": "PASS", "phase": "处理完成", "progress": 100,
                                           "worker_pid": os.getpid(), "result": result,
                                           "completed_at": datetime.now(UTC).isoformat()})
        return 0
    except Exception as error:
        print(traceback.format_exc(), flush=True)
        atomic_json(paths["progress.json"], {"status": "FAIL", "phase": "处理失败", "progress": 0,
                                           "worker_pid": os.getpid(), "error": str(error),
                                           "completed_at": datetime.now(UTC).isoformat()})
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.project_root.resolve(), args.job_id))
