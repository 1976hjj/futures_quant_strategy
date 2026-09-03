"""Independently audit the immutable M4.3 statistical evidence release."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime
from pathlib import Path

import duckdb

from alpha_research_os.evaluation import EvidenceBundleManifest, RobustnessEvidenceManifest
from alpha_research_os.kernel.canonical import content_hash

DEFAULT_ROBUSTNESS_ID = "sha256:a8368caf70682a1918fb3f2c7380e510b2b62a169db1e142a603d0d601337eaa"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _close(left: float | None, right: float | None, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def _independent_hac(values: list[float], max_lag: int) -> tuple[float, float, float, float]:
    count = len(values)
    mean = sum(values) / count
    centered = [value - mean for value in values]
    lag = min(max_lag, count - 1)
    long_run = sum(value * value for value in centered) / count
    for offset in range(1, lag + 1):
        covariance = sum(centered[index] * centered[index - offset] for index in range(offset, count)) / count
        long_run += 2 * (1 - offset / (lag + 1)) * covariance
    standard_error = math.sqrt(max(0.0, long_run) / count)
    statistic = mean / standard_error
    return mean, standard_error, statistic, math.erfc(abs(statistic) / math.sqrt(2))


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _independent_bootstrap(
    values: list[float], block_length: int, resamples: int, seed: int, confidence: float
) -> tuple[float, float, float]:
    rng = random.Random(seed)
    count = len(values)
    observed = sum(values) / count
    means = []
    extreme = 0
    for _ in range(resamples):
        sample = []
        while len(sample) < count:
            start = rng.randrange(count)
            sample.extend(values[(start + offset) % count] for offset in range(block_length))
        replicate = sum(sample[:count]) / count
        means.append(replicate)
        extreme += abs(replicate - observed) >= abs(observed)
    tail = (1 - confidence) / 2
    return (extreme + 1) / (resamples + 1), _percentile(means, tail), _percentile(means, 1 - tail)


def _independent_bh(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = [0.0] * len(values)
    running = 1.0
    for index in range(len(ordered) - 1, -1, -1):
        original, value = ordered[index]
        running = min(running, value * len(ordered) / (index + 1))
        result[original] = min(1.0, running)
    return result


def audit(database: Path, evidence_store: Path, robustness_id: str) -> dict[str, object]:
    failures: list[str] = []
    directory = evidence_store / "robustness" / robustness_id.removeprefix("sha256:")
    manifest = RobustnessEvidenceManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if manifest.robustness_id != robustness_id:
        failures.append("manifest robustness identity differs from request")
    paths = {item.name: evidence_store / item.relative_path for item in manifest.files}
    for item in manifest.files:
        if _sha256_file(paths[item.name]) != item.artifact_hash:
            failures.append(f"artifact hash mismatch: {item.name}")
    for reference in manifest.request.evidence_inputs:
        input_directory = evidence_store / "bundles" / reference.evidence_id.removeprefix("sha256:")
        input_manifest = EvidenceBundleManifest.model_validate_json((input_directory / "manifest.json").read_bytes())
        if content_hash(input_manifest) != reference.evidence_manifest_hash:
            failures.append(f"input evidence manifest hash mismatch: {reference.factor_variant}")

    hypothesis_path = paths["hypothesis_statistics"]
    segment_path = paths["stability_segments"]
    family_path = paths["family_summary"]
    with duckdb.connect(str(database), read_only=True) as connection:
        registered = connection.execute(
            "SELECT manifest_hash FROM metadata.robustness_evidence_manifest WHERE robustness_id=?",
            [robustness_id],
        ).fetchone()
        if registered != (content_hash(manifest),):
            failures.append("DuckDB robustness registry differs from manifest")
        dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (variant,factor_id,factor_version)),
              count(*) FILTER (WHERE bootstrap_p_value_two_sided NOT BETWEEN 0 AND 1
                OR bootstrap_bh_q_value NOT BETWEEN 0 AND 1 OR hac_p_value_two_sided NOT BETWEEN 0 AND 1
                OR hac_bh_q_value NOT BETWEEN 0 AND 1),
              sum(bootstrap_fdr_reject::INTEGER),sum(hac_fdr_reject::INTEGER)
            FROM read_parquet('{_sql_path(hypothesis_path)}')"""
        ).fetchone()
        if dimensions[:3] != (manifest.hypothesis_count, manifest.hypothesis_count, 0):
            failures.append("hypothesis dimensions, uniqueness, or probability bounds failed")
        segment_count = connection.execute(
            f"SELECT count(*) FROM read_parquet('{_sql_path(segment_path)}')"
        ).fetchone()[0]
        if segment_count != manifest.hypothesis_count * manifest.request.inference.stability_segments:
            failures.append("stability segment dimensions differ from frozen specification")
        family = connection.execute(f"SELECT * FROM read_parquet('{_sql_path(family_path)}')").fetchone()
        if family is None or family[4] != manifest.hypothesis_count:
            failures.append("multiple-testing family summary is missing or incomplete")
        rows = connection.execute(
            f"""SELECT variant,factor_id,factor_version,hac_p_value_two_sided,bootstrap_p_value_two_sided,
              hac_bh_q_value,bootstrap_bh_q_value FROM read_parquet('{_sql_path(hypothesis_path)}')
              ORDER BY variant,factor_id,factor_version"""
        ).fetchall()
        expected_hac_q = _independent_bh([row[3] for row in rows])
        expected_bootstrap_q = _independent_bh([row[4] for row in rows])
        if any(
            not _close(row[5], hac_q) or not _close(row[6], bootstrap_q)
            for row, hac_q, bootstrap_q in zip(rows, expected_hac_q, expected_bootstrap_q, strict=True)
        ):
            failures.append("independent BH-FDR calculation differs from stored q-values")

        target = connection.execute(
            f"""SELECT * FROM read_parquet('{_sql_path(hypothesis_path)}')
            WHERE variant='RAW' AND factor_id='book-to-price'"""
        ).fetchone()
        target_columns = [item[0] for item in connection.description]
        target_map = dict(zip(target_columns, target, strict=True))
        evidence_id = target_map["evidence_id"]
        input_manifest = EvidenceBundleManifest.model_validate_json(
            (
                evidence_store / "bundles" / evidence_id.removeprefix("sha256:") / "manifest.json"
            ).read_bytes()
        )
        daily_path = evidence_store / next(
            item.relative_path for item in input_manifest.files if item.name == "daily_metrics"
        )
        values = [
            row[0]
            for row in connection.execute(
                f"""SELECT rank_ic FROM read_parquet('{_sql_path(daily_path)}')
                WHERE factor_id='book-to-price' AND rank_ic IS NOT NULL ORDER BY session"""
            ).fetchall()
        ]
        hac = _independent_hac(values, manifest.request.inference.hac_max_lag)
        stored_hac = (
            target_map["mean_rank_ic"],
            target_map["hac_standard_error"],
            target_map["hac_z_statistic"],
            target_map["hac_p_value_two_sided"],
        )
        if any(not _close(left, right) for left, right in zip(hac, stored_hac, strict=True)):
            failures.append("independent Newey-West calculation differs from stored result")
        bootstrap = _independent_bootstrap(
            values,
            target_map["bootstrap_block_length"],
            target_map["bootstrap_resamples"],
            target_map["bootstrap_seed"],
            manifest.request.inference.bootstrap_confidence_level,
        )
        stored_bootstrap = (
            target_map["bootstrap_p_value_two_sided"],
            target_map["bootstrap_confidence_lower"],
            target_map["bootstrap_confidence_upper"],
        )
        if any(not _close(left, right) for left, right in zip(bootstrap, stored_bootstrap, strict=True)):
            failures.append("independent moving-block bootstrap differs from stored result")

    limitations = " ".join(manifest.limitations).lower()
    for phrase in ("58", "price-limit", "correlated", "standard-normal"):
        if phrase not in limitations:
            failures.append(f"required statistical limitation is missing: {phrase}")
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "robustness_id": robustness_id,
        "hypothesis_count": manifest.hypothesis_count,
        "stability_segment_rows": segment_count,
        "bootstrap_fdr_rejection_count": dimensions[3],
        "hac_fdr_rejection_count": dimensions[4],
        "crosscheck_variant": "RAW",
        "crosscheck_factor": "book-to-price",
        "failures": failures,
        "status": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--robustness-id", default=DEFAULT_ROBUSTNESS_ID)
    parser.add_argument("--output", type=Path, default=Path("reports/m4_3_robustness_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.evidence_store, args.robustness_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
