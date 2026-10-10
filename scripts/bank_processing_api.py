"""Persistent bounded processing jobs, separate from acquisition and stock backtests."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime

from scripts.bank_processing import ProcessingRequest, processing_plan


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def process_alive(pid):
    if not pid or pid <= 4:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_uint32()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class BankProcessingManager:
    def __init__(self, root):
        self.root = root
        self.run_root = root / "reports/bank_processing"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process = None
        self.active_job_id = None

    def paths(self, job_id):
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", job_id):
            raise ValueError("处理任务ID格式无效")
        return {name: self.run_root / (job_id + "." + name) for name in ("request.json", "progress.json", "log")}

    def status(self, job_id):
        paths = self.paths(job_id)
        payload = json.loads(paths["request.json"].read_bytes())
        progress = json.loads(paths["progress.json"].read_bytes())
        if progress.get("status") == "RUNNING":
            owned = self.active_job_id == job_id and self.process is not None
            alive = self.process.poll() is None if owned else process_alive(progress.get("worker_pid"))
            # A newly queued worker may not have published its PID yet.
            if not alive and progress.get("worker_pid"):
                progress.update(status="FAIL", phase="处理进程已退出", error="任务未写入完成状态，请重新检查计划")
                atomic_json(paths["progress.json"], progress)
        log = paths["log"].read_text(encoding="utf-8", errors="replace") if paths["log"].exists() else ""
        return {"job_id": job_id, "request": payload["request"], **progress, "log_tail": log[-5000:]}

    def latest(self):
        paths = sorted(self.run_root.glob("*.request.json"), reverse=True)
        return self.status(paths[0].name.removesuffix(".request.json")) if paths else None

    def running(self):
        return any(self.status(path.name.removesuffix(".request.json"))["status"] == "RUNNING"
                   for path in self.run_root.glob("*.request.json"))

    def start(self, payload):
        from scripts.bank_factor_workflow import dependencies_running

        if dependencies_running(self.root):
            raise RuntimeError("银行因子正在准备依赖数据，请完成后再处理")
        request = ProcessingRequest.model_validate(payload)
        prepared = processing_plan(self.root, request)
        if request.expected_processing_key and request.expected_processing_key != prepared["processing_key"]:
            raise RuntimeError("处理计划已过期，请重新检查计划")
        if request.stage != "STANDARD":
            request.source_id = prepared["source_id"]  # Freeze the source selected by preflight.
        with self.lock:
            if self.running():
                raise RuntimeError("已有数据处理任务正在运行")
            for path in (self.root / "reports/data_updates").glob("*.progress.json"):
                if json.loads(path.read_bytes()).get("status") == "RUNNING":
                    raise RuntimeError("来源数据正在更新，请完成后再处理")
            job_id = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            paths = self.paths(job_id)
            atomic_json(paths["request.json"], {"request": request.model_dump(mode="json"),
                                               "processing_key": prepared["processing_key"]})
            atomic_json(paths["progress.json"], {"status": "RUNNING", "phase": "准备处理", "progress": 0,
                                               "created_at": datetime.now(UTC).isoformat()})
            log = paths["log"].open("wb")
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(self.root / "src"), str(self.root)))}
            environment.pop("TUSHARE_TOKEN", None)
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "scripts/run_bank_processing.py", "--project-root", str(self.root),
                     "--job-id", job_id], cwd=self.root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                atomic_json(paths["progress.json"], {"status": "FAIL", "phase": "启动失败", "progress": 0,
                                                   "error": str(error)})
                raise
            finally:
                log.close()
            self.active_job_id = job_id
            return self.status(job_id)
