"""Persistent local job wrapper for incremental data updates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from alpha_research_os.kernel.canonical import canonical_json_bytes
from scripts.data_update import DataUpdateRequest, plan, tushare_token


class DataUpdateManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_root = root / "reports" / "data_updates"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.active_job_id: str | None = None

    def _paths(self, job_id: str) -> dict[str, Path]:
        if not job_id or any(char not in "0123456789-abcdef" for char in job_id):
            raise ValueError("invalid data update job id")
        return {part: self.run_root / f"{job_id}.{part}"
                for part in ("request.json", "progress.json", "log")}

    def running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        for path in self.run_root.glob("*.progress.json"):
            try:
                if json.loads(path.read_bytes()).get("status") == "RUNNING":
                    return True
            except (OSError, ValueError):
                continue
        return False

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = DataUpdateRequest.model_validate(payload)
        prepared = plan(self.root, request)
        if any(stage["needs_update"] and stage["id"] in (
            "reference", "market", "financial", "limits", "corporate") for stage in prepared["stages"]):
            if not prepared["token_available"]:
                raise ValueError("TUSHARE_TOKEN is not configured in the backend environment")
        with self.lock:
            if self.running():
                raise RuntimeError("a data update is already running")
            job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            paths = self._paths(job_id)
            paths["request.json"].write_bytes(canonical_json_bytes(request) + b"\n")
            paths["progress.json"].write_bytes(canonical_json_bytes({"status": "RUNNING", "phase": "准备更新", "progress": 0}))
            paths["log"].write_text("", encoding="utf-8")
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(self.root / "src"), str(self.root)))}
            token = tushare_token(self.root)
            if token:
                environment["TUSHARE_TOKEN"] = token
            self.process = subprocess.Popen(
                [sys.executable, "scripts/data_update.py", "--project-root", str(self.root),
                 "--request", str(paths["request.json"]), "--progress", str(paths["progress.json"]),
                 "--log", str(paths["log"])],
                cwd=self.root, env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.active_job_id = job_id
        return self.status(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        paths = self._paths(job_id)
        if not paths["request.json"].exists():
            raise FileNotFoundError(job_id)
        progress = json.loads(paths["progress.json"].read_bytes()) if paths["progress.json"].exists() else {}
        if (self.active_job_id == job_id and self.process is not None
                and self.process.poll() is not None and progress.get("status") == "RUNNING"):
            progress.update({"status": "FAIL", "phase": "进程意外退出", "error": "worker exited without final status"})
        log = paths["log"].read_text(encoding="utf-8", errors="replace") if paths["log"].exists() else ""
        return {"job_id": job_id, "request": json.loads(paths["request.json"].read_bytes()),
                **progress, "log_tail": log[-8000:]}

    def latest(self) -> dict[str, Any] | None:
        requests = sorted(self.run_root.glob("*.request.json"), reverse=True)
        return self.status(requests[0].name.removesuffix(".request.json")) if requests else None
