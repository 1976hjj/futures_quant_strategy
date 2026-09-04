"""Independently audit one generated Factor Evidence Explorer bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpha_research_os.kernel.canonical import content_hash  # noqa: E402


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def audit(report_directory: Path, database: Path, evidence_store: Path) -> dict[str, Any]:
    failures: list[str] = []
    findings = [
        "The Explorer is a read-only derived report, not a promotion decision or Evidence Asset.",
        "Static integrity checks do not replace browser rendering and human visual review.",
    ]
    manifest_path = report_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = json.loads((report_directory / "evidence-summary.json").read_text(encoding="utf-8"))

    if content_hash(manifest["request"]) != manifest["report_id"]:
        failures.append("report_id does not match the canonical request hash")
    for item in manifest["files"]:
        path = report_directory / item["path"]
        if not path.exists() or _sha256_file(path) != item["sha256"]:
            failures.append(f"file hash mismatch: {item['path']}")
        elif path.stat().st_size != item["size_bytes"]:
            failures.append(f"file size mismatch: {item['path']}")
    if payload["report"]["report_id"] != manifest["report_id"]:
        failures.append("snapshot and manifest report IDs differ")
    if manifest["decision_status"] != "READ_ONLY_DERIVED_REPORT_NOT_EVIDENCE":
        failures.append("report decision status is not the required read-only status")

    factors = payload["factors"]
    summary = payload["summary"]
    observed_summary = {
        "entity_count": len(factors),
        "factor_count": len({item["factor_id"] for item in factors}),
        "canonical_count": sum(item["deduplication"].get("is_canonical", True) for item in factors),
        "cluster_count": len(payload["clusters"]),
        "integrity_blocker_count": sum("QUARANTINED_INTEGRITY_FAILURE" in item["routes"] for item in factors),
        "execution_available_count": sum(item["execution"]["status"] == "AVAILABLE" for item in factors),
    }
    if summary != observed_summary or manifest["summary"] != observed_summary:
        failures.append("summary counts do not match the snapshot contents")

    duplicate_paths = [item for item in factors if not item["deduplication"].get("is_canonical", True)]
    if any(item["incremental"] is not None for item in duplicate_paths):
        failures.append("noncanonical paths incorrectly contain path-level incremental evidence")
    if any(item["canonical_incremental"] is None for item in duplicate_paths):
        failures.append("noncanonical paths cannot resolve their canonical incremental evidence")
    if any(item["execution"]["status"] != "NOT_AVAILABLE" for item in factors):
        failures.append("M4.6 execution placeholders are not consistently NOT_AVAILABLE")
    if any(item["model_contribution"]["status"] != "NOT_AVAILABLE" for item in factors):
        failures.append("M6 model placeholders are not consistently NOT_AVAILABLE")

    source = {item["kind"]: item for item in manifest["request"]["source_manifests"]}
    walk_forward_id = payload["report"]["walk_forward_id"]
    redundancy_id = payload["report"]["redundancy_id"]
    if source["WALK_FORWARD"]["asset_id"] != walk_forward_id:
        failures.append("walk-forward lineage differs between request and snapshot")
    if source["REDUNDANCY"]["asset_id"] != redundancy_id:
        failures.append("redundancy lineage differs between request and snapshot")

    redundancy_manifest_path = evidence_store / "redundancy" / redundancy_id.removeprefix("sha256:") / "manifest.json"
    redundancy_manifest = json.loads(redundancy_manifest_path.read_text(encoding="utf-8"))
    artifact_paths = {item["name"]: evidence_store / item["relative_path"] for item in redundancy_manifest["files"]}
    with duckdb.connect(str(database), read_only=True) as connection:
        expected_entities = connection.execute(
            """SELECT count(*) FROM (
            SELECT DISTINCT variant,factor_id,factor_version
            FROM research.factor_walk_forward_decisions WHERE walk_forward_id=?)""",
            [walk_forward_id],
        ).fetchone()[0]
        expected_folds = connection.execute(
            "SELECT count(*) FROM research.factor_walk_forward_decisions WHERE walk_forward_id=?",
            [walk_forward_id],
        ).fetchone()[0]
        expected_regimes = connection.execute(
            "SELECT count(*) FROM raw.factor_regime_statistics WHERE walk_forward_id=?",
            [walk_forward_id],
        ).fetchone()[0]
        expected_incremental = connection.execute(
            f"SELECT count(*) FROM read_parquet('{_sql_path(artifact_paths['incremental_value_summary'])}')"
        ).fetchone()[0]
        expected_correlations = connection.execute(
            f"SELECT count(*) FROM read_parquet('{_sql_path(artifact_paths['pair_correlations'])}')"
        ).fetchone()[0]

    observed_counts = {
        "entities": len(factors),
        "folds": sum(len(item["folds"]) for item in factors),
        "regimes": sum(len(item["regimes"]) for item in factors),
        "incremental": sum(item["incremental"] is not None for item in factors),
        "correlations": len(payload["correlations"]),
    }
    expected_counts = {
        "entities": expected_entities,
        "folds": expected_folds,
        "regimes": expected_regimes,
        "incremental": expected_incremental,
        "correlations": expected_correlations,
    }
    if observed_counts != expected_counts:
        failures.append(f"snapshot/source row counts differ: {observed_counts} != {expected_counts}")

    local_text = "\n".join(
        (report_directory / name).read_text(encoding="utf-8") for name in ("index.html", "app.css", "app.js")
    ).lower()
    if "http://" in local_text or "https://" in local_text:
        failures.append("frontend contains an external network reference")
    if "tushare token" in local_text or "tushare_token" in local_text:
        failures.append("frontend contains a forbidden credential marker")

    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "report_id": manifest["report_id"],
        "report_directory": str(report_directory.resolve()),
        "observed_counts": observed_counts,
        "duplicate_path_count": len(duplicate_paths),
        "findings": findings,
        "failures": failures,
        "status": "FAIL" if failures else "PASS_WITH_FINDINGS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    args = parser.parse_args()
    report_directory = (
        args.report_directory if args.report_directory.is_absolute() else PROJECT_ROOT / args.report_directory
    )
    database = args.database if args.database.is_absolute() else PROJECT_ROOT / args.database
    evidence_store = args.evidence_store if args.evidence_store.is_absolute() else PROJECT_ROOT / args.evidence_store
    result = audit(report_directory, database, evidence_store)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
