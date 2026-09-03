"""Publish M4.3 HAC, block-bootstrap, stability, and BH-FDR evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_research_os.evaluation import (
    EvidenceBundleManifest,
    EvidenceFile,
    EvidenceInputRef,
    RobustnessEvidenceManifest,
    RobustnessEvidenceRequest,
    StatisticalInferenceSpec,
    benjamini_hochberg,
    moving_block_bootstrap_mean,
    newey_west_mean_test,
    stability_diagnostic,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash

ENGINE_VERSION = "python-m4-3-time-series-inference-1.0.0"
FAMILY_ID = "M4-3-Q1-RANKIC-ALL-FACTORS-ALL-VARIANTS-v1"
DEFAULT_EVIDENCE_IDS = (
    "sha256:818115f4bfd6e8b7bc5e6f09b02dfaeb8638778348399429da695a1cd7383766",
    "sha256:84e278c977fe28e3277f4cd910cec9ca303844148a60c2da3edf3569e65d2528",
    "sha256:8b0aa51eed07843f79860f7f7e9098220f70caab8e85629f889b72a58b398cfc",
)


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


def _load_inputs(
    evidence_store: Path, evidence_ids: tuple[str, ...]
) -> tuple[tuple[EvidenceInputRef, ...], dict[str, tuple[EvidenceBundleManifest, Path]]]:
    loaded: dict[str, tuple[EvidenceBundleManifest, Path]] = {}
    references: list[EvidenceInputRef] = []
    label_ids: set[str] = set()
    for evidence_id in evidence_ids:
        directory = evidence_store / "bundles" / evidence_id.removeprefix("sha256:")
        manifest_path = directory / "manifest.json"
        manifest = EvidenceBundleManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.evidence_id != evidence_id:
            raise ValueError("evidence input identity differs from its manifest")
        daily = evidence_store / next(item.relative_path for item in manifest.files if item.name == "daily_metrics")
        expected_hash = next(item.artifact_hash for item in manifest.files if item.name == "daily_metrics")
        if _sha256_file(daily) != expected_hash:
            raise ValueError("daily evidence file differs from its immutable manifest")
        label_ids.add(manifest.request.label_release_id)
        reference = EvidenceInputRef(
            evidence_id=evidence_id,
            evidence_manifest_hash=content_hash(manifest),
            factor_release_id=manifest.request.factor_release_id,
            factor_variant=manifest.request.factor_variant,
        )
        references.append(reference)
        loaded[manifest.request.factor_variant] = (manifest, daily)
    if len(label_ids) != 1:
        raise ValueError("M4.3 input evidence must use one identical label release")
    if len(loaded) != len(evidence_ids):
        raise ValueError("M4.3 input evidence variants must be unique")
    ordered = tuple(sorted(references, key=lambda item: (item.factor_variant, item.evidence_id)))
    return ordered, loaded


def _seed(base_seed: int, variant: str, factor_id: str, factor_version: str) -> int:
    payload = f"{base_seed}|{variant}|{factor_id}|{factor_version}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def _daily_series(path: Path) -> dict[tuple[str, str], list[tuple[date, float | None]]]:
    grouped: dict[tuple[str, str], list[tuple[date, float | None]]] = defaultdict(list)
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT factor_id,factor_version,session,rank_ic
            FROM read_parquet('{_sql_path(path)}') ORDER BY factor_id,factor_version,session"""
        ).fetchall()
    for factor_id, factor_version, session, rank_ic in rows:
        grouped[(factor_id, factor_version)].append((session, rank_ic))
    return grouped


def _calculate(
    request: RobustnessEvidenceRequest,
    loaded: dict[str, tuple[EvidenceBundleManifest, Path]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    spec = request.inference
    hypotheses: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for variant in sorted(loaded):
        manifest, daily_path = loaded[variant]
        for (factor_id, factor_version), observations in _daily_series(daily_path).items():
            values = [item[1] for item in observations]
            seed = _seed(spec.random_seed, variant, factor_id, factor_version)
            hac = newey_west_mean_test(values, max_lag=spec.hac_max_lag)
            bootstrap = moving_block_bootstrap_mean(
                values,
                block_length=spec.bootstrap_block_length,
                resamples=spec.bootstrap_resamples,
                seed=seed,
                confidence_level=spec.bootstrap_confidence_level,
            )
            stability = stability_diagnostic(observations, segment_count=spec.stability_segments)
            present = [float(value) for value in values if value is not None and math.isfinite(value)]
            hypotheses.append(
                {
                    "robustness_id": request.robustness_id,
                    "multiple_testing_family_id": request.multiple_testing_family_id,
                    "evidence_id": manifest.evidence_id,
                    "factor_release_id": manifest.request.factor_release_id,
                    "label_release_id": request.label_release_id,
                    "variant": variant,
                    "factor_id": factor_id,
                    "factor_version": factor_version,
                    "rank_ic_sessions": len(present),
                    "mean_rank_ic": hac.mean,
                    "rank_ic_stddev_pop": statistics.pstdev(present) if len(present) > 1 else None,
                    "hac_max_lag": hac.max_lag,
                    "hac_standard_error": hac.standard_error,
                    "hac_z_statistic": (
                        hac.z_statistic
                        if hac.z_statistic is not None and math.isfinite(hac.z_statistic)
                        else None
                    ),
                    "hac_p_value_two_sided": hac.p_value_two_sided,
                    "bootstrap_method": spec.bootstrap_method,
                    "bootstrap_block_length": bootstrap.block_length,
                    "bootstrap_resamples": bootstrap.resamples,
                    "bootstrap_seed": bootstrap.seed,
                    "bootstrap_p_value_two_sided": bootstrap.p_value_two_sided,
                    "bootstrap_confidence_lower": bootstrap.confidence_lower,
                    "bootstrap_confidence_upper": bootstrap.confidence_upper,
                    "stability_segment_count": len(stability.segments),
                    "same_sign_segment_fraction": stability.same_sign_fraction,
                    "worst_segment_mean_rank_ic": stability.worst_segment_mean,
                    "segment_mean_range": stability.segment_range,
                    "evidence_status": "SHORT_WINDOW_DIAGNOSTIC_ONLY",
                }
            )
            for item in stability.segments:
                segments.append(
                    {
                        "robustness_id": request.robustness_id,
                        "variant": variant,
                        "factor_id": factor_id,
                        "factor_version": factor_version,
                        "segment": item.segment,
                        "start_session": item.start_session,
                        "end_session": item.end_session,
                        "observations": item.observations,
                        "mean_rank_ic": item.mean,
                    }
                )
    hypotheses.sort(key=lambda item: (item["variant"], item["factor_id"], item["factor_version"]))
    bootstrap_q = benjamini_hochberg([item["bootstrap_p_value_two_sided"] for item in hypotheses])
    hac_q = benjamini_hochberg([item["hac_p_value_two_sided"] for item in hypotheses])
    for item, bootstrap_value, hac_value in zip(hypotheses, bootstrap_q, hac_q, strict=True):
        item["bootstrap_bh_q_value"] = bootstrap_value
        item["hac_bh_q_value"] = hac_value
        item["bootstrap_fdr_reject"] = bootstrap_value is not None and bootstrap_value <= spec.fdr_alpha
        item["hac_fdr_reject"] = hac_value is not None and hac_value <= spec.fdr_alpha
    segments.sort(key=lambda item: (item["variant"], item["factor_id"], item["factor_version"], item["segment"]))
    summary = {
        "robustness_id": request.robustness_id,
        "multiple_testing_family_id": request.multiple_testing_family_id,
        "primary_metric": spec.primary_metric,
        "alternative": spec.alternative,
        "hypothesis_count": len(hypotheses),
        "variant_count": len(loaded),
        "factor_identity_count": len({(item["factor_id"], item["factor_version"]) for item in hypotheses}),
        "bootstrap_fdr_rejection_count": sum(item["bootstrap_fdr_reject"] for item in hypotheses),
        "hac_fdr_rejection_count": sum(item["hac_fdr_reject"] for item in hypotheses),
        "minimum_bootstrap_p_value": min(item["bootstrap_p_value_two_sided"] for item in hypotheses),
        "minimum_bootstrap_bh_q_value": min(item["bootstrap_bh_q_value"] for item in hypotheses),
        "minimum_hac_p_value": min(item["hac_p_value_two_sided"] for item in hypotheses),
        "minimum_hac_bh_q_value": min(item["hac_bh_q_value"] for item in hypotheses),
        "fdr_alpha": spec.fdr_alpha,
        "decision_status": "DIAGNOSTIC_ONLY_NOT_OOS",
    }
    return hypotheses, segments, summary


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd", compression_level=6)


def _quality(paths: dict[str, Path], expected_hypotheses: int, expected_segments: int) -> None:
    with duckdb.connect() as connection:
        hypotheses = f"read_parquet('{_sql_path(paths['hypothesis_statistics'])}')"
        row = connection.execute(
            f"""SELECT count(*),count(DISTINCT (variant,factor_id,factor_version)),
              count(*) FILTER (WHERE mean_rank_ic IS NOT NULL AND NOT isfinite(mean_rank_ic)),
              count(*) FILTER (WHERE bootstrap_p_value_two_sided<0 OR bootstrap_p_value_two_sided>1
                OR bootstrap_bh_q_value<0 OR bootstrap_bh_q_value>1
                OR hac_p_value_two_sided<0 OR hac_p_value_two_sided>1
                OR hac_bh_q_value<0 OR hac_bh_q_value>1)
            FROM {hypotheses}"""
        ).fetchone()
        segment_count = connection.execute(
            f"SELECT count(*) FROM read_parquet('{_sql_path(paths['stability_segments'])}')"
        ).fetchone()[0]
    if row != (expected_hypotheses, expected_hypotheses, 0, 0) or segment_count != expected_segments:
        raise ValueError(f"M4.3 quality gate failed hypotheses={row} segments={segment_count}")


def _register(database: Path, evidence_store: Path, manifest: RobustnessEvidenceManifest) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
        connection.execute("CREATE SCHEMA IF NOT EXISTS research")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.robustness_evidence_manifest (
            robustness_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, multiple_testing_family_id VARCHAR,
            label_release_id VARCHAR, hypothesis_count BIGINT, quality_status VARCHAR,
            decision_status VARCHAR, request_json JSON, limitations_json JSON, created_at TIMESTAMPTZ)"""
        )
        manifest_hash = content_hash(manifest)
        existing = connection.execute(
            "SELECT manifest_hash FROM metadata.robustness_evidence_manifest WHERE robustness_id=?",
            [manifest.robustness_id],
        ).fetchone()
        if existing and existing != (manifest_hash,):
            raise ValueError("immutable M4.3 registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.robustness_evidence_manifest VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                manifest.robustness_id,
                manifest_hash,
                manifest.request.multiple_testing_family_id,
                manifest.request.label_release_id,
                manifest.hypothesis_count,
                manifest.quality_status,
                manifest.decision_status,
                json.dumps(manifest.request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                json.dumps(manifest.limitations, ensure_ascii=False, separators=(",", ":")),
                manifest.created_at,
            ],
        )
        root = _sql_path(evidence_store / "robustness" / "*")
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_robustness_statistics AS
            SELECT * FROM read_parquet('{root}/hypothesis_statistics.parquet', union_by_name=true)"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.factor_robustness_summary AS
            SELECT * FROM raw.factor_robustness_statistics"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_stability_segments AS
            SELECT * FROM read_parquet('{root}/stability_segments.parquet', union_by_name=true)"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW research.multiple_testing_family_summary AS
            SELECT * FROM read_parquet('{root}/family_summary.parquet', union_by_name=true)"""
        )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def publish(database: Path, evidence_store: Path, evidence_ids: tuple[str, ...]) -> dict[str, Any]:
    references, loaded = _load_inputs(evidence_store, evidence_ids)
    label_release_id = next(iter(manifest.request.label_release_id for manifest, _ in loaded.values()))
    request = RobustnessEvidenceRequest(
        engine_version=ENGINE_VERSION,
        multiple_testing_family_id=FAMILY_ID,
        label_release_id=label_release_id,
        evidence_inputs=references,
        inference=StatisticalInferenceSpec(),
    )
    directory = evidence_store / "robustness" / request.robustness_id.removeprefix("sha256:")
    manifest_path = directory / "manifest.json"
    targets = {
        "family_summary": directory / "family_summary.parquet",
        "hypothesis_statistics": directory / "hypothesis_statistics.parquet",
        "stability_segments": directory / "stability_segments.parquet",
    }
    if manifest_path.exists() and all(path.exists() for path in targets.values()):
        manifest = RobustnessEvidenceManifest.model_validate_json(manifest_path.read_bytes())
        hashes = {item.name: item.artifact_hash for item in manifest.files}
        if manifest.request != request or any(_sha256_file(path) != hashes[name] for name, path in targets.items()):
            raise ValueError("cached M4.3 robustness release failed immutable verification")
        _register(database, evidence_store, manifest)
        return {"cache_hit": True, "robustness_id": manifest.robustness_id, "manifest": str(manifest_path.resolve())}
    directory.mkdir(parents=True, exist_ok=True)
    hypotheses, segments, summary = _calculate(request, loaded)
    temporary = {
        name: path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.parquet") for name, path in targets.items()
    }
    _write_parquet(temporary["hypothesis_statistics"], hypotheses)
    _write_parquet(temporary["stability_segments"], segments)
    _write_parquet(temporary["family_summary"], [summary])
    _quality(temporary, len(hypotheses), len(segments))
    files = []
    for name, path in sorted(targets.items()):
        os.replace(temporary[name], path)
        with duckdb.connect() as connection:
            row_count = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(path)}')").fetchone()[0]
        files.append(
            EvidenceFile(
                name=name,
                relative_path=path.relative_to(evidence_store).as_posix(),
                artifact_hash=_sha256_file(path),
                row_count=row_count,
            )
        )
    manifest = RobustnessEvidenceManifest(
        robustness_id=request.robustness_id,
        request=request,
        created_at=datetime.now().astimezone(),
        files=tuple(files),
        hypothesis_count=len(hypotheses),
        quality_status="PASS",
        decision_status="DIAGNOSTIC_ONLY_NOT_OOS",
        limitations=(
            "Only 58 Q1 2024 sessions are observed; stability segments are short-window diagnostics.",
            "The provisional label is not price-limit, delisting-return, or transaction-cost aware.",
            "BH-FDR is applied to correlated factor variants and does not substitute for OOS validation.",
            "HAC uses an asymptotic standard-normal reference; block-bootstrap inference is also reported.",
        ),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _register(database, evidence_store, manifest)
    return {
        "cache_hit": False,
        "robustness_id": manifest.robustness_id,
        "hypothesis_count": manifest.hypothesis_count,
        "bootstrap_fdr_rejection_count": summary["bootstrap_fdr_rejection_count"],
        "hac_fdr_rejection_count": summary["hac_fdr_rejection_count"],
        "minimum_bootstrap_bh_q_value": summary["minimum_bootstrap_bh_q_value"],
        "minimum_hac_bh_q_value": summary["minimum_hac_bh_q_value"],
        "decision_status": manifest.decision_status,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--evidence-id", action="append", dest="evidence_ids")
    args = parser.parse_args()
    evidence_ids = tuple(args.evidence_ids) if args.evidence_ids else DEFAULT_EVIDENCE_IDS
    print(json.dumps(publish(args.database, args.evidence_store, evidence_ids), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
