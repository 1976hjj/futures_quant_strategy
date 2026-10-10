"""Versioned bank data processing and read-only, paginated inspection contracts."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from alpha_research_os.factors.bank_timing import bank_timing_catalog
from alpha_research_os.kernel.canonical import content_hash
from scripts.build_bank_timing_data import build, file_hash, frame_hash, released_window

VERSION = "bank-processing-1.0.0"
STAGES = {"STANDARD": "标准事实处理", "DERIVED": "派生数据处理", "INDICATORS": "板块指标计算"}


class ProcessingRequest(BaseModel):
    stage: Literal["STANDARD", "DERIVED", "INDICATORS"]
    start: date = date(2020, 1, 2)
    end: date = date(2025, 12, 31)
    source_mode: Literal["frozen", "warehouse"] = "frozen"
    source_id: str | None = None
    mode: Literal["UPDATE_IF_NEEDED", "REBUILD"] = "UPDATE_IF_NEEDED"
    factor_ids: list[str] = Field(default_factory=list, max_length=10)
    expected_processing_key: str | None = None

    @model_validator(mode="after")
    def validate_selection(self):
        if self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        if self.source_id and not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", self.source_id):
            raise ValueError("数据版本ID格式无效")
        allowed = {item.factor_id for item in bank_timing_catalog()}
        if len(set(self.factor_ids)) != len(self.factor_ids) or set(self.factor_ids) - allowed:
            raise ValueError("板块指标选择无效")
        if self.factor_ids and self.stage != "INDICATORS":
            raise ValueError("仅板块计算任务可以选择指标")
        return self


def config(root):
    return json.loads((root / "config/bank_timing_data.json").read_text(encoding="utf-8"))


def pipeline_hash(root):
    names = ("scripts/bank_processing.py", "scripts/build_bank_timing_data.py", "scripts/bank_factor_inputs.py",
             "src/alpha_research_os/data/bank_timing.py", "src/alpha_research_os/data/bank_sector_indicators.py",
             "src/alpha_research_os/factors/bank.py", "src/alpha_research_os/factors/bank_timing.py")
    return content_hash({name: file_hash(root / name) for name in names if (root / name).exists()})


def store(root, stage):
    return root / ("data/bank_timing_store/packs" if stage == "DERIVED"
                   else "data/bank_processing_store/" + ("standard" if stage == "STANDARD" else "indicators"))


def manifests(root, stage):
    items = []
    for path in store(root, stage).glob("*/manifest.json"):
        try:
            item = json.loads(path.read_bytes())
            item["asset_id"] = item.get("pack_id") or item.get("release_id")
            if item["asset_id"] != "sha256:" + path.parent.name:
                continue
            items.append(item)
        except (ValueError, KeyError):
            continue
    return sorted(items, key=lambda item: (item["created_at"], item["asset_id"]), reverse=True)


def resolve_asset(root, stage, asset_id=None, start=None, end=None):
    candidates = [item for item in manifests(root, stage)
                  if (not asset_id or item["asset_id"] == "sha256:" + asset_id.removeprefix("sha256:"))
                  and (start is None or item["start"] <= str(start))
                  and (end is None or item["end"] >= str(end))]
    if not candidates:
        raise ValueError("没有覆盖所选日期的上游版本，请先处理上一层数据")
    manifest = candidates[0]
    folder = store(root, stage) / manifest["asset_id"].removeprefix("sha256:")
    return folder, manifest


def verify_files(folder, manifest):
    for name, digest in manifest["files"].items():
        if Path(name).name != name:
            raise ValueError("数据清单包含非法路径")
        if file_hash(folder / name) != digest.removeprefix("sha256:"):
            raise ValueError(f"数据版本完整性检查失败：{name}")


def warehouse_signature(root):
    names = ["config/bank_factors.json", "data/warehouse/bank_token_summary.json"]
    settings = root / "config/bank_factors.json"
    if settings.exists():
        parsed = json.loads(settings.read_bytes())
        names.extend(parsed.get("original_fact_files", []) + parsed.get("original_growth_files", []))
    # Checkpoints describe the original archive versions without exposing connection settings.
    names.extend(str(path.relative_to(root)) for path in (root / "data").glob("*_archive/checkpoint.json"))
    signature = {name: file_hash(root / name) for name in sorted(set(names)) if (root / name).is_file()}
    settings = config(root)
    if settings.get("pb_source"):
        from scripts.bank_factor_inputs import daily_pb_inputs

        signature["daily_pb"] = frame_hash(daily_pb_inputs(
            root, date.fromisoformat(settings["pb_history_start"]),
            date.fromisoformat(settings["released_feature_end"])))
    return content_hash(signature)


def benchmark_fingerprint(root, end):
    """Benchmark source updates invalidate derived cache without touching financial facts."""
    settings = config(root)
    candidates = []
    for path in (root / 'data/bank_timing_store/benchmark_sources').glob('*/manifest.json'):
        item = json.loads(path.read_bytes())
        if (item.get('index_code') == 'H00300' and item.get('return_basis') == 'gross_total_return_index'
                and item['start'] <= settings.get('market_warmup_start', settings['start'])
                and str(end) <= item['end'] <= settings['released_feature_end']):
            candidates.append((item['retrieved_at'], str(path), file_hash(path)))
    return max(candidates)[2] if candidates else None


def processing_inventory(root):
    settings = config(root)
    collections = {}
    for stage in STAGES:
        collections[stage] = [{key: item.get(key) for key in
                               ("asset_id", "start", "end", "created_at", "source_mode", "source_fingerprint",
                                "source_id", "standard_pack_id", "row_counts", "historical_grade", "summaries")}
                              for item in manifests(root, stage)[:20]]
        if stage == "DERIVED":
            for item in collections[stage]:
                folder = store(root, stage) / item["asset_id"].removeprefix("sha256:")
                audit_path = folder / "audit.json"
                if audit_path.exists():
                    audit = json.loads(audit_path.read_bytes())
                    item["benchmark_missing_sessions"] = audit.get("benchmark_missing_sessions", [])
                    item["input_ready_count"] = sum(row["latest_ready"] for row in audit["indicator_inputs"])
    raw_path = root / "data/warehouse/bank_token_summary.json"
    raw = json.loads(raw_path.read_bytes()) if raw_path.exists() else {}
    return {"released_start": settings["start"], "released_end": settings["released_feature_end"],
            "raw_end": raw.get("complete_cutoff_date"), "raw_version": raw.get("run_id"),
            "warehouse_fingerprint": warehouse_signature(root), "versions": collections,
            "execution_policy": "版本未变化时复用；首次处理或来源修订时按选定范围及必要预热历史重建",
            "indicators": [{"factor_id": item.factor_id, "name": item.chinese_name,
                            "research_batch": item.research_batch} for item in bank_timing_catalog()]}


def processing_plan(root, request):
    settings = config(root)
    released_window(settings, request.start, request.end)
    if request.start < date.fromisoformat(settings["start"]):
        raise ValueError("开始日期早于当前已开放研究范围")
    source_id, fingerprint = None, None
    if request.stage == "STANDARD":
        if request.source_mode == "frozen":
            path = root / settings["frozen_bank_input"] / "input_manifest.json"
            original = json.loads(path.read_bytes())
            if original["start"] > str(request.start) or original["end"] < str(request.end):
                raise ValueError("冻结来源未覆盖所选研究日期")
            source_id = original["input_key"]
            fingerprint = content_hash({"manifest": file_hash(path), "source_id": source_id})
            if settings.get("pb_source"):
                from scripts.bank_factor_inputs import daily_pb_inputs

                fingerprint = content_hash({"financial_frozen": fingerprint, "daily_pb": frame_hash(daily_pb_inputs(
                    root, date.fromisoformat(settings["pb_history_start"]), request.end))})
        else:
            if not (root / "data/warehouse/bank_token.duckdb").exists():
                raise ValueError("银行原始归档尚未发布，请先执行行业同步")
            fingerprint = warehouse_signature(root)
    else:
        previous = "STANDARD" if request.stage == "DERIVED" else "DERIVED"
        _, upstream = resolve_asset(root, previous, request.source_id, request.start, request.end)
        source_id = upstream["asset_id"]
        fingerprint = content_hash({"source_id": source_id, "files": upstream["files"]})
        if request.stage == 'DERIVED':
            fingerprint = content_hash({'upstream': fingerprint,
                                        'benchmark': benchmark_fingerprint(root, request.end)})
    expected = {"stage": request.stage, "start": str(request.start), "end": str(request.end),
                "source_mode": request.source_mode if request.stage == "STANDARD" else None,
                "source_id": source_id, "source_fingerprint": fingerprint,
                "factor_ids": sorted(request.factor_ids), "pipeline_hash": pipeline_hash(root),
                "configuration": settings}
    key = content_hash(expected)
    match = next((item for item in manifests(root, request.stage) if item.get("processing_key") == key), None)
    receipt = root / "data/bank_processing_store/receipts" / (key.removeprefix("sha256:") + ".json")
    if request.stage == "DERIVED" and receipt.exists():
        saved = json.loads(receipt.read_bytes())
        _, match = resolve_asset(root, "DERIVED", saved["asset_id"])
    return {"stage": request.stage, "stage_name": STAGES[request.stage], "start": str(request.start),
            "end": str(request.end), "source_id": source_id, "source_fingerprint": fingerprint,
            "processing_key": key, "pipeline_hash": expected["pipeline_hash"], "can_start": True,
            "cache_asset_id": match["asset_id"] if match else None,
            "action": "REUSE" if match and request.mode == "UPDATE_IF_NEEDED" else "REBUILD",
            "execution_policy": "复用相同版本" if match and request.mode == "UPDATE_IF_NEEDED"
            else "选定范围重建，自动带入必要预热历史", "requires_token": False,
            "historical_grade": settings["historical_grade"]}


def write_asset(root, stage, identity, frames, details):
    folder = store(root, stage) / identity.removeprefix("sha256:")
    if (folder / "manifest.json").exists():
        manifest = json.loads((folder / "manifest.json").read_bytes())
        verify_files(folder, manifest)
        return folder, manifest
    folder.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(folder / (name + ".parquet"), index=False)
    files = {path.name: file_hash(path) for path in folder.glob("*.parquet")}
    manifest = {"schema_version": "1.0.0", "pack_id": identity, "asset_id": identity,
                "created_at": datetime.now(UTC).isoformat(), "files": files,
                "row_counts": {name: len(frame) for name, frame in frames.items()},
                "historical_grade": config(root)["historical_grade"], **details}
    temporary = folder / "manifest.pending.json"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(folder / "manifest.json")
    return folder, manifest


def standardize(root, request, prepared):
    from scripts.bank_factor_inputs import source_inputs

    settings = config(root)
    base_start = date.fromisoformat(settings["start"])
    feature_start = date.fromisoformat(settings.get("feature_warmup_start", settings["start"]))
    if request.source_mode == "frozen":
        source = root / settings["frozen_bank_input"]
        original = json.loads((source / "input_manifest.json").read_bytes())
        verify_files(source, original)
        names = ("facts", "dividend_events", "bank_market", "membership", "capital_reference_prices")
        frames = {name: pd.read_parquet(source / (name + ".parquet")) for name in names}
        dividend_coverage = original["dividend_coverage"]
    else:
        facts, events, market, membership, capitals, coverage = source_inputs(root, feature_start, request.end)
        frames = dict(facts=facts, dividend_events=events, bank_market=market, membership=membership,
                      capital_reference_prices=capitals)
        dividend_coverage = sorted(coverage)
        if warehouse_signature(root) != prepared["source_fingerprint"]:
            raise RuntimeError("原始归档在处理期间发生变化，请重新检查计划")
    if settings.get("pb_source"):
        from scripts.bank_factor_inputs import daily_pb_inputs

        frames["bank_pb_history"] = daily_pb_inputs(root, date.fromisoformat(settings["pb_history_start"]), request.end)
    cutoff = pd.Timestamp(request.end, tz="Asia/Shanghai") + pd.Timedelta(hours=15)
    facts = frames["facts"].copy()
    facts["available_at"] = pd.to_datetime(facts.available_at, utc=True)
    facts["report_date"] = pd.to_datetime(facts.report_date).dt.date
    frames["facts"] = facts[(facts.available_at <= cutoff) & (facts.report_date <= request.end)].reset_index(drop=True)
    excluded = len(facts) - len(frames["facts"])
    events = frames["dividend_events"].copy()
    events["ex_date"] = pd.to_datetime(events.ex_date).dt.date
    frames["dividend_events"] = events[events.ex_date <= request.end].reset_index(drop=True)
    warmup = date.fromisoformat(settings["market_warmup_start"])
    for name, column, lower in (("bank_market", "session", feature_start),
                                ("capital_reference_prices", "trade_date", min(warmup, feature_start))):
        frame = frames[name].copy()
        frame[column] = pd.to_datetime(frame[column]).dt.date
        frame = frame[(frame[column] >= lower) & (frame[column] <= request.end)].reset_index(drop=True)
        code = "instrument_id" if name == "bank_market" else "ts_code"
        if frame.duplicated([column, code]).any() or frame.empty:
            raise ValueError("行情键重复或研究区间没有行情")
        frames[name] = frame
    facts = frames["facts"]
    if facts.empty or facts.available_at.isna().any() or not np.isfinite(facts.value).all():
        raise ValueError("标准财务事实为空或包含非法值/信息时间")
    identity = content_hash({"processing": prepared["processing_key"],
                             "frames": {name: frame_hash(frame) for name, frame in frames.items()}})
    details = {**prepared, "start": str(base_start), "requested_start": str(request.start),
               "feature_warmup_start": str(frames['bank_market'].session.min()),
               "source_mode": request.source_mode, "dividend_coverage": dividend_coverage,
               "excluded_after_cutoff": excluded, "status": "RESEARCH_ONLY",
               "facts_latest_available_at": str(facts.available_at.max()), "feature_values_computed": False}
    return write_asset(root, "STANDARD", identity, frames, details)


def feature_snapshot(root, folder, manifest):
    from scripts.bank_factor_inputs import build_features

    verify_files(folder, manifest)
    names = ("facts", "dividend_events", "bank_market", "membership", "capital_reference_prices")
    frames = {name: pd.read_parquet(folder / (name + ".parquet")) for name in names}
    if (folder / "bank_pb_history.parquet").exists():
        frames["bank_pb_history"] = pd.read_parquet(folder / "bank_pb_history.parquet")
    for name, columns in (("facts", ["report_date"]), ("dividend_events", ["ex_date", "imp_ann_date"]),
                           ("bank_market", ["session"]), ("capital_reference_prices", ["trade_date"])):
        for column in columns:
            frames[name][column] = pd.to_datetime(frames[name][column]).dt.date
    features = build_features(frames["facts"], frames["dividend_events"], frames["bank_market"],
                              frames["capital_reference_prices"], set(manifest["dividend_coverage"]))
    identity = content_hash({"standard_pack": manifest["pack_id"], "pipeline": pipeline_hash(root),
                             "features": frame_hash(features)})
    target = root / "data/bank_processing_store/feature_inputs" / identity.removeprefix("sha256:")
    if (target / "input_manifest.json").exists():
        saved = json.loads((target / "input_manifest.json").read_bytes())
        verify_files(target, saved)
        return target
    target.mkdir(parents=True, exist_ok=True)
    for name, frame in {**frames, "features": features}.items():
        frame.to_parquet(target / (name + ".parquet"), index=False)
    saved = {"input_key": identity, "start": manifest["start"], "end": manifest["end"],
             "feature_warmup_start": str(features.session.min()),
             "standard_pack_id": manifest["pack_id"], "historical_status": manifest["historical_grade"],
             "files": {path.name: "sha256:" + file_hash(path) for path in target.glob("*.parquet")}}
    (target / "input_manifest.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return target


def derive(root, request, prepared):
    folder, source = resolve_asset(root, "STANDARD", prepared["source_id"])
    feature_folder = feature_snapshot(root, folder, source)
    result = build(root, request.start, request.end, source_path=feature_folder)
    path = store(root, "DERIVED") / result["pack_id"].removeprefix("sha256:")
    # The immutable pack remains untouched; a separate immutable receipt indexes its processing identity.
    receipt_folder = root / "data/bank_processing_store/receipts"
    receipt_folder.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_folder / (prepared["processing_key"].removeprefix("sha256:") + ".json")
    receipt_path.write_text(json.dumps({**prepared, "asset_id": result["pack_id"]}, indent=2), encoding="utf-8")
    return path, result


def compute_indicators(root, request, prepared):
    from alpha_research_os.data.bank_sector_indicators import calculate_sector_indicators

    folder, upstream = resolve_asset(root, "DERIVED", prepared["source_id"])
    verify_files(folder, upstream)
    spec = json.loads((folder / "build_spec.json").read_bytes())
    history_file = folder / "bank_history_panel.parquet"
    history = pd.read_parquet(history_file if history_file.exists() else folder / "bank_panel.parquet")
    technical = pd.read_parquet(folder / "stock_total_return_inputs.parquet")
    basket_file = folder / "bank_basket_history.parquet"
    basket = pd.read_parquet(basket_file if basket_file.exists() else folder / "bank_equal_basket_daily.parquet")
    benchmark = pd.read_parquet(folder / "benchmark_daily.parquet")
    readiness = pd.read_parquet(folder / "indicator_input_readiness.parquet")
    for frame in (history, technical, basket, benchmark, readiness):
        frame["session"] = pd.to_datetime(frame.session).dt.date
    readiness = readiness[(readiness.session >= request.start) & (readiness.session <= request.end)]
    calendar = sorted(benchmark.session.unique())
    valuation_file = folder / "bank_valuation_history.parquet"
    valuation_history = pd.read_parquet(valuation_file) if valuation_file.exists() else None
    if valuation_history is not None:
        valuation_history['session'] = pd.to_datetime(valuation_history.session).dt.date
    values = calculate_sector_indicators(history, technical, basket, benchmark, readiness, calendar,
                                        spec["configuration"], request.factor_ids,
                                        pd.read_parquet(folder / "bank_pb_history.parquet")
                                        if (folder / "bank_pb_history.parquet").exists() else None,
                                        valuation_history)
    summaries = []
    for factor_id, group in values.groupby("factor_id", sort=True):
        latest = group.iloc[-1]
        summaries.append({"factor_id": factor_id, "factor_version": latest.factor_version, "session_count": len(group),
                          "valid_sessions": int(group.value.notna().sum()), "latest_session": str(latest.session),
                          "latest_value": float(latest.value) if pd.notna(latest.value) else None,
                          "latest_status": latest.status, "latest_reason": latest.reason,
                          "latest_valid_count": int(latest.valid_count),
                          "latest_universe_count": int(latest.universe_count),
                          "latest_denominator_count": int(latest.denominator_count),
                          "latest_coverage_basis": latest.coverage_basis,
                          "latest_coverage": float(latest.coverage),
                          "latest_reason_detail": getattr(latest, "reason_detail", "")})
    identity = content_hash({"processing": prepared["processing_key"], "values": frame_hash(values)})
    return write_asset(root, "INDICATORS", identity, {"sector_values": values},
                       {**prepared, "stage": "INDICATORS", "release_id": identity, "summaries": summaries,
                        "observation_level": "SECTOR", "future_labels": False, "position_targets_generated": False})


def execute(root, request, progress):
    prepared = processing_plan(root, request)
    if prepared["action"] == "REUSE":
        folder, asset = resolve_asset(root, request.stage, prepared["cache_asset_id"])
        verify_files(folder, asset)
        return {"asset_id": asset["asset_id"], "cache_hit": True, "stage": request.stage}
    progress("读取并核验上游版本", 15)
    if request.stage == "STANDARD":
        _, asset = standardize(root, request, prepared)
    elif request.stage == "DERIVED":
        progress("构建财务、收益与历史覆盖输入", 30)
        _, asset = derive(root, request, prepared)
    else:
        progress("按日汇总板块指标，覆盖不足保留缺失", 30)
        _, asset = compute_indicators(root, request, prepared)
    return {"asset_id": asset.get("asset_id") or asset["pack_id"], "cache_hit": False, "stage": request.stage}


def table_page(root, stage, asset_id=None, page=1, page_size=20, factor_id=None, field=None):
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("分页参数无效")
    folder, manifest = resolve_asset(root, stage, asset_id)
    if stage == "DERIVED":
        name = "latest_input_gaps.parquet"
        condition, args = ("WHERE field = ?", [field]) if field else ("", [])
        order = "priority,field,instrument_id"
    elif stage == "INDICATORS":
        name = "sector_values.parquet"
        allowed = {item.factor_id for item in bank_timing_catalog()}
        if factor_id and factor_id not in allowed:
            raise ValueError("板块指标ID无效")
        condition, args = ("WHERE factor_id = ?", [factor_id]) if factor_id else ("", [])
        order = "session DESC,factor_id"
    else:
        raise ValueError("不支持的结果类型")
    if file_hash(folder / name) != manifest["files"][name]:
        raise ValueError("结果文件完整性检查失败")
    with duckdb.connect() as connection:
        base = f"FROM read_parquet(?) {condition}"
        total = connection.execute("SELECT count(*) " + base, [str(folder / name), *args]).fetchone()[0]
        frame = connection.execute("SELECT * " + base + f" ORDER BY {order} LIMIT ? OFFSET ?",
                                   [str(folder / name), *args, page_size, (page - 1) * page_size]).df()
    return {"items": json.loads(frame.to_json(orient="records", date_format="iso")), "page": page,
            "pageSize": page_size, "totalItems": total, "totalPages": (total + page_size - 1) // page_size,
            "asset_id": manifest["asset_id"]}
