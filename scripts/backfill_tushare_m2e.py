"""Checkpointed M2-E backfill for execution, benchmark, PIT-event, and ownership data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import duckdb

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.providers.tushare import TushareProvider, tushare_response_rows
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

INDEX_CODES = ("000300.SH", "000905.SH", "000852.SH", "000016.SH", "000688.SH")
CORE_APIS = (
    "index_basic",
    "index_classify",
    "index_member_all",
    "disclosure_date",
    "forecast_vip",
    "express_vip",
    "fina_mainbz_vip",
    "index_weight",
    "stk_limit",
)
IMPORTANT_APIS = (
    "share_float",
    "repurchase",
    "stk_holdertrade",
    "stk_holdernumber",
    "pledge_stat",
    "top10_holders",
    "top10_floatholders",
    "margin",
    "margin_detail",
    "margin_secs",
    "hk_hold",
)
DOMAINS = {
    "index_basic": DataDomain.UNIVERSE,
    "index_classify": DataDomain.UNIVERSE,
    "index_member_all": DataDomain.UNIVERSE,
    "index_weight": DataDomain.UNIVERSE,
    "stk_limit": DataDomain.SECURITY_STATUS,
    "disclosure_date": DataDomain.FUNDAMENTAL,
    "forecast_vip": DataDomain.FUNDAMENTAL,
    "express_vip": DataDomain.FUNDAMENTAL,
    "fina_mainbz_vip": DataDomain.FUNDAMENTAL,
    "stk_holdernumber": DataDomain.FUNDAMENTAL,
    "top10_holders": DataDomain.FUNDAMENTAL,
    "top10_floatholders": DataDomain.FUNDAMENTAL,
    "share_float": DataDomain.CORPORATE_ACTION,
    "repurchase": DataDomain.CORPORATE_ACTION,
    "stk_holdertrade": DataDomain.CORPORATE_ACTION,
    "pledge_stat": DataDomain.CORPORATE_ACTION,
    "margin": DataDomain.MARKET,
    "margin_detail": DataDomain.MARKET,
    "margin_secs": DataDomain.MARKET,
    "hk_hold": DataDomain.MARKET,
}
REQUEST_FIELD = {
    DataDomain.UNIVERSE: "index_code",
    DataDomain.SECURITY_STATUS: "ts_code",
    DataDomain.FUNDAMENTAL: "ts_code",
    DataDomain.CORPORATE_ACTION: "ts_code",
    DataDomain.MARKET: "ts_code",
}


@dataclass(frozen=True)
class Task:
    api: str
    key: str
    start: date
    end: date
    params: tuple[str, ...] = ()
    instrument: str | None = None
    page_size: int = 5000


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1 * (attempt + 1))


def _save_status(output: Path, **status: object) -> None:
    _atomic_write(
        output / "run_status.json",
        canonical_json_bytes({"updated_at": datetime.now().astimezone().isoformat(), **status}),
    )


def _retry_delay(error: Exception, attempt: int) -> float | None:
    if isinstance(error, HTTPError):
        if error.code == 429:
            return min(15.0 * (2 ** min(attempt - 1, 4)), 120.0)
        if error.code in {500, 502, 503, 504}:
            return min(5.0 * (2 ** min(attempt - 1, 4)), 60.0)
        return None
    if isinstance(error, (URLError, TimeoutError, ConnectionError, IncompleteRead, json.JSONDecodeError)):
        return min(5.0 * (2 ** min(attempt - 1, 4)), 60.0)
    return None


Observer = Callable[[str, int, Exception | None, float | None], None]


def _fetch_with_retry(provider: TushareProvider, request: FetchRequest, observer: Observer):
    attempt = 0
    while True:
        attempt += 1
        try:
            response = provider.fetch(request)
            if attempt > 1:
                observer("RUNNING", attempt, None, None)
            return response
        except Exception as error:
            delay = _retry_delay(error, attempt)
            if delay is None:
                raise
            observer("RETRYING", attempt, error, delay)
            time.sleep(delay)


def _observer(output: Path, api_name: str, partition: str) -> Observer:
    def observe(status: str, attempt: int, error: Exception | None, delay: float | None) -> None:
        payload: dict[str, object] = {
            "api_name": api_name,
            "partition": partition,
            "retry_attempt": attempt,
            "status": status,
        }
        if error is not None:
            payload.update(
                error_message=str(error),
                error_type=type(error).__name__,
                next_retry_seconds=delay,
            )
        _save_status(output, **payload)

    return observe


def _months(start: date, end: date) -> Iterable[tuple[date, date]]:
    current = date(start.year, start.month, 1)
    while current <= end:
        next_month = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
        yield max(start, current), min(end, date.fromordinal(next_month.toordinal() - 1))
        current = next_month


def _load_inputs(reference: Path, financial: Path, database: Path) -> tuple[list[str], list[str], list[str]]:
    ref = json.loads((reference / "checkpoint.json").read_bytes())
    fin = json.loads((financial / "checkpoint.json").read_bytes())
    sessions = [str(value) for value in ref.get("open_sessions", [])]
    periods = [str(value) for value in fin.get("periods", [])]
    with duckdb.connect(str(database), read_only=True) as connection:
        securities = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT ts_code FROM research.security_master WHERE ts_code IS NOT NULL ORDER BY 1"
            ).fetchall()
        ]
    if not sessions or not periods or not securities:
        raise ValueError("M2-E prerequisites are incomplete")
    return sessions, periods, securities


def _tasks(start: date, end: date, sessions: list[str], periods: list[str], securities: list[str]) -> list[Task]:
    tasks: list[Task] = []
    for market in ("MSCI", "CSI", "SSE", "SZSE", "CICC", "SW", "OTH"):
        tasks.append(Task("index_basic", f"market={market}", end, end, (f"market={market}",), page_size=5000))
    for level in ("L1", "L2", "L3"):
        tasks.append(
            Task("index_classify", f"SW2021:{level}", end, end, ("src=SW2021", f"level={level}"), page_size=2000)
        )
    tasks.append(Task("index_member_all", "SW2021:all-history", end, end, ("is_new=N",), page_size=2000))
    usable_periods = [value for value in periods if start <= datetime.strptime(value, "%Y%m%d").date() <= end]
    for period in usable_periods:
        period_date = datetime.strptime(period, "%Y%m%d").date()
        tasks.extend(
            (
                Task("disclosure_date", period, period_date, period_date, (f"end_date={period}",), page_size=6000),
                Task("forecast_vip", period, period_date, period_date, (f"period={period}",), page_size=3500),
                Task("express_vip", period, period_date, period_date, (f"period={period}",), page_size=5000),
                Task(
                    "fina_mainbz_vip",
                    f"{period}:P",
                    period_date,
                    period_date,
                    (f"period={period}", "type=P"),
                    page_size=5000,
                ),
                Task(
                    "fina_mainbz_vip",
                    f"{period}:D",
                    period_date,
                    period_date,
                    (f"period={period}", "type=D"),
                    page_size=5000,
                ),
            )
        )
    for month_start, month_end in _months(max(start, date(2005, 1, 1)), end):
        for index_code in INDEX_CODES:
            tasks.append(
                Task(
                    "index_weight",
                    f"{index_code}:{month_start:%Y%m}",
                    month_start,
                    month_end,
                    (f"index_code={index_code}", f"start_date={month_start:%Y%m%d}", f"end_date={month_end:%Y%m%d}"),
                    page_size=5000,
                )
            )
    session_dates = [
        (value, datetime.strptime(value, "%Y%m%d").date())
        for value in sessions
        if start <= datetime.strptime(value, "%Y%m%d").date() <= end
    ]
    for value, session in session_dates:
        tasks.append(Task("stk_limit", value, session, session, page_size=5800))
    for month_start, month_end in _months(start, end):
        key = f"{month_start:%Y%m}"
        common = (f"start_date={month_start:%Y%m%d}", f"end_date={month_end:%Y%m%d}")
        tasks.extend(
            (
                Task("share_float", key, month_start, month_end, common, page_size=6000),
                Task("repurchase", key, month_start, month_end, common, page_size=2000),
                Task("stk_holdertrade", key, month_start, month_end, common, page_size=3000),
                Task("stk_holdernumber", key, month_start, month_end, common, page_size=3000),
            )
        )
    for code in securities:
        tasks.extend(
            (
                Task("pledge_stat", code, start, end, instrument=code, page_size=5000),
                Task("top10_holders", code, start, end, instrument=code, page_size=5000),
                Task("top10_floatholders", code, start, end, instrument=code, page_size=5000),
            )
        )
    for value, session in session_dates:
        if session >= date(2010, 1, 1):
            tasks.extend(
                (
                    Task("margin", value, session, session, page_size=6000),
                    Task("margin_detail", value, session, session, page_size=6000),
                    Task("margin_secs", value, session, session, page_size=6000),
                )
            )
        if date(2014, 11, 17) <= session <= date(2024, 8, 19):
            tasks.append(Task("hk_hold", value, session, session, page_size=3800))
    return tasks


def _new_checkpoint(endpoint: str, start: date, end: date, task_count: int) -> dict[str, Any]:
    return {
        "api_base_url": endpoint,
        "completed": {api: {} for api in (*CORE_APIS, *IMPORTANT_APIS)},
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "expected_base_partitions": task_count,
        "schema": "tushare-m2e-backfill-v1",
    }


def _request(task: Task, offset: int) -> FetchRequest:
    domain = DOMAINS[task.api]
    return FetchRequest(
        request_id=f"M2E-{task.api.upper().replace('_', '-')}-{hashlib_key(task.key)}-{offset}",
        data_domain=domain,
        start=task.start,
        end=task.end,
        fields=(REQUEST_FIELD[domain],),
        instrument_ids=(task.instrument,) if task.instrument else (),
        parameters=(
            f"api_name={task.api}",
            "_all_fields=true",
            *task.params,
            f"limit={task.page_size}",
            f"offset={offset}",
        ),
    )


def hashlib_key(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _offset_parameter_rejected(error: Exception) -> bool:
    message = str(error).lower()
    return "code=50101" in message and "offset" in message


def _adaptive_split(task: Task, securities: list[str]) -> list[Task]:
    if task.api != "share_float":
        return []
    if task.start < task.end:
        children = []
        current = task.start
        while current <= task.end:
            value = current.strftime("%Y%m%d")
            children.append(
                Task(
                    task.api,
                    f"{task.key}:day={value}",
                    current,
                    current,
                    (f"start_date={value}", f"end_date={value}"),
                    page_size=task.page_size,
                )
            )
            current = date.fromordinal(current.toordinal() + 1)
        return children
    if task.instrument is None:
        value = task.start.strftime("%Y%m%d")
        return [
            Task(
                task.api,
                f"{task.key}:instrument={code}",
                task.start,
                task.end,
                (f"start_date={value}", f"end_date={value}"),
                instrument=code,
                page_size=task.page_size,
            )
            for code in securities
        ]
    return []


def _expand_adaptive_splits(
    tasks: list[Task],
    splits: dict[str, dict[str, object]],
    securities: list[str],
) -> list[Task]:
    expanded: list[Task] = []
    pending = list(tasks)
    while pending:
        task = pending.pop(0)
        if task.key not in splits.get(task.api, {}):
            expanded.append(task)
            continue
        children = _adaptive_split(task, securities)
        if not children:
            raise ValueError(f"recorded adaptive split cannot be reconstructed: {task.api} {task.key}")
        pending[0:0] = children
    return expanded


def backfill(
    *,
    provider: TushareProvider,
    start: date,
    end: date,
    output: Path,
    reference: Path,
    financial: Path,
    database: Path,
    min_free_gb: float,
    sleep_seconds: float,
    max_tasks: int | None = None,
) -> dict[str, Any]:
    sessions, periods, securities = _load_inputs(reference, financial, database)
    tasks = _tasks(start, end, sessions, periods, securities)
    if max_tasks is not None:
        tasks = tasks[:max_tasks]
    checkpoint_path = output / "checkpoint.json"
    output.mkdir(parents=True, exist_ok=True)
    expected = _new_checkpoint(provider.spec.api_base_url, start, end, len(tasks))
    state = json.loads(checkpoint_path.read_bytes()) if checkpoint_path.exists() else expected
    for field in ("api_base_url", "coverage", "schema"):
        if state.get(field) != expected[field]:
            raise ValueError(f"M2-E checkpoint configuration differs: {field}")
    state["expected_base_partitions"] = max(int(state.get("expected_base_partitions", 0)), len(tasks))
    for api in (*CORE_APIS, *IMPORTANT_APIS):
        state.setdefault("completed", {}).setdefault(api, {})
        state.setdefault("pagination_splits", {}).setdefault(api, {})
    tasks = _expand_adaptive_splits(tasks, state["pagination_splits"], securities)
    state["expected_base_partitions"] = max(int(state.get("expected_base_partitions", 0)), len(tasks))
    store = RawSnapshotStore(ArtifactStore(output / "artifacts"))
    fetched = 0
    task_index = 0
    while task_index < len(tasks):
        task = tasks[task_index]
        offset = 0
        split_applied = False
        while True:
            partition = f"{task.key}:offset={offset}"
            existing = state["completed"][task.api].get(partition)
            if existing is not None:
                if int(existing["rows"]) < task.page_size:
                    break
                offset += task.page_size
                continue
            free_gb = shutil.disk_usage(output).free / (1024**3)
            if free_gb < min_free_gb:
                raise RuntimeError(f"disk safety stop: {free_gb:.2f} GiB is below {min_free_gb:.2f} GiB")

            try:
                response = _fetch_with_retry(
                    provider,
                    _request(task, offset),
                    _observer(output, task.api, partition),
                )
            except Exception as error:
                children = _adaptive_split(task, securities) if _offset_parameter_rejected(error) else []
                if not children:
                    raise
                state["pagination_splits"][task.api][task.key] = {
                    "child_count": len(children),
                    "reason": "provider rejected high offset; replaced by narrower immutable partitions",
                    "recorded_at": datetime.now().astimezone().isoformat(),
                }
                tasks[task_index : task_index + 1] = children
                state["expected_base_partitions"] = max(state["expected_base_partitions"], len(tasks))
                _atomic_write(checkpoint_path, canonical_json_bytes(state))
                _save_status(
                    output,
                    api_name=task.api,
                    completed_partitions=sum(len(value) for value in state["completed"].values()),
                    expected_partitions=state["expected_base_partitions"],
                    partition=partition,
                    status="RUNNING",
                )
                print(
                    f"M2-E adaptive pagination api={task.api} partition={partition} "
                    f"replacement_tasks={len(children)}",
                    flush=True,
                )
                split_applied = True
                break
            document = json.loads(response.payload)
            fields = document.get("data", {}).get("fields")
            rows = tushare_response_rows(response.payload)
            snapshot = store.capture(provider.spec, response, storage_encoding="gzip")
            state["completed"][task.api][partition] = {
                "fields": fields,
                "page_size": task.page_size,
                "payload_artifact_id": snapshot.reference.payload_artifact_id,
                "raw_bytes": snapshot.reference.uncompressed_byte_size,
                "retrieved_at": snapshot.reference.retrieved_at.isoformat(),
                "rows": len(rows),
                "snapshot_id": snapshot.reference.snapshot_id,
                "stored_bytes": snapshot.payload_reference.byte_size,
            }
            _atomic_write(checkpoint_path, canonical_json_bytes(state))
            fetched += 1
            completed = sum(len(value) for value in state["completed"].values())
            full_pages = sum(
                int(entry["rows"]) == int(entry.get("page_size", 0))
                for entries in state["completed"].values()
                for entry in entries.values()
            )
            _save_status(
                output,
                api_name=task.api,
                completed_partitions=completed,
                expected_partitions=state["expected_base_partitions"] + full_pages,
                free_gb=round(free_gb, 3),
                partition=partition,
                status="RUNNING",
            )
            if fetched == 1 or fetched % 10 == 0 or task_index + 1 == len(tasks):
                progress = f"M2-E progress {task_index + 1}/{len(tasks)} pages={completed}"
                detail = f"api={task.api} partition={partition} rows={len(rows)}"
                print(f"{progress} {detail}", flush=True)
            if sleep_seconds:
                time.sleep(sleep_seconds)
            if len(rows) < task.page_size:
                break
            offset += task.page_size
        if not split_applied:
            task_index += 1
    totals = {
        api: {"partitions": len(entries), "rows": sum(int(entry["rows"]) for entry in entries.values())}
        for api, entries in state["completed"].items()
    }
    summary = {"coverage": state["coverage"], "fetched_this_run": fetched, "totals": totals}
    _atomic_write(output / "latest_summary.json", canonical_json_bytes(summary))
    _save_status(output, status="COMPLETED", summary=summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(1990, 12, 31))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--endpoint", default="https://api.tushare.pro/")
    parser.add_argument("--output", type=Path, default=Path("data/tushare_m2e_archive"))
    parser.add_argument("--reference", type=Path, default=Path("data/tushare_reference_archive"))
    parser.add_argument("--financial", type=Path, default=Path("data/tushare_financial_archive"))
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--sleep-ms", type=float, default=100.0)
    parser.add_argument("--max-tasks", type=int)
    args = parser.parse_args()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        parser.error("TUSHARE_TOKEN is not set")
    try:
        summary = backfill(
            provider=TushareProvider(token=token, api_base_url=args.endpoint),
            start=args.start,
            end=args.end,
            output=args.output,
            reference=args.reference,
            financial=args.financial,
            database=args.database,
            min_free_gb=args.min_free_gb,
            sleep_seconds=args.sleep_ms / 1000,
            max_tasks=args.max_tasks,
        )
    except Exception as error:
        _save_status(
            args.output,
            error_message=str(error),
            error_type=type(error).__name__,
            status="FAILED",
            traceback=traceback.format_exc(limit=8),
        )
        raise
    sys.stdout.buffer.write(canonical_json_bytes(summary) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
