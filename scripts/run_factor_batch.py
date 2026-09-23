"""Run a selected factor batch, then evaluate its members and shared M4.5 cohort."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for root in (PROJECT_ROOT, SRC_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from scripts.factor_compute_runtime import accuracy_status  # noqa: E402
from scripts.serve_m4_control_api import M4RunRequest, build_pipeline_config  # noqa: E402
from scripts.publish_factor_cohort import publish_cohort  # noqa: E402


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _job_id() -> str:
    import uuid

    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _publisher(factor_id: str) -> tuple[str, list[str]]:
    from alpha_research_os.factors.library import m4_2_factor_entries

    current = {item.spec.factor_id for item in m4_2_factor_entries()}
    if factor_id in current:
        return "scripts/publish_factor_release.py", ["--catalog-profile", "m4.2"]
    if factor_id.startswith("jqdata-"):
        return "scripts/publish_jqdata_factor.py", []
    return "scripts/publish_alpha158_factor.py", []


def _run(command: list[str], log_path: Path) -> tuple[bool, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(SRC_ROOT), str(PROJECT_ROOT)))
    with log_path.open("wb") as stream:
        completed = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, stdout=stream, stderr=subprocess.STDOUT, check=False)
    tail = log_path.read_bytes()[-3000:].decode("utf-8", errors="replace")
    return completed.returncode == 0, tail


def _m4_error(job_id: str, fallback: str) -> str:
    report = PROJECT_ROOT / "reports/m4_runs" / f"{job_id}.json"
    if report.exists():
        try:
            error = json.loads(report.read_text(encoding="utf-8")).get("error") or {}
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"]).splitlines()[0]
        except (OSError, ValueError):
            pass
    return fallback[-1000:] or "M4 process failed"


def _m4_payload(request: dict[str, Any], release_id: str, stages: list[str]) -> dict[str, Any]:
    return {
        "factor_release_id": release_id,
        "stages": stages,
        "window_start": request["start"],
        "window_end": request["end"],
        "holding_sessions": request["holding_sessions"],
        "quantile_count": request["quantile_count"],
        "minimum_pairs_per_session": request["minimum_pairs_per_session"],
        "processed_variants": request["processed_variants"],
        "selection_quantile": request["selection_quantile"],
        "capital_scenarios_cny": request["capital_scenarios_cny"],
        "buy_commission_bps": request["buy_commission_bps"],
        "sell_commission_bps": request["sell_commission_bps"],
        "sell_stamp_duty_bps": request["sell_stamp_duty_bps"],
        "base_slippage_bps": request["base_slippage_bps"],
        "square_root_impact_bps": request["square_root_impact_bps"],
        "maximum_slippage_bps": request["maximum_slippage_bps"],
        "maximum_participation_rate": request["maximum_participation_rate"],
    }


def run_batch(request_path: Path) -> dict[str, Any]:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    batch_id = request_path.name.removesuffix(".request.json")
    root = request_path.parent
    state_path = root / f"{batch_id}.state.json"
    stop_path = root / f"{batch_id}.stop"
    items = [{"factor_id": item["factor_id"], "factor_version": item["factor_version"],
              "name": item["name"], "status": "WAITING", "phase": "等待计算",
              "release_id": None, "m4_job_id": None, "error": None} for item in request["factors"]]
    state: dict[str, Any] = {"batch_id": batch_id, "status": "RUNNING", "phase": "计算因子值",
                             "started_at": datetime.now().astimezone().isoformat(), "items": items,
                             "cohort_job_id": None, "cohort_status": "NOT_RUN", "error": None}
    _write(state_path, state)
    successful: list[tuple[dict[str, Any], str]] = []
    for index, item in enumerate(items):
        if stop_path.exists():
            state.update(status="STOPPED", phase="已按请求停止", completed_at=datetime.now().astimezone().isoformat())
            _write(state_path, state)
            return state
        selected = request["factors"][index]
        reused = request.get("reuse_items", {}).get(item["factor_id"])
        if reused and not reused.get("m4_retry"):
            item.update(status="PASS", phase="已复用前次完成结果",
                        release_id=reused["release_id"], m4_job_id=reused.get("m4_job_id"))
            successful.append((selected, reused["release_id"]))
            _write(state_path, state)
            continue
        if reused and reused.get("m4_retry"):
            release_id = reused["release_id"]
            item.update(status="RUNNING", phase="M4 retry", release_id=release_id)
            _write(state_path, state)
        else:
            item["status"], item["phase"] = "RUNNING", "计算因子值"
            _write(state_path, state)
            publisher, extra = _publisher(item["factor_id"])
            result_path = root / f"{batch_id}.{index}.factor.json"
            command = [sys.executable, publisher, "--factor-id", item["factor_id"],
                       "--start", request["start"], "--end", request["end"],
                       "--result", str(result_path), *extra]
            ok, tail = _run(command, root / f"{batch_id}.{index}.factor.log")
            if not ok or not result_path.exists():
                item.update(status="FAIL", phase="因子计算失败", error=tail[-1000:] or "计算进程未返回结果")
                _write(state_path, state)
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            release_id = result["release_id"]
            item["release_id"] = release_id
            # A publisher may advance an immutable factor version after an
            # implementation correction. Keep the batch result truthful even
            # when this run originated from an older catalog selection.
            if result.get("factor_version"):
                item["factor_version"] = result["factor_version"]
            release_dir = PROJECT_ROOT / "data/factor_store/releases" / release_id.removeprefix("sha256:")
            verification = accuracy_status(release_dir)
            if verification.get("status") == "PENDING":
                item["phase"] = "准确性复核"
                _write(state_path, state)
                for _ in range(120):
                    time.sleep(5)
                    verification = accuracy_status(release_dir)
                    if verification.get("status") != "PENDING":
                        break
            if verification.get("status") in {"FAIL", "PENDING"}:
                item.update(status="FAIL", phase="准确性复核未通过", error=verification.get("error") or "复核未完成")
                _write(state_path, state)
                continue
        successful.append((selected, release_id))
        per_factor_stages = [stage for stage in request["resolved_stages"] if stage != "m4_5"]
        if per_factor_stages:
            item["phase"] = "运行 M4 检验"
            m4_id = _job_id()
            item["m4_job_id"] = m4_id
            config = build_pipeline_config(PROJECT_ROOT, M4RunRequest.model_validate(
                _m4_payload(request, release_id, per_factor_stages)), m4_id)
            config_path = PROJECT_ROOT / "reports/m4_runs" / f"{m4_id}.config.json"
            _write(config_path, config.model_dump(mode="json"))
            _write(state_path, state)
            ok, tail = _run([sys.executable, "scripts/run_m4_pipeline.py", "--config", str(config_path)],
                            root / f"{batch_id}.{index}.m4.log")
            if not ok:
                item.update(status="FAIL", phase="M4 检验失败", error=_m4_error(m4_id, tail))
                _write(state_path, state)
                continue
        item.update(status="PASS", phase="完成")
        _write(state_path, state)

    if stop_path.exists():
        state.update(status="STOPPED", phase="已按请求停止", completed_at=datetime.now().astimezone().isoformat())
        _write(state_path, state)
        return state
    if "m4_5" in request["resolved_stages"]:
        eligible = [(item, release_id) for item, release_id in successful
                    if next(row for row in items if row["factor_id"] == item["factor_id"])["status"] == "PASS"]
        if len(eligible) < 2:
            state["cohort_status"] = "SKIPPED"
            state["error"] = "M4.5 至少需要两个完成前置检验的因子。"
        else:
            state["phase"], state["cohort_status"] = "批次 M4.5 去重检验", "RUNNING"
            _write(state_path, state)
            try:
                cohort_release = publish_cohort(PROJECT_ROOT, [release_id for _, release_id in eligible],
                                                request["start"], request["end"])
                cohort_id = _job_id()
                state["cohort_job_id"] = cohort_id
                # The members already passed their individual M4.1–M4.4 checks.
                # Joint M4.5 needs processed versions and a joint walk-forward,
                # not another expensive basic-evidence/robustness pass.
                cohort_stages = [stage for stage in ("m4_2", "m4_5") if stage in request["resolved_stages"]]
                config = build_pipeline_config(PROJECT_ROOT, M4RunRequest.model_validate(
                    _m4_payload(request, cohort_release, cohort_stages)), cohort_id)
                config_path = PROJECT_ROOT / "reports/m4_runs" / f"{cohort_id}.config.json"
                _write(config_path, config.model_dump(mode="json"))
                _write(state_path, state)
                ok, tail = _run([sys.executable, "scripts/run_m4_pipeline.py", "--config", str(config_path)],
                                root / f"{batch_id}.cohort.log")
                state["cohort_status"] = "PASS" if ok else "FAIL"
                if not ok:
                    state["error"] = _m4_error(cohort_id, tail)
            except Exception as error:
                state["cohort_status"] = "FAIL"
                state["error"] = str(error)
            _write(state_path, state)
    failures = any(item["status"] == "FAIL" for item in items) or state["cohort_status"] in {"FAIL", "SKIPPED"}
    state["status"] = "PARTIAL" if failures and any(item["status"] == "PASS" for item in items) else "FAIL" if failures else "PASS"
    state["phase"] = "批次完成" if state["status"] == "PASS" else "部分完成" if state["status"] == "PARTIAL" else "批次失败"
    state["completed_at"] = datetime.now().astimezone().isoformat()
    _write(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    request = parser.parse_args().request
    try:
        state = run_batch(request)
        return 0 if state["status"] == "PASS" else 1
    except Exception:
        state_path = request.with_name(request.name.replace(".request.json", ".state.json"))
        current = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        current.update(status="FAIL", phase="批次意外终止", error=traceback.format_exc()[-3000:])
        _write(state_path, current)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
