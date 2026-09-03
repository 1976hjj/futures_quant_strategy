"""Checkpointed local archive for Tushare-compatible A-share daily tables."""

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.providers.tushare import TushareProvider, tushare_response_rows
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

API_FIELDS = {
    "daily": ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"),
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": (
        "ts_code",
        "trade_date",
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "dv_ratio",
        "dv_ttm",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
}


def _load_checkpoint(path: Path, *, endpoint: str) -> dict[str, object]:
    if not path.exists():
        return {
            "api_base_url": endpoint,
            "calendar_snapshots": [],
            "completed": {},
            "schema": "tushare-daily-backfill-v1",
        }
    state = json.loads(path.read_bytes())
    if state.get("schema") != "tushare-daily-backfill-v1" or state.get("api_base_url") != endpoint:
        raise ValueError("checkpoint belongs to a different schema or API endpoint")
    return state


def _save_checkpoint(path: Path, state: dict[str, object]) -> None:
    _atomic_write(path, canonical_json_bytes(state))


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace a checkpoint despite short-lived Windows read locks."""

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


def _save_run_status(output: Path, **status: object) -> None:
    path = output / "run_status.json"
    payload = {"updated_at": datetime.now().astimezone().isoformat(), **status}
    _atomic_write(path, canonical_json_bytes(payload))


def _retry_delay(error: Exception, attempt: int) -> float | None:
    """Return a bounded backoff for transient gateway failures, else fail fast."""

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


def _calendar_fields(provider: TushareProvider) -> tuple[str, ...]:
    return next(
        capability.fields
        for capability in provider.spec.capabilities
        if capability.data_domain is DataDomain.TRADING_CALENDAR
    )


def backfill(
    *,
    provider: TushareProvider,
    start: date,
    end: date,
    apis: tuple[str, ...],
    output: Path,
    min_free_gb: float,
    max_sessions: int | None,
    sleep_seconds: float,
    workers: int,
) -> dict[str, object]:
    artifacts = ArtifactStore(output / "artifacts")
    _save_run_status(
        output,
        apis=list(apis),
        coverage={"end": end.isoformat(), "start": start.isoformat()},
        status="RUNNING",
        workers=workers,
    )
    raw_store = RawSnapshotStore(artifacts)
    checkpoint_path = output / "checkpoint.json"
    state = _load_checkpoint(checkpoint_path, endpoint=provider.spec.api_base_url)
    completed = state.setdefault("completed", {})
    for api_name in apis:
        completed.setdefault(api_name, {})

    def fetch_resilient(request: FetchRequest):
        def observe(state_name: str, attempt: int, error: Exception | None, delay: float | None) -> None:
            status: dict[str, object] = {
                "apis": list(apis),
                "coverage": {"end": end.isoformat(), "start": start.isoformat()},
                "request_id": request.request_id,
                "retry_attempt": attempt,
                "status": state_name,
                "workers": workers,
            }
            if error is not None:
                status.update(
                    {
                        "error_message": str(error),
                        "error_type": type(error).__name__,
                        "next_retry_seconds": delay,
                    }
                )
            _save_run_status(output, **status)

        return _fetch_with_retry(provider, request, attempts=None, observer=observe)

    calendar_request = FetchRequest(
        request_id=f"BACKFILL-CALENDAR-{start:%Y%m%d}-{end:%Y%m%d}",
        data_domain=DataDomain.TRADING_CALENDAR,
        start=start,
        end=end,
        fields=_calendar_fields(provider),
        parameters=("api_name=trade_cal", "exchange=SSE"),
    )
    calendar_response = fetch_resilient(calendar_request)
    calendar_snapshot = raw_store.capture(provider.spec, calendar_response, storage_encoding="gzip")
    state["calendar_snapshots"] = sorted(
        set([*state.get("calendar_snapshots", []), calendar_snapshot.reference.snapshot_id])
    )
    sessions = sorted(
        row["cal_date"]
        for row in tushare_response_rows(calendar_response.payload)
        if str(row["is_open"]) == "1"
    )
    if max_sessions is not None:
        sessions = sessions[:max_sessions]
    _save_checkpoint(checkpoint_path, state)

    fetched = 0
    skipped = 0
    pending = []
    for session in sessions:
        for api_name in apis:
            if str(session) in completed[api_name]:
                skipped += 1
                continue
            pending.append((str(session), api_name))

    def fetch_partition(task: tuple[str, str]):
        session, api_name = task
        session_day = datetime.strptime(session, "%Y%m%d").date()
        request = FetchRequest(
            request_id=f"BACKFILL-{api_name}-{session}",
            data_domain=DataDomain.MARKET,
            start=session_day,
            end=session_day,
            fields=API_FIELDS[api_name],
            parameters=(f"api_name={api_name}",),
        )
        response = fetch_resilient(request)
        if sleep_seconds:
            time.sleep(sleep_seconds)
        return session, api_name, response

    batch_size = max(workers * 4, 1)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for offset in range(0, len(pending), batch_size):
            free_gb = shutil.disk_usage(output).free / (1024**3)
            if free_gb < min_free_gb:
                raise RuntimeError(
                    f"disk safety stop: {free_gb:.2f} GiB free is below the {min_free_gb:.2f} GiB floor"
                )
            batch = pending[offset : offset + batch_size]
            futures = [executor.submit(fetch_partition, task) for task in batch]
            try:
                for future in as_completed(futures):
                    session, api_name, response = future.result()
                    snapshot = raw_store.capture(provider.spec, response, storage_encoding="gzip")
                    completed[api_name][session] = {
                        "payload_artifact_id": snapshot.reference.payload_artifact_id,
                        "raw_bytes": snapshot.reference.uncompressed_byte_size,
                        "rows": len(tushare_response_rows(response.payload)),
                        "snapshot_id": snapshot.reference.snapshot_id,
                        "stored_bytes": snapshot.payload_reference.byte_size,
                    }
                    fetched += 1
            finally:
                _save_checkpoint(checkpoint_path, state)
            print(
                f"progress partitions={fetched}/{len(pending)} skipped={skipped} free_gb={free_gb:.2f}",
                flush=True,
            )

    totals = {
        api_name: {
            "partitions": len(completed[api_name]),
            "raw_bytes": sum(item["raw_bytes"] for item in completed[api_name].values()),
            "rows": sum(item["rows"] for item in completed[api_name].values()),
            "stored_bytes": sum(item["stored_bytes"] for item in completed[api_name].values()),
        }
        for api_name in apis
    }
    summary = {
        "apis": list(apis),
        "coverage": {"end": end.isoformat(), "start": start.isoformat()},
        "fetched_this_run": fetched,
        "free_gb": round(shutil.disk_usage(output).free / (1024**3), 3),
        "skipped_this_run": skipped,
        "totals": totals,
    }
    (output / "latest_summary.json").write_bytes(canonical_json_bytes(summary))
    _save_run_status(output, status="COMPLETED", summary=summary, workers=workers)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--api", action="append", choices=sorted(API_FIELDS))
    parser.add_argument("--endpoint", default="https://api.tushare.pro/")
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    parser.add_argument("--output", type=Path, default=Path("data/tushare_archive"))
    parser.add_argument("--min-free-gb", type=float, default=30.0)
    parser.add_argument("--max-sessions", type=int)
    parser.add_argument("--sleep-ms", type=float, default=50.0)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"token environment variable is not set: {args.token_env}")
    provider = TushareProvider(token=token, api_base_url=args.endpoint)
    if args.workers < 1 or args.workers > 8:
        parser.error("workers must be between 1 and 8")
    try:
        summary = backfill(
            provider=provider,
            start=args.start,
            end=args.end,
            apis=tuple(args.api or API_FIELDS),
            output=args.output,
            min_free_gb=args.min_free_gb,
            max_sessions=args.max_sessions,
            sleep_seconds=args.sleep_ms / 1000,
            workers=args.workers,
        )
    except Exception as error:
        _save_run_status(
            args.output,
            error_message=str(error),
            error_type=type(error).__name__,
            status="FAILED",
            traceback=traceback.format_exc(limit=8),
            workers=args.workers,
        )
        raise
    sys.stdout.buffer.write(canonical_json_bytes(summary) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
