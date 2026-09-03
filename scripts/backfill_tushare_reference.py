"""Checkpointed M2-B backfill for A-share reference and historical status data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import date, datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.providers.tushare import TushareProvider, tushare_response_rows
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

FIELDS = {
    "trade_cal": ("exchange", "cal_date", "is_open", "pretrade_date"),
    "stock_basic": (
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "fullname",
        "enname",
        "cnspell",
        "market",
        "exchange",
        "curr_type",
        "list_status",
        "list_date",
        "delist_date",
        "is_hs",
        "act_name",
        "act_ent_type",
    ),
    "namechange": ("ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"),
    "stock_st": ("ts_code", "name", "trade_date", "type", "type_name"),
    "suspend_d": ("ts_code", "trade_date", "suspend_type", "suspend_timing"),
}

DOMAINS = {
    "trade_cal": DataDomain.TRADING_CALENDAR,
    "stock_basic": DataDomain.SECURITY_MASTER,
    "namechange": DataDomain.SECURITY_STATUS,
    "stock_st": DataDomain.SECURITY_STATUS,
    "suspend_d": DataDomain.SECURITY_STATUS,
}

LIST_STATUSES = ("L", "D", "P", "G", "UN")
ST_COVERAGE_START = date(2000, 1, 1)


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


RetryObserver = Callable[[str, int, Exception | None, float | None], None]


def _fetch_with_retry(
    provider: TushareProvider,
    request: FetchRequest,
    *,
    attempts: int | None = 10,
    observer: RetryObserver | None = None,
):
    attempt = 0
    while True:
        attempt += 1
        try:
            response = provider.fetch(request)
            if observer is not None and attempt > 1:
                observer("RUNNING", attempt, None, None)
            return response
        except Exception as error:
            delay = _retry_delay(error, attempt)
            if delay is None or (attempts is not None and attempt >= attempts):
                raise
            if observer is not None:
                observer("RETRYING", attempt, error, delay)
            time.sleep(delay)


def _new_checkpoint(endpoint: str, start: date, end: date) -> dict[str, Any]:
    return {
        "api_base_url": endpoint,
        "completed": {api_name: {} for api_name in FIELDS},
        "coverage": {"end": end.isoformat(), "start": start.isoformat()},
        "open_sessions": [],
        "schema": "tushare-reference-backfill-v1",
    }


def _load_checkpoint(path: Path, *, endpoint: str, start: date, end: date) -> dict[str, Any]:
    if not path.exists():
        return _new_checkpoint(endpoint, start, end)
    state = json.loads(path.read_bytes())
    if state.get("schema") != "tushare-reference-backfill-v1":
        raise ValueError("unsupported M2-B checkpoint schema")
    if state.get("api_base_url") != endpoint:
        raise ValueError("M2-B checkpoint belongs to another endpoint")
    if state.get("coverage") != {"end": end.isoformat(), "start": start.isoformat()}:
        raise ValueError("coverage changes require a new M2-B archive or an explicit migration")
    for api_name in FIELDS:
        state.setdefault("completed", {}).setdefault(api_name, {})
    return state


def _save_status(output: Path, **status: object) -> None:
    _atomic_write(
        output / "run_status.json",
        canonical_json_bytes({"updated_at": datetime.now().astimezone().isoformat(), **status}),
    )


def _request(
    *,
    api_name: str,
    request_id: str,
    start: date,
    end: date,
    parameters: tuple[str, ...],
) -> FetchRequest:
    return FetchRequest(
        request_id=request_id,
        data_domain=DOMAINS[api_name],
        start=start,
        end=end,
        fields=FIELDS[api_name],
        parameters=(f"api_name={api_name}", *parameters),
    )


def _entry(snapshot, rows: int) -> dict[str, Any]:
    return {
        "payload_artifact_id": snapshot.reference.payload_artifact_id,
        "raw_bytes": snapshot.reference.uncompressed_byte_size,
        "retrieved_at": snapshot.reference.retrieved_at.isoformat(),
        "rows": rows,
        "snapshot_id": snapshot.reference.snapshot_id,
        "stored_bytes": snapshot.payload_reference.byte_size,
    }


def backfill(
    *,
    provider: TushareProvider,
    start: date,
    end: date,
    output: Path,
    apis: tuple[str, ...],
    max_sessions: int | None,
    min_free_gb: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must not be after end")
    output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output / "checkpoint.json"
    state = _load_checkpoint(checkpoint_path, endpoint=provider.spec.api_base_url, start=start, end=end)
    completed = state["completed"]
    raw_store = RawSnapshotStore(ArtifactStore(output / "artifacts"))

    selected = set(apis)
    selected.add("trade_cal")
    fetched = 0
    skipped = 0

    def fetch_and_capture(request: FetchRequest, api_name: str, key: str) -> list[dict[str, object]]:
        nonlocal fetched

        def observe(state_name: str, attempt: int, error: Exception | None, delay: float | None) -> None:
            payload: dict[str, object] = {
                "api_name": api_name,
                "partition": key,
                "retry_attempt": attempt,
                "status": state_name,
            }
            if error is not None:
                payload.update(
                    {
                        "error_message": str(error),
                        "error_type": type(error).__name__,
                        "next_retry_seconds": delay,
                    }
                )
            _save_status(output, **payload)

        response = _fetch_with_retry(provider, request, attempts=None, observer=observe)
        rows = tushare_response_rows(response.payload)
        snapshot = raw_store.capture(provider.spec, response, storage_encoding="gzip")
        completed[api_name][key] = _entry(snapshot, len(rows))
        fetched += 1
        _atomic_write(checkpoint_path, canonical_json_bytes(state))
        if sleep_seconds:
            time.sleep(sleep_seconds)
        return rows

    calendar_key = f"SSE:{start:%Y%m%d}:{end:%Y%m%d}"
    if calendar_key not in completed["trade_cal"]:
        rows = fetch_and_capture(
            _request(
                api_name="trade_cal",
                request_id=f"M2B-CALENDAR-{start:%Y%m%d}-{end:%Y%m%d}",
                start=start,
                end=end,
                parameters=("exchange=SSE",),
            ),
            "trade_cal",
            calendar_key,
        )
        state["open_sessions"] = sorted(str(row["cal_date"]) for row in rows if str(row["is_open"]) == "1")
        _atomic_write(checkpoint_path, canonical_json_bytes(state))
    else:
        skipped += 1
    sessions = [str(value) for value in state.get("open_sessions", [])]
    if not sessions:
        raise ValueError("canonical SSE calendar contains no open sessions")
    if max_sessions is not None:
        sessions = sessions[:max_sessions]

    as_of = end.strftime("%Y%m%d")
    if "stock_basic" in selected:
        for status in LIST_STATUSES:
            key = f"{as_of}:list_status={status}"
            if key in completed["stock_basic"]:
                skipped += 1
                continue
            fetch_and_capture(
                _request(
                    api_name="stock_basic",
                    request_id=f"M2B-STOCK-BASIC-{as_of}-{status}",
                    start=end,
                    end=end,
                    parameters=(f"list_status={status}",),
                ),
                "stock_basic",
                key,
            )

    if "namechange" in selected:
        key = f"{as_of}:all"
        if key in completed["namechange"]:
            skipped += 1
        else:
            fetch_and_capture(
                _request(
                    api_name="namechange",
                    request_id=f"M2B-NAMECHANGE-{as_of}",
                    start=start,
                    end=end,
                    parameters=("_query_mode=all",),
                ),
                "namechange",
                key,
            )
        for year in range(start.year, end.year + 1):
            range_start = max(start, date(year, 1, 1))
            range_end = min(end, date(year, 12, 31))
            range_key = f"range:{year:04d}"
            if range_key in completed["namechange"]:
                skipped += 1
                continue
            fetch_and_capture(
                _request(
                    api_name="namechange",
                    request_id=f"M2B-NAMECHANGE-{year:04d}",
                    start=range_start,
                    end=range_end,
                    parameters=("_query_mode=range",),
                ),
                "namechange",
                range_key,
            )

    daily_tasks = []
    for session in sessions:
        session_date = datetime.strptime(session, "%Y%m%d").date()
        if "suspend_d" in selected and session not in completed["suspend_d"]:
            daily_tasks.append(("suspend_d", session, session_date))
        elif "suspend_d" in selected:
            skipped += 1
        if "stock_st" in selected and session_date >= ST_COVERAGE_START and session not in completed["stock_st"]:
            daily_tasks.append(("stock_st", session, session_date))
        elif "stock_st" in selected and session_date >= ST_COVERAGE_START:
            skipped += 1

    latest_session = sessions[-1]
    daily_tasks.sort(key=lambda task: (task[1] != latest_session, task[1], task[0]))

    expected = fetched + skipped + len(daily_tasks)
    _save_status(
        output,
        completed_partitions=fetched + skipped,
        expected_partitions=expected,
        status="RUNNING",
    )
    for index, (api_name, session, session_date) in enumerate(daily_tasks, start=1):
        free_gb = shutil.disk_usage(output).free / (1024**3)
        if free_gb < min_free_gb:
            raise RuntimeError(f"disk safety stop: {free_gb:.2f} GiB is below {min_free_gb:.2f} GiB")
        fetch_and_capture(
            _request(
                api_name=api_name,
                request_id=f"M2B-{api_name.upper()}-{session}",
                start=session_date,
                end=session_date,
                parameters=(),
            ),
            api_name,
            session,
        )
        if index == 1 or index % 10 == 0 or index == len(daily_tasks):
            done = fetched + skipped
            _save_status(
                output,
                api_name=api_name,
                completed_partitions=done,
                expected_partitions=expected,
                free_gb=round(free_gb, 3),
                partition=session,
                status="RUNNING",
            )
            print(f"M2-B progress {done}/{expected} api={api_name} partition={session}", flush=True)

    totals = {
        api_name: {
            "partitions": len(partitions),
            "rows": sum(int(item["rows"]) for item in partitions.values()),
        }
        for api_name, partitions in completed.items()
    }
    summary = {
        "apis": sorted(selected),
        "coverage": state["coverage"],
        "fetched_this_run": fetched,
        "skipped_this_run": skipped,
        "totals": totals,
    }
    _atomic_write(output / "latest_summary.json", canonical_json_bytes(summary))
    _save_status(output, status="COMPLETED", summary=summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(1990, 12, 19))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--api", action="append", choices=sorted(set(FIELDS) - {"trade_cal"}))
    parser.add_argument("--endpoint", default="https://api.tushare.pro/")
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    parser.add_argument("--output", type=Path, default=Path("data/tushare_reference_archive"))
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--sleep-ms", type=float, default=100.0)
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"token environment variable is not set: {args.token_env}")
    provider = TushareProvider(token=token, api_base_url=args.endpoint)
    try:
        summary = backfill(
            provider=provider,
            start=args.start,
            end=args.end,
            output=args.output,
            apis=tuple(args.api or ("stock_basic", "namechange", "stock_st", "suspend_d")),
            max_sessions=args.max_sessions,
            min_free_gb=args.min_free_gb,
            sleep_seconds=args.sleep_ms / 1000,
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
