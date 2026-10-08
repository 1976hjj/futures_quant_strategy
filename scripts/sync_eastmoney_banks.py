"""Archive all configured banks without credentials and publish a separate, versioned staging warehouse."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for import_root in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.data.providers.eastmoney_bank import (  # noqa: E402
    CORE_METRICS,
    ENDPOINT,
    METRICS,
    fetch_page,
    normalize_row,
)

ARCHIVE_NAME = "eastmoney_bank_archive"
WAREHOUSE_NAME = "eastmoney_bank.duckdb"


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    # Windows readers can briefly deny replacement while polling the progress file.
    for attempt in range(20):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)



def bank_universe(root: Path) -> list[dict[str, str]]:
    banks = json.loads((root / "config" / "eastmoney_banks.json").read_text(encoding="utf-8"))["banks"]
    if not banks or len({b["code"] for b in banks}) != len(banks):
        raise ValueError("bank universe is empty or duplicated")
    return banks


def _acquire_bank(bank: dict[str, str], folder: Path, end: date, run_id: str) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    page, pages = 1, 1
    while page <= pages:
        raw, params = fetch_page(bank["code"], page)
        retrieved = datetime.now(UTC).isoformat()
        digest = hashlib.sha256(raw).hexdigest()
        file = folder / f"{bank['code']}.page{page}.json"
        with file.open("xb") as target:
            target.write(raw)
        payload = json.loads(raw)
        pages = int(payload["result"].get("pages") or 1)
        if not 1 <= pages <= 20:
            raise ValueError("bank pagination outside supported bounds")
        with file.with_suffix(".metadata.json").open("x", encoding="utf-8") as target:
            json.dump({"endpoint": ENDPOINT, "parameters": params, "retrieved_at_utc": retrieved,
                       "raw_sha256": digest, "http_status": 200}, target, ensure_ascii=False, indent=2)
        for original in payload["result"]["data"]:
            row = normalize_row(original, bank, retrieved_at=retrieved, requested_end=end,
                                run_id=run_id, raw_sha256=digest)
            if row is None:
                continue
            key = (row["report_date"], row["record_sha256"])
            if key in seen:
                raise ValueError(f"duplicate report version for {bank['code']}: {key}")
            seen.add(key)
            rows.append(row)
        page += 1
    if not rows:
        raise ValueError(f"no bank disclosures through {end}: {bank['code']}")
    return rows


def publish(root: Path, run_dir: Path, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    import duckdb

    warehouse = root / "data" / "warehouse"
    warehouse.mkdir(parents=True, exist_ok=True)
    strings = [k for k in rows[0] if k not in METRICS]
    columns = {**{k: "VARCHAR" for k in strings}, **{k: "DOUBLE" for k in METRICS}}
    definitions = ", ".join(f'"{k}" {v}' for k, v in columns.items())
    names = ", ".join(f'"{k}"' for k in columns)
    source = str(run_dir / "normalized.json").replace("'", "''")
    column_spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    with duckdb.connect(str(warehouse / WAREHOUSE_NAME)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(f"CREATE TABLE IF NOT EXISTS bank_financial_snapshots ({definitions}, "
                               "PRIMARY KEY(run_id, code, report_date, record_sha256))")
            connection.execute(f"INSERT INTO bank_financial_snapshots SELECT {names} FROM read_json("
                               f"'{source}', columns={column_spec})")
            connection.execute("CREATE OR REPLACE VIEW bank_financial_latest_versions AS "
                               "SELECT * FROM bank_financial_snapshots QUALIFY row_number() OVER "
                               "(PARTITION BY code, report_date ORDER BY retrieved_at_utc DESC, "
                               "provider_update_date DESC, run_id DESC)=1")
            connection.execute("CREATE OR REPLACE VIEW bank_financial_current AS "
                               f"SELECT * FROM bank_financial_snapshots WHERE run_id='{summary['run_id']}' "
                               "QUALIFY row_number() OVER "
                               "(PARTITION BY code ORDER BY report_date DESC, provider_update_date DESC)=1")
            count = connection.execute("SELECT count(*) FROM bank_financial_current").fetchone()[0]
            if count != summary["expected_banks"]:
                raise ValueError("published bank coverage does not match configured universe")
            parquet = str(run_dir / "normalized.parquet").replace("'", "''")
            connection.execute(f"COPY (SELECT * FROM bank_financial_snapshots WHERE run_id=?) "
                               f"TO '{parquet}' (FORMAT PARQUET)", [summary["run_id"]])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    # Mutable pointers are separate from append-only raw responses and run evidence.
    atomic_json(root / "data" / ARCHIVE_NAME / "checkpoint.json", summary)


def sync(root: Path, end: date, *, workers: int = 2, min_free_gb: float = 10,
         progress_path: Path | None = None) -> dict[str, Any]:
    if end > date.today():
        raise ValueError("bank disclosure cutoff cannot be in the future")
    if shutil.disk_usage(root).free < min_free_gb * 1024 ** 3:
        raise ValueError("insufficient free disk space for bank archive")
    banks = bank_universe(root)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    archive = root / "data" / ARCHIVE_NAME
    run_dir = archive / "runs" / run_id
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True)
    started = datetime.now(UTC).isoformat()
    rows, errors = [], []
    completed = 0
    with ThreadPoolExecutor(max_workers=min(2, max(1, workers))) as pool:
        pending = {pool.submit(_acquire_bank, bank, raw_dir, end, run_id): bank for bank in banks}
        for future in as_completed(pending):
            bank = pending[future]
            try:
                result = future.result()
                rows.extend(result)
                print(f"BANK {bank['code']} {bank['name']}: {len(result)} reports", flush=True)
            except Exception as error:
                errors.append({"code": bank["code"], "error": str(error)})
                print(f"BANK {bank['code']} FAIL: {error}", flush=True)
            completed += 1
            if progress_path is not None:
                atomic_json(progress_path, {"status": "RUNNING", "group": "bank_free",
                            "phase": f"免费银行财报：{completed}/{len(banks)}家已采集",
                            "progress": round(completed / len(banks) * 90),
                            "completed_groups": 0, "total_groups": 1,
                            "bank_completed": completed, "bank_total": len(banks)})
    rows.sort(key=lambda r: (r["code"], r["report_date"], r["provider_update_date"] or ""))
    with (run_dir / "normalized.json").open("x", encoding="utf-8") as target:
        json.dump(rows, target, ensure_ascii=False, allow_nan=False)
    current = {}
    for row in rows:
        current[row["code"]] = row
    core_counts = {k: sum(row[k] is not None for row in current.values()) for k in CORE_METRICS}
    summary = {"schema_version": 1, "source_id": "eastmoney", "credential_required": False,
               "run_id": run_id, "run_directory": str(run_dir), "started_at_utc": started,
               "retrieved_at_utc": datetime.now(UTC).isoformat(), "requested_end": end.isoformat(),
               "expected_banks": len(banks), "bank_count": len(current), "history_rows": len(rows),
               "coverage": {"start": min((r["report_date"] for r in rows), default=None),
                            "end": max((r["report_date"] for r in rows), default=None)},
               "latest_metric_coverage": core_counts,
               "history_metric_coverage": {k: sum(row[k] is not None for row in rows) for k in METRICS},
               "current_complete_banks": sum(all(row[k] is not None for k in CORE_METRICS)
                                             for row in current.values()),
               "quality_flag_rows": sum(r["quality_flags"] != "[]" for r in rows),
               "missing_notice_rows": sum(r["provider_notice_date"] is None for r in rows),
               "metric_count": len(METRICS), "metrics": list(METRICS), "errors": errors,
               "published": False, "status": "FAIL" if errors else "ACQUIRED",
               "pit_grade": "CURRENT_SNAPSHOT_HISTORY_UNVERIFIED",
               "available_at_policy": "actual retrieval time; provider notice is preserved separately",
               "universe_scope": "current banks, not a historical tradable universe"}
    if not errors and len(current) == len(banks):
        if any(row["quality_flags"] != "[]" for row in current.values()):
            summary["status"] = "FAIL"
            summary["errors"].append({"error": "latest bank records failed basic quality checks"})
        else:
            summary["published"] = True
            summary["status"] = "PASS"
            try:
                publish(root, run_dir, rows, summary)
            except Exception as error:
                summary.update(published=False, status="FAIL")
                summary["errors"].append({"error": str(error)})
    with (run_dir / "summary.json").open("x", encoding="utf-8") as target:
        json.dump(summary, target, ensure_ascii=False, allow_nan=False, indent=2)
    if summary["status"] != "PASS":
        raise RuntimeError(f"bank update failed; previous published snapshot retained; see {run_dir / 'summary.json'}")
    print(json.dumps({"status": "PASS", "run_id": run_id, "banks": len(current), "rows": len(rows),
                      "core_coverage": core_counts}, ensure_ascii=False), flush=True)
    return summary


def bank_inventory(root: Path) -> dict[str, Any]:
    path = root / "data" / ARCHIVE_NAME / "checkpoint.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    coverage = value.get("coverage") or {}
    return {"start": coverage.get("start"), "end": coverage.get("end"),
            "last_collected_at": value.get("retrieved_at_utc"), "bank_count": value.get("bank_count", 0),
            "expected_banks": len(bank_universe(root)), "history_rows": value.get("history_rows", 0),
            "metric_coverage": value.get("latest_metric_coverage", {}),
            "current_complete_banks": value.get("current_complete_banks", 0),
            "quality_flag_rows": value.get("quality_flag_rows", 0),
            "missing_notice_rows": value.get("missing_notice_rows", 0),
            "published": bool(value.get("published") and (root / "data" / "warehouse" / WAREHOUSE_NAME).exists()),
            "snapshot_day": (datetime.fromisoformat(value["retrieved_at_utc"]).astimezone(
                timezone(timedelta(hours=8))).date().isoformat() if value.get("retrieved_at_utc") else None),
            "snapshot_through": value.get("requested_end"), "run_id": value.get("run_id"),
            "partitions": {"bank_reports": value.get("history_rows", 0)},
            "datasets": [{"id": k, "start": coverage.get("start"), "end": coverage.get("end"),
                          "partitions": value.get("history_metric_coverage", {}).get(k, 0)} for k in CORE_METRICS],
            "quality_label": "当期快照；历史披露版本待核验"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--min-free-gb", type=float, default=10)
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    sync(args.project_root.resolve(), args.end, workers=args.workers,
         min_free_gb=args.min_free_gb, progress_path=args.progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
