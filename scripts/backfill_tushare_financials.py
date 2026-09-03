"""Checkpointed M2-D full-field financial backfill via period-based VIP endpoints."""

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

APIS = ("income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator_vip")
REQUEST_FIELDS = ("ts_code", "ann_date", "f_ann_date", "end_date", "update_flag")
QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


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


def financial_periods(start: date, end: date) -> tuple[str, ...]:
    periods = []
    for year in range(start.year, end.year + 1):
        for month, day in QUARTER_ENDS:
            period = date(year, month, day)
            if start <= period <= end:
                periods.append(period.strftime("%Y%m%d"))
    return tuple(periods)


def _new_checkpoint(
    endpoint: str,
    start: date,
    end: date,
    apis: tuple[str, ...],
    periods: tuple[str, ...],
    page_size: int,
):
    return {
        "api_base_url": endpoint,
        "apis": list(apis),
        "completed": {api: {} for api in apis},
        "coverage": {"end": end.isoformat(), "start": start.isoformat()},
        "periods": list(periods),
        "page_size": page_size,
        "schema": "tushare-financial-backfill-v1",
        "terminal_offsets": {api: {} for api in apis},
    }


def _load_checkpoint(
    path: Path,
    *,
    endpoint: str,
    start: date,
    end: date,
    apis: tuple[str, ...],
    periods: tuple[str, ...],
    page_size: int,
) -> dict[str, Any]:
    if not path.exists():
        return _new_checkpoint(endpoint, start, end, apis, periods, page_size)
    state = json.loads(path.read_bytes())
    expected = _new_checkpoint(endpoint, start, end, apis, periods, page_size)
    for field in ("api_base_url", "apis", "coverage", "periods", "page_size", "schema"):
        if state.get(field) != expected[field]:
            raise ValueError(f"M2-D checkpoint configuration differs: {field}")
    for api in apis:
        state.setdefault("completed", {}).setdefault(api, {})
        state.setdefault("terminal_offsets", {}).setdefault(api, {})
    return state


def _request(api: str, period: str, offset: int, page_size: int) -> FetchRequest:
    period_date = datetime.strptime(period, "%Y%m%d").date()
    return FetchRequest(
        request_id=f"M2D-{api.upper().replace('_', '-')}-{period}-{offset}",
        data_domain=DataDomain.FUNDAMENTAL,
        start=period_date,
        end=period_date,
        fields=REQUEST_FIELDS,
        parameters=(
            f"api_name={api}",
            "_all_fields=true",
            f"period={period}",
            f"limit={page_size}",
            f"offset={offset}",
        ),
    )


def _entry(snapshot, rows: int, fields: list[str]) -> dict[str, Any]:
    return {
        "fields": fields,
        "payload_artifact_id": snapshot.reference.payload_artifact_id,
        "raw_bytes": snapshot.reference.uncompressed_byte_size,
        "retrieved_at": snapshot.reference.retrieved_at.isoformat(),
        "rows": rows,
        "snapshot_id": snapshot.reference.snapshot_id,
        "stored_bytes": snapshot.payload_reference.byte_size,
    }


def _progress(state: dict[str, Any], page_size: int) -> tuple[int, int]:
    entries = [entry for partitions in state["completed"].values() for entry in partitions.values()]
    completed = len(entries)
    base = len(state["apis"]) * len(state["periods"])
    full_pages = sum(int(entry["rows"]) == page_size for entry in entries)
    return completed, base + full_pages


def _observer(output: Path, api: str, key: str) -> Observer:
    def observe(status: str, attempt: int, error: Exception | None, delay: float | None) -> None:
        payload: dict[str, object] = {
            "api_name": api,
            "partition": key,
            "retry_attempt": attempt,
            "status": status,
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

    return observe


def backfill(
    *,
    provider: TushareProvider,
    start: date,
    end: date,
    output: Path,
    apis: tuple[str, ...],
    periods: tuple[str, ...],
    page_size: int,
    min_free_gb: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    if not periods or page_size <= 0:
        raise ValueError("periods must be nonempty and page_size must be positive")
    if any(api not in APIS for api in apis) or len(set(apis)) != len(apis):
        raise ValueError("financial API list is invalid or contains duplicates")
    output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output / "checkpoint.json"
    state = _load_checkpoint(
        checkpoint_path,
        endpoint=provider.spec.api_base_url,
        start=start,
        end=end,
        apis=apis,
        periods=periods,
        page_size=page_size,
    )
    raw_store = RawSnapshotStore(ArtifactStore(output / "artifacts"))
    fetched = 0
    skipped = sum(len(partitions) for partitions in state["completed"].values())
    completed_count, expected_count = _progress(state, page_size)
    _save_status(
        output,
        completed_partitions=completed_count,
        expected_partitions=expected_count,
        status="RUNNING",
    )

    for api in apis:
        for period in periods:
            if period in state["terminal_offsets"][api]:
                continue
            offset = 0
            while True:
                key = f"{period}:offset={offset}"
                existing = state["completed"][api].get(key)
                if existing is not None:
                    if int(existing["rows"]) < page_size:
                        state["terminal_offsets"][api][period] = offset
                        _atomic_write(checkpoint_path, canonical_json_bytes(state))
                        break
                    offset += page_size
                    continue
                free_gb = shutil.disk_usage(output).free / (1024**3)
                if free_gb < min_free_gb:
                    raise RuntimeError(f"disk safety stop: {free_gb:.2f} GiB is below {min_free_gb:.2f} GiB")
                response = _fetch_with_retry(
                    provider,
                    _request(api, period, offset, page_size),
                    _observer(output, api, key),
                )
                document = json.loads(response.payload)
                fields = document.get("data", {}).get("fields")
                items = document.get("data", {}).get("items")
                if not isinstance(fields, list) or not isinstance(items, list):
                    raise ValueError(f"financial response has invalid tabular data: {api}/{key}")
                if items and not fields:
                    raise ValueError(f"nonempty financial response has no fields: {api}/{key}")
                rows = tushare_response_rows(response.payload)
                if any(str(row.get("end_date")) != period for row in rows):
                    raise ValueError(f"financial row period differs from partition: {api}/{key}")
                snapshot = raw_store.capture(provider.spec, response, storage_encoding="gzip")
                state["completed"][api][key] = _entry(snapshot, len(rows), [str(field) for field in fields])
                if len(rows) < page_size:
                    state["terminal_offsets"][api][period] = offset
                fetched += 1
                _atomic_write(checkpoint_path, canonical_json_bytes(state))
                completed_count, expected_count = _progress(state, page_size)
                _save_status(
                    output,
                    api_name=api,
                    completed_partitions=completed_count,
                    expected_partitions=expected_count,
                    free_gb=round(free_gb, 3),
                    partition=key,
                    status="RUNNING",
                )
                if fetched == 1 or fetched % 5 == 0 or len(rows) < page_size:
                    print(
                        f"M2-D progress {completed_count}/{expected_count} api={api} "
                        f"partition={key} rows={len(rows)}",
                        flush=True,
                    )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
                if len(rows) < page_size:
                    break
                offset += page_size

    totals = {
        api: {
            "partitions": len(state["completed"][api]),
            "periods": len(state["terminal_offsets"][api]),
            "rows": sum(int(item["rows"]) for item in state["completed"][api].values()),
        }
        for api in apis
    }
    summary = {
        "coverage": state["coverage"],
        "fetched_this_run": fetched,
        "skipped_pages_this_run": skipped,
        "totals": totals,
    }
    _atomic_write(output / "latest_summary.json", canonical_json_bytes(summary))
    _save_status(output, status="COMPLETED", summary=summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(1990, 12, 31))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--endpoint", default="https://api.tushare.pro/")
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    parser.add_argument("--output", type=Path, default=Path("data/tushare_financial_archive"))
    parser.add_argument("--api", action="append", choices=APIS)
    parser.add_argument("--max-periods", type=int)
    parser.add_argument("--page-size", type=int, default=5000)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--sleep-ms", type=float, default=100.0)
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"token environment variable is not set: {args.token_env}")
    periods = financial_periods(args.start, args.end)
    if args.max_periods is not None:
        periods = periods[: args.max_periods]
    provider = TushareProvider(token=token, api_base_url=args.endpoint)
    try:
        summary = backfill(
            provider=provider,
            start=args.start,
            end=args.end,
            output=args.output,
            apis=tuple(args.api or APIS),
            periods=periods,
            page_size=args.page_size,
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
