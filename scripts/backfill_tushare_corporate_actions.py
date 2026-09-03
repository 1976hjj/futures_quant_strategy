"""Checkpointed M2-C dividend/corporate-action backfill, partitioned by security."""

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

import duckdb

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.providers.tushare import TushareProvider, tushare_response_rows
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

FIELDS = (
    "ts_code",
    "end_date",
    "ann_date",
    "div_proc",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
    "base_date",
    "base_share",
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


def _fetch_with_retry(
    provider: TushareProvider,
    request: FetchRequest,
    *,
    observer: Callable[[str, int, Exception | None, float | None], None],
):
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


def _load_codes(database: Path) -> tuple[str, ...]:
    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute(
            "SELECT ts_code FROM research.security_master WHERE is_a_share ORDER BY ts_code"
        ).fetchall()
    finally:
        connection.close()
    codes = tuple(str(row[0]) for row in rows)
    if not codes:
        raise ValueError("research.security_master contains no A-share securities")
    return codes


def _new_checkpoint(endpoint: str, start: date, end: date) -> dict[str, Any]:
    return {
        "api_base_url": endpoint,
        "completed": {"dividend": {}},
        "coverage": {"end": end.isoformat(), "start": start.isoformat()},
        "schema": "tushare-corporate-action-backfill-v1",
    }


def _load_checkpoint(path: Path, *, endpoint: str, start: date, end: date) -> dict[str, Any]:
    if not path.exists():
        return _new_checkpoint(endpoint, start, end)
    state = json.loads(path.read_bytes())
    if state.get("schema") != "tushare-corporate-action-backfill-v1":
        raise ValueError("unsupported M2-C checkpoint schema")
    if state.get("api_base_url") != endpoint:
        raise ValueError("M2-C checkpoint belongs to another endpoint")
    if state.get("coverage") != {"end": end.isoformat(), "start": start.isoformat()}:
        raise ValueError("coverage changes require a new M2-C archive or an explicit migration")
    state.setdefault("completed", {}).setdefault("dividend", {})
    return state


def _request(ts_code: str, start: date, end: date) -> FetchRequest:
    return FetchRequest(
        request_id=f"M2C-DIVIDEND-{ts_code.replace('.', '-')}",
        data_domain=DataDomain.CORPORATE_ACTION,
        start=start,
        end=end,
        fields=FIELDS,
        parameters=("api_name=dividend", "_query_mode=all", f"ts_code={ts_code}"),
    )


def _retry_observer(output: Path, ts_code: str) -> Callable[[str, int, Exception | None, float | None], None]:
    def observe(state_name: str, attempt: int, error: Exception | None, delay: float | None) -> None:
        payload: dict[str, object] = {
            "api_name": "dividend",
            "partition": ts_code,
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

    return observe


def backfill(
    *,
    provider: TushareProvider,
    start: date,
    end: date,
    output: Path,
    codes: tuple[str, ...],
    min_free_gb: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start must not be after end")
    if len(set(codes)) != len(codes):
        raise ValueError("security code list contains duplicates")
    output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output / "checkpoint.json"
    state = _load_checkpoint(checkpoint_path, endpoint=provider.spec.api_base_url, start=start, end=end)
    completed = state["completed"]["dividend"]
    raw_store = RawSnapshotStore(ArtifactStore(output / "artifacts"))
    ordered_codes = tuple(sorted(codes))
    skipped = sum(code in completed for code in ordered_codes)
    fetched = 0
    expected = len(ordered_codes)
    _save_status(
        output,
        completed_partitions=skipped,
        expected_partitions=expected,
        status="RUNNING",
    )

    for ts_code in ordered_codes:
        if ts_code in completed:
            continue
        free_gb = shutil.disk_usage(output).free / (1024**3)
        if free_gb < min_free_gb:
            raise RuntimeError(f"disk safety stop: {free_gb:.2f} GiB is below {min_free_gb:.2f} GiB")

        response = _fetch_with_retry(
            provider,
            _request(ts_code, start, end),
            observer=_retry_observer(output, ts_code),
        )
        rows = tushare_response_rows(response.payload)
        if len(rows) >= 5000:
            raise RuntimeError(f"suspicious dividend row cap for {ts_code}: {len(rows)} rows")
        if any(str(row.get("ts_code")) != ts_code for row in rows):
            raise RuntimeError(f"dividend response contains another security in partition {ts_code}")
        snapshot = raw_store.capture(provider.spec, response, storage_encoding="gzip")
        completed[ts_code] = {
            "payload_artifact_id": snapshot.reference.payload_artifact_id,
            "raw_bytes": snapshot.reference.uncompressed_byte_size,
            "retrieved_at": snapshot.reference.retrieved_at.isoformat(),
            "rows": len(rows),
            "snapshot_id": snapshot.reference.snapshot_id,
            "stored_bytes": snapshot.payload_reference.byte_size,
        }
        fetched += 1
        _atomic_write(checkpoint_path, canonical_json_bytes(state))
        done = skipped + fetched
        _save_status(
            output,
            api_name="dividend",
            completed_partitions=done,
            expected_partitions=expected,
            free_gb=round(free_gb, 3),
            partition=ts_code,
            status="RUNNING",
        )
        if fetched == 1 or fetched % 10 == 0 or done == expected:
            print(f"M2-C progress {done}/{expected} partition={ts_code} rows={len(rows)}", flush=True)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    summary = {
        "coverage": state["coverage"],
        "fetched_this_run": fetched,
        "skipped_this_run": skipped,
        "totals": {
            "dividend": {
                "partitions": len(completed),
                "rows": sum(int(item["rows"]) for item in completed.values()),
            }
        },
    }
    _atomic_write(output / "latest_summary.json", canonical_json_bytes(summary))
    _save_status(output, status="COMPLETED", summary=summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(1990, 12, 19))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--endpoint", default="https://api.tushare.pro/")
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--output", type=Path, default=Path("data/tushare_corporate_action_archive"))
    parser.add_argument("--max-securities", type=int)
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--sleep-ms", type=float, default=100.0)
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"token environment variable is not set: {args.token_env}")
    codes = _load_codes(args.database.resolve())
    if args.max_securities is not None:
        codes = codes[: args.max_securities]
    provider = TushareProvider(token=token, api_base_url=args.endpoint)
    try:
        summary = backfill(
            provider=provider,
            start=args.start,
            end=args.end,
            output=args.output,
            codes=codes,
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
