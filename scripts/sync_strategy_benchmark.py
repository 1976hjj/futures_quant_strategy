"""Fetch and persist the local CSI 300 daily-close benchmark used by strategy reports."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START = date(2020, 1, 1)
DEFAULT_END = date(2026, 8, 31)


def _fetch(start: date, end: date) -> dict[str, object]:
    # Eastmoney publishes CSI 300 sessions through the current market-data
    # horizon. Request in five-year chunks and deduplicate boundary sessions.
    rows_by_session: dict[str, float] = {}
    chunk_start = start
    source_urls: list[str] = []
    while chunk_start <= end:
        chunk_end = min(end, date(chunk_start.year + 4, 12, 31))
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            "secid=1.000300&klt=101&fqt=0&"
            f"beg={chunk_start:%Y%m%d}&end={chunk_end:%Y%m%d}&"
            "fields1=f1,f2,f3,f4,f5,f6&"
            "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        )
        request = Request(url, headers={"User-Agent": "alpha-research-os/1.0"})
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed public market-data endpoint
            payload = json.loads(response.read())
        rows = (payload.get("data") or {}).get("klines") or []
        for row in rows:
            fields = str(row).split(",")
            if len(fields) >= 3 and float(fields[2]) > 0:
                rows_by_session[fields[0]] = float(fields[2])
        source_urls.append(url)
        chunk_start = date(chunk_end.year + 1, 1, 1)
    daily = [{"session": session, "close": close} for session, close in sorted(rows_by_session.items())]
    if len(daily) < 2:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must not precede --start")
    payload = _fetch(args.start, args.end)
    target = args.project_root / "data" / "benchmarks" / "csi300_daily.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)
    print(f"Stored {len(payload['daily'])} CSI 300 closes: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
