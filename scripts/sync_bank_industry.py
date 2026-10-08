"""Incremental bank-only Token data collection; immutable evidence and atomic publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import requests

from scripts.bank_data_lineage import add_lineage, implemented_dividend_events
from scripts.data_update import _archive_endpoint, latest_complete_day, tushare_token

APIS = ("daily", "adj_factor", "daily_basic", "dividend", "fina_indicator", "income", "balancesheet", "cashflow")
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


def sync(root: Path, end: date, workers: int, min_free_gb: float):
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
    old = {}
    if database.exists():
        with duckdb.connect(str(database), read_only=True) as conn:
            for api in APIS:
                old[api] = conn.execute(f"select * from {api}").df()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    folder = root / "data/tushare_bank_archive" / run_id
    folder.mkdir(parents=True)
    retrieved = datetime.now(UTC).isoformat()
    tasks = [(bank["code"], api) for bank in banks for api in APIS]

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
        for api, frame, result in pool.map(fetch, tasks):
            collected[api].append(frame)
            results.append(result)
            print(f"Completed {len(results)}/{len(tasks)} bank endpoints", flush=True)
    merged = {}
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
    from scripts.sync_eastmoney_banks import bank_inventory
    from scripts.sync_eastmoney_banks import sync as sync_free

    free_status = bank_inventory(root)
    if free_status.get("snapshot_day") != date.today().isoformat() or (free_status.get("snapshot_through") or "") < str(
        end
    ):
        result = sync_free(root, end=end, workers=min(2, workers), min_free_gb=min_free_gb)
        if not result.get("published"):
            raise ValueError("Free bank supplement refresh failed; preserving published bank version")
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
    for api, mapping in FIELDS.items():
        frame = merged[api]
        grouped = {(code, dt.strftime("%Y-%m-%d")): rows for (code, dt), rows in frame.groupby(["ts_code", "end_date"])}
        for field, dest in mapping.items():
            values = []
            notices = []
            for row in panel.itertuples():
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
            panel["tushare_" + dest] = values
            panel["tushare_" + dest + "_available_at"] = notices
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
    for metadata in (root / "data/eastmoney_bank_archive/runs").glob("*/raw/*.metadata.json"):
        detail = json.loads(metadata.read_text(encoding="utf8"))
        free_evidence[detail["raw_sha256"]] = {
            "source_file": str(metadata.with_name(metadata.name.replace(".metadata.json", ".json"))),
            "source_url": free_endpoint,
        }
    panel, lineage = add_lineage(panel, token_evidence, free_evidence)
    panel.to_parquet(folder / "bank_primary_research_panel.parquet", index=False)
    lineage.to_parquet(folder / "bank_metric_lineage.parquet", index=False)
    dividend_events, event_conflicts = implemented_dividend_events(merged["dividend"], end)
    dividend_events.to_parquet(folder / "bank_dividend_events.parquet", index=False)
    conflicts.extend(event_conflicts)
    (folder / "conflicts.json").write_text(json.dumps(conflicts, ensure_ascii=False, indent=2), encoding="utf8")
    (folder / "manifest.json").write_text(
        json.dumps(
            {"run_id": run_id, "requested_end": str(end), "source_endpoint": endpoint, "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf8",
    )
    # All 336 collections must succeed before changing any published alias.
    schema = "run_" + run_id.replace("-", "_")
    with duckdb.connect(str(database)) as conn:
        conn.execute("begin")
        try:
            conn.execute(f"create schema {schema}")
            for api in [*APIS, "bank_primary_research_panel", "bank_metric_lineage", "bank_dividend_events"]:
                path = str(folder / f"{api}.parquet").replace("'", "''")
                conn.execute(f"create table {schema}.{api} as select * from read_parquet('{path}')")
                conn.execute(f"create or replace view {api} as select * from {schema}.{api}")
                if api in APIS[:3]:
                    conn.execute(
                        f"create or replace view {api}_complete as select * from {schema}.{api} "
                        f"where trade_date <= DATE '{end}'"
                    )
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise
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
            "expected_banks": len(banks),
            "rows": sum(len(f) for f in frames),
            "start": dates.min().strftime("%Y-%m-%d"),
            "end": dates.max().strftime("%Y-%m-%d"),
            "collected_at": retrieved,
            "quality_label": "Token主源；原始版本保留；历史首次披露待核",
        }
    summary = {
        "published": True,
        "run_id": run_id,
        "groups": groups,
        "bank_database": str(database),
        "complete_cutoff_date": str(end),
        "source_precedence": ["tushare_compatible_token", "free_missing_fields_only"],
        "conflict_fields": len(conflicts),
    }
    temp = warehouse / "bank_token_summary.next.json"
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
    temp.replace(warehouse / "bank_token_summary.json")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--min-free-gb", type=float, default=10)
    args = parser.parse_args()
    sync(Path(__file__).resolve().parents[1], args.end, args.workers, args.min_free_gb)
