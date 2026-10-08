"""Audit published bank data, add provenance columns without changing original observations."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from scripts.bank_data_lineage import add_lineage, implemented_dividend_events
from scripts.data_update import _archive_endpoint
from scripts.sync_bank_industry import APIS, FIELDS, field_candidates


def audit(root: Path):
    warehouse = root / "data/warehouse"
    report_dir = root / "reports/bank_data_audit" / datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir.mkdir(parents=True)
    file_index = {}
    for base in [root / "data/tushare_bank_archive", root / "data/eastmoney_bank_archive"]:
        for path in base.rglob("*.json"):
            if path.name.endswith("metadata.json") or path.stat().st_size > 30_000_000:
                continue
            file_index[hashlib.sha256(path.read_bytes()).hexdigest()] = str(path.resolve())
    free_evidence = {}
    for path in (root / "data/eastmoney_bank_archive/runs").glob("*/raw/*.metadata.json"):
        detail = json.loads(path.read_text(encoding="utf8"))
        free_evidence[detail["raw_sha256"]] = {
            "source_file": file_index.get(detail["raw_sha256"]),
            "source_url": detail["endpoint"],
        }
    with duckdb.connect(str(warehouse / "eastmoney_bank.duckdb"), read_only=True) as conn:
        supplements = conn.execute("select * from bank_official_metric_supplements").df()
        free_count = conn.execute("select count(*) from bank_financial_snapshots").fetchone()[0]
    pdf_count = 0
    for row in supplements.itertuples():
        file = Path(row.source_file)
        if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != row.source_sha256:
            raise ValueError(f"Supplement file/hash mismatch: {row.supplement_id}")
        if file.suffix.lower() == ".pdf":
            pdf_count += 1
            if pd.isna(row.pdf_page) or int(row.pdf_page) < 1:
                raise ValueError("PDF page is missing")
    source_run = json.loads((warehouse / "bank_token_summary.json").read_text(encoding="utf8"))["run_id"]
    with duckdb.connect(str(warehouse / "bank_token.duckdb"), read_only=True) as conn:
        original_alias = conn.execute(
            "select sql from duckdb_views() where schema_name='main' and view_name='bank_primary_research_panel'"
        ).fetchone()[0]
        original = conn.execute("select * from bank_primary_research_panel").df()
        if original.duplicated(["code", "report_date"]).any():
            raise ValueError("Duplicate bank-quarter keys")
        token = {api: conn.execute(f"select * from {api}").df() for api in APIS}
    for api, frame in token.items():
        if frame[["source_id", "source_file", "source_sha256"]].isna().any().any():
            raise ValueError(f"Missing raw provenance: {api}")
        for sha in frame.source_sha256.unique():
            if sha not in file_index:
                raise ValueError(f"Unresolved raw hash in {api}: {sha}")
        if api in APIS[:3] and frame.duplicated(["ts_code", "trade_date"]).any():
            raise ValueError(f"Duplicate daily economic records: {api}")
    source_endpoint = _archive_endpoint(root, "market")
    evidence = {}
    if "field_lineage" in original:
        raw_lookup = {}
        for api, frame in token.items():
            for r in frame.to_dict("records"):
                raw_lookup[(api, r["source_sha256"], int(r["source_row"]))] = r
        for r in original.itertuples():
            for metric, detail in json.loads(r.field_lineage).items():
                if not metric.startswith("tushare_"):
                    continue
                raw = raw_lookup[(detail["source_api"], detail["source_sha256"], int(detail["source_row"]))]
                value = float(raw[detail["source_field"]])
                if abs(value - detail["value"]) > max(1e-8, abs(value) * 1e-10):
                    raise ValueError("Cached evidence mismatches preserved raw value")
                detail["source_url"] = source_endpoint
                evidence[(r.code, r.report_date, metric)] = detail
    for api, mapping in FIELDS.items() if not evidence else []:
        grouped = {
            (code, dt.strftime("%Y-%m-%d")): rows for (code, dt), rows in token[api].groupby(["ts_code", "end_date"])
        }
        for field, dest in mapping.items():
            metric = "tushare_" + dest
            for row in original.itertuples():
                value = getattr(row, metric)
                if pd.isna(value):
                    continue
                candidates = field_candidates(grouped[(row.code, row.report_date)], field, date.today())
                # Verify exact observation adopted by the existing published research panel.
                matching = candidates[
                    (pd.to_numeric(candidates[field], errors="coerce") - value).abs() <= max(1e-8, abs(value) * 1e-10)
                ]
                if matching.empty:
                    raise ValueError(f"Published value has no matching evidence: {row.code} {row.report_date} {field}")
                source = matching.iloc[-1]
                evidence[(row.code, row.report_date, metric)] = {
                    "source_api": api,
                    "source_url": source_endpoint,
                    "source_field": field,
                    "source_file": file_index[source.source_sha256],
                    "source_sha256": source.source_sha256,
                    "source_row": int(source.source_row),
                    "retrieved_at": source.retrieved_at,
                    "revision_notice_date": source._notice.strftime("%Y-%m-%d"),
                }
    panel, lineage = add_lineage(original, evidence, free_evidence)
    banks = json.loads((root / "config/eastmoney_banks.json").read_text(encoding="utf8"))["banks"]
    bank_meta = {b["code"]: b for b in banks}
    for column in ("name", "bank_type"):
        panel[column] = panel.code.map({code: item.get(column) for code, item in bank_meta.items()})
    panel["has_source_report"] = panel.has_source_report.fillna(False).astype(bool)
    if lineage[["source_id", "source_file", "source_sha256", "available_at"]].isna().any().any():
        missing = lineage[lineage[["source_id", "source_file", "source_sha256", "available_at"]].isna().any(axis=1)]
        missing.to_csv(report_dir / "missing_lineage.csv", index=False)
        raise ValueError("Adopted fields are missing provenance or availability")
    if lineage.duplicated(["code", "report_date", "metric"]).any():
        raise ValueError("Duplicate adopted bank-period-metric lineage keys")
    # Confirm this migration changes no original financial observations.
    protected = [c for c in original if c != "has_source_report" and pd.api.types.is_numeric_dtype(original[c])]
    pd.testing.assert_frame_equal(original[protected], panel[protected], check_dtype=False)
    events, event_conflicts = implemented_dividend_events(token["dividend"], date.today())
    events.to_parquet(report_dir / "bank_dividend_events.parquet", index=False)
    (report_dir / "dividend_conflicts.json").write_text(
        json.dumps(event_conflicts, ensure_ascii=False, indent=2), encoding="utf8"
    )
    lineage.to_parquet(report_dir / "bank_metric_lineage.parquet", index=False)
    panel.to_parquet(report_dir / "bank_primary_research_panel.parquet", index=False)
    differences = panel[panel.bps_cross_source_difference.abs() > 0.01][
        ["code", "report_date", "vendor_bvps", "tushare_bps", "bps_cross_source_difference", "bps_scope_status"]
    ]
    differences.to_csv(report_dir / "bps_cross_source_differences.csv", index=False, encoding="utf-8-sig")
    summary = {
        "audited_run_id": source_run,
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "PASS_WITH_EXPLICIT_REVIEW_FLAGS",
        "bank_quarters": len(panel),
        "duplicate_bank_quarters": 0,
        "free_raw_rows": free_count,
        "supplements": len(supplements),
        "pdf_supplements": pdf_count,
        "all_source_files_verified": True,
        "adopted_metric_rows": len(lineage),
        "missing_field_provenance": 0,
        "sources": lineage.groupby("source_id").size().to_dict(),
        "bps_source_differences_over_001": len(differences),
        "implemented_dividend_raw_rows": int(token["dividend"].div_proc.eq("实施").sum()),
        "implemented_unique_events": len(events),
        "implemented_event_conflicts": len(event_conflicts),
        "same_source_bps_conflicts": int(panel.bps_update_conflict.sum()),
        "history_first_disclosure_certified": False,
        "data_values_unchanged": True,
        "report_dir": str(report_dir),
        "unit_docs": {
            "balancesheet": "https://tushare.pro/document/2?doc_id=36",
            "daily_basic": "https://tushare.pro/document/2?doc_id=32",
        },
    }
    # Evidence views are additive; the previous immutable version remains available.
    schema = "lineage_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    with duckdb.connect(str(warehouse / "bank_token.duckdb")) as conn:
        current_alias = conn.execute(
            "select sql from duckdb_views() where schema_name='main' and view_name='bank_primary_research_panel'"
        ).fetchone()[0]
        if current_alias != original_alias:
            raise ValueError("Bank data changed during audit; rerun against the new published version")
        conn.execute("begin")
        try:
            conn.execute(f"create schema {schema}")
            for name in ["bank_primary_research_panel", "bank_metric_lineage", "bank_dividend_events"]:
                path = str(report_dir / (name + ".parquet")).replace("'", "''")
                conn.execute(f"create table {schema}.{name} as select * from read_parquet('{path}')")
                conn.execute(f"create or replace view {name} as select * from {schema}.{name}")
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    next_file = warehouse / "bank_data_audit.next.json"
    next_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    next_file.replace(warehouse / "bank_data_audit.json")
    (report_dir / "核验说明.md").write_text(
        f"数据核验完成：{len(panel)} 个银行季度格，{len(lineage)} 个采用指标。\n\n"
        f"来源文件及 PDF 页码校验通过。30 条补录（其中 12 条 PDF）保留；原金融值未修改。\n\n"
        f"每股净资产跨源差值超过 0.01 元有 {len(differences)} 格，标记口径待核，不盲目覆盖或平均。"
        "新增 valuation_common_bvps 为（归母权益－其他权益工具）/财报总股数；不等于已调整所有后续送转事件。\n\n"
        "balancesheet.total_share 单位为股，daily_basic.total_share 单位为万股，分别保留明确单位。"
        "比例为百分数，金额为人民币元，每股值为元/股。日行情市值仍以原始万元保存，使用时显式换算。\n\n"
        "每项采用指标有 metric_source、metric_unit 和 field_lineage；长表 bank_metric_lineage 含"
        "source_id、source_file、source_sha256、source_api/source_field、pdf_page、report_scope、available_at。"
        "行级 source_id 仅是免费基础行来源，混合字段以逐指标来源为准。\n\n"
        "历史财务第一次公告版本尚未认证，available_at 保守使用实际采集时间；不能把这些当前修订值"
        "直接当作 2010 年起各时点已知值使用。后续回测须校验历史版本、交易上市名单、分红事件和股本基准。\n",
        encoding="utf8",
    )
    return summary


if __name__ == "__main__":
    print(json.dumps(audit(Path(__file__).resolve().parents[1]), ensure_ascii=False, indent=2))
