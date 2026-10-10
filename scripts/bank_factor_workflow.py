"""Prepare versioned bank dependencies inside the ordinary factor job runtime."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime

import pandas as pd

from alpha_research_os.kernel.canonical import content_hash
from scripts.bank_processing import (
    ProcessingRequest,
    config,
    execute,
    feature_snapshot,
    pipeline_hash,
    processing_plan,
    resolve_asset,
    verify_files,
)
from scripts.bank_processing_api import atomic_json, process_alive
from scripts.build_bank_timing_data import frame_hash


def dependency_preflight(root, start, end, source_mode="warehouse"):
    settings = config(root)
    request = ProcessingRequest(stage="STANDARD", start=settings["start"], end=end, source_mode=source_mode)
    # Check the user's own range before preparing additional historical context.
    processing_plan(root, ProcessingRequest(stage="STANDARD", start=start, end=end, source_mode=source_mode))
    for directory in ("data_updates", "bank_processing"):
        for path in (root / "reports" / directory).glob("*.progress.json"):
            if json.loads(path.read_bytes()).get("status") == "RUNNING":
                raise ValueError("来源更新或银行数据处理正在运行，请完成后再计算因子")
    return processing_plan(root, request)


def workflow_progress(phase, progress):
    print("bank-workflow: " + json.dumps({"phase": phase, "progress": progress}, ensure_ascii=True), flush=True)


def latest_workflow_progress(log):
    for line in reversed(log.splitlines()):
        if line.startswith("bank-workflow: "):
            try:
                return json.loads(line.removeprefix("bank-workflow: "))
            except ValueError:
                continue
    return None


def dependencies_running(root):
    for path in (root / "reports/bank_factor_dependencies").glob("*.progress.json"):
        try:
            state = json.loads(path.read_bytes())
            if state.get("status") == "RUNNING" and process_alive(state.get("worker_pid")):
                return True
        except (OSError, ValueError):
            continue
    return False


@contextmanager
def dependency_task(root):
    directory = root / "reports/bank_factor_dependencies"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{os.getpid()}.progress.json"
    state = {"worker_pid": os.getpid(), "updated_at": datetime.now(UTC).isoformat()}
    atomic_json(path, dict(state, status="RUNNING"))
    try:
        yield
    except Exception as error:
        atomic_json(path, dict(state, status="FAIL", error=str(error)))
        raise
    else:
        atomic_json(path, dict(state, status="PASS"))


def prepare_standard(root, start, end, source_mode="warehouse"):
    dependency_preflight(root, start, end, source_mode)
    workflow_progress("核验标准事实与来源版本", 8)
    request = ProcessingRequest(stage="STANDARD", start=config(root)["start"], end=end, source_mode=source_mode)
    result = execute(root, request, lambda phase, percent: workflow_progress(phase, min(30, percent)))
    return resolve_asset(root, "STANDARD", result["asset_id"])


def prepare_stock_inputs(root, start, end, source_mode="warehouse"):
    folder, manifest = prepare_standard(root, start, end, source_mode)
    workflow_progress("准备银行个股派生输入", 35)
    # Reconstruct the existing snapshot identity from its immutable values rather
    # than recalculating all bank-days for every member of the same batch.
    for path in (root / "data/bank_processing_store/feature_inputs").glob("*/input_manifest.json"):
        saved = json.loads(path.read_bytes())
        if saved.get("standard_pack_id") != manifest["asset_id"]:
            continue
        verify_files(path.parent, saved)
        features = pd.read_parquet(path.parent / "features.parquet")
        identity = content_hash({"standard_pack": manifest["asset_id"], "pipeline": pipeline_hash(root),
                                 "features": frame_hash(features)})
        if saved["input_key"] == identity and path.parent.name == identity.removeprefix("sha256:"):
            workflow_progress("复用已核验银行个股派生输入", 65)
            return path.parent, manifest
    return feature_snapshot(root, folder, manifest), manifest


def prepare_sector_inputs(root, start, end, source_mode="warehouse"):
    _, standard = prepare_standard(root, start, end, source_mode)
    workflow_progress("准备银行个股与板块派生输入", 40)
    request = ProcessingRequest(stage="DERIVED", start=start, end=end, source_id=standard["asset_id"])
    result = execute(root, request, lambda phase, percent: workflow_progress(phase, 40 + percent // 3))
    return result["asset_id"]
