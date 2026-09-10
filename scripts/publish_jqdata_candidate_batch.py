"""Batch-publish the user-selected JQFactor candidate set without running full M4."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

CANDIDATE_FACTOR_IDS = (
    "jqdata-book-to-price-ratio",
    "jqdata-earnings-to-price-ratio",
    "jqdata-cash-flow-to-price-ratio",
    "jqdata-cash-earnings-to-price-ratio",
    "jqdata-roe-ttm",
    "jqdata-roa-ttm",
    "jqdata-acca",
    "jqdata-adjusted-profit-to-total-profit",
    "jqdata-net-operating-cash-flow-coverage",
    "jqdata-debt-to-equity-ratio",
    "jqdata-growth",
    "jqdata-momentum",
    "jqdata-rank1m",
    "jqdata-variance20",
    "jqdata-sharpe-ratio-60",
    "jqdata-beta",
    "jqdata-atr6",
    "jqdata-davol10",
    "jqdata-liquidity",
    "jqdata-natural-log-of-market-cap",
)


def run_batch(database: Path, store: Path, start: date, end: date) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="jqdata-candidate-batch-") as temp_name:
        temporary = Path(temp_name)
        for index, factor_id in enumerate(CANDIDATE_FACTOR_IDS, start=1):
            print(f"[{index}/{len(CANDIDATE_FACTOR_IDS)}] {factor_id}", flush=True)
            result_path = temporary / f"{index:02d}.json"
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts/publish_jqdata_factor.py"),
                "--database", str(database), "--store", str(store),
                "--factor-id", factor_id, "--start", start.isoformat(), "--end", end.isoformat(),
                "--result", str(result_path), "--skip-verification",
            ]
            completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            if completed.returncode:
                # On Windows a completed analytical connection can briefly retain
                # the DuckDB file handle while the same process opens the metadata
                # writer. A fresh process sees the immutable cached result and only
                # performs the registration pass.
                completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            if completed.returncode:
                raise RuntimeError(f"factor publication failed: {factor_id}")
            results.append(json.loads(result_path.read_text(encoding="utf-8")))
    return {
        "status": "COMPLETE",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "factor_count": len(results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data/warehouse/alpha_research.duckdb")
    parser.add_argument("--store", type=Path, default=PROJECT_ROOT / "data/factor_store")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    result = run_batch(args.database.resolve(), args.store.resolve(), args.start, args.end)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
