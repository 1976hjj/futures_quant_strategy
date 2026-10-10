"""Collect a bounded CSI300 gross TR reference without changing shared benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.data_update import _archive_endpoint, tushare_token  # noqa: E402


def sync(root: Path, start: date, end: date):
    config = json.loads((root / "config/bank_timing_data.json").read_text(encoding="utf-8"))
    if end > date.fromisoformat(config["released_feature_end"]) or end < start:
        raise ValueError("Benchmark request is outside the released research window")
    token = tushare_token(root)
    if not token:
        raise ValueError("Existing market credential is unavailable")
    endpoint = _archive_endpoint(root, "market")
    if not endpoint:
        raise ValueError("Existing market endpoint is unavailable")
    archive = root / "data/bank_timing_store/benchmark_sources"
    archive.mkdir(parents=True, exist_ok=True)
    attempts = []
    for symbol in ("H00300.CSI", "H00300.SH"):
        response = requests.post(endpoint, json={
            "api_name": "index_daily", "token": token,
            "params": {"ts_code": symbol, "start_date": start.strftime("%Y%m%d"),
                       "end_date": end.strftime("%Y%m%d")},
            "fields": "ts_code,trade_date,close,pre_close,pct_chg",
        }, timeout=25)
        response.raise_for_status()
        document = response.json()
        if document.get("code") != 0:
            attempts.append({"symbol": symbol, "status": "API_REJECTED", "code": document.get("code")})
            continue
        data = document.get("data") or {}
        frame = pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])
        if frame.empty:
            attempts.append({"symbol": symbol, "status": "NO_ROWS"})
            continue
        if not {"ts_code", "trade_date", "close"}.issubset(frame):
            raise ValueError("Benchmark response schema mismatch")
        if not frame.ts_code.eq(symbol).all():
            raise ValueError("Benchmark response returned a different index")
        frame["session"] = pd.to_datetime(frame.trade_date, format="%Y%m%d").dt.date
        frame["gross_total_return_index"] = pd.to_numeric(frame.close, errors="coerce")
        if (frame.session.min() < start or frame.session.max() > end or frame.session.duplicated().any()
                or frame.gross_total_return_index.isna().any() or (frame.gross_total_return_index <= 0).any()):
            raise ValueError("Benchmark bounds, keys or levels failed validation")
        # Only the successful tabular response is archived; request tokens and server messages are excluded.
        raw = json.dumps({"code": 0, "data": data}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        folder = archive / digest
        if folder.exists():
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            return manifest | {"folder": str(folder), "cache_hit": True}
        folder.mkdir()
        (folder / "response.json").write_bytes(raw)
        clean = frame[["session", "gross_total_return_index"]].sort_values("session")
        clean["available_at"] = pd.to_datetime(clean.session.astype(str), utc=True) + pd.Timedelta(hours=9)
        clean.to_parquet(folder / "benchmark.parquet", index=False)
        manifest = {
            "schema_version": "1.0.0", "index_code": "H00300", "provider_symbol": symbol,
            "return_basis": "gross_total_return_index", "row_count": len(clean),
            "start": str(clean.session.min()), "end": str(clean.session.max()),
            "retrieved_at": datetime.now(UTC).isoformat(), "raw_sha256": digest,
            "parquet_sha256": hashlib.sha256((folder / "benchmark.parquet").read_bytes()).hexdigest(),
            "definition_source": config["benchmark"]["primary_definition"],
            "historical_grade": "vendor historical reconstruction; exhaustive revision certification incomplete",
            "availability_policy": "daily session 17:00 Asia/Shanghai; conservative post-close cutoff",
            "attempts": attempts,
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest | {"folder": str(folder), "cache_hit": False}
    receipt = {"status": "MISSING_SOURCE", "index_code": "H00300", "attempts": attempts,
               "created_at": datetime.now(UTC).isoformat()}
    target = archive / ("missing-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json")
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def fill_gaps(root: Path, start: date, end: date):
    """Append a merged version, filling only absent sessions from CSI's own history."""
    import duckdb

    config = json.loads((root / 'config/bank_timing_data.json').read_bytes())
    if end > date.fromisoformat(config['released_feature_end']) or end < start:
        raise ValueError('Benchmark gap request is outside the released research window')
    archive = root / 'data/bank_timing_store/benchmark_sources'
    candidates = []
    for path in archive.glob('*/manifest.json'):
        manifest = json.loads(path.read_bytes())
        if (manifest.get('index_code') == 'H00300' and manifest.get('return_basis') == 'gross_total_return_index'
                and manifest['start'] <= str(start) and manifest['end'] >= str(end)
                and manifest['end'] <= config['released_feature_end']):
            candidates.append((manifest['retrieved_at'], path, manifest))
    if not candidates:
        raise ValueError('No existing bounded total-return benchmark archive')
    _, path, base = max(candidates)
    source = path.parent / 'benchmark.parquet'
    if hashlib.sha256(source.read_bytes()).hexdigest() != base['parquet_sha256']:
        raise ValueError('Existing benchmark hash mismatch')
    frame = pd.read_parquet(source)
    frame['session'] = pd.to_datetime(frame.session).dt.date
    with duckdb.connect(str(root / 'data/warehouse/alpha_research.duckdb'), read_only=True) as c:
        calendar = [r[0] for r in c.execute(
            'SELECT DISTINCT trade_date FROM research.market_daily WHERE trade_date BETWEEN ? AND ? ORDER BY 1',
            [start, end]).fetchall()]
    missing = sorted(set(calendar) - set(frame.session))
    if not missing:
        return base | {'cache_hit': True, 'filled_sessions': []}
    responses, additions = [], []
    url = 'https://www.csindex.com.cn/csindex-home/perf/index-perf'
    existing = frame.set_index('session').gross_total_return_index.to_dict()
    for day in missing:
        position = calendar.index(day)
        low, high = calendar[max(0, position - 1)], calendar[min(len(calendar) - 1, position + 1)]
        response = requests.get(url, params=dict(indexCode='H00300', startDate=low.strftime('%Y%m%d'),
                                                endDate=high.strftime('%Y%m%d')),
                                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.csindex.com.cn/'},
                                timeout=25)
        response.raise_for_status()
        document = response.json()
        rows = document.get('data') or []
        responses.append(dict(start=str(low), end=str(high), response=document))
        for row in rows:
            session = datetime.strptime(row['tradeDate'], '%Y%m%d').date()
            if row.get('indexCode') != 'H00300' or not low <= session <= high:
                raise ValueError('Official benchmark returned a different index or date')
            value = float(row['close'])
            if value <= 0 or not math.isfinite(value):
                raise ValueError('Official benchmark level is invalid')
            if session in existing and abs(value - existing[session]) > .0051:
                raise ValueError('Official and archived overlapping levels disagree beyond display precision')
            if session == day:
                additions.append(dict(session=day, gross_total_return_index=value,
                                      available_at=pd.Timestamp(day, tz='Asia/Shanghai') + pd.Timedelta(hours=17)))
    added = pd.DataFrame(additions)
    if added.empty or added.session.duplicated().any():
        raise ValueError('Official source did not resolve missing benchmark sessions')
    merged = pd.concat([frame, added], ignore_index=True).sort_values('session').reset_index(drop=True)
    raw = json.dumps(dict(base_manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          official_responses=responses), ensure_ascii=False, sort_keys=True).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    folder = archive / digest
    if not folder.exists():
        folder.mkdir()
        (folder/'response.json').write_bytes(raw)
        merged.to_parquet(folder/'benchmark.parquet', index=False)
        manifest = dict(base, retrieved_at=datetime.now(UTC).isoformat(), raw_sha256=digest,
                        parquet_sha256=hashlib.sha256((folder/'benchmark.parquet').read_bytes()).hexdigest(),
                        row_count=len(merged), base_source_manifest=str(path), supplemental_source=url,
                        filled_sessions=list(map(str, added.session)), existing_values_replaced=0,
                        overlap_validation='within 0.0051 index points; CSI website reports 2 decimal places')
        (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return json.loads((folder/'manifest.json').read_bytes()) | {'folder':str(folder), 'cache_hit':False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2019, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument('--fill-gaps', action='store_true')
    args = parser.parse_args()
    # Deliberately print only safe status/count metadata, never endpoints, request bodies or credentials.
    result = fill_gaps(ROOT, args.start, args.end) if args.fill_gaps else sync(ROOT, args.start, args.end)
    print(json.dumps({key: result.get(key) for key in ("status", "index_code", "row_count", "start", "end", "folder")},
                     ensure_ascii=False))
