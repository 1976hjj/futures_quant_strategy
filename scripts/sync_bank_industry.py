"""Incremental bank-only Token data collection; immutable evidence and atomic publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import requests

from scripts.bank_data_lineage import add_lineage, implemented_dividend_events
from scripts.data_update import _archive_endpoint, latest_complete_day, tushare_token

APIS = ("daily", "adj_factor", "daily_basic", "dividend", "fina_indicator", "income", "balancesheet", "cashflow")
API_NAMES = dict(zip(APIS, ("日行情", "复权系数", "日估值", "分红公告", "财务指标", "利润表", "资产负债表", "现金流量表")))
FIELD_NAMES = {
    "bps": "每股净资产", "eps": "每股收益", "roe_pct": "净资产收益率", "roe_waa_pct": "加权净资产收益率",
    "roa_annual_pct": "资产收益率", "loans_advances_carrying": "贷款账面额", "customer_deposits_carrying": "客户存款",
    "total_assets": "总资产", "total_liab": "总负债", "parent_equity": "归母净资产", "other_equity_tools": "其他权益工具",
    "report_shares": "报告期股本", "parent_profit": "归母净利润", "revenue": "营业收入", "interest_income": "利息收入",
    "interest_expense": "利息支出", "admin_expense": "管理费用", "operating_cashflow": "经营现金流",
}
Fina_FIELDS = "ts_code,ann_date,end_date,bps,eps,roe,roe_waa,roa_yearly,roa2_yearly,roe_yearly,update_flag"
FIELDS = {
    "fina_indicator": {
        "bps": "bps",
        "eps": "eps",
        "roe": "roe_pct",
        "roe_waa": "roe_waa_pct",
        "roa_yearly": "roa_annual_pct",
    },
    "balancesheet": {
        "decr_in_disbur": "loans_advances_carrying",
        "depos": "customer_deposits_carrying",
        "total_assets": "total_assets",
        "total_liab": "total_liab",
        "total_hldr_eqy_exc_min_int": "parent_equity",
        "oth_eqt_tools": "other_equity_tools",
        "total_share": "report_shares",
    },
    "income": {
        "n_income_attr_p": "parent_profit",
        "revenue": "revenue",
        "int_income": "interest_income",
        "int_exp": "interest_expense",
        "admin_exp": "admin_expense",
    },
    "cashflow": {"n_cashflow_act": "operating_cashflow"},
}

PROGRESS_STAGES = (
    ("prepare", "读取已有归档", 0, 3),
    ("token", "采集银行接口", 3, 60),
    ("merge", "合并接口历史", 60, 65),
    ("free", "免费来源补充", 65, 75),
    ("facts", "整合财务字段与检查冲突", 75, 92),
    ("lineage", "核对字段来源", 92, 95),
    ("dividends", "整理分红事件", 95, 97),
    ("publish", "入库并发布新版本", 97, 100),
)


class BankSyncProgress:
    """Actual work counters; weighted workflow percentage is not a time estimate."""
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.RLock()
        self.parent = json.loads(path.read_bytes()) if path and path.exists() else {}
        self.stages = [{"id": key, "name": name, "status": "PENDING"}
                       for key, name, *_ in PROGRESS_STAGES]
        self.current = 0
        self.started_at = self.parent.get("started_at") or datetime.now().astimezone().isoformat()

    def report(self, key, completed=0, total=None, unit="项", detail=""):
        from scripts.sync_eastmoney_banks import atomic_json
        with self.lock:
            index = next(i for i, row in enumerate(PROGRESS_STAGES) if row[0] == key)
            if index != self.current:
                print(f"STAGE {index + 1}/{len(PROGRESS_STAGES)} {PROGRESS_STAGES[index][1]}", flush=True)
            self.current = index
            for previous in self.stages[:index]:
                previous["status"] = "DONE"
            fraction = min(1, completed / total) if total else 0
            _, name, start, end = PROGRESS_STAGES[index]
            self.stages[index].update(status="RUNNING",
                                      completed=completed, total=total, unit=unit)
            # The enclosing job, not a child stage, alone declares PASS / 100%.
            percent = min(99, round(start + (end - start) * fraction, 1))
            base = self.parent.get("completed_groups", 0)
            groups = max(1, self.parent.get("total_groups", 1))
            value = {**self.parent, "status": "RUNNING", "phase": name,
                     "progress": min(99, round((base + percent / 100) / groups * 100, 1)),
                     "stage_id": key, "stage_index": index + 1, "stage_count": len(self.stages),
                     "stage_completed": completed, "stage_total": total, "stage_unit": unit,
                     "stage_progress": round(fraction * 100, 1) if total else None,
                     "stages": self.stages, "detail": detail, "started_at": self.started_at,
                     "updated_at": datetime.now().astimezone().isoformat()}
            if self.path:
                atomic_json(self.path, value)

    def working(self, detail):
        with self.lock:
            row = self.stages[self.current]
            self.report(row["id"], row.get("completed", 0), row.get("total"), row.get("unit", "项"), detail)

    def advance(self, detail):
        with self.lock:
            row = self.stages[self.current]
            self.report(row["id"], row.get("completed", 0) + 1, row.get("total"), row.get("unit", "项"), detail)


def field_candidates(frame, field, cutoff):
    """Latest disclosed non-null field; prefer update_flag, abstain on conflicts."""
    if field not in frame:
        return frame.iloc[:0]
    rows = frame[frame[field].notna()].copy()
    if "report_type" in rows:
        rows = rows[rows.report_type.astype(str).isin(["1", "1.0"])]
    notice = rows.get("f_ann_date", rows.ann_date).fillna(rows.ann_date)
    rows = rows.assign(_notice=pd.to_datetime(notice))
    rows = rows[rows._notice.notna() & (rows._notice <= pd.Timestamp(cutoff))]
    if rows.empty:
        return rows
    rows = rows[rows._notice == rows._notice.max()]
    if "update_flag" in rows and rows.update_flag.astype(str).eq("1").any():
        rows = rows[rows.update_flag.astype(str).eq("1")]
    if "retrieved_at" in rows and rows.retrieved_at.notna().any():
        rows = rows[rows.retrieved_at == rows.retrieved_at.max()]
    return rows


def resolve_field(frame, field, cutoff):
    rows = field_candidates(frame, field, cutoff)
    if rows.empty:
        return None, False, None
    vals = pd.to_numeric(rows[field], errors="coerce").dropna().unique()
    return (float(vals[0]) if len(vals) == 1 else None), len(vals) > 1, rows._notice.max().strftime("%Y-%m-%d")


def bank_refresh_policy(root: Path, mode: str = "AUTO", now=None):
    if mode not in ("AUTO", "FULL"):
        raise ValueError("Unknown bank update mode")
    path = root / "data/warehouse/bank_token_summary.json"
    summary = json.loads(path.read_bytes()) if path.exists() else {}
    stamp = summary.get("groups", {}).get("financial", {}).get("collected_at")
    now = now or datetime.now(UTC)
    try:
        collected = datetime.fromisoformat(stamp)
        age = (now - collected).total_seconds() / 86400
    except (TypeError, ValueError):
        age = None
    required = mode == "FULL" or not (root / "data/warehouse/bank_token.duckdb").exists() or age is None or age >= 7 or age < 0
    return {"refresh_financial": required, "financial_age_days": age,
            "reason": "手动完整检查" if mode == "FULL" else "财务首次采集或7天检查到期" if required else "近期财务已检查，复用本地版本"}


def sync(root: Path, end: date, workers: int, min_free_gb: float, progress_path: Path | None = None, update_mode="AUTO"):
    progress = BankSyncProgress(progress_path)
    progress.report("prepare", 0, len(APIS), detail="读取当前银行库，准备增量范围")
    if end > latest_complete_day():
        raise ValueError("Market cutoff is not complete")
    if shutil.disk_usage(root).free < min_free_gb * 1024**3:
        raise ValueError("Insufficient disk space")
    token = tushare_token(root)
    if not token:
        raise ValueError("Token required")
    endpoint = _archive_endpoint(root, "market")
    banks = json.loads((root / "config/eastmoney_banks.json").read_text(encoding="utf8"))["banks"]
    warehouse = root / "data/warehouse"
    database = warehouse / "bank_token.duckdb"
    previous_summary_path = warehouse / "bank_token_summary.json"
    previous_summary = json.loads(previous_summary_path.read_bytes()) if previous_summary_path.exists() else {}
    policy = bank_refresh_policy(root, update_mode)
    refresh_financial = policy["refresh_financial"]
    old = {}
    if database.exists():
        with duckdb.connect(str(database), read_only=True) as conn:
            for api in APIS:
                old[api] = conn.execute(f"select * from {api}").df()
                progress.advance(f"已读取 {api}，{len(old[api]):,} 条历史记录")
    progress.report("prepare", len(APIS), len(APIS), detail="已有归档读取完成")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    folder = root / "data/tushare_bank_archive" / run_id
    folder.mkdir(parents=True)
    retrieved = datetime.now(UTC).isoformat()
    selected_apis = APIS if refresh_financial else APIS[:3]
    tasks = [(bank["code"], api) for bank in banks for api in selected_apis]
    bank_names = {bank["code"]: bank.get("name", bank["code"]) for bank in banks}
    progress.report("token", 0, len(tasks), "个接口任务", f"{policy['reason']}；每家采集 {len(selected_apis)} 类接口")

    def fetch(task):
        code, api = task
        previous = old.get(api, pd.DataFrame())
        subset = previous[previous.ts_code == code] if not previous.empty else previous
        start = date(2010, 1, 1)
        if not subset.empty:
            if api in APIS[:3]:
                start = pd.to_datetime(subset.trade_date).max().date() - timedelta(days=7)
            elif api != "dividend":
                start = pd.to_datetime(subset.end_date).max().date() - timedelta(days=400)
        params = {"ts_code": code, "limit": 100 if api in APIS[4:] else 5000}
        if api != "dividend":
            params.update(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        frames, pages, seen = [], [], set()
        for offset in range(0, 100000, params["limit"]):
            query = dict(params, offset=offset)
            for attempt in range(3):
                progress.working(f"{bank_names[code]} {code} · {API_NAMES[api]} · 第{offset // params['limit'] + 1}页 · 请求尝试{attempt + 1}/3")
                try:
                    response = requests.post(
                        endpoint,
                        json={
                            "api_name": api,
                            "token": token,
                            "params": query,
                            "fields": Fina_FIELDS if api == "fina_indicator" else "",
                        },
                        timeout=(15, 60),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("code") != 0:
                        raise ValueError(f"{api} returned source error code {payload.get('code')}")
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt * 2)
            data = payload.get("data") or {}
            fields = data.get("fields", [])
            items = data.get("items", [])
            digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            if items and digest in seen:
                raise ValueError(f"Repeated pagination: {api} {code}")
            seen.add(digest)
            path = folder / f"{api}_{code}_{offset}.json"
            raw = response.content
            path.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            pages.append(
                {"path": str(path), "sha256": sha, "rows": len(items), "params": query, "retrieved_at": retrieved}
            )
            frame = pd.DataFrame(items, columns=fields)
            if not frame.empty:
                if not frame.ts_code.eq(code).all():
                    raise ValueError("Response bank code mismatch")
                for col in frame.columns:
                    if col.endswith("date"):
                        frame[col] = pd.to_datetime(frame[col], format="%Y%m%d", errors="coerce")
                frame = frame.assign(
                    source_api=api,
                    source_id="tushare_compatible_token",
                    source_file=str(path),
                    source_sha256=sha,
                    source_row=range(len(frame)),
                    retrieved_at=retrieved,
                )
                frames.append(frame)
            if len(items) < params["limit"]:
                break
        else:
            raise ValueError("Pagination limit exceeded")
        return (
            api,
            pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(),
            {"api": api, "code": code, "status": "PASS", "pages": pages},
        )

    collected = {api: [] for api in APIS}
    results = []
    with ThreadPoolExecutor(max_workers=min(2, workers)) as pool:
        pending = [pool.submit(fetch, task) for task in tasks]
        for future in as_completed(pending):
            api, frame, result = future.result()
            collected[api].append(frame)
            results.append(result)
            progress.advance(f"已完成 {bank_names[result['code']]} · {API_NAMES[api]}，采集 {len(frame):,} 条")
            print(f"Completed {len(results)}/{len(tasks)} bank endpoints", flush=True)
    merged = {}
    progress.report("merge", 0, len(APIS), "类接口", "合并本次增量与已有历史，保留来源版本")
    for api in APIS:
        parts = [old.get(api, pd.DataFrame()), *collected[api]]
        frame = pd.concat([x for x in parts if not x.empty], ignore_index=True)
        if api in APIS[:3]:
            frame = frame.drop_duplicates(["ts_code", "trade_date"], keep="last")
        else:
            columns = [col for col in frame if not col.startswith("source_") and col != "retrieved_at"]
            frame = frame.drop_duplicates(columns, keep="last")
        merged[api] = frame
        frame.to_parquet(folder / f"{api}.parquet", index=False)
        progress.advance(f"已保存 {api}，合并后 {len(frame):,} 条")
    from scripts.sync_eastmoney_banks import bank_inventory
    from scripts.sync_eastmoney_banks import sync as sync_free

    free_status = bank_inventory(root)
    progress.report("free", 0, len(banks), "家银行", "检查免费补充源是否需要更新")
    if (refresh_financial and (update_mode == "FULL" or free_status.get("snapshot_day") != date.today().isoformat()
                              or (free_status.get("snapshot_through") or "") < str(end))) or not free_status.get("published", bool(free_status.get("snapshot_day"))):
        result = sync_free(root, end=end, workers=min(2, workers), min_free_gb=min_free_gb,
                           progress_callback=lambda done, total, detail: progress.report("free", done, total, "家银行", detail))
        if not result.get("published"):
            raise ValueError("Free bank supplement refresh failed; preserving published bank version")
    progress.report("free", len(banks), len(banks), "家银行", "免费补充源已可用；整项更新仍需整合并入库")
    if not refresh_financial:
        # Financial inputs do not depend on today's quotes. Reuse their actual
        # source evidence and collected_at, without pretending to reacquire it.
        with duckdb.connect(str(database), read_only=True) as c:
            for table in ("bank_primary_research_panel", "bank_metric_lineage", "bank_dividend_events"):
                c.execute(f"COPY (SELECT * FROM {table}) TO ? (FORMAT PARQUET)", [str(folder / f"{table}.parquet")])
        prior_run = previous_summary.get("run_id")
        prior_conflicts = root / "data/tushare_bank_archive" / str(prior_run) / "conflicts.json"
        (folder / "conflicts.json").write_bytes(prior_conflicts.read_bytes() if prior_conflicts.exists() else b"[]")
        reuse_reason = "复用已有财务、分红及字段来源；采集时间保持原值"
        for stage in ("free", "facts", "lineage", "dividends"):
            next(row for row in progress.stages if row["id"] == stage)["reused"] = True
            progress.report(stage, 0, None, detail=reuse_reason)
        return publish_bank_version(root, end, run_id, folder, warehouse, database, merged, results,
                                    retrieved, previous_summary.get("conflict_fields", 0), progress,
                                    previous_summary=previous_summary, financial_refreshed=False, update_mode=update_mode, endpoint=endpoint)
    # Rebuild the research view from free evidence, retaining nulls and field provenance.
    with duckdb.connect(str(warehouse / "eastmoney_bank.duckdb"), read_only=True) as free:
        panel = free.execute("select * from bank_financial_research_panel_2010_verified_all").df()
    periods = pd.period_range("2010Q1", pd.Timestamp(end).to_period("Q"), freq="Q")
    periods = [p.end_time.strftime("%Y-%m-%d") for p in periods if p.end_time.date() <= end]
    index = pd.MultiIndex.from_product([[b["code"] for b in banks], periods], names=["code", "report_date"])
    panel = panel.set_index(["code", "report_date"]).reindex(index).reset_index()
    bank_meta = {b["code"]: b for b in banks}
    for column in ("name", "bank_type"):
        panel[column] = panel.code.map({code: item.get(column) for code, item in bank_meta.items()})
    if "has_source_report" in panel:
        panel["has_source_report"] = panel.has_source_report.fillna(False).astype(bool)
    conflicts = []
    token_evidence = {}
    field_total = sum(len(mapping) for mapping in FIELDS.values())
    completed_fields = 0
    progress.report("facts", 0, field_total * len(panel), "个字段记录", f"处理 {len(panel):,} 个银行报告期、{field_total} 个字段")
    for api, mapping in FIELDS.items():
        frame = merged[api]
        grouped = {(code, dt.strftime("%Y-%m-%d")): rows for (code, dt), rows in frame.groupby(["ts_code", "end_date"])}
        for field, dest in mapping.items():
            values = []
            notices = []
            for row_index, row in enumerate(panel.itertuples(), 1):
                rows = grouped.get((row.code, row.report_date))
                value, conflict, notice = resolve_field(rows, field, end) if rows is not None else (None, False, None)
                values.append(value)
                notices.append(notice)
                if value is not None:
                    origin = field_candidates(rows, field, end).iloc[-1]
                    token_evidence[(row.code, row.report_date, "tushare_" + dest)] = {
                        "source_api": api,
                        "source_url": endpoint,
                        "source_field": field,
                        "source_file": origin.get("source_file"),
                        "source_sha256": origin.get("source_sha256"),
                        "source_row": int(origin.source_row),
                        "retrieved_at": origin.get("retrieved_at"),
                        "revision_notice_date": notice,
                    }
                if conflict:
                    conflicts.append({"code": row.code, "period": row.report_date, "field": field})
                if row_index % 50 == 0 or row_index == len(panel):
                    progress.report("facts", completed_fields * len(panel) + row_index,
                                    field_total * len(panel), "个字段记录",
                                    f"{FIELD_NAMES[dest]} · {bank_names[row.code]} · {row.report_date}；已发现 {len(conflicts)} 项冲突，冲突字段保留缺失")
            panel["tushare_" + dest] = values
            panel["tushare_" + dest + "_available_at"] = notices
            completed_fields += 1
    bad = {(r["code"], r["period"]) for r in conflicts if r["field"] == "bps"}
    panel["bps_update_conflict"] = [(r.code, r.report_date) in bad for r in panel.itertuples()]
    panel["selected_bvps"] = panel.tushare_bps.combine_first(panel.vendor_bvps).mask(panel.bps_update_conflict)
    panel["selected_bvps_source"] = [
        "conflict_pending"
        if r.bps_update_conflict
        else "tushare_compatible_token"
        if pd.notna(r.tushare_bps)
        else "free_missing_fields_only"
        if pd.notna(r.vendor_bvps)
        else None
        for r in panel.itertuples()
    ]
    panel["source_policy"] = "Token first for matching period/unit/scope; free fills missing only"
    panel["token_available_at"] = panel.filter(regex="^tushare_.*_available_at$").apply(
        lambda row: max((x for x in row if pd.notna(x)), default=None), axis=1
    )
    panel["token_pit_grade"] = "current_snapshot_not_verified_first_disclosure"
    from alpha_research_os.data.providers.eastmoney_bank import ENDPOINT as free_endpoint

    free_evidence = {}
    progress.report("lineage", 0, len(panel), "个银行报告期", "核对原文及接口来源证据")
    for metadata in (root / "data/eastmoney_bank_archive/runs").glob("*/raw/*.metadata.json"):
        detail = json.loads(metadata.read_text(encoding="utf8"))
        free_evidence[detail["raw_sha256"]] = {
            "source_file": str(metadata.with_name(metadata.name.replace(".metadata.json", ".json"))),
            "source_url": free_endpoint,
        }
    panel, lineage = add_lineage(panel, token_evidence, free_evidence,
                                 progress_callback=lambda done, total, detail: progress.report("lineage", done, total, "个银行报告期", detail))
    panel.to_parquet(folder / "bank_primary_research_panel.parquet", index=False)
    lineage.to_parquet(folder / "bank_metric_lineage.parquet", index=False)
    progress.report("dividends", 0, 1, "批", "核对已实施分红与事件冲突")
    dividend_events, event_conflicts = implemented_dividend_events(merged["dividend"], end)
    dividend_events.to_parquet(folder / "bank_dividend_events.parquet", index=False)
    conflicts.extend(event_conflicts)
    progress.report("dividends", 1, 1, "批", f"已整理 {len(dividend_events):,} 条分红事件，累计 {len(conflicts)} 项冲突")
    (folder / "conflicts.json").write_text(json.dumps(conflicts, ensure_ascii=False, indent=2), encoding="utf8")
    (folder / "manifest.json").write_text(
        json.dumps(
            {"run_id": run_id, "requested_end": str(end), "source_endpoint": endpoint, "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf8",
    )
    return publish_bank_version(root, end, run_id, folder, warehouse, database, merged, results,
                                retrieved, len(conflicts), progress, previous_summary=previous_summary,
                                financial_refreshed=True, update_mode=update_mode, endpoint=endpoint)


def publish_bank_version(root, end, run_id, folder, warehouse, database, merged, results,
                         retrieved, conflict_count, progress, *, previous_summary, financial_refreshed, update_mode, endpoint):
    # Every selected collection must succeed before changing any published alias.
    expected_banks = len(json.loads((root / "config/eastmoney_banks.json").read_bytes())["banks"])
    (folder / "manifest.json").write_text(json.dumps({"run_id": run_id, "requested_end": str(end),
        "results": results, "source_endpoint": endpoint, "update_mode": update_mode, "financial_refreshed": financial_refreshed,
        "reused_financial_run_id": previous_summary.get("run_id") if not financial_refreshed else None},
        ensure_ascii=False, indent=2), encoding="utf8")
    schema = "run_" + run_id.replace("-", "_")
    tables = [*APIS, "bank_primary_research_panel", "bank_metric_lineage", "bank_dividend_events"]
    progress.report("publish", 0, len(tables) + 2, "项", "开始事务入库；完成前保留原已发布版本")
    with duckdb.connect(str(database)) as conn:
        conn.execute("begin")
        try:
            conn.execute(f"create schema {schema}")
            for api in tables:
                path = str(folder / f"{api}.parquet").replace("'", "''")
                conn.execute(f"create table {schema}.{api} as select * from read_parquet('{path}')")
                conn.execute(f"create or replace view {api} as select * from {schema}.{api}")
                if api in APIS[:3]:
                    conn.execute(
                        f"create or replace view {api}_complete as select * from {schema}.{api} "
                        f"where trade_date <= DATE '{end}'"
                    )
                progress.advance(f"已写入 {api}；事务尚未提交")
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
    progress.advance("数据库事务已提交，正在发布覆盖台账")
    groups = {}
    for group, apis, column in [
        ("market", APIS[:3], "trade_date"),
        ("financial", APIS[4:], "end_date"),
        ("corporate", ["dividend"], "end_date"),
    ]:
        frames = [merged[a][pd.to_datetime(merged[a][column]) <= pd.Timestamp(end)] for a in apis]
        dates = pd.concat([f[column] for f in frames]).dropna()
        groups[group] = {
            "banks": min(f.ts_code.nunique() for f in frames),
            "expected_banks": expected_banks,
            "rows": sum(len(f) for f in frames),
            "start": dates.min().strftime("%Y-%m-%d"),
            "end": dates.max().strftime("%Y-%m-%d"),
            "collected_at": retrieved if group == "market" or financial_refreshed else previous_summary.get("groups", {}).get(group, {}).get("collected_at"),
            "quality_label": "Token主源；原始版本保留；历史首次披露待核",
        }
    summary = {
        "published": True,
        "run_id": run_id,
        "groups": groups,
        "bank_database": str(database),
        "complete_cutoff_date": str(end),
        "source_precedence": ["tushare_compatible_token", "free_missing_fields_only"],
        "conflict_fields": conflict_count,
        "update_mode": update_mode,
        "financial_refreshed": financial_refreshed,
        "requested_api_tasks": len(results),
        "reused_financial_run_id": previous_summary.get("run_id") if not financial_refreshed else None,
    }
    temp = warehouse / "bank_token_summary.next.json"
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    temp.replace(warehouse / "bank_token_summary.json")
    progress.advance(f"新版本 {run_id} 已发布；接口采集完成不等于财报全面核验")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--min-free-gb", type=float, default=10)
    parser.add_argument("--progress", type=Path)
    parser.add_argument("--update-mode", choices=("AUTO", "FULL"), default="AUTO")
    args = parser.parse_args()
    sync(Path(__file__).resolve().parents[1], args.end, args.workers, args.min_free_gb, args.progress, args.update_mode)
