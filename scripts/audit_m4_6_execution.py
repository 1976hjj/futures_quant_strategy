"""Independently audit an immutable M4.6 execution evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from alpha_research_os.evaluation import ExecutionEvidenceManifest
from alpha_research_os.kernel.canonical import canonical_json_bytes

DEFAULT_EXECUTION_EVIDENCE_ID = "sha256:cbffb3e717b0ff4faa9aeac962c486d504080b9fb3b40f712d5367b00cf5ee9c"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def audit(database: Path, evidence_store: Path, evidence_id: str) -> dict[str, Any]:
    directory = evidence_store / "execution" / evidence_id.removeprefix("sha256:")
    manifest_path = directory / "manifest.json"
    manifest = ExecutionEvidenceManifest.model_validate_json(manifest_path.read_bytes())
    failures: list[str] = []
    findings: list[str] = []
    if manifest.execution_evidence_id != evidence_id:
        failures.append("requested and manifest execution evidence IDs differ")
    paths = {item.name: evidence_store / item.relative_path for item in manifest.files}
    with duckdb.connect() as connection:
        for item in manifest.files:
            path = paths[item.name]
            if _sha256_file(path) != item.artifact_hash:
                failures.append(f"artifact hash mismatch: {item.name}")
                continue
            count = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(path)}')").fetchone()[0]
            if count != item.row_count:
                failures.append(f"row count mismatch: {item.name}")
        daily = _sql_path(paths["daily_execution"])
        entity = _sql_path(paths["entity_summary"])
        rejection = _sql_path(paths["rejection_summary"])
        turnover = _sql_path(paths["turnover_summary"])
        score_count = connection.execute(f"SELECT count(DISTINCT score_id) FROM read_parquet('{entity}')").fetchone()[0]
        if score_count != manifest.score_count:
            failures.append("score count does not match manifest")
        if not connection.execute(
            f"SELECT bool_or(score_id='equal-weight-rank-combination') FROM read_parquet('{entity}')"
        ).fetchone()[0]:
            failures.append("simple combination score is missing")
        cost_violations = connection.execute(
            f"""
            SELECT count(*) FROM read_parquet('{daily}')
            WHERE net_return > gross_return OR average_cost_bps < 0 OR filled_count > selected_count
            """
        ).fetchone()[0]
        if cost_violations:
            failures.append(f"daily cost or count invariants failed: {cost_violations}")
        daily_selected = connection.execute(
            f"SELECT capital_cny, sum(selected_count) FROM read_parquet('{daily}') GROUP BY 1 ORDER BY 1"
        ).fetchall()
        rejection_total = connection.execute(
            f"SELECT capital_cny, sum(order_count) FROM read_parquet('{rejection}') GROUP BY 1 ORDER BY 1"
        ).fetchall()
        if daily_selected != rejection_total:
            failures.append("rejection outcomes do not reconcile to selected orders")
        summary_drift = connection.execute(
            f"""
            WITH rebuilt AS (
              SELECT score_id, score_version, capital_cny,
                     sum(filled_count)::DOUBLE/nullif(sum(selected_count),0) AS fill_rate,
                     avg(net_return) AS average_daily_net_return,
                     avg(average_cost_bps) AS average_cost_bps
              FROM read_parquet('{daily}') GROUP BY 1,2,3
            ) SELECT count(*) FROM rebuilt r JOIN read_parquet('{entity}') e USING(score_id,score_version,capital_cny)
              WHERE abs(r.fill_rate-e.fill_rate)>1e-12
                 OR abs(r.average_daily_net_return-e.average_daily_net_return)>1e-12
                 OR abs(r.average_cost_bps-e.average_cost_bps)>1e-12
            """
        ).fetchone()[0]
        if summary_drift:
            failures.append(f"entity summaries differ from independent aggregation: {summary_drift}")
        monotonic_violations = connection.execute(
            f"""
            WITH ordered AS (
              SELECT *, lag(fill_rate) OVER (PARTITION BY score_id ORDER BY capital_cny) AS prior_fill,
                        lag(average_cost_bps) OVER (PARTITION BY score_id ORDER BY capital_cny) AS prior_cost
              FROM read_parquet('{entity}')
            ) SELECT count(*) FROM ordered
              WHERE fill_rate > prior_fill + 1e-12 OR average_cost_bps < prior_cost - 1e-12
            """
        ).fetchone()[0]
        if monotonic_violations:
            failures.append(f"capital sensitivity is not monotonic: {monotonic_violations}")
        turnover_violations = connection.execute(
            f"SELECT count(*) FROM read_parquet('{turnover}') WHERE one_way_turnover < 0 OR one_way_turnover > 1"
        ).fetchone()[0]
        if turnover_violations:
            failures.append(f"turnover outside [0,1]: {turnover_violations}")
        delisting_unavailable = connection.execute(
            f"SELECT coalesce(sum(order_count),0) FROM read_parquet('{rejection}') "
            "WHERE outcome='DELISTING_RETURN_UNAVAILABLE'"
        ).fetchone()[0]
        findings.append(f"Explicitly invalid delisting-return orders: {delisting_unavailable}")
        capital_summary = connection.execute(
            f"""
            SELECT capital_cny, avg(fill_rate), avg(average_daily_net_return), avg(average_cost_bps)
            FROM read_parquet('{entity}') GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
    with duckdb.connect(str(database), read_only=True) as connection:
        hashes = connection.execute(
            "SELECT DISTINCT core_checkpoint_hash FROM metadata.m2e_core_archive_manifest"
        ).fetchall()
        if hashes != [(manifest.request.m2e_core_checkpoint_hash,)]:
            failures.append("warehouse M2-E core vintage differs from execution request")
    report = {
        "schema_version": "1",
        "audited_at": datetime.now().astimezone().isoformat(),
        "execution_evidence_id": evidence_id,
        "status": "FAIL" if failures else "PASS_WITH_FINDINGS",
        "score_count": score_count,
        "capital_summary": [
            {
                "capital_cny": row[0],
                "mean_fill_rate": row[1],
                "mean_daily_net_return": row[2],
                "mean_cost_bps": row[3],
            }
            for row in capital_summary
        ],
        "findings": findings,
        "failures": failures,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--execution-evidence-id", default=DEFAULT_EXECUTION_EVIDENCE_ID)
    parser.add_argument("--report", type=Path, default=Path("reports/m4_6_execution_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.evidence_store, args.execution_evidence_id)
    _atomic_write(args.report, canonical_json_bytes(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
