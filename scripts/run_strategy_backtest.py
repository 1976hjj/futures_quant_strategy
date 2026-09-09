"""Run one configurable factor-combination strategy backtest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpha_research_os.kernel.canonical import canonical_json_bytes  # noqa: E402
from alpha_research_os.portfolio.strategy_backtest import (  # noqa: E402
    StrategyBacktestRequest,
    run_backtest,
)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    try:
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


class ProgressReporter:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.state: dict[str, Any] = {"phase": "校验输入", "progress": 3}
        self.thread: threading.Thread | None = None
        if path is not None:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()

    def update(self, patch: dict[str, Any]) -> None:
        with self.lock:
            self.state.update(patch)

    def _write(self) -> None:
        if self.path is None:
            return
        with self.lock:
            payload = {**self.state, "heartbeat_at": datetime.now().astimezone().isoformat()}
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")

    def _heartbeat(self) -> None:
        while not self.stopped.wait(1.5):
            try:
                self._write()
            except OSError:
                # A status request or antivirus scanner can briefly hold the
                # destination file on Windows. One collision must not kill all
                # subsequent heartbeats.
                continue

    def close(self) -> None:
        self._write()
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    reporter = ProgressReporter(args.progress)
    request = StrategyBacktestRequest.model_validate_json(args.request.read_bytes())
    print("validating factor inputs", flush=True)
    print("running continuous account", flush=True)
    reporter.update({"phase": "校验因子输入", "progress": 5})
    try:
        result = run_backtest(args.project_root.resolve(), request, reporter.update)
    except BaseException:
        reporter.update({"phase": "回测失败"})
        reporter.close()
        raise
    print("publishing backtest report", flush=True)
    reporter.update({"phase": "生成回测报告", "progress": 98})
    _atomic_write(args.result, canonical_json_bytes(result) + b"\n")
    reporter.update({"phase": "回测完成", "progress": 100})
    reporter.close()
    print(json.dumps({"run_id": result["run_id"], "status": "PASS"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
