"""Fetch and persist the local CSI 300 daily-close benchmark used by strategy reports."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START = date(2010, 1, 1)
DEFAULT_END = date(2026, 8, 31)


def _configured_tushare_token(project_root: Path) -> str:
    """Use the same local credential source as the data-update service."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    credential = project_root / "secrets" / "tushare.env"
    if not credential.exists():
        return ""
    prefix, separator, value = credential.read_text(encoding="utf-8").strip().partition("=")
    return value.strip() if separator and prefix == "TUSHARE_TOKEN" else ""


def _fetch_chunk(url: str) -> dict[str, object]:
    """Read one Eastmoney chunk with bounded retry for transient disconnects."""

    for attempt in range(1, 6):
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; alpha-research-os/1.0)",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed public market-data endpoint
                return json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, RemoteDisconnected) as error:
            if attempt == 5:
                raise RuntimeError(f"CSI 300 source unavailable after {attempt} attempts: {error}") from error
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _fetch(start: date, end: date, *, minimum_rows: int = 2) -> dict[str, object]:
    # Eastmoney publishes CSI 300 sessions through the current market-data
    # horizon. Request one calendar year at a time to keep each public request small.
    rows_by_session: dict[str, float] = {}
    chunk_start = start
    source_urls: list[str] = []
    while chunk_start <= end:
        chunk_end = min(end, date(chunk_start.year, 12, 31))
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            "secid=1.000300&klt=101&fqt=0&"
            f"beg={chunk_start:%Y%m%d}&end={chunk_end:%Y%m%d}&"
            "fields1=f1,f2,f3,f4,f5,f6&"
            "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        )
        payload = _fetch_chunk(url)
        rows = (payload.get("data") or {}).get("klines") or []
        for row in rows:
            fields = str(row).split(",")
            if len(fields) >= 3 and float(fields[2]) > 0:
                rows_by_session[fields[0]] = float(fields[2])
        source_urls.append(url)
        chunk_start = date(chunk_end.year + 1, 1, 1)
    daily = [{"session": session, "close": close} for session, close in sorted(rows_by_session.items())]
    if len(daily) < minimum_rows:
        raise RuntimeError("CSI 300 source returned insufficient daily closes")
    return {
        "schema_version": "1",
        "benchmark_id": "CSI300",
        "name": "沪深300",
        "symbol": "000300.SH",
        "pricing": "close_price_index",
        "source": {"provider": "Eastmoney", "urls": source_urls},
        "fetched_at": datetime.now().astimezone().isoformat(),
        "coverage": {"start": daily[0]["session"], "end": daily[-1]["session"]},
        "daily": daily,
    }


def _fetch_tushare(start: date, end: date, *, endpoint: str, token: str) -> dict[str, object]:
    """Fetch CSI 300 from the configured Tushare-compatible gateway."""

    body = json.dumps(
        {
            "api_name": "index_daily",
            "token": token,
            "params": {
                "ts_code": "000300.SH",
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            },
            "fields": "ts_code,trade_date,close",
        }, separators=(",", ":"),
    ).encode()
    for attempt in range(1, 6):
        request = Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json", "Accept-Encoding": "gzip", "User-Agent": "alpha-research-os/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - configured HTTPS endpoint
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                document = json.loads(raw)
            if document.get("code") != 0:
                raise RuntimeError(f"CSI 300 Tushare source failed: code={document.get('code')}")
            data = document.get("data") or {}
            fields = data.get("fields") or []
            rows = [dict(zip(fields, item, strict=True)) for item in data.get("items") or []]
            daily = []
            for row in rows:
                raw_date = str(row.get("trade_date", ""))
                if row.get("close") is None or len(raw_date) != 8:
                    continue
                daily.append(
                    {"session": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}", "close": float(row["close"])}
                )
            if not daily:
                raise RuntimeError("CSI 300 Tushare source returned no daily closes")
            daily.sort(key=lambda item: item["session"])
            return {
                "schema_version": "1", "benchmark_id": "CSI300", "name": "沪深300", "symbol": "000300.SH",
                "pricing": "close_price_index", "source": {"provider": "Tushare-compatible", "urls": [endpoint]},
                "fetched_at": datetime.now().astimezone().isoformat(),
                "coverage": {"start": daily[0]["session"], "end": daily[-1]["session"]}, "daily": daily,
            }
        except (HTTPError, URLError, TimeoutError, RemoteDisconnected, json.JSONDecodeError) as error:
            if attempt == 5:
                raise RuntimeError(f"CSI 300 Tushare source unavailable after {attempt} attempts: {error}") from error
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _merge_incremental(existing: dict[str, object], fresh: dict[str, object]) -> dict[str, object]:
    rows = {
        str(item["session"]): float(item["close"])
        for item in [*(existing.get("daily") or []), *(fresh.get("daily") or [])]
    }
    daily = [{"session": session, "close": close} for session, close in sorted(rows.items())]
    urls = [
        *(existing.get("source", {}).get("urls", []) if isinstance(existing.get("source"), dict) else []),
        *(fresh.get("source", {}).get("urls", []) if isinstance(fresh.get("source"), dict) else []),
    ]
    fresh_source = fresh.get("source") if isinstance(fresh.get("source"), dict) else {}
    return {
        **fresh,
        "source": {"provider": fresh_source.get("provider", "Eastmoney"), "urls": list(dict.fromkeys(urls))},
        "coverage": {"start": daily[0]["session"], "end": daily[-1]["session"]},
        "daily": daily,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--tushare-endpoint")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must not precede --start")
    target = args.project_root / "data" / "benchmarks" / "csi300_daily.json"
    existing = json.loads(target.read_text(encoding="utf-8")) if args.incremental and target.exists() else None
    coverage = (existing or {}).get("coverage") or {}
    existing_start = coverage.get("start")
    existing_end = coverage.get("end")
    token = _configured_tushare_token(args.project_root)

    def fetch_range(start: date, end: date) -> dict[str, object]:
        return (
            _fetch_tushare(start, end, endpoint=args.tushare_endpoint, token=token)
            if token and args.tushare_endpoint
            else _fetch(start, end, minimum_rows=1)
        )

    payload = existing
    if existing_start:
        historical_end = date.fromisoformat(existing_start) - timedelta(days=1)
        # Do not request a short pre-listing/weekend gap: index_daily correctly
        # returns no rows for it, while coverage already begins at the first session.
        if args.start <= historical_end - timedelta(days=7):
            historical = fetch_range(args.start, historical_end)
            payload = _merge_incremental(payload or {}, historical)
    fetch_start = max(args.start, date.fromisoformat(existing_end) + timedelta(days=1)) if existing_end else args.start
    if fetch_start <= args.end:
        fresh = fetch_range(fetch_start, args.end)
        payload = _merge_incremental(payload or {}, fresh) if payload else fresh
    if payload is None:
        raise RuntimeError("CSI 300 benchmark has no available coverage")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)
    print(f"Stored {len(payload['daily'])} CSI 300 closes: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
