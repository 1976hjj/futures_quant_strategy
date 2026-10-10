"""Build immutable bank allocation inputs and readiness; no factors, labels or backtests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    sys.path.insert(0, str(directory))

from alpha_research_os.data.bank_timing import (  # noqa: E402
    benchmark_timeline,
    disclosure_changes,
    equal_bank_basket,
    history_readiness,
    latest_input_gaps,
    stock_total_return_inputs,
)
from alpha_research_os.factors.bank_timing import bank_timing_catalog  # noqa: E402
from alpha_research_os.kernel.canonical import content_hash  # noqa: E402
from scripts.bank_factor_inputs import select_fact  # noqa: E402

ENGINE = "bank-allocation-inputs-1.0.0"
FIELDS = ("book_to_price", "cash_yield365", "annual_earnings_yield", "quality_gate", "nim_change",
          "profit_growth", "npl_improvement", "provision_change_yoy", "cet1_change_yoy", "bank_total_return_index")


def file_hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frame_hash(frame: pd.DataFrame):
    return hashlib.sha256(frame.to_json(orient="split", date_format="iso", default_handler=str).encode()).hexdigest()


def date_columns(frame, names):
    for name in names:
        frame[name] = pd.to_datetime(frame[name]).dt.date
    return frame


def released_window(config, start: date, end: date):
    if end < start or end > date.fromisoformat(config["released_feature_end"]):
        raise ValueError("Requested window is outside released feature data; no holdout access is allowed")


def frozen_actions(events):
    """Explain event values using the frozen source, retaining uncertainty explicitly."""
    records = []
    for (code, day), group in events.groupby(["ts_code", "ex_date"], sort=True):
        event = group.iloc[0]
        known = pd.notna(event.imp_ann_date) and event.imp_ann_date < day
        valid = (len(group) == 1 and not bool(event.event_conflict) and known
                 and pd.notna(event.cash_div_tax) and pd.notna(event.stk_div)
                 and event.cash_div_tax >= 0 and event.stk_div >= 0)
        records.append({
            "ts_code": code, "effective_date": day, "first_available_date": event.imp_ann_date,
            "last_available_date": event.imp_ann_date, "cash_dividend_per_share": event.cash_div_tax,
            "stock_dividend_ratio": event.stk_div, "approved_for_dividend_adjustment": bool(valid),
            "source_id": event.source_id, "source_sha256": event.source_sha256,
            "source_file": event.source_file,
            "historical_grade": "frozen reconstructed dividend version; exhaustive certification incomplete",
        })
    return pd.DataFrame(records)


def benchmark_input(root, start, end, calendar):
    candidates = []
    for path in (root / "data/bank_timing_store/benchmark_sources").glob("*/manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (manifest.get("index_code") == "H00300" and manifest.get("return_basis") == "gross_total_return_index"
                and manifest["start"] <= str(calendar[0]) and manifest["end"] >= str(end)
                and manifest["end"] <= "2025-12-31"):
            candidates.append((manifest["retrieved_at"], path, manifest))
    if not candidates:
        empty = pd.DataFrame(columns=["session", "gross_total_return_index", "available_at"])
        return benchmark_timeline(empty, calendar), {"status": "MISSING", "missing_sessions": list(map(str, calendar))}
    _, path, manifest = max(candidates, key=lambda item: (item[0], str(item[1])))
    parquet = path.parent / "benchmark.parquet"
    if file_hash(parquet) != manifest["parquet_sha256"]:
        raise ValueError("Benchmark immutable file hash mismatch")
    with duckdb.connect() as connection:
        frame = connection.execute("SELECT * FROM read_parquet(?) WHERE session BETWEEN ? AND ? ORDER BY session",
                                   [str(parquet), start, end]).df()
    date_columns(frame, ["session"])
    frame = benchmark_timeline(frame, calendar)
    frame["benchmark_id"] = "CSI300_GROSS_TR_H00300"
    frame["return_basis"] = "gross_total_return_index"
    missing = frame[frame.benchmark_status != "READY"].session
    return frame, {**manifest, "status": "PARTIAL" if len(missing) else "READY",
                   "missing_sessions": list(map(str, missing)), "valid_sessions": len(frame) - len(missing),
                   "source_manifest": str(path.relative_to(root))}


def input_readiness(panel, basket, benchmark, config):
    coverage, readiness = [], []
    history_min = config["minimum_history_sessions"]
    basket_lookup = basket.set_index("session").segment_returns.to_dict()
    benchmark_counts = benchmark.set_index("session").segment_observations.to_dict()
    for day, group in panel.groupby("session", sort=True):
        size = len(group)
        finite = {field: np.isfinite(pd.to_numeric(group[field], errors="coerce")) for field in FIELDS}
        finite["book_to_price"] &= group.book_to_price > 0
        for field, valid in finite.items():
            coverage.append({"session": day, "field": field, "universe_count": size,
                             "valid_count": int(valid.sum()), "coverage": float(valid.mean())})
        pb_field = "pb_daily" if "pb_daily" in group else "book_to_price"
        pb_current = np.isfinite(pd.to_numeric(group[pb_field], errors="coerce")) & (group[pb_field] > 0)
        pb = pb_current & (group.pb_history_count >= history_min)
        dy = finite["cash_yield365"] & (group.yield_history_count >= history_min)
        ey = finite["annual_earnings_yield"] & (group.earnings_history_count >= history_min)
        qualified = group.quality_gate == 1
        masks = (pb, dy, pb & dy, qualified & pb & dy, finite["nim_change"],
                 finite["bank_total_return_index"] & (group.segment_observations >= config["trend_sessions"]),
                 None, pb & dy, None, ey)
        for item, mask in zip(bank_timing_catalog(), masks, strict=True):
            denominator = int(qualified.sum()) if item.factor_id.endswith("quality-cheap-high-yield-breadth") else size
            valid_count = int(mask.sum()) if mask is not None else 0
            reason = ""
            basis = "quality_eligible_pool" if denominator != size else "bank_universe"
            if item.factor_id == "bank-sector-operating-deterioration":
                parts = ("profit_growth", "nim_change", "npl_improvement", "provision_change_yoy", "cet1_change_yoy")
                valid_count = min(int(finite[field].sum()) for field in parts)
                basis = "minimum_of_five_component_coverages"
            ratio = valid_count / denominator if denominator else 0.0
            good = valid_count >= config["minimum_valid_banks"] and ratio >= config["minimum_coverage"]
            if item.factor_id == "bank-sector-relative-strength":
                endpoint_count = config["relative_strength_sessions"] + 1
                good = (benchmark_counts.get(day, 0) >= endpoint_count
                        and basket_lookup.get(day, 0) >= endpoint_count)
                valid_count = size if good else 0
                ratio = float(good)
                basis = "paired_gross_total_return_series"
                reason = "" if good else "BENCHMARK_OR_BASKET_SEGMENT_WARMUP_OR_MISSING"
            elif item.factor_id.endswith("quality-cheap-high-yield-breadth"):
                good &= finite["quality_gate"].mean() >= config["minimum_coverage"]
                reason = "" if good else "QUALITY_OR_VALUATION_COVERAGE_OR_WARMUP"
            if not good and not reason:
                reason = "COVERAGE_OR_HISTORY_WARMUP"
            detail = ""
            if not good:
                required = max(config["minimum_valid_banks"], int(np.ceil(denominator * config["minimum_coverage"])))
                detail = (f"有效银行{valid_count}/{denominator}家，需要至少{required}家"
                          f"（覆盖率{config['minimum_coverage']:.0%}）")
                if any(field in {"book_to_price", "pb_daily"} for field in item.required_fields):
                    missing = int((~pb_current).sum())
                    warming = int((pb_current & (group.pb_history_count < history_min)).sum())
                    detail += f"；PB当日缺失或非正{missing}家，PB有效历史不足{history_min}个交易日{warming}家"
                    reason = ("PB_CURRENT_INPUT_MISSING" if missing
                              else "PB_VALID_HISTORY_INSUFFICIENT" if warming else reason)
                if "cash_yield365" in item.required_fields:
                    yield_missing = int((~finite['cash_yield365']).sum())
                    yield_warming = int((finite['cash_yield365'] & (group.yield_history_count < history_min)).sum())
                    detail += f"；已实施股息率当日缺失{yield_missing}家，有效历史不足{history_min}日{yield_warming}家"
                if "annual_earnings_yield" in item.required_fields:
                    earnings_missing = int((~finite['annual_earnings_yield']).sum())
                    earnings_warming = int((finite['annual_earnings_yield']
                                           & (group.earnings_history_count < history_min)).sum())
                    detail += (f"；年度盈利收益率当日缺失{earnings_missing}家，"
                               f"有效历史不足{history_min}日{earnings_warming}家")
                if item.factor_id.endswith("quality-cheap-high-yield-breadth"):
                    unknown = int((~finite['quality_gate']).sum())
                    detail += f"；质量状态未知{unknown}家，明确合格{int(qualified.sum())}家"
                if item.factor_id.endswith("relative-strength"):
                    detail = (f"宽基连续有效{benchmark_counts.get(day, 0)}日、"
                              f"银行篮子连续收益{basket_lookup.get(day, 0)}日；"
                              f"各需至少{endpoint_count}日，缺日后重新预热")
            readiness.append({"session": day, "factor_id": item.factor_id, "factor_version": item.factor_version,
                              "data_ready": bool(good), "valid_count": valid_count, "universe_count": size,
                              "denominator_count": denominator, "coverage": ratio, "coverage_basis": basis,
                              "reason": reason, "reason_detail": detail})
    return pd.DataFrame(coverage), pd.DataFrame(readiness)


def build(root: Path, start: date, end: date, source_path: Path | None = None):
    config = json.loads((root / "config/bank_timing_data.json").read_text(encoding="utf-8"))
    released_window(config, start, end)
    source = source_path or root / config["frozen_bank_input"]
    source_manifest = json.loads((source / "input_manifest.json").read_text(encoding="utf-8"))
    if source_manifest["start"] > str(start) or source_manifest["end"] < str(end):
        raise ValueError("Frozen bank feature pack does not cover requested dates")
    for name in ("features.parquet", "facts.parquet", "capital_reference_prices.parquet", "dividend_events.parquet"):
        if "sha256:" + file_hash(source / name) != source_manifest["files"][name]:
            raise ValueError(f"Frozen bank input changed: {name}")
    with duckdb.connect() as connection:
        features = connection.execute("""SELECT * FROM read_parquet(?) WHERE session BETWEEN ? AND ?
                                      ORDER BY instrument_id,session""",
                                      [str(source / "features.parquet"),
                                       source_manifest.get("feature_warmup_start", source_manifest["start"]), end]).df()
        cutoff = pd.Timestamp(end, tz="Asia/Shanghai") + pd.Timedelta(hours=15)
        facts = connection.execute("""SELECT * FROM read_parquet(?) WHERE available_at <= ? AND report_date <= ?
                                   ORDER BY code,available_at,report_date,metric,source_id,source_sha256""",
                                   [str(source / "facts.parquet"), cutoff.to_pydatetime(), end]).df()
    date_columns(features, ["session"])
    date_columns(facts, ["report_date"])
    if features.empty or features.duplicated(["session", "instrument_id"]).any():
        raise ValueError("Frozen feature keys are empty or duplicated")
    cutoffs = pd.to_datetime(features.session.astype(str), utc=True) + pd.Timedelta(hours=7)
    if features.available_at.isna().any() or (features.available_at > cutoffs).any():
        raise ValueError("Frozen feature availability exceeds the declared source cutoff")
    warmup = date.fromisoformat(config["market_warmup_start"])
    valuation_history = features[['session', 'instrument_id', 'book_to_price',
                                  'cash_yield365', 'annual_earnings_yield']].copy()
    features = features[features.session >= warmup].reset_index(drop=True)
    print("Loading bounded frozen market and dividend evidence", flush=True)
    with duckdb.connect() as connection:
        market = connection.execute("""SELECT ts_code,trade_date,close,pre_close FROM read_parquet(?)
                        WHERE trade_date BETWEEN ? AND ? ORDER BY ts_code,trade_date""",
                        [str(source / "capital_reference_prices.parquet"), warmup, end]).df()
        events = connection.execute("""SELECT * FROM read_parquet(?) WHERE ex_date BETWEEN ? AND ?
                        ORDER BY ts_code,ex_date""", [str(source / "dividend_events.parquet"), warmup, end]).df()
    date_columns(market, ["trade_date"])
    date_columns(events, ["ex_date", "imp_ann_date"])
    market = market[market.ts_code.isin(features.instrument_id.unique())].copy()
    if market.duplicated(["ts_code", "trade_date"]).any():
        raise ValueError("Frozen market keys are duplicated")
    calendar = sorted(market.trade_date.unique())
    market["is_valid_close"] = np.isfinite(market.close) & (market.close > 0)
    market["source_snapshot_id"] = source_manifest["input_key"] + ":capital_reference_prices"
    actions = frozen_actions(events)
    universe = features[["session", "instrument_id"]].sort_values(["session", "instrument_id"]).copy()
    universe["eligible_for_signal"] = True
    universe["eligibility_basis"] = "existing frozen bank-masked eligible feature keys"
    universe["source_input_key"] = source_manifest["input_key"]
    for field in ("listed_session_number", "is_suspended", "is_st", "name_is_point_in_time"):
        universe[field] = None  # These states are not independently re-certified by this package.
    prices = market.rename(columns={"trade_date": "session", "ts_code": "instrument_id", "close": "source_close"})
    aligned = features.merge(prices[["session", "instrument_id", "source_close"]],
                             on=["session", "instrument_id"], validate="one_to_one")
    if len(aligned) != len(features) or not np.allclose(aligned.close, aligned.source_close, atol=1e-8, rtol=0):
        raise ValueError("Frozen and current historical raw closes differ; refuse silent replacement")
    print("Deriving same-period financial changes and forward-built TR segments", flush=True)
    changes, lineage = disclosure_changes(features, facts, select_fact)
    technical = stock_total_return_inputs(market, actions, calendar)
    additions = technical.drop(columns="available_at")
    panel = features.merge(changes, on=["session", "instrument_id"], validate="one_to_one")
    panel = panel.merge(additions, on=["session", "instrument_id"], how="left", validate="one_to_one")
    analysis_calendar = [day for day in calendar if features.session.min() <= day <= end]
    pb_file = source / "bank_pb_history.parquet"
    pb_history = pd.read_parquet(pb_file) if pb_file.exists() else None
    if pb_history is not None:
        date_columns(pb_history, ["session"])
        if pb_history.session.max() > end:
            raise ValueError("PB history exceeds released calculation end")
    panel = history_readiness(panel, analysis_calendar, config["history_sessions"], pb_history,
                              config["minimum_history_sessions"], valuation_history)
    universe["signal_cutoff_at"] = pd.to_datetime(universe.session.astype(str), utc=True) + pd.Timedelta(hours=10)
    universe["bank_type"] = None  # No current classification is backfilled into past dates.
    panel["source_input_key"] = source_manifest["input_key"]
    panel["signal_cutoff_at"] = pd.to_datetime(panel.session.astype(str), utc=True) + pd.Timedelta(hours=10)
    basket, weights = equal_bank_basket(
        universe, technical, analysis_calendar, config["bank_basket"]["universe_lag_sessions"]
    )
    benchmark, benchmark_manifest = benchmark_input(root, warmup, end, calendar)
    field_coverage, readiness = input_readiness(panel, basket, benchmark, config)
    history_panel = panel.copy()
    history_basket = basket.copy()
    panel = panel[panel.session >= start].reset_index(drop=True)
    universe = universe[universe.session >= start].reset_index(drop=True)
    lineage = lineage[lineage.session >= start].reset_index(drop=True)
    basket = basket[basket.session >= start].reset_index(drop=True)
    weights = weights[weights.session >= start].reset_index(drop=True)
    field_coverage = field_coverage[field_coverage.session >= start].reset_index(drop=True)
    readiness = readiness[readiness.session >= start].reset_index(drop=True)
    gaps = latest_input_gaps(panel, config["minimum_history_sessions"], config["trend_sessions"])
    for column in ("current_available_at", "prior_available_at"):
        if (pd.to_datetime(lineage[column], utc=True) > pd.to_datetime(lineage.signal_cutoff, utc=True)).any():
            raise ValueError("Derived financial lineage includes future availability")
    frames = {
        "bank_universe_daily": universe, "bank_panel": panel, "financial_facts": facts,
        "financial_change_lineage": lineage, "stock_total_return_inputs": technical,
        "bank_equal_basket_daily": basket, "bank_equal_basket_weights": weights,
        "benchmark_daily": benchmark, "market_inputs": market, "frozen_action_inputs": actions,
        "field_coverage_daily": field_coverage, "indicator_input_readiness": readiness,
        "latest_input_gaps": gaps, "bank_valuation_history": valuation_history,
        "bank_history_panel": history_panel,
        "bank_basket_history": history_basket,
    }
    if pb_history is not None:
        frames["bank_pb_history"] = pb_history
    inputs = {name: frame_hash(frame) for name, frame in frames.items()}
    code_hashes = {str(path.relative_to(root)): file_hash(path) for path in
                  (root / "scripts/build_bank_timing_data.py", root / "src/alpha_research_os/data/bank_timing.py",
                   root / "scripts/bank_factor_inputs.py", root / "src/alpha_research_os/factors/bank_timing.py")}
    identity = content_hash({"engine": ENGINE, "start": str(start), "end": str(end), "config": config,
                             "source_key": source_manifest["input_key"], "frames": inputs, "code": code_hashes})
    folder = root / "data/bank_timing_store/packs" / identity.removeprefix("sha256:")
    if folder.exists():
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if any(file_hash(folder / name) != digest for name, digest in manifest["files"].items()):
            raise ValueError("Existing immutable timing pack hash mismatch")
        return manifest | {"folder": str(folder), "cache_hit": True}
    folder.mkdir(parents=True)
    spec = {
        "engine": ENGINE, "status": "RESEARCH_ONLY", "created_at": datetime.now(UTC).isoformat(),
        "start": str(start), "end": str(end), "released_feature_end": config["released_feature_end"],
        "source_input_key": source_manifest["input_key"], "configuration": config,
        "standard_pack_id": source_manifest.get("standard_pack_id"),
        "indicator_definitions": [asdict(item) for item in bank_timing_catalog()],
        "feature_only": True, "future_labels": False, "indicator_values_computed": False,
        "position_targets_generated": False, "backtest_started": False,
    }
    (folder / "build_spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, frame in frames.items():
        frame.to_parquet(folder / f"{name}.parquet", index=False)
    summaries = []
    for factor_id, group in readiness.groupby("factor_id", sort=True):
        ready_days = group[group.data_ready].session
        summaries.append({"factor_id": factor_id, "session_count": len(group), "ready_sessions": len(ready_days),
                          "first_ready_session": str(ready_days.min()) if len(ready_days) else None,
                          "last_ready_session": str(ready_days.max()) if len(ready_days) else None,
                          "latest_ready": bool(group.data_ready.iloc[-1]),
                          "latest_coverage": float(group.coverage.iloc[-1]),
                          "latest_reason": group.reason.iloc[-1]})
    audit = {
        "status": "PASS_WITH_EXPLICIT_LIMITATIONS", "source_keys_and_prices_reconciled": True,
        "future_availability_rows": 0, "duplicate_bank_day_keys": 0,
        "bank_day_rows": len(panel), "bank_count": int(panel.instrument_id.nunique()),
        "session_count": int(panel.session.nunique()), "financial_change_lineage_rows": len(lineage),
        "tr_status_counts": technical.tr_status.value_counts().to_dict(),
        "basket_status_counts": basket.status.value_counts().to_dict(),
        "benchmark_status": benchmark_manifest["status"], "indicator_inputs": summaries,
        "benchmark_missing_sessions": benchmark_manifest.get("missing_sessions", []),
        "latest_gap_counts": (gaps.groupby(["field", "gap_status"]).size().rename("count")
                              .reset_index().to_dict("records")),
        "limitations": [source_manifest["historical_status"],
                        f"Valuation warmup starts {valuation_history.session.min()}; "
                        "only currently available financial/dividend facts enter each historical day",
                        "Gross TR is analytical reinvested ex-date return, not after-tax cash-account strategy NAV",
                        "Bank types are unknown until historical classification evidence is supplied",
                        "Bank source universe follows the existing current42 source cohort; certification incomplete",
                        "Missing returns break segments; no reweighting around missing held banks"],
    }
    (folder / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "benchmark_source_manifest.json").write_text(
        json.dumps(benchmark_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files = {path.name: file_hash(path) for path in sorted(folder.iterdir()) if path.is_file()}
    manifest = {
        "schema_version": "1.0.0", "pack_id": identity, "engine": ENGINE,
        "created_at": spec["created_at"], "start": str(start), "end": str(end),
        "source_input_key": source_manifest["input_key"], "files": files, "frame_hashes": inputs,
        "standard_pack_id": source_manifest.get("standard_pack_id"),
        "code_hashes": code_hashes, "row_counts": {name: len(frame) for name, frame in frames.items()},
        "historical_grade": config["historical_grade"], "indicator_inputs": summaries,
        "indicator_values_computed": False, "future_labels": False, "position_targets_generated": False,
        "audit_status": audit["status"],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest | {"folder": str(folder), "cache_hit": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    args = parser.parse_args()
    result = build(ROOT, args.start, args.end)
    print(json.dumps({key: result[key] for key in ("pack_id", "folder", "row_counts", "audit_status", "cache_hit")},
                     ensure_ascii=False, indent=2))
